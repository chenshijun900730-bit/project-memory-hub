from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from project_memory_hub.probes.base import (
    ExpectedPathType,
    ProbeClock,
    RecognizedSchema,
    SourceDescriptor,
    TrustedAnchor,
    TrustedPath,
)
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    LightInspection,
    ProbeBudget,
    ProbeMetrics,
    ProbeWarningCode,
    StructureInspection,
    StructureStatus,
)
from project_memory_hub.utf8 import (
    InvalidUtf8Text,
    contains_unsafe_text_control,
    strict_utf8_size,
)


_OPEN_SUPPORTS_DIR_FD = os.open in os.supports_dir_fd
_STAT_SUPPORTS_DIR_FD = os.stat in os.supports_dir_fd
_STAT_SUPPORTS_FOLLOW_SYMLINKS = os.stat in os.supports_follow_symlinks
_POST_PREVIEW_RACE_ERRNOS = frozenset({errno.ENOENT, errno.ENOTDIR, errno.ELOOP})
_SQLITE_MAGIC = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SQLITE_TABLE_QUERY = (
    "SELECT schema, name, type, ncol, wr, strict FROM pragma_table_list "
    "WHERE schema = 'main' AND name NOT LIKE 'sqlite_%'"
)
_SQLITE_COLUMN_QUERY = (
    'SELECT p.cid, p.name, p.type, p."notnull", p.dflt_value, p.pk, p.hidden '
    "FROM pragma_table_xinfo(?) AS p"
)
_SQLITE_HARDENING_STATEMENTS = (
    "PRAGMA query_only = ON",
    "PRAGMA trusted_schema = OFF",
    "PRAGMA busy_timeout = 0",
    "PRAGMA temp_store = MEMORY",
)
_DEGRADING_STRUCTURE_WARNINGS = frozenset(
    {
        ProbeWarningCode.PERMISSION_BLOCKED,
        ProbeWarningCode.SYMLINK_REJECTED,
        ProbeWarningCode.UNSAFE_FILE_TYPE,
        ProbeWarningCode.UNSUPPORTED_FORMAT,
        ProbeWarningCode.MALFORMED_METADATA,
        ProbeWarningCode.INVALID_UTF8,
        ProbeWarningCode.BUDGET_EXCEEDED,
        ProbeWarningCode.PROBE_TIMEOUT,
        ProbeWarningCode.SOURCE_CHANGED,
        ProbeWarningCode.PROBE_FAILED,
    }
)
_MAX_SCHEMA_VALUE_BYTES = 255
_SQLITE_RUNTIME_LENGTH_LIMIT = 4_096


class _ProbeFilesystemError(RuntimeError):
    def __init__(self, code: ProbeWarningCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class PathSafetyPolicy:
    home: Path

    def __post_init__(self) -> None:
        selected = Path(self.home)
        if not selected.is_absolute():
            raise ValueError("probe home must be absolute")
        if any(component == ".." for component in selected.parts):
            raise ValueError("probe home must not contain parent traversal")
        object.__setattr__(self, "home", selected)


@dataclass(frozen=True, slots=True)
class _PathInspection:
    readable: bool
    state: DataStatus
    warning: ProbeWarningCode | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    parent_fd: int
    leaf: str
    relative_components: tuple[str, ...]
    preview_identity: tuple[int, int, int, int, int]

    def __post_init__(self) -> None:
        if type(self.parent_fd) is not int:
            raise TypeError("candidate parent fd must be an integer")
        if self.parent_fd < 0:
            raise ValueError("candidate parent fd must be non-negative")
        _require_safe_candidate_component(self.leaf)
        if type(self.relative_components) is not tuple:
            raise TypeError("candidate components must be an immutable tuple")
        if not self.relative_components:
            raise ValueError("candidate components must not be empty")
        for component in self.relative_components:
            _require_safe_candidate_component(component)
        if self.relative_components[-1] != self.leaf:
            raise ValueError("candidate leaf must match its final component")
        if type(self.preview_identity) is not tuple:
            raise TypeError("candidate identity must be an immutable tuple")
        if len(self.preview_identity) != 5:
            raise ValueError("candidate identity must contain five fields")
        if any(type(value) is not int for value in self.preview_identity):
            raise TypeError("candidate identity fields must be integers")


@dataclass(slots=True)
class _TraversalCounters:
    entries: int = 0
    candidate_files: int = 0
    requested_header_bytes: int = 0
    metadata_files: int = 0
    sqlite_candidates: int = 0
    schema_objects: int = 0
    bounded_record_count: int | None = None
    bounded_record_count_blocked: bool = False
    has_session_identifier: bool = False
    has_model_identifier_field: bool = False


@dataclass(slots=True)
class _TraversalState:
    counters: _TraversalCounters
    candidates: list[Candidate]
    sqlite_candidate_entries: list[Candidate]
    warnings: set[ProbeWarningCode]
    recognized_schema: bool = False
    stop: bool = False


@dataclass(slots=True)
class _StructureCounters:
    sqlite_candidates: int = 0
    sqlite_total_bytes: int = 0
    sqlite_vm_steps: int = 0
    schema_identifiers: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.sqlite_candidates,
            self.sqlite_total_bytes,
            self.sqlite_vm_steps,
            self.schema_identifiers,
        ):
            if type(value) is not int:
                raise TypeError("structure counters must contain exact integers")
            if value < 0:
                raise ValueError("structure counters must be non-negative")


@dataclass(slots=True)
class _SqliteResultState:
    warnings: set[ProbeWarningCode]
    attempted: bool = False
    recognized: bool = False
    schema_object_count: int = 0
    bounded_record_count: int | None = None
    has_session_identifier: bool = False
    has_model_identifier_field: bool = False


def aggregate_data_status(states: tuple[str, ...]) -> DataStatus:
    normalized = tuple(DataStatus(state) for state in states)
    for candidate in (
        DataStatus.READABLE,
        DataStatus.BLOCKED,
        DataStatus.REJECTED,
        DataStatus.MISSING,
    ):
        if candidate in normalized:
            return candidate
    return DataStatus.MISSING


def aggregate_installation_status(
    marker_hits: tuple[bool, ...],
) -> InstallationStatus:
    return InstallationStatus.DETECTED if any(marker_hits) else InstallationStatus.NOT_DETECTED


