from __future__ import annotations

import errno
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes import filesystem as filesystem_module
from project_memory_hub.probes.base import (
    ExpectedPathType,
    ProbeClock,
    SourceDescriptor,
    SystemProbeClock,
    TrustedAnchor,
    TrustedPath,
)
from project_memory_hub.probes.filesystem import (
    PathSafetyPolicy,
    SafeProbeFilesystem,
    aggregate_data_status,
    aggregate_installation_status,
)
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ProbeBudget,
    ProbeCapability,
    ProbeWarningCode,
)


class MutableClock(ProbeClock):
    def __init__(self, monotonic_value: float = 0.0) -> None:
        self.monotonic_value = monotonic_value

    def now(self) -> datetime:
        return datetime(2026, 7, 17, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.monotonic_value


def _descriptor(
    *,
    markers: tuple[TrustedPath, ...] = (),
    roots: tuple[TrustedPath, ...] = (),
) -> SourceDescriptor:
    return SourceDescriptor(
        source_agent=SourceAgent.WORKBUDDY,
        installation_markers=markers,
        data_roots=roots,
        capability=ProbeCapability.PRESENCE_AND_ACCESS,
    )


def _home_directory(*components: str) -> TrustedPath:
    return TrustedPath(TrustedAnchor.HOME, components, ExpectedPathType.DIRECTORY)


def _home_executable(*components: str) -> TrustedPath:
    return TrustedPath(
        TrustedAnchor.HOME,
        components,
        ExpectedPathType.EXECUTABLE_FILE,
    )


def _inspect(
    home: Path,
    descriptor: SourceDescriptor,
    *,
    clock: ProbeClock | None = None,
    deadline: float = 2.0,
) -> filesystem_module.LightInspection:
    return SafeProbeFilesystem(PathSafetyPolicy(home=home)).inspect_light(
        descriptor,
        budget=ProbeBudget(),
        clock=clock or MutableClock(),
        deadline=deadline,
    )


def test_path_safety_policy_requires_an_absolute_home_without_touching_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("policy construction touched the filesystem")

    monkeypatch.setattr(filesystem_module.os, "stat", unexpected)
    monkeypatch.setattr(filesystem_module.os, "open", unexpected)

    policy = PathSafetyPolicy(home=Path("/definitely/not/required"))

    assert policy.home == Path("/definitely/not/required")
    with pytest.raises(ValueError, match="absolute"):
        PathSafetyPolicy(home=Path("relative"))


def test_light_probe_never_enumerates_or_reads_root_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / "safe-root").mkdir()
    descriptor = _descriptor(roots=(_home_directory("safe-root"),))

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("light probe accessed directory contents")

    monkeypatch.setattr(filesystem_module.os, "scandir", unexpected)
    monkeypatch.setattr(filesystem_module.os, "pread", unexpected)
    monkeypatch.setattr(filesystem_module.os, "read", unexpected)
    monkeypatch.setattr(filesystem_module.os, "listdir", unexpected)
    monkeypatch.setattr(filesystem_module.os, "walk", unexpected)
    clock = SystemProbeClock()
    inspection = SafeProbeFilesystem(PathSafetyPolicy(home=home)).inspect_light(
        descriptor,
        budget=ProbeBudget(),
        clock=clock,
        deadline=clock.monotonic() + 2.0,
    )

    assert inspection.data_status is DataStatus.READABLE
    assert inspection.metrics.readable_data_root_count == 1


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (("readable", "blocked", "rejected", "missing"), DataStatus.READABLE),
        (("blocked", "rejected", "missing"), DataStatus.BLOCKED),
        (("rejected", "missing"), DataStatus.REJECTED),
        (("missing",), DataStatus.MISSING),
        ((), DataStatus.MISSING),
    ],
)
def test_light_data_status_has_fixed_precedence(
    states: tuple[str, ...], expected: DataStatus
) -> None:
    assert aggregate_data_status(states) is expected


@pytest.mark.parametrize(
    ("marker_hits", "expected"),
    [
        ((False, False), InstallationStatus.NOT_DETECTED),
        ((True, False), InstallationStatus.DETECTED),
        ((False, True), InstallationStatus.DETECTED),
        ((True, True), InstallationStatus.DETECTED),
        ((), InstallationStatus.NOT_DETECTED),
    ],
)
def test_installation_status_is_detected_when_any_marker_is_safe(
    marker_hits: tuple[bool, ...], expected: InstallationStatus
) -> None:
    assert aggregate_installation_status(marker_hits) is expected


