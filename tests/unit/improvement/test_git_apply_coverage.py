from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import project_memory_hub.improvement.git_apply as subject
from project_memory_hub.improvement.git_apply import (
    GitProposalError,
    GitProposalRecoveryRequired,
    PatchValidator,
    UnsafeGitPatch,
    ValidatedPatch,
)


VALID_MODIFY = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-seed
+updated
"""

VALID_ADD = """\
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new
"""


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["/usr/bin/git", "-C", str(root), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=8,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    ("keyword", "value"),
    (
        ("max_patch_bytes", 0),
        ("max_patch_bytes", True),
        ("max_patch_bytes", 2**31),
        ("max_files", 0),
        ("max_files", True),
        ("max_files", 10_001),
    ),
)
def test_patch_validator_rejects_invalid_limits(keyword: str, value: object) -> None:
    with pytest.raises(ValueError):
        PatchValidator(**{keyword: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("document", (None, "", "\ud800"))
def test_patch_validator_rejects_non_documents(document: object) -> None:
    with pytest.raises(UnsafeGitPatch, match="patch rejected"):
        PatchValidator().validate(document, Path("/unused"), "unused")  # type: ignore[arg-type]


def test_patch_validator_rejects_duplicate_exact_paths(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)

    with pytest.raises(UnsafeGitPatch, match="patch rejected"):
        PatchValidator().validate(f"{VALID_ADD}\n{VALID_ADD}", root, base)


@pytest.mark.parametrize(
    "section",
    (
        [],
        ["diff --git a/README.md b/other.md"],
        ["diff --git a/README.md b/README.md"],
        [
            "diff --git a/README.md b/README.md",
            "--- a/README.md",
            "+++ b/README.md",
            "not-a-hunk",
        ],
        [
            "diff --git a/README.md b/README.md",
            "--- a/README.md",
            "+++ b/README.md",
            "@@ -1 +1 @@",
            "?invalid",
        ],
    ),
)
def test_parse_section_rejects_truncated_or_ambiguous_shapes(section: list[str]) -> None:
    with pytest.raises(UnsafeGitPatch, match="patch rejected"):
        subject._parse_section(section)


def test_parse_patch_rejects_leading_non_diff_content() -> None:
    with pytest.raises(UnsafeGitPatch, match="patch rejected"):
        subject._parse_patch("comment\n" + VALID_MODIFY)


def test_parse_section_accepts_a_bounded_index_header() -> None:
    section = VALID_MODIFY.splitlines()
    section.insert(1, "index 1111111..2222222 100644")

    assert subject._parse_section(section) == subject._PatchEntry(path="README.md", added=False)


def test_recorded_validator_accepts_an_addition_below_missing_ancestors(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path)
    patch = VALID_ADD.replace("new.txt", "missing/deep/new.txt")

    assert PatchValidator().validate_recorded(patch, root, base).paths == ("missing/deep/new.txt",)


def test_recorded_validator_rejects_adding_over_an_existing_tree_entry(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path)
    patch = VALID_ADD.replace("new.txt", "README.md")

    with pytest.raises(UnsafeGitPatch, match="patch target rejected"):
        PatchValidator().validate_recorded(patch, root, base)


@pytest.mark.parametrize("target_kind", ("symlink", "tree", "gitlink"))
def test_recorded_validator_rejects_non_blob_modify_targets(
    tmp_path: Path,
    target_kind: str,
) -> None:
    root, base = _repository(tmp_path)
    if target_kind == "symlink":
        (root / "special").symlink_to("README.md")
        _git(root, "add", "special")
    elif target_kind == "tree":
        (root / "special").mkdir()
        (root / "special" / "child").write_text("child\n", encoding="utf-8")
        _git(root, "add", "special/child")
    else:
        _git(root, "update-index", "--add", "--cacheinfo", f"160000,{base},special")
    _git(root, "commit", "-m", f"add {target_kind}")
    base = _git(root, "rev-parse", "HEAD")
    patch = VALID_MODIFY.replace("README.md", "special")

    with pytest.raises(UnsafeGitPatch, match="patch target rejected"):
        PatchValidator().validate_recorded(patch, root, base)


def test_filesystem_validator_accepts_missing_nested_addition(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)
    patch = VALID_ADD.replace("new.txt", "missing/deep/new.txt")

    assert PatchValidator().validate(patch, root, base).paths == ("missing/deep/new.txt",)


def test_filesystem_validator_rejects_untracked_add_target(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)
    (root / "new.txt").write_text("untracked\n", encoding="utf-8")

    with pytest.raises(UnsafeGitPatch, match="patch target rejected"):
        PatchValidator().validate(VALID_ADD, root, base)


def test_filesystem_validator_rejects_add_shape_for_missing_tracked_target(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path)
    (root / "README.md").unlink()
    patch = VALID_ADD.replace("new.txt", "README.md")

    with pytest.raises(UnsafeGitPatch, match="patch target rejected"):
        PatchValidator().validate(patch, root, base)


@pytest.mark.parametrize(
    "overrides",
    (
        {"repair_runtime_permissions": 1},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 3_601},
        {"max_output_bytes": True},
        {"max_output_bytes": 0},
        {"max_output_bytes": 2**24 + 1},
    ),
)
def test_applier_constructor_rejects_invalid_runtime_limits(
    monkeypatch: pytest.MonkeyPatch,
    overrides: dict[str, object],
) -> None:
    identity = object()
    monkeypatch.setattr(subject, "_verified_repository_root", lambda value: value)
    monkeypatch.setattr(subject, "_private_runtime", lambda value, **_kwargs: value)
    monkeypatch.setattr(subject, "_allowed_commands", lambda _value: {})
    monkeypatch.setattr(subject, "_trusted_system_executable", lambda _path: identity)

    with pytest.raises(ValueError):
        subject.GitProposalApplier(
            Path("/repository"),
            Path("/runtime"),
            allowed_verification_argv=(("/usr/bin/true",),),
            **overrides,  # type: ignore[arg-type]
        )


def test_applier_constructor_redacts_invalid_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_repository(_value: Path) -> Path:
        raise UnsafeGitPatch("private repository detail")

    monkeypatch.setattr(subject, "_verified_repository_root", reject_repository)

    with pytest.raises(ValueError, match="repository path is invalid") as captured:
        subject.GitProposalApplier(
            Path("/repository"),
            Path("/runtime"),
            allowed_verification_argv=(("/usr/bin/true",),),
        )

    assert "private repository detail" not in str(captured.value)


@pytest.mark.parametrize(
    "output",
    (
        "refs/heads/topic\tshort",
        "refs/heads/topic\t" + "a" * 40 + "\nrefs/heads/topic\t" + "b" * 40,
        "refs/heads/topic/child\t" + "a" * 40,
    ),
)
def test_ref_value_rejects_malformed_duplicate_or_colliding_refs(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    monkeypatch.setattr(subject, "_git", lambda *_args: output)

    with pytest.raises(GitProposalError, match="repository ref"):
        subject._ref_value(Path("/repository"), "topic")


def test_ref_value_returns_none_for_an_empty_branch() -> None:
    assert subject._ref_value(Path("/repository"), "") is None


@pytest.mark.parametrize(
    "output",
    (
        "branch refs/heads/main",
        "worktree relative/path\nbranch refs/heads/main",
    ),
)
def test_worktree_records_reject_missing_or_relative_paths(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    monkeypatch.setattr(subject, "_git", lambda *_args: output)

    with pytest.raises(GitProposalError, match="worktree registry rejected"):
        subject._worktree_records(Path("/repository"))


def test_worktree_records_ignore_empty_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subject, "_git", lambda *_args: "\n\n")

    assert subject._worktree_records(Path("/repository")) == ()


@pytest.mark.parametrize(
    "output",
    (
        "main\t" + "a" * 40,
        "refs/heads/main\tshort",
        "refs/heads/main\t" + "a" * 40 + "\nrefs/heads/main\t" + "b" * 40,
    ),
)
def test_refs_snapshot_rejects_malformed_or_duplicate_refs(
    monkeypatch: pytest.MonkeyPatch,
    output: str,
) -> None:
    monkeypatch.setattr(subject, "_git", lambda *_args: output)

    with pytest.raises(GitProposalError, match="repository refs rejected"):
        subject._refs_snapshot(Path("/repository"))


@pytest.mark.parametrize(
    "drift",
    (
        "recorded_proposal_ref",
        "refs",
        "worktrees",
        "original_ref",
        "original_worktree",
        "proposal_head",
    ),
)
def test_post_commit_boundary_rejects_each_ref_and_worktree_race(
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    root = Path("/repository")
    worktree = Path("/runtime/proposal-worktree-test")
    base = "a" * 40
    applied = "b" * 40
    proposal = "codex/memory-hub-proposal-test"
    original = "main"
    proposal_ref = f"refs/heads/{proposal}"
    before_refs = (("refs/heads/main", base), (proposal_ref, base))
    if drift == "recorded_proposal_ref":
        before_refs = (("refs/heads/main", base),)
    records = ((root, original), (worktree, proposal))
    before = subject._VerificationBoundary(status="", refs=before_refs, worktrees=records)
    expected_refs = (("refs/heads/main", base), (proposal_ref, applied))
    monkeypatch.setattr(
        subject,
        "_refs_snapshot",
        lambda _root: (("refs/heads/main", applied),) if drift == "refs" else expected_refs,
    )
    monkeypatch.setattr(
        subject,
        "_worktree_records",
        lambda _root: ((root, original),) if drift == "worktrees" else records,
    )
    monkeypatch.setattr(
        subject,
        "_ref_value",
        lambda _root, _branch: applied if drift == "original_ref" else base,
    )

    def fake_git(selected: Path, *arguments: str) -> str:
        if arguments[0] == "symbolic-ref":
            return "other" if drift == "original_worktree" else original
        if arguments[0] == "status":
            return ""
        if selected == worktree:
            return base if drift == "proposal_head" else applied
        return base

    monkeypatch.setattr(subject, "_git", fake_git)

    with pytest.raises(GitProposalError):
        subject._require_post_commit_boundary(
            root,
            worktree,
            before,
            proposal,
            applied,
            original,
            base,
        )


@pytest.mark.parametrize("kind", ("symlink", "directory", "hardlink", "non_executable"))
def test_active_hook_rejects_special_files_and_ignores_non_executable(
    tmp_path: Path,
    kind: str,
) -> None:
    hook = tmp_path / "post-commit"
    target = tmp_path / "target"
    if kind == "symlink":
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        hook.symlink_to(target)
    elif kind == "directory":
        hook.mkdir()
    elif kind == "hardlink":
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o700)
        os.link(target, hook)
    else:
        hook.write_text("#!/bin/sh\n", encoding="utf-8")
        hook.chmod(0o600)

    if kind == "non_executable":
        assert subject._read_active_hook(hook) is None
    else:
        with pytest.raises(GitProposalError, match="repository hook rejected"):
            subject._read_active_hook(hook)


@pytest.mark.parametrize("changed_at", ("open", "read"))
def test_active_hook_rejects_identity_change_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed_at: str,
) -> None:
    hook = tmp_path / "post-commit"
    hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    hook.chmod(0o700)
    identities = iter(((1,), (2,))) if changed_at == "open" else iter(((1,), (1,), (2,), (1,)))
    monkeypatch.setattr(subject, "_file_identity", lambda _metadata: next(identities))

    with pytest.raises(GitProposalError, match="repository hook changed"):
        subject._read_active_hook(hook)


@pytest.mark.parametrize("kind", ("disabled", "missing", "symlink"))
def test_repository_hooks_path_handles_disabled_missing_and_symlinked_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    monkeypatch.setattr(subject, "_trusted_system_executable", lambda _path: object())
    if kind == "disabled":
        selected = Path("/dev/null")
    elif kind == "missing":
        selected = tmp_path / "missing-hooks"
    else:
        actual = tmp_path / "actual-hooks"
        actual.mkdir()
        selected = tmp_path / "hooks-link"
        selected.symlink_to(actual, target_is_directory=True)
    monkeypatch.setattr(subject, "_run_bounded", lambda *_args, **_kwargs: (0, str(selected)))

    if kind in {"disabled", "missing"}:
        assert subject._repository_hooks_path(tmp_path) is None
    else:
        with pytest.raises(GitProposalError, match="repository hooks rejected"):
            subject._repository_hooks_path(tmp_path)


def test_hook_snapshot_change_requires_recovery(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = (subject._HookSnapshot(name="post-commit", document=b"old"),)
    monkeypatch.setattr(
        subject,
        "_capture_repository_hooks",
        lambda _root: (subject._HookSnapshot(name="post-commit", document=b"new"),),
    )

    with pytest.raises(GitProposalRecoveryRequired, match="hooks require state recovery"):
        subject._require_hooks_unchanged(Path("/repository"), expected)


def test_execute_recovers_exact_commit_created_before_commit_command_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    applier = object.__new__(subject.GitProposalApplier)
    applier._repository_root = tmp_path / "repository"
    applier._runtime = runtime
    branch = "codex/memory-hub-proposal-test"
    base = "a" * 40
    candidate = "b" * 40
    expected_tree = "c" * 40
    proposal = SimpleNamespace(
        proposal_branch=branch,
        base_commit=base,
        original_branch="main",
        proposal_id=uuid4(),
        apply_attempt_id=uuid4(),
        verification_argv=("/usr/bin/true",),
    )
    boundary = subject._VerificationBoundary(status="", refs=(), worktrees=())
    ref_values = iter((None, candidate))
    verified: list[tuple[object, str, str]] = []

    monkeypatch.setattr(subject, "_ref_value", lambda *_args: next(ref_values))
    monkeypatch.setattr(subject, "_capture_repository_hooks", lambda _root: ())
    monkeypatch.setattr(subject, "_materialize_repository_hooks", lambda path: path / "hooks")
    monkeypatch.setattr(subject, "_write_private", lambda *_args: None)
    monkeypatch.setattr(
        subject,
        "_git",
        lambda _root, *arguments: expected_tree if arguments[0] == "write-tree" else "",
    )
    monkeypatch.setattr(subject, "_verification_boundary", lambda *_args: boundary)
    monkeypatch.setattr(subject.GitProposalApplier, "_verify", lambda *_args: None)
    monkeypatch.setattr(
        subject.GitProposalApplier,
        "_require_expected_staging",
        lambda *_args: None,
    )

    def commit_then_report_failure(*_args: object) -> None:
        raise GitProposalError("commit command failed after creating commit")

    monkeypatch.setattr(subject.GitProposalApplier, "_commit", commit_then_report_failure)
    monkeypatch.setattr(
        subject.GitProposalApplier,
        "_verify_commit",
        lambda _self, record, commit, tree: verified.append((record, commit, tree)),
    )
    monkeypatch.setattr(subject, "_require_post_commit_boundary", lambda *_args: None)
    monkeypatch.setattr(subject, "_require_hooks_unchanged", lambda *_args: None)
    monkeypatch.setattr(subject, "_cleanup_created_worktree", lambda *_args, **_kwargs: None)

    with pytest.raises(GitProposalRecoveryRequired, match="requires state recovery"):
        applier._execute(proposal, ValidatedPatch(b"patch", ("README.md",)), create_branch=True)

    assert verified == [(proposal, candidate, expected_tree)]


@pytest.mark.parametrize(
    ("create_branch", "branch_commit"),
    ((True, "b" * 40), (False, None), (False, "c" * 40)),
)
def test_execute_rejects_branch_ref_races_before_worktree_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    create_branch: bool,
    branch_commit: str | None,
) -> None:
    applier = object.__new__(subject.GitProposalApplier)
    applier._repository_root = tmp_path / "repository"
    applier._runtime = tmp_path
    proposal = SimpleNamespace(proposal_branch="proposal", base_commit="a" * 40)
    monkeypatch.setattr(subject, "_ref_value", lambda *_args: branch_commit)

    with pytest.raises(GitProposalError, match="proposal branch"):
        applier._execute(
            proposal, ValidatedPatch(b"patch", ("README.md",)), create_branch=create_branch
        )


def test_quarantined_admin_identity_mismatch_is_restored(tmp_path: Path) -> None:
    admin = tmp_path / "worktree-admin"
    admin.mkdir()
    sentinel = admin / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(GitProposalError, match="worktree metadata changed"):
        subject._quarantine_worktree_admin(admin, (0, 0, 0, 0))

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not tuple(tmp_path.glob(".proposal-cleanup-*"))


def test_remove_private_directory_removes_only_service_owned_directory(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    worktree = runtime / "proposal-worktree-test"
    worktree.mkdir()
    (worktree / "sentinel").write_text("remove\n", encoding="utf-8")

    subject._remove_private_directory(worktree, runtime)

    assert not worktree.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(GitProposalError, match="cleanup rejected"):
        subject._remove_private_directory(outside, runtime)
    assert outside.is_dir()


def test_cleanup_preserves_first_registry_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    worktree = runtime / "proposal-worktree-test"
    monkeypatch.setattr(
        subject,
        "_registered_worktree",
        lambda *_args: (_ for _ in ()).throw(GitProposalError("registry changed")),
    )
    monkeypatch.setattr(subject, "_remove_private_directory", lambda *_args: None)

    with pytest.raises(GitProposalError, match="private worktree cleanup failed"):
        subject._cleanup_created_worktree(
            tmp_path / "repository",
            worktree,
            runtime,
            known_registered=False,
        )


def test_quarantine_identity_race_restores_original_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worktree = tmp_path / "proposal-worktree-race"
    worktree.mkdir()
    sentinel = worktree / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    identities = iter(
        (
            (1, 1, stat.S_IFDIR, os.getuid()),
            (1, 2, stat.S_IFDIR, os.getuid()),
            (1, 1, stat.S_IFDIR, os.getuid()),
        )
    )
    monkeypatch.setattr(subject, "_directory_identity", lambda _metadata: next(identities))

    with pytest.raises(GitProposalError, match="cleanup rejected"):
        subject._quarantine_private_directory(worktree)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert not tuple(tmp_path.glob(".proposal-cleanup-*"))


def test_remove_quarantine_fails_closed_without_symlink_safe_rmtree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quarantine = tmp_path / "quarantine"
    quarantine.mkdir()
    metadata = quarantine.stat()
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    monkeypatch.setattr(subject.shutil.rmtree, "avoids_symlink_attacks", False)
    try:
        with pytest.raises(GitProposalError, match="cleanup rejected"):
            subject._remove_quarantined_directory(
                parent_fd,
                quarantine.name,
                subject._directory_identity(metadata),
            )
    finally:
        os.close(parent_fd)

    assert quarantine.is_dir()


def test_remove_entry_at_handles_a_directory_without_following_links(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    (directory / "sentinel").write_text("remove\n", encoding="utf-8")
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        subject._remove_entry_at(parent_fd, directory.name)
    finally:
        os.close(parent_fd)

    assert not directory.exists()


def test_clear_sandbox_environment_rejects_a_swapped_symlink(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    (worktree / ".proposal-home").symlink_to(outside, target_is_directory=True)

    with pytest.raises(GitProposalError, match="cleanup rejected"):
        subject._clear_sandbox_environment(worktree)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_sandbox_builders_reject_empty_relative_and_control_character_inputs() -> None:
    with pytest.raises(GitProposalError, match="sandbox boundary rejected"):
        subject._sandbox_command((), (Path("/tmp"),))
    with pytest.raises(GitProposalError, match="sandbox boundary rejected"):
        subject._sandbox_command(("/usr/bin/true",), ())
    with pytest.raises(GitProposalError, match="sandbox boundary rejected"):
        subject._sandbox_profile((Path("relative"),))
    with pytest.raises(GitProposalError, match="sandbox boundary rejected"):
        subject._seatbelt_text("bad\npath")


@pytest.mark.parametrize("input_bytes", ("not-bytes", b"x" * (1024 * 1024 + 1)))
def test_bounded_process_rejects_invalid_or_oversized_input(
    tmp_path: Path,
    input_bytes: object,
) -> None:
    with pytest.raises(GitProposalError, match="process input rejected"):
        subject._run_bounded(
            ("/usr/bin/true",),
            cwd=tmp_path,
            timeout=1,
            max_output_bytes=1_024,
            input_bytes=input_bytes,  # type: ignore[arg-type]
        )


def test_bounded_process_reports_launch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_launch(*_args: object, **_kwargs: object) -> object:
        raise OSError("launch denied")

    monkeypatch.setattr(subject.subprocess, "Popen", fail_launch)

    with pytest.raises(GitProposalError, match="process launch failed"):
        subject._run_bounded(
            ("/missing",),
            cwd=tmp_path,
            timeout=1,
            max_output_bytes=1_024,
        )


def test_bounded_process_closes_empty_input_and_returns_success(tmp_path: Path) -> None:
    returncode, output = subject._run_bounded(
        (sys.executable, "-c", "pass"),
        cwd=tmp_path,
        timeout=2,
        max_output_bytes=1_024,
        input_bytes=b"",
    )

    assert returncode == 0
    assert output == ""
