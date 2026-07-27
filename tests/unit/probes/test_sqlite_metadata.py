from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn, cast

import pytest

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes import filesystem as filesystem_module
from project_memory_hub.probes.base import (
    ExpectedPathType,
    ProbeClock,
    RecognizedSchema,
    SourceDescriptor,
    TrustedAnchor,
    TrustedPath,
)
from project_memory_hub.probes.filesystem import (
    Candidate,
    PathSafetyPolicy,
    SafeProbeFilesystem,
    SqliteMetadataInspector,
    _StructureCounters,
)
from project_memory_hub.probes.models import (
    ProbeBudget,
    ProbeCapability,
    ProbeWarningCode,
    StructureStatus,
)


class MutableClock(ProbeClock):
    def __init__(self, monotonic_value: float = 0.0) -> None:
        self.monotonic_value = monotonic_value

    def now(self) -> datetime:
        return datetime(2026, 7, 17, tzinfo=UTC)

    def monotonic(self) -> float:
        return self.monotonic_value


class RecordingConnection:
    def __init__(
        self,
        wrapped: sqlite3.Connection,
        *,
        on_execute: Callable[[str], None] | None = None,
    ) -> None:
        self._wrapped = wrapped
        self._on_execute = on_execute
        self.statements: list[tuple[str, tuple[object, ...]]] = []
        self.extension_flags: list[bool] = []
        self.progress_handlers: list[tuple[object, int]] = []
        self.limit_calls: list[tuple[int, int]] = []
        self.closed = False

    def enable_load_extension(self, enabled: bool) -> None:
        self.extension_flags.append(enabled)
        self._wrapped.enable_load_extension(enabled)

    def set_progress_handler(self, callback: object, instructions: int) -> None:
        self.progress_handlers.append((callback, instructions))
        self._wrapped.set_progress_handler(callback, instructions)  # type: ignore[arg-type]

    def setlimit(self, category: int, limit: int) -> int:
        self.limit_calls.append((category, limit))
        return self._wrapped.setlimit(category, limit)

    def getlimit(self, category: int) -> int:
        return self._wrapped.getlimit(category)

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        self.statements.append((statement, parameters))
        if self._on_execute is not None:
            self._on_execute(statement)
        return self._wrapped.execute(statement, parameters)

    def close(self) -> None:
        self.closed = True
        self._wrapped.close()


class StreamingRows:
    def __init__(self, rows: Iterator[tuple[object, ...]]) -> None:
        self._rows = rows
        self.next_calls = 0
        self.closed = False

    def __iter__(self) -> StreamingRows:
        return self

    def __next__(self) -> tuple[object, ...]:
        self.next_calls += 1
        return next(self._rows)

    def close(self) -> None:
        self.closed = True


class StreamingTableConnection(RecordingConnection):
    def __init__(self, wrapped: sqlite3.Connection, table_rows: StreamingRows) -> None:
        super().__init__(wrapped)
        self.table_rows = table_rows

    def execute(
        self,
        statement: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        self.statements.append((statement, parameters))
        if "pragma_table_list" in statement:
            return cast(sqlite3.Cursor, self.table_rows)
        return self._wrapped.execute(statement, parameters)


def _create_database(
    directory: Path,
    *,
    ddl: str = "CREATE TABLE synthetic(id INTEGER PRIMARY KEY)",
    rows: tuple[tuple[object, ...], ...] = (),
    insert_sql: str | None = None,
) -> Path:
    database = directory / "session_memory.sqlite"
    connection = sqlite3.connect(database)
    try:
        connection.execute(ddl)
        if insert_sql is not None:
            connection.executemany(insert_sql, rows)
        connection.commit()
    finally:
        connection.close()
    return database


def _candidate(database: Path) -> tuple[Candidate, int]:
    parent_fd = os.open(database.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    preview = os.stat(database.name, dir_fd=parent_fd, follow_symlinks=False)
    return (
        Candidate(
            parent_fd=parent_fd,
            leaf=database.name,
            relative_components=(database.name,),
            preview_identity=filesystem_module._identity(preview),
        ),
        parent_fd,
    )


def _patch_matching_dev_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    real_stat = filesystem_module.os.stat
    real_fstat = filesystem_module.os.fstat

    def matching_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        selected = os.fspath(path)
        if isinstance(selected, str) and re.fullmatch(r"/dev/fd/[0-9]+", selected):
            return real_fstat(int(selected.rsplit("/", 1)[1]))
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_module.os, "stat", matching_stat)


def _patch_mismatched_dev_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    real_stat = filesystem_module.os.stat
    real_fstat = filesystem_module.os.fstat

    def mismatching_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        selected = os.fspath(path)
        if isinstance(selected, str) and re.fullmatch(r"/dev/fd/[0-9]+", selected):
            opened = real_fstat(int(selected.rsplit("/", 1)[1]))
            return cast(
                os.stat_result,
                SimpleNamespace(
                    st_dev=opened.st_dev + 1,
                    st_ino=opened.st_ino,
                    st_mode=opened.st_mode,
                ),
            )
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_module.os, "stat", mismatching_stat)


