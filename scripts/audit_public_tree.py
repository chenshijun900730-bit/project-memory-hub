from __future__ import annotations

import argparse
import hashlib
import html
import importlib
import json
import os
import pwd
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import tomllib
import unicodedata
import urllib.parse
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn, cast
from uuid import UUID

from project_memory_hub.demo.privacy import (
    PrivacyLimits,
    PrivacyPolicy,
    PrivacyViolation,
    scan_document,
)
from project_memory_hub.demo.seed import SYNTHETIC_UUIDS

try:
    _asset_verifier = importlib.import_module("scripts.verify_public_assets")
except ModuleNotFoundError:  # pragma: no cover - direct script execution
    _asset_verifier = importlib.import_module("verify_public_assets")
_verify_public_assets = cast(
    Callable[..., dict[str, Any]],
    _asset_verifier.verify_public_assets,
)


_AUDITOR = "project-memory-hub-public-tree"
_EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_TOKEN_PATTERNS = (
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?i)(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"(?i)AWS_SECRET_ACCESS_KEY\s*[:=]\s*[A-Za-z0-9/+=]{32,}"),
    re.compile(r"(?i)xox[bpars]-[A-Za-z0-9-]{20,}"),
    re.compile(r"(?i)xapp-[A-Za-z0-9-]{20,}"),
)
_NAMED_TOKEN = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])"
    r"(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*(?P<value>\S{8,})"
)
_PRIVATE_KEY_BEGIN = re.compile(
    r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
)
_GENERIC_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
_SESSION_BODY = re.compile(
    r"(?is)[\"']type[\"']\s*:\s*[\"']"
    r"(?:session_meta|response_item|event_msg|turn_context)[\"']"
)
_DATABASE_CREATE = re.compile(r"(?im)^\s*CREATE\s+TABLE\b")
_DATABASE_INSERT = re.compile(r"(?im)^\s*INSERT\s+INTO\b")
_DATABASE_DUMP_MARKER = re.compile(r"(?i)(?:database|sqlite|postgres(?:ql)?)\s+dump")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_WEBP_SIGNATURE = b"RIFF"
_SQLITE_SIGNATURE = b"SQLite format 3\x00"
_FORBIDDEN_MAX_BYTES = 64 * 1024
_MAX_FILES = 20_000
_MAX_FILE_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_BYTES = 512 * 1024 * 1024
_MAX_PATH_BYTES = 4_096
_MAX_TERM_CHARS = 512
_GIT_TIMEOUT_SECONDS = 30.0
_DIRFD_SUPPORTED = all(
    operation in os.supports_dir_fd for operation in (os.open, os.stat, os.link, os.unlink)
)
_WINDOWS_RESERVED = frozenset(
    {
        "aux",
        "con",
        "nul",
        "prn",
        *(f"com{index}" for index in range(1, 10)),
        *(f"lpt{index}" for index in range(1, 10)),
    }
)
_ALLOWLIST_RULES = frozenset(
    {
        "binary_unsupported",
        "database_dump",
        "forbidden_term",
        "home_prefix",
        "session_body",
        "token_like",
        "unknown_uuid",
    }
)
_PRIVACY_LIMITS = PrivacyLimits(
    max_files=_MAX_FILES,
    max_file_bytes=_MAX_FILE_BYTES,
    max_total_bytes=_MAX_TOTAL_BYTES,
    max_decoded_chars=_MAX_FILE_BYTES,
    max_metadata_bytes=64 * 1024,
    max_pixels=8_000_000,
)