class SafeProbeFilesystem:
    def __init__(self, policy: PathSafetyPolicy) -> None:
        self._policy = policy

    def inspect_light(
        self,
        descriptor: SourceDescriptor,
        *,
        budget: ProbeBudget,
        clock: ProbeClock,
        deadline: float,
    ) -> LightInspection:
        if _deadline_reached(clock, deadline):
            return _timeout_inspection()
        if not _safe_directory_open_capabilities_available():
            return LightInspection(
                installation_status=InstallationStatus.NOT_DETECTED,
                data_status=DataStatus.REJECTED,
                metrics=ProbeMetrics(),
                warning_codes=(ProbeWarningCode.UNSUPPORTED_FORMAT,),
            )

        marker_hits: list[bool] = []
        data_states: list[str] = []
        warnings: set[ProbeWarningCode] = set()
        checked_markers = 0
        checked_roots = 0
        readable_roots = 0
        blocked_roots = 0
        missing_roots = 0
        rejected_roots = 0
        consumed_targets = 0
        stop = False

        for marker in descriptor.installation_markers:
            if _deadline_reached(clock, deadline):
                warnings.add(ProbeWarningCode.PROBE_TIMEOUT)
                stop = True
                break
            if consumed_targets >= budget.light_max_targets_per_source:
                warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
                break
            consumed_targets += 1
            checked_markers += 1
            outcome = self._inspect_path(marker, clock=clock, deadline=deadline)
            if outcome.warning is ProbeWarningCode.PROBE_TIMEOUT:
                warnings.add(ProbeWarningCode.PROBE_TIMEOUT)
                stop = True
                break
            marker_hits.append(outcome.readable)
            if outcome.warning is not None:
                warnings.add(outcome.warning)

        if not stop:
            for root in descriptor.data_roots:
                if _deadline_reached(clock, deadline):
                    warnings.add(ProbeWarningCode.PROBE_TIMEOUT)
                    break
                if consumed_targets >= budget.light_max_targets_per_source:
                    warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
                    break
                consumed_targets += 1
                checked_roots += 1
                outcome = self._inspect_path(root, clock=clock, deadline=deadline)
                if outcome.warning is ProbeWarningCode.PROBE_TIMEOUT:
                    warnings.add(ProbeWarningCode.PROBE_TIMEOUT)
                    break
                data_states.append(outcome.state.value)
                if outcome.state is DataStatus.READABLE:
                    readable_roots += 1
                elif outcome.state is DataStatus.BLOCKED:
                    blocked_roots += 1
                elif outcome.state is DataStatus.REJECTED:
                    rejected_roots += 1
                else:
                    missing_roots += 1
                if outcome.warning is not None:
                    warnings.add(outcome.warning)

        installation_status = aggregate_installation_status(tuple(marker_hits))
        data_status = aggregate_data_status(tuple(data_states))
        roots_are_completely_missing = bool(descriptor.data_roots) and (
            checked_roots == len(descriptor.data_roots) and missing_roots == checked_roots
        )
        markers_are_completely_absent = not descriptor.data_roots and (
            checked_markers == len(descriptor.installation_markers)
            and installation_status is InstallationStatus.NOT_DETECTED
        )
        if roots_are_completely_missing or markers_are_completely_absent:
            warnings.add(ProbeWarningCode.SOURCE_MISSING)

        metrics = ProbeMetrics(
            checked_installation_marker_count=checked_markers,
            detected_installation_marker_count=sum(marker_hits),
            checked_data_root_count=checked_roots,
            readable_data_root_count=readable_roots,
            blocked_data_root_count=blocked_roots,
            missing_data_root_count=missing_roots,
            rejected_data_root_count=rejected_roots,
        )
        return LightInspection(
            installation_status=installation_status,
            data_status=data_status,
            metrics=metrics,
            warning_codes=_sorted_warnings(warnings),
        )

    def inspect_trae_structure(
        self,
        descriptor: SourceDescriptor,
        *,
        budget: ProbeBudget,
        clock: ProbeClock,
        deadline: float,
    ) -> StructureInspection:
        light = self.inspect_light(
            descriptor,
            budget=budget,
            clock=clock,
            deadline=deadline,
        )
        warnings = set(light.warning_codes)
        state = _TraversalState(
            counters=_TraversalCounters(),
            candidates=[],
            sqlite_candidate_entries=[],
            warnings=warnings,
        )
        if ProbeWarningCode.PROBE_TIMEOUT in warnings:
            return _structure_inspection(light, state)
        if not _safe_structure_capabilities_available():
            warnings.add(ProbeWarningCode.UNSUPPORTED_FORMAT)
            return _structure_inspection(light, state)

        try:
            with ExitStack() as candidate_stack:
                for root in descriptor.data_roots:
                    if state.stop:
                        break
                    try:
                        _check_deadline(clock, deadline)
                        with self._opened_verified_path(
                            root,
                            clock=clock,
                            deadline=deadline,
                        ) as root_fd:
                            _walk_structure_directory(
                                root_fd,
                                relative_components=(),
                                depth=0,
                                budget=budget,
                                clock=clock,
                                deadline=deadline,
                                state=state,
                                candidate_stack=candidate_stack,
                            )
                        _check_deadline(clock, deadline)
                    except Exception as error:
                        _record_structure_exception(
                            state,
                            error,
                            clock=clock,
                            deadline=deadline,
                            post_preview=False,
                        )

                if ProbeWarningCode.PROBE_TIMEOUT not in state.warnings:
                    _inspect_candidate_headers(
                        state,
                        budget=budget,
                        clock=clock,
                        deadline=deadline,
                    )
                if ProbeWarningCode.PROBE_TIMEOUT not in state.warnings:
                    _inspect_sqlite_candidates(
                        state,
                        recognized_schemas=descriptor.recognized_schemas,
                        budget=budget,
                        clock=clock,
                        deadline=deadline,
                    )
            _check_deadline(clock, deadline)
        except Exception as error:
            _record_structure_exception(
                state,
                error,
                clock=clock,
                deadline=deadline,
                post_preview=False,
            )

        return _structure_inspection(light, state)

    def _inspect_path(
        self,
        trusted_path: TrustedPath,
        *,
        clock: ProbeClock,
        deadline: float,
    ) -> _PathInspection:
        try:
            self._open_and_verify_path(
                trusted_path,
                clock=clock,
                deadline=deadline,
            )
        except _ProbeFilesystemError as error:
            if error.code is ProbeWarningCode.PROBE_TIMEOUT:
                return _PathInspection(False, DataStatus.MISSING, error.code)
            if error.code is ProbeWarningCode.PERMISSION_BLOCKED:
                return _PathInspection(False, DataStatus.BLOCKED, error.code)
            if error.code in {
                ProbeWarningCode.SYMLINK_REJECTED,
                ProbeWarningCode.UNSAFE_FILE_TYPE,
                ProbeWarningCode.SOURCE_CHANGED,
                ProbeWarningCode.UNSUPPORTED_FORMAT,
            }:
                return _PathInspection(False, DataStatus.REJECTED, error.code)
            return _PathInspection(False, DataStatus.MISSING, error.code)
        except OSError as error:
            if _deadline_reached(clock, deadline):
                return _PathInspection(
                    False,
                    DataStatus.MISSING,
                    ProbeWarningCode.PROBE_TIMEOUT,
                )
            if error.errno in {errno.ENOENT, errno.ENOTDIR}:
                return _PathInspection(False, DataStatus.MISSING)
            if error.errno in {errno.EACCES, errno.EPERM}:
                return _PathInspection(
                    False,
                    DataStatus.BLOCKED,
                    ProbeWarningCode.PERMISSION_BLOCKED,
                )
            if error.errno == errno.ELOOP:
                return _PathInspection(
                    False,
                    DataStatus.REJECTED,
                    ProbeWarningCode.SYMLINK_REJECTED,
                )
            raise
        return _PathInspection(True, DataStatus.READABLE)

    def _open_and_verify_path(
        self,
        trusted_path: TrustedPath,
        *,
        clock: ProbeClock,
        deadline: float,
    ) -> None:
        with self._opened_verified_path(
            trusted_path,
            clock=clock,
            deadline=deadline,
        ):
            pass
        _check_deadline(clock, deadline)

    @contextmanager
    def _opened_verified_path(
        self,
        trusted_path: TrustedPath,
        *,
        clock: ProbeClock,
        deadline: float,
    ) -> Iterator[int]:
        if (
            trusted_path.expected_type is ExpectedPathType.EXECUTABLE_FILE
            and not _nonblocking_file_open_available()
        ):
            raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)
        with ExitStack() as stack:
            parent_fd = _open_verified_root(clock=clock, deadline=deadline)
            stack.callback(os.close, parent_fd)

            anchor_components: tuple[str, ...]
            if trusted_path.anchor is TrustedAnchor.HOME:
                anchor_components = tuple(self._policy.home.parts[1:])
            else:
                anchor_components = ()
            components = (*anchor_components, *trusted_path.components)
            last_index = len(components) - 1
            for index, component in enumerate(components):
                expected_directory = (
                    index != last_index or trusted_path.expected_type is ExpectedPathType.DIRECTORY
                )
                next_fd = _open_verified_component(
                    component,
                    parent_fd=parent_fd,
                    is_final_component=index == last_index,
                    expected_directory=expected_directory,
                    expected_executable=(
                        index == last_index
                        and trusted_path.expected_type is ExpectedPathType.EXECUTABLE_FILE
                    ),
                    clock=clock,
                    deadline=deadline,
                )
                stack.callback(os.close, next_fd)
                parent_fd = next_fd
            yield parent_fd


