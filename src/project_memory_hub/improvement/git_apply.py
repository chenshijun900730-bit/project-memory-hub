from __future__ import annotations

import os
import re
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pydantic import ValidationError

from project_memory_hub.improvement.models import ApplyResult, ProposalRecord


_DEFAULT_MAX_PATCH_BYTES = 256 * 1024
_DEFAULT_MAX_FILES = 32
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_INDEX = re.compile(r"index [0-9a-f]+[.][.][0-9a-f]+(?: 100644)?\Z")
_SANDBOX_EXEC = Path("/usr/bin/sandbox-exec")
_GIT_EXEC = Path("/usr/bin/git")
_TRUE_EXEC = Path("/usr/bin/true")
_SANDBOX_SYSTEM_READ_ROOTS = (
    Path("/System"),
    Path("/usr/bin"),
    Path("/usr/lib"),
    Path("/usr/libexec"),
    Path("/usr/sbin"),
    Path("/bin"),
    Path("/sbin"),
    Path("/Library/Apple"),
    Path("/private/var/db/dyld"),
)
_MAX_HOOK_BYTES = 1024 * 1024
_REPLAYED_HOOKS = frozenset({"post-checkout", "post-commit"})
_KNOWN_GIT_HOOKS = frozenset(
    {
        "applypatch-msg",
        "commit-msg",
        "fsmonitor-watchman",
        "p4-changelist",
        "p4-post-changelist",
        "p4-pre-submit",
        "p4-prepare-changelist",
        "post-applypatch",
        "post-checkout",
        "post-commit",
        "post-index-change",
        "post-merge",
        "post-receive",
        "post-rewrite",
        "post-update",
        "pre-applypatch",
        "pre-auto-gc",
        "pre-commit",
        "pre-merge-commit",
        "pre-push",
        "pre-rebase",
        "pre-receive",
        "prepare-commit-msg",
        "proc-receive",
        "push-to-checkout",
        "reference-transaction",
        "sendemail-validate",
        "update",
    }
)
_SAFE_GIT_CONFIG = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "gc.auto=0",
    "-c",
    "maintenance.auto=false",
)


class GitProposalError(RuntimeError):
    """A stable, non-disclosing Git proposal failure."""


class GitProposalRecoveryRequired(GitProposalError):
    """An exact proposal commit exists but cleanup must be reconciled."""


class UnsafeGitPatch(GitProposalError):
    """The proposed patch is outside the deliberately small safe subset."""


@dataclass(frozen=True, slots=True)
class ValidatedPatch:
    patch_bytes: bytes
    paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _PatchEntry:
    path: str
    added: bool


@dataclass(frozen=True, slots=True)
class GitApplyPlan:
    repository_root: Path
    original_branch: str
    base_commit: str
    proposal_branch: str


@dataclass(frozen=True, slots=True)
class _VerificationBoundary:
    status: str
    refs: tuple[tuple[str, str], ...]
    worktrees: tuple[tuple[Path, str | None], ...]


@dataclass(frozen=True, slots=True)
class _ExecutableIdentity:
    path: Path
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int
    mode: int
    uid: int


@dataclass(frozen=True, slots=True)
class _HookSnapshot:
    name: str
    document: bytes


class PatchValidator:
    def __init__(
        self,
        *,
        max_patch_bytes: int = _DEFAULT_MAX_PATCH_BYTES,
        max_files: int = _DEFAULT_MAX_FILES,
    ) -> None:
        if type(max_patch_bytes) is not int or not 1 <= max_patch_bytes <= 2**31 - 1:
            raise ValueError("patch byte limit is invalid")
        if type(max_files) is not int or not 1 <= max_files <= 10_000:
            raise ValueError("patch file limit is invalid")
        self._max_patch_bytes = max_patch_bytes
        self._max_files = max_files

    def validate(self, patch: str, repository_root: Path, base_commit: str) -> ValidatedPatch:
        document, root, base, entries = self._validated_document(
            patch,
            repository_root,
            base_commit,
        )
        for entry in entries:
            _validate_filesystem_target(root, base, entry)
        return ValidatedPatch(
            patch_bytes=document,
            paths=tuple(entry.path for entry in entries),
        )

    def validate_recorded(
        self, patch: str, repository_root: Path, base_commit: str
    ) -> ValidatedPatch:
        document, root, base, entries = self._validated_document(
            patch,
            repository_root,
            base_commit,
        )
        for entry in entries:
            _validate_tree_target(root, base, entry)
        return ValidatedPatch(
            patch_bytes=document,
            paths=tuple(entry.path for entry in entries),
        )

    def _validated_document(
        self, patch: str, repository_root: Path, base_commit: str
    ) -> tuple[bytes, Path, str, tuple[_PatchEntry, ...]]:
        if type(patch) is not str or not patch:
            raise UnsafeGitPatch("patch rejected")
        try:
            document = patch.encode("utf-8")
        except UnicodeEncodeError:
            raise UnsafeGitPatch("patch rejected") from None
        if len(document) > self._max_patch_bytes or b"\x00" in document:
            raise UnsafeGitPatch("patch rejected")
        if b"GIT binary patch" in document or b"Binary files " in document:
            raise UnsafeGitPatch("patch rejected")

        root = _verified_repository_root(repository_root)
        base = _verified_commit(root, base_commit)
        entries = _parse_patch(patch)
        if not entries or len(entries) > self._max_files:
            raise UnsafeGitPatch("patch rejected")
        paths = tuple(entry.path for entry in entries)
        if len(set(paths)) != len(paths):
            raise UnsafeGitPatch("patch rejected")
        aliases = tuple(unicodedata.normalize("NFC", path).casefold() for path in paths)
        if len(set(aliases)) != len(aliases):
            raise UnsafeGitPatch("patch rejected")
        return document, root, base, entries


def _parse_patch(document: str) -> tuple[_PatchEntry, ...]:
    lines = document.splitlines()
    starts = [index for index, line in enumerate(lines) if line.startswith("diff --")]
    if not starts or starts[0] != 0:
        raise UnsafeGitPatch("patch rejected")
    starts.append(len(lines))
    entries: list[_PatchEntry] = []
    for position in range(len(starts) - 1):
        section = lines[starts[position] : starts[position + 1]]
        entries.append(_parse_section(section))
    return tuple(entries)


def _parse_section(lines: list[str]) -> _PatchEntry:
    if not lines:
        raise UnsafeGitPatch("patch rejected")
    words = lines[0].split(" ")
    if len(words) != 4 or words[:2] != ["diff", "--git"]:
        raise UnsafeGitPatch("patch rejected")
    left = _prefixed_path(words[2], "a/")
    right = _prefixed_path(words[3], "b/")
    if left != right:
        raise UnsafeGitPatch("patch rejected")

    cursor = 1
    if cursor < len(lines) and _INDEX.fullmatch(lines[cursor]):
        cursor += 1
    added = False
    if cursor < len(lines) and lines[cursor].startswith("new file mode "):
        if lines[cursor] != "new file mode 100644":
            raise UnsafeGitPatch("patch rejected")
        added = True
        cursor += 1

    expected_old = "/dev/null" if added else f"a/{left}"
    if cursor + 2 >= len(lines):
        raise UnsafeGitPatch("patch rejected")
    if lines[cursor] != f"--- {expected_old}" or lines[cursor + 1] != f"+++ b/{left}":
        raise UnsafeGitPatch("patch rejected")
    cursor += 2
    if not lines[cursor].startswith("@@ "):
        raise UnsafeGitPatch("patch rejected")

    forbidden = (
        "old mode ",
        "new mode ",
        "deleted file mode ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
        "similarity index ",
        "dissimilarity index ",
    )
    for line in lines[1:cursor]:
        if line.startswith(forbidden):
            raise UnsafeGitPatch("patch rejected")
    for line in lines[cursor:]:
        if line.startswith("diff --"):
            raise UnsafeGitPatch("patch rejected")
        if line and line[0] not in {" ", "+", "-", "@", "\\"}:
            raise UnsafeGitPatch("patch rejected")
    return _PatchEntry(path=left, added=added)