@pytest.mark.parametrize("symlink_position", ["anchor", "intermediate", "leaf"])
def test_light_probe_rejects_symlink_in_any_path_component(
    tmp_path: Path, symlink_position: str
) -> None:
    base = tmp_path.resolve()
    actual_home = base / "actual-home"
    actual_home.mkdir()
    (actual_home / "real").mkdir()
    (actual_home / "real" / "leaf").mkdir()

    if symlink_position == "anchor":
        selected_home = base / "home-link"
        selected_home.symlink_to(actual_home, target_is_directory=True)
        root = _home_directory("real", "leaf")
    elif symlink_position == "intermediate":
        selected_home = actual_home
        (selected_home / "middle-link").symlink_to(selected_home / "real", target_is_directory=True)
        root = _home_directory("middle-link", "leaf")
    else:
        selected_home = actual_home
        (selected_home / "leaf-link").symlink_to(
            selected_home / "real" / "leaf", target_is_directory=True
        )
        root = _home_directory("leaf-link")

    inspection = _inspect(selected_home, _descriptor(roots=(root,)))

    assert inspection.data_status is DataStatus.REJECTED
    assert ProbeWarningCode.SYMLINK_REJECTED in inspection.warning_codes
    assert inspection.metrics.rejected_data_root_count == 1


def test_home_parent_symlink_is_rejected_and_traversal_is_fd_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path.resolve()
    real_users = base / "real-users"
    selected_home = real_users / "home"
    (selected_home / "safe").mkdir(parents=True)
    users_link = base / "users-link"
    users_link.symlink_to(real_users, target_is_directory=True)
    lexical_home = users_link / "home"
    real_stat = filesystem_module.os.stat
    real_open = filesystem_module.os.open
    absolute_arguments: list[str] = []

    def tracked_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        selected = os.fspath(path)
        if isinstance(selected, str) and selected.startswith("/"):
            absolute_arguments.append(selected)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        selected = os.fspath(path)
        if isinstance(selected, str) and selected.startswith("/"):
            absolute_arguments.append(selected)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "stat", tracked_stat)
    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)

    inspection = _inspect(
        lexical_home,
        _descriptor(roots=(_home_directory("safe"),)),
    )

    assert inspection.data_status is DataStatus.REJECTED
    assert ProbeWarningCode.SYMLINK_REJECTED in inspection.warning_codes
    assert absolute_arguments and set(absolute_arguments) == {"/"}


@contextmanager
def _unsafe_marker(home: Path, marker_kind: str) -> Iterator[None]:
    target = home / "claude"
    if marker_kind == "fifo":
        os.mkfifo(target)
        yield
        return
    target.write_bytes(b"#!/bin/sh\n")
    target.chmod(0o600)
    yield