def _safe_directory_open_capabilities_available() -> bool:
    if not (_OPEN_SUPPORTS_DIR_FD and _STAT_SUPPORTS_DIR_FD and _STAT_SUPPORTS_FOLLOW_SYMLINKS):
        return False
    for name in ("O_NOFOLLOW", "O_CLOEXEC", "O_DIRECTORY"):
        value = getattr(os, name, None)
        if type(value) is not int or value == 0:
            return False
    return type(getattr(os, "O_RDONLY", None)) is int


def _nonblocking_file_open_available() -> bool:
    value = getattr(os, "O_NONBLOCK", None)
    return type(value) is int and value != 0


def _safe_structure_capabilities_available() -> bool:
    return (
        _safe_directory_open_capabilities_available()
        and _nonblocking_file_open_available()
        and callable(getattr(os, "pread", None))
        and callable(getattr(os, "scandir", None))
        and callable(getattr(os, "dup", None))
    )


def _deadline_reached(clock: ProbeClock, deadline: float) -> bool:
    return clock.monotonic() >= deadline


def _check_deadline(clock: ProbeClock, deadline: float) -> None:
    if _deadline_reached(clock, deadline):
        raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT)


def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
    )


def is_session_memory_candidate(components: tuple[str, ...]) -> bool:
    if not components:
        return False
    return "session_memory" in components or Path(components[-1]).stem == "session_memory"


def _strict_entry_name(value: object) -> str:
    if type(value) is not str:
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8)
    if len(value) > 255:
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8)
    try:
        encoded_size = strict_utf8_size(value)
    except InvalidUtf8Text:
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8) from None
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\x00" in value
        or encoded_size > 255
        or contains_unsafe_text_control(value)
    ):
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8)
    return value


def _require_safe_candidate_component(value: object) -> str:
    try:
        return _strict_entry_name(value)
    except _ProbeFilesystemError:
        if type(value) is not str:
            raise TypeError("candidate components must be strings") from None
        raise ValueError("candidate component is unsafe") from None


def _walk_structure_directory(
    directory_fd: int,
    *,
    relative_components: tuple[str, ...],
    depth: int,
    budget: ProbeBudget,
    clock: ProbeClock,
    deadline: float,
    state: _TraversalState,
    candidate_stack: ExitStack,
) -> None:
    try:
        scanner = os.scandir(directory_fd)
    except OSError as error:
        raise _ProbeFilesystemError(
            _stable_warning_for_oserror(error, post_preview=False)
        ) from None
    with scanner as iterator:
        while not state.stop:
            _check_deadline(clock, deadline)
            if state.counters.entries >= budget.max_entries:
                state.warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
                state.stop = True
                return
            try:
                entry = next(iterator)
            except StopIteration:
                _check_deadline(clock, deadline)
                return
            except OSError as error:
                raise _ProbeFilesystemError(
                    _stable_warning_for_oserror(error, post_preview=False)
                ) from None
            state.counters.entries += 1
            _check_deadline(clock, deadline)

            try:
                name = _strict_entry_name(entry.name)
            except _ProbeFilesystemError as error:
                state.warnings.add(error.code)
                continue
            child = (*relative_components, name)
            try:
                preview = entry.stat(follow_symlinks=False)
            except Exception as error:
                _record_structure_exception(
                    state,
                    error,
                    clock=clock,
                    deadline=deadline,
                    post_preview=True,
                )
                if state.stop:
                    return
                continue
            _check_deadline(clock, deadline)

            if stat.S_ISLNK(preview.st_mode):
                state.warnings.add(ProbeWarningCode.SYMLINK_REJECTED)
                continue
            if stat.S_ISDIR(preview.st_mode):
                if depth >= budget.max_depth:
                    state.warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
                    continue
                _walk_structure_child_directory(
                    directory_fd,
                    name=name,
                    child=child,
                    depth=depth,
                    preview=preview,
                    budget=budget,
                    clock=clock,
                    deadline=deadline,
                    state=state,
                    candidate_stack=candidate_stack,
                )
                continue
            if stat.S_ISREG(preview.st_mode):
                if not is_session_memory_candidate(child):
                    continue
                if state.counters.candidate_files >= budget.max_candidate_files:
                    state.warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
                    state.stop = True
                    return
                state.counters.candidate_files += 1
                try:
                    _check_deadline(clock, deadline)
                    parent_fd = os.dup(directory_fd)
                    candidate_stack.callback(os.close, parent_fd)
                    _check_deadline(clock, deadline)
                except Exception as error:
                    _record_structure_exception(
                        state,
                        error,
                        clock=clock,
                        deadline=deadline,
                        post_preview=False,
                    )
                    if state.stop:
                        return
                    continue
                state.candidates.append(
                    Candidate(
                        parent_fd=parent_fd,
                        leaf=name,
                        relative_components=child,
                        preview_identity=_identity(preview),
                    )
                )
                continue
            state.warnings.add(ProbeWarningCode.UNSAFE_FILE_TYPE)


def _walk_structure_child_directory(
    directory_fd: int,
    *,
    name: str,
    child: tuple[str, ...],
    depth: int,
    preview: os.stat_result,
    budget: ProbeBudget,
    clock: ProbeClock,
    deadline: float,
    state: _TraversalState,
    candidate_stack: ExitStack,
) -> None:
    try:
        _check_deadline(clock, deadline)
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        _check_deadline(clock, deadline)
        if stat.S_ISLNK(before.st_mode):
            state.warnings.add(ProbeWarningCode.SYMLINK_REJECTED)
            return
        if _identity(preview) != _identity(before):
            state.warnings.add(ProbeWarningCode.SOURCE_CHANGED)
            return
        if not stat.S_ISDIR(before.st_mode):
            state.warnings.add(ProbeWarningCode.SOURCE_CHANGED)
            return
        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
        child_fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            _check_deadline(clock, deadline)
            opened = os.fstat(child_fd)
            _check_deadline(clock, deadline)
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            _check_deadline(clock, deadline)
            identity = _identity(preview)
            if not (identity == _identity(before) == _identity(opened) == _identity(after)):
                state.warnings.add(ProbeWarningCode.SOURCE_CHANGED)
                return
            if not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(after.st_mode):
                state.warnings.add(ProbeWarningCode.SOURCE_CHANGED)
                return
            _walk_structure_directory(
                child_fd,
                relative_components=child,
                depth=depth + 1,
                budget=budget,
                clock=clock,
                deadline=deadline,
                state=state,
                candidate_stack=candidate_stack,
            )
        finally:
            os.close(child_fd)
    except _ProbeFilesystemError:
        raise
    except Exception as error:
        _record_structure_exception(
            state,
            error,
            clock=clock,
            deadline=deadline,
            post_preview=True,
        )


