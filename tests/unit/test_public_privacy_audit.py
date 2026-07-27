from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
AUDITOR_PATH = PROJECT_ROOT / "scripts/audit_public_tree.py"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()


def _load_auditor() -> ModuleType:
    assert AUDITOR_PATH.is_file(), "public tree auditor script is missing"
    name = "audit_public_tree_contract"
    spec = importlib.util.spec_from_file_location(name, AUDITOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run(
    repository: Path,
    *arguments: str,
    input_document: bytes | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_NAME": "Fixture Maintainer",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture Maintainer",
        }
    )
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        arguments,
        cwd=repository,
        env=environment,
        input=input_document,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )


def _git(repository: Path, *arguments: str, input_document: bytes | None = None) -> str:
    return (
        _run(repository, "git", *arguments, input_document=input_document).stdout.decode().strip()
    )


def _write_allowlist(repository: Path, exemptions: list[dict[str, object]] | None = None) -> Path:
    path = repository / "config/public-release-allowlist.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["schema_version = 1", ""]
    for entry in exemptions or []:
        rules = ", ".join(json.dumps(rule) for rule in entry["rules"])
        lines.extend(
            [
                "[[exemptions]]",
                f"path = {json.dumps(entry['path'])}",
                f"sha256 = {json.dumps(entry['sha256'])}",
                f"rules = [{rules}]",
                f"reason = {json.dumps(entry['reason'])}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def _forbidden_file(tmp_path: Path, text: str = "private-project-name\n") -> Path:
    path = tmp_path / "private-terms.txt"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


def _repository(tmp_path: Path, files: dict[str, bytes | str] | None = None) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Fixture Maintainer")
    _git(repository, "config", "user.email", "fixture@example.invalid")
    _write_allowlist(repository)
    for relative, document in (files or {"README.md": "safe public text\n"}).items():
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(document, bytes):
            path.write_bytes(document)
        else:
            path.write_text(document, encoding="utf-8")
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", "fixture")
    return repository


def _audit(
    auditor: ModuleType,
    repository: Path,
    forbidden: Path,
    *,
    ref: str = "HEAD",
    mode: str = "tree",
    receipt: Path | None = None,
) -> dict[str, object]:
    return auditor.audit_public_tree(
        repository,
        ref,
        mode,
        forbidden,
        repository / "config/public-release-allowlist.toml",
        receipt,
    )


def _report_for_failure(
    auditor: ModuleType,
    repository: Path,
    forbidden: Path,
    *,
    ref: str = "HEAD",
) -> dict[str, Any]:
    with pytest.raises(auditor.PublicTreeAuditError) as captured:
        _audit(auditor, repository, forbidden, ref=ref)
    return captured.value.report


def _rules(report: dict[str, Any]) -> set[str]:
    return {item["rule"] for item in report["violations"]}


def _commit(repository: Path, message: str = "fixture update") -> str:
    _git(repository, "add", "-A")
    _git(repository, "commit", "-qm", message)
    return _git(repository, "rev-parse", "HEAD")


def _commit_with_root_records(repository: Path, additions: list[tuple[bytes, bytes]]) -> str:
    base_tree = _git(repository, "rev-parse", "HEAD^{tree}")
    base = _run(repository, "git", "ls-tree", "-z", base_tree).stdout
    records = [record for record in base.split(b"\0") if record]
    for name, document in additions:
        blob = _git(repository, "hash-object", "-w", "--stdin", input_document=document)
        records.append(f"100644 blob {blob}\t".encode() + name)
    records.sort(key=lambda record: record.split(b"\t", 1)[1])
    tree = _git(
        repository,
        "mktree",
        "-z",
        input_document=b"\0".join(records) + b"\0",
    )
    return _git(repository, "commit-tree", tree, "-m", "object fixture")


def _png_with_text_metadata() -> bytes:
    import struct
    import zlib

    def chunk(kind: bytes, payload: bytes) -> bytes:
        checksum = zlib.crc32(payload, zlib.crc32(kind)) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", checksum)

    raw_pixel = b"\x00\x00\x00\x00\xff"
    return b"".join(
        (
            b"\x89PNG\r\n\x1a\n",
            chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 6, 0, 0, 0)),
            chunk(b"tEXt", b"Comment\x00private metadata"),
            chunk(b"IDAT", zlib.compress(raw_pixel)),
            chunk(b"IEND", b""),
        )
    )