@pytest.mark.parametrize("marker_kind", ["fifo", "socket", "non_executable"])
def test_light_probe_rejects_fifo_socket_and_non_executable_cli_marker(
    marker_kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with TemporaryDirectory(prefix="pmh-probe-", dir="/tmp") as temporary_home:
        home = Path(temporary_home).resolve()
        if marker_kind == "socket":
            (home / "claude").touch()
            real_stat = filesystem_module.os.stat

            def socket_leaf_stat(
                path: str | bytes | os.PathLike[str],
                *,
                dir_fd: int | None = None,
                follow_symlinks: bool = True,
            ) -> os.stat_result:
                value = real_stat(
                    path,
                    dir_fd=dir_fd,
                    follow_symlinks=follow_symlinks,
                )
                if path == "claude":
                    fields = list(value)
                    fields[0] = stat.S_IFSOCK | 0o600
                    return os.stat_result(fields)
                return value

            monkeypatch.setattr(filesystem_module.os, "stat", socket_leaf_stat)
            inspection = _inspect(
                home,
                _descriptor(markers=(_home_executable("claude"),)),
            )
        else:
            with _unsafe_marker(home, marker_kind):
                inspection = _inspect(
                    home,
                    _descriptor(markers=(_home_executable("claude"),)),
                )

    assert inspection.installation_status is InstallationStatus.NOT_DETECTED
    assert ProbeWarningCode.UNSAFE_FILE_TYPE in inspection.warning_codes


@pytest.mark.parametrize("marker_type", ["directory", "executable"])
def test_light_probe_detects_safe_directory_and_executable_markers(
    tmp_path: Path, marker_type: str
) -> None:
    home = tmp_path.resolve()
    marker = home / "marker"
    if marker_type == "directory":
        marker.mkdir()
        trusted_marker = _home_directory("marker")
    else:
        marker.write_bytes(b"#!/bin/sh\n")
        marker.chmod(0o700)
        trusted_marker = _home_executable("marker")

    inspection = _inspect(
        home,
        _descriptor(markers=(trusted_marker,)),
    )

    assert inspection.installation_status is InstallationStatus.DETECTED
    assert inspection.metrics.checked_installation_marker_count == 1
    assert inspection.metrics.detected_installation_marker_count == 1


def test_executable_leaf_open_is_nonblocking_and_rejects_opened_fifo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    executable = home / "claude"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o700)
    real_open = filesystem_module.os.open
    real_fstat = filesystem_module.os.fstat
    real_close = filesystem_module.os.close
    leaf_fds: set[int] = set()
    leaf_flags: list[int] = []
    directory_flags: list[int] = []

    def tracked_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "claude":
            leaf_flags.append(flags)
            leaf_fds.add(fd)
        else:
            directory_flags.append(flags)
        return fd

    def changed_fstat(fd: int) -> os.stat_result:
        value = real_fstat(fd)
        if fd not in leaf_fds:
            return value
        fields = list(value)
        fields[0] = stat.S_IFIFO | 0o700
        return os.stat_result(fields)

    def tracked_close(fd: int) -> None:
        real_close(fd)
        leaf_fds.discard(fd)

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", changed_fstat)
    monkeypatch.setattr(filesystem_module.os, "close", tracked_close)

    inspection = _inspect(
        home,
        _descriptor(markers=(_home_executable("claude"),)),
    )

    assert leaf_flags and leaf_flags[0] & os.O_NONBLOCK
    assert all(flags & os.O_NONBLOCK == 0 for flags in directory_flags)
    assert inspection.installation_status is InstallationStatus.NOT_DETECTED
    assert ProbeWarningCode.SOURCE_CHANGED in inspection.warning_codes
    assert leaf_fds == set()


def test_light_probe_maps_eacces_to_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / "blocked").mkdir()
    real_open = filesystem_module.os.open

    def blocked_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == "blocked":
            raise PermissionError(errno.EACCES, "private detail")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "open", blocked_open)

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("blocked"),)),
    )

    assert inspection.data_status is DataStatus.BLOCKED
    assert ProbeWarningCode.PERMISSION_BLOCKED in inspection.warning_codes
    assert inspection.metrics.blocked_data_root_count == 1


def _changed_stat_result(value: os.stat_result, identity_field: str = "inode") -> os.stat_result:
    selected = {
        "st_mode": value.st_mode,
        "st_dev": value.st_dev,
        "st_ino": value.st_ino,
        "st_size": value.st_size,
        "st_mtime_ns": value.st_mtime_ns,
    }
    if identity_field == "type":
        selected["st_mode"] = stat.S_IFREG | 0o600
    else:
        attribute = {
            "device": "st_dev",
            "inode": "st_ino",
            "size": "st_size",
            "mtime": "st_mtime_ns",
        }[identity_field]
        selected[attribute] += 1
    return cast(os.stat_result, SimpleNamespace(**selected))


def _stat_result_with_mode(value: os.stat_result, mode: int) -> os.stat_result:
    return cast(
        os.stat_result,
        SimpleNamespace(
            st_mode=mode,
            st_dev=value.st_dev,
            st_ino=value.st_ino,
            st_size=value.st_size,
            st_mtime_ns=value.st_mtime_ns,
        ),
    )


