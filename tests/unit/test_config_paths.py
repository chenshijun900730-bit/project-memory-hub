import errno
import multiprocessing
import os
import stat
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from project_memory_hub.config import (
    AppConfig,
    ConfigCommitUncertainError,
    ConfigConflictError,
    ConfigIOError,
    ConfigManager,
    ConfigRevision,
)
from project_memory_hub.paths import RuntimePaths


def _save_config_from_competing_process(
    config_path: str,
    revision_digest: str,
    writer: str,
    replace_barrier: Any,
    second_writer_replaced: Any,
    outcomes: Any,
) -> None:
    manager = ConfigManager(Path(config_path))
    original = manager.load()
    revision = ConfigRevision(revision_digest)
    real_replace = os.replace

    def ordered_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        try:
            replace_barrier.wait(timeout=0.5)
        except threading.BrokenBarrierError:
            real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            return
        if writer == "first":
            if not second_writer_replaced.wait(timeout=1):
                raise RuntimeError("competing writer did not replace config")
            real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            return
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        second_writer_replaced.set()

    os.replace = ordered_replace
    selected = (
        replace(original, max_recall_tokens=700)
        if writer == "first"
        else replace(original, inactive_days=45)
    )
    try:
        manager.save(selected, expected_revision=revision)
    except ConfigConflictError:
        outcomes.put(f"{writer}:conflict")
    except BaseException as error:
        outcomes.put(f"{writer}:error:{type(error).__name__}")
        raise
    else:
        outcomes.put(f"{writer}:saved")


def test_defaults_enable_only_codex_and_chatgpt(tmp_path: Path) -> None:
    config = AppConfig.defaults(tmp_path)
    assert [item.value for item in config.enabled_sources] == ["codex", "chatgpt"]
    assert config.setup_completed is False
    assert config.max_recall_tokens == 800
    assert config.inactive_days == 21
    assert config.project_roots == (
        tmp_path / "Documents",
        tmp_path / "Code x",
        tmp_path / "Workbuddy",
    )


def test_runtime_paths_are_private(tmp_path: Path) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()
    assert paths.root.stat().st_mode & 0o777 == 0o700
    assert paths.imports.stat().st_mode & 0o777 == 0o700


def test_runtime_paths_use_stable_mapping_without_creating_private_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    paths = RuntimePaths.for_root(root)

    assert paths.root == root
    assert paths.database == root / "memory.db"
    assert paths.imports == root / "imports"
    assert paths.retries == root / "retries"
    assert paths.backups == root / "backups"
    assert paths.logs == root / "logs"
    assert paths.access_token == root / "access-token"

    paths.ensure()

    assert all(
        directory.is_dir()
        for directory in (
            paths.root,
            paths.imports,
            paths.retries,
            paths.backups,
            paths.logs,
        )
    )
    assert not paths.database.exists()
    assert not paths.access_token.exists()


