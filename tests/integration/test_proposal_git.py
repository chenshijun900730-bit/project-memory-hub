from __future__ import annotations

import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

from project_memory_hub.improvement.git_apply import (
    GitProposalApplier,
    GitProposalError,
)
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.improvement.service import (
    ProposalApplyBusy,
    ProposalService,
)
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.proposals import ProposalRepository


PATCH = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-seed
+updated
"""
RESOLVED_PYTHON = str(Path(sys.executable).resolve())


def _git(root: Path, *arguments: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=check,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=8,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path, name: str = "repository") -> Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    return root


def _database(tmp_path: Path) -> tuple[ProposalRepository, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    return ProposalRepository(database), runtime


def _script(tmp_path: Path, body: str, name: str = "verify") -> Path:
    path = tmp_path / name
    path.write_text("#!/bin/sh\nset -eu\n" + body + "\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def _approved(
    proposals: ProposalRepository,
    verification_argv: tuple[str, ...],
    *,
    patch: str = PATCH,
) -> object:
    created = proposals.create(
        ProposalDraft(
            signature="local.git.safe-change.v1",
            title="Apply a reviewed safe change",
            description="Change the synthetic README after local approval.",
            risk="low",
            patch=patch,
            verification_argv=verification_argv,
            target_version=None,
            origin="local_cli",
        )
    )
    return proposals.approve(created.record.proposal_id, actor="local_test")


def _service(
    proposals: ProposalRepository,
    root: Path,
    runtime: Path,
    verification_argv: tuple[str, ...],
    *,
    timeout_seconds: float = 4,
    max_output_bytes: int = 4096,
) -> ProposalService:
    applier = GitProposalApplier(
        root,
        runtime,
        allowed_verification_argv=(verification_argv,),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    return ProposalService(
        proposals,
        applier,
        ProcessLock(runtime / "proposal-apply.lock"),
    )


def _snapshot(root: Path) -> tuple[str, str, str, bytes, str]:
    return (
        _git(root, "symbolic-ref", "--short", "HEAD", check=False),
        _git(root, "rev-parse", "HEAD"),
        _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        (root / "README.md").read_bytes(),
        _git(root, "for-each-ref", "--format=%(refname):%(objectname)"),
    )


def _filesystem_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        metadata = path.lstat()
        relative = str(path.relative_to(root))
        if path.is_symlink():
            payload: object = ("symlink", os.readlink(path))
        elif path.is_file():
            payload = ("file", path.read_bytes())
        else:
            payload = ("directory", None)
        entries.append(
            (
                relative,
                stat.S_IMODE(metadata.st_mode),
                metadata.st_mtime_ns,
                payload,
            )
        )
    return tuple(entries)


def _mutable_user_state(root: Path) -> tuple[str, str, str, str, bytes | None]:
    target = root / "README.md"
    if target.is_symlink():
        kind = "symlink"
        content: bytes | None = os.readlink(target).encode()
    elif target.exists():
        kind = "file"
        content = target.read_bytes()
    else:
        kind = "missing"
        content = None
    return (
        _git(root, "symbolic-ref", "--short", "HEAD", check=False),
        _git(root, "rev-parse", "HEAD"),
        _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        kind,
        content,
    )


def test_bounded_process_input_cannot_block_past_timeout(tmp_path: Path) -> None:
    import project_memory_hub.improvement.git_apply as subject

    started = time.monotonic()
    with pytest.raises(GitProposalError, match="boundary exceeded"):
        subject._run_bounded(
            (
                sys.executable,
                "-c",
                "import time; time.sleep(10)",
            ),
            cwd=tmp_path,
            timeout=0.1,
            max_output_bytes=4_096,
            input_bytes=b"x" * (1024 * 1024),
        )

    assert time.monotonic() - started < 2


def test_apply_commits_once_in_private_worktree_without_touching_user_tree(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(
        tmp_path,
        f'test "$PWD" != "{root}"; test "$(stat -f %Lp .)" = "700"',
    )
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    before = _snapshot(root)

    result = _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert _snapshot(root)[:4] == before[:4]
    assert result.original_branch == "main"
    assert result.base_commit == before[1]
    assert result.proposal_branch == (f"codex/memory-hub-proposal-{approved.proposal_id.hex}")
    assert _git(root, "rev-parse", result.proposal_branch) == result.applied_commit
    assert _git(root, "show", "-s", "--format=%s", result.applied_commit) == (
        f"chore(improvement): apply proposal {approved.proposal_id}"
    )
    assert _git(root, "show", f"{result.applied_commit}:README.md") == "updated"
    assert _git(root, "rev-list", "--count", f"{before[1]}..{result.applied_commit}") == "1"
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert stat.S_IMODE(runtime.stat().st_mode) == 0o700
    assert proposals.get(approved.proposal_id).status == "applied"


def test_apply_supports_configured_root_that_is_a_linked_worktree(
    tmp_path: Path,
) -> None:
    primary = _repository(tmp_path, "primary")
    linked = tmp_path / "linked"
    _git(primary, "worktree", "add", "-b", "linked-branch", str(linked))
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    primary_before = _snapshot(primary)
    linked_before = _snapshot(linked)

    result = _service(proposals, linked, runtime, argv).apply(approved.proposal_id)

    assert (linked / ".git").is_file()
    assert result.original_branch == "linked-branch"
    assert _snapshot(primary)[:4] == primary_before[:4]
    assert _snapshot(linked)[:4] == linked_before[:4]


@pytest.mark.parametrize("state", ("dirty", "detached", "branch_collision"))
def test_preflight_rejects_unsafe_repository_without_transition_or_mutation(
    tmp_path: Path, state: str
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    branch = f"codex/memory-hub-proposal-{approved.proposal_id.hex}"
    if state == "dirty":
        (root / "README.md").write_text("dirty\n", encoding="utf-8")
    elif state == "detached":
        _git(root, "switch", "--detach")
    else:
        _git(root, "branch", branch)
    before = _snapshot(root)

    with pytest.raises(GitProposalError):
        _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "approved"
    assert _snapshot(root) == before
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize("ancestor_kind", ("symlink", "gitlink"))
def test_preflight_rejects_nested_special_ancestor_hidden_by_skip_worktree(
    tmp_path: Path, ancestor_kind: str
) -> None:
    root = _repository(tmp_path)
    blocked = root / "outer" / "blocked"
    blocked.parent.mkdir(parents=True)
    if ancestor_kind == "symlink":
        blocked.symlink_to("../target")
        _git(root, "add", "outer/blocked")
    else:
        blocked.mkdir()
        commit = _git(root, "rev-parse", "HEAD")
        _git(
            root,
            "update-index",
            "--add",
            "--cacheinfo",
            f"160000,{commit},outer/blocked",
        )
    _git(root, "commit", "-m", f"add nested {ancestor_kind}")
    _git(root, "update-index", "--skip-worktree", "--", "outer/blocked")
    if ancestor_kind == "symlink":
        blocked.unlink()
    else:
        blocked.rmdir()
    blocked.parent.rmdir()
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""

    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    patch = """\