def _inject_identity_change(
    *,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor_level: str,
    change_phase: str,
    identity_field: str,
) -> None:
    target_component = {
        "root": "/",
        "home": home.name,
        "intermediate": "middle",
    }[ancestor_level]
    real_open = filesystem_module.os.open
    real_fstat = filesystem_module.os.fstat
    real_stat = filesystem_module.os.stat
    target_fds: set[int] = set()
    target_stat_calls = 0

    def tracked_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == target_component:
            target_fds.add(fd)
        return fd

    def changed_fstat(fd: int) -> os.stat_result:
        value = real_fstat(fd)
        if change_phase == "opened" and fd in target_fds:
            return _changed_stat_result(value, identity_field)
        return value

    def changed_after_stat(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal target_stat_calls
        value = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == target_component:
            target_stat_calls += 1
            if change_phase == "after" and target_stat_calls == 2:
                return _changed_stat_result(value, identity_field)
        return value

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", changed_fstat)
    monkeypatch.setattr(filesystem_module.os, "stat", changed_after_stat)


@pytest.mark.parametrize("ancestor_level", ["root", "home", "intermediate"])
@pytest.mark.parametrize("identity_field", ["size", "mtime"])
@pytest.mark.parametrize("change_phase", ["opened", "after"])
def test_ancestor_metadata_churn_does_not_reject_a_stable_leaf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor_level: str,
    identity_field: str,
    change_phase: str,
) -> None:
    home = tmp_path.resolve()
    (home / "middle" / "leaf").mkdir(parents=True)
    _inject_identity_change(
        home=home,
        monkeypatch=monkeypatch,
        ancestor_level=ancestor_level,
        change_phase=change_phase,
        identity_field=identity_field,
    )

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("middle", "leaf"),)),
    )

    assert inspection.data_status is DataStatus.READABLE
    assert ProbeWarningCode.SOURCE_CHANGED not in inspection.warning_codes


@pytest.mark.parametrize("ancestor_level", ["root", "home", "intermediate"])
@pytest.mark.parametrize("identity_field", ["device", "inode", "type"])
def test_ancestor_location_change_is_still_source_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ancestor_level: str,
    identity_field: str,
) -> None:
    home = tmp_path.resolve()
    (home / "middle" / "leaf").mkdir(parents=True)
    _inject_identity_change(
        home=home,
        monkeypatch=monkeypatch,
        ancestor_level=ancestor_level,
        change_phase="opened",
        identity_field=identity_field,
    )

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("middle", "leaf"),)),
    )

    assert inspection.data_status is DataStatus.REJECTED
    assert ProbeWarningCode.SOURCE_CHANGED in inspection.warning_codes


@pytest.mark.parametrize("identity_field", ["device", "inode", "type", "size", "mtime"])
@pytest.mark.parametrize("change_phase", ["opened", "after"])
def test_light_probe_rejects_preview_open_and_after_open_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change_phase: str,
    identity_field: str,
) -> None:
    home = tmp_path.resolve()
    (home / "changing").mkdir()

    if change_phase == "opened":
        real_open = filesystem_module.os.open
        real_fstat = filesystem_module.os.fstat
        leaf_fds: set[int] = set()

        def tracked_open(
            path: str | bytes,
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            fd = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "changing":
                leaf_fds.add(fd)
            return fd

        def changed_fstat(fd: int) -> os.stat_result:
            value = real_fstat(fd)
            if fd in leaf_fds:
                return _changed_stat_result(value, identity_field)
            return value

        monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
        monkeypatch.setattr(filesystem_module.os, "fstat", changed_fstat)
    else:
        real_stat = filesystem_module.os.stat
        leaf_calls = 0

        def changed_after_stat(
            path: str | bytes,
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            nonlocal leaf_calls
            value = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if path == "changing":
                leaf_calls += 1
                if leaf_calls == 2:
                    return _changed_stat_result(value, identity_field)
            return value

        monkeypatch.setattr(filesystem_module.os, "stat", changed_after_stat)

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("changing"),)),
    )

    assert inspection.data_status is DataStatus.REJECTED
    assert ProbeWarningCode.SOURCE_CHANGED in inspection.warning_codes