class PublicTreeAuditError(ValueError):
    """A public tree failed with a stable, non-disclosing aggregate report."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = report
        super().__init__("public_tree_audit_failed")


class _GitObjectError(ValueError):
    pass


class _PolicyError(ValueError):
    def __init__(self, rule: str) -> None:
        self.rule = rule
        super().__init__(rule)


@dataclass(frozen=True, slots=True)
class _TreeEntry:
    raw_path: bytes
    path: str | None
    report_path: str
    mode: str
    object_type: str
    oid: str
    path_valid: bool


@dataclass(frozen=True, slots=True)
class _AllowlistEntry:
    path: str
    sha256: str
    rules: frozenset[str]
    reason: str


@dataclass(frozen=True, slots=True)
class _ForbiddenSnapshot:
    document: bytes
    terms: tuple[str, ...]


def audit_public_tree(
    repository: Path | str,
    ref: str,
    mode: str,
    forbidden_file: Path | str,
    allowlist_file: Path | str,
    receipt_path: Path | str | None = None,
) -> dict[str, object]:
    """Audit an immutable commit tree and optionally write its canonical PASS receipt."""
    findings: list[tuple[str, str]] = []
    try:
        selected_repository = _validated_repository(Path(repository))
        if mode not in {"tree", "snapshot"}:
            raise _PolicyError("mode_invalid")
        source_commit, tree, commit_document = _resolve_commit(
            selected_repository,
            ref,
        )
        if mode == "snapshot" and any(
            line.startswith(b"parent ") for line in commit_document.splitlines()
        ):
            findings.append(("<tree>", "snapshot_has_parent"))
        forbidden = _read_forbidden_snapshot(selected_repository, Path(forbidden_file))
        home_prefixes = _home_prefixes()
        privacy_policy = PrivacyPolicy(
            synthetic_uuid_allowlist=SYNTHETIC_UUIDS,
            _home_prefixes=home_prefixes,
            _forbidden_terms=forbidden.terms,
        )
        entries, structural_findings = _read_tree(
            selected_repository,
            tree,
            home_prefixes=home_prefixes,
            forbidden_terms=forbidden.terms,
        )
        findings.extend(structural_findings)
        allowlist_relative = _allowlist_relative_path(
            selected_repository,
            Path(allowlist_file),
        )
        documents, object_findings = _read_regular_blobs(selected_repository, entries)
        findings.extend(object_findings)
    except _PolicyError as error:
        _raise_report([(f"<{error.rule}>", error.rule)])
    except _GitObjectError:
        _raise_report([("<tree>", "git_object_invalid")])

    allowlist_document = documents.get(allowlist_relative)
    if allowlist_document is None:
        findings.append(("<allowlist>", "allowlist_invalid"))
        exemptions: tuple[_AllowlistEntry, ...] = ()
    else:
        try:
            exemptions = _parse_allowlist(allowlist_document)
        except _PolicyError:
            findings.append(("<allowlist>", "allowlist_invalid"))
            exemptions = ()

    entry_by_path = {entry.path: entry for entry in entries if entry.path is not None}
    active_exemptions: dict[str, _AllowlistEntry] = {}
    stale_paths: set[str] = set()
    for exemption in exemptions:
        document = documents.get(exemption.path)
        target = entry_by_path.get(exemption.path)
        if (
            document is None
            or target is None
            or not target.path_valid
            or hashlib.sha256(document).hexdigest() != exemption.sha256
        ):
            stale_paths.add(exemption.path)
            continue
        active_exemptions[exemption.path] = exemption

    observed_exemptions: set[tuple[str, str]] = set()
    for entry in entries:
        if entry.path is None:
            continue
        path_rules = _scan_text_rules(
            entry.path,
            home_prefixes=home_prefixes,
            forbidden_terms=forbidden.terms,
        )
        report_path = entry.report_path
        for rule in sorted(path_rules):
            findings.append((report_path, rule))

        document = documents.get(entry.path)
        if document is None:
            continue
        content_rules = _document_rules(
            entry.path,
            document,
            privacy_policy=privacy_policy,
            home_prefixes=home_prefixes,
            forbidden_terms=forbidden.terms,
        )
        active_exemption = active_exemptions.get(entry.path)
        for rule in sorted(content_rules):
            if active_exemption is not None and rule in active_exemption.rules:
                observed_exemptions.add((entry.path, rule))
                continue
            findings.append((report_path, rule))

    for listed_exemption in exemptions:
        if listed_exemption.path in stale_paths or any(
            (listed_exemption.path, rule) not in observed_exemptions
            for rule in listed_exemption.rules
        ):
            findings.append(
                (
                    _safe_report_path(
                        listed_exemption.path.encode("utf-8", errors="strict"),
                        listed_exemption.path,
                        path_valid=True,
                        home_prefixes=home_prefixes,
                        forbidden_terms=forbidden.terms,
                    ),
                    "allowlist_stale",
                )
            )

    asset_documents = {
        path.removeprefix("docs/assets/"): document
        for path, document in documents.items()
        if path.startswith("docs/assets/")
    }
    if asset_documents:
        try:
            _verify_staged_assets(
                selected_repository,
                asset_documents,
                forbidden.document,
            )
        except (OSError, ValueError):
            findings.append(("docs/assets", "public_assets_invalid"))

    if findings:
        _raise_report(findings)

    assert allowlist_document is not None
    manifest = documents.get(
        "docs/assets/demo-manifest.json",
        documents.get("docs/assets/manifest.json", b""),
    )
    receipt: dict[str, object] = {
        "schema_version": 1,
        "auditor": _AUDITOR,
        "policy_version": 1,
        "mode": mode,
        "source_commit": source_commit,
        "tree": tree,
        "allowlist_sha256": hashlib.sha256(allowlist_document).hexdigest(),
        "forbidden_terms_sha256": hashlib.sha256(forbidden.document).hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
        "file_count": len(entries),
        "total_bytes": sum(len(document) for document in documents.values()),
    }
    if receipt_path is not None:
        try:
            _write_receipt_no_clobber(Path(receipt_path), _canonical_json(receipt))
        except FileExistsError:
            _raise_report([("<receipt>", "receipt_exists")])
        except OSError:
            _raise_report([("<receipt>", "receipt_write_failed")])
    return receipt


def _validated_repository(repository: Path) -> Path:
    selected = Path(os.path.abspath(repository))
    _reject_symlink_components(selected, "repository_invalid")
    try:
        metadata = selected.lstat()
    except OSError as error:
        raise _PolicyError("repository_invalid") from error
    if not stat.S_ISDIR(metadata.st_mode):
        raise _PolicyError("repository_invalid")
    try:
        top_level = (
            _git_bytes(selected, "rev-parse", "--show-toplevel")
            .decode("utf-8", errors="strict")
            .strip()
        )
    except UnicodeError as error:
        raise _PolicyError("repository_invalid") from error
    if Path(os.path.abspath(top_level)) != selected:
        raise _PolicyError("repository_invalid")
    return selected


def _resolve_commit(repository: Path, ref: str) -> tuple[str, str, bytes]:
    if (
        not isinstance(ref, str)
        or not ref
        or len(ref) > 512
        or any(ord(character) < 32 or ord(character) == 127 for character in ref)
    ):
        raise _PolicyError("ref_invalid")
    resolved = (
        _git_bytes(
            repository,
            "rev-parse",
            "--verify",
            "--end-of-options",
            f"{ref}^{{commit}}",
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    if _OID.fullmatch(resolved) is None:
        raise _GitObjectError
    if _git_bytes(repository, "cat-file", "-t", resolved).strip() != b"commit":
        raise _GitObjectError
    document = _git_bytes(repository, "cat-file", "commit", resolved)
    tree_lines = [line for line in document.splitlines() if line.startswith(b"tree ")]
    if len(tree_lines) != 1:
        raise _GitObjectError
    try:
        tree = tree_lines[0].removeprefix(b"tree ").decode("ascii", errors="strict")
    except UnicodeError as error:
        raise _GitObjectError from error
    if _OID.fullmatch(tree) is None:
        raise _GitObjectError
    if _git_bytes(repository, "cat-file", "-t", tree).strip() != b"tree":
        raise _GitObjectError
    return resolved, tree, document


def _read_tree(
    repository: Path,
    tree: str,
    *,
    home_prefixes: tuple[str, ...],
    forbidden_terms: tuple[str, ...],
) -> tuple[tuple[_TreeEntry, ...], list[tuple[str, str]]]:
    document = _git_bytes(
        repository,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        tree,
    )
    records = document.split(b"\0")
    if records[-1:] != [b""]:
        raise _GitObjectError
    records = records[:-1]
    if len(records) > _MAX_FILES:
        raise _PolicyError("file_count_exceeded")
    entries: list[_TreeEntry] = []
    findings: list[tuple[str, str]] = []
    for record in records:
        try:
            header, raw_path = record.split(b"\t", 1)
            mode_raw, object_type_raw, oid_raw = header.split(b" ", 2)
            mode = mode_raw.decode("ascii", errors="strict")
            object_type = object_type_raw.decode("ascii", errors="strict")
            oid = oid_raw.decode("ascii", errors="strict")
        except (UnicodeError, ValueError) as error:
            raise _GitObjectError from error
        if not raw_path or _OID.fullmatch(oid) is None:
            raise _GitObjectError
        path, path_valid, path_rule = _validated_tree_path(raw_path)
        report_path = _safe_report_path(
            raw_path,
            path,
            path_valid=path_valid,
            home_prefixes=home_prefixes,
            forbidden_terms=forbidden_terms,
        )
        if path_rule is not None:
            findings.append((report_path, path_rule))
        if mode == "120000":
            findings.append((report_path, "symlink"))
        elif mode == "160000" or object_type == "commit":
            findings.append((report_path, "gitlink"))
        elif mode not in {"100644", "100755"} or object_type != "blob":
            findings.append((report_path, "special_mode"))
        entries.append(
            _TreeEntry(
                raw_path=raw_path,
                path=path,
                report_path=report_path,
                mode=mode,
                object_type=object_type,
                oid=oid,
                path_valid=path_valid,
            )
        )

    conflict_indexes = _conflicting_path_indexes(entries)
    for index in sorted(conflict_indexes):
        entry = entries[index]
        findings.append((_path_id(entry.raw_path), "path_conflict"))
        entries[index] = _TreeEntry(
            raw_path=entry.raw_path,
            path=entry.path,
            report_path=_path_id(entry.raw_path),
            mode=entry.mode,
            object_type=entry.object_type,
            oid=entry.oid,
            path_valid=False,
        )
    return tuple(entries), findings


def _validated_tree_path(raw_path: bytes) -> tuple[str | None, bool, str | None]:
    if len(raw_path) > _MAX_PATH_BYTES:
        return None, False, "unsafe_path"
    try:
        path = raw_path.decode("utf-8", errors="strict")
    except UnicodeError:
        return None, False, "path_not_utf8"
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or candidate.as_posix() != path
        or "\\" in path
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        return path, False, "unsafe_path"
    for component in candidate.parts:
        folded = component.casefold()
        stem = component.split(".", 1)[0].casefold()
        if (
            not component
            or len(component.encode("utf-8")) > 255
            or component != component.strip()
            or component.endswith(".")
            or component.startswith("-")
            or folded == ".git"
            or stem in _WINDOWS_RESERVED
            or any(character in '<>:"|?*' for character in component)
            or unicodedata.normalize("NFC", component) != component
            or any(unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in component)
        ):
            return path, False, "unsafe_path"
    return path, True, None


def _safe_report_path(
    raw_path: bytes,
    path: str | None,
    *,
    path_valid: bool,
    home_prefixes: tuple[str, ...],
    forbidden_terms: tuple[str, ...],
) -> str:
    if (
        path is None
        or not path_valid
        or _scan_text_rules(
            path,
            home_prefixes=home_prefixes,
            forbidden_terms=forbidden_terms,
        )
    ):
        return _path_id(raw_path)
    return path


def _conflicting_path_indexes(entries: list[_TreeEntry]) -> set[int]:
    normalized: dict[str, list[int]] = {}
    for index, entry in enumerate(entries):
        if entry.path is None:
            continue
        key = unicodedata.normalize("NFKC", entry.path).casefold()
        normalized.setdefault(key, []).append(index)
    conflicts: set[int] = set()
    keys = set(normalized)
    for key, indexes in normalized.items():
        if len(indexes) > 1:
            conflicts.update(indexes)
        components = key.split("/")
        for end in range(1, len(components)):
            parent = "/".join(components[:end])
            if parent in keys:
                conflicts.update(indexes)
                conflicts.update(normalized[parent])
    return conflicts


def _read_regular_blobs(
    repository: Path,
    entries: tuple[_TreeEntry, ...],
) -> tuple[dict[str, bytes], list[tuple[str, str]]]:
    documents: dict[str, bytes] = {}
    findings: list[tuple[str, str]] = []
    total_bytes = 0
    for entry in entries:
        if (
            entry.path is None
            or entry.mode not in {"100644", "100755"}
            or entry.object_type != "blob"
        ):
            continue
        try:
            size_text = (
                _git_bytes(repository, "cat-file", "-s", entry.oid)
                .decode("ascii", errors="strict")
                .strip()
            )
            if not size_text.isascii() or not size_text.isdigit():
                raise _GitObjectError
            size = int(size_text)
            if size > _MAX_FILE_BYTES:
                findings.append((entry.report_path, "file_too_large"))
                continue
            total_bytes += size
            if total_bytes > _MAX_TOTAL_BYTES:
                raise _PolicyError("total_bytes_exceeded")
            document = _git_bytes(repository, "cat-file", "blob", entry.oid)
            if len(document) != size:
                raise _GitObjectError
        except (UnicodeError, _GitObjectError):
            findings.append((entry.report_path, "object_read_failed"))
            continue
        documents[entry.path] = document
    return documents, findings


def _allowlist_relative_path(repository: Path, allowlist: Path) -> str:
    if allowlist.is_absolute():
        raw_parts = allowlist.parts
        if ".." in raw_parts:
            raise _PolicyError("allowlist_invalid")
        selected = Path(os.path.abspath(allowlist))
        try:
            relative = selected.relative_to(repository).as_posix()
        except ValueError as error:
            raise _PolicyError("allowlist_invalid") from error
    else:
        relative = allowlist.as_posix()
    path, valid, _rule = _validated_tree_path(relative.encode("utf-8", errors="strict"))
    if not valid or path != relative:
        raise _PolicyError("allowlist_invalid")
    return relative


def _parse_allowlist(document: bytes) -> tuple[_AllowlistEntry, ...]:
    try:
        text = document.decode("utf-8", errors="strict")
        payload = tomllib.loads(text)
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise _PolicyError("allowlist_invalid") from error
    if not isinstance(payload, dict) or not set(payload) <= {"schema_version", "exemptions"}:
        raise _PolicyError("allowlist_invalid")
    if set(payload) not in ({"schema_version"}, {"schema_version", "exemptions"}):
        raise _PolicyError("allowlist_invalid")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise _PolicyError("allowlist_invalid")
    raw_entries = payload.get("exemptions", [])
    if type(raw_entries) is not list:
        raise _PolicyError("allowlist_invalid")
    entries: list[_AllowlistEntry] = []
    seen_paths: set[str] = set()
    for raw in raw_entries:
        if not isinstance(raw, dict) or set(raw) != {"path", "sha256", "rules", "reason"}:
            raise _PolicyError("allowlist_invalid")
        path = raw["path"]
        digest = raw["sha256"]
        rules = raw["rules"]
        reason = raw["reason"]
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or not isinstance(reason, str)
            or type(rules) is not list
            or not rules
            or any(not isinstance(rule, str) for rule in rules)
        ):
            raise _PolicyError("allowlist_invalid")
        try:
            encoded_path = path.encode("utf-8", errors="strict")
        except UnicodeError as error:
            raise _PolicyError("allowlist_invalid") from error
        validated_path, path_valid, _path_rule = _validated_tree_path(encoded_path)
        if (
            not path_valid
            or validated_path != path
            or path in seen_paths
            or _SHA256.fullmatch(digest) is None
            or not reason.strip()
            or reason != reason.strip()
            or len(reason) > 512
            or rules != sorted(set(rules))
            or not set(rules) <= _ALLOWLIST_RULES
        ):
            raise _PolicyError("allowlist_invalid")
        seen_paths.add(path)
        entries.append(
            _AllowlistEntry(
                path=path,
                sha256=digest,
                rules=frozenset(rules),
                reason=reason,
            )
        )
    return tuple(entries)


def _read_forbidden_snapshot(repository: Path, path: Path) -> _ForbiddenSnapshot:
    if not path.is_absolute() or ".." in path.parts:
        raise _PolicyError("forbidden_file_invalid")
    selected = Path(os.path.abspath(path))
    _reject_symlink_components(selected, "forbidden_file_invalid")
    try:
        selected.relative_to(repository)
    except ValueError:
        pass
    else:
        raise _PolicyError("forbidden_file_invalid")
    if _is_tracked_by_enclosing_repository(selected):
        raise _PolicyError("forbidden_file_invalid")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(selected, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > _FORBIDDEN_MAX_BYTES
        ):
            raise _PolicyError("forbidden_file_invalid")
        document = bytearray()
        while len(document) <= _FORBIDDEN_MAX_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _FORBIDDEN_MAX_BYTES + 1 - len(document)),
            )
            if not chunk:
                break
            document.extend(chunk)
        after = os.fstat(descriptor)
        live = selected.lstat()
        if (
            _file_identity(before) != _file_identity(after)
            or _file_identity(before) != _file_identity(live)
            or stat.S_ISLNK(live.st_mode)
            or len(document) != before.st_size
            or len(document) > _FORBIDDEN_MAX_BYTES
        ):
            raise _PolicyError("forbidden_file_invalid")
    except _PolicyError:
        raise
    except OSError as error:
        raise _PolicyError("forbidden_file_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        text = bytes(document).decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise _PolicyError("forbidden_file_invalid") from error
    terms: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        term = unicodedata.normalize("NFKC", line.strip())
        if not term:
            continue
        if (
            len(term) > _MAX_TERM_CHARS
            or any(unicodedata.category(character) in {"Cc", "Cs"} for character in term)
            or term.casefold() in seen
        ):
            raise _PolicyError("forbidden_file_invalid")
        seen.add(term.casefold())
        terms.append(term)
    if not terms:
        raise _PolicyError("forbidden_file_invalid")
    return _ForbiddenSnapshot(bytes(document), tuple(terms))


def _is_tracked_by_enclosing_repository(path: Path) -> bool:
    for parent in path.parents:
        marker = parent / ".git"
        try:
            marker.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        try:
            relative = path.relative_to(parent).as_posix()
            result = _git_process(
                parent,
                "ls-files",
                "--error-unmatch",
                "--",
                relative,
                check=False,
            )
        except (OSError, ValueError):
            return True
        return result.returncode == 0
    return False


def _home_prefixes() -> tuple[str, ...]:
    candidates: list[str] = []
    for value in (
        os.environ.get("HOME"),
        str(Path.home()),
        pwd.getpwuid(os.getuid()).pw_dir,
    ):
        if value:
            normalized = unicodedata.normalize("NFKC", os.path.abspath(value))
            if normalized not in candidates:
                candidates.append(normalized)
    return tuple(candidates)


def _scan_text_rules(
    text: str,
    *,
    home_prefixes: tuple[str, ...],
    forbidden_terms: tuple[str, ...],
) -> frozenset[str]:
    rules: set[str] = set()
    for variant in _text_variants(text):
        folded = variant.casefold()
        if any(prefix.casefold() in folded for prefix in home_prefixes if prefix):
            rules.add("home_prefix")
        if any(term.casefold() in folded for term in forbidden_terms):
            rules.add("forbidden_term")
        if (
            any(pattern.search(variant) is not None for pattern in _TOKEN_PATTERNS)
            or any(
                _named_token_value_is_credential(match.group("value"))
                for match in _NAMED_TOKEN.finditer(variant)
            )
            or any(
                any(character.isupper() for character in match.group(0))
                for match in _GENERIC_TOKEN.finditer(variant)
            )
        ):
            rules.add("token_like")
        if _PRIVATE_KEY_BEGIN.search(variant) is not None:
            rules.add("private_key")
        for match in _UUID.finditer(variant):
            if (match.start() > 0 and variant[match.start() - 1] in "0123456789abcdefABCDEF") or (
                match.end() < len(variant) and variant[match.end()] in "0123456789abcdefABCDEF"
            ):
                rules.add("unknown_uuid")
                continue
            try:
                identifier = UUID(match.group(0))
            except ValueError:
                rules.add("unknown_uuid")
                continue
            if identifier not in SYNTHETIC_UUIDS:
                rules.add("unknown_uuid")
        if _SESSION_BODY.search(variant) is not None:
            rules.add("session_body")
        if _DATABASE_DUMP_MARKER.search(variant) is not None or (
            _DATABASE_CREATE.search(variant) is not None
            and _DATABASE_INSERT.search(variant) is not None
        ):
            rules.add("database_dump")
    return frozenset(rules)


def _named_token_value_is_credential(value: str) -> bool:
    return (
        value[:1] in {"'", '"'}
        or any(character.isupper() or character.isdigit() for character in value)
        or any(character in "-/+=" for character in value)
    )


def _document_rules(
    path: str,
    document: bytes,
    *,
    privacy_policy: PrivacyPolicy,
    home_prefixes: tuple[str, ...],
    forbidden_terms: tuple[str, ...],
) -> frozenset[str]:
    rules: set[str] = set()
    suffix = PurePosixPath(path).suffix.casefold()
    raster_kind: str | None = None
    if document.startswith(_PNG_SIGNATURE) or suffix == ".png":
        raster_kind = ".png"
    elif (
        len(document) >= 12 and document.startswith(_WEBP_SIGNATURE) and document[8:12] == b"WEBP"
    ) or suffix == ".webp":
        raster_kind = ".webp"
    if raster_kind is not None:
        try:
            scan_document(
                document,
                privacy_policy,
                asset_name=f"asset{raster_kind}",
                limits=_PRIVACY_LIMITS,
            )
        except PrivacyViolation as error:
            rules.add(error.code)
        return frozenset(rules)
    if document.startswith(_SQLITE_SIGNATURE):
        return frozenset({"database_dump"})
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeError:
        return frozenset({"binary_unsupported"})
    rules.update(
        _scan_text_rules(
            text,
            home_prefixes=home_prefixes,
            forbidden_terms=forbidden_terms,
        )
    )
    if "\x00" in text:
        rules.add("binary_unsupported")
    return frozenset(rules)


def _text_variants(text: str) -> tuple[str, ...]:
    variants: list[str] = []
    current = unicodedata.normalize("NFKC", text)
    for _index in range(3):
        if current not in variants:
            variants.append(current)
        decoded = unicodedata.normalize("NFKC", urllib.parse.unquote(html.unescape(current)))
        if decoded == current:
            break
        current = decoded
    return tuple(variants)


def _verify_staged_assets(
    repository: Path,
    documents: dict[str, bytes],
    forbidden_document: bytes,
) -> None:
    with tempfile.TemporaryDirectory(prefix="pmh-public-assets-") as temporary:
        root = Path(temporary).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("asset_root_invalid")
        root.chmod(0o700)
        asset_root = root / "assets"
        asset_root.mkdir(mode=0o700)
        for relative, document in sorted(documents.items()):
            encoded = relative.encode("utf-8", errors="strict")
            path, valid, _rule = _validated_tree_path(encoded)
            if not valid or path != relative:
                raise ValueError("asset_set_invalid")
            destination = asset_root / PurePosixPath(relative)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _write_exact_file(destination, document, 0o600)
        staged_forbidden = root / "forbidden-terms.txt"
        _write_exact_file(staged_forbidden, forbidden_document, 0o600)
        _verify_public_assets(
            asset_root,
            repository_root=repository,
            denylist_path=staged_forbidden,
        )


def _write_exact_file(path: Path, document: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, mode)
    try:
        os.fchmod(descriptor, mode)
        view = memoryview(document)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_receipt_no_clobber(path: Path, document: bytes) -> None:
    selected = Path(os.path.abspath(path))
    if not selected.name or selected.name in {".", ".."}:
        raise OSError("receipt path has no filename")
    requested_parent = selected.parent
    try:
        canonical_parent = requested_parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise OSError("receipt parent cannot be resolved") from error

    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory is None or not _DIRFD_SUPPORTED:
        raise OSError("secure dirfd operations are unavailable")

    directory_descriptor = -1
    descriptor = -1
    temporary_name: str | None = None
    temporary_identity: tuple[int, int] | None = None
    destination_linked = False
    try:
        directory_descriptor = os.open(
            canonical_parent,
            os.O_RDONLY | os.O_CLOEXEC | directory | no_follow,
        )
        parent_metadata = os.fstat(directory_descriptor)
        if not stat.S_ISDIR(parent_metadata.st_mode) or not _receipt_parent_is_bound(
            requested_parent,
            canonical_parent,
            parent_metadata,
        ):
            raise OSError("receipt parent identity changed")

        temporary_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | no_follow
        for _attempt in range(128):
            candidate = f".{selected.name}.{secrets.token_hex(16)}"
            try:
                descriptor = os.open(
                    candidate,
                    temporary_flags,
                    0o600,
                    dir_fd=directory_descriptor,
                )
            except FileExistsError:
                continue
            temporary_name = candidate
            temporary_identity = _inode_identity(os.fstat(descriptor))
            break
        else:
            raise OSError("receipt temporary name exhausted")

        os.fchmod(descriptor, 0o600)
        view = memoryview(document)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
        os.fsync(descriptor)
        temporary_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or _inode_identity(temporary_metadata) != temporary_identity
            or temporary_metadata.st_uid != os.getuid()
            or temporary_metadata.st_nlink != 1
            or stat.S_IMODE(temporary_metadata.st_mode) != 0o600
            or not _name_matches_inode(
                directory_descriptor,
                temporary_name,
                temporary_identity,
            )
            or not _receipt_parent_is_bound(
                requested_parent,
                canonical_parent,
                parent_metadata,
            )
        ):
            raise OSError("receipt temporary identity changed")

        os.link(
            temporary_name,
            selected.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
        destination_linked = True
        if not _name_matches_inode(
            directory_descriptor,
            selected.name,
            temporary_identity,
        ) or not _receipt_parent_is_bound(
            requested_parent,
            canonical_parent,
            parent_metadata,
        ):
            raise OSError("receipt destination identity changed")

        _unlink_if_same_inode(
            directory_descriptor,
            temporary_name,
            temporary_identity,
        )
        temporary_name = None
        os.fsync(directory_descriptor)
        if not _name_matches_inode(
            directory_descriptor,
            selected.name,
            temporary_identity,
            expected_links=1,
        ) or not _receipt_parent_is_bound(
            requested_parent,
            canonical_parent,
            parent_metadata,
        ):
            raise OSError("receipt parent identity changed")
    except BaseException:
        if destination_linked and temporary_identity is not None:
            _unlink_if_same_inode(
                directory_descriptor,
                selected.name,
                temporary_identity,
            )
        if temporary_name is not None and temporary_identity is not None:
            _unlink_if_same_inode(
                directory_descriptor,
                temporary_name,
                temporary_identity,
            )
        if directory_descriptor >= 0:
            try:
                os.fsync(directory_descriptor)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if directory_descriptor >= 0:
            os.close(directory_descriptor)


def _receipt_parent_is_bound(
    requested_parent: Path,
    canonical_parent: Path,
    expected: os.stat_result,
) -> bool:
    try:
        if requested_parent.resolve(strict=True) != canonical_parent:
            return False
        live = canonical_parent.lstat()
    except (OSError, RuntimeError):
        return False
    return stat.S_ISDIR(live.st_mode) and _inode_identity(live) == _inode_identity(expected)


def _inode_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _name_matches_inode(
    directory_descriptor: int,
    name: str,
    expected: tuple[int, int],
    *,
    expected_links: int | None = None,
) -> bool:
    try:
        live = os.stat(
            name,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except OSError:
        return False
    return (
        stat.S_ISREG(live.st_mode)
        and _inode_identity(live) == expected
        and stat.S_IMODE(live.st_mode) == 0o600
        and (expected_links is None or live.st_nlink == expected_links)
    )


def _unlink_if_same_inode(
    directory_descriptor: int,
    name: str,
    expected: tuple[int, int],
) -> None:
    if _name_matches_inode(directory_descriptor, name, expected):
        os.unlink(name, dir_fd=directory_descriptor)


def _reject_symlink_components(path: Path, rule: str) -> None:
    if not path.is_absolute():
        raise _PolicyError(rule)
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise _PolicyError(rule) from error
        if stat.S_ISLNK(metadata.st_mode):
            raise _PolicyError(rule)


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _path_id(path: bytes) -> str:
    return f"path-sha256:{hashlib.sha256(path).hexdigest()}"


def _canonical_json(payload: dict[str, object]) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8", errors="strict")


def _raise_report(findings: list[tuple[str, str]]) -> NoReturn:
    counts = Counter(findings)
    violations = [
        {"count": count, "path": path, "rule": rule}
        for (path, rule), count in sorted(counts.items())
    ]
    raise PublicTreeAuditError(
        {
            "status": "FAIL",
            "violation_count": sum(counts.values()),
            "violations": violations,
        }
    )


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    result = _git_process(repository, *arguments, check=False)
    if result.returncode != 0:
        raise _GitObjectError
    return result.stdout


def _git_process(
    repository: Path,
    *arguments: str,
    check: bool,
) -> subprocess.CompletedProcess[bytes]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_") and key not in {"CDPATH", "ENV", "BASH_ENV"}
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "LC_ALL": "C",
        }
    )
    try:
        return subprocess.run(
            [
                "git",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=/dev/null",
                *arguments,
            ],
            cwd=repository,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=check,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise _GitObjectError from error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit an immutable Git tree for public release.")
    parser.add_argument("--mode", choices=("tree", "snapshot"), required=True)
    parser.add_argument("--ref", default="HEAD")
    parser.add_argument("--forbidden-file", type=Path, required=True)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path("config/public-release-allowlist.toml"),
    )
    parser.add_argument("--receipt", type=Path)
    arguments = parser.parse_args(argv)
    try:
        receipt = audit_public_tree(
            Path.cwd(),
            arguments.ref,
            arguments.mode,
            arguments.forbidden_file,
            arguments.allowlist,
            arguments.receipt,
        )
    except PublicTreeAuditError as error:
        sys.stderr.buffer.write(_canonical_json(error.report))
        return 1
    sys.stdout.buffer.write(_canonical_json(receipt))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
