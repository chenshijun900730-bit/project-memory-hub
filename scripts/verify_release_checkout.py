from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_COMMIT_OID = re.compile(r"[0-9a-f]{40}")
_GIT_TIMEOUT_SECONDS = 30.0


class ReleaseCheckoutError(RuntimeError):
    """Raised when a release build is no longer bound to its reviewed commit."""


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in tuple(environment):
        if key in {
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_COMMON_DIR",
            "GIT_CONFIG_COUNT",
            "GIT_DIR",
            "GIT_INDEX_FILE",
            "GIT_OBJECT_DIRECTORY",
            "GIT_WORK_TREE",
        } or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            environment.pop(key, None)
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _git_executable() -> Path:
    candidate = shutil.which("git")
    if candidate is None:
        raise ReleaseCheckoutError("release_checkout_git_unavailable")
    try:
        executable = Path(candidate).resolve(strict=True)
    except OSError as error:
        raise ReleaseCheckoutError("release_checkout_git_unavailable") from error
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ReleaseCheckoutError("release_checkout_git_unavailable")
    return executable


def _run_git(
    executable: Path,
    repository: Path,
    *arguments: str,
    allowed_returncodes: frozenset[int] = frozenset({0}),
) -> subprocess.CompletedProcess[bytes]:
    command = [
        str(executable),
        "--no-replace-objects",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        *arguments,
    ]
    try:
        result = subprocess.run(
            command,
            cwd=repository,
            env=_git_environment(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseCheckoutError("release_checkout_git_failed") from error
    if result.returncode not in allowed_returncodes:
        raise ReleaseCheckoutError("release_checkout_git_failed")
    return result


def _head(executable: Path, repository: Path) -> str:
    result = _run_git(executable, repository, "rev-parse", "--verify", "HEAD^{commit}")
    try:
        head = result.stdout.decode("ascii", errors="strict").strip()
    except UnicodeError as error:
        raise ReleaseCheckoutError("release_checkout_git_failed") from error
    if _COMMIT_OID.fullmatch(head) is None:
        raise ReleaseCheckoutError("release_checkout_git_failed")
    return head


def _require_repository_root(executable: Path, repository: Path) -> None:
    result = _run_git(executable, repository, "rev-parse", "--show-toplevel")
    try:
        top_level = Path(os.fsdecode(result.stdout.rstrip(b"\n"))).resolve(strict=True)
    except (OSError, UnicodeError) as error:
        raise ReleaseCheckoutError("release_checkout_git_failed") from error
    try:
        same_directory = os.path.samefile(repository, top_level)
    except OSError as error:
        raise ReleaseCheckoutError("release_checkout_git_failed") from error
    if not same_directory:
        raise ReleaseCheckoutError("release_checkout_git_failed")


def _is_dirty(executable: Path, repository: Path) -> bool:
    index_entries = _run_git(
        executable,
        repository,
        "ls-files",
        "-v",
        "-z",
        "--",
    )
    records = [record for record in index_entries.stdout.split(b"\0") if record]
    if any(not record.startswith(b"H ") for record in records):
        return True
    tracked = _run_git(
        executable,
        repository,
        "diff-index",
        "--quiet",
        "HEAD",
        "--",
        allowed_returncodes=frozenset({0, 1}),
    )
    if tracked.returncode == 1:
        return True
    untracked = _run_git(
        executable,
        repository,
        "ls-files",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
    )
    return bool(untracked.stdout)


def verify_release_checkout(repository: Path, *, expected_head: str) -> str:
    if _COMMIT_OID.fullmatch(expected_head) is None:
        raise ReleaseCheckoutError("release_checkout_expected_head_invalid")
    selected = Path(repository)
    try:
        metadata = selected.lstat()
        resolved = selected.resolve(strict=True)
    except OSError as error:
        raise ReleaseCheckoutError("release_checkout_git_failed") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseCheckoutError("release_checkout_git_failed")

    executable = _git_executable()
    _require_repository_root(executable, resolved)
    before = _head(executable, resolved)
    if before != expected_head:
        raise ReleaseCheckoutError("release_checkout_head_mismatch")
    if _is_dirty(executable, resolved) or _is_dirty(executable, resolved):
        raise ReleaseCheckoutError("release_checkout_dirty")
    after = _head(executable, resolved)
    if after != expected_head:
        raise ReleaseCheckoutError("release_checkout_head_mismatch")
    return after


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify that a release checkout still matches its reviewed commit"
    )
    parser.add_argument("--expected-head", required=True)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    try:
        verify_release_checkout(PROJECT_ROOT, expected_head=arguments.expected_head)
    except ReleaseCheckoutError as error:
        raise SystemExit(str(error)) from None


if __name__ == "__main__":
    main()