def _record_connections(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_execute: Callable[[str], None] | None = None,
) -> tuple[list[tuple[str, dict[str, object]]], list[RecordingConnection]]:
    real_connect = filesystem_module.sqlite3.connect
    calls: list[tuple[str, dict[str, object]]] = []
    connections: list[RecordingConnection] = []

    def tracking_connect(
        database: str,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        calls.append((database, dict(kwargs)))
        wrapped = real_connect(database, *args, **kwargs)
        recorded = RecordingConnection(wrapped, on_execute=on_execute)
        connections.append(recorded)
        return cast(sqlite3.Connection, recorded)

    monkeypatch.setattr(filesystem_module.sqlite3, "connect", tracking_connect)
    return calls, connections


def _inspect(
    candidate: Candidate,
    *,
    recognized_schemas: tuple[RecognizedSchema, ...] = (),
    budget: ProbeBudget | None = None,
    clock: ProbeClock | None = None,
    deadline: float = 3.0,
    counters: _StructureCounters | None = None,
):
    return SqliteMetadataInspector(
        budget or ProbeBudget(),
        clock or MutableClock(),
    ).inspect(
        candidate,
        recognized_schemas=recognized_schemas,
        deadline=deadline,
        counters=counters,
    )


def _reviewed_schema_fingerprint() -> str:
    rows: list[tuple[object, ...]] = [
        ("table", "main", "reviewed_metadata", "table", 2, 0, 0),
        ("column", "reviewed_metadata", 0, "session_id", "TEXT", 0, None, 0, 0),
        ("column", "reviewed_metadata", 1, "model_id", "TEXT", 0, None, 0, 0),
    ]
    rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    document = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(document.encode('utf-8')).hexdigest()}"


def _reviewed_schema(*, count_query: str | None = None) -> RecognizedSchema:
    return RecognizedSchema(
        fingerprint=_reviewed_schema_fingerprint(),
        session_identifier_fields=frozenset({"session_id"}),
        model_identifier_fields=frozenset({"model_id"}),
        bounded_count_query=count_query,
    )


def test_sqlite_connects_only_through_verified_dev_fd_with_safe_open_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    calls, connections = _record_connections(monkeypatch)
    real_open = filesystem_module.os.open
    candidate_flags: list[int] = []

    def tracking_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == candidate.leaf and dir_fd == candidate.parent_fd:
            candidate_flags.append(flags)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "open", tracking_open)
    directory_entries_before = {path.name for path in tmp_path.iterdir()}
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert result.structure_status is StructureStatus.UNSUPPORTED
    assert len(calls) == 1
    uri, options = calls[0]
    assert re.fullmatch(r"file:/dev/fd/[0-9]+\?mode=ro&immutable=1", uri)
    assert str(tmp_path) not in uri
    assert options["uri"] is True
    assert options["timeout"] == 0.0
    assert len(candidate_flags) == 1
    assert candidate_flags[0] & os.O_NOFOLLOW
    assert candidate_flags[0] & os.O_CLOEXEC
    assert candidate_flags[0] & os.O_NONBLOCK
    assert candidate_flags[0] & os.O_ACCMODE == os.O_RDONLY
    assert connections[0].closed is True
    assert {path.name for path in tmp_path.iterdir()} == directory_entries_before


def test_dev_fd_identity_mismatch_never_connects_or_falls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_mismatched_dev_fd(monkeypatch)
    calls, _connections = _record_connections(monkeypatch)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert calls == []
    assert result.structure_status is StructureStatus.UNSUPPORTED
    assert result.warning_codes == (ProbeWarningCode.UNSUPPORTED_FORMAT,)
    assert str(database) not in repr(result)