@pytest.mark.parametrize("change_phase", ["opened", "after"])
def test_executable_permission_is_revalidated_after_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change_phase: str,
) -> None:
    home = tmp_path.resolve()
    executable = home / "claude"
    executable.write_bytes(b"#!/bin/sh\n")
    executable.chmod(0o700)
    real_open = filesystem_module.os.open
    real_fstat = filesystem_module.os.fstat
    real_stat = filesystem_module.os.stat
    leaf_fds: set[int] = set()
    leaf_stat_calls = 0

    def tracked_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "claude":
            leaf_fds.add(fd)
        return fd

    def changed_fstat(fd: int) -> os.stat_result:
        value = real_fstat(fd)
        if change_phase == "opened" and fd in leaf_fds:
            return _stat_result_with_mode(value, stat.S_IFREG | 0o600)
        return value

    def changed_stat(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal leaf_stat_calls
        value = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == "claude":
            leaf_stat_calls += 1
            if change_phase == "after" and leaf_stat_calls == 2:
                return _stat_result_with_mode(value, stat.S_IFREG | 0o600)
        return value

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", changed_fstat)
    monkeypatch.setattr(filesystem_module.os, "stat", changed_stat)

    inspection = _inspect(
        home,
        _descriptor(markers=(_home_executable("claude"),)),
    )

    assert inspection.installation_status is InstallationStatus.NOT_DETECTED
    assert ProbeWarningCode.UNSAFE_FILE_TYPE in inspection.warning_codes


def test_bad_root_warning_does_not_hide_another_readable_root(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    (home / "unsafe-target").mkdir()
    (home / "unsafe").symlink_to(home / "unsafe-target", target_is_directory=True)
    (home / "safe").mkdir()

    inspection = _inspect(
        home,
        _descriptor(
            roots=(_home_directory("unsafe"), _home_directory("safe")),
        ),
    )

    assert inspection.data_status is DataStatus.READABLE
    assert ProbeWarningCode.SYMLINK_REJECTED in inspection.warning_codes
    assert inspection.metrics.readable_data_root_count == 1
    assert inspection.metrics.rejected_data_root_count == 1


@pytest.mark.parametrize("fail_leaf", [False, True])
def test_light_probe_closes_every_descriptor_on_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_leaf: bool,
) -> None:
    home = tmp_path.resolve()
    (home / "outer").mkdir()
    (home / "outer" / "leaf").mkdir()
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    opened: set[int] = set()

    def tracked_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if fail_leaf and path == "leaf":
            raise PermissionError(errno.EACCES, "private detail")
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.add(fd)
        return fd

    def tracked_close(fd: int) -> None:
        real_close(fd)
        opened.discard(fd)

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "close", tracked_close)

    _inspect(
        home,
        _descriptor(roots=(_home_directory("outer", "leaf"),)),
    )

    assert opened == set()


@pytest.mark.parametrize(
    ("failure_phase", "expected_exception"),
    [
        ("fstat_exception", RuntimeError),
        ("after_exception", OSError),
        ("keyboard_interrupt", KeyboardInterrupt),
    ],
)
def test_post_open_unknown_failures_propagate_after_closing_every_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    expected_exception: type[BaseException],
) -> None:
    home = tmp_path.resolve()
    (home / "leaf").mkdir()
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    real_fstat = filesystem_module.os.fstat
    real_stat = filesystem_module.os.stat
    opened: set[int] = set()
    leaf_fds: set[int] = set()
    leaf_stat_calls = 0

    def tracked_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.add(fd)
        if path == "leaf":
            leaf_fds.add(fd)
        return fd

    def failing_fstat(fd: int) -> os.stat_result:
        if fd in leaf_fds:
            if failure_phase == "fstat_exception":
                raise RuntimeError("private detail")
            if failure_phase == "keyboard_interrupt":
                raise KeyboardInterrupt
        return real_fstat(fd)

    def failing_after_stat(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal leaf_stat_calls
        if path == "leaf":
            leaf_stat_calls += 1
            if failure_phase == "after_exception" and leaf_stat_calls == 2:
                raise OSError(errno.EIO, "private detail")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def tracked_close(fd: int) -> None:
        real_close(fd)
        opened.discard(fd)
        leaf_fds.discard(fd)

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", failing_fstat)
    monkeypatch.setattr(filesystem_module.os, "stat", failing_after_stat)
    monkeypatch.setattr(filesystem_module.os, "close", tracked_close)

    with pytest.raises(expected_exception):
        _inspect(home, _descriptor(roots=(_home_directory("leaf"),)))

    assert opened == set()


def test_deadline_crossing_after_final_stat_closes_fds_and_returns_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / "leaf").mkdir()
    clock = MutableClock(0.0)
    real_stat = filesystem_module.os.stat
    leaf_stat_calls = 0

    def advance_after_stat(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal leaf_stat_calls
        value = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == "leaf":
            leaf_stat_calls += 1
            if leaf_stat_calls == 2:
                clock.monotonic_value = 2.0
        return value

    monkeypatch.setattr(filesystem_module.os, "stat", advance_after_stat)

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("leaf"),)),
        clock=clock,
        deadline=2.0,
    )

    assert inspection.data_status is DataStatus.MISSING
    assert inspection.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


