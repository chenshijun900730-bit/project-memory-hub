from __future__ import annotations

import errno
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import NoReturn, cast

import pytest

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes import filesystem as filesystem_module
from project_memory_hub.probes.base import (
    ExpectedPathType,
    ProbeClock,
    SourceDescriptor,
    TrustedAnchor,
    TrustedPath,
)
from project_memory_hub.probes.filesystem import PathSafetyPolicy, SafeProbeFilesystem
from project_memory_hub.probes.models import (
    DataStatus,
    ProbeBudget,
    ProbeCapability,
    ProbeWarningCode,
    StructureStatus,
)


SQLITE_MAGIC = b"SQLite format 3\x00"


class MutableClock(ProbeClock):
    def __init__(self, monotonic_value: float = 0.0) -> None:
        self.monotonic_value = monotonic_value

    def now(self) -> datetime:
        return datetime(2026, 7, 17, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.monotonic_value


def _descriptor(*root_components: str) -> SourceDescriptor:
    return SourceDescriptor(
        source_agent=SourceAgent.TRAE,
        installation_markers=(),
        data_roots=(
            TrustedPath(
                TrustedAnchor.HOME,
                root_components,
                ExpectedPathType.DIRECTORY,
            ),
        ),
        capability=ProbeCapability.STRUCTURE_METADATA,
        recognized_schemas=(),
    )


def _inspect(
    home: Path,
    *,
    root_components: tuple[str, ...] = (".trae",),
    budget: ProbeBudget | None = None,
    clock: ProbeClock | None = None,
    deadline: float = 3.0,
) -> filesystem_module.StructureInspection:
    return SafeProbeFilesystem(PathSafetyPolicy(home=home)).inspect_trae_structure(
        _descriptor(*root_components),
        budget=budget or ProbeBudget(),
        clock=clock or MutableClock(),
        deadline=deadline,
    )


@pytest.mark.parametrize(
    ("components", "expected"),
    [
        (("session_memory", "cache.db"), True),
        (("nested", "session_memory.sqlite"), True),
        (("nested", "Session_Memory.sqlite"), False),
        (("nested", "session_memory_backup.sqlite"), False),
        (("nested", "my_session_memory.sqlite"), False),
    ],
)
def test_session_memory_candidate_rule(components: tuple[str, ...], expected: bool) -> None:
    assert filesystem_module.is_session_memory_candidate(components) is expected


def test_structure_walk_does_not_open_non_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    non_candidates = (
        "Session_Memory.sqlite",
        "session_memory_backup.sqlite",
        "my_session_memory.sqlite",
        "ordinary.json",
    )
    for name in non_candidates:
        (root / name).write_bytes(b"private content must not be read")

    real_open = filesystem_module.os.open
    opened_non_candidates: list[str] = []
    scandir_arguments: list[int | str | bytes | os.PathLike[str]] = []
    real_scandir = filesystem_module.os.scandir

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        selected = os.fspath(path)
        if selected in non_candidates:
            opened_non_candidates.append(str(selected))
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def tracked_scandir(
        path: int | str | bytes | os.PathLike[str],
    ) -> os.ScandirIterator[str]:
        scandir_arguments.append(path)
        return real_scandir(path)

    def unexpected_pread(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("non-candidate content was read")

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "scandir", tracked_scandir)
    monkeypatch.setattr(filesystem_module.os, "pread", unexpected_pread)

    result = _inspect(home)

    assert result.data_status is DataStatus.READABLE
    assert result.structure_status is StructureStatus.UNSUPPORTED
    assert result.metrics.metadata_file_count == 0
    assert opened_non_candidates == []
    assert scandir_arguments and all(type(argument) is int for argument in scandir_arguments)


def test_json_jsonl_log_and_unknown_candidates_receive_header_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    candidate_names = (
        "session_memory.json",
        "session_memory.jsonl",
        "session_memory.log",
        "session_memory.bin",
    )
    for name in candidate_names:
        (root / name).write_bytes((name.encode() + b" private") * 16)

    real_pread = filesystem_module.os.pread
    calls: list[tuple[int, int, int]] = []

    def tracked_pread(fd: int, size: int, offset: int) -> bytes:
        calls.append((fd, size, offset))
        return real_pread(fd, size, offset)

    monkeypatch.setattr(filesystem_module.os, "pread", tracked_pread)

    result = _inspect(home)

    assert len(calls) == 4
    assert all(size == 64 and offset == 0 for _, size, offset in calls)
    assert result.metrics.metadata_file_count == 4
    assert result.metrics.sqlite_candidate_count == 0
    assert result.warning_codes == (ProbeWarningCode.UNSUPPORTED_FORMAT,)


def test_candidate_header_reads_never_exceed_per_file_and_total_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    candidate_directory = home / ".trae" / "session_memory"
    candidate_directory.mkdir(parents=True)
    for index in range(65):
        (candidate_directory / f"candidate-{index:02d}.data").write_bytes(b"x" * 128)

    real_pread = filesystem_module.os.pread
    calls: list[tuple[int, int, int]] = []

    def tracked_pread(fd: int, size: int, offset: int) -> bytes:
        calls.append((fd, size, offset))
        return real_pread(fd, size, offset)

    monkeypatch.setattr(filesystem_module.os, "pread", tracked_pread)

    result = _inspect(home)

    assert len(calls) == 64
    assert all(size <= 64 and offset == 0 for _, size, offset in calls)
    assert sum(size for _, size, _ in calls) <= 4_096
    assert result.metrics.metadata_file_count == 64
    assert ProbeWarningCode.BUDGET_EXCEEDED in result.warning_codes


def test_depth_budget_stops_before_opening_depth_five(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    current = home / ".trae"
    current.mkdir()
    for name in ("one", "two", "three", "four", "five"):
        current = current / name
        current.mkdir()
    (current / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)

    real_open = filesystem_module.os.open
    opened: list[str] = []

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        selected = os.fspath(path)
        if isinstance(selected, str):
            opened.append(selected)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)

    result = _inspect(home)

    assert "five" not in opened
    assert "session_memory.sqlite" not in opened
    assert result.metrics.metadata_file_count == 0
    assert ProbeWarningCode.BUDGET_EXCEEDED in result.warning_codes


class _TrackingScandir:
    def __init__(self, wrapped: os.ScandirIterator[str]) -> None:
        self._wrapped = wrapped
        self.next_calls = 0

    def __enter__(self) -> _TrackingScandir:
        self._wrapped.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        return self._wrapped.__exit__(exc_type, exc, traceback)

    def __iter__(self) -> _TrackingScandir:
        return self

    def __next__(self) -> os.DirEntry[str]:
        self.next_calls += 1
        return next(self._wrapped)


def test_entry_budget_never_requests_entry_2049(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    for index in range(2_049):
        (root / f"ordinary-{index:04d}.txt").touch()

    real_scandir = filesystem_module.os.scandir
    trackers: list[_TrackingScandir] = []

    def tracked_scandir(path: int) -> _TrackingScandir:
        tracker = _TrackingScandir(real_scandir(path))
        trackers.append(tracker)
        return tracker

    monkeypatch.setattr(filesystem_module.os, "scandir", tracked_scandir)

    result = _inspect(home)

    assert sum(tracker.next_calls for tracker in trackers) == 2_048
    assert ProbeWarningCode.BUDGET_EXCEEDED in result.warning_codes


def test_candidate_budget_never_opens_candidate_65(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    candidate_directory = home / ".trae" / "session_memory"
    candidate_directory.mkdir(parents=True)
    candidate_names = tuple(f"candidate-{index:02d}.data" for index in range(65))
    for name in candidate_names:
        (candidate_directory / name).write_bytes(b"not sqlite")

    real_open = filesystem_module.os.open
    candidate_open_flags: list[int] = []

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if os.fspath(path) in candidate_names:
            candidate_open_flags.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)

    result = _inspect(home)

    assert len(candidate_open_flags) == 64
    assert all(flags & os.O_NONBLOCK for flags in candidate_open_flags)
    assert result.metrics.metadata_file_count == 64
    assert ProbeWarningCode.BUDGET_EXCEEDED in result.warning_codes


class _DeadlineScandir:
    def __init__(self, entry: os.DirEntry[str], clock: MutableClock, timing: str) -> None:
        self._entry = entry
        self._clock = clock
        self._timing = timing
        self.next_calls = 0
        self._returned = False

    def __enter__(self) -> _DeadlineScandir:
        if self._timing == "before":
            self._clock.monotonic_value = 3.0
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    def __iter__(self) -> _DeadlineScandir:
        return self

    def __next__(self) -> os.DirEntry[str]:
        self.next_calls += 1
        if self._returned:
            raise StopIteration
        self._returned = True
        if self._timing == "after":
            self._clock.monotonic_value = 3.0
        return self._entry


@pytest.mark.parametrize(("timing", "expected_next_calls"), [("before", 0), ("after", 1)])
def test_structure_checks_deadline_before_and_after_each_entry_next(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    timing: str,
    expected_next_calls: int,
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    (root / "ordinary.txt").touch()
    real_scandir = filesystem_module.os.scandir
    with real_scandir(root) as iterator:
        entry = next(iterator)
    clock = MutableClock()
    tracker = _DeadlineScandir(entry, clock, timing)
    monkeypatch.setattr(filesystem_module.os, "scandir", lambda _fd: tracker)

    result = _inspect(
        home,
        clock=clock,
        budget=ProbeBudget(max_entries=1),
    )

    assert tracker.next_calls == expected_next_calls
    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


@pytest.mark.parametrize("swap_kind", ["symlink", "directory", "file"])
def test_structure_walk_rejects_symlink_and_preview_to_open_swaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_kind: str,
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    expected_warning: ProbeWarningCode
    if swap_kind == "symlink":
        target = root / "real.sqlite"
        target.write_bytes(SQLITE_MAGIC)
        (root / "session_memory.sqlite").symlink_to(target)
        expected_warning = ProbeWarningCode.SYMLINK_REJECTED
    elif swap_kind == "directory":
        nested = root / "nested"
        nested.mkdir()
        (nested / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
        expected_warning = ProbeWarningCode.SOURCE_CHANGED
    else:
        (root / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
        expected_warning = ProbeWarningCode.SOURCE_CHANGED

    if swap_kind in {"directory", "file"}:
        selected_leaf = "nested" if swap_kind == "directory" else "session_memory.sqlite"
        real_stat = filesystem_module.os.stat
        changed_once = False

        def changed_stat(
            path: str | bytes | os.PathLike[str],
            *,
            dir_fd: int | None = None,
            follow_symlinks: bool = True,
        ) -> os.stat_result:
            nonlocal changed_once
            value = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if path == selected_leaf and dir_fd is not None and not changed_once:
                changed_once = True
                fields = list(value)
                fields[1] += 1
                return os.stat_result(fields)
            return value

        monkeypatch.setattr(filesystem_module.os, "stat", changed_stat)

    result = _inspect(home)

    assert expected_warning in result.warning_codes
    assert result.metrics.sqlite_candidate_count == 0


@pytest.mark.parametrize(
    "invalid_name",
    [
        "",
        ".",
        "..",
        "slash/name",
        "nul\x00name",
        "delete\x7fcontrol",
        b"not-text",
        "invalid-\udcff",
        "line\nbreak",
        "unicode\u0085control",
        "é" * 128,
    ],
)
def test_structure_walk_rejects_invalid_utf8_control_and_oversized_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    invalid_name: object,
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    opened_names: list[str] = []
    real_open = filesystem_module.os.open

    class InvalidEntry:
        name = invalid_name

        def stat(self, *, follow_symlinks: bool = True) -> NoReturn:
            raise AssertionError("invalid entry name reached stat")

    class InvalidScandir:
        def __init__(self) -> None:
            self._entries = iter((InvalidEntry(),))

        def __enter__(self) -> InvalidScandir:
            return self

        def __exit__(
            self,
            _exc_type: type[BaseException] | None,
            _exc: BaseException | None,
            _traceback: TracebackType | None,
        ) -> None:
            return None

        def __iter__(self) -> Iterator[InvalidEntry]:
            return self

        def __next__(self) -> InvalidEntry:
            return next(self._entries)

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        selected = os.fspath(path)
        if isinstance(selected, str):
            opened_names.append(selected)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def unexpected_pread(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("invalid entry content was read")

    monkeypatch.setattr(filesystem_module.os, "scandir", lambda _fd: InvalidScandir())
    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "pread", unexpected_pread)

    result = _inspect(home)

    assert invalid_name not in opened_names
    assert ProbeWarningCode.INVALID_UTF8 in result.warning_codes


def test_structure_walk_accepts_exactly_255_utf8_bytes(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    valid_name = "é" * 127 + "a"
    assert len(valid_name.encode("utf-8")) == 255
    (root / valid_name).touch()

    result = _inspect(home)

    assert ProbeWarningCode.INVALID_UTF8 not in result.warning_codes


def test_candidate_identity_is_rechecked_after_header_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    candidate = root / "session_memory.sqlite"
    candidate.write_bytes(SQLITE_MAGIC + b"x" * 64)
    real_pread = filesystem_module.os.pread

    def changing_pread(fd: int, size: int, offset: int) -> bytes:
        value = real_pread(fd, size, offset)
        current = candidate.stat().st_mtime_ns
        os.utime(candidate, ns=(current + 1_000_000, current + 1_000_000))
        return value

    monkeypatch.setattr(filesystem_module.os, "pread", changing_pread)

    result = _inspect(home)

    assert ProbeWarningCode.SOURCE_CHANGED in result.warning_codes
    assert result.metrics.sqlite_candidate_count == 0


def test_structure_candidate_open_fails_closed_without_nonblock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    (root / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
    opened_candidate = False
    real_open = filesystem_module.os.open

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal opened_candidate
        if path == "session_memory.sqlite":
            opened_candidate = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "O_NONBLOCK", 0)
    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)

    result = _inspect(home)

    assert opened_candidate is False
    assert result.metrics.metadata_file_count == 0
    assert ProbeWarningCode.UNSUPPORTED_FORMAT in result.warning_codes


@pytest.mark.parametrize("exit_kind", ["timeout", "partial_failure"])
def test_structure_walk_closes_all_fds_after_timeout_and_partial_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_kind: str,
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    (root / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
    clock = MutableClock()
    real_open = filesystem_module.os.open
    real_dup = filesystem_module.os.dup
    real_close = filesystem_module.os.close
    owned_fds: set[int] = set()

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        owned_fds.add(fd)
        return fd

    def tracked_dup(fd: int) -> int:
        duplicated = real_dup(fd)
        owned_fds.add(duplicated)
        return duplicated

    def tracked_close(fd: int) -> None:
        real_close(fd)
        owned_fds.discard(fd)

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "dup", tracked_dup)
    monkeypatch.setattr(filesystem_module.os, "close", tracked_close)

    if exit_kind == "timeout":
        real_scandir = filesystem_module.os.scandir

        class TimeoutScandir(_TrackingScandir):
            def __next__(self) -> os.DirEntry[str]:
                entry = super().__next__()
                clock.monotonic_value = 3.0
                return entry

        monkeypatch.setattr(
            filesystem_module.os,
            "scandir",
            lambda fd: TimeoutScandir(real_scandir(fd)),
        )
    else:

        def failed_pread(_fd: int, _size: int, _offset: int) -> NoReturn:
            raise OSError(errno.EACCES, "private operating system detail")

        monkeypatch.setattr(filesystem_module.os, "pread", failed_pread)

    result = _inspect(home, clock=clock)

    assert owned_fds == set()
    expected = (
        ProbeWarningCode.PROBE_TIMEOUT
        if exit_kind == "timeout"
        else ProbeWarningCode.PERMISSION_BLOCKED
    )
    assert expected in result.warning_codes


@pytest.mark.parametrize(
    "leaf",
    ["", ".", "..", "/etc/passwd", "../secret", "nested/secret", "nul\x00name", b"bytes"],
)
def test_candidate_rejects_untrusted_leaf_before_any_filesystem_call(leaf: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        filesystem_module.Candidate(
            parent_fd=0,
            leaf=leaf,  # type: ignore[arg-type]
            relative_components=(leaf,),  # type: ignore[arg-type]
            preview_identity=(1, 2, 3, 4, 5),
        )


def test_candidate_rejects_mismatched_or_mutable_metadata() -> None:
    with pytest.raises((TypeError, ValueError)):
        filesystem_module.Candidate(
            parent_fd=0,
            leaf="session_memory.sqlite",
            relative_components=("different.sqlite",),
            preview_identity=(1, 2, 3, 4, 5),
        )
    with pytest.raises((TypeError, ValueError)):
        filesystem_module.Candidate(
            parent_fd=0,
            leaf="session_memory.sqlite",
            relative_components=["session_memory.sqlite"],  # type: ignore[arg-type]
            preview_identity=(1, 2, 3, 4, 5),
        )
    with pytest.raises((TypeError, ValueError)):
        filesystem_module.Candidate(
            parent_fd=0,
            leaf="session_memory.sqlite",
            relative_components=("session_memory.sqlite",),
            preview_identity=(1, 2, 3, 4, True),
        )


def test_failed_candidate_dup_still_consumes_candidate_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    candidate_directory = home / ".trae" / "session_memory"
    candidate_directory.mkdir(parents=True)
    for index in range(5):
        (candidate_directory / f"candidate-{index}.data").touch()
    dup_calls = 0

    def failed_dup(_fd: int) -> NoReturn:
        nonlocal dup_calls
        dup_calls += 1
        raise OSError(errno.EMFILE, "secret fd detail")

    monkeypatch.setattr(filesystem_module.os, "dup", failed_dup)

    result = _inspect(home, budget=ProbeBudget(max_candidate_files=2))

    assert dup_calls == 2
    assert ProbeWarningCode.BUDGET_EXCEEDED in result.warning_codes


def test_failed_pread_still_consumes_total_header_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    candidate_directory = home / ".trae" / "session_memory"
    candidate_directory.mkdir(parents=True)
    for index in range(4):
        (candidate_directory / f"candidate-{index}.data").write_bytes(b"private")
    requested: list[int] = []

    def failed_pread(_fd: int, size: int, _offset: int) -> NoReturn:
        requested.append(size)
        raise OSError(errno.EACCES, "secret content path")

    monkeypatch.setattr(filesystem_module.os, "pread", failed_pread)

    result = _inspect(
        home,
        budget=ProbeBudget(max_header_bytes=4, max_total_header_bytes=8),
    )

    assert requested == [4, 4]
    assert sum(requested) == 8
    assert ProbeWarningCode.BUDGET_EXCEEDED in result.warning_codes


@pytest.mark.parametrize("phase", ["scandir", "next", "dup", "pread"])
def test_structure_syscall_error_crossing_deadline_returns_only_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    (root / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
    clock = MutableClock()
    real_scandir = filesystem_module.os.scandir

    class FailingNext:
        def __init__(self, wrapped: os.ScandirIterator[str]) -> None:
            self._wrapped = wrapped

        def __enter__(self) -> FailingNext:
            self._wrapped.__enter__()
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None,
            exc: BaseException | None,
            traceback: TracebackType | None,
        ) -> bool | None:
            return self._wrapped.__exit__(exc_type, exc, traceback)

        def __iter__(self) -> FailingNext:
            return self

        def __next__(self) -> os.DirEntry[str]:
            clock.monotonic_value = 3.0
            raise OSError(errno.EACCES, "secret next detail")

    if phase == "scandir":

        def failed_scandir(_fd: int) -> NoReturn:
            clock.monotonic_value = 3.0
            raise OSError(errno.EACCES, "secret scandir detail")

        monkeypatch.setattr(filesystem_module.os, "scandir", failed_scandir)
    elif phase == "next":
        monkeypatch.setattr(
            filesystem_module.os,
            "scandir",
            lambda fd: FailingNext(real_scandir(fd)),
        )
    elif phase == "dup":

        def failed_dup(_fd: int) -> NoReturn:
            clock.monotonic_value = 3.0
            raise OSError(errno.EACCES, "secret dup detail")

        monkeypatch.setattr(filesystem_module.os, "dup", failed_dup)
    else:

        def failed_pread(_fd: int, _size: int, _offset: int) -> NoReturn:
            clock.monotonic_value = 3.0
            raise OSError(errno.EACCES, "secret pread detail")

        monkeypatch.setattr(filesystem_module.os, "pread", failed_pread)

    result = _inspect(home, clock=clock)

    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


def _changed_identity(value: os.stat_result, field: str) -> os.stat_result:
    selected = {
        "st_dev": value.st_dev,
        "st_ino": value.st_ino,
        "st_mode": value.st_mode,
        "st_size": value.st_size,
        "st_mtime_ns": value.st_mtime_ns,
    }
    selected[field] += 1
    return cast(os.stat_result, SimpleNamespace(**selected))


@pytest.mark.parametrize("field", ["st_size", "st_mtime_ns"])
@pytest.mark.parametrize("phase", ["before", "opened", "after"])
def test_structure_directory_uses_full_four_way_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    phase: str,
) -> None:
    home = tmp_path.resolve()
    nested = home / ".trae" / "nested"
    nested.mkdir(parents=True)
    (nested / "ordinary.txt").touch()
    real_open = filesystem_module.os.open
    real_stat = filesystem_module.os.stat
    real_fstat = filesystem_module.os.fstat
    nested_fds: set[int] = set()
    nested_stat_calls = 0

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == "nested":
            nested_fds.add(fd)
        return fd

    def changed_fstat(fd: int) -> os.stat_result:
        value = real_fstat(fd)
        return _changed_identity(value, field) if phase == "opened" and fd in nested_fds else value

    def changed_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal nested_stat_calls
        value = real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        if path == "nested" and dir_fd is not None:
            nested_stat_calls += 1
            if (phase == "before" and nested_stat_calls == 1) or (
                phase == "after" and nested_stat_calls == 2
            ):
                return _changed_identity(value, field)
        return value

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", changed_fstat)
    monkeypatch.setattr(filesystem_module.os, "stat", changed_stat)

    result = _inspect(home)

    assert ProbeWarningCode.SOURCE_CHANGED in result.warning_codes


def test_structure_localizes_exception_text_but_does_not_swallow_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / ".trae").mkdir()

    def failed_scandir(_fd: int) -> NoReturn:
        raise RuntimeError("SECRET /private/source/session_memory")

    monkeypatch.setattr(filesystem_module.os, "scandir", failed_scandir)
    result = _inspect(home)
    assert result.warning_codes == (ProbeWarningCode.PROBE_FAILED,)
    assert "SECRET" not in repr(result)
    assert "/private/source" not in repr(result)

    def interrupted_scandir(_fd: int) -> NoReturn:
        raise KeyboardInterrupt

    monkeypatch.setattr(filesystem_module.os, "scandir", interrupted_scandir)
    with pytest.raises(KeyboardInterrupt):
        _inspect(home)


def test_structure_failure_in_one_root_does_not_skip_a_later_root(tmp_path: Path) -> None:
    home = tmp_path.resolve()
    later_root = home / ".trae" / "available"
    later_root.mkdir(parents=True)
    (later_root / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
    descriptor = SourceDescriptor(
        source_agent=SourceAgent.TRAE,
        installation_markers=(),
        data_roots=(
            TrustedPath(
                TrustedAnchor.HOME,
                (".trae", "missing"),
                ExpectedPathType.DIRECTORY,
            ),
            TrustedPath(
                TrustedAnchor.HOME,
                (".trae", "available"),
                ExpectedPathType.DIRECTORY,
            ),
        ),
        capability=ProbeCapability.STRUCTURE_METADATA,
        recognized_schemas=(),
    )

    result = SafeProbeFilesystem(PathSafetyPolicy(home=home)).inspect_trae_structure(
        descriptor,
        budget=ProbeBudget(),
        clock=MutableClock(),
        deadline=3.0,
    )

    assert result.data_status is DataStatus.READABLE
    assert result.metrics.sqlite_candidate_count == 1
    assert ProbeWarningCode.SOURCE_MISSING in result.warning_codes


def test_structure_checks_deadline_after_root_fd_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / ".trae").mkdir()
    clock = MutableClock()
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    root_fds: set[int] = set()

    def tracked_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == ".trae":
            root_fds.add(fd)
        return fd

    def deadline_crossing_close(fd: int) -> None:
        is_root = fd in root_fds
        real_close(fd)
        root_fds.discard(fd)
        if is_root:
            clock.monotonic_value = 3.0

    monkeypatch.setattr(filesystem_module.os, "open", tracked_open)
    monkeypatch.setattr(filesystem_module.os, "close", deadline_crossing_close)

    result = _inspect(home, clock=clock)

    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
    assert root_fds == set()


@pytest.mark.parametrize("stat_call", [2, 3])
def test_candidate_disappearance_after_open_is_source_changed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stat_call: int,
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    (root / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
    real_stat = filesystem_module.os.stat
    candidate_stat_calls = 0

    def disappearing_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        nonlocal candidate_stat_calls
        if path == "session_memory.sqlite" and dir_fd is not None:
            candidate_stat_calls += 1
            if candidate_stat_calls == stat_call:
                raise OSError(errno.ENOENT, "private race detail")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_module.os, "stat", disappearing_stat)

    result = _inspect(home)

    assert ProbeWarningCode.SOURCE_CHANGED in result.warning_codes
    assert result.metrics.sqlite_candidate_count == 0


def test_successful_candidate_dup_crossing_deadline_is_closed_and_times_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    (root / "session_memory.sqlite").write_bytes(SQLITE_MAGIC)
    clock = MutableClock()
    real_dup = filesystem_module.os.dup
    real_close = filesystem_module.os.close
    duplicated_fds: set[int] = set()

    def deadline_crossing_dup(fd: int) -> int:
        duplicated = real_dup(fd)
        duplicated_fds.add(duplicated)
        clock.monotonic_value = 3.0
        return duplicated

    def tracked_close(fd: int) -> None:
        real_close(fd)
        duplicated_fds.discard(fd)

    monkeypatch.setattr(filesystem_module.os, "dup", deadline_crossing_dup)
    monkeypatch.setattr(filesystem_module.os, "close", tracked_close)

    result = _inspect(home, clock=clock)

    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
    assert result.metrics.metadata_file_count == 0
    assert duplicated_fds == set()


def test_exhausted_scandir_checks_deadline_after_stop_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / ".trae").mkdir()
    clock = MutableClock()
    real_scandir = filesystem_module.os.scandir

    class DeadlineOnExhaustion(_TrackingScandir):
        def __next__(self) -> os.DirEntry[str]:
            try:
                return super().__next__()
            except StopIteration:
                clock.monotonic_value = 3.0
                raise

    monkeypatch.setattr(
        filesystem_module.os,
        "scandir",
        lambda fd: DeadlineOnExhaustion(real_scandir(fd)),
    )

    result = _inspect(home, clock=clock)

    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