def _prefixed_path(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        raise UnsafeGitPatch("patch rejected")
    path = value[len(prefix) :]
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise UnsafeGitPatch("patch rejected")
    parsed = PurePosixPath(path)
    if str(parsed) != path or any(part in {"", ".", ".."} for part in parsed.parts):
        raise UnsafeGitPatch("patch rejected")
    lowered = tuple(part.casefold() for part in parsed.parts)
    reserved = {
        ".git",
        ".gitmodules",
        ".proposal-home",
        ".proposal-tmp",
        ".proposal-graphify-out",
        ".proposal-hooks",
    }
    if (
        any(part in reserved for part in lowered)
        or lowered[0] == ".proposal.patch"
        or lowered[0].startswith(".proposal-")
    ):
        raise UnsafeGitPatch("patch rejected")
    return path


def _verified_repository_root(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise UnsafeGitPatch("repository rejected")
    try:
        root = value.resolve(strict=True)
    except (OSError, RuntimeError):
        raise UnsafeGitPatch("repository rejected") from None
    if value.absolute() != root:
        raise UnsafeGitPatch("repository rejected")
    result = _run_git(root, "rev-parse", "--show-toplevel")
    try:
        top = Path(result).resolve(strict=True)
    except (OSError, RuntimeError):
        raise UnsafeGitPatch("repository rejected") from None
    if top != root:
        raise UnsafeGitPatch("repository rejected")
    return root


def _verified_commit(root: Path, value: str) -> str:
    if type(value) is not str or _COMMIT.fullmatch(value) is None:
        raise UnsafeGitPatch("base commit rejected")
    resolved = _run_git(root, "rev-parse", "--verify", f"{value}^{{commit}}")
    if resolved != value:
        raise UnsafeGitPatch("base commit rejected")
    return resolved


def _validate_filesystem_target(root: Path, base: str, entry: _PatchEntry) -> None:
    relative = PurePosixPath(entry.path)
    current = root
    ancestor_parts: list[str] = []
    missing_tree_ancestor = False
    missing_filesystem_ancestor = False
    for part in relative.parts[:-1]:
        ancestor_parts.append(part)
        tree_path = "/".join(ancestor_parts)
        tree_entry = _tree_entry(root, base, tree_path)
        if tree_entry is None:
            missing_tree_ancestor = True
        elif missing_tree_ancestor or tree_entry != ("040000", "tree"):
            raise UnsafeGitPatch("patch target rejected")
        current = current / part
        if missing_filesystem_ancestor:
            continue
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            missing_filesystem_ancestor = True
            continue
        except OSError:
            raise UnsafeGitPatch("patch target rejected") from None
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise UnsafeGitPatch("patch target rejected")

    target = root.joinpath(*relative.parts)
    try:
        target_metadata: os.stat_result | None = target.lstat()
    except FileNotFoundError:
        target_metadata = None
    except OSError:
        raise UnsafeGitPatch("patch target rejected") from None
    if entry.added:
        if target_metadata is not None:
            raise UnsafeGitPatch("patch target rejected")
        if _tree_entry(root, base, entry.path) is not None:
            raise UnsafeGitPatch("patch target rejected")
        return
    if (
        target_metadata is None
        or not stat.S_ISREG(target_metadata.st_mode)
        or target_metadata.st_nlink != 1
    ):
        raise UnsafeGitPatch("patch target rejected")
    tree = _tree_entry(root, base, entry.path)
    if tree is None or tree[0] != "100644" or tree[1] != "blob":
        raise UnsafeGitPatch("patch target rejected")


def _tree_entry(root: Path, base: str, path: str) -> tuple[str, str] | None:
    output = _run_git(root, "ls-tree", base, "--", path)
    if not output:
        return None
    head, separator, selected = output.partition("\t")
    pieces = head.split()
    if separator != "\t" or selected != path or len(pieces) != 3:
        raise UnsafeGitPatch("patch target rejected")
    return pieces[0], pieces[1]


def _validate_tree_target(root: Path, base: str, entry: _PatchEntry) -> None:
    relative = PurePosixPath(entry.path)
    missing_ancestor = False
    parts: list[str] = []
    for part in relative.parts[:-1]:
        parts.append(part)
        selected = _tree_entry(root, base, "/".join(parts))
        if selected is None:
            missing_ancestor = True
        elif missing_ancestor or selected != ("040000", "tree"):
            raise UnsafeGitPatch("patch target rejected")
    target = _tree_entry(root, base, entry.path)
    if entry.added:
        if target is not None:
            raise UnsafeGitPatch("patch target rejected")
        return
    if missing_ancestor or target != ("100644", "blob"):
        raise UnsafeGitPatch("patch target rejected")


def _run_git(root: Path, *arguments: str) -> str:
    try:
        _trusted_system_executable(_GIT_EXEC)
        returncode, output = _run_bounded(
            (str(_GIT_EXEC), "-C", str(root), *_SAFE_GIT_CONFIG, *arguments),
            cwd=root,
            timeout=8,
            max_output_bytes=16_384,
        )
    except (GitProposalError, ValueError):
        raise UnsafeGitPatch("git validation failed") from None
    if returncode != 0:
        raise UnsafeGitPatch("git validation failed")
    return output.strip()


def _minimal_environment() -> dict[str, str]:
    return {
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Project Memory Hub",
        "GIT_AUTHOR_EMAIL": "memory-hub@example.invalid",
        "GIT_COMMITTER_NAME": "Project Memory Hub",
        "GIT_COMMITTER_EMAIL": "memory-hub@example.invalid",
    }


class GitProposalApplier:
    def __init__(
        self,
        repository_root: Path,
        runtime_directory: Path,
        *,
        allowed_verification_argv: tuple[tuple[str, ...], ...],
        timeout_seconds: float = 120,
        max_output_bytes: int = 64 * 1024,
        patch_validator: PatchValidator | None = None,
        repair_runtime_permissions: bool = True,
    ) -> None:
        try:
            self._repository_root = _verified_repository_root(repository_root)
        except UnsafeGitPatch:
            raise ValueError("repository path is invalid") from None
        if type(repair_runtime_permissions) is not bool:
            raise ValueError("runtime permission policy is invalid")
        self._runtime = _private_runtime(
            runtime_directory,
            repair_permissions=repair_runtime_permissions,
        )
        self._allowed_commands = _allowed_commands(allowed_verification_argv)
        self._sandbox_identity = _trusted_system_executable(_SANDBOX_EXEC)
        self._git_identity = _trusted_system_executable(_GIT_EXEC)
        if type(timeout_seconds) not in {int, float} or isinstance(timeout_seconds, bool):
            raise ValueError("verification timeout is invalid")
        if not 0 < timeout_seconds <= 3_600:
            raise ValueError("verification timeout is invalid")
        if type(max_output_bytes) is not int or not 1 <= max_output_bytes <= 2**24:
            raise ValueError("verification output limit is invalid")
        self._timeout = float(timeout_seconds)
        self._max_output = max_output_bytes
        self._validator = patch_validator or PatchValidator()

    def preflight(self, record: ProposalRecord) -> GitApplyPlan:
        proposal = _executable_record(record, {"approved"})
        root, branch, base = self._current_repository()
        proposal_branch = f"codex/memory-hub-proposal-{proposal.proposal_id.hex}"
        if _ref_value(root, proposal_branch) is not None:
            raise GitProposalError("proposal branch unavailable")
        self._require_allowed(proposal.verification_argv)
        validated = self._validator.validate(proposal.patch or "", root, base)
        self._check_patch_applicability(validated.patch_bytes, root)
        self._probe_sandbox_boundary()
        _capture_repository_hooks(root)
        return GitApplyPlan(root, branch, base, proposal_branch)

    def apply(self, record: ProposalRecord) -> ApplyResult:
        proposal = _executable_record(record, {"applying"})
        _matching_plan(proposal, self._current_repository())
        self._require_allowed(proposal.verification_argv)
        validated = self._validator.validate(
            proposal.patch or "", self._repository_root, proposal.base_commit or ""
        )
        if _ref_value(self._repository_root, proposal.proposal_branch or "") is not None:
            raise GitProposalError("proposal branch unavailable")
        return self._execute(proposal, validated, create_branch=True)

    def _execute(
        self,
        proposal: ProposalRecord,
        validated: ValidatedPatch,
        *,
        create_branch: bool,
    ) -> ApplyResult:
        branch = proposal.proposal_branch or ""
        base = proposal.base_commit or ""
        branch_commit = _ref_value(self._repository_root, branch)
        if create_branch and branch_commit is not None:
            raise GitProposalError("proposal branch unavailable")
        if not create_branch and branch_commit != base:
            raise GitProposalError("proposal branch rejected")

        hook_snapshots = _capture_repository_hooks(self._repository_root)

        worktree = Path(tempfile.mkdtemp(prefix="proposal-worktree-", dir=self._runtime))
        os.chmod(worktree, 0o700)
        registered = False
        exact_commit_ready = False
        try:
            if create_branch:
                _git(
                    self._repository_root,
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(worktree),
                    base,
                )
            else:
                _git(
                    self._repository_root,
                    "worktree",
                    "add",
                    str(worktree),
                    branch,
                )
            registered = True
            if stat.S_IMODE(worktree.stat().st_mode) != 0o700:
                raise GitProposalError("private worktree rejected")
            hooks = _materialize_repository_hooks(worktree)
            patch_path = worktree / ".proposal.patch"
            _write_private(patch_path, validated.patch_bytes)
            try:
                _git(worktree, "apply", "--check", "--", str(patch_path))
                _git(worktree, "apply", "--index", "--", str(patch_path))
            finally:
                patch_path.unlink(missing_ok=True)
            expected_tree = _git(worktree, "write-tree")
            boundary = _verification_boundary(worktree, self._repository_root)
            self._verify(proposal.verification_argv, worktree)
            self._require_expected_staging(
                worktree,
                base,
                branch,
                expected_tree,
                boundary,
            )
            try:
                self._commit(
                    worktree,
                    hooks,
                    (
                        "commit",
                        "--no-gpg-sign",
                        "-m",
                        f"chore(improvement): apply proposal {proposal.proposal_id}",
                        "-m",
                        f"Apply-Attempt: {proposal.apply_attempt_id}",
                    ),
                )
            except GitProposalError:
                candidate = _ref_value(self._repository_root, branch)
                if candidate is None or candidate == base:
                    raise
                self._verify_commit(proposal, candidate, expected_tree)
                _require_post_commit_boundary(
                    self._repository_root,
                    worktree,
                    boundary,
                    branch,
                    candidate,
                    proposal.original_branch or "",
                    base,
                )
                exact_commit_ready = True
                _require_hooks_unchanged(self._repository_root, hook_snapshots)
                raise GitProposalRecoveryRequired(
                    "proposal commit requires state recovery"
                ) from None
            commit = _git(worktree, "rev-parse", "HEAD")
            self._verify_commit(proposal, commit, expected_tree)
            _require_post_commit_boundary(
                self._repository_root,
                worktree,
                boundary,
                branch,
                commit,
                proposal.original_branch or "",
                base,
            )
            exact_commit_ready = True
            _require_hooks_unchanged(self._repository_root, hook_snapshots)
            return ApplyResult(
                proposal_id=proposal.proposal_id,
                repository_root=self._repository_root,
                original_branch=proposal.original_branch or "",
                base_commit=proposal.base_commit or "",
                proposal_branch=proposal.proposal_branch or "",
                applied_commit=commit,
                verification_summary=_verification_summary(hook_snapshots),
            )
        except GitProposalError:
            raise
        except (OSError, RuntimeError):
            raise GitProposalError("proposal apply failed") from None
        finally:
            try:
                _cleanup_created_worktree(
                    self._repository_root,
                    worktree,
                    self._runtime,
                    known_registered=registered,
                )
            except GitProposalError:
                if exact_commit_ready:
                    raise GitProposalRecoveryRequired(
                        "proposal commit requires cleanup recovery"
                    ) from None
                raise

    def recover(self, record: ProposalRecord) -> ApplyResult:
        proposal = _executable_record(record, {"applying"})
        _matching_recovery_plan(
            proposal,
            _verified_repository_root(self._repository_root),
        )
        validated = self._validator.validate_recorded(
            proposal.patch or "", self._repository_root, proposal.base_commit or ""
        )
        commit = _ref_value(self._repository_root, proposal.proposal_branch or "")
        if commit is None or commit == proposal.base_commit:
            raise GitProposalError("apply recovery rejected")
        expected_tree = self._expected_tree(proposal, validated)
        self._verify_commit(proposal, commit, expected_tree)
        self._cleanup_recovery_worktrees(proposal)
        return ApplyResult(
            proposal_id=proposal.proposal_id,
            repository_root=self._repository_root,
            original_branch=proposal.original_branch or "",
            base_commit=proposal.base_commit or "",
            proposal_branch=proposal.proposal_branch or "",
            applied_commit=commit,
            verification_summary="verification passed; hooks deferred: recovery-unknown",
        )

    def _cleanup_recovery_worktrees(self, proposal: ProposalRecord) -> None:
        branch = proposal.proposal_branch or ""
        for path in _branch_worktrees(self._repository_root, branch):
            if not _is_private_service_path(path, self._runtime):
                raise GitProposalError("apply recovery rejected")
            try:
                _git_cleanup(self._repository_root, path)
                _remove_private_directory(path, self._runtime)
            except GitProposalError:
                raise GitProposalRecoveryRequired("proposal cleanup requires recovery") from None

    def verify_rollback(self, record: ProposalRecord) -> None:
        proposal = _executable_record(record, {"applied"})
        _matching_plan(proposal, self._current_repository())
        commit = _ref_value(self._repository_root, proposal.proposal_branch or "")
        if commit != proposal.applied_commit:
            raise GitProposalError("rollback verification rejected")
        validated = self._validator.validate(
            proposal.patch or "", self._repository_root, proposal.base_commit or ""
        )
        expected_tree = self._expected_tree(proposal, validated)
        self._verify_commit(proposal, commit or "", expected_tree)

    def preview_rollback(self, record: ProposalRecord) -> None:
        """Check rollback state and exact refs without creating a worktree."""
        proposal = _executable_record(record, {"applied"})
        _matching_plan(proposal, self._current_repository())
        commit = _ref_value(self._repository_root, proposal.proposal_branch or "")
        if commit != proposal.applied_commit:
            raise GitProposalError("rollback verification rejected")
        self._verify_commit_metadata(proposal, commit or "")
        self._validator.validate(
            proposal.patch or "",
            self._repository_root,
            proposal.base_commit or "",
        )

    def preview_recover(self, record: ProposalRecord) -> None:
        """Check recovery refs and commit metadata without cleanup writes."""
        proposal = _executable_record(record, {"applying"})
        _matching_recovery_plan(
            proposal,
            _verified_repository_root(self._repository_root),
        )
        self._validator.validate_recorded(
            proposal.patch or "",
            self._repository_root,
            proposal.base_commit or "",
        )
        commit = _ref_value(self._repository_root, proposal.proposal_branch or "")
        if commit is None or commit == proposal.base_commit:
            raise GitProposalError("apply recovery rejected")
        self._verify_commit_metadata(proposal, commit)

    def _current_repository(self) -> tuple[Path, str, str]:
        root = _verified_repository_root(self._repository_root)
        status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
        if status:
            raise GitProposalError("repository is not clean")
        branch = _git(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if not branch or branch.startswith("-"):
            raise GitProposalError("repository branch rejected")
        base = _git(root, "rev-parse", "HEAD")
        if _COMMIT.fullmatch(base) is None:
            raise GitProposalError("repository commit rejected")
        return root, branch, base

    def _require_allowed(self, argv: tuple[str, ...]) -> None:
        expected = self._allowed_commands.get(argv)
        if expected is None:
            raise GitProposalError("verification command rejected")
        try:
            actual = _verification_executable_identity(argv[0])
        except (IndexError, ValueError):
            raise GitProposalError("verification command rejected") from None
        if actual != expected:
            raise GitProposalError("verification command rejected")

    def _check_patch_applicability(self, patch: bytes, root: Path) -> None:
        try:
            current = _trusted_system_executable(_GIT_EXEC)
        except ValueError:
            raise GitProposalError("trusted git boundary unavailable") from None
        if current != self._git_identity:
            raise GitProposalError("trusted git boundary unavailable")
        returncode, _output = _run_bounded(
            (
                str(_GIT_EXEC),
                "-C",
                str(root),
                *_SAFE_GIT_CONFIG,
                "apply",
                "--check",
            ),
            cwd=root,
            timeout=8,
            max_output_bytes=16_384,
            input_bytes=patch,
        )
        if returncode != 0:
            raise GitProposalError("patch applicability rejected")

    def _verify(self, argv: tuple[str, ...], worktree: Path) -> None:
        self._require_sandbox_boundary()
        environment = _sandbox_environment(worktree)
        command = _sandbox_command(
            argv,
            (worktree,),
            deny_roots=(worktree / ".git", worktree / ".proposal-hooks"),
            read_paths=(Path(argv[0]),),
        )
        try:
            returncode, _output = _run_bounded(
                command,
                cwd=worktree,
                timeout=self._timeout,
                max_output_bytes=self._max_output,
                environment=environment,
            )
        finally:
            _clear_sandbox_environment(worktree)
        if returncode != 0:
            raise GitProposalError("verification failed")

    def _commit(self, worktree: Path, hooks: Path, arguments: tuple[str, ...]) -> None:
        self._require_sandbox_boundary()
        try:
            git_identity = _trusted_system_executable(_GIT_EXEC)
        except ValueError:
            raise GitProposalError("trusted git boundary unavailable") from None
        if git_identity != self._git_identity:
            raise GitProposalError("trusted git boundary unavailable")
        try:
            environment = _sandbox_environment(worktree)
            environment["GRAPHIFY_OUT"] = str(worktree / ".proposal-graphify-out")
            command = (
                str(_GIT_EXEC),
                "-C",
                str(worktree),
                "-c",
                f"core.hooksPath={hooks}",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "gc.auto=0",
                "-c",
                "maintenance.auto=false",
                *arguments,
            )
            returncode, _output = _run_bounded(
                command,
                cwd=worktree,
                timeout=30,
                max_output_bytes=64 * 1024,
                environment=environment,
            )
        finally:
            _clear_sandbox_environment(worktree)
        if returncode != 0:
            raise GitProposalError("git commit failed")

    def _require_sandbox_boundary(self) -> None:
        try:
            current = _trusted_system_executable(_SANDBOX_EXEC)
        except ValueError:
            raise GitProposalError("sandbox boundary unavailable") from None
        if current != self._sandbox_identity:
            raise GitProposalError("sandbox boundary unavailable")

    def _probe_sandbox_boundary(self) -> None:
        self._require_sandbox_boundary()
        try:
            _trusted_system_executable(_TRUE_EXEC)
        except ValueError:
            raise GitProposalError("sandbox boundary unavailable") from None
        profile = " ".join(
            (
                "(version 1)",
                "(deny default)",
                "(allow process*)",
                "(allow file-read*)",
                "(allow sysctl-read)",
                "(deny network*)",
            )
        )
        returncode, _output = _run_bounded(
            (str(_SANDBOX_EXEC), "-p", profile, str(_TRUE_EXEC)),
            cwd=self._runtime,
            timeout=8,
            max_output_bytes=4_096,
        )
        if returncode != 0:
            raise GitProposalError("sandbox boundary unavailable")

    def _require_expected_staging(
        self,
        worktree: Path,
        base: str,
        branch: str,
        expected_tree: str,
        boundary: _VerificationBoundary,
    ) -> None:
        if _git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD") != branch:
            raise GitProposalError("verification changed repository")
        if _git(worktree, "rev-parse", "HEAD") != base:
            raise GitProposalError("verification changed repository")
        try:
            git_identity = _trusted_system_executable(_GIT_EXEC)
        except ValueError:
            raise GitProposalError("trusted git boundary unavailable") from None
        if git_identity != self._git_identity:
            raise GitProposalError("trusted git boundary unavailable")
        unstaged, _ = _run_bounded(
            (
                str(_GIT_EXEC),
                "-C",
                str(worktree),
                *_SAFE_GIT_CONFIG,
                "diff",
                "--quiet",
            ),
            cwd=worktree,
            timeout=8,
            max_output_bytes=4_096,
        )
        if unstaged != 0 or _git(worktree, "write-tree") != expected_tree:
            raise GitProposalError("verification changed repository")
        if _verification_boundary(worktree, self._repository_root) != boundary:
            raise GitProposalError("verification changed repository")

    def _expected_tree(self, proposal: ProposalRecord, validated: ValidatedPatch) -> str:
        worktree = Path(tempfile.mkdtemp(prefix="proposal-recovery-", dir=self._runtime))
        os.chmod(worktree, 0o700)
        registered = False
        try:
            _git(
                self._repository_root,
                "worktree",
                "add",
                "--detach",
                str(worktree),
                proposal.base_commit or "",
            )
            registered = True
            patch_path = worktree / ".proposal.patch"
            _write_private(patch_path, validated.patch_bytes)
            try:
                _git(worktree, "apply", "--check", "--", str(patch_path))
                _git(worktree, "apply", "--index", "--", str(patch_path))
            finally:
                patch_path.unlink(missing_ok=True)
            return _git(worktree, "write-tree")
        finally:
            try:
                _cleanup_created_worktree(
                    self._repository_root,
                    worktree,
                    self._runtime,
                    known_registered=registered,
                )
            except GitProposalError:
                raise GitProposalRecoveryRequired("proposal cleanup requires recovery") from None

    def _verify_commit(self, proposal: ProposalRecord, commit: str, expected_tree: str) -> None:
        self._verify_commit_metadata(proposal, commit)
        if _git(self._repository_root, "show", "-s", "--format=%T", commit) != expected_tree:
            raise GitProposalError("proposal commit rejected")

    def _verify_commit_metadata(self, proposal: ProposalRecord, commit: str) -> None:
        if _COMMIT.fullmatch(commit) is None:
            raise GitProposalError("proposal commit rejected")
        ancestry = _git(self._repository_root, "rev-list", "--parents", "-n", "1", commit).split()
        if ancestry != [commit, proposal.base_commit]:
            raise GitProposalError("proposal commit rejected")
        expected_subject = f"chore(improvement): apply proposal {proposal.proposal_id}"
        if _git(self._repository_root, "show", "-s", "--format=%s", commit) != expected_subject:
            raise GitProposalError("proposal commit rejected")
        expected_message = f"{expected_subject}\n\nApply-Attempt: {proposal.apply_attempt_id}"
        if _git(self._repository_root, "show", "-s", "--format=%B", commit) != expected_message:
            raise GitProposalError("proposal commit rejected")


def _configured_path(value: Path, label: str) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError(f"{label} path is invalid")
    if any(ord(character) < 32 or ord(character) == 127 for character in str(value)):
        raise ValueError(f"{label} path is invalid")
    absolute = value.absolute()
    try:
        resolved = value.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError(f"{label} path is invalid") from None
    if absolute != resolved:
        raise ValueError(f"{label} path is invalid")
    return resolved


def _private_runtime(value: Path, *, repair_permissions: bool) -> Path:
    runtime = _configured_path(value, "runtime")
    metadata = runtime.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise ValueError("runtime path is invalid")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        if not repair_permissions:
            raise ValueError("runtime path is invalid")
        os.chmod(runtime, 0o700)
    return runtime


def _allowed_commands(
    value: tuple[tuple[str, ...], ...],
) -> dict[tuple[str, ...], _ExecutableIdentity]:
    if type(value) is not tuple or not value:
        raise ValueError("verification allowlist is invalid")
    commands: dict[tuple[str, ...], _ExecutableIdentity] = {}
    for command in value:
        if type(command) is not tuple or not 1 <= len(command) <= 64:
            raise ValueError("verification allowlist is invalid")
        total = 0
        for argument in command:
            if (
                type(argument) is not str
                or not argument
                or "\x00" in argument
                or any(ord(character) < 32 or ord(character) == 127 for character in argument)
                or len(argument.encode("utf-8")) > 1_024
            ):
                raise ValueError("verification allowlist is invalid")
            total += len(argument.encode("utf-8"))
        if total > 16_384:
            raise ValueError("verification allowlist is invalid")
        executable = Path(command[0]).name.casefold()
        interpreters = {"sh", "bash", "zsh", "dash", "ksh"}
        if executable == "env":
            raise ValueError("verification allowlist is invalid")
        if executable in interpreters and any(
            argument.startswith("-") and "c" in argument[1:] for argument in command[1:]
        ):
            raise ValueError("verification allowlist is invalid")
        if executable.startswith("python") and any(
            argument == "-c" or argument.startswith("-c") for argument in command[1:]
        ):
            raise ValueError("verification allowlist is invalid")
        commands[command] = _verification_executable_identity(command[0])
    return commands


def _verification_executable_identity(value: str) -> _ExecutableIdentity:
    try:
        return _executable_identity(value)
    except ValueError:
        return _trusted_system_executable(Path(value))


def _executable_identity(
    value: str, *, allow_system_hardlinks: bool = False
) -> _ExecutableIdentity:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("verification executable is invalid")
    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ValueError("verification executable is invalid") from None
    if (
        path.absolute() != resolved
        or not stat.S_ISREG(metadata.st_mode)
        or (not allow_system_hardlinks and metadata.st_nlink != 1)
        or metadata.st_nlink < 1
        or metadata.st_mode & 0o111 == 0
    ):
        raise ValueError("verification executable is invalid")
    return _ExecutableIdentity(
        path=resolved,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        modified_ns=metadata.st_mtime_ns,
        changed_ns=metadata.st_ctime_ns,
        mode=metadata.st_mode,
        uid=metadata.st_uid,
    )


def _trusted_system_executable(path: Path) -> _ExecutableIdentity:
    identity = _executable_identity(str(path), allow_system_hardlinks=True)
    if identity.uid != 0 or identity.mode & 0o022:
        raise ValueError("trusted process boundary is unavailable")
    return identity


def _executable_record(record: ProposalRecord, statuses: set[str]) -> ProposalRecord:
    if type(record) is not ProposalRecord or record.status not in statuses:
        raise GitProposalError("proposal state rejected")
    try:
        proposal = ProposalRecord.model_validate(
            {name: getattr(record, name) for name in ProposalRecord.model_fields},
            strict=True,
        )
    except (AttributeError, ValidationError):
        raise GitProposalError("proposal record rejected") from None
    if proposal.patch is None or not proposal.verification_argv:
        raise GitProposalError("proposal execution rejected")
    return proposal


def _matching_plan(record: ProposalRecord, current: tuple[Path, str, str]) -> None:
    root, branch, commit = current
    if (
        record.repository_root != root
        or record.original_branch != branch
        or record.base_commit != commit
        or record.proposal_branch != f"codex/memory-hub-proposal-{record.proposal_id.hex}"
    ):
        raise GitProposalError("recorded repository refs rejected")


def _matching_recovery_plan(record: ProposalRecord, root: Path) -> None:
    expected_branch = f"codex/memory-hub-proposal-{record.proposal_id.hex}"
    if (
        record.repository_root != root
        or record.proposal_branch != expected_branch
        or not record.original_branch
        or record.original_branch.startswith("-")
        or record.base_commit is None
    ):
        raise GitProposalError("recorded repository refs rejected")
    try:
        _verified_commit(root, record.base_commit)
    except UnsafeGitPatch:
        raise GitProposalError("recorded repository refs rejected") from None


def _ref_value(root: Path, branch: str) -> str | None:
    if not branch:
        return None
    output = _git(
        root,
        "for-each-ref",
        "--format=%(refname)%09%(objectname)",
        f"refs/heads/{branch}",
    )
    if not output:
        return None
    exact_name = f"refs/heads/{branch}"
    selected: str | None = None
    for line in output.splitlines():
        name, separator, commit = line.partition("\t")
        if separator != "\t" or _COMMIT.fullmatch(commit) is None:
            raise GitProposalError("repository ref rejected")
        if name == exact_name:
            if selected is not None:
                raise GitProposalError("repository ref rejected")
            selected = commit
        elif name.startswith(f"{exact_name}/"):
            raise GitProposalError("repository ref namespace collision")
    return selected


def _git(root: Path, *arguments: str) -> str:
    try:
        _trusted_system_executable(_GIT_EXEC)
    except ValueError:
        raise GitProposalError("trusted git boundary unavailable") from None
    returncode, output = _run_bounded(
        (str(_GIT_EXEC), "-C", str(root), *_SAFE_GIT_CONFIG, *arguments),
        cwd=root,
        timeout=30,
        max_output_bytes=64 * 1024,
    )
    if returncode != 0:
        raise GitProposalError("git operation failed")
    return output.strip()


def _capture_repository_hooks(root: Path) -> tuple[_HookSnapshot, ...]:
    hooks = _repository_hooks_path(root)
    if hooks is None:
        return ()
    snapshots: list[_HookSnapshot] = []
    for name in sorted(_KNOWN_GIT_HOOKS):
        document = _read_active_hook(hooks / name)
        if document is None:
            continue
        if name not in _REPLAYED_HOOKS:
            raise GitProposalError("unsupported active repository hook")
        snapshots.append(_HookSnapshot(name=name, document=document))
    return tuple(snapshots)


def _repository_hooks_path(root: Path) -> Path | None:
    try:
        _trusted_system_executable(_GIT_EXEC)
    except ValueError:
        raise GitProposalError("trusted git boundary unavailable") from None
    returncode, output = _run_bounded(
        (
            str(_GIT_EXEC),
            "-C",
            str(root),
            "-c",
            "core.fsmonitor=false",
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "rev-parse",
            "--git-path",
            "hooks",
        ),
        cwd=root,
        timeout=8,
        max_output_bytes=16_384,
    )
    document = output.strip()
    if (
        returncode != 0
        or not document
        or any(ord(character) < 32 or ord(character) == 127 for character in document)
    ):
        raise GitProposalError("repository hooks rejected")
    candidate = Path(document)
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate == Path("/dev/null"):
        return None
    try:
        metadata = candidate.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise GitProposalError("repository hooks rejected") from None
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise GitProposalError("repository hooks rejected") from None
    if (
        candidate.absolute() != resolved
        or stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
    ):
        raise GitProposalError("repository hooks rejected")
    return resolved


def _read_active_hook(path: Path) -> bytes | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise GitProposalError("repository hook rejected") from None
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise GitProposalError("repository hook rejected")
    if before.st_mode & 0o111 == 0:
        return None
    if (
        before.st_uid != os.getuid()
        or before.st_nlink != 1
        or not 0 <= before.st_size <= _MAX_HOOK_BYTES
    ):
        raise GitProposalError("repository hook rejected")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise GitProposalError("repository hook rejected") from None
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise GitProposalError("repository hook changed")
        document = bytearray()
        while len(document) <= _MAX_HOOK_BYTES:
            chunk = os.read(descriptor, min(64 * 1024, _MAX_HOOK_BYTES + 1 - len(document)))
            if not chunk:
                break
            document.extend(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise GitProposalError("repository hook rejected") from None
    finally:
        os.close(descriptor)
    if (
        len(document) > _MAX_HOOK_BYTES
        or len(document) != before.st_size
        or _file_identity(after) != _file_identity(before)
    ):
        raise GitProposalError("repository hook changed")
    return bytes(document)


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
    )


def _materialize_repository_hooks(worktree: Path) -> Path:
    hooks = worktree / ".proposal-hooks"
    try:
        hooks.mkdir(mode=0o700)
        metadata = hooks.lstat()
    except OSError:
        raise GitProposalError("private hooks rejected") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 2
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise GitProposalError("private hooks rejected")
    return hooks


def _require_hooks_unchanged(
    root: Path,
    expected: tuple[_HookSnapshot, ...],
) -> None:
    if _capture_repository_hooks(root) != expected:
        raise GitProposalRecoveryRequired("repository hooks require state recovery")


def _verification_summary(snapshots: tuple[_HookSnapshot, ...]) -> str:
    if not snapshots:
        return "verification passed"
    names = ",".join(snapshot.name for snapshot in snapshots)
    return f"verification passed; hooks deferred: {names}"


def _write_private(path: Path, document: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(document)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)


def _git_cleanup(root: Path, worktree: Path) -> None:
    admin, admin_expected = _worktree_admin_directory(root, worktree)
    matching_records = tuple(path for path, _branch in _worktree_records(root) if path == worktree)
    if matching_records != (worktree,):
        raise GitProposalError("private worktree cleanup rejected")

    parent_fd, quarantine, expected = _quarantine_private_directory(worktree)
    admin_fd: int | None = None
    admin_quarantine: str | None = None
    admin_selected: tuple[int, int, int, int] | None = None
    admin_removed = False
    try:
        try:
            admin_fd, admin_quarantine, admin_selected = _quarantine_worktree_admin(
                admin, admin_expected
            )
            if _entry_exists(parent_fd, worktree.name):
                _remove_entry_at(parent_fd, worktree.name)
                _restore_quarantined_directory(
                    admin_fd,
                    admin_quarantine,
                    admin.name,
                    admin_selected,
                )
                admin_quarantine = None
                _restore_quarantined_directory(
                    parent_fd,
                    quarantine,
                    worktree.name,
                    expected,
                )
                quarantine = ""
                raise GitProposalError("private worktree cleanup rejected")
            _remove_quarantined_directory(
                admin_fd,
                admin_quarantine,
                admin_selected,
            )
            admin_quarantine = None
            admin_removed = True
            if _registered_worktree(root, worktree):
                raise GitProposalError("private worktree cleanup failed")
            if _entry_exists(parent_fd, worktree.name):
                _remove_entry_at(parent_fd, worktree.name)
            _remove_quarantined_directory(parent_fd, quarantine, expected)
            quarantine = ""
        except (GitProposalError, OSError):
            if (
                not admin_removed
                and admin_fd is not None
                and admin_quarantine is not None
                and admin_selected is not None
            ):
                _restore_quarantined_directory(
                    admin_fd,
                    admin_quarantine,
                    admin.name,
                    admin_selected,
                )
                admin_quarantine = None
            if quarantine:
                if _entry_exists(parent_fd, worktree.name):
                    _remove_entry_at(parent_fd, worktree.name)
                _restore_quarantined_directory(
                    parent_fd,
                    quarantine,
                    worktree.name,
                    expected,
                )
                quarantine = ""
            raise GitProposalError("private worktree cleanup failed") from None
    finally:
        if admin_fd is not None:
            os.close(admin_fd)
        os.close(parent_fd)


def _worktree_admin_directory(
    root: Path,
    worktree: Path,
) -> tuple[Path, tuple[int, int, int, int]]:
    document = _read_private_file(worktree / ".git", 4_096)
    try:
        line = document.decode("utf-8")
    except UnicodeDecodeError:
        raise GitProposalError("worktree metadata rejected") from None
    if not line.endswith("\n") or line.count("\n") != 1 or not line.startswith("gitdir: "):
        raise GitProposalError("worktree metadata rejected")
    raw_admin = line.removeprefix("gitdir: ").removesuffix("\n")
    if not raw_admin or any(ord(character) < 32 for character in raw_admin):
        raise GitProposalError("worktree metadata rejected")
    candidate = Path(raw_admin)
    if not candidate.is_absolute():
        raise GitProposalError("worktree metadata rejected")
    try:
        admin = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise GitProposalError("worktree metadata rejected") from None
    if candidate.absolute() != admin:
        raise GitProposalError("worktree metadata rejected")

    common_value = _git(root, "rev-parse", "--git-common-dir")
    common_candidate = Path(common_value)
    if not common_candidate.is_absolute():
        common_candidate = root / common_candidate
    try:
        common = common_candidate.resolve(strict=True)
        worktrees = (common / "worktrees").resolve(strict=True)
    except (OSError, RuntimeError):
        raise GitProposalError("worktree metadata rejected") from None
    if (
        common_candidate.absolute() != common
        or admin.parent != worktrees
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}", admin.name) is None
    ):
        raise GitProposalError("worktree metadata rejected")
    try:
        metadata = admin.lstat()
    except OSError:
        raise GitProposalError("worktree metadata rejected") from None
    expected = _directory_identity(metadata)
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise GitProposalError("worktree metadata rejected")
    backlink = _read_private_file(admin / "gitdir", 4_096)
    if backlink != f"{worktree / '.git'}\n".encode():
        raise GitProposalError("worktree metadata rejected")
    return admin, expected


def _read_private_file(path: Path, limit: int) -> bytes:
    try:
        before = path.lstat()
    except OSError:
        raise GitProposalError("private metadata rejected") from None
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or not 0 <= before.st_size <= limit
    ):
        raise GitProposalError("private metadata rejected")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise GitProposalError("private metadata rejected") from None
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise GitProposalError("private metadata changed")
        document = bytearray()
        while len(document) <= limit:
            chunk = os.read(descriptor, min(4_096, limit + 1 - len(document)))
            if not chunk:
                break
            document.extend(chunk)
        after = os.fstat(descriptor)
    except OSError:
        raise GitProposalError("private metadata rejected") from None
    finally:
        os.close(descriptor)
    if (
        len(document) > limit
        or len(document) != before.st_size
        or _file_identity(after) != _file_identity(before)
    ):
        raise GitProposalError("private metadata changed")
    return bytes(document)


def _quarantine_worktree_admin(
    admin: Path,
    expected: tuple[int, int, int, int],
) -> tuple[int, str, tuple[int, int, int, int]]:
    parent_fd, quarantine, selected = _quarantine_private_directory(admin)
    if selected == expected:
        return parent_fd, quarantine, selected
    try:
        _restore_quarantined_directory(
            parent_fd,
            quarantine,
            admin.name,
            selected,
        )
    finally:
        os.close(parent_fd)
    raise GitProposalError("worktree metadata changed")


def _cleanup_created_worktree(
    root: Path,
    worktree: Path,
    runtime: Path,
    *,
    known_registered: bool,
) -> None:
    first_error: GitProposalError | None = None
    should_unregister = known_registered
    if not known_registered:
        try:
            should_unregister = _registered_worktree(root, worktree)
        except GitProposalError as error:
            first_error = error
    if should_unregister:
        try:
            _git_cleanup(root, worktree)
        except GitProposalError as error:
            first_error = first_error or error
    try:
        _remove_private_directory(worktree, runtime)
    except GitProposalError as error:
        first_error = first_error or error
    if first_error is not None:
        raise GitProposalError("private worktree cleanup failed") from None


def _remove_private_directory(path: Path, runtime: Path) -> None:
    if not _is_private_service_path(path, runtime):
        raise GitProposalError("private worktree cleanup rejected")
    try:
        path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise GitProposalError("private worktree cleanup failed") from None
    parent_fd, quarantine, expected = _quarantine_private_directory(path)
    try:
        _remove_quarantined_directory(parent_fd, quarantine, expected)
    finally:
        os.close(parent_fd)


def _quarantine_private_directory(
    path: Path,
) -> tuple[int, str, tuple[int, int, int, int]]:
    if not path.is_absolute() or not path.name:
        raise GitProposalError("private worktree cleanup rejected")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        parent_fd = os.open(path.parent, flags)
    except OSError:
        raise GitProposalError("private worktree cleanup rejected") from None
    quarantine = f".proposal-cleanup-{uuid4().hex}"
    try:
        before = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        expected = _directory_identity(before)
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISDIR(before.st_mode):
            raise GitProposalError("private worktree cleanup rejected")
        os.rename(
            path.name,
            quarantine,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
        )
        after = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if _directory_identity(after) != expected:
            _restore_quarantined_directory(
                parent_fd,
                quarantine,
                path.name,
                expected,
            )
            raise GitProposalError("private worktree cleanup rejected")
        return parent_fd, quarantine, expected
    except GitProposalError:
        os.close(parent_fd)
        raise
    except OSError:
        os.close(parent_fd)
        raise GitProposalError("private worktree cleanup rejected") from None


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_uid


def _restore_quarantined_directory(
    parent_fd: int,
    quarantine: str,
    original: str,
    expected: tuple[int, int, int, int],
) -> None:
    try:
        selected = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if _directory_identity(selected) != expected:
            raise GitProposalError("private worktree cleanup rejected")
        try:
            os.stat(original, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            os.rename(
                quarantine,
                original,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
            )
            return
        raise GitProposalError("private worktree cleanup rejected")
    except GitProposalError:
        raise
    except OSError:
        raise GitProposalError("private worktree cleanup rejected") from None


def _remove_quarantined_directory(
    parent_fd: int,
    quarantine: str,
    expected: tuple[int, int, int, int],
) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise GitProposalError("private worktree cleanup rejected")
    try:
        selected = os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        if _directory_identity(selected) != expected:
            raise GitProposalError("private worktree cleanup rejected")
        shutil.rmtree(quarantine, dir_fd=parent_fd)
        try:
            os.stat(quarantine, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise GitProposalError("private worktree cleanup failed")
    except GitProposalError:
        raise
    except OSError:
        raise GitProposalError("private worktree cleanup failed") from None


def _entry_exists(parent_fd: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        raise GitProposalError("private worktree cleanup failed") from None
    return True


def _remove_entry_at(parent_fd: int, name: str) -> None:
    if not shutil.rmtree.avoids_symlink_attacks:
        raise GitProposalError("private worktree cleanup rejected")
    try:
        metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(name, dir_fd=parent_fd)
        else:
            os.unlink(name, dir_fd=parent_fd)
        if _entry_exists(parent_fd, name):
            raise GitProposalError("private worktree cleanup failed")
    except GitProposalError:
        raise
    except OSError:
        raise GitProposalError("private worktree cleanup failed") from None


def _is_private_service_path(path: Path, runtime: Path) -> bool:
    return (
        path.is_absolute()
        and path.parent == runtime
        and path.name.startswith(("proposal-worktree-", "proposal-recovery-"))
    )


def _worktree_records(root: Path) -> tuple[tuple[Path, str | None], ...]:
    output = _git(root, "worktree", "list", "--porcelain")
    records: list[tuple[Path, str | None]] = []
    for block in output.split("\n\n"):
        if not block:
            continue
        path: Path | None = None
        branch: str | None = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree "))
            elif line.startswith("branch refs/heads/"):
                branch = line.removeprefix("branch refs/heads/")
        if path is None or not path.is_absolute():
            raise GitProposalError("worktree registry rejected")
        records.append((path, branch))
    return tuple(records)


def _registered_worktree(root: Path, selected: Path) -> bool:
    return any(path == selected for path, _branch in _worktree_records(root))


def _branch_worktrees(root: Path, selected_branch: str) -> tuple[Path, ...]:
    return tuple(path for path, branch in _worktree_records(root) if branch == selected_branch)


def _refs_snapshot(root: Path) -> tuple[tuple[str, str], ...]:
    output = _git(root, "for-each-ref", "--format=%(refname)%09%(objectname)")
    refs: list[tuple[str, str]] = []
    for line in output.splitlines():
        name, separator, commit = line.partition("\t")
        if separator != "\t" or not name.startswith("refs/") or _COMMIT.fullmatch(commit) is None:
            raise GitProposalError("repository refs rejected")
        refs.append((name, commit))
    if len({name for name, _commit in refs}) != len(refs):
        raise GitProposalError("repository refs rejected")
    return tuple(refs)


def _verification_boundary(worktree: Path, root: Path) -> _VerificationBoundary:
    return _VerificationBoundary(
        status=_git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            "--ignored=matching",
        ),
        refs=_refs_snapshot(root),
        worktrees=_worktree_records(root),
    )


def _require_post_commit_boundary(
    root: Path,
    worktree: Path,
    before: _VerificationBoundary,
    proposal_branch: str,
    applied_commit: str,
    original_branch: str,
    base_commit: str,
) -> None:
    expected_refs = dict(before.refs)
    proposal_ref = f"refs/heads/{proposal_branch}"
    if expected_refs.get(proposal_ref) != base_commit:
        raise GitProposalError("proposal branch rejected")
    expected_refs[proposal_ref] = applied_commit
    if dict(_refs_snapshot(root)) != expected_refs:
        raise GitProposalError("proposal commit changed refs")
    if _worktree_records(root) != before.worktrees:
        raise GitProposalError("proposal commit changed worktrees")
    if _ref_value(root, original_branch) != base_commit:
        raise GitProposalError("original branch changed")
    if (
        _git(root, "symbolic-ref", "--quiet", "--short", "HEAD") != original_branch
        or _git(root, "rev-parse", "HEAD") != base_commit
        or _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    ):
        raise GitProposalError("original worktree changed")
    if _git(worktree, "rev-parse", "HEAD") != applied_commit:
        raise GitProposalError("proposal commit rejected")


def _sandbox_environment(worktree: Path) -> dict[str, str]:
    home = worktree / ".proposal-home"
    temporary = worktree / ".proposal-tmp"
    graphify = worktree / ".proposal-graphify-out"
    for directory in (home, temporary, graphify):
        try:
            directory.mkdir(mode=0o700)
            metadata = directory.lstat()
        except OSError:
            _clear_sandbox_environment(worktree)
            raise GitProposalError("sandbox environment rejected") from None
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 2
        ):
            _clear_sandbox_environment(worktree)
            raise GitProposalError("sandbox environment rejected")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.chmod(directory, 0o700)
    environment = _minimal_environment()
    environment.update(
        {
            "HOME": str(home),
            "TMPDIR": str(temporary),
            "XDG_CACHE_HOME": str(home / ".cache"),
        }
    )
    return environment


def _clear_sandbox_environment(worktree: Path) -> None:
    for name in (
        ".proposal-home",
        ".proposal-tmp",
        ".proposal-graphify-out",
    ):
        path = worktree / name
        if not path.exists() and not path.is_symlink():
            continue
        try:
            metadata = path.lstat()
        except OSError:
            raise GitProposalError("sandbox environment cleanup failed") from None
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise GitProposalError("sandbox environment cleanup rejected")
        try:
            shutil.rmtree(path)
        except OSError:
            raise GitProposalError("sandbox environment cleanup failed") from None


def _sandbox_command(
    argv: tuple[str, ...],
    write_roots: tuple[Path, ...],
    *,
    deny_roots: tuple[Path, ...] = (),
    read_paths: tuple[Path, ...] = (),
) -> tuple[str, ...]:
    if not argv or not write_roots:
        raise GitProposalError("sandbox boundary rejected")
    profile = _sandbox_profile(
        write_roots,
        deny_roots=deny_roots,
        read_paths=read_paths,
    )
    return (str(_SANDBOX_EXEC), "-p", profile, *argv)


def _sandbox_profile(
    write_roots: tuple[Path, ...],
    *,
    deny_roots: tuple[Path, ...] = (),
    read_paths: tuple[Path, ...] = (),
) -> str:
    clauses: list[str] = []
    for root in write_roots:
        if not root.is_absolute():
            raise GitProposalError("sandbox boundary rejected")
        clauses.append(f'(subpath "{_seatbelt_text(str(root))}")')
    write_denials = tuple(
        f'(deny file-write* (subpath "{_seatbelt_text(str(root))}"))' for root in deny_roots
    )
    read_denials = tuple(
        " ".join(
            (
                f'(deny file-read* (subpath "{_seatbelt_text(str(root))}"))',
                f'(deny file-read* (literal "{_seatbelt_text(str(root))}"))',
            )
        )
        for root in deny_roots
        if not any(path == root or root in path.parents for path in read_paths)
    )
    read_roots = tuple((*_SANDBOX_SYSTEM_READ_ROOTS, *write_roots))
    reads = tuple(f'(subpath "{_seatbelt_text(str(root))}")' for root in read_roots) + (
        '(literal "/")',
        '(literal "/dev/null")',
        '(literal "/dev/random")',
        '(literal "/dev/urandom")',
        *tuple(f'(literal "{_seatbelt_text(str(path))}")' for path in read_paths),
    )
    return " ".join(
        (
            "(version 1)",
            "(deny default)",
            "(allow process*)",
            "(allow file-read-metadata)",
            f"(allow file-read* {' '.join(reads)})",
            "(allow sysctl-read)",
            "(deny network*)",
            f'(allow file-write* {" ".join(clauses)} (literal "/dev/null"))',
            *write_denials,
            *read_denials,
        )
    )


def _seatbelt_text(value: str) -> str:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise GitProposalError("sandbox boundary rejected")
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _run_bounded(
    command: tuple[str, ...],
    *,
    cwd: Path,
    timeout: float,
    max_output_bytes: int,
    environment: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> tuple[int, str]:
    if input_bytes is not None and (
        type(input_bytes) is not bytes or len(input_bytes) > 1024 * 1024
    ):
        raise GitProposalError("process input rejected")
    try:
        process = subprocess.Popen(
            command,
            shell=False,
            cwd=cwd,
            env=dict(environment) if environment is not None else _minimal_environment(),
            stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=(os.name == "posix"),
        )
    except OSError:
        raise GitProposalError("process launch failed") from None
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    input_offset = 0
    try:
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        if process.stdin is not None:
            if input_bytes:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            else:
                process.stdin.close()
    except (OSError, ValueError):
        selector.close()
        process.stdout.close()
        if process.stdin is not None:
            process.stdin.close()
        _terminate_process_group(process)
        raise GitProposalError("process boundary failed") from None
    output = bytearray()
    deadline = time.monotonic() + timeout
    failed = False
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failed = True
                break
            events = selector.select(min(remaining, 0.1))
            for key, _mask in events:
                if key.data == "stdin":
                    assert input_bytes is not None
                    assert process.stdin is not None
                    try:
                        count = os.write(
                            process.stdin.fileno(),
                            input_bytes[input_offset : input_offset + 8_192],
                        )
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        count = 0
                    if count > 0:
                        input_offset += count
                    if count <= 0 or input_offset == len(input_bytes):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                    continue
                chunk = os.read(process.stdout.fileno(), 8_192)
                if not chunk:
                    selector.unregister(process.stdout)
                    continue
                room = max_output_bytes + 1 - len(output)
                output.extend(chunk[: max(0, room)])
                if len(output) > max_output_bytes:
                    failed = True
                    break
            if failed:
                break
            if process.poll() is not None and not events:
                continue
        if failed:
            _terminate_process_group(process)
            raise GitProposalError("process boundary exceeded")
        remaining = max(0.0, deadline - time.monotonic())
        try:
            returncode = process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            _terminate_process_group(process)
            raise GitProposalError("process boundary exceeded") from None
    except OSError:
        raise GitProposalError("process boundary failed") from None
    finally:
        selector.close()
        try:
            process.stdout.close()
        except OSError:
            pass
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            _terminate_process_group(process)
    try:
        text = bytes(output).decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return returncode, text


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        pass