@pytest.mark.parametrize("error_phase", ["preview_stat", "open", "fstat", "after_stat"])
def test_syscall_error_crossing_deadline_returns_only_timeout_after_fd_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_phase: str,
) -> None:
    home = tmp_path.resolve()
    (home / "leaf").mkdir()
    clock = MutableClock(0.0)
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    real_fstat = filesystem_module.os.fstat
    real_stat = filesystem_module.os.stat
    opened: set[int] = set()
    leaf_fds: set[int] = set()
    leaf_stat_calls = 0

    def deadline_crossing_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if error_phase == "open" and path == "leaf":
            clock.monotonic_value = 2.0
            raise OSError(errno.EACCES, "private detail")
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        opened.add(fd)
        if path == "leaf":
            leaf_fds.add(fd)
        return fd

    def deadline_crossing_fstat(fd: int) -> os.stat_result:
        if error_phase == "fstat" and fd in leaf_fds:
            clock.monotonic_value = 2.0
            raise OSError(errno.EIO, "private detail")
        return real_fstat(fd)

    def deadline_crossing_stat(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal leaf_stat_calls
        if path == "leaf":
            leaf_stat_calls += 1
            if error_phase == "preview_stat" and leaf_stat_calls == 1:
                clock.monotonic_value = 2.0
                raise OSError(errno.ENOENT, "private detail")
            if error_phase == "after_stat" and leaf_stat_calls == 2:
                clock.monotonic_value = 2.0
                raise OSError(errno.ENOENT, "private detail")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def tracked_close(fd: int) -> None:
        real_close(fd)
        opened.discard(fd)
        leaf_fds.discard(fd)

    monkeypatch.setattr(filesystem_module.os, "open", deadline_crossing_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", deadline_crossing_fstat)
    monkeypatch.setattr(filesystem_module.os, "stat", deadline_crossing_stat)
    monkeypatch.setattr(filesystem_module.os, "close", tracked_close)

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("leaf"),)),
        clock=clock,
        deadline=2.0,
    )

    assert inspection.data_status is DataStatus.MISSING
    assert inspection.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
    assert opened == set()


def test_deadline_crossing_during_exit_stack_cleanup_returns_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / "leaf").mkdir()
    clock = MutableClock(0.0)
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    leaf_fds: set[int] = set()

    def tracked_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "leaf":
            leaf_fds.add(fd)
        return fd

    def deadline_crossing_close(fd: int) -> None:
        is_leaf = fd in leaf_fds
        real_close(fd)
        leaf_fds.discard(fd)
        if is_leaf:
            clock.monotonic_value = 2.0

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "close", deadline_crossing_close)

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("leaf"),)),
        clock=clock,
        deadline=2.0,
    )

    assert inspection.data_status is DataStatus.MISSING
    assert inspection.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
    assert leaf_fds == set()


@pytest.mark.parametrize("race_phase", ["open", "after"])
@pytest.mark.parametrize("race_errno", [errno.ENOENT, errno.ENOTDIR, errno.ELOOP])
def test_preview_to_open_or_after_disappearance_is_source_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    race_phase: str,
    race_errno: int,
) -> None:
    home = tmp_path.resolve()
    (home / "leaf").mkdir()
    real_open = filesystem_module.os.open
    real_stat = filesystem_module.os.stat
    leaf_stat_calls = 0

    def racing_open(
        path: str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if race_phase == "open" and path == "leaf":
            raise OSError(race_errno, "private detail")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def racing_after_stat(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal leaf_stat_calls
        if path == "leaf":
            leaf_stat_calls += 1
            if race_phase == "after" and leaf_stat_calls == 2:
                raise OSError(race_errno, "private detail")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_module.os, "open", racing_open)
    monkeypatch.setattr(filesystem_module.os, "stat", racing_after_stat)

    inspection = _inspect(home, _descriptor(roots=(_home_directory("leaf"),)))

    assert inspection.data_status is DataStatus.REJECTED
    assert ProbeWarningCode.SOURCE_CHANGED in inspection.warning_codes


def test_light_probe_enforces_sixteen_targets_per_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    for index in range(17):
        (home / f"root-{index}").mkdir()
    roots = tuple(_home_directory(f"root-{index}") for index in range(17))
    real_stat = filesystem_module.os.stat
    seen_paths: list[str | bytes] = []

    def tracked_stat(
        path: str | bytes,
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        seen_paths.append(path)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_module.os, "stat", tracked_stat)

    inspection = _inspect(home, _descriptor(roots=roots))

    assert "root-15" in seen_paths
    assert "root-16" not in seen_paths
    assert inspection.metrics.checked_data_root_count == 16
    assert ProbeWarningCode.BUDGET_EXCEEDED in inspection.warning_codes


@pytest.mark.parametrize(
    (
        "phase",
        "marker_count",
        "root_count",
        "deadline_trigger",
        "untouched_target",
        "expected_checked_markers",
        "expected_checked_roots",
    ),
    [
        ("markers", 17, 0, "marker-15", "marker-16", 16, 0),
        ("marker_to_roots", 1, 16, "root-14", "root-15", 1, 15),
        ("roots", 0, 17, "root-15", "root-16", 0, 16),
    ],
)
def test_deadline_wins_when_target_budget_becomes_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    marker_count: int,
    root_count: int,
    deadline_trigger: str,
    untouched_target: str,
    expected_checked_markers: int,
    expected_checked_roots: int,
) -> None:
    del phase
    home = tmp_path.resolve()
    clock = MutableClock(0.0)
    markers = tuple(_home_directory(f"marker-{index}") for index in range(marker_count))
    roots = tuple(_home_directory(f"root-{index}") for index in range(root_count))
    real_stat = filesystem_module.os.stat
    real_open = filesystem_module.os.open
    filesystem_calls: list[str | bytes | os.PathLike[str]] = []

    def deadline_crossing_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        filesystem_calls.append(path)
        try:
            return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        finally:
            if path == deadline_trigger:
                clock.monotonic_value = 2.0

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        filesystem_calls.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "stat", deadline_crossing_stat)
    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)

    inspection = _inspect(
        home,
        _descriptor(markers=markers, roots=roots),
        clock=clock,
        deadline=2.0,
    )

    assert inspection.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
    assert ProbeWarningCode.BUDGET_EXCEEDED not in inspection.warning_codes
    assert inspection.metrics.checked_installation_marker_count == expected_checked_markers
    assert inspection.metrics.checked_data_root_count == expected_checked_roots
    assert untouched_target not in filesystem_calls