def test_dev_fd_identity_deadline_after_fstat_stops_before_dev_fd_stat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    clock = MutableClock()
    real_open = filesystem_module.os.open
    real_fstat = filesystem_module.os.fstat
    real_stat = filesystem_module.os.stat
    candidate_fds: set[int] = set()
    candidate_fstat_calls = 0
    dev_fd_stat_calls: list[str] = []

    def tracking_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == candidate.leaf and dir_fd == candidate.parent_fd:
            candidate_fds.add(fd)
        return fd

    def deadline_crossing_fstat(fd: int) -> os.stat_result:
        nonlocal candidate_fstat_calls
        value = real_fstat(fd)
        if fd in candidate_fds:
            candidate_fstat_calls += 1
            if candidate_fstat_calls == 2:
                clock.monotonic_value = 3.0
        return value

    def tracking_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        selected = os.fspath(path)
        if isinstance(selected, str) and re.fullmatch(r"/dev/fd/[0-9]+", selected):
            dev_fd_stat_calls.append(selected)
            return real_fstat(int(selected.rsplit("/", 1)[1]))
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_module.os, "open", tracking_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", deadline_crossing_fstat)
    monkeypatch.setattr(filesystem_module.os, "stat", tracking_stat)
    try:
        result = _inspect(candidate, clock=clock)
    finally:
        os.close(parent_fd)

    assert candidate_fstat_calls == 2
    assert dev_fd_stat_calls == []
    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("sidecar_kind", ["file", "symlink", "fifo"])
def test_sqlite_rejects_any_sidecar_type_before_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    suffix: str,
    sidecar_kind: str,
) -> None:
    database = _create_database(tmp_path)
    sidecar = database.with_name(database.name + suffix)
    if sidecar_kind == "file":
        sidecar.touch()
    elif sidecar_kind == "symlink":
        sidecar.symlink_to(database.name)
    else:
        os.mkfifo(sidecar)
    candidate, parent_fd = _candidate(database)
    calls, _connections = _record_connections(monkeypatch)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert calls == []
    assert ProbeWarningCode.SOURCE_CHANGED in result.warning_codes


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_sqlite_rejects_sidecar_created_after_schema_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    created = False

    def create_sidecar(statement: str) -> None:
        nonlocal created
        if "pragma_table_xinfo" in statement and not created:
            created = True
            database.with_name(database.name + suffix).touch()

    calls, _connections = _record_connections(monkeypatch, on_execute=create_sidecar)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert len(calls) == 1
    assert ProbeWarningCode.SOURCE_CHANGED in result.warning_codes
    assert result.metrics.bounded_record_count is None