def _inspect_candidate_headers(
    state: _TraversalState,
    *,
    budget: ProbeBudget,
    clock: ProbeClock,
    deadline: float,
) -> None:
    for candidate in state.candidates:
        try:
            _check_deadline(clock, deadline)
            remaining = budget.max_total_header_bytes - state.counters.requested_header_bytes
            if remaining <= 0:
                state.warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
                return
            requested = min(budget.max_header_bytes, remaining)
            state.counters.requested_header_bytes += requested
            header = _read_verified_candidate_header(
                candidate,
                requested=requested,
                clock=clock,
                deadline=deadline,
            )
            if header is None:
                state.warnings.add(ProbeWarningCode.SOURCE_CHANGED)
                continue
            state.counters.metadata_files += 1
            if header[: len(_SQLITE_MAGIC)] == _SQLITE_MAGIC:
                state.counters.sqlite_candidates += 1
                state.sqlite_candidate_entries.append(candidate)
            else:
                state.warnings.add(ProbeWarningCode.UNSUPPORTED_FORMAT)
        except Exception as error:
            _record_structure_exception(
                state,
                error,
                clock=clock,
                deadline=deadline,
                post_preview=False,
            )
            if state.stop:
                return


def _read_verified_candidate_header(
    candidate: Candidate,
    *,
    requested: int,
    clock: ProbeClock,
    deadline: float,
) -> bytes | None:
    _check_deadline(clock, deadline)
    try:
        before = os.stat(
            candidate.leaf,
            dir_fd=candidate.parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        raise _ProbeFilesystemError(_stable_warning_for_oserror(error, post_preview=True)) from None
    _check_deadline(clock, deadline)
    if stat.S_ISLNK(before.st_mode):
        raise _ProbeFilesystemError(ProbeWarningCode.SYMLINK_REJECTED)
    if _identity(before) != candidate.preview_identity:
        return None
    if not stat.S_ISREG(before.st_mode):
        return None

    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        candidate_fd = os.open(candidate.leaf, flags, dir_fd=candidate.parent_fd)
    except OSError as error:
        raise _ProbeFilesystemError(_stable_warning_for_oserror(error, post_preview=True)) from None
    try:
        _check_deadline(clock, deadline)
        opened = os.fstat(candidate_fd)
        _check_deadline(clock, deadline)
        after = os.stat(
            candidate.leaf,
            dir_fd=candidate.parent_fd,
            follow_symlinks=False,
        )
        _check_deadline(clock, deadline)
        if not (
            candidate.preview_identity == _identity(before) == _identity(opened) == _identity(after)
        ):
            return None
        if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(after.st_mode):
            return None
        header = os.pread(candidate_fd, requested, 0)
        _check_deadline(clock, deadline)
        post_opened = os.fstat(candidate_fd)
        _check_deadline(clock, deadline)
        post_after = os.stat(
            candidate.leaf,
            dir_fd=candidate.parent_fd,
            follow_symlinks=False,
        )
        _check_deadline(clock, deadline)
        if not (candidate.preview_identity == _identity(post_opened) == _identity(post_after)):
            return None
        return header
    except OSError as error:
        raise _ProbeFilesystemError(_stable_warning_for_oserror(error, post_preview=True)) from None
    finally:
        os.close(candidate_fd)


class SqliteMetadataInspector:
    def __init__(self, budget: ProbeBudget, clock: ProbeClock) -> None:
        if type(budget) is not ProbeBudget:
            raise TypeError("SQLite inspector budget must be a ProbeBudget")
        self._budget = budget
        self._clock = clock

    def inspect(
        self,
        candidate: Candidate,
        *,
        recognized_schemas: tuple[RecognizedSchema, ...],
        deadline: float,
        counters: _StructureCounters | None = None,
    ) -> StructureInspection:
        selected_counters = counters if counters is not None else _StructureCounters()
        state = _SqliteResultState(warnings=set())
        try:
            if type(candidate) is not Candidate:
                raise TypeError("SQLite candidate must be a Candidate")
            if type(recognized_schemas) is not tuple or any(
                type(schema) is not RecognizedSchema for schema in recognized_schemas
            ):
                raise TypeError("recognized schemas must be an immutable reviewed tuple")
            if type(selected_counters) is not _StructureCounters:
                raise TypeError("SQLite counters must be structure counters")
            self._inspect_verified_candidate(
                candidate,
                recognized_schemas=recognized_schemas,
                deadline=deadline,
                counters=selected_counters,
                state=state,
            )
        except Exception as error:
            _record_sqlite_exception(
                state,
                error,
                clock=self._clock,
                deadline=deadline,
                post_preview=True,
            )
        return _sqlite_structure_inspection(state)

    def _inspect_verified_candidate(
        self,
        candidate: Candidate,
        *,
        recognized_schemas: tuple[RecognizedSchema, ...],
        deadline: float,
        counters: _StructureCounters,
        state: _SqliteResultState,
    ) -> None:
        _check_deadline(self._clock, deadline)
        if counters.sqlite_candidates >= self._budget.max_sqlite_candidates:
            raise _ProbeFilesystemError(ProbeWarningCode.BUDGET_EXCEEDED)
        counters.sqlite_candidates += 1
        state.attempted = True

        candidate_size = candidate.preview_identity[3]
        if candidate_size < 0:
            raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
        if candidate_size > self._budget.max_sqlite_file_bytes:
            raise _ProbeFilesystemError(ProbeWarningCode.BUDGET_EXCEEDED)
        if counters.sqlite_total_bytes + candidate_size > self._budget.max_sqlite_total_bytes:
            raise _ProbeFilesystemError(ProbeWarningCode.BUDGET_EXCEEDED)
        counters.sqlite_total_bytes += candidate_size

        _check_sqlite_sidecars(
            candidate,
            clock=self._clock,
            deadline=deadline,
        )
        before = _stat_verified_candidate(
            candidate,
            clock=self._clock,
            deadline=deadline,
        )
        if stat.S_ISLNK(before.st_mode):
            raise _ProbeFilesystemError(ProbeWarningCode.SYMLINK_REJECTED)
        if not stat.S_ISREG(before.st_mode):
            raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
        if _identity(before) != candidate.preview_identity:
            raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)

        flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
        _check_deadline(self._clock, deadline)
        try:
            candidate_fd = os.open(candidate.leaf, flags, dir_fd=candidate.parent_fd)
        except OSError as error:
            _raise_sqlite_oserror(
                error,
                clock=self._clock,
                deadline=deadline,
                post_preview=True,
            )
        with _managed_owned_fd(candidate_fd):
            pending_count = self._inspect_open_candidate(
                candidate,
                candidate_fd=candidate_fd,
                before=before,
                recognized_schemas=recognized_schemas,
                deadline=deadline,
                counters=counters,
                state=state,
            )
        _check_deadline(self._clock, deadline)
        state.bounded_record_count = pending_count

    def _inspect_open_candidate(
        self,
        candidate: Candidate,
        *,
        candidate_fd: int,
        before: os.stat_result,
        recognized_schemas: tuple[RecognizedSchema, ...],
        deadline: float,
        counters: _StructureCounters,
        state: _SqliteResultState,
    ) -> int | None:
        try:
            _check_deadline(self._clock, deadline)
            opened = os.fstat(candidate_fd)
            _check_deadline(self._clock, deadline)
            after = os.stat(
                candidate.leaf,
                dir_fd=candidate.parent_fd,
                follow_symlinks=False,
            )
            _check_deadline(self._clock, deadline)
            if not (
                candidate.preview_identity
                == _identity(before)
                == _identity(opened)
                == _identity(after)
            ):
                raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
            if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(after.st_mode):
                raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
            _verify_dev_fd_identity(
                candidate_fd,
                clock=self._clock,
                deadline=deadline,
            )
            _check_sqlite_sidecars(
                candidate,
                clock=self._clock,
                deadline=deadline,
            )

            connection = _connect_verified_sqlite_fd(
                candidate_fd,
                clock=self._clock,
                deadline=deadline,
            )
            with _managed_sqlite_connection(connection):
                abort_reason = _configure_sqlite_connection(
                    connection,
                    counters=counters,
                    budget=self._budget,
                    clock=self._clock,
                    deadline=deadline,
                )
                pending_count = _read_sqlite_schema(
                    connection,
                    recognized_schemas=recognized_schemas,
                    counters=counters,
                    budget=self._budget,
                    clock=self._clock,
                    deadline=deadline,
                    abort_reason=abort_reason,
                    state=state,
                )
            _check_deadline(self._clock, deadline)

            post_opened = os.fstat(candidate_fd)
            _check_deadline(self._clock, deadline)
            post_after = os.stat(
                candidate.leaf,
                dir_fd=candidate.parent_fd,
                follow_symlinks=False,
            )
            _check_deadline(self._clock, deadline)
            if not (candidate.preview_identity == _identity(post_opened) == _identity(post_after)):
                raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
            _verify_dev_fd_identity(
                candidate_fd,
                clock=self._clock,
                deadline=deadline,
            )
            _check_sqlite_sidecars(
                candidate,
                clock=self._clock,
                deadline=deadline,
            )
            return pending_count
        except OSError as error:
            _raise_sqlite_oserror(
                error,
                clock=self._clock,
                deadline=deadline,
                post_preview=True,
            )