def test_light_probe_consumes_the_same_supplied_deadline_across_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / "first").mkdir()
    (home / "second").mkdir()
    clock = MutableClock(0.0)
    shared_deadline = 2.0

    first = _inspect(
        home,
        _descriptor(roots=(_home_directory("first"),)),
        clock=clock,
        deadline=shared_deadline,
    )
    clock.monotonic_value = 2.0

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("timed-out probe touched the filesystem")

    monkeypatch.setattr(filesystem_module.os, "stat", unexpected)
    monkeypatch.setattr(filesystem_module.os, "open", unexpected)
    second = _inspect(
        home,
        _descriptor(roots=(_home_directory("second"),)),
        clock=clock,
        deadline=shared_deadline,
    )

    assert first.data_status is DataStatus.READABLE
    assert second.data_status is DataStatus.MISSING
    assert second.metrics.checked_data_root_count == 0
    assert second.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


@pytest.mark.parametrize("missing_flag", ["O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY"])
@pytest.mark.parametrize("unavailable_as", ["missing", "zero"])
def test_light_probe_fails_closed_when_required_open_flag_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_flag: str,
    unavailable_as: str,
) -> None:
    home = tmp_path.resolve()
    (home / "safe").mkdir()
    real_stat = filesystem_module.os.stat
    real_open = filesystem_module.os.open
    probe_calls: list[str | bytes] = []
    if unavailable_as == "missing":
        monkeypatch.delattr(filesystem_module.os, missing_flag)
    else:
        monkeypatch.setattr(filesystem_module.os, missing_flag, 0)

    def tracked_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path in {"/", "safe"}:
            probe_calls.append(path)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path in {"/", "safe"}:
            probe_calls.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "stat", tracked_stat)
    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("safe"),)),
    )

    assert inspection.data_status is DataStatus.REJECTED
    assert inspection.warning_codes == (ProbeWarningCode.UNSUPPORTED_FORMAT,)
    assert probe_calls == []


@pytest.mark.parametrize("unavailable_as", ["missing", "zero"])
def test_nonblocking_flag_is_required_only_for_executable_leaf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unavailable_as: str
) -> None:
    home = tmp_path.resolve()
    (home / "safe").mkdir()
    if unavailable_as == "missing":
        monkeypatch.delattr(filesystem_module.os, "O_NONBLOCK")
    else:
        monkeypatch.setattr(filesystem_module.os, "O_NONBLOCK", 0)

    directory_inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("safe"),)),
    )
    executable_inspection = _inspect(
        home,
        _descriptor(markers=(_home_executable("claude"),)),
    )

    assert directory_inspection.data_status is DataStatus.READABLE
    assert ProbeWarningCode.UNSUPPORTED_FORMAT not in directory_inspection.warning_codes
    assert executable_inspection.installation_status is InstallationStatus.NOT_DETECTED
    assert ProbeWarningCode.UNSUPPORTED_FORMAT in executable_inspection.warning_codes