def test_runtime_paths_tighten_existing_permissions(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    imports = root / "imports"
    imports.mkdir(parents=True, mode=0o755)
    root.chmod(0o777)
    imports.chmod(0o754)

    RuntimePaths.for_root(root).ensure()

    assert root.stat().st_mode & 0o777 == 0o700
    assert imports.stat().st_mode & 0o777 == 0o700


def test_runtime_paths_reject_existing_directory_without_owner_access(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    root.chmod(0o600)

    try:
        with pytest.raises(PermissionError):
            RuntimePaths.for_root(root).ensure()
    finally:
        root.chmod(0o700)


def test_config_round_trip_is_private(tmp_path: Path) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    expected = AppConfig.defaults(tmp_path)
    manager.save(expected)
    assert manager.load() == expected
    assert manager.path.stat().st_mode & 0o777 == 0o600


def test_config_save_fsyncs_file_and_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    synced_kinds: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(descriptor: int) -> None:
        mode = os.fstat(descriptor).st_mode
        synced_kinds.append("directory" if stat.S_ISDIR(mode) else "file")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    manager = ConfigManager(tmp_path / "config.toml")

    manager.save(AppConfig.defaults(tmp_path))

    assert "file" in synced_kinds
    assert "directory" in synced_kinds


def test_config_save_reports_a_visible_but_not_durable_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    original = AppConfig.defaults(tmp_path)
    manager.save(original)
    _, revision = manager.load_with_revision()
    updated = replace(original, inactive_days=30)
    real_fsync = os.fsync

    def fail_parent_sync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("simulated parent fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fail_parent_sync)

    with pytest.raises(ConfigCommitUncertainError) as error:
        manager.save(updated, expected_revision=revision)

    assert error.value.replacement_completed is True
    assert error.value.durability_confirmed is False
    assert manager.load() == updated


@pytest.mark.parametrize("error_number", [errno.ENOSPC, errno.EIO])
def test_config_save_distinguishes_io_failure_from_policy_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    original = AppConfig.defaults(tmp_path)
    manager.save(original)
    _, revision = manager.load_with_revision()

    def fail_write(_descriptor: int, _document: bytes) -> int:
        raise OSError(error_number, "simulated config I/O failure")

    monkeypatch.setattr(os, "write", fail_write)

    with pytest.raises(ConfigIOError) as error:
        manager.save(replace(original, inactive_days=30), expected_revision=revision)

    assert not isinstance(error.value, PermissionError)
    assert manager.load() == original
    assert not tuple(tmp_path.glob(".config.toml.*.tmp"))


def test_config_load_distinguishes_io_failure_from_policy_denial(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    manager.save(AppConfig.defaults(tmp_path))

    def fail_read(_descriptor: int, _size: int) -> bytes:
        raise OSError(errno.EIO, "simulated config read failure")

    monkeypatch.setattr(os, "read", fail_read)

    with pytest.raises(ConfigIOError) as error:
        manager.load()

    assert not isinstance(error.value, PermissionError)


def test_config_save_rejects_a_stale_revision_without_overwriting(
    tmp_path: Path,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    original = AppConfig.defaults(tmp_path)
    manager.save(original)
    _, stale_revision = manager.load_with_revision()
    latest = replace(original, inactive_days=30)
    manager.save(latest)

    with pytest.raises(ConfigConflictError):
        manager.save(
            replace(original, max_recall_tokens=700),
            expected_revision=stale_revision,
        )

    assert manager.load() == latest


def test_config_save_serializes_writers_through_the_replace_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    original = AppConfig.defaults(tmp_path)
    manager.save(original)
    _, revision = manager.load_with_revision()
    real_replace = os.replace
    replace_barrier = threading.Barrier(2)
    second_writer_replaced = threading.Event()
    outcomes: list[str] = []

    def ordered_replace(
        source: str,
        destination: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        try:
            replace_barrier.wait(timeout=0.25)
        except threading.BrokenBarrierError:
            real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            return
        if threading.current_thread().name == "first-config-writer":
            assert second_writer_replaced.wait(timeout=1)
            real_replace(
                source,
                destination,
                src_dir_fd=src_dir_fd,
                dst_dir_fd=dst_dir_fd,
            )
            return
        real_replace(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        second_writer_replaced.set()

    monkeypatch.setattr(os, "replace", ordered_replace)

    def save(name: str, config: AppConfig) -> None:
        try:
            manager.save(config, expected_revision=revision)
        except ConfigConflictError:
            outcomes.append(f"{name}:conflict")
        else:
            outcomes.append(f"{name}:saved")

    first = threading.Thread(
        target=save,
        args=("first", replace(original, max_recall_tokens=700)),
        name="first-config-writer",
    )
    second = threading.Thread(
        target=save,
        args=("second", replace(original, inactive_days=45)),
        name="second-config-writer",
    )

    first.start()
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert sorted(outcomes) in (
        ["first:conflict", "second:saved"],
        ["first:saved", "second:conflict"],
    )


def test_config_save_serializes_competing_processes(
    tmp_path: Path,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    manager.save(AppConfig.defaults(tmp_path))
    _, revision = manager.load_with_revision()
    context = multiprocessing.get_context("spawn")
    replace_barrier = context.Barrier(2)
    second_writer_replaced = context.Event()
    outcomes = context.Queue()
    processes = tuple(
        context.Process(
            target=_save_config_from_competing_process,
            args=(
                str(manager.path),
                revision.digest,
                writer,
                replace_barrier,
                second_writer_replaced,
                outcomes,
            ),
        )
        for writer in ("first", "second")
    )

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=5)

    assert all(not process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0, 0]
    results = sorted(outcomes.get(timeout=1) for _process in processes)
    assert results in (
        ["first:conflict", "second:saved"],
        ["first:saved", "second:conflict"],
    )
    outcomes.close()


def test_config_save_is_a_zero_write_when_document_is_unchanged(tmp_path: Path) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    expected = AppConfig.defaults(tmp_path)
    manager.save(expected)
    _, revision = manager.load_with_revision()
    before = manager.path.stat()

    manager.save(expected, expected_revision=revision)

    after = manager.path.stat()
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )


def test_config_save_tightens_a_matching_document_to_private_mode(tmp_path: Path) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    expected = AppConfig.defaults(tmp_path)
    manager.save(expected)
    manager.path.chmod(0o644)
    _, revision = manager.load_with_revision()

    manager.save(expected, expected_revision=revision)

    assert stat.S_IMODE(manager.path.stat().st_mode) == 0o600
    assert manager.load() == expected


def test_config_save_rejects_an_existing_hard_link(tmp_path: Path) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    original = AppConfig.defaults(tmp_path)
    manager.save(original)
    linked = tmp_path / "linked-config.toml"
    os.link(manager.path, linked)
    before = manager.path.read_bytes()

    with pytest.raises(PermissionError):
        manager.save(replace(original, inactive_days=30))

    assert manager.path.read_bytes() == before
    assert linked.read_bytes() == before


def test_config_save_rejects_a_target_change_during_atomic_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")
    original = AppConfig.defaults(tmp_path)
    manager.save(original)
    _, revision = manager.load_with_revision()
    real_fsync = os.fsync
    swapped = False

    def swap_after_temporary_file_sync(descriptor: int) -> None:
        nonlocal swapped
        mode = os.fstat(descriptor).st_mode
        if not swapped and stat.S_ISREG(mode):
            swapped = True
            document = manager.path.read_text(encoding="utf-8")
            manager.path.write_text(
                document.replace("inactive_days = 21", "inactive_days = 31"),
                encoding="utf-8",
            )
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", swap_after_temporary_file_sync)

    with pytest.raises(ConfigConflictError):
        manager.save(
            replace(original, max_recall_tokens=700),
            expected_revision=revision,
        )

    assert manager.load().inactive_days == 31


def test_config_save_rejects_a_parent_directory_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "runtime"
    parent.mkdir(mode=0o700)
    manager = ConfigManager(parent / "config.toml")
    original = AppConfig.defaults(tmp_path)
    manager.save(original)
    _, revision = manager.load_with_revision()
    original_document = manager.path.read_bytes()
    moved_parent = tmp_path / "runtime-moved"
    attacker_parent = tmp_path / "attacker"
    attacker_parent.mkdir(mode=0o700)
    real_fsync = os.fsync
    swapped = False

    def swap_parent_after_temporary_file_sync(descriptor: int) -> None:
        nonlocal swapped
        mode = os.fstat(descriptor).st_mode
        if not swapped and stat.S_ISREG(mode):
            swapped = True
            temporary = next(parent.glob(".config.toml.*.tmp"))
            parent.rename(moved_parent)
            parent.symlink_to(attacker_parent, target_is_directory=True)
            (attacker_parent / "config.toml").write_bytes(original_document)
            malicious_temporary = attacker_parent / temporary.name
            malicious_temporary.write_bytes(b"attacker-controlled\n")
            malicious_temporary.chmod(0o600)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", swap_parent_after_temporary_file_sync)

    with pytest.raises(PermissionError):
        manager.save(
            replace(original, inactive_days=30),
            expected_revision=revision,
        )

    assert (attacker_parent / "config.toml").read_bytes() == original_document
    assert (moved_parent / "config.toml").read_bytes() == original_document


def test_old_config_without_improvement_keys_loads_compatible_defaults(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        "\n".join(
            (
                f'project_roots = ["{tmp_path}"]',
                'enabled_sources = ["codex", "chatgpt"]',
                "inactive_days = 21",
                "max_recall_tokens = 800",
                'daily_reconcile_time = "03:30"',
                "",
            )
        ),
        encoding="utf-8",
    )

    loaded = ConfigManager(config_path).load()

    assert loaded.improvement_repository_root is None
    assert loaded.improvement_verification_commands == ()
    assert loaded.codex_project_id is None
    assert loaded.setup_completed is True


def test_improvement_execution_config_round_trips_as_exact_argv(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "project-memory-hub"
    command = ("/usr/bin/true", "--fixed-check")
    expected = AppConfig(
        project_roots=(tmp_path,),
        enabled_sources=AppConfig.defaults(tmp_path).enabled_sources,
        inactive_days=21,
        max_recall_tokens=800,
        daily_reconcile_time="03:30",
        improvement_repository_root=repository,
        improvement_verification_commands=(command,),
    )
    manager = ConfigManager(tmp_path / "config.toml")

    manager.save(expected)

    assert manager.load() == expected
    assert manager.load().improvement_verification_commands == (command,)


def test_none_improvement_config_is_omitted_from_serialized_toml(
    tmp_path: Path,
) -> None:
    manager = ConfigManager(tmp_path / "config.toml")

    manager.save(AppConfig.defaults(tmp_path))

    document = manager.path.read_text(encoding="utf-8")
    assert "improvement_repository_root" not in document
    assert "improvement_verification_commands" not in document
    assert "codex_project_id" not in document


def test_codex_project_id_round_trips_as_bounded_host_metadata(tmp_path: Path) -> None:
    expected = AppConfig(
        project_roots=(tmp_path,),
        enabled_sources=AppConfig.defaults(tmp_path).enabled_sources,
        inactive_days=21,
        max_recall_tokens=800,
        daily_reconcile_time="03:30",
        codex_project_id="opaque-codex-project-id",
    )
    manager = ConfigManager(tmp_path / "config.toml")

    manager.save(expected)

    assert manager.load() == expected
    with pytest.raises(ValueError):
        AppConfig(
            project_roots=(tmp_path,),
            enabled_sources=expected.enabled_sources,
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
            codex_project_id="bad\nproject-id",
        )