def test_audit_reads_the_immutable_commit_tree_and_writes_a_canonical_receipt(
    tmp_path: Path,
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    source_commit = _git(repository, "rev-parse", "HEAD")
    tree = _git(repository, "rev-parse", "HEAD^{tree}")
    committed_allowlist = _run(
        repository,
        "git",
        "show",
        "HEAD:config/public-release-allowlist.toml",
    ).stdout

    (repository / "README.md").unlink()
    (repository / "README.md").symlink_to("/etc/passwd")
    (repository / "config/public-release-allowlist.toml").write_text(
        "this is not TOML = [",
        encoding="utf-8",
    )
    receipt_path = tmp_path / "receipt.json"

    receipt = _audit(auditor, repository, forbidden, receipt=receipt_path)

    assert set(receipt) == {
        "allowlist_sha256",
        "auditor",
        "file_count",
        "forbidden_terms_sha256",
        "manifest_sha256",
        "mode",
        "policy_version",
        "schema_version",
        "source_commit",
        "total_bytes",
        "tree",
    }
    assert receipt == {
        "schema_version": 1,
        "auditor": "project-memory-hub-public-tree",
        "policy_version": 1,
        "mode": "tree",
        "source_commit": source_commit,
        "tree": tree,
        "allowlist_sha256": hashlib.sha256(committed_allowlist).hexdigest(),
        "forbidden_terms_sha256": hashlib.sha256(forbidden.read_bytes()).hexdigest(),
        "manifest_sha256": EMPTY_SHA256,
        "file_count": 2,
        "total_bytes": len(committed_allowlist) + len(b"safe public text\n"),
    }
    expected_document = (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    assert receipt_path.read_bytes() == expected_document
    assert stat.S_IMODE(receipt_path.stat().st_mode) == 0o600


def test_receipt_is_pass_only_atomic_and_no_clobber(tmp_path: Path) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_bytes(b"operator-owned\n")
    receipt_path.chmod(0o600)

    report = _report_for_failure_with_receipt(
        auditor,
        repository,
        forbidden,
        receipt_path,
    )

    assert _rules(report) == {"receipt_exists"}
    assert receipt_path.read_bytes() == b"operator-owned\n"
    assert not list(tmp_path.glob(".receipt.json.*"))


def test_receipt_writer_accepts_a_canonicalizable_symlink_alias(tmp_path: Path) -> None:
    auditor = _load_auditor()
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    private_var = Path("/private/var")
    if Path("/var").is_symlink() and real_parent.is_relative_to(private_var):
        alias_parent = Path("/var") / real_parent.relative_to(private_var)
        assert alias_parent.resolve(strict=True) == real_parent
    else:
        alias_parent = tmp_path / "parent-alias"
        alias_parent.symlink_to(real_parent, target_is_directory=True)
    receipt = alias_parent / "receipt.json"

    auditor._write_receipt_no_clobber(receipt, b'{"status":"PASS"}\n')

    assert receipt.read_bytes() == b'{"status":"PASS"}\n'
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert not list(real_parent.glob(".receipt.json.*"))


def test_receipt_writer_uses_a_pinned_directory_and_no_follow_opens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auditor = _load_auditor()
    parent = tmp_path / "receipts"
    parent.mkdir()
    opened: list[tuple[object, int, int | None]] = []
    linked: list[tuple[object, object, int | None, int | None]] = []
    original_open = auditor.os.open
    original_link = auditor.os.link

    def observe_open(
        path: object,
        flags: int,
        *arguments: object,
        **keywords: object,
    ) -> int:
        opened.append((path, flags, keywords.get("dir_fd")))
        return original_open(path, flags, *arguments, **keywords)

    def observe_link(
        source: object,
        destination: object,
        *arguments: object,
        **keywords: object,
    ) -> None:
        linked.append(
            (
                source,
                destination,
                keywords.get("src_dir_fd"),
                keywords.get("dst_dir_fd"),
            )
        )
        original_link(source, destination, *arguments, **keywords)

    monkeypatch.setattr(auditor.os, "open", observe_open)
    monkeypatch.setattr(auditor.os, "link", observe_link)

    auditor._write_receipt_no_clobber(parent / "receipt.json", b"PASS\n")

    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    assert any(
        flags & no_follow and flags & directory and dir_fd is None
        for _path, flags, dir_fd in opened
    )
    assert any(flags & no_follow and dir_fd is not None for _path, flags, dir_fd in opened)
    assert linked
    assert linked[0][2] is not None and linked[0][2] == linked[0][3]


def test_receipt_writer_rolls_back_if_the_parent_is_replaced_mid_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auditor = _load_auditor()
    parent = tmp_path / "receipts"
    parent.mkdir()
    displaced = tmp_path / "displaced-receipts"
    original_link = auditor.os.link
    replaced = False

    def replace_parent_then_link(
        source: object,
        destination: object,
        *arguments: object,
        **keywords: object,
    ) -> None:
        nonlocal replaced
        if not replaced:
            parent.rename(displaced)
            parent.mkdir()
            replaced = True
        original_link(source, destination, *arguments, **keywords)

    monkeypatch.setattr(auditor.os, "link", replace_parent_then_link)

    with pytest.raises(OSError):
        auditor._write_receipt_no_clobber(parent / "receipt.json", b"PASS\n")

    assert replaced
    assert not list(parent.iterdir())
    assert not list(displaced.iterdir())


def _report_for_failure_with_receipt(
    auditor: ModuleType,
    repository: Path,
    forbidden: Path,
    receipt: Path,
) -> dict[str, Any]:
    with pytest.raises(auditor.PublicTreeAuditError) as captured:
        _audit(auditor, repository, forbidden, receipt=receipt)
    return captured.value.report


def test_git_replace_objects_cannot_change_the_audited_commit(tmp_path: Path) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    safe_commit = _git(repository, "rev-parse", "HEAD")
    (repository / "README.md").write_text("sk-" + "A" * 24, encoding="utf-8")
    replacement_commit = _commit(repository, "private replacement")
    _git(repository, "replace", safe_commit, replacement_commit)

    receipt = _audit(auditor, repository, forbidden, ref=safe_commit)

    assert receipt["source_commit"] == safe_commit
    assert receipt["tree"] != _git(repository, "rev-parse", f"{replacement_commit}^{{tree}}")


def test_git_process_disables_a_repository_fsmonitor_hook(tmp_path: Path) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    sentinel = tmp_path / "fsmonitor-was-run"
    hook = tmp_path / "malicious-fsmonitor"
    hook.write_text(
        f"#!/bin/sh\nprintf invoked > {str(sentinel)!r}\nprintf '2\\n'\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)
    _git(repository, "config", "core.fsmonitor", str(hook))

    result = auditor._git_process(repository, "status", "--porcelain", check=False)

    assert result.returncode == 0
    assert not sentinel.exists()


def test_git_process_disables_lazy_fetch_and_unsafe_repository_integrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    observed: dict[str, object] = {}

    def capture_run(
        command: list[str],
        **keywords: object,
    ) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed["environment"] = keywords["env"]
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(auditor.subprocess, "run", capture_run)

    auditor._git_process(repository, "status", "--porcelain", check=False)

    command = observed["command"]
    environment = observed["environment"]
    assert isinstance(command, list)
    assert isinstance(environment, dict)
    assert ["-c", "core.fsmonitor=false"] == command[command.index("-c") :][:2]
    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert environment["GIT_NO_REPLACE_OBJECTS"] == "1"


@pytest.mark.parametrize(
    ("document", "expected_rule"),
    [
        ("authorization = Bearer " + "A" * 24, "token_like"),
        (str(Path.home() / "Documents" / "personal"), "home_prefix"),
        ("private-project-name", "forbidden_term"),
        ("de305d54-75b4-431b-adb2-eb6b9e546014", "unknown_uuid"),
        ('{"type":"response_item","payload":{"body":"private"}}', "session_body"),
        (
            "-- database dump\nCREATE TABLE secret(value TEXT);\nINSERT INTO secret VALUES('x');",
            "database_dump",
        ),
        (b"\x00\xff\x10binary", "binary_unsupported"),
        (_png_with_text_metadata(), "png_metadata"),
    ],
)
def test_audit_rejects_private_text_sessions_dumps_and_unknown_binary(
    tmp_path: Path,
    document: bytes | str,
    expected_rule: str,
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path, {"fixture.bin": document})
    forbidden = _forbidden_file(tmp_path)

    report = _report_for_failure(auditor, repository, forbidden)

    assert expected_rule in _rules(report)
    serialized = json.dumps(report, ensure_ascii=False)
    for private_value in (
        "private-project-name",
        "de305d54-75b4-431b-adb2-eb6b9e546014",
        str(Path.home()),
        "Bearer",
        "CREATE TABLE",
        "response_item",
    ):
        assert private_value not in serialized


def test_generic_token_rule_ignores_lowercase_identifiers_but_rejects_uppercase_tokens(
    tmp_path: Path,
) -> None:
    auditor = _load_auditor()
    safe_identifier = "release_candidate_snapshot_identifier_value"
    assert len(safe_identifier) == 43
    repository = _repository(
        tmp_path,
        {
            f"docs/{safe_identifier}.md": f"{safe_identifier}\n",
            "credential.txt": "A" * 43,
        },
    )
    forbidden = _forbidden_file(tmp_path)

    report = _report_for_failure(auditor, repository, forbidden)

    assert report["violations"] == [{"count": 1, "path": "credential.txt", "rule": "token_like"}]


def test_label_assignment_rule_ignores_code_attributes_but_rejects_real_config(
    tmp_path: Path,
) -> None:
    auditor = _load_auditor()
    repository = _repository(
        tmp_path,
        {
            "example.py": (
                "self._access_token = 'example-placeholder-value'\n"
                "access_token=selected_root\n"
                "access_token=runtime_config.parent\n"
            ),
            "private.env": "access_token = local-config-secret-73\n",
        },
    )
    forbidden = _forbidden_file(tmp_path)

    report = _report_for_failure(auditor, repository, forbidden)

    assert report["violations"] == [{"count": 1, "path": "private.env", "rule": "token_like"}]


@pytest.mark.parametrize(
    "key_kind",
    ["", "RSA ", "DSA ", "EC ", "OPENSSH ", "ENCRYPTED "],
)
def test_private_key_headers_are_rejected_even_with_lowercase_bodies(
    tmp_path: Path,
    key_kind: str,
) -> None:
    auditor = _load_auditor()
    document = (
        f"-----BEGIN {key_kind}PRIVATE KEY-----\n"
        + ("a" * 64)
        + f"\n-----END {key_kind}PRIVATE KEY-----\n"
    )
    repository = _repository(tmp_path, {"fixture.pem": document})
    forbidden = _forbidden_file(tmp_path)

    report = _report_for_failure(auditor, repository, forbidden)

    assert report["violations"] == [{"count": 1, "path": "fixture.pem", "rule": "private_key"}]
    assert "private_key" not in auditor._ALLOWLIST_RULES


def test_failure_report_aggregates_only_safe_paths_rules_and_counts(tmp_path: Path) -> None:
    auditor = _load_auditor()
    token = "sk-" + "Z" * 24
    repository = _repository(
        tmp_path,
        {
            "one.txt": token,
            "two.txt": token,
            "private-project-name.txt": "safe body",
        },
    )
    forbidden = _forbidden_file(tmp_path)

    report = _report_for_failure(auditor, repository, forbidden)

    assert report["status"] == "FAIL"
    assert report["violation_count"] == 3
    assert {tuple(sorted(item)) for item in report["violations"]} == {
        ("count", "path", "rule"),
    }
    token_findings = [item for item in report["violations"] if item["rule"] == "token_like"]
    assert token_findings == [
        {"count": 1, "path": "one.txt", "rule": "token_like"},
        {"count": 1, "path": "two.txt", "rule": "token_like"},
    ]
    unsafe_path = next(
        item["path"] for item in report["violations"] if item["rule"] == "forbidden_term"
    )
    assert unsafe_path.startswith("path-sha256:")
    assert len(unsafe_path) == len("path-sha256:") + 64
    assert "private-project-name" not in json.dumps(report)


def test_exact_digest_and_rule_allowlist_exempts_only_the_reviewed_blob(tmp_path: Path) -> None:
    auditor = _load_auditor()
    token_document = ("example token: sk-" + "T" * 24 + "\n").encode()
    repository = _repository(tmp_path, {"tests/fixtures/token.txt": token_document})
    forbidden = _forbidden_file(tmp_path)
    exemption = {
        "path": "tests/fixtures/token.txt",
        "sha256": hashlib.sha256(token_document).hexdigest(),
        "rules": ["token_like"],
        "reason": "Intentional inert scanner test vector.",
    }
    _write_allowlist(repository, [exemption])
    _commit(repository, "review exact fixture")

    receipt = _audit(auditor, repository, forbidden)

    assert receipt["file_count"] == 2
    (repository / "tests/fixtures/token.txt").write_bytes(token_document + b"changed\n")
    _commit(repository, "change reviewed fixture")
    report = _report_for_failure(auditor, repository, forbidden)
    assert {"allowlist_stale", "token_like"}.issubset(_rules(report))


@pytest.mark.parametrize(
    "entry",
    [
        {
            "path": "tests/**",
            "sha256": "0" * 64,
            "rules": ["token_like"],
            "reason": "Glob must never be accepted.",
        },
        {
            "path": "tests/fixtures/token.txt/",
            "sha256": "0" * 64,
            "rules": ["token_like"],
            "reason": "Directory must never be accepted.",
        },
        {
            "path": "tests/fixtures/token.txt",
            "sha256": "A" * 64,
            "rules": ["token_like"],
            "reason": "Uppercase digest must never be accepted.",
        },
        {
            "path": "tests/fixtures/token.txt",
            "sha256": "0" * 64,
            "rules": ["made_up_rule"],
            "reason": "Unknown rule must never be accepted.",
        },
        {
            "path": "tests/fixtures/token.txt",
            "sha256": "0" * 64,
            "rules": ["token_like", "token_like"],
            "reason": "Duplicate rule must never be accepted.",
        },
        {
            "path": "tests/fixtures/token.txt",
            "sha256": "0" * 64,
            "rules": ["token_like"],
            "reason": " ",
        },
    ],
)
def test_allowlist_rejects_globs_directories_bad_digests_rules_and_reasons(
    tmp_path: Path,
    entry: dict[str, object],
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path, {"tests/fixtures/token.txt": "safe"})
    forbidden = _forbidden_file(tmp_path)
    _write_allowlist(repository, [entry])
    _commit(repository)

    report = _report_for_failure(auditor, repository, forbidden)

    assert _rules(report) == {"allowlist_invalid"}


def test_allowlist_rejects_duplicate_and_stale_entries(tmp_path: Path) -> None:
    auditor = _load_auditor()
    document = b"safe public text\n"
    repository = _repository(tmp_path, {"fixture.txt": document})
    forbidden = _forbidden_file(tmp_path)
    entry = {
        "path": "fixture.txt",
        "sha256": hashlib.sha256(document).hexdigest(),
        "rules": ["token_like"],
        "reason": "Claims a rule that the file no longer triggers.",
    }
    _write_allowlist(repository, [entry, entry])
    _commit(repository)
    assert _rules(_report_for_failure(auditor, repository, forbidden)) == {"allowlist_invalid"}

    _write_allowlist(repository, [entry])
    _commit(repository)
    assert _rules(_report_for_failure(auditor, repository, forbidden)) == {"allowlist_stale"}


def test_stale_allowlist_paths_are_reported_only_by_digest(tmp_path: Path) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    private_path = "private-project-name-missing.txt"
    _write_allowlist(
        repository,
        [
            {
                "path": private_path,
                "sha256": "0" * 64,
                "rules": ["token_like"],
                "reason": "Intentional stale-path regression fixture.",
            }
        ],
    )
    _commit(repository)

    report = _report_for_failure(auditor, repository, forbidden)

    stale = next(item for item in report["violations"] if item["rule"] == "allowlist_stale")
    assert stale["path"].startswith("path-sha256:")
    assert private_path not in json.dumps(report)


@pytest.mark.parametrize("unsafe_kind", ["mode", "empty", "invalid_utf8", "oversized"])
def test_external_forbidden_file_is_exact_0600_bounded_nonempty_utf8(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    if unsafe_kind == "mode":
        forbidden.chmod(0o400)
    elif unsafe_kind == "empty":
        forbidden.write_bytes(b"")
    elif unsafe_kind == "invalid_utf8":
        forbidden.write_bytes(b"\xff")
    else:
        forbidden.write_bytes(b"x" * (64 * 1024 + 1))

    report = _report_for_failure(auditor, repository, forbidden)

    assert _rules(report) == {"forbidden_file_invalid"}
    assert str(forbidden) not in json.dumps(report)


def test_external_forbidden_file_rejects_repo_paths_symlinks_and_hardlinks(
    tmp_path: Path,
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    inside = repository / "private.txt"
    inside.write_text("private", encoding="utf-8")
    inside.chmod(0o600)
    assert _rules(_report_for_failure(auditor, repository, inside)) == {"forbidden_file_invalid"}

    target = _forbidden_file(tmp_path)
    symlink = tmp_path / "terms-link.txt"
    symlink.symlink_to(target)
    assert _rules(_report_for_failure(auditor, repository, symlink)) == {"forbidden_file_invalid"}

    hardlink = tmp_path / "terms-hardlink.txt"
    os.link(target, hardlink)
    assert _rules(_report_for_failure(auditor, repository, target)) == {"forbidden_file_invalid"}


def test_external_forbidden_file_revalidates_the_path_after_fd_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    original_stat = auditor.os.stat
    selected_calls = 0

    def rebound_stat(path: object, *arguments: object, **keywords: object) -> object:
        nonlocal selected_calls
        metadata = original_stat(path, *arguments, **keywords)
        if Path(path) != forbidden:
            return metadata
        selected_calls += 1
        if selected_calls == 1:
            return metadata
        return SimpleNamespace(
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino + 1,
            st_size=metadata.st_size,
            st_mode=metadata.st_mode,
            st_uid=metadata.st_uid,
            st_nlink=metadata.st_nlink,
            st_mtime_ns=metadata.st_mtime_ns,
            st_ctime_ns=metadata.st_ctime_ns,
        )

    monkeypatch.setattr(auditor.os, "stat", rebound_stat)

    report = _report_for_failure(auditor, repository, forbidden)

    assert _rules(report) == {"forbidden_file_invalid"}
    assert selected_calls >= 2


def test_tree_modes_reject_symlinks_and_gitlinks(tmp_path: Path) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    link = repository / "public-link"
    link.symlink_to("README.md")
    _commit(repository, "track symlink")
    report = _report_for_failure(auditor, repository, forbidden)
    assert "symlink" in _rules(report)

    link.unlink()
    _git(repository, "rm", "--cached", "public-link")
    target_commit = _git(repository, "rev-parse", "HEAD")
    _git(repository, "update-index", "--add", "--cacheinfo", f"160000,{target_commit},vendor")
    _git(repository, "commit", "-qm", "track gitlink")
    report = _report_for_failure(auditor, repository, forbidden)
    assert "gitlink" in _rules(report)


def test_structural_findings_never_disclose_a_private_symlink_path(tmp_path: Path) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    private_path = "private-project-name-link"
    (repository / private_path).symlink_to("README.md")
    _commit(repository, "track private-named symlink")

    report = _report_for_failure(auditor, repository, forbidden)

    symlink = next(item for item in report["violations"] if item["rule"] == "symlink")
    assert symlink["path"].startswith("path-sha256:")
    assert private_path not in json.dumps(report)


@pytest.mark.parametrize(
    "names",
    [
        [b"unsafe\xff.txt"],
        [b"line\nbreak.txt"],
        [b"README.txt", b"readme.txt"],
    ],
)
def test_tree_rejects_non_utf8_dangerous_and_conflicting_paths_by_digest_id(
    tmp_path: Path,
    names: list[bytes],
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    commit = _commit_with_root_records(repository, [(name, b"safe") for name in names])

    report = _report_for_failure(auditor, repository, forbidden, ref=commit)

    assert _rules(report) <= {"path_conflict", "path_not_utf8", "unsafe_path"}
    assert _rules(report)
    assert all(item["path"].startswith("path-sha256:") for item in report["violations"])
    serialized = json.dumps(report)
    assert "line\\nbreak" not in serialized
    assert "README.txt" not in serialized
    assert "readme.txt" not in serialized


def test_docs_assets_are_verified_from_a_staged_object_snapshot(tmp_path: Path) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path, {"docs/assets/manifest.json": "{}\n"})
    forbidden = _forbidden_file(tmp_path)
    (repository / "docs/assets/manifest.json").write_text(
        json.dumps({"working_tree_only": True}),
        encoding="utf-8",
    )

    report = _report_for_failure(auditor, repository, forbidden)

    assert "public_assets_invalid" in _rules(report)
    assert report["violations"] == [
        {"count": 1, "path": "docs/assets", "rule": "public_assets_invalid"}
    ]


def test_staged_asset_snapshot_canonicalizes_a_symlinked_temporary_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auditor = _load_auditor()
    real_root = tmp_path / "private" / "staged-assets"
    real_root.mkdir(parents=True)
    alias = tmp_path / "temporary-alias"
    alias.symlink_to(real_root, target_is_directory=True)

    class TemporaryAlias:
        def __enter__(self) -> str:
            return str(alias)

        def __exit__(self, *_arguments: object) -> None:
            return None

    def verify(
        asset_root: Path,
        *,
        repository_root: Path,
        denylist_path: Path,
    ) -> None:
        assert repository_root == tmp_path
        assert asset_root == real_root / "assets"
        assert denylist_path == real_root / "forbidden-terms.txt"

    monkeypatch.setattr(auditor.tempfile, "TemporaryDirectory", lambda **_kwargs: TemporaryAlias())
    monkeypatch.setattr(auditor, "_verify_public_assets", verify)

    auditor._verify_staged_assets(
        tmp_path,
        {"demo-manifest.json": b"{}\n"},
        b"private-project-name\n",
    )


def test_receipt_hashes_the_real_demo_manifest_object(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auditor = _load_auditor()
    demo_manifest = b'{"schema_version":1}\n'
    legacy_manifest = b'{"legacy":true}\n'
    repository = _repository(
        tmp_path,
        {
            "docs/assets/demo-manifest.json": demo_manifest,
            "docs/assets/manifest.json": legacy_manifest,
        },
    )
    forbidden = _forbidden_file(tmp_path)
    monkeypatch.setattr(auditor, "_verify_staged_assets", lambda *_arguments: None)

    receipt = _audit(auditor, repository, forbidden)

    assert receipt["manifest_sha256"] == hashlib.sha256(demo_manifest).hexdigest()


def test_cli_supports_fixed_arguments_and_emits_only_canonical_json(tmp_path: Path) -> None:
    _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    receipt_path = tmp_path / "cli-receipt.json"

    completed = _run(
        repository,
        sys.executable,
        str(AUDITOR_PATH),
        "--mode",
        "snapshot",
        "--ref",
        "HEAD",
        "--forbidden-file",
        str(forbidden),
        "--allowlist",
        "config/public-release-allowlist.toml",
        "--receipt",
        str(receipt_path),
    )

    receipt = json.loads(completed.stdout)
    assert receipt["mode"] == "snapshot"
    assert (
        completed.stdout
        == (
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode()
    )
    assert completed.stderr == b""
    assert json.loads(receipt_path.read_text(encoding="utf-8")) == receipt


def test_cli_failure_never_echoes_matching_content(tmp_path: Path) -> None:
    _load_auditor()
    secret = "private-project-name"
    repository = _repository(tmp_path, {"private.txt": secret})
    forbidden = _forbidden_file(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            str(AUDITOR_PATH),
            "--mode",
            "tree",
            "--forbidden-file",
            str(forbidden),
        ],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    report = json.loads(completed.stderr)
    assert report["status"] == "FAIL"
    assert secret.encode() not in completed.stderr


def test_git_timeout_maps_to_a_stable_non_disclosing_object_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auditor = _load_auditor()
    repository = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    original_run = auditor.subprocess.run
    observed_timeout: list[float] = []

    def timed_run(command: list[str], **keywords: object) -> subprocess.CompletedProcess[bytes]:
        if "ls-tree" in command:
            observed_timeout.append(keywords["timeout"])
            raise subprocess.TimeoutExpired(command, keywords["timeout"])
        return original_run(command, **keywords)

    monkeypatch.setattr(auditor.subprocess, "run", timed_run)

    report = _report_for_failure(auditor, repository, forbidden)

    assert _rules(report) == {"git_object_invalid"}
    assert observed_timeout and 0 < observed_timeout[0] <= 60


def test_source_contract_uses_only_no_replace_git_object_reads() -> None:
    source = AUDITOR_PATH.read_text(encoding="utf-8")

    assert "--no-replace-objects" in source
    assert "ls-tree" in source
    assert "cat-file" in source
    for forbidden_operation in (
        "git show",
        "git archive",
        "git grep",
        "git checkout",
        "git switch",
    ):
        assert forbidden_operation not in source