diff --git a/outer/blocked/new.txt b/outer/blocked/new.txt
new file mode 100644
--- /dev/null
+++ b/outer/blocked/new.txt
@@ -0,0 +1 @@
+new
"""
    approved = _approved(proposals, argv, patch=patch)
    before = _snapshot(root)

    with pytest.raises(GitProposalError):
        _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "approved"
    assert _snapshot(root) == before
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


@pytest.mark.parametrize("boundary", ("sandbox", "unsupported_hook"))
def test_preflight_rejects_unavailable_execution_boundary_before_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import project_memory_hub.improvement.git_apply as subject

    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    if boundary == "unsupported_hook":
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o700)
    else:
        real_run = subject._run_bounded

        def reject_sandbox(command, **kwargs):
            if tuple(command)[0] == "/usr/bin/sandbox-exec":
                raise GitProposalError("sandbox unavailable")
            return real_run(command, **kwargs)

        monkeypatch.setattr(subject, "_run_bounded", reject_sandbox)
    before = _snapshot(root)

    with pytest.raises(GitProposalError):
        _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "approved"
    assert _snapshot(root) == before
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_lock_contention_leaves_approved_proposal_and_refs_unchanged(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    lock = ProcessLock(runtime / "proposal-apply.lock")
    service = ProposalService(
        proposals,
        GitProposalApplier(root, runtime, allowed_verification_argv=(argv,)),
        ProcessLock(runtime / "proposal-apply.lock"),
    )
    before = _snapshot(root)

    with lock.acquire() as outcome:
        assert outcome.acquired is True
        with pytest.raises(ProposalApplyBusy):
            service.apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "approved"
    assert _snapshot(root) == before


@pytest.mark.parametrize("failure", ("exit", "side_effect", "untracked", "overflow", "timeout"))
def test_verification_failure_is_bounded_and_never_changes_user_tree(
    tmp_path: Path, failure: str
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    delayed_marker = tmp_path / "delayed-marker"
    bodies = {
        "exit": "echo PRIVATE_VERIFY_TEXT >&2; exit 7",
        "side_effect": "printf 'tampered\\n' > README.md",
        "untracked": "printf 'unexpected\\n' > verification-artifact.txt",
        "overflow": f'"{sys.executable}" -c "print(\'x\'*8192)"',
        "timeout": f'(sleep 1; echo late > "{delayed_marker}") & wait',
    }
    verifier = _script(tmp_path, bodies[failure])
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    before = _snapshot(root)
    service = _service(
        proposals,
        root,
        runtime,
        argv,
        timeout_seconds=0.1 if failure == "timeout" else 2,
        max_output_bytes=128 if failure == "overflow" else 4096,
    )

    with pytest.raises(GitProposalError) as captured:
        service.apply(approved.proposal_id)

    assert "PRIVATE_VERIFY_TEXT" not in str(captured.value)
    assert proposals.get(approved.proposal_id).status == "failed"
    assert _snapshot(root)[:4] == before[:4]
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
    if failure == "timeout":
        time.sleep(1.2)
        assert not delayed_marker.exists()


def test_verification_uses_fixed_process_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import project_memory_hub.improvement.git_apply as subject

    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(
        tmp_path,
        'test "${PRIVATE_CALLER_TOKEN-unset}" = "unset"',
    )
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    calls: list[tuple[object, dict[str, object]]] = []
    real_popen = subprocess.Popen

    def recording_popen(command, **kwargs):
        calls.append((command, kwargs))
        return real_popen(command, **kwargs)

    monkeypatch.setenv("PRIVATE_CALLER_TOKEN", "must-not-cross")
    monkeypatch.setattr(subject.subprocess, "Popen", recording_popen)

    _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    verification = next(call for call in calls if tuple(call[0])[-len(argv) :] == argv)
    kwargs = verification[1]
    assert tuple(verification[0])[0] == "/usr/bin/sandbox-exec"
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["start_new_session"] is (os.name == "posix")
    assert Path(kwargs["cwd"]) != root
    assert "PRIVATE_CALLER_TOKEN" not in kwargs["env"]
    assert all(tuple(command)[0] != "git" for command, _kwargs in calls)


def test_exact_full_verification_argv_is_required_before_git_mutation(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    approved = _approved(proposals, (str(verifier), "unexpected"))
    allowed = (str(verifier),)
    before = _snapshot(root)

    with pytest.raises(GitProposalError):
        _service(proposals, root, runtime, allowed).apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "approved"
    assert _snapshot(root) == before


@pytest.mark.parametrize(
    "command",
    (
        ("relative-verifier",),
        (RESOLVED_PYTHON, "-c", "print('unsafe')"),
        (RESOLVED_PYTHON, "-cprint('unsafe')"),
        ("/bin/sh", "-c", "exit 0"),
        ("/bin/sh", "-ec", "exit 0"),
        ("/usr/bin/env", RESOLVED_PYTHON, "-c", "print('unsafe')"),
    ),
)
def test_verification_allowlist_rejects_relative_or_interpreter_code(
    tmp_path: Path, command: tuple[str, ...]
) -> None:
    root = _repository(tmp_path)
    _proposals, runtime = _database(tmp_path)

    with pytest.raises(ValueError):
        GitProposalApplier(
            root,
            runtime,
            allowed_verification_argv=(command,),
        )


def test_verification_executable_cannot_change_after_allowlist_configuration(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    verifier.write_text("#!/bin/sh\nset -eu\nexit 0\n", encoding="utf-8")
    verifier.chmod(0o700)

    with pytest.raises(GitProposalError):
        service.apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "approved"


@pytest.mark.parametrize("branch_state", ("missing", "base"))
def test_apply_recovery_fails_closed_without_an_exact_commit(
    tmp_path: Path, branch_state: str
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    base = _git(root, "rev-parse", "HEAD")
    branch = f"codex/memory-hub-proposal-{approved.proposal_id.hex}"
    attempt = uuid4()
    proposals.begin_apply(
        approved.proposal_id,
        apply_attempt_id=attempt,
        repository_root=root,
        original_branch="main",
        base_commit=base,
        proposal_branch=branch,
    )
    if branch_state == "base":
        _git(root, "branch", branch, base)

    with pytest.raises(GitProposalError):
        _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    recovered = proposals.get(approved.proposal_id)
    assert recovered.status == "failed"
    assert recovered.apply_attempt_id == attempt
    if branch_state == "missing":
        assert _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False) == ""
    else:
        assert _git(root, "rev-parse", branch) == base


def test_git_commit_db_crash_recovers_exact_commit_without_reapplying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    real_mark_applied = proposals.mark_applied
    attempts = 0

    def crash_once(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("PRIVATE_SQLITE_CRASH")
        return real_mark_applied(*args, **kwargs)

    monkeypatch.setattr(proposals, "mark_applied", crash_once)
    with pytest.raises(RuntimeError, match="PRIVATE_SQLITE_CRASH"):
        service.apply(approved.proposal_id)
    applying = proposals.get(approved.proposal_id)
    first_commit = _git(root, "rev-parse", applying.proposal_branch or "")
    repository_before = _filesystem_snapshot(root)
    runtime_before = _filesystem_snapshot(runtime)

    preview = service.preview_action(applying.proposal_id, action="apply")

    assert preview.mode == "recovery"
    assert preview.complete is False
    assert "commit_tree" in preview.unverified
    assert _filesystem_snapshot(root) == repository_before
    assert _filesystem_snapshot(runtime) == runtime_before

    recovered = service.apply(approved.proposal_id)

    assert recovered.applied_commit == first_commit
    assert _git(root, "rev-list", "--count", f"{applying.base_commit}..{first_commit}") == "1"
    assert proposals.get(approved.proposal_id).status == "applied"


@pytest.mark.parametrize("drift", ("dirty", "original_ahead", "wrong_branch"))
def test_exact_commit_recovery_ignores_later_original_worktree_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    real_mark_applied = proposals.mark_applied

    monkeypatch.setattr(
        proposals,
        "mark_applied",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db crash")),
    )
    with pytest.raises(RuntimeError, match="db crash"):
        service.apply(approved.proposal_id)
    applying = proposals.get(approved.proposal_id)
    first_commit = _git(root, "rev-parse", applying.proposal_branch or "")
    monkeypatch.setattr(proposals, "mark_applied", real_mark_applied)

    if drift == "dirty":
        (root / "README.md").write_text("later user edit\n", encoding="utf-8")
    elif drift == "original_ahead":
        (root / "later.txt").write_text("later\n", encoding="utf-8")
        _git(root, "add", "later.txt")
        _git(root, "commit", "-m", "later user commit")
    else:
        _git(root, "switch", "-c", "other-user-branch")
    user_before_recovery = _snapshot(root)

    recovered = service.apply(approved.proposal_id)

    assert recovered.applied_commit == first_commit
    assert proposals.get(approved.proposal_id).status == "applied"
    assert _snapshot(root) == user_before_recovery


@pytest.mark.parametrize("drift", ("target_deleted", "target_symlink", "new_hook"))
def test_exact_commit_recovery_uses_only_recorded_git_objects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    real_mark_applied = proposals.mark_applied
    monkeypatch.setattr(
        proposals,
        "mark_applied",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db crash")),
    )
    with pytest.raises(RuntimeError, match="db crash"):
        service.apply(approved.proposal_id)
    applying = proposals.get(approved.proposal_id)
    exact_commit = _git(root, "rev-parse", applying.proposal_branch or "")
    monkeypatch.setattr(proposals, "mark_applied", real_mark_applied)

    if drift == "target_deleted":
        (root / "README.md").unlink()
    elif drift == "target_symlink":
        (root / "README.md").unlink()
        (root / "README.md").symlink_to(tmp_path / "outside-target")
    else:
        hook = root / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        hook.chmod(0o700)
    before = _mutable_user_state(root)

    recovered = service.apply(approved.proposal_id)

    assert recovered.applied_commit == exact_commit
    assert recovered.verification_summary.endswith("recovery-unknown")
    assert proposals.get(approved.proposal_id).status == "applied"
    assert _mutable_user_state(root) == before


@pytest.mark.parametrize("drift", ("missing", "base"))
def test_final_db_crash_never_reapplies_after_proposal_ref_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, drift: str
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)

    monkeypatch.setattr(
        proposals,
        "mark_applied",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db crash")),
    )
    with pytest.raises(RuntimeError, match="db crash"):
        service.apply(approved.proposal_id)
    applying = proposals.get(approved.proposal_id)
    branch = applying.proposal_branch or ""
    first_commit = _git(root, "rev-parse", branch)
    if drift == "missing":
        _git(root, "branch", "-D", branch)
    else:
        _git(root, "update-ref", f"refs/heads/{branch}", applying.base_commit or "")
    monkeypatch.undo()

    with pytest.raises(GitProposalError):
        service.apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "failed"
    assert _git(root, "cat-file", "-t", first_commit) == "commit"
    if drift == "missing":
        assert _git(root, "show-ref", "--verify", f"refs/heads/{branch}", check=False) == ""
    else:
        assert _git(root, "rev-parse", branch) == applying.base_commit


def test_recovery_rejects_merge_commit_even_with_expected_tree_and_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)

    def crash_final_write(*_args, **_kwargs):
        raise RuntimeError("synthetic final database crash")

    monkeypatch.setattr(proposals, "mark_applied", crash_final_write)
    with pytest.raises(RuntimeError, match="synthetic final database crash"):
        service.apply(approved.proposal_id)
    applying = proposals.get(approved.proposal_id)
    branch = applying.proposal_branch or ""
    proposal_commit = _git(root, "rev-parse", branch)
    tree = _git(root, "rev-parse", f"{proposal_commit}^{{tree}}")
    other = _git(root, "commit-tree", tree, "-p", applying.base_commit or "", "-m", "other")
    merge = _git(
        root,
        "commit-tree",
        tree,
        "-p",
        applying.base_commit or "",
        "-p",
        other,
        "-m",
        f"chore(improvement): apply proposal {approved.proposal_id}",
    )
    _git(root, "update-ref", f"refs/heads/{branch}", merge, proposal_commit)
    monkeypatch.undo()

    with pytest.raises(GitProposalError):
        service.apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "failed"


def test_post_commit_cleanup_failure_remains_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import project_memory_hub.improvement.git_apply as subject

    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    real_cleanup = subject._git_cleanup

    def cleanup_then_fail(repository: Path, worktree: Path) -> None:
        real_cleanup(repository, worktree)
        raise GitProposalError("synthetic cleanup failure")

    monkeypatch.setattr(subject, "_git_cleanup", cleanup_then_fail)
    with pytest.raises(GitProposalError):
        service.apply(approved.proposal_id)
    applying = proposals.get(approved.proposal_id)
    first_commit = _git(root, "rev-parse", applying.proposal_branch or "")
    assert applying.status == "applying"

    with pytest.raises(GitProposalError):
        service.apply(approved.proposal_id)
    assert proposals.get(approved.proposal_id).status == "applying"

    monkeypatch.setattr(subject, "_git_cleanup", real_cleanup)
    recovered = service.apply(approved.proposal_id)

    assert recovered.applied_commit == first_commit
    assert proposals.get(approved.proposal_id).status == "applied"


def test_verifier_cannot_write_the_original_user_worktree(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, f'printf "tampered\\n" > "{root / "README.md"}"')
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    before = _snapshot(root)

    with pytest.raises(GitProposalError):
        _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "failed"
    assert _snapshot(root)[:4] == before[:4]
    branch = f"codex/memory-hub-proposal-{approved.proposal_id.hex}"
    assert _git(root, "rev-parse", branch) == before[1]


def test_verifier_cannot_read_outside_private_worktree(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    secret = tmp_path / "outside-secret"
    secret.write_text("PRIVATE\n", encoding="utf-8")
    verifier = _script(
        tmp_path,
        f'if IFS= read -r private_value < "{secret}"; then exit 91; fi\nexit 0',
        name="verify-private-read",
    )
    argv = (str(verifier),)
    approved = _approved(proposals, argv)

    result = _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert result.applied_commit
    assert proposals.get(approved.proposal_id).status == "applied"


@pytest.mark.parametrize("hook_name", ("post-checkout", "post-commit"))
def test_repository_hook_is_preserved_and_deferred_without_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hook_name: str
) -> None:
    import project_memory_hub.improvement.git_apply as subject

    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    hook = root / ".git" / "hooks" / hook_name
    hook.write_text(
        "#!/bin/sh\n"
        "echo HOOK_EXECUTED\n"
        f'printf "escaped\\n" > "{tmp_path / "detached-hook-child"}"\n'
        f"git update-ref refs/heads/hook-escape HEAD 2>/dev/null || true\n"
        f'printf "# hook tamper\\n" >> "{root / ".git" / "config"}" 2>/dev/null || true\n'
        f'printf "corrupt" > "{root / ".git" / "index"}" 2>/dev/null || true\n'
        f'printf "tampered\\n" > "{root / "README.md"}" 2>/dev/null || true\n',
        encoding="utf-8",
    )
    hook.chmod(0o700)
    hook_before = hook.read_bytes()
    hook_mode_before = hook.stat().st_mode
    config_before = (root / ".git" / "config").read_bytes()
    index_before = (root / ".git" / "index").read_bytes()
    user_before = _snapshot(root)
    outputs: list[str] = []
    real_run = subject._run_bounded

    def recording_run(*args, **kwargs):
        result = real_run(*args, **kwargs)
        outputs.append(result[1])
        return result

    monkeypatch.setattr(subject, "_run_bounded", recording_run)

    result = _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert result.applied_commit
    assert "hooks deferred" in result.verification_summary
    assert not any("HOOK_EXECUTED" in output for output in outputs)
    assert not (tmp_path / "detached-hook-child").exists()
    assert hook.read_bytes() == hook_before
    assert hook.stat().st_mode == hook_mode_before
    assert (root / ".git" / "config").read_bytes() == config_before
    assert (root / ".git" / "index").read_bytes() == index_before
    assert _snapshot(root)[:4] == user_before[:4]
    assert _git(root, "show-ref", "--verify", "refs/heads/hook-escape", check=False) == ""


@pytest.mark.parametrize("hook_name", ("post-checkout", "post-commit"))
def test_repository_hook_cannot_read_outside_private_worktree(
    tmp_path: Path, hook_name: str
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    secret = tmp_path / "outside-hook-secret"
    secret.write_text("PRIVATE\n", encoding="utf-8")
    hook = root / ".git" / "hooks" / hook_name
    hook.write_text(
        "#!/bin/sh\n"
        f'if IFS= read -r private_value < "{secret}"; then\n'
        "  i=0\n"
        '  while [ "$i" -lt 70000 ]; do\n'
        "    printf x\n"
        "    i=$((i + 1))\n"
        "  done\n"
        "fi\n"
        "echo HOOK_READ_DENIED\n",
        encoding="utf-8",
    )
    hook.chmod(0o700)

    result = _service(proposals, root, runtime, argv).apply(approved.proposal_id)

    assert result.applied_commit
    assert proposals.get(approved.proposal_id).status == "applied"


def test_cleanup_never_follows_a_swapped_worktree_symlink(tmp_path: Path) -> None:
    import project_memory_hub.improvement.git_apply as subject

    root = _repository(tmp_path)
    worktree = tmp_path / "registered-worktree"
    original = tmp_path / "moved-worktree"
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    _git(root, "worktree", "add", "-b", "cleanup-test", str(worktree))
    worktree.rename(original)
    (victim / ".git").write_bytes((original / ".git").read_bytes())
    worktree.symlink_to(victim, target_is_directory=True)

    with pytest.raises(GitProposalError):
        subject._git_cleanup(root, worktree)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_cleanup_rejects_symlink_swap_between_check_and_git_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import project_memory_hub.improvement.git_apply as subject

    root = _repository(tmp_path)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    worktree = runtime / "proposal-worktree-race"
    victim = tmp_path / "victim"
    victim.mkdir()
    sentinel = victim / "sentinel"
    sentinel.write_text("keep\n", encoding="utf-8")
    _git(root, "worktree", "add", "-b", "cleanup-race", str(worktree))
    (victim / ".git").write_bytes((worktree / ".git").read_bytes())
    real_unregister = subject._quarantine_worktree_admin
    swapped = False

    def swap_after_lstat(*args, **kwargs):
        nonlocal swapped
        swapped = True
        worktree.symlink_to(victim, target_is_directory=True)
        return real_unregister(*args, **kwargs)

    monkeypatch.setattr(subject, "_quarantine_worktree_admin", swap_after_lstat)

    with pytest.raises(GitProposalError):
        subject._git_cleanup(root, worktree)

    assert swapped is True
    assert sentinel.read_text(encoding="utf-8") == "keep\n"
    assert _git(root, "rev-parse", "cleanup-race")
    assert str(worktree) in _git(root, "worktree", "list", "--porcelain")


def test_rollback_only_marks_state_after_exact_ref_verification(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    service.apply(approved.proposal_id)
    before = _snapshot(root)

    rolled_back = service.rollback(approved.proposal_id)

    assert rolled_back.status == "rolled_back"
    assert _snapshot(root) == before


def test_apply_and_rollback_previews_validate_exact_state_without_any_write(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)

    repository_before = _filesystem_snapshot(root)
    runtime_before = _filesystem_snapshot(runtime)
    service.preview_action(approved.proposal_id, action="apply")

    assert proposals.get(approved.proposal_id).status == "approved"
    assert _filesystem_snapshot(root) == repository_before
    assert _filesystem_snapshot(runtime) == runtime_before

    service.apply(approved.proposal_id)
    applied = proposals.get(approved.proposal_id)
    repository_before = _filesystem_snapshot(root)
    runtime_before = _filesystem_snapshot(runtime)
    service.preview_action(applied.proposal_id, action="rollback")

    assert proposals.get(applied.proposal_id).status == "applied"
    assert _filesystem_snapshot(root) == repository_before
    assert _filesystem_snapshot(runtime) == runtime_before

    _git(root, "branch", "-f", applied.proposal_branch or "", applied.base_commit or "")
    drifted_repository = _filesystem_snapshot(root)
    drifted_runtime = _filesystem_snapshot(runtime)
    with pytest.raises(GitProposalError):
        service.preview_action(applied.proposal_id, action="rollback")
    assert _filesystem_snapshot(root) == drifted_repository
    assert _filesystem_snapshot(runtime) == drifted_runtime


def test_rollback_preview_rejects_ref_and_database_that_agree_on_wrong_commit(
    tmp_path: Path,
) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    service.apply(approved.proposal_id)
    applied = proposals.get(approved.proposal_id)
    branch = applied.proposal_branch or ""
    base = applied.base_commit or ""
    tree = _git(root, "rev-parse", f"{base}^{{tree}}")
    wrong = _git(root, "commit-tree", tree, "-p", base, "-m", "wrong metadata")
    _git(
        root,
        "update-ref",
        f"refs/heads/{branch}",
        wrong,
        applied.applied_commit or "",
    )
    with Database(runtime / "memory.db").transaction() as connection:
        connection.execute(
            "update improvement_proposals set applied_commit = ? where proposal_id = ?",
            (wrong, str(applied.proposal_id)),
        )
    repository_before = _filesystem_snapshot(root)
    runtime_before = _filesystem_snapshot(runtime)

    with pytest.raises(GitProposalError):
        service.preview_action(applied.proposal_id, action="rollback")

    assert _filesystem_snapshot(root) == repository_before
    assert _filesystem_snapshot(runtime) == runtime_before


@pytest.mark.parametrize("drift", ("dirty", "original_ahead", "wrong_branch"))
def test_rollback_ref_or_worktree_drift_fails_closed(tmp_path: Path, drift: str) -> None:
    root = _repository(tmp_path)
    proposals, runtime = _database(tmp_path)
    verifier = _script(tmp_path, ":")
    argv = (str(verifier),)
    approved = _approved(proposals, argv)
    service = _service(proposals, root, runtime, argv)
    service.apply(approved.proposal_id)
    if drift == "dirty":
        (root / "README.md").write_text("dirty\n", encoding="utf-8")
    elif drift == "original_ahead":
        (root / "later.txt").write_text("later\n", encoding="utf-8")
        _git(root, "add", "later.txt")
        _git(root, "commit", "-m", "later user commit")
    else:
        _git(root, "switch", "-c", "other-user-branch")
    before = _snapshot(root)

    with pytest.raises(GitProposalError):
        service.rollback(approved.proposal_id)

    assert proposals.get(approved.proposal_id).status == "applied"
    assert _snapshot(root) == before