@contextmanager
def _managed_owned_fd(fd: int) -> Iterator[int]:
    try:
        yield fd
    except BaseException:
        try:
            os.close(fd)
        except BaseException:
            pass
        raise
    else:
        os.close(fd)


@contextmanager
def _managed_sqlite_connection(
    connection: sqlite3.Connection,
) -> Iterator[sqlite3.Connection]:
    try:
        yield connection
    except BaseException:
        try:
            connection.close()
        except BaseException:
            pass
        raise
    else:
        connection.close()


@contextmanager
def _managed_sqlite_cursor(cursor: sqlite3.Cursor) -> Iterator[sqlite3.Cursor]:
    try:
        yield cursor
    except BaseException:
        try:
            cursor.close()
        except BaseException:
            pass
        raise
    else:
        cursor.close()


def _check_sqlite_sidecars(
    candidate: Candidate,
    *,
    clock: ProbeClock,
    deadline: float,
) -> None:
    try:
        leaf_size = strict_utf8_size(candidate.leaf)
    except InvalidUtf8Text:
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8) from None
    for suffix in _SQLITE_SIDECAR_SUFFIXES:
        if leaf_size + len(suffix) > 255:
            raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)
        _check_deadline(clock, deadline)
        try:
            os.stat(
                candidate.leaf + suffix,
                dir_fd=candidate.parent_fd,
                follow_symlinks=False,
            )
        except OSError as error:
            if _deadline_reached(clock, deadline):
                raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
            if error.errno == errno.ENOENT:
                continue
            if error.errno == errno.ENAMETOOLONG:
                raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT) from None
            raise _ProbeFilesystemError(
                _stable_warning_for_oserror(error, post_preview=False)
            ) from None
        _check_deadline(clock, deadline)
        raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)


def _stat_verified_candidate(
    candidate: Candidate,
    *,
    clock: ProbeClock,
    deadline: float,
) -> os.stat_result:
    _check_deadline(clock, deadline)
    try:
        value = os.stat(
            candidate.leaf,
            dir_fd=candidate.parent_fd,
            follow_symlinks=False,
        )
    except OSError as error:
        _raise_sqlite_oserror(
            error,
            clock=clock,
            deadline=deadline,
            post_preview=True,
        )
    _check_deadline(clock, deadline)
    return value


def _verify_dev_fd_identity(
    candidate_fd: int,
    *,
    clock: ProbeClock,
    deadline: float,
) -> None:
    _check_deadline(clock, deadline)
    try:
        opened = os.fstat(candidate_fd)
        _check_deadline(clock, deadline)
        dev_fd = os.stat(f"/dev/fd/{candidate_fd}", follow_symlinks=True)
    except OSError:
        if _deadline_reached(clock, deadline):
            raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
        raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT) from None
    _check_deadline(clock, deadline)
    if (opened.st_dev, opened.st_ino) != (dev_fd.st_dev, dev_fd.st_ino):
        raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)
    if not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(dev_fd.st_mode):
        raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)


def _connect_verified_sqlite_fd(
    candidate_fd: int,
    *,
    clock: ProbeClock,
    deadline: float,
) -> sqlite3.Connection:
    _check_deadline(clock, deadline)
    uri = f"file:/dev/fd/{candidate_fd}?mode=ro&immutable=1"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=0.0)
    except sqlite3.Error:
        if _deadline_reached(clock, deadline):
            raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
        raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT) from None
    try:
        _check_deadline(clock, deadline)
    except BaseException:
        try:
            connection.close()
        except BaseException:
            pass
        raise
    return connection


def _configure_sqlite_connection(
    connection: sqlite3.Connection,
    *,
    counters: _StructureCounters,
    budget: ProbeBudget,
    clock: ProbeClock,
    deadline: float,
) -> list[ProbeWarningCode | None]:
    try:
        _check_deadline(clock, deadline)
        for category in (sqlite3.SQLITE_LIMIT_LENGTH, sqlite3.SQLITE_LIMIT_SQL_LENGTH):
            _check_deadline(clock, deadline)
            connection.setlimit(category, _SQLITE_RUNTIME_LENGTH_LIMIT)
            _check_deadline(clock, deadline)
            configured_limit = connection.getlimit(category)
            _check_deadline(clock, deadline)
            if configured_limit > _SQLITE_RUNTIME_LENGTH_LIMIT:
                raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)
        _check_deadline(clock, deadline)
        connection.enable_load_extension(False)
        _check_deadline(clock, deadline)
        for statement in _SQLITE_HARDENING_STATEMENTS:
            _execute_and_close(connection, statement, clock=clock, deadline=deadline)
        for statement, expected in (
            ("PRAGMA query_only", 1),
            ("PRAGMA trusted_schema", 0),
            ("PRAGMA busy_timeout", 0),
            ("PRAGMA temp_store", 2),
        ):
            if (
                _read_single_pragma(connection, statement, clock=clock, deadline=deadline)
                != expected
            ):
                raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)
    except (sqlite3.Error, AttributeError):
        if _deadline_reached(clock, deadline):
            raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
        raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT) from None

    abort_reason: list[ProbeWarningCode | None] = [None]

    def progress() -> int:
        if _deadline_reached(clock, deadline):
            abort_reason[0] = ProbeWarningCode.PROBE_TIMEOUT
            return 1
        if counters.sqlite_vm_steps >= budget.max_sqlite_vm_steps:
            abort_reason[0] = ProbeWarningCode.BUDGET_EXCEEDED
            return 1
        counters.sqlite_vm_steps += 1
        if counters.sqlite_vm_steps >= budget.max_sqlite_vm_steps:
            abort_reason[0] = ProbeWarningCode.BUDGET_EXCEEDED
            return 1
        return 0

    try:
        connection.set_progress_handler(progress, 1)
    except sqlite3.Error:
        if _deadline_reached(clock, deadline):
            raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
        raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT) from None
    _check_deadline(clock, deadline)
    return abort_reason


