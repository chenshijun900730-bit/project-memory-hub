#!/usr/bin/env python3
"""Build a deterministic, single-root public snapshot without checking files out."""

from __future__ import annotations

import argparse
import ctypes
import errno
import importlib
import json
import os
import re
import secrets
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast


try:
    _auditor_module = importlib.import_module("scripts.audit_public_tree")
except ModuleNotFoundError:
    try:
        _auditor_module = importlib.import_module("audit_public_tree")
    except ModuleNotFoundError:

        def audit_public_tree(**_kwargs: object) -> dict[str, object]:
            raise RuntimeError("public tree auditor is unavailable")

    else:
        audit_public_tree = cast(
            Callable[..., dict[str, object]],
            _auditor_module.audit_public_tree,
        )
else:
    audit_public_tree = cast(
        Callable[..., dict[str, object]],
        _auditor_module.audit_public_tree,
    )


APPROVED_BRANCH = "codex/public-beta-0.2.1"
AUDITOR_NAME = "project-memory-hub-public-tree"
POLICY_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "auditor",
        "policy_version",
        "mode",
        "source_commit",
        "tree",
        "allowlist_sha256",
        "forbidden_terms_sha256",
        "manifest_sha256",
        "file_count",
        "total_bytes",
    }
)
ALLOWED_GIT_SUBCOMMANDS = frozenset(
    {
        "cat-file",
        "commit-tree",
        "for-each-ref",
        "ls-files",
        "ls-tree",
        "read-tree",
        "rev-parse",
        "show-ref",
        "symbolic-ref",
        "update-ref",
        "worktree",
        "write-tree",
    }
)
FIXED_NAME = "Project Memory Hub Maintainers"
FIXED_EMAIL = "noreply@project-memory-hub.invalid"
FIXED_MESSAGE = "chore: create public beta 0.2.1 snapshot\n"
ZERO_OID = "0" * 40
MAX_RECEIPT_BYTES = 64 * 1024
MAX_SOURCE_COMMIT_BYTES = 1024 * 1024
OID_PATTERN = re.compile(r"[0-9a-f]{40}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class PublicSnapshotError(RuntimeError):
    """A path-safe public snapshot failure."""


@dataclass(frozen=True)
class SnapshotResult:
    branch: str
    commit: str
    tree: str
    worktree: Path


@dataclass(frozen=True)
class _TreeEntry:
    mode: str
    oid: str
    path: bytes
    payload: bytes


@dataclass
class _WorktreeReservation:
    final_path: Path
    parent_fd: int
    parent_identity: tuple[int, int, int, int]
    anchor_path: Path
    anchor_fd: int
    anchor_identity: tuple[int, int, int, int]
    staging_path: Path
    staging_fd: int
    staging_identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _WorktreeMetadata:
    path: Path
    parent_fd: int
    parent_identity: tuple[int, int, int, int]
    directory_fd: int
    directory_identity: tuple[int, int, int, int]
    gitdir_identity: tuple[int, int, int, int, int]


def _git_environment(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    if overrides is not None:
        environment.update(overrides)
    return environment


def _run_git(
    repository: Path,
    *arguments: str,
    input_bytes: bytes | None = None,
    env_overrides: Mapping[str, str] | None = None,
    check: bool = True,
) -> bytes:
    if not arguments or arguments[0] not in ALLOWED_GIT_SUBCOMMANDS:
        raise PublicSnapshotError("git subcommand is not allowlisted")
    command = [
        "git",
        "--no-replace-objects",
        "-c",
        f"core.hooksPath={os.devnull}",
        "-c",
        "core.fsmonitor=false",
        *arguments,
    ]
    run_options: dict[str, Any] = {
        "cwd": repository,
        "env": _git_environment(env_overrides),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "timeout": 30,
        "check": False,
    }
    if input_bytes is None:
        run_options["stdin"] = subprocess.DEVNULL
    else:
        run_options["input"] = input_bytes
    try:
        completed = subprocess.run(command, **run_options)
    except (OSError, subprocess.SubprocessError) as error:
        raise PublicSnapshotError(f"git {arguments[0]} failed") from error
    if check and completed.returncode != 0:
        raise PublicSnapshotError(f"git {arguments[0]} failed")
    return bytes(completed.stdout)


def _git_scalar(repository: Path, *arguments: str, check: bool = True) -> str:
    return _run_git(repository, *arguments, check=check).decode("ascii").strip()


def _git_path(repository: Path, *arguments: str) -> Path:
    payload = _run_git(repository, *arguments)
    raw_path = payload[:-1] if payload.endswith(b"\n") else payload
    if not raw_path or b"\0" in raw_path:
        raise PublicSnapshotError("git path output is invalid")
    try:
        return Path(os.fsdecode(raw_path))
    except (TypeError, UnicodeError, ValueError) as error:
        raise PublicSnapshotError("git path output is invalid") from error


def _repository_root(repository: Path) -> Path:
    if not repository.is_dir():
        raise PublicSnapshotError("source repository is unavailable")
    payload = _run_git(repository, "rev-parse", "--show-toplevel")
    raw_path = payload[:-1] if payload.endswith(b"\n") else payload
    try:
        root = Path(os.fsdecode(raw_path)).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise PublicSnapshotError("source repository root is invalid") from error
    if not root.is_dir():
        raise PublicSnapshotError("source repository root is invalid")
    return root


def _safe_tree_path(path: bytes) -> tuple[bytes, ...]:
    components = tuple(path.split(b"/"))
    if (
        not path
        or path.startswith(b"/")
        or any(component in {b"", b".", b".."} for component in components)
        or any(component.lower() == b".git" for component in components)
    ):
        raise PublicSnapshotError("tree contains an unsafe path")
    return components


def _index_entries(repository: Path) -> tuple[tuple[str, str, bytes], ...]:
    entries: list[tuple[str, str, bytes]] = []
    for record in _run_git(repository, "ls-files", "--stage", "-z").split(b"\0"):
        if not record:
            continue
        try:
            metadata, path = record.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split(" ")
        except (UnicodeError, ValueError) as error:
            raise PublicSnapshotError("source index is malformed") from error
        if stage != "0" or not OID_PATTERN.fullmatch(oid):
            raise PublicSnapshotError("source index is not clean")
        _safe_tree_path(path)
        entries.append((mode, oid, path))
    return tuple(entries)


def _tree_index_entries(repository: Path, source: str) -> tuple[tuple[str, str, bytes], ...]:
    entries: list[tuple[str, str, bytes]] = []
    payload = _run_git(repository, "ls-tree", "-r", "-z", "--full-tree", source)
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            metadata, path = record.split(b"\t", 1)
            mode_raw, kind, oid_raw = metadata.split(b" ")
            mode = mode_raw.decode("ascii")
            oid = oid_raw.decode("ascii")
        except (UnicodeError, ValueError) as error:
            raise PublicSnapshotError("source tree inventory is malformed") from error
        if kind != b"blob" or OID_PATTERN.fullmatch(oid) is None:
            raise PublicSnapshotError("source tree contains an unsupported entry")
        _safe_tree_path(path)
        entries.append((mode, oid, path))
    return tuple(entries)


def _validate_index_flags(repository: Path) -> None:
    for record in _run_git(repository, "ls-files", "-v", "-z").split(b"\0"):
        if record and not record.startswith(b"H "):
            raise PublicSnapshotError("source index flag is not permitted")


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _anchored_lstat(path: Path) -> os.stat_result:
    parent_fd = _open_absolute_directory(path.parent)
    try:
        return os.stat(
            os.fsencode(path.name),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
    finally:
        os.close(parent_fd)


def _read_regular_file(
    path: Path,
    initial: os.stat_result,
    *,
    max_bytes: int,
    failure: str,
) -> bytes:
    if not hasattr(os, "O_NOFOLLOW"):
        raise PublicSnapshotError(failure)
    parent_fd = -1
    file_fd = -1
    try:
        parent_fd = _open_absolute_directory(path.parent)
        flags = os.O_RDONLY | os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        file_fd = os.open(os.fsencode(path.name), flags, dir_fd=parent_fd)
        opened = os.fstat(file_fd)
        if (
            not stat.S_ISREG(opened.st_mode)
            or _file_identity(opened) != _file_identity(initial)
            or opened.st_size < 0
            or opened.st_size > max_bytes
        ):
            raise PublicSnapshotError(failure)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_fd, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise PublicSnapshotError(failure)
        descriptor_after = os.fstat(file_fd)
        entry_after = os.stat(
            os.fsencode(path.name),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        path_after = _anchored_lstat(path)
        identity = _file_identity(opened)
        if (
            _file_identity(descriptor_after) != identity
            or _file_identity(entry_after) != identity
            or _file_identity(path_after) != identity
        ):
            raise PublicSnapshotError(failure)
        return b"".join(chunks)
    except PublicSnapshotError:
        raise
    except OSError as error:
        raise PublicSnapshotError(failure) from error
    finally:
        if file_fd >= 0:
            os.close(file_fd)
        if parent_fd >= 0:
            os.close(parent_fd)


def _validate_tracked_worktree(
    repository: Path,
    entries: Sequence[tuple[str, str, bytes]],
) -> None:
    root = os.fsencode(repository)
    for mode, oid, relative in entries:
        full_path = root + b"/" + relative
        try:
            metadata = os.lstat(full_path)
        except OSError as error:
            raise PublicSnapshotError("source tracked file is not clean") from error
        expected = _run_git(repository, "cat-file", "blob", oid)
        if mode in {"100644", "100755"}:
            if not stat.S_ISREG(metadata.st_mode):
                raise PublicSnapshotError("source tracked file is not clean")
            executable = bool(metadata.st_mode & 0o111)
            if (
                executable != (mode == "100755")
                or _read_regular_file(
                    Path(os.fsdecode(full_path)),
                    metadata,
                    max_bytes=len(expected),
                    failure="source tracked file is not clean",
                )
                != expected
            ):
                raise PublicSnapshotError("source tracked file is not clean")
        elif mode == "120000":
            if not stat.S_ISLNK(metadata.st_mode):
                raise PublicSnapshotError("source tracked file is not clean")
            try:
                target = os.readlink(full_path)
            except OSError as error:
                raise PublicSnapshotError("source tracked file is not clean") from error
            if target != expected:
                raise PublicSnapshotError("source tracked file is not clean")
        else:
            raise PublicSnapshotError("source tree contains an unsupported entry")


def _validate_source_clean(repository: Path, source: str, expected_tree: str) -> None:
    current = _git_scalar(repository, "rev-parse", "--verify", "HEAD^{commit}")
    if current != source:
        raise PublicSnapshotError("source commit changed during snapshot preparation")
    _validate_index_flags(repository)
    entries = _index_entries(repository)
    if entries != _tree_index_entries(repository, source):
        raise PublicSnapshotError("source index is not clean")
    _validate_tracked_worktree(repository, entries)
    if _run_git(repository, "ls-files", "--others", "--exclude-standard", "-z"):
        raise PublicSnapshotError("source worktree is not clean")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PublicSnapshotError("receipt contains a duplicate JSON key")
        result[key] = value
    return result


def _load_receipt(path: Path) -> dict[str, object]:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise PublicSnapshotError("receipt is unavailable") from error
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PublicSnapshotError("receipt must be a regular file")
    if metadata.st_size <= 0 or metadata.st_size > MAX_RECEIPT_BYTES:
        raise PublicSnapshotError("receipt size is invalid")
    try:
        document = _read_regular_file(
            path,
            metadata,
            max_bytes=MAX_RECEIPT_BYTES,
            failure="receipt changed during secure read",
        )
        loaded = json.loads(document, object_pairs_hook=_reject_duplicate_keys)
    except PublicSnapshotError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PublicSnapshotError("receipt is invalid JSON") from error
    if not isinstance(loaded, dict):
        raise PublicSnapshotError("receipt must be a JSON object")
    return loaded


def _validate_receipt(
    receipt: Mapping[str, object],
    *,
    source: str,
    tree: str,
) -> dict[str, object]:
    if set(receipt) != RECEIPT_KEYS:
        raise PublicSnapshotError("receipt keys are not canonical")
    if type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1:
        raise PublicSnapshotError("receipt schema is invalid")
    if receipt["auditor"] != AUDITOR_NAME:
        raise PublicSnapshotError("receipt auditor is invalid")
    if type(receipt["policy_version"]) is not int or receipt["policy_version"] != POLICY_VERSION:
        raise PublicSnapshotError("receipt policy is invalid")
    if receipt["mode"] != "tree":
        raise PublicSnapshotError("receipt mode is invalid")
    if receipt["source_commit"] != source or receipt["tree"] != tree:
        raise PublicSnapshotError("receipt source identity is invalid")
    for key in (
        "allowlist_sha256",
        "forbidden_terms_sha256",
        "manifest_sha256",
    ):
        value = receipt[key]
        if not isinstance(value, str) or DIGEST_PATTERN.fullmatch(value) is None:
            raise PublicSnapshotError("receipt digest is invalid")
    for key in ("file_count", "total_bytes"):
        value = receipt[key]
        if type(value) is not int or value < 0:
            raise PublicSnapshotError("receipt count is invalid")
    return dict(receipt)


def _existing_worktrees(repository: Path) -> tuple[Path, ...]:
    paths: list[Path] = []
    payload = _run_git(repository, "worktree", "list", "--porcelain", "-z")
    for field in payload.split(b"\0"):
        if field.startswith(b"worktree "):
            raw_path = field[len(b"worktree ") :]
            try:
                paths.append(Path(os.fsdecode(raw_path)).resolve(strict=True))
            except OSError as error:
                raise PublicSnapshotError("existing worktree inventory is invalid") from error
    return tuple(paths)


def _check_no_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError as error:
            raise PublicSnapshotError("worktree parent is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PublicSnapshotError("worktree path contains a symlink component")


def _is_within(candidate: Path, root: Path) -> bool:
    return candidate == root or root in candidate.parents


def _validate_worktree_path(repository: Path, worktree: Path) -> Path:
    if not worktree.is_absolute():
        raise PublicSnapshotError("worktree path must be absolute")
    if os.path.lexists(worktree):
        raise PublicSnapshotError("worktree path already exists")
    parent = worktree.parent
    _check_no_symlink_components(parent)
    try:
        resolved_parent = parent.resolve(strict=True)
    except OSError as error:
        raise PublicSnapshotError("worktree parent is unavailable") from error
    if not resolved_parent.is_dir():
        raise PublicSnapshotError("worktree parent is unavailable")
    candidate = resolved_parent / worktree.name
    if candidate != worktree:
        raise PublicSnapshotError("worktree path is not canonical")
    for existing in _existing_worktrees(repository):
        if _is_within(candidate, existing) or _is_within(existing, candidate):
            raise PublicSnapshotError("worktree path overlaps an existing worktree")
    return candidate


def _ref_oid(repository: Path, ref: str) -> str | None:
    value = _git_scalar(repository, "show-ref", "--verify", "--hash", ref, check=False)
    return value or None


def _read_tree_entries(repository: Path, source: str) -> tuple[_TreeEntry, ...]:
    entries: list[_TreeEntry] = []
    payload = _run_git(repository, "ls-tree", "-r", "-z", "--full-tree", source)
    for record in payload.split(b"\0"):
        if not record:
            continue
        try:
            metadata, relative = record.split(b"\t", 1)
            mode_raw, kind, oid_raw = metadata.split(b" ")
            mode = mode_raw.decode("ascii")
            oid = oid_raw.decode("ascii")
        except (UnicodeError, ValueError) as error:
            raise PublicSnapshotError("source tree inventory is malformed") from error
        _safe_tree_path(relative)
        if kind != b"blob" or mode not in {"100644", "100755", "120000"}:
            raise PublicSnapshotError("source tree contains an unsupported entry")
        if OID_PATTERN.fullmatch(oid) is None:
            raise PublicSnapshotError("source tree inventory is malformed")
        entries.append(
            _TreeEntry(
                mode=mode,
                oid=oid,
                path=relative,
                payload=_run_git(repository, "cat-file", "blob", oid),
            )
        )
    return tuple(entries)


def _open_directory(parent_fd: int, component: bytes) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(component, flags, dir_fd=parent_fd)


def _open_absolute_directory(path: Path) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    current_fd = os.open(os.fsencode(path.anchor), flags)
    try:
        for component in path.parts[1:]:
            next_fd = _open_directory(current_fd, os.fsencode(component))
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_mode,
    )


def _entry_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_uid,
        metadata.st_mode,
        metadata.st_nlink,
    )


def _owned_private_directory(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(metadata.st_mode)
        and metadata.st_uid == os.geteuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _fd_directory_matches(
    descriptor: int,
    expected: tuple[int, int, int, int],
) -> bool:
    try:
        metadata = os.fstat(descriptor)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and _directory_identity(metadata) == expected


def _entry_directory_matches(
    parent_fd: int,
    name: bytes,
    expected: tuple[int, int, int, int],
) -> bool:
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return False
    return stat.S_ISDIR(metadata.st_mode) and _directory_identity(metadata) == expected


def _absolute_directory_matches(
    path: Path,
    expected: tuple[int, int, int, int],
) -> bool:
    descriptor = -1
    try:
        descriptor = _open_absolute_directory(path)
        return _fd_directory_matches(descriptor, expected)
    except OSError:
        return False
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _reserve_worktree(path: Path) -> _WorktreeReservation:
    parent_fd = -1
    anchor_fd = -1
    staging_fd = -1
    anchor_name = b""
    try:
        parent_fd = _open_absolute_directory(path.parent)
        parent_metadata = os.fstat(parent_fd)
        parent_identity = _directory_identity(parent_metadata)
        if not _absolute_directory_matches(path.parent, parent_identity):
            raise PublicSnapshotError("worktree parent changed during reservation")
        try:
            os.stat(
                os.fsencode(path.name),
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise PublicSnapshotError("worktree path already exists")

        for _attempt in range(32):
            anchor_name = f".pmh-public-snapshot-{secrets.token_hex(12)}".encode("ascii")
            try:
                os.mkdir(anchor_name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                continue
            break
        else:
            raise PublicSnapshotError("worktree private anchor could not be reserved")

        anchor_fd = _open_directory(parent_fd, anchor_name)
        os.fchmod(anchor_fd, 0o700)
        anchor_metadata = os.fstat(anchor_fd)
        if not _owned_private_directory(anchor_metadata):
            raise PublicSnapshotError("worktree private anchor is not owned securely")
        anchor_identity = _directory_identity(anchor_metadata)
        os.mkdir(b"worktree", mode=0o700, dir_fd=anchor_fd)
        staging_fd = _open_directory(anchor_fd, b"worktree")
        os.fchmod(staging_fd, 0o700)
        staging_metadata = os.fstat(staging_fd)
        if not _owned_private_directory(staging_metadata):
            raise PublicSnapshotError("worktree reservation is not owned securely")
        staging_identity = _directory_identity(staging_metadata)
        anchor_path = path.parent / os.fsdecode(anchor_name)
        staging_path = anchor_path / "worktree"
        if (
            not _entry_directory_matches(parent_fd, anchor_name, anchor_identity)
            or not _entry_directory_matches(anchor_fd, b"worktree", staging_identity)
            or not _absolute_directory_matches(anchor_path, anchor_identity)
            or not _absolute_directory_matches(staging_path, staging_identity)
        ):
            raise PublicSnapshotError("worktree reservation changed during creation")
        return _WorktreeReservation(
            final_path=path,
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            anchor_path=anchor_path,
            anchor_fd=anchor_fd,
            anchor_identity=anchor_identity,
            staging_path=staging_path,
            staging_fd=staging_fd,
            staging_identity=staging_identity,
        )
    except PublicSnapshotError:
        raise
    except OSError as error:
        raise PublicSnapshotError("worktree parent changed or contains a symlink") from error
    finally:
        if sys.exc_info()[0] is not None:
            if staging_fd >= 0:
                os.close(staging_fd)
            if anchor_fd >= 0:
                try:
                    os.rmdir(b"worktree", dir_fd=anchor_fd)
                except OSError:
                    pass
                os.close(anchor_fd)
            if parent_fd >= 0:
                if anchor_name:
                    try:
                        os.rmdir(anchor_name, dir_fd=parent_fd)
                    except OSError:
                        pass
                os.close(parent_fd)


def _assert_reservation_intact(reservation: _WorktreeReservation) -> None:
    if (
        not _fd_directory_matches(reservation.parent_fd, reservation.parent_identity)
        or not _absolute_directory_matches(
            reservation.final_path.parent,
            reservation.parent_identity,
        )
        or not _fd_directory_matches(reservation.anchor_fd, reservation.anchor_identity)
        or not _entry_directory_matches(
            reservation.parent_fd,
            os.fsencode(reservation.anchor_path.name),
            reservation.anchor_identity,
        )
        or not _absolute_directory_matches(
            reservation.anchor_path,
            reservation.anchor_identity,
        )
        or not _fd_directory_matches(reservation.staging_fd, reservation.staging_identity)
        or not _entry_directory_matches(
            reservation.anchor_fd,
            b"worktree",
            reservation.staging_identity,
        )
        or not _absolute_directory_matches(
            reservation.staging_path,
            reservation.staging_identity,
        )
    ):
        raise PublicSnapshotError("worktree reservation changed or was replaced")


def _rename_directory_noreplace(
    source_parent_fd: int,
    source_name: bytes,
    destination_parent_fd: int,
    destination_name: bytes,
    *,
    operation: str = "worktree publish",
) -> None:
    library = ctypes.CDLL(None, use_errno=True)
    function_name: str
    flag: int
    if sys.platform == "darwin":
        function_name = "renameatx_np"
        flag = 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"
        flag = 0x00000001  # RENAME_NOREPLACE
    else:
        raise PublicSnapshotError(f"atomic no-replace {operation} is unsupported")
    try:
        rename_function = getattr(library, function_name)
    except AttributeError as error:
        raise PublicSnapshotError(f"atomic no-replace {operation} is unavailable") from error
    rename_function.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    rename_function.restype = ctypes.c_int
    if (
        rename_function(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            flag,
        )
        != 0
    ):
        error_number = ctypes.get_errno()
        if error_number in {errno.EEXIST, errno.ENOTEMPTY}:
            raise PublicSnapshotError(f"{operation} destination already exists")
        raise PublicSnapshotError(f"{operation} failed") from OSError(
            error_number, os.strerror(error_number)
        )


def _open_parent(root_fd: int, components: Sequence[bytes]) -> int:
    current_fd = os.dup(root_fd)
    try:
        for component in components:
            try:
                os.mkdir(component, mode=0o755, dir_fd=current_fd)
            except FileExistsError:
                pass
            next_fd = _open_directory(current_fd, component)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except Exception:
        os.close(current_fd)
        raise


def _write_regular(parent_fd: int, name: bytes, payload: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_fd = os.open(name, flags, mode, dir_fd=parent_fd)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(file_fd, view)
            if written <= 0:
                raise PublicSnapshotError("worktree blob write failed")
            view = view[written:]
        os.fchmod(file_fd, mode)
    finally:
        os.close(file_fd)


def _materialize_tree(
    repository: Path,
    worktree: Path,
    entries: Sequence[_TreeEntry],
) -> None:
    del repository
    flags = os.O_RDONLY | os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    root_fd = os.open(worktree, flags)
    try:
        for entry in entries:
            components = _safe_tree_path(entry.path)
            parent_fd = _open_parent(root_fd, components[:-1])
            try:
                if entry.mode == "120000":
                    os.symlink(entry.payload, components[-1], dir_fd=parent_fd)
                else:
                    _write_regular(
                        parent_fd,
                        components[-1],
                        entry.payload,
                        0o755 if entry.mode == "100755" else 0o644,
                    )
            finally:
                os.close(parent_fd)
    except (OSError, ValueError) as error:
        raise PublicSnapshotError("worktree materialization failed") from error
    finally:
        os.close(root_fd)


def _read_bound_entry(
    parent_fd: int,
    name: bytes,
    *,
    max_bytes: int,
    failure: str,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    descriptor = -1
    try:
        initial = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(initial.st_mode)
            or initial.st_uid != os.geteuid()
            or initial.st_nlink != 1
            or initial.st_size < 0
            or initial.st_size > max_bytes
        ):
            raise PublicSnapshotError(failure)
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(name, flags, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        identity = _entry_identity(initial)
        if _entry_identity(opened) != identity:
            raise PublicSnapshotError(failure)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(4096, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise PublicSnapshotError(failure)
        after_descriptor = os.fstat(descriptor)
        after_entry = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _entry_identity(after_descriptor) != identity
            or _entry_identity(after_entry) != identity
        ):
            raise PublicSnapshotError(failure)
        return b"".join(chunks), identity
    except PublicSnapshotError:
        raise
    except OSError as error:
        raise PublicSnapshotError(failure) from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _bind_worktree_metadata(
    repository: Path,
    reservation: _WorktreeReservation,
) -> _WorktreeMetadata:
    _assert_reservation_intact(reservation)
    marker, _marker_identity = _read_bound_entry(
        reservation.staging_fd,
        b".git",
        max_bytes=4096,
        failure="worktree metadata marker is invalid",
    )
    if not marker.startswith(b"gitdir: ") or not marker.endswith(b"\n") or marker.count(b"\n") != 1:
        raise PublicSnapshotError("worktree metadata marker is invalid")
    try:
        metadata_path = Path(os.fsdecode(marker[len(b"gitdir: ") : -1]))
    except UnicodeError as error:
        raise PublicSnapshotError("worktree metadata marker is invalid") from error
    if not metadata_path.is_absolute() or metadata_path.name in {"", ".", ".."}:
        raise PublicSnapshotError("worktree metadata marker is invalid")

    try:
        common_path = _git_path(
            repository,
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    except PublicSnapshotError as error:
        raise PublicSnapshotError("worktree metadata root is invalid") from error
    if not common_path.is_absolute():
        raise PublicSnapshotError("worktree metadata root is invalid")
    _check_no_symlink_components(common_path)
    try:
        common_path = common_path.resolve(strict=True)
    except OSError as error:
        raise PublicSnapshotError("worktree metadata root is invalid") from error
    metadata_parent = common_path / "worktrees"
    if metadata_path.parent != metadata_parent:
        raise PublicSnapshotError("worktree metadata escaped the repository")

    parent_fd = -1
    directory_fd = -1
    bound = False
    try:
        parent_fd = _open_absolute_directory(metadata_parent)
        parent_metadata = os.fstat(parent_fd)
        parent_identity = _directory_identity(parent_metadata)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.geteuid()
            or parent_metadata.st_mode & 0o022
            or not _absolute_directory_matches(metadata_parent, parent_identity)
        ):
            raise PublicSnapshotError("worktree metadata parent is not owned securely")
        entry_metadata = os.stat(
            os.fsencode(metadata_path.name),
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISDIR(entry_metadata.st_mode)
            or entry_metadata.st_uid != os.geteuid()
            or entry_metadata.st_mode & 0o022
        ):
            raise PublicSnapshotError("worktree metadata directory is not owned securely")
        directory_identity = _directory_identity(entry_metadata)
        directory_fd = _open_directory(parent_fd, os.fsencode(metadata_path.name))
        if not _fd_directory_matches(directory_fd, directory_identity):
            raise PublicSnapshotError("worktree metadata directory was replaced")
        gitdir, gitdir_identity = _read_bound_entry(
            directory_fd,
            b"gitdir",
            max_bytes=4096,
            failure="worktree metadata gitdir is invalid",
        )
        expected = os.fsencode(reservation.staging_path / ".git") + b"\n"
        if gitdir != expected:
            raise PublicSnapshotError("worktree metadata does not own the reservation")
        metadata = _WorktreeMetadata(
            path=metadata_path,
            parent_fd=parent_fd,
            parent_identity=parent_identity,
            directory_fd=directory_fd,
            directory_identity=directory_identity,
            gitdir_identity=gitdir_identity,
        )
        bound = True
        return metadata
    except PublicSnapshotError:
        raise
    except OSError as error:
        raise PublicSnapshotError("worktree metadata is unavailable") from error
    finally:
        if not bound:
            if directory_fd >= 0:
                os.close(directory_fd)
            if parent_fd >= 0:
                os.close(parent_fd)


def _metadata_directory_matches(metadata: _WorktreeMetadata) -> bool:
    return (
        _fd_directory_matches(metadata.parent_fd, metadata.parent_identity)
        and _absolute_directory_matches(metadata.path.parent, metadata.parent_identity)
        and _fd_directory_matches(metadata.directory_fd, metadata.directory_identity)
        and _entry_directory_matches(
            metadata.parent_fd,
            os.fsencode(metadata.path.name),
            metadata.directory_identity,
        )
    )


def _read_metadata_gitdir(metadata: _WorktreeMetadata) -> bytes:
    if not _metadata_directory_matches(metadata):
        raise PublicSnapshotError("worktree metadata directory was replaced")
    payload, identity = _read_bound_entry(
        metadata.directory_fd,
        b"gitdir",
        max_bytes=4096,
        failure="worktree metadata gitdir was replaced",
    )
    if identity != metadata.gitdir_identity:
        raise PublicSnapshotError("worktree metadata gitdir was replaced")
    return payload


def _rewrite_metadata_gitdir(
    metadata: _WorktreeMetadata,
    *,
    expected: bytes,
    replacement: bytes,
) -> None:
    if _read_metadata_gitdir(metadata) != expected:
        raise PublicSnapshotError("worktree metadata gitdir changed unexpectedly")
    descriptor = -1
    try:
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        descriptor = os.open(b"gitdir", flags, dir_fd=metadata.directory_fd)
        if _entry_identity(os.fstat(descriptor)) != metadata.gitdir_identity:
            raise PublicSnapshotError("worktree metadata gitdir was replaced")
        os.lseek(descriptor, 0, os.SEEK_SET)
        view = memoryview(replacement)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise PublicSnapshotError("worktree metadata gitdir update failed")
            view = view[written:]
        os.ftruncate(descriptor, len(replacement))
        os.fsync(descriptor)
        after_descriptor = os.fstat(descriptor)
        after_entry = os.stat(
            b"gitdir",
            dir_fd=metadata.directory_fd,
            follow_symlinks=False,
        )
        if (
            _entry_identity(after_descriptor) != metadata.gitdir_identity
            or _entry_identity(after_entry) != metadata.gitdir_identity
        ):
            raise PublicSnapshotError("worktree metadata gitdir was replaced")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, len(replacement) + 1) != replacement:
            raise PublicSnapshotError("worktree metadata gitdir update failed")
    except PublicSnapshotError:
        raise
    except OSError as error:
        raise PublicSnapshotError("worktree metadata gitdir update failed") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _publish_worktree(
    reservation: _WorktreeReservation,
    metadata: _WorktreeMetadata,
) -> None:
    _assert_reservation_intact(reservation)
    final_name = os.fsencode(reservation.final_path.name)
    try:
        os.stat(final_name, dir_fd=reservation.parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise PublicSnapshotError("worktree publish destination is unavailable") from error
    else:
        raise PublicSnapshotError("worktree publish destination already exists")
    staging_gitdir = os.fsencode(reservation.staging_path / ".git") + b"\n"
    final_gitdir = os.fsencode(reservation.final_path / ".git") + b"\n"
    _rewrite_metadata_gitdir(
        metadata,
        expected=staging_gitdir,
        replacement=final_gitdir,
    )
    try:
        _rename_directory_noreplace(
            reservation.anchor_fd,
            b"worktree",
            reservation.parent_fd,
            final_name,
        )
    except Exception:
        try:
            _rewrite_metadata_gitdir(
                metadata,
                expected=final_gitdir,
                replacement=staging_gitdir,
            )
        except PublicSnapshotError:
            pass
        raise
    if (
        not _fd_directory_matches(reservation.staging_fd, reservation.staging_identity)
        or not _entry_directory_matches(
            reservation.parent_fd,
            final_name,
            reservation.staging_identity,
        )
        or not _absolute_directory_matches(
            reservation.final_path,
            reservation.staging_identity,
        )
        or not _absolute_directory_matches(
            reservation.final_path.parent,
            reservation.parent_identity,
        )
        or _read_metadata_gitdir(metadata) != final_gitdir
    ):
        raise PublicSnapshotError("published worktree identity changed")
    _isolate_and_remove_owned_directory(
        reservation.parent_fd,
        os.fsencode(reservation.anchor_path.name),
        reservation.anchor_fd,
        reservation.anchor_identity,
    )


def _actual_worktree_entries(worktree: Path) -> dict[bytes, os.stat_result]:
    found: dict[bytes, os.stat_result] = {}

    def visit(directory: bytes, prefix: bytes) -> None:
        try:
            children = list(os.scandir(directory))
        except OSError as error:
            raise PublicSnapshotError("worktree verification failed") from error
        for child in children:
            name = child.name
            if not isinstance(name, bytes):
                raise PublicSnapshotError("worktree verification failed")
            if not prefix and name == b".git":
                continue
            relative = name if not prefix else prefix + b"/" + name
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise PublicSnapshotError("worktree verification failed") from error
            found[relative] = metadata
            if stat.S_ISDIR(metadata.st_mode):
                visit(os.path.join(directory, name), relative)

    visit(os.fsencode(worktree), b"")
    return found


def _verify_materialized_worktree(
    worktree: Path,
    expected_tree: str,
    entries: Sequence[_TreeEntry],
) -> None:
    if _index_entries(worktree) != _tree_index_entries(worktree, expected_tree):
        raise PublicSnapshotError("worktree index tree does not match audited tree")
    git_marker = worktree / ".git"
    try:
        marker_metadata = git_marker.lstat()
    except OSError as error:
        raise PublicSnapshotError("worktree metadata is unavailable") from error
    if not stat.S_ISREG(marker_metadata.st_mode) or stat.S_ISLNK(marker_metadata.st_mode):
        raise PublicSnapshotError("worktree metadata is invalid")

    expected_paths: set[bytes] = set()
    expected_entries = {entry.path: entry for entry in entries}
    for entry in entries:
        components = _safe_tree_path(entry.path)
        for end in range(1, len(components)):
            expected_paths.add(b"/".join(components[:end]))
        expected_paths.add(entry.path)
    actual = _actual_worktree_entries(worktree)
    if set(actual) != expected_paths:
        raise PublicSnapshotError("worktree file inventory does not match audited tree")

    root = os.fsencode(worktree)
    for relative, entry in expected_entries.items():
        metadata = actual[relative]
        full_path = root + b"/" + relative
        if entry.mode == "120000":
            if not stat.S_ISLNK(metadata.st_mode) or os.readlink(full_path) != entry.payload:
                raise PublicSnapshotError("worktree symlink does not match audited tree")
            continue
        expected_mode = 0o755 if entry.mode == "100755" else 0o644
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or _read_regular_file(
                Path(os.fsdecode(full_path)),
                metadata,
                max_bytes=len(entry.payload),
                failure="worktree blob does not match audited tree",
            )
            != entry.payload
        ):
            raise PublicSnapshotError("worktree blob does not match audited tree")


def _public_snapshot_date(repository: Path, source: str) -> tuple[str, str]:
    size_text = _git_scalar(repository, "cat-file", "-s", source)
    if not size_text.isdecimal() or not 0 < int(size_text) <= MAX_SOURCE_COMMIT_BYTES:
        raise PublicSnapshotError("source commit metadata is invalid")
    raw = _run_git(repository, "cat-file", "commit", source)
    headers, separator, _message = raw.partition(b"\n\n")
    if not separator:
        raise PublicSnapshotError("source commit metadata is invalid")
    committer_lines = [line for line in headers.splitlines() if line.startswith(b"committer ")]
    if len(committer_lines) != 1:
        raise PublicSnapshotError("source commit metadata is invalid")
    match = re.search(
        rb" (?P<epoch>[0-9]{1,12}) (?P<sign>[+-])"
        rb"(?P<hours>[0-2][0-9])(?P<minutes>[0-5][0-9])\Z",
        committer_lines[0],
    )
    if match is None:
        raise PublicSnapshotError("source commit metadata is invalid")
    hours = int(match.group("hours"))
    if hours > 23:
        raise PublicSnapshotError("source commit metadata is invalid")
    try:
        source_date = datetime.fromtimestamp(int(match.group("epoch")), tz=timezone.utc)
        public_date = source_date.replace(hour=0, minute=0, second=0, microsecond=0)
    except (OverflowError, OSError, ValueError) as error:
        raise PublicSnapshotError("source commit metadata is invalid") from error
    public_epoch = int(public_date.timestamp())
    return public_date.isoformat(), f"{public_epoch} +0000"


def _create_root_commit(repository: Path, tree: str, source: str) -> str:
    public_date, public_epoch = _public_snapshot_date(repository, source)
    identity_environment = {
        "GIT_AUTHOR_NAME": FIXED_NAME,
        "GIT_AUTHOR_EMAIL": FIXED_EMAIL,
        "GIT_AUTHOR_DATE": public_date,
        "GIT_COMMITTER_NAME": FIXED_NAME,
        "GIT_COMMITTER_EMAIL": FIXED_EMAIL,
        "GIT_COMMITTER_DATE": public_date,
    }
    commit = (
        _run_git(
            repository,
            "commit-tree",
            tree,
            input_bytes=FIXED_MESSAGE.encode("ascii"),
            env_overrides=identity_environment,
        )
        .decode("ascii")
        .strip()
    )
    if OID_PATTERN.fullmatch(commit) is None:
        raise PublicSnapshotError("snapshot commit identity is invalid")
    expected = (
        f"tree {tree}\n"
        f"author {FIXED_NAME} <{FIXED_EMAIL}> {public_epoch}\n"
        f"committer {FIXED_NAME} <{FIXED_EMAIL}> {public_epoch}\n"
        f"\n{FIXED_MESSAGE}"
    ).encode("ascii")
    if _run_git(repository, "cat-file", "commit", commit) != expected:
        raise PublicSnapshotError("snapshot commit metadata is not deterministic")
    return commit


def _verify_snapshot_commit(
    repository: Path,
    *,
    ref: str,
    commit: str,
    tree: str,
) -> None:
    if _ref_oid(repository, ref) != commit:
        raise PublicSnapshotError("snapshot ref changed during preparation")
    raw = _run_git(repository, "cat-file", "commit", commit)
    if raw.count(b"\nparent ") or raw.startswith(b"parent "):
        raise PublicSnapshotError("snapshot commit unexpectedly has a parent")
    if raw.splitlines()[:1] != [f"tree {tree}".encode("ascii")]:
        raise PublicSnapshotError("snapshot commit tree does not match audited tree")


def _restore_isolated_directory(
    parent_fd: int,
    isolated_name: bytes,
    original_name: bytes,
) -> None:
    """Best-effort restore of a directory that proved not to be ours."""

    try:
        _rename_directory_noreplace(
            parent_fd,
            isolated_name,
            parent_fd,
            original_name,
            operation="snapshot rollback restore",
        )
    except PublicSnapshotError:
        # Never delete an entry whose identity is not ours. If a concurrent
        # process already occupied the original name, both entries are kept.
        pass


def _isolate_owned_entry(
    parent_fd: int,
    original_name: bytes,
    expected: tuple[int, int, int, int, int],
) -> bytes:
    """Move a non-directory entry to an unpredictable name before unlinking it."""

    isolated_name = f".pmh-remove-entry-{secrets.token_hex(16)}".encode("ascii")
    _rename_directory_noreplace(
        parent_fd,
        original_name,
        parent_fd,
        isolated_name,
        operation="snapshot rollback entry isolation",
    )
    try:
        isolated = os.stat(isolated_name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as error:
        _restore_isolated_directory(parent_fd, isolated_name, original_name)
        raise PublicSnapshotError("snapshot rollback isolated entry disappeared") from error
    if _entry_identity(isolated) != expected:
        _restore_isolated_directory(parent_fd, isolated_name, original_name)
        raise PublicSnapshotError("snapshot rollback refused a replaced entry")
    return isolated_name


def _isolate_owned_directory(
    parent_fd: int,
    original_name: bytes,
    directory_fd: int,
    expected: tuple[int, int, int, int],
) -> bytes:
    """Atomically move an entry aside, then prove the moved inode is ours."""

    isolated_name = f".pmh-remove-{secrets.token_hex(16)}".encode("ascii")
    _rename_directory_noreplace(
        parent_fd,
        original_name,
        parent_fd,
        isolated_name,
        operation="snapshot rollback isolation",
    )
    if not (
        _fd_directory_matches(directory_fd, expected)
        and _entry_directory_matches(parent_fd, isolated_name, expected)
    ):
        _restore_isolated_directory(parent_fd, isolated_name, original_name)
        raise PublicSnapshotError("snapshot rollback refused a replaced directory")
    return isolated_name


def _remove_directory_contents(directory_fd: int) -> None:
    """Remove one owned directory tree without resolving an absolute path."""

    try:
        names = tuple(os.listdir(directory_fd))
    except OSError as error:
        raise PublicSnapshotError("snapshot rollback could not inspect directory") from error
    for item in names:
        name = os.fsencode(item)
        try:
            before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except OSError as error:
            raise PublicSnapshotError("snapshot rollback directory changed") from error
        if stat.S_ISDIR(before.st_mode):
            child_fd = -1
            isolated_name: bytes | None = None
            try:
                child_fd = _open_directory(directory_fd, name)
                child_identity = _directory_identity(before)
                if not _fd_directory_matches(child_fd, child_identity):
                    raise PublicSnapshotError("snapshot rollback directory changed")
                isolated_name = _isolate_owned_directory(
                    directory_fd,
                    name,
                    child_fd,
                    child_identity,
                )
                _remove_directory_contents(child_fd)
                if not (
                    _fd_directory_matches(child_fd, child_identity)
                    and _entry_directory_matches(
                        directory_fd,
                        isolated_name,
                        child_identity,
                    )
                ):
                    raise PublicSnapshotError("snapshot rollback directory changed")
                os.rmdir(isolated_name, dir_fd=directory_fd)
            except PublicSnapshotError:
                raise
            except OSError as error:
                raise PublicSnapshotError("snapshot rollback directory changed") from error
            finally:
                if child_fd >= 0:
                    os.close(child_fd)
            continue
        try:
            entry_identity = _entry_identity(before)
            if before.st_uid != os.geteuid() or before.st_nlink != 1:
                raise PublicSnapshotError("snapshot rollback refused an unowned entry")
            isolated_name = _isolate_owned_entry(directory_fd, name, entry_identity)
            after = os.stat(isolated_name, dir_fd=directory_fd, follow_symlinks=False)
            if _entry_identity(after) != entry_identity:
                raise PublicSnapshotError("snapshot rollback isolated entry changed")
            os.unlink(isolated_name, dir_fd=directory_fd)
        except PublicSnapshotError:
            raise
        except OSError as error:
            raise PublicSnapshotError("snapshot rollback directory changed") from error


def _isolate_and_remove_owned_directory(
    parent_fd: int,
    original_name: bytes,
    directory_fd: int,
    expected: tuple[int, int, int, int],
) -> None:
    isolated_name = _isolate_owned_directory(
        parent_fd,
        original_name,
        directory_fd,
        expected,
    )
    _remove_directory_contents(directory_fd)
    if not (
        _fd_directory_matches(directory_fd, expected)
        and _entry_directory_matches(parent_fd, isolated_name, expected)
    ):
        raise PublicSnapshotError("snapshot rollback isolated directory changed")
    try:
        os.rmdir(isolated_name, dir_fd=parent_fd)
    except OSError as error:
        raise PublicSnapshotError("snapshot rollback directory removal failed") from error


def _close_metadata(metadata: _WorktreeMetadata | None) -> None:
    if metadata is None:
        return
    try:
        os.close(metadata.directory_fd)
    finally:
        os.close(metadata.parent_fd)


def _close_reservation(reservation: _WorktreeReservation | None) -> None:
    if reservation is None:
        return
    for descriptor in (
        reservation.staging_fd,
        reservation.anchor_fd,
        reservation.parent_fd,
    ):
        try:
            os.close(descriptor)
        except OSError:
            pass


def _cleanup_reserved_worktree(
    repository: Path,
    reservation: _WorktreeReservation,
    metadata: _WorktreeMetadata | None,
) -> None:
    del repository
    final_name = os.fsencode(reservation.final_path.name)
    final_matches = _entry_directory_matches(
        reservation.parent_fd,
        final_name,
        reservation.staging_identity,
    )
    staging_matches = _entry_directory_matches(
        reservation.anchor_fd,
        b"worktree",
        reservation.staging_identity,
    )
    if final_matches == staging_matches:
        raise PublicSnapshotError("snapshot rollback worktree location is ambiguous")
    if final_matches:
        parent_fd = reservation.parent_fd
        name = final_name
    else:
        parent_fd = reservation.anchor_fd
        name = b"worktree"

    if not _fd_directory_matches(
        parent_fd,
        (reservation.parent_identity if final_matches else reservation.anchor_identity),
    ):
        raise PublicSnapshotError("snapshot rollback parent directory changed")
    _isolate_and_remove_owned_directory(
        parent_fd,
        name,
        reservation.staging_fd,
        reservation.staging_identity,
    )

    if metadata is not None:
        _isolate_and_remove_owned_directory(
            metadata.parent_fd,
            os.fsencode(metadata.path.name),
            metadata.directory_fd,
            metadata.directory_identity,
        )

    anchor_name = os.fsencode(reservation.anchor_path.name)
    if _entry_directory_matches(
        reservation.parent_fd,
        anchor_name,
        reservation.anchor_identity,
    ):
        if not (
            _fd_directory_matches(reservation.parent_fd, reservation.parent_identity)
            and _fd_directory_matches(reservation.anchor_fd, reservation.anchor_identity)
        ):
            raise PublicSnapshotError("snapshot rollback private anchor changed")
        _isolate_and_remove_owned_directory(
            reservation.parent_fd,
            anchor_name,
            reservation.anchor_fd,
            reservation.anchor_identity,
        )
    elif staging_matches and _fd_directory_matches(
        reservation.parent_fd, reservation.parent_identity
    ):
        raise PublicSnapshotError("snapshot rollback private anchor was replaced")


def _rollback(
    repository: Path,
    *,
    ref: str,
    commit: str | None,
    reservation: _WorktreeReservation | None,
    metadata: _WorktreeMetadata | None,
) -> None:
    if reservation is not None and metadata is None:
        try:
            metadata = _bind_worktree_metadata(repository, reservation)
        except BaseException:
            # Only a fully rebound, inode-verified metadata directory is safe to remove.
            pass
    try:
        if reservation is not None:
            _cleanup_reserved_worktree(repository, reservation, metadata)
    except BaseException:
        # Cleanup is best effort. The ref CAS below is an independent security boundary.
        pass
    try:
        if commit is not None and _ref_oid(repository, ref) == commit:
            _run_git(repository, "update-ref", "-d", ref, commit)
    except BaseException:
        # A concurrent ref winner must never be deleted by rollback.
        pass
    try:
        _close_metadata(metadata)
    except BaseException:
        pass
    try:
        _close_reservation(reservation)
    except BaseException:
        pass


def prepare_public_snapshot(
    *,
    repository: Path,
    source: str,
    receipt_path: Path,
    branch: str,
    worktree: Path,
    forbidden_file: Path,
    allowlist_file: Path,
) -> SnapshotResult:
    """Create an isolated deterministic snapshot after independently repeating the audit."""

    if OID_PATTERN.fullmatch(source) is None:
        raise PublicSnapshotError("source must be a full 40-character commit OID")
    if branch != APPROVED_BRANCH:
        raise PublicSnapshotError("branch is not the approved public snapshot branch")
    root = _repository_root(repository)
    candidate = _validate_worktree_path(root, worktree)
    ref = f"refs/heads/{branch}"
    if _ref_oid(root, ref) is not None:
        raise PublicSnapshotError("snapshot ref already exists")

    head = _git_scalar(root, "rev-parse", "--verify", "HEAD^{commit}")
    if head != source:
        raise PublicSnapshotError("source must equal the current HEAD commit")
    tree = _git_scalar(root, "rev-parse", "--verify", f"{source}^{{tree}}")
    _validate_source_clean(root, source, tree)
    receipt = _validate_receipt(_load_receipt(receipt_path), source=source, tree=tree)

    try:
        repeated = audit_public_tree(
            repository=root,
            ref=source,
            mode="tree",
            forbidden_file=forbidden_file,
            allowlist_file=allowlist_file,
            receipt_path=None,
        )
    except Exception as error:
        raise PublicSnapshotError("fresh audit failed") from error
    repeated_receipt = _validate_receipt(repeated, source=source, tree=tree)
    if repeated_receipt != receipt:
        raise PublicSnapshotError("receipt does not match fresh audit")

    _validate_source_clean(root, source, tree)
    entries = _read_tree_entries(root, source)
    commit = _create_root_commit(root, tree, source)
    _validate_source_clean(root, source, tree)

    reservation: _WorktreeReservation | None = None
    metadata: _WorktreeMetadata | None = None
    created_commit: str | None = None
    try:
        reservation = _reserve_worktree(candidate)
        _run_git(root, "update-ref", ref, commit, ZERO_OID)
        created_commit = commit
        _validate_source_clean(root, source, tree)
        _assert_reservation_intact(reservation)
        _run_git(
            root,
            "worktree",
            "add",
            "--no-checkout",
            str(reservation.staging_path),
            branch,
        )
        metadata = _bind_worktree_metadata(root, reservation)
        _assert_reservation_intact(reservation)
        _run_git(reservation.staging_path, "read-tree", source)
        _assert_reservation_intact(reservation)
        _materialize_tree(root, reservation.staging_path, entries)
        _assert_reservation_intact(reservation)
        _verify_materialized_worktree(reservation.staging_path, tree, entries)
        _verify_snapshot_commit(root, ref=ref, commit=commit, tree=tree)
        _validate_source_clean(root, source, tree)
        _publish_worktree(reservation, metadata)
        _verify_snapshot_commit(root, ref=ref, commit=commit, tree=tree)
    except BaseException:
        _rollback(
            root,
            ref=ref,
            commit=created_commit,
            reservation=reservation,
            metadata=metadata,
        )
        raise

    _close_metadata(metadata)
    _close_reservation(reservation)

    return SnapshotResult(branch=branch, commit=commit, tree=tree, worktree=candidate)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--branch", required=True)
    parser.add_argument("--worktree", required=True, type=Path)
    parser.add_argument("--forbidden-file", required=True, type=Path)
    parser.add_argument(
        "--allowlist",
        type=Path,
        default=Path("config/public-release-allowlist.toml"),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        repository = _repository_root(Path.cwd())
        allowlist = arguments.allowlist
        if not allowlist.is_absolute():
            allowlist = repository / allowlist
        result = prepare_public_snapshot(
            repository=repository,
            source=arguments.source,
            receipt_path=arguments.receipt,
            branch=arguments.branch,
            worktree=arguments.worktree,
            forbidden_file=arguments.forbidden_file,
            allowlist_file=allowlist,
        )
    except PublicSnapshotError:
        print("error: public_snapshot_failed", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "branch": result.branch,
                "commit": result.commit,
                "tree": result.tree,
                "worktree": str(result.worktree),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