@pytest.mark.parametrize(
    "missing_capability",
    [
        "_OPEN_SUPPORTS_DIR_FD",
        "_STAT_SUPPORTS_DIR_FD",
        "_STAT_SUPPORTS_FOLLOW_SYMLINKS",
    ],
)
def test_light_probe_fails_closed_without_fd_relative_stat_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing_capability: str,
) -> None:
    home = tmp_path.resolve()
    (home / "safe").mkdir()
    real_stat = filesystem_module.os.stat
    real_open = filesystem_module.os.open
    probe_calls: list[str | bytes | os.PathLike[str]] = []
    monkeypatch.setattr(filesystem_module, missing_capability, False, raising=False)

    def tracked_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path in {"/", "safe"}:
            probe_calls.append(path)
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path in {"/", "safe"}:
            probe_calls.append(path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "stat", tracked_stat)
    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)

    inspection = _inspect(
        home,
        _descriptor(roots=(_home_directory("safe"),)),
    )

    assert inspection.data_status is DataStatus.REJECTED
    assert inspection.warning_codes == (ProbeWarningCode.UNSUPPORTED_FORMAT,)
    assert probe_calls == []


def test_expired_deadline_wins_over_missing_platform_capability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delattr(filesystem_module.os, "O_DIRECTORY")

    inspection = _inspect(
        tmp_path.resolve(),
        _descriptor(roots=(_home_directory("safe"),)),
        clock=MutableClock(2.0),
        deadline=2.0,
    )

    assert inspection.data_status is DataStatus.MISSING
    assert inspection.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


def test_light_probe_reports_normal_absence_without_leaking_paths(tmp_path: Path) -> None:
    inspection = _inspect(
        tmp_path.resolve(),
        _descriptor(
            markers=(_home_directory("missing-app"),),
            roots=(_home_directory("missing-data"),),
        ),
    )

    assert inspection.installation_status is InstallationStatus.NOT_DETECTED
    assert inspection.data_status is DataStatus.MISSING
    assert inspection.metrics.checked_installation_marker_count == 1
    assert inspection.metrics.checked_data_root_count == 1
    assert inspection.metrics.missing_data_root_count == 1
    assert inspection.warning_codes == (ProbeWarningCode.SOURCE_MISSING,)
    assert "missing-app" not in repr(inspection)
    assert str(tmp_path) not in repr(inspection)


def test_detected_marker_with_all_data_roots_missing_has_one_aggregate_warning(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve()
    (home / "installed-app").mkdir()

    inspection = _inspect(
        home,
        _descriptor(
            markers=(_home_directory("installed-app"),),
            roots=(
                _home_directory("missing-data-a"),
                _home_directory("missing-data-b"),
            ),
        ),
    )

    assert inspection.installation_status is InstallationStatus.DETECTED
    assert inspection.data_status is DataStatus.MISSING
    assert inspection.metrics.missing_data_root_count == 2
    assert inspection.warning_codes == (ProbeWarningCode.SOURCE_MISSING,)


def test_all_missing_roots_add_source_missing_beside_a_separate_marker_warning(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve()
    (home / "unsafe-target").mkdir()
    (home / "unsafe-marker").symlink_to(home / "unsafe-target", target_is_directory=True)

    inspection = _inspect(
        home,
        _descriptor(
            markers=(_home_directory("unsafe-marker"),),
            roots=(_home_directory("missing-data"),),
        ),
    )

    assert inspection.metrics.checked_data_root_count == 1
    assert inspection.metrics.missing_data_root_count == 1
    assert inspection.warning_codes == (
        ProbeWarningCode.SOURCE_MISSING,
        ProbeWarningCode.SYMLINK_REJECTED,
    )


def test_empty_data_roots_use_complete_marker_absence_for_source_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path.resolve()
    (home / "installed-app").mkdir()

    absent = _inspect(
        home,
        _descriptor(markers=(_home_directory("missing-app"),)),
    )
    detected = _inspect(
        home,
        _descriptor(markers=(_home_directory("installed-app"),)),
    )

    assert absent.warning_codes == (ProbeWarningCode.SOURCE_MISSING,)
    assert detected.installation_status is InstallationStatus.DETECTED
    assert ProbeWarningCode.SOURCE_MISSING not in detected.warning_codes