def _execute_and_close(
    connection: sqlite3.Connection,
    statement: str,
    *,
    clock: ProbeClock,
    deadline: float,
) -> None:
    _check_deadline(clock, deadline)
    cursor = connection.execute(statement)
    with _managed_sqlite_cursor(cursor):
        _check_deadline(clock, deadline)


def _read_single_pragma(
    connection: sqlite3.Connection,
    statement: str,
    *,
    clock: ProbeClock,
    deadline: float,
) -> int:
    _check_deadline(clock, deadline)
    cursor = connection.execute(statement)
    with _managed_sqlite_cursor(cursor):
        row = _next_cursor_row(
            cursor,
            clock=clock,
            deadline=deadline,
            abort_reason=[None],
        )
        if row is None or type(row) is not tuple or len(row) != 1 or type(row[0]) is not int:
            raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)
        if (
            _next_cursor_row(
                cursor,
                clock=clock,
                deadline=deadline,
                abort_reason=[None],
            )
            is not None
        ):
            raise _ProbeFilesystemError(ProbeWarningCode.UNSUPPORTED_FORMAT)
        return row[0]


def _read_sqlite_schema(
    connection: sqlite3.Connection,
    *,
    recognized_schemas: tuple[RecognizedSchema, ...],
    counters: _StructureCounters,
    budget: ProbeBudget,
    clock: ProbeClock,
    deadline: float,
    abort_reason: list[ProbeWarningCode | None],
    state: _SqliteResultState,
) -> int | None:
    schema_rows: list[tuple[object, ...]] = []
    tables: list[str] = []
    try:
        _check_deadline(clock, deadline)
        table_cursor = connection.execute(_SQLITE_TABLE_QUERY)
        with _managed_sqlite_cursor(table_cursor):
            while True:
                row = _next_cursor_row(
                    table_cursor,
                    clock=clock,
                    deadline=deadline,
                    abort_reason=abort_reason,
                )
                if row is None:
                    break
                normalized, table_name = _normalize_table_row(
                    row,
                    counters=counters,
                    budget=budget,
                )
                schema_rows.append(normalized)
                tables.append(table_name)
                state.schema_object_count += 1

        for table_name in tables:
            _check_deadline(clock, deadline)
            column_cursor = connection.execute(_SQLITE_COLUMN_QUERY, (table_name,))
            with _managed_sqlite_cursor(column_cursor):
                while True:
                    row = _next_cursor_row(
                        column_cursor,
                        clock=clock,
                        deadline=deadline,
                        abort_reason=abort_reason,
                    )
                    if row is None:
                        break
                    schema_rows.append(
                        _normalize_column_row(
                            row,
                            table_name=table_name,
                            counters=counters,
                            budget=budget,
                        )
                    )
                    state.schema_object_count += 1
    except sqlite3.Error as error:
        _raise_sqlite_query_error(
            error,
            abort_reason=abort_reason,
            clock=clock,
            deadline=deadline,
        )

    fingerprint = _schema_fingerprint(
        schema_rows,
        clock=clock,
        deadline=deadline,
    )
    reviewed = next(
        (schema for schema in recognized_schemas if schema.fingerprint == fingerprint),
        None,
    )
    _check_deadline(clock, deadline)
    if reviewed is None:
        state.warnings.add(ProbeWarningCode.UNSUPPORTED_FORMAT)
        return None

    state.recognized = True
    state.has_session_identifier = bool(reviewed.session_identifier_fields)
    state.has_model_identifier_field = bool(reviewed.model_identifier_fields)
    if reviewed.bounded_count_query is None:
        return None
    try:
        return _execute_reviewed_count(
            connection,
            reviewed.bounded_count_query,
            clock=clock,
            deadline=deadline,
            abort_reason=abort_reason,
        )
    except Exception as error:
        _record_sqlite_exception(
            state,
            error,
            clock=clock,
            deadline=deadline,
            post_preview=True,
        )
        return None


def _next_cursor_row(
    cursor: sqlite3.Cursor,
    *,
    clock: ProbeClock,
    deadline: float,
    abort_reason: list[ProbeWarningCode | None],
) -> tuple[object, ...] | None:
    _check_deadline(clock, deadline)
    try:
        row = next(cursor)
    except StopIteration:
        _check_deadline(clock, deadline)
        return None
    except sqlite3.Error as error:
        _raise_sqlite_query_error(
            error,
            abort_reason=abort_reason,
            clock=clock,
            deadline=deadline,
        )
    _check_deadline(clock, deadline)
    if type(row) is not tuple:
        raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA)
    return row


def _normalize_table_row(
    row: tuple[object, ...],
    *,
    counters: _StructureCounters,
    budget: ProbeBudget,
) -> tuple[tuple[object, ...], str]:
    _reserve_schema_row(row, expected_length=6, counters=counters, budget=budget)
    schema = _schema_text(row[0], allow_empty=False)
    name = _schema_text(row[1], allow_empty=False)
    object_type = _schema_text(row[2], allow_empty=False)
    ncol = _schema_integer(row[3])
    without_rowid = _schema_integer(row[4])
    strict = _schema_integer(row[5])
    if schema != "main":
        raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA)
    return (
        ("table", schema, name, object_type, ncol, without_rowid, strict),
        name,
    )


def _normalize_column_row(
    row: tuple[object, ...],
    *,
    table_name: str,
    counters: _StructureCounters,
    budget: ProbeBudget,
) -> tuple[object, ...]:
    _reserve_schema_row(row, expected_length=7, counters=counters, budget=budget)
    cid = _schema_integer(row[0])
    name = _schema_text(row[1], allow_empty=False)
    declared_type = _schema_text(row[2], allow_empty=True)
    not_null = _schema_integer(row[3])
    default = None if row[4] is None else _schema_text(row[4], allow_empty=True)
    primary_key = _schema_integer(row[5])
    hidden = _schema_integer(row[6])
    return (
        "column",
        table_name,
        cid,
        name,
        declared_type,
        not_null,
        default,
        primary_key,
        hidden,
    )


def _reserve_schema_row(
    row: tuple[object, ...],
    *,
    expected_length: int,
    counters: _StructureCounters,
    budget: ProbeBudget,
) -> None:
    if counters.schema_identifiers >= budget.max_schema_identifiers:
        raise _ProbeFilesystemError(ProbeWarningCode.BUDGET_EXCEEDED)
    counters.schema_identifiers += 1
    if len(row) != expected_length:
        raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA)


