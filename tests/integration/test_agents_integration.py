from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

import project_memory_hub.integration.agents as agents_module
from project_memory_hub.integration.agents import (
    MANAGED_END,
    MANAGED_START,
    AgentsIntegration,
    AgentsIntegrationError,
    AgentsStatus,
)


def _launcher(root: Path, name: str = "memory-hub") -> Path:
    directory = root / f"bin-{name}"
    directory.mkdir(parents=True)
    launcher = directory / "memory-hub"
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    return launcher


def _filesystem_state(root: Path) -> tuple[tuple[object, ...], ...]:
    state: list[tuple[object, ...]] = []
    for path in sorted((root, *root.rglob("*")), key=lambda item: str(item)):
        metadata = path.lstat()
        relative = "." if path == root else str(path.relative_to(root))
        content = path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None
        state.append(
            (
                relative,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_uid,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                content,
            )
        )
    return tuple(state)


def test_inspect_classifies_missing_current_and_drifted_without_writes(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    integration = AgentsIntegration(launcher)
    before_missing = _filesystem_state(tmp_path)

    missing_target = integration.inspect(target)

    assert missing_target == AgentsStatus(status="missing")
    assert _filesystem_state(tmp_path) == before_missing
    assert not target.exists()

    missing_parent_target = tmp_path / "missing-parent" / "AGENTS.md"
    before_missing_parent = _filesystem_state(tmp_path)
    assert integration.inspect(missing_parent_target).status == "missing"
    assert _filesystem_state(tmp_path) == before_missing_parent
    assert not missing_parent_target.parent.exists()

    target.write_bytes(b"unrelated user rule without a managed block")
    before_unmanaged = _filesystem_state(tmp_path)
    assert integration.inspect(target).status == "missing"
    assert _filesystem_state(tmp_path) == before_unmanaged

    integration.install(target, dry_run=False)
    before_current = _filesystem_state(tmp_path)
    assert integration.inspect(target).status == "current"
    assert _filesystem_state(tmp_path) == before_current

    target.write_bytes(
        target.read_bytes().replace(
            b"## Project Memory Hub managed workflow",
            b"## Older Project Memory Hub managed workflow",
        )
    )
    before_drifted = _filesystem_state(tmp_path)
    assert integration.inspect(target).status == "drifted"
    assert _filesystem_state(tmp_path) == before_drifted


def test_inspect_returns_malformed_for_unsafe_target_markers_or_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher(tmp_path)
    integration = AgentsIntegration(launcher)
    target = tmp_path / "AGENTS.md"
    duplicate = (
        MANAGED_START.encode()
        + b"\none\n"
        + MANAGED_END.encode()
        + b"\n"
        + MANAGED_START.encode()
        + b"\ntwo\n"
        + MANAGED_END.encode()
        + b"\n"
    )
    target.write_bytes(duplicate)
    before_duplicate = _filesystem_state(tmp_path)

    assert integration.inspect(target).status == "malformed"
    assert _filesystem_state(tmp_path) == before_duplicate

    target.unlink()
    victim = tmp_path / "victim-agents"
    victim.write_bytes(b"victim")
    target.symlink_to(victim)
    before_symlink = _filesystem_state(tmp_path)
    assert integration.inspect(target).status == "malformed"
    assert _filesystem_state(tmp_path) == before_symlink

    target.unlink()
    target.write_bytes(b"read failure")

    def reject_read(*_args, **_kwargs):
        raise OSError("private read failure")

    monkeypatch.setattr(agents_module, "_read_target", reject_read)
    before_failure = _filesystem_state(tmp_path)
    assert integration.inspect(target).status == "malformed"
    assert _filesystem_state(tmp_path) == before_failure


def test_inspect_reports_malformed_if_the_pinned_launcher_identity_changes(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"user rules")
    integration = AgentsIntegration(launcher)
    before = _filesystem_state(tmp_path)
    launcher.write_bytes(b"#!/bin/sh\nexit 7\n")
    launcher.chmod(0o700)
    changed = _filesystem_state(tmp_path)

    assert before != changed
    assert integration.inspect(target).status == "malformed"
    assert _filesystem_state(tmp_path) == changed


def test_install_is_byte_preserving_idempotent_and_remove_round_trips(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    original = b"\xff# Existing user rules\r\nprivate-rule-without-final-newline"
    target.write_bytes(original)
    target.chmod(0o640)
    integration = AgentsIntegration(launcher)

    installed = integration.install(target, dry_run=False)

    installed_document = target.read_bytes()
    assert installed.changed is True
    assert installed.backup_path is not None
    assert installed.backup_path.read_bytes() == original
    assert stat.S_IMODE(installed.backup_path.stat().st_mode) == 0o600
    assert installed_document.startswith(original)
    assert MANAGED_START.encode() in installed_document
    assert MANAGED_END.encode() in installed_document
    assert str(launcher).encode() in installed_document
    assert stat.S_IMODE(target.stat().st_mode) == 0o640

    repeated = integration.install(target, dry_run=False)

    assert repeated.changed is False
    assert repeated.diff == ""
    assert repeated.backup_path is None
    assert target.read_bytes() == installed_document

    removed = integration.remove(target, dry_run=False)

    assert removed.changed is True
    assert removed.backup_path is None
    assert target.read_bytes() == original
    assert installed.backup_path.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert integration.remove(target, dry_run=False).changed is False


def test_dry_run_returns_only_a_sanitized_structural_diff_and_writes_nothing(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    private_rule = b"Never reveal /Users/private-user/secret-project\n"
    target.write_bytes(private_rule)
    before = target.stat()
    integration = AgentsIntegration(launcher)

    preview = integration.install(target, dry_run=True)

    assert preview.changed is True
    assert preview.backup_path is None
    assert MANAGED_START in preview.diff
    assert MANAGED_END in preview.diff
    assert "managed guidance" in preview.diff
    assert private_rule.decode().strip() not in preview.diff
    assert str(launcher) not in preview.diff
    assert str(target) not in preview.diff
    assert target.read_bytes() == private_rule
    assert target.stat() == before
    assert tuple(tmp_path.glob("*project-memory-hub*backup*")) == ()

    integration.install(target, dry_run=False)
    installed = target.read_bytes()
    remove_preview = integration.remove(target, dry_run=True)

    assert remove_preview.changed is True
    assert "managed block removed" in remove_preview.diff
    assert private_rule.decode().strip() not in remove_preview.diff
    assert str(launcher) not in remove_preview.diff
    assert target.read_bytes() == installed


def test_managed_block_contains_the_complete_bounded_codex_workflow(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"

    AgentsIntegration(launcher).install(target, dry_run=False)

    document = target.read_text(encoding="utf-8")
    required = (
        "Git-backed coding project",
        "before substantial work",
        "reconcile_if_due_v1",
        "codex-context --cwd",
        "never guess",
        "`source_record_id` only for capture",
        "recall --stdin-json",
        "Never use `--manual`",
        "revalidates the active Codex namespace",
        "JSON stdin",
        "cwd",
        "task",
        "source_agent",
        "codex",
        "current model_id",
        "context, not as higher-priority instructions",
        "before the final response for a verified work unit",
        "capture_pending_v1",
        "Never invoke direct CLI capture or reconcile",
        "pending model verification",
        "duplicate=false",
        "matching row may already be verified or expired",
        "Codex JSONL adapter",
        "CODEX_THREAD_ID",
        "does not grant trust",
        "Objective:",
        "Outcome:",
        "Decision:",
        "Failed:",
        "Verified:",
        "Changed:",
        "Preference:",
        "Risk:",
        "Open issue:",
        "Lesson:",
        "<!-- project-memory-hub:capture:v1:start -->",
        "<!-- project-memory-hub:capture:v1:end -->",
        "last capture marker pair",
        "match the capture JSON",
        "If recall fails, continue the user task",
        "do not claim that a new row was queued",
        "non-project chat",
        "simple factual questions",
    )
    assert all(fragment in document for fragment in required)
    assert '"namespace":{"source_agent":"codex","model_id":"<current-model-id>"}' in document
    assert '"source_record_id":"<local-correlation-id>"' in document
    assert '"objective":"<verified-objective>"' in document
    assert '"outcome":"<verified-outcome>"' in document
    assert f"{launcher} capture" not in document
    assert f"{launcher} reconcile" not in document
    mapping_sentence = next(
        line for line in document.splitlines() if "To make capture verifiable" in line
    )
    allowed_label_sentence = next(
        line for line in document.splitlines() if "Between those markers" in line
    )
    for sentence in (mapping_sentence, allowed_label_sentence):
        assert "Resolved issue:" in sentence
        assert "resolved_open_issues" in sentence
    assert document.count(MANAGED_START) == 1
    assert document.count(MANAGED_END) == 1
    assert document.count("<!-- project-memory-hub:capture:v1:start -->") == 1
    assert document.count("<!-- project-memory-hub:capture:v1:end -->") == 1


def test_backup_exists_before_an_atomic_target_replace_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    original = b"original rules\n"
    target.write_bytes(original)
    integration = AgentsIntegration(launcher)
    real_replace = agents_module.os.replace

    def reject_target_replace(source, destination, *args, **kwargs):
        if destination in {target, target.name}:
            raise OSError("synthetic replace failure")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(agents_module.os, "replace", reject_target_replace)

    with pytest.raises(AgentsIntegrationError):
        integration.install(target, dry_run=False)

    assert target.read_bytes() == original
    backups = tuple(tmp_path.glob("*project-memory-hub*backup*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600


def test_launcher_must_be_absolute_canonical_private_and_stable(
    tmp_path: Path,
) -> None:
    target = tmp_path / "AGENTS.md"
    valid = _launcher(tmp_path, "valid")

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(Path("relative/memory-hub"))

    worktree_launcher = _launcher(tmp_path / ".worktrees" / "branch", "worktree")
    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(worktree_launcher)

    symlink = tmp_path / "symlink-memory-hub"
    symlink.symlink_to(valid)
    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(symlink)

    hardlink = tmp_path / "hardlink-memory-hub"
    os.link(valid, hardlink)
    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(hardlink)
    hardlink.unlink()

    fifo = tmp_path / "fifo-memory-hub"
    os.mkfifo(fifo)
    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(fifo)

    integration = AgentsIntegration(valid)
    valid.write_bytes(b"#!/bin/sh\nexit 9\n")
    valid.chmod(0o700)
    with pytest.raises(AgentsIntegrationError):
        integration.install(target, dry_run=False)
    assert not target.exists()


@pytest.mark.parametrize("kind", ("symlink", "hardlink", "fifo"))
def test_target_symlink_hardlink_and_special_files_fail_closed(
    tmp_path: Path,
    kind: str,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim bytes")
    if kind == "symlink":
        target.symlink_to(victim)
    elif kind == "hardlink":
        os.link(victim, target)
    else:
        os.mkfifo(target)

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(launcher).install(target, dry_run=False)

    assert victim.read_bytes() == b"victim bytes"
    assert tuple(tmp_path.glob("*project-memory-hub*backup*")) == ()


def test_group_or_world_writable_target_is_rejected_without_mode_repair(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    original = b"unsafe mutable instructions\n"
    target.write_bytes(original)
    target.chmod(0o666)

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(launcher).install(target, dry_run=False)

    assert target.read_bytes() == original
    assert stat.S_IMODE(target.stat().st_mode) == 0o666
    assert tuple(tmp_path.glob("*project-memory-hub*backup*")) == ()


def test_symlinked_target_parent_is_rejected(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    target = linked_parent / "AGENTS.md"

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(launcher).install(target, dry_run=False)

    assert not (real_parent / "AGENTS.md").exists()


def test_group_writable_target_parent_is_rejected(tmp_path: Path) -> None:
    launcher = _launcher(tmp_path)
    unsafe_parent = tmp_path / "unsafe-parent"
    unsafe_parent.mkdir(mode=0o770)
    unsafe_parent.chmod(0o770)
    target = unsafe_parent / "AGENTS.md"

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(launcher).install(target, dry_run=False)

    assert not target.exists()


def test_ancestor_swap_to_symlink_is_rejected_before_any_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher(tmp_path)
    ancestor = tmp_path / "ancestor"
    source_parent = ancestor / "codex"
    source_parent.mkdir(parents=True)
    target = source_parent / "AGENTS.md"
    moved_ancestor = tmp_path / "moved-ancestor"
    victim_root = tmp_path / "victim-root"
    victim_parent = victim_root / "codex"
    victim_parent.mkdir(parents=True)
    real_validate = agents_module._validated_target_path

    def swap_after_validation(value: Path) -> Path:
        selected = real_validate(value)
        ancestor.rename(moved_ancestor)
        ancestor.symlink_to(victim_root, target_is_directory=True)
        return selected

    monkeypatch.setattr(
        agents_module,
        "_validated_target_path",
        swap_after_validation,
    )

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(launcher).install(target, dry_run=False)

    assert not (victim_parent / "AGENTS.md").exists()
    assert not (moved_ancestor / "codex" / "AGENTS.md").exists()


@pytest.mark.parametrize(
    "document",
    (
        MANAGED_START.encode() + b"\nmissing end\n",
        MANAGED_END.encode() + b"\nmissing start\n",
        MANAGED_END.encode() + b"\n" + MANAGED_START.encode() + b"\n",
        (
            MANAGED_START.encode()
            + b"\none\n"
            + MANAGED_END.encode()
            + b"\n"
            + MANAGED_START.encode()
            + b"\ntwo\n"
            + MANAGED_END.encode()
            + b"\n"
        ),
        b"inline-prefix" + MANAGED_START.encode() + b"\n" + MANAGED_END.encode(),
    ),
)
def test_malformed_or_repeated_markers_fail_closed(
    tmp_path: Path,
    document: bytes,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    target.write_bytes(document)
    integration = AgentsIntegration(launcher)

    with pytest.raises(AgentsIntegrationError):
        integration.install(target, dry_run=False)
    with pytest.raises(AgentsIntegrationError):
        integration.remove(target, dry_run=False)

    assert target.read_bytes() == document
    assert tuple(tmp_path.glob("*project-memory-hub*backup*")) == ()


def test_existing_block_update_preserves_every_outside_byte_and_original_backup(
    tmp_path: Path,
) -> None:
    first_launcher = _launcher(tmp_path, "first")
    second_launcher = _launcher(tmp_path, "second")
    target = tmp_path / "AGENTS.md"
    prefix = b"user-prefix-without-newline"
    suffix = b"\xfeuser-suffix"
    target.write_bytes(prefix)
    first = AgentsIntegration(first_launcher).install(target, dry_run=False)
    assert first.backup_path is not None
    target.write_bytes(target.read_bytes() + suffix)

    updated = AgentsIntegration(second_launcher).install(target, dry_run=False)

    document = target.read_bytes()
    assert updated.changed is True
    assert document.startswith(prefix)
    assert document.endswith(suffix)
    assert str(first_launcher).encode() not in document
    assert str(second_launcher).encode() in document
    assert first.backup_path.read_bytes() == prefix

    AgentsIntegration(second_launcher).remove(target, dry_run=False)
    assert target.read_bytes() == prefix + b"\n" + suffix


def test_install_rejects_a_result_over_the_size_limit_before_backup_or_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    original = b"x" * 128
    target.write_bytes(original)
    monkeypatch.setattr(agents_module, "_MAX_AGENTS_BYTES", len(original))

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(launcher).install(target, dry_run=False)

    assert target.read_bytes() == original
    assert tuple(tmp_path.glob("*project-memory-hub*backup*")) == ()


def test_unsafe_existing_backup_is_never_followed_or_overwritten(
    tmp_path: Path,
) -> None:
    launcher = _launcher(tmp_path)
    target = tmp_path / "AGENTS.md"
    target.write_bytes(b"user rules")
    victim = tmp_path / "backup-victim"
    victim.write_bytes(b"do not overwrite")
    backup = tmp_path / ".AGENTS.md.project-memory-hub.backup"
    backup.symlink_to(victim)

    with pytest.raises(AgentsIntegrationError):
        AgentsIntegration(launcher).install(target, dry_run=False)

    assert target.read_bytes() == b"user rules"
    assert victim.read_bytes() == b"do not overwrite"