def test_fifth_sqlite_candidate_is_never_opened_or_connected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    counters = _StructureCounters(sqlite_candidates=4)

    def forbidden_open(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("the fifth SQLite candidate was opened")

    def forbidden_connect(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("the fifth SQLite candidate was connected")

    monkeypatch.setattr(filesystem_module.os, "open", forbidden_open)
    monkeypatch.setattr(filesystem_module.sqlite3, "connect", forbidden_connect)
    try:
        result = _inspect(candidate, counters=counters)
    finally:
        os.close(parent_fd)

    assert result.warning_codes == (ProbeWarningCode.BUDGET_EXCEEDED,)
    assert counters.sqlite_candidates == 4


def test_sqlite_single_and_total_file_byte_limits_stop_before_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    original, parent_fd = _candidate(database)
    calls, _connections = _record_connections(monkeypatch)
    too_large_identity = (
        original.preview_identity[0],
        original.preview_identity[1],
        original.preview_identity[2],
        ProbeBudget().max_sqlite_file_bytes + 1,
        original.preview_identity[4],
    )
    too_large = Candidate(
        parent_fd=original.parent_fd,
        leaf=original.leaf,
        relative_components=original.relative_components,
        preview_identity=too_large_identity,
    )
    try:
        single = _inspect(too_large)
        total_counters = _StructureCounters(
            sqlite_total_bytes=ProbeBudget().max_sqlite_total_bytes
            - original.preview_identity[3]
            + 1
        )
        total = _inspect(original, counters=total_counters)
    finally:
        os.close(parent_fd)

    assert calls == []
    assert single.warning_codes == (ProbeWarningCode.BUDGET_EXCEEDED,)
    assert total.warning_codes == (ProbeWarningCode.BUDGET_EXCEEDED,)


def test_failed_connect_still_consumes_candidate_and_file_byte_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    counters = _StructureCounters()

    def failed_connect(*_args: object, **_kwargs: object) -> NoReturn:
        raise sqlite3.OperationalError("SECRET original database path")

    monkeypatch.setattr(filesystem_module.sqlite3, "connect", failed_connect)
    try:
        result = _inspect(candidate, counters=counters)
    finally:
        os.close(parent_fd)

    assert counters.sqlite_candidates == 1
    assert counters.sqlite_total_bytes == candidate.preview_identity[3]
    assert result.warning_codes == (ProbeWarningCode.UNSUPPORTED_FORMAT,)
    assert "SECRET" not in repr(result)


def test_failed_candidate_open_still_consumes_candidate_and_file_byte_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    counters = _StructureCounters()
    real_open = filesystem_module.os.open

    def failed_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if path == candidate.leaf and dir_fd == candidate.parent_fd:
            raise OSError(errno.EACCES, "SECRET candidate path")
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(filesystem_module.os, "open", failed_open)
    try:
        result = _inspect(candidate, counters=counters)
    finally:
        os.close(parent_fd)

    assert counters.sqlite_candidates == 1
    assert counters.sqlite_total_bytes == candidate.preview_identity[3]
    assert result.warning_codes == (ProbeWarningCode.PERMISSION_BLOCKED,)


def test_sqlite_preview_to_open_identity_swap_never_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    real_open = filesystem_module.os.open
    real_fstat = filesystem_module.os.fstat
    candidate_fds: set[int] = set()
    calls, _connections = _record_connections(monkeypatch)

    def tracking_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == candidate.leaf and dir_fd == candidate.parent_fd:
            candidate_fds.add(fd)
        return fd

    def changed_fstat(fd: int) -> os.stat_result:
        value = real_fstat(fd)
        if fd not in candidate_fds:
            return value
        return cast(
            os.stat_result,
            SimpleNamespace(
                st_dev=value.st_dev,
                st_ino=value.st_ino + 1,
                st_mode=value.st_mode,
                st_size=value.st_size,
                st_mtime_ns=value.st_mtime_ns,
            ),
        )

    monkeypatch.setattr(filesystem_module.os, "open", tracking_open)
    monkeypatch.setattr(filesystem_module.os, "fstat", changed_fstat)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert calls == []
    assert ProbeWarningCode.SOURCE_CHANGED in result.warning_codes


def test_sqlite_hardening_precedes_schema_queries_and_avoids_disk_sorting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    _calls, connections = _record_connections(monkeypatch)
    try:
        _inspect(candidate)
    finally:
        os.close(parent_fd)

    connection = connections[0]
    assert connection.extension_flags == [False]
    assert connection.limit_calls == [
        (sqlite3.SQLITE_LIMIT_LENGTH, 4_096),
        (sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4_096),
    ]
    statements = [statement for statement, _parameters in connection.statements]
    schema_index = next(
        index for index, statement in enumerate(statements) if "pragma_table_list" in statement
    )
    hardening = "\n".join(statements[:schema_index]).casefold()
    assert "pragma query_only = on" in hardening
    assert "pragma trusted_schema = off" in hardening
    assert "pragma busy_timeout = 0" in hardening
    assert "pragma temp_store = memory" in hardening
    assert all("order by" not in statement.casefold() for statement in statements)


def test_missing_extension_toggle_continues_hardening_and_schema_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    real_connect = filesystem_module.sqlite3.connect
    connections: list[RecordingConnection] = []

    class MissingExtensionToggleConnection(RecordingConnection):
        def enable_load_extension(self, enabled: bool) -> None:
            self.extension_flags.append(enabled)
            raise AttributeError("extension loading is unavailable")

    def tracking_connect(
        database_name: str,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        wrapped = real_connect(database_name, *args, **kwargs)
        connection = MissingExtensionToggleConnection(wrapped)
        connections.append(connection)
        return cast(sqlite3.Connection, connection)

    monkeypatch.setattr(filesystem_module.sqlite3, "connect", tracking_connect)
    try:
        _inspect(candidate)
    finally:
        os.close(parent_fd)

    connection = connections[0]
    assert connection.extension_flags == [False]
    statements = [statement for statement, _parameters in connection.statements]
    assert any("pragma_table_list" in statement for statement in statements)
    hardening = "\n".join(statements).casefold()
    assert "pragma query_only = on" in hardening
    assert "pragma trusted_schema = off" in hardening
    assert "pragma busy_timeout = 0" in hardening
    assert "pragma temp_store = memory" in hardening


@pytest.mark.parametrize(
    ("deadline_trigger", "expected_operations"),
    [
        (
            "setlimit",
            (("setlimit", sqlite3.SQLITE_LIMIT_LENGTH),),
        ),
        (
            "getlimit",
            (
                ("setlimit", sqlite3.SQLITE_LIMIT_LENGTH),
                ("getlimit", sqlite3.SQLITE_LIMIT_LENGTH),
            ),
        ),
    ],
)
def test_runtime_limit_deadline_stops_before_next_limit_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    deadline_trigger: str,
    expected_operations: tuple[tuple[str, int], ...],
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    clock = MutableClock()
    _patch_matching_dev_fd(monkeypatch)
    real_connect = filesystem_module.sqlite3.connect
    connections: list[DeadlineLimitConnection] = []

    class DeadlineLimitConnection(RecordingConnection):
        def __init__(self, wrapped: sqlite3.Connection) -> None:
            super().__init__(wrapped)
            self.limit_operations: list[tuple[str, int]] = []

        def setlimit(self, category: int, limit: int) -> int:
            self.limit_operations.append(("setlimit", category))
            previous = super().setlimit(category, limit)
            if deadline_trigger == "setlimit" and category == sqlite3.SQLITE_LIMIT_LENGTH:
                clock.monotonic_value = 3.0
            return previous

        def getlimit(self, category: int) -> int:
            self.limit_operations.append(("getlimit", category))
            configured = super().getlimit(category)
            if deadline_trigger == "getlimit" and category == sqlite3.SQLITE_LIMIT_LENGTH:
                clock.monotonic_value = 3.0
            return configured

    def tracking_connect(
        name: str,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        wrapped = real_connect(name, *args, **kwargs)
        recorded = DeadlineLimitConnection(wrapped)
        connections.append(recorded)
        return cast(sqlite3.Connection, recorded)

    monkeypatch.setattr(filesystem_module.sqlite3, "connect", tracking_connect)
    try:
        result = _inspect(candidate, clock=clock)
    finally:
        os.close(parent_fd)

    assert len(connections) == 1
    assert tuple(connections[0].limit_operations) == expected_operations
    assert connections[0].closed is True
    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


def test_sqlite_queries_only_allowlisted_metadata_and_binds_table_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    _calls, connections = _record_connections(monkeypatch)
    try:
        result = _inspect(candidate, recognized_schemas=())
    finally:
        os.close(parent_fd)

    statements = connections[0].statements
    selects = [
        (sql, parameters)
        for sql, parameters in statements
        if sql.lstrip().upper().startswith("SELECT")
    ]
    assert selects
    assert any("pragma_table_list" in sql for sql, _parameters in selects)
    xinfo = [(sql, parameters) for sql, parameters in selects if "pragma_table_xinfo" in sql]
    assert xinfo
    assert all("?" in sql and len(parameters) == 1 for sql, parameters in xinfo)
    assert all("synthetic" not in sql for sql, _parameters in selects)
    assert all("count(" not in sql.casefold() for sql, _parameters in selects)
    assert result.metrics.bounded_record_count is None
    assert result.metrics.has_session_identifier is False
    assert result.metrics.has_model_identifier_field is False


def test_sqlite_vm_step_and_deadline_limits_map_without_exception_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    try:
        vm_result = _inspect(candidate, budget=ProbeBudget(max_sqlite_vm_steps=1))

        clock = MutableClock()

        def cross_deadline(statement: str) -> None:
            if "pragma_table_list" in statement:
                clock.monotonic_value = 3.0

        _record_connections(monkeypatch, on_execute=cross_deadline)
        timeout_result = _inspect(candidate, clock=clock)
    finally:
        os.close(parent_fd)

    assert ProbeWarningCode.BUDGET_EXCEEDED in vm_result.warning_codes
    assert ProbeWarningCode.PROBE_TIMEOUT in timeout_result.warning_codes
    assert ProbeWarningCode.BUDGET_EXCEEDED not in timeout_result.warning_codes


def test_sqlite_deadline_wins_when_vm_budget_is_reached_at_the_same_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    clock = MutableClock()

    def cross_deadline(statement: str) -> None:
        if "pragma_table_list" in statement:
            clock.monotonic_value = 3.0

    _record_connections(monkeypatch, on_execute=cross_deadline)
    try:
        result = _inspect(
            candidate,
            clock=clock,
            budget=ProbeBudget(max_sqlite_vm_steps=1),
        )
    finally:
        os.close(parent_fd)

    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)


def test_sqlite_schema_identifier_budget_stops_before_storing_row_over_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    try:
        result = _inspect(candidate, budget=ProbeBudget(max_schema_identifiers=1))
    finally:
        os.close(parent_fd)

    assert ProbeWarningCode.BUDGET_EXCEEDED in result.warning_codes
    assert result.metrics.schema_object_count == 1


def test_sqlite_schema_identifier_budget_stores_2048_rows_but_not_row_2049(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    rows = StreamingRows(
        iter(("main", f"reviewed_{index}", "table", 0, 0, 0) for index in range(2_049))
    )
    real_connect = filesystem_module.sqlite3.connect
    connections: list[StreamingTableConnection] = []

    def streaming_connect(
        database_name: str,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        wrapped = real_connect(database_name, *args, **kwargs)
        connection = StreamingTableConnection(wrapped, rows)
        connections.append(connection)
        return cast(sqlite3.Connection, connection)

    monkeypatch.setattr(filesystem_module.sqlite3, "connect", streaming_connect)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert result.warning_codes == (ProbeWarningCode.BUDGET_EXCEEDED,)
    assert result.metrics.schema_object_count == 2_048
    assert rows.next_calls == 2_049
    assert rows.closed is True
    assert connections and connections[0].closed is True


@pytest.mark.parametrize(
    "unsafe_identifier",
    ["bad\nname", "x" * 300],
)
def test_sqlite_invalid_or_oversized_identifiers_are_not_returned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_identifier: str,
) -> None:
    quoted = unsafe_identifier.replace('"', '""')
    database = _create_database(tmp_path, ddl=f'CREATE TABLE "{quoted}"(id INTEGER)')
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert ProbeWarningCode.INVALID_UTF8 in result.warning_codes
    assert unsafe_identifier not in repr(result)


def test_sqlite_huge_schema_default_is_bounded_before_python_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_default = "SECRET_DEFAULT_" * 2_000
    database = _create_database(
        tmp_path,
        ddl=f"CREATE TABLE synthetic(id TEXT DEFAULT '{private_default}')",
    )
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert set(result.warning_codes) & {
        ProbeWarningCode.INVALID_UTF8,
        ProbeWarningCode.MALFORMED_METADATA,
    }
    assert "SECRET_DEFAULT" not in repr(result)


def test_damaged_sqlite_is_malformed_without_exception_or_path_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "session_memory.sqlite"
    database.write_bytes(b"SQLite format 3\x00" + b"damaged private bytes")
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    try:
        result = _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert ProbeWarningCode.MALFORMED_METADATA in result.warning_codes
    assert str(database) not in repr(result)
    assert "damaged" not in repr(result)


def test_injected_reviewed_fingerprint_can_recognize_and_run_fixed_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(
        tmp_path,
        ddl="CREATE TABLE reviewed_metadata(session_id TEXT, model_id TEXT)",
        rows=(("a", "m1"), ("b", "m2")),
        insert_sql="INSERT INTO reviewed_metadata(session_id, model_id) VALUES (?, ?)",
    )
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    _calls, connections = _record_connections(monkeypatch)
    reviewed = _reviewed_schema(count_query="SELECT COUNT(*) FROM reviewed_metadata")
    try:
        result = _inspect(candidate, recognized_schemas=(reviewed,))
    finally:
        os.close(parent_fd)

    assert result.structure_status is StructureStatus.RECOGNIZED
    assert result.metrics.schema_object_count == 3
    assert result.metrics.has_session_identifier is True
    assert result.metrics.has_model_identifier_field is True
    assert result.metrics.bounded_record_count == 2
    assert any(
        sql == "SELECT COUNT(*) FROM reviewed_metadata"
        for sql, _parameters in connections[0].statements
    )


def test_reviewed_count_vm_budget_failure_is_partial_and_omits_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(
        tmp_path,
        ddl="CREATE TABLE reviewed_metadata(session_id TEXT, model_id TEXT)",
        rows=(("a", "m1"), ("b", "m2")),
        insert_sql="INSERT INTO reviewed_metadata(session_id, model_id) VALUES (?, ?)",
    )
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    baseline_counters = _StructureCounters()
    try:
        baseline = _inspect(
            candidate,
            recognized_schemas=(_reviewed_schema(),),
            counters=baseline_counters,
        )
        assert baseline.structure_status is StructureStatus.RECOGNIZED
        budget = ProbeBudget(max_sqlite_vm_steps=baseline_counters.sqlite_vm_steps + 1)
        result = _inspect(
            candidate,
            recognized_schemas=(
                _reviewed_schema(count_query="SELECT COUNT(*) FROM reviewed_metadata"),
            ),
            budget=budget,
        )
    finally:
        os.close(parent_fd)

    assert result.structure_status is StructureStatus.PARTIAL
    assert result.warning_codes == (ProbeWarningCode.BUDGET_EXCEEDED,)
    assert result.metrics.bounded_record_count is None


def test_reviewed_count_deadline_failure_is_partial_and_omits_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(
        tmp_path,
        ddl="CREATE TABLE reviewed_metadata(session_id TEXT, model_id TEXT)",
    )
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    clock = MutableClock()

    def cross_deadline(statement: str) -> None:
        if statement == "SELECT COUNT(*) FROM reviewed_metadata":
            clock.monotonic_value = 3.0

    _record_connections(monkeypatch, on_execute=cross_deadline)
    try:
        result = _inspect(
            candidate,
            recognized_schemas=(
                _reviewed_schema(count_query="SELECT COUNT(*) FROM reviewed_metadata"),
            ),
            clock=clock,
        )
    finally:
        os.close(parent_fd)

    assert result.structure_status is StructureStatus.PARTIAL
    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
    assert result.metrics.bounded_record_count is None


def test_reviewed_count_over_metric_limit_is_budget_exceeded_and_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(
        tmp_path,
        ddl="CREATE TABLE reviewed_metadata(session_id TEXT, model_id TEXT)",
        rows=(("a", "m1"),),
        insert_sql="INSERT INTO reviewed_metadata(session_id, model_id) VALUES (?, ?)",
    )
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    try:
        result = _inspect(
            candidate,
            recognized_schemas=(
                _reviewed_schema(
                    count_query=("SELECT COUNT(*) + 2147483647 FROM reviewed_metadata")
                ),
            ),
        )
    finally:
        os.close(parent_fd)

    assert result.structure_status is StructureStatus.PARTIAL
    assert result.warning_codes == (ProbeWarningCode.BUDGET_EXCEEDED,)
    assert result.metrics.bounded_record_count is None


def test_recognized_schema_with_post_read_race_is_partial_and_omits_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(
        tmp_path,
        ddl="CREATE TABLE reviewed_metadata(session_id TEXT, model_id TEXT)",
    )
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    created = False

    def race_after_count(statement: str) -> None:
        nonlocal created
        if statement == "SELECT COUNT(*) FROM reviewed_metadata" and not created:
            created = True
            database.with_name(database.name + "-journal").touch()

    _record_connections(monkeypatch, on_execute=race_after_count)
    try:
        result = _inspect(
            candidate,
            recognized_schemas=(
                _reviewed_schema(count_query="SELECT COUNT(*) FROM reviewed_metadata"),
            ),
        )
    finally:
        os.close(parent_fd)

    assert result.structure_status is StructureStatus.PARTIAL
    assert ProbeWarningCode.SOURCE_CHANGED in result.warning_codes
    assert result.metrics.bounded_record_count is None


def test_sqlite_resources_close_before_return_and_base_exceptions_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    candidate_fds: set[int] = set()

    def tracking_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == candidate.leaf and dir_fd == candidate.parent_fd:
            candidate_fds.add(fd)
        return fd

    def tracking_close(fd: int) -> None:
        real_close(fd)
        candidate_fds.discard(fd)

    def interrupt_schema(statement: str) -> None:
        if "pragma_table_list" in statement:
            raise KeyboardInterrupt

    monkeypatch.setattr(filesystem_module.os, "open", tracking_open)
    monkeypatch.setattr(filesystem_module.os, "close", tracking_close)
    _calls, connections = _record_connections(monkeypatch, on_execute=interrupt_schema)
    try:
        with pytest.raises(KeyboardInterrupt):
            _inspect(candidate)
    finally:
        os.close(parent_fd)

    assert candidate_fds == set()
    assert connections and connections[0].closed is True


@pytest.mark.parametrize("exit_kind", ["malformed", "timeout"])
def test_sqlite_closes_connection_and_candidate_fd_on_stable_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exit_kind: str,
) -> None:
    database = tmp_path / "session_memory.sqlite"
    if exit_kind == "malformed":
        database.write_bytes(b"SQLite format 3\x00" + b"damaged private bytes")
    else:
        _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    clock = MutableClock()
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    candidate_fds: set[int] = set()

    def tracking_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == candidate.leaf and dir_fd == candidate.parent_fd:
            candidate_fds.add(fd)
        return fd

    def tracking_close(fd: int) -> None:
        real_close(fd)
        candidate_fds.discard(fd)

    def fail_if_selected(statement: str) -> None:
        if exit_kind == "timeout" and "pragma_table_list" in statement:
            clock.monotonic_value = 3.0

    monkeypatch.setattr(filesystem_module.os, "open", tracking_open)
    monkeypatch.setattr(filesystem_module.os, "close", tracking_close)
    _calls, connections = _record_connections(monkeypatch, on_execute=fail_if_selected)
    try:
        result = _inspect(candidate, clock=clock)
    finally:
        os.close(parent_fd)

    expected = (
        ProbeWarningCode.MALFORMED_METADATA
        if exit_kind == "malformed"
        else ProbeWarningCode.PROBE_TIMEOUT
    )
    assert expected in result.warning_codes
    assert candidate_fds == set()
    assert connections and all(connection.closed for connection in connections)


def test_candidate_close_error_does_not_mask_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    _patch_matching_dev_fd(monkeypatch)
    real_open = filesystem_module.os.open
    real_close = filesystem_module.os.close
    candidate_fds: set[int] = set()

    def tracking_open(
        path: str | bytes | os.PathLike[str],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        fd = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == candidate.leaf and dir_fd == candidate.parent_fd:
            candidate_fds.add(fd)
        return fd

    def noisy_close(fd: int) -> None:
        was_candidate = fd in candidate_fds
        real_close(fd)
        candidate_fds.discard(fd)
        if was_candidate:
            raise OSError(errno.EIO, "SECRET close detail")

    def interrupt_schema(statement: str) -> None:
        if "pragma_table_list" in statement:
            raise KeyboardInterrupt

    monkeypatch.setattr(filesystem_module.os, "open", tracking_open)
    monkeypatch.setattr(filesystem_module.os, "close", noisy_close)
    _record_connections(monkeypatch, on_execute=interrupt_schema)
    try:
        with pytest.raises(KeyboardInterrupt):
            _inspect(candidate)
    finally:
        real_close(parent_fd)

    assert candidate_fds == set()


def test_structure_mode_integrates_sqlite_metadata_without_production_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    root = home / ".trae"
    root.mkdir()
    _create_database(root)
    _patch_matching_dev_fd(monkeypatch)
    descriptor = SourceDescriptor(
        source_agent=SourceAgent.TRAE,
        installation_markers=(),
        data_roots=(
            TrustedPath(
                TrustedAnchor.HOME,
                (".trae",),
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

    assert result.structure_status is StructureStatus.UNSUPPORTED
    assert result.metrics.sqlite_candidate_count == 1
    assert result.metrics.schema_object_count > 0
    assert result.metrics.has_session_identifier is False
    assert result.metrics.has_model_identifier_field is False


def test_recognized_candidate_plus_invalid_identifier_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    candidate_directory = home / ".trae" / "session_memory"
    candidate_directory.mkdir(parents=True)
    reviewed_database = candidate_directory / "reviewed.sqlite"
    reviewed_connection = sqlite3.connect(reviewed_database)
    try:
        reviewed_connection.execute(
            "CREATE TABLE reviewed_metadata(session_id TEXT, model_id TEXT)"
        )
        reviewed_connection.commit()
    finally:
        reviewed_connection.close()
    invalid_database = candidate_directory / "invalid.sqlite"
    invalid_connection = sqlite3.connect(invalid_database)
    try:
        invalid_connection.execute('CREATE TABLE "bad\nname"(id INTEGER)')
        invalid_connection.commit()
    finally:
        invalid_connection.close()
    _patch_matching_dev_fd(monkeypatch)
    descriptor = SourceDescriptor(
        source_agent=SourceAgent.TRAE,
        installation_markers=(),
        data_roots=(
            TrustedPath(
                TrustedAnchor.HOME,
                (".trae",),
                ExpectedPathType.DIRECTORY,
            ),
        ),
        capability=ProbeCapability.STRUCTURE_METADATA,
        recognized_schemas=(_reviewed_schema(),),
    )

    result = SafeProbeFilesystem(PathSafetyPolicy(home=home)).inspect_trae_structure(
        descriptor,
        budget=ProbeBudget(),
        clock=MutableClock(),
        deadline=3.0,
    )

    assert result.structure_status is StructureStatus.PARTIAL
    assert ProbeWarningCode.INVALID_UTF8 in result.warning_codes
    assert result.metrics.has_session_identifier is True
    assert result.metrics.has_model_identifier_field is True


def test_sidecar_name_too_long_fails_stably_without_stat_or_connect_leak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    long_leaf = "s" * 255
    forged = Candidate(
        parent_fd=candidate.parent_fd,
        leaf=long_leaf,
        relative_components=(long_leaf,),
        preview_identity=candidate.preview_identity,
    )
    calls, _connections = _record_connections(monkeypatch)
    try:
        result = _inspect(forged)
    finally:
        os.close(parent_fd)

    assert calls == []
    assert result.warning_codes == (ProbeWarningCode.UNSUPPORTED_FORMAT,)
    assert long_leaf not in repr(result)


def test_sidecar_stat_error_crossing_deadline_returns_only_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _create_database(tmp_path)
    candidate, parent_fd = _candidate(database)
    clock = MutableClock()
    real_stat = filesystem_module.os.stat

    def failed_stat(
        path: str | bytes | os.PathLike[str],
        *,
        dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> os.stat_result:
        if path == candidate.leaf + "-wal":
            clock.monotonic_value = 3.0
            raise OSError(errno.EACCES, "SECRET sidecar path")
        return real_stat(path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(filesystem_module.os, "stat", failed_stat)
    try:
        result = _inspect(candidate, clock=clock)
    finally:
        os.close(parent_fd)

    assert result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,)