def _schema_text(value: object, *, allow_empty: bool) -> str:
    if type(value) is not str:
        raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA)
    if len(value) > _MAX_SCHEMA_VALUE_BYTES:
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8)
    try:
        encoded_size = strict_utf8_size(value)
    except InvalidUtf8Text:
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8) from None
    if (
        (not value and not allow_empty)
        or encoded_size > _MAX_SCHEMA_VALUE_BYTES
        or contains_unsafe_text_control(value)
    ):
        raise _ProbeFilesystemError(ProbeWarningCode.INVALID_UTF8)
    return value


def _schema_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA)
    return value


def _schema_fingerprint(
    rows: list[tuple[object, ...]],
    *,
    clock: ProbeClock,
    deadline: float,
) -> str:
    _check_deadline(clock, deadline)
    rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, separators=(",", ":")))
    document = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    fingerprint = f"sha256:{hashlib.sha256(document.encode('utf-8')).hexdigest()}"
    _check_deadline(clock, deadline)
    return fingerprint


def _execute_reviewed_count(
    connection: sqlite3.Connection,
    statement: str,
    *,
    clock: ProbeClock,
    deadline: float,
    abort_reason: list[ProbeWarningCode | None],
) -> int:
    if type(statement) is not str or len(statement) > 4_096:
        raise _ProbeFilesystemError(ProbeWarningCode.PROBE_FAILED)
    try:
        statement_size = strict_utf8_size(statement)
    except InvalidUtf8Text:
        raise _ProbeFilesystemError(ProbeWarningCode.PROBE_FAILED) from None
    if (
        statement_size > 4_096
        or not statement.casefold().startswith("select count(")
        or ";" in statement
        or "\x00" in statement
        or contains_unsafe_text_control(statement)
    ):
        raise _ProbeFilesystemError(ProbeWarningCode.PROBE_FAILED)
    try:
        _check_deadline(clock, deadline)
        cursor = connection.execute(statement)
        with _managed_sqlite_cursor(cursor):
            row = _next_cursor_row(
                cursor,
                clock=clock,
                deadline=deadline,
                abort_reason=abort_reason,
            )
            if row is None or len(row) != 1 or type(row[0]) is not int or row[0] < 0:
                raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA)
            if row[0] > 2**31 - 1:
                raise _ProbeFilesystemError(ProbeWarningCode.BUDGET_EXCEEDED)
            if (
                _next_cursor_row(
                    cursor,
                    clock=clock,
                    deadline=deadline,
                    abort_reason=abort_reason,
                )
                is not None
            ):
                raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA)
            return row[0]
    except sqlite3.Error as error:
        _raise_sqlite_query_error(
            error,
            abort_reason=abort_reason,
            clock=clock,
            deadline=deadline,
        )


def _raise_sqlite_query_error(
    _error: sqlite3.Error,
    *,
    abort_reason: list[ProbeWarningCode | None],
    clock: ProbeClock,
    deadline: float,
) -> NoReturn:
    if _deadline_reached(clock, deadline):
        raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
    if abort_reason[0] is not None:
        raise _ProbeFilesystemError(abort_reason[0]) from None
    raise _ProbeFilesystemError(ProbeWarningCode.MALFORMED_METADATA) from None


def _raise_sqlite_oserror(
    error: OSError,
    *,
    clock: ProbeClock,
    deadline: float,
    post_preview: bool,
) -> NoReturn:
    if _deadline_reached(clock, deadline):
        raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
    raise _ProbeFilesystemError(
        _stable_warning_for_oserror(error, post_preview=post_preview)
    ) from None


def _record_sqlite_exception(
    state: _SqliteResultState,
    error: Exception,
    *,
    clock: ProbeClock,
    deadline: float,
    post_preview: bool,
) -> None:
    if _deadline_reached(clock, deadline):
        _record_sqlite_warning(state, ProbeWarningCode.PROBE_TIMEOUT)
        return
    if isinstance(error, _ProbeFilesystemError):
        _record_sqlite_warning(state, error.code)
        return
    if isinstance(error, sqlite3.Error):
        _record_sqlite_warning(state, ProbeWarningCode.MALFORMED_METADATA)
        return
    if isinstance(error, OSError):
        _record_sqlite_warning(
            state,
            _stable_warning_for_oserror(error, post_preview=post_preview),
        )
        return
    _record_sqlite_warning(state, ProbeWarningCode.PROBE_FAILED)


def _record_sqlite_warning(
    state: _SqliteResultState,
    code: ProbeWarningCode,
) -> None:
    if code is ProbeWarningCode.PROBE_TIMEOUT:
        state.warnings.discard(ProbeWarningCode.BUDGET_EXCEEDED)
    state.warnings.add(code)


def _sqlite_structure_inspection(state: _SqliteResultState) -> StructureInspection:
    structure_status = StructureStatus.UNSUPPORTED
    if state.recognized:
        structure_status = (
            StructureStatus.PARTIAL
            if state.warnings & _DEGRADING_STRUCTURE_WARNINGS
            else StructureStatus.RECOGNIZED
        )
    return StructureInspection(
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        structure_status=structure_status,
        metrics=ProbeMetrics(
            sqlite_candidate_count=int(state.attempted),
            schema_object_count=state.schema_object_count,
            bounded_record_count=state.bounded_record_count,
            has_session_identifier=state.has_session_identifier,
            has_model_identifier_field=state.has_model_identifier_field,
        ),
        warning_codes=_sorted_warnings(state.warnings),
    )


def _inspect_sqlite_candidates(
    state: _TraversalState,
    *,
    recognized_schemas: tuple[RecognizedSchema, ...],
    budget: ProbeBudget,
    clock: ProbeClock,
    deadline: float,
) -> None:
    counters = _StructureCounters()
    inspector = SqliteMetadataInspector(budget, clock)
    for candidate in state.sqlite_candidate_entries:
        result = inspector.inspect(
            candidate,
            recognized_schemas=recognized_schemas,
            deadline=deadline,
            counters=counters,
        )
        for warning in result.warning_codes:
            _record_structure_error(state, warning)
        state.counters.schema_objects += result.metrics.schema_object_count
        state.counters.has_session_identifier |= result.metrics.has_session_identifier
        state.counters.has_model_identifier_field |= result.metrics.has_model_identifier_field
        count_was_required_but_failed = (
            result.structure_status is StructureStatus.PARTIAL
            and result.metrics.bounded_record_count is None
            and any(schema.bounded_count_query is not None for schema in recognized_schemas)
        )
        if count_was_required_but_failed:
            state.counters.bounded_record_count = None
            state.counters.bounded_record_count_blocked = True
        elif (
            result.metrics.bounded_record_count is not None
            and not state.counters.bounded_record_count_blocked
        ):
            current = state.counters.bounded_record_count or 0
            combined = current + result.metrics.bounded_record_count
            if combined > 2**31 - 1:
                state.counters.bounded_record_count = None
                state.counters.bounded_record_count_blocked = True
                _record_structure_error(state, ProbeWarningCode.BUDGET_EXCEEDED)
            else:
                state.counters.bounded_record_count = combined
        if result.structure_status in {StructureStatus.RECOGNIZED, StructureStatus.PARTIAL}:
            state.recognized_schema = True
        if any(
            warning in result.warning_codes
            for warning in (
                ProbeWarningCode.PROBE_TIMEOUT,
                ProbeWarningCode.BUDGET_EXCEEDED,
            )
        ):
            return


def _stable_warning_for_oserror(
    error: OSError,
    *,
    post_preview: bool,
) -> ProbeWarningCode:
    if post_preview and error.errno in _POST_PREVIEW_RACE_ERRNOS:
        return ProbeWarningCode.SOURCE_CHANGED
    if error.errno in {errno.EACCES, errno.EPERM}:
        return ProbeWarningCode.PERMISSION_BLOCKED
    if error.errno == errno.ELOOP:
        return ProbeWarningCode.SYMLINK_REJECTED
    if error.errno in {errno.ENOENT, errno.ENOTDIR}:
        return ProbeWarningCode.SOURCE_CHANGED if post_preview else ProbeWarningCode.SOURCE_MISSING
    return ProbeWarningCode.PROBE_FAILED


def _record_structure_error(state: _TraversalState, code: ProbeWarningCode) -> None:
    if code is ProbeWarningCode.PROBE_TIMEOUT:
        state.warnings.discard(ProbeWarningCode.BUDGET_EXCEEDED)
        state.warnings.add(code)
        state.stop = True
        return
    state.warnings.add(code)


def _record_structure_exception(
    state: _TraversalState,
    error: Exception,
    *,
    clock: ProbeClock,
    deadline: float,
    post_preview: bool,
) -> None:
    if _deadline_reached(clock, deadline):
        _record_structure_error(state, ProbeWarningCode.PROBE_TIMEOUT)
        return
    if isinstance(error, _ProbeFilesystemError):
        _record_structure_error(state, error.code)
        return
    if isinstance(error, OSError):
        _record_structure_error(
            state,
            _stable_warning_for_oserror(error, post_preview=post_preview),
        )
        return
    _record_structure_error(state, ProbeWarningCode.PROBE_FAILED)


def _structure_inspection(
    light: LightInspection,
    state: _TraversalState,
) -> StructureInspection:
    structure_status = StructureStatus.UNSUPPORTED
    if state.recognized_schema:
        structure_status = (
            StructureStatus.PARTIAL
            if state.warnings & _DEGRADING_STRUCTURE_WARNINGS
            else StructureStatus.RECOGNIZED
        )
    return StructureInspection(
        installation_status=light.installation_status,
        data_status=light.data_status,
        structure_status=structure_status,
        metrics=light.metrics.model_copy(
            update={
                "metadata_file_count": state.counters.metadata_files,
                "sqlite_candidate_count": state.counters.sqlite_candidates,
                "schema_object_count": state.counters.schema_objects,
                "bounded_record_count": state.counters.bounded_record_count,
                "has_session_identifier": state.counters.has_session_identifier,
                "has_model_identifier_field": state.counters.has_model_identifier_field,
            }
        ),
        warning_codes=_sorted_warnings(state.warnings),
    )


def _ancestor_identity(value: os.stat_result) -> tuple[int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
    )


def _open_verified_root(*, clock: ProbeClock, deadline: float) -> int:
    _check_deadline(clock, deadline)
    before = os.stat("/", follow_symlinks=False)
    _check_deadline(clock, deadline)
    if stat.S_ISLNK(before.st_mode):
        raise _ProbeFilesystemError(ProbeWarningCode.SYMLINK_REJECTED)
    if not stat.S_ISDIR(before.st_mode):
        raise _ProbeFilesystemError(ProbeWarningCode.UNSAFE_FILE_TYPE)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    _check_deadline(clock, deadline)
    try:
        fd = os.open("/", flags)
    except OSError as error:
        _raise_timeout_if_deadline_reached(clock, deadline)
        _raise_post_preview_race(error)
        raise
    try:
        _check_deadline(clock, deadline)
        _check_deadline(clock, deadline)
        opened = os.fstat(fd)
        _check_deadline(clock, deadline)
        _check_deadline(clock, deadline)
        try:
            after = os.stat("/", follow_symlinks=False)
        except OSError as error:
            _raise_timeout_if_deadline_reached(clock, deadline)
            _raise_post_preview_race(error)
            raise
        _check_deadline(clock, deadline)
        if _ancestor_identity(before) != _ancestor_identity(opened) or _ancestor_identity(
            opened
        ) != _ancestor_identity(after):
            raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
        if not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(after.st_mode):
            raise _ProbeFilesystemError(ProbeWarningCode.UNSAFE_FILE_TYPE)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_verified_component(
    component: str,
    *,
    parent_fd: int,
    is_final_component: bool,
    expected_directory: bool,
    expected_executable: bool,
    clock: ProbeClock,
    deadline: float,
) -> int:
    _check_deadline(clock, deadline)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if expected_directory:
        flags |= os.O_DIRECTORY
    elif expected_executable:
        flags |= os.O_NONBLOCK
    before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    _check_deadline(clock, deadline)
    if stat.S_ISLNK(before.st_mode):
        raise _ProbeFilesystemError(ProbeWarningCode.SYMLINK_REJECTED)
    if expected_directory and not stat.S_ISDIR(before.st_mode):
        raise _ProbeFilesystemError(ProbeWarningCode.UNSAFE_FILE_TYPE)
    if expected_executable and (not stat.S_ISREG(before.st_mode) or before.st_mode & 0o111 == 0):
        raise _ProbeFilesystemError(ProbeWarningCode.UNSAFE_FILE_TYPE)
    _check_deadline(clock, deadline)
    try:
        fd = os.open(component, flags, dir_fd=parent_fd)
    except OSError as error:
        _raise_timeout_if_deadline_reached(clock, deadline)
        _raise_post_preview_race(error)
        raise
    try:
        _check_deadline(clock, deadline)
        _check_deadline(clock, deadline)
        opened = os.fstat(fd)
        _check_deadline(clock, deadline)
        _check_deadline(clock, deadline)
        try:
            after = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as error:
            _raise_timeout_if_deadline_reached(clock, deadline)
            _raise_post_preview_race(error)
            raise
        _check_deadline(clock, deadline)
        identity = _identity if is_final_component else _ancestor_identity
        if identity(before) != identity(opened) or identity(opened) != identity(after):
            raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
        if expected_directory and (
            not stat.S_ISDIR(opened.st_mode) or not stat.S_ISDIR(after.st_mode)
        ):
            raise _ProbeFilesystemError(ProbeWarningCode.UNSAFE_FILE_TYPE)
        if expected_executable and any(
            not stat.S_ISREG(value.st_mode) or value.st_mode & 0o111 == 0
            for value in (before, opened, after)
        ):
            raise _ProbeFilesystemError(ProbeWarningCode.UNSAFE_FILE_TYPE)
        return fd
    except BaseException:
        os.close(fd)
        raise


def _sorted_warnings(
    warnings: set[ProbeWarningCode],
) -> tuple[ProbeWarningCode, ...]:
    return tuple(sorted(warnings, key=lambda item: item.value))


def _timeout_inspection() -> LightInspection:
    return LightInspection(
        installation_status=InstallationStatus.NOT_DETECTED,
        data_status=DataStatus.MISSING,
        metrics=ProbeMetrics(),
        warning_codes=(ProbeWarningCode.PROBE_TIMEOUT,),
    )


def _raise_post_preview_race(error: OSError) -> None:
    if error.errno in _POST_PREVIEW_RACE_ERRNOS:
        raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED) from None


def _raise_timeout_if_deadline_reached(clock: ProbeClock, deadline: float) -> None:
    if _deadline_reached(clock, deadline):
        raise _ProbeFilesystemError(ProbeWarningCode.PROBE_TIMEOUT) from None
