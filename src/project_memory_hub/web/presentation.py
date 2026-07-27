from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Protocol, TypeVar


class _SourcePresentationRecord(Protocol):
    @property
    def available(self) -> bool: ...

    @property
    def probe(self) -> object | None: ...


_SourceRecordT = TypeVar("_SourceRecordT", bound=_SourcePresentationRecord)


@dataclass(frozen=True, slots=True)
class SourceRecordGroups(Generic[_SourceRecordT]):
    ingestion: tuple[_SourceRecordT, ...]
    probes: tuple[_SourceRecordT, ...]


def group_source_records(
    records: Iterable[_SourceRecordT],
) -> SourceRecordGroups[_SourceRecordT]:
    """Group existing source records without deriving capabilities from labels."""
    ingestion: list[_SourceRecordT] = []
    probes: list[_SourceRecordT] = []
    for record in records:
        if record.available and record.probe is None:
            ingestion.append(record)
        elif not record.available and record.probe is not None:
            probes.append(record)
        else:
            raise ValueError("source capability state is contradictory")
    return SourceRecordGroups(ingestion=tuple(ingestion), probes=tuple(probes))


class NextSafeStepKind(StrEnum):
    DISCOVER = "discover"
    SCAN = "scan"
    DOCTOR = "doctor"
    RECONCILE = "reconcile"


@dataclass(frozen=True, slots=True)
class NextSafeStep:
    kind: NextSafeStepKind
    command: str
    reason: str
    success_condition: str


_STEPS = {
    NextSafeStepKind.DISCOVER: NextSafeStep(
        kind=NextSafeStepKind.DISCOVER,
        command="memory-hub discover --dry-run --format json",
        reason="No project is registered yet, so preview discovery before changing the store.",
        success_condition="The preview lists only the project candidates you expect to review.",
    ),
    NextSafeStepKind.SCAN: NextSafeStep(
        kind=NextSafeStepKind.SCAN,
        command='memory-hub scan --cwd "$PWD" --dry-run --format json',
        reason=(
            "A project is registered, but no shared fact has been recorded yet. "
            "Run this preview from that registered project directory."
        ),
        success_condition="The dry run reports reviewable facts without changing the store.",
    ),
    NextSafeStepKind.DOCTOR: NextSafeStep(
        kind=NextSafeStepKind.DOCTOR,
        command="memory-hub doctor --format json",
        reason="A permission problem or degraded startup needs diagnosis before more ingestion.",
        success_condition="Doctor reports no unexplained failure, or names one bounded repair.",
    ),
    NextSafeStepKind.RECONCILE: NextSafeStep(
        kind=NextSafeStepKind.RECONCILE,
        command="memory-hub reconcile --if-due --format json",
        reason="The local store is ready for its next due maintenance pass.",
        success_condition="Reconcile reports success or skipped without a permission error.",
    ),
}


def select_next_safe_step(
    *,
    project_count: int,
    fact_count: int,
    permission_error_count: int,
    startup_status: str,
) -> NextSafeStep:
    """Choose one display-only command from the state already shown on Overview."""
    _validate_count("project_count", project_count)
    _validate_count("fact_count", fact_count)
    _validate_count("permission_error_count", permission_error_count)

    if project_count == 0:
        return _STEPS[NextSafeStepKind.DISCOVER]
    if permission_error_count > 0 or startup_status == "degraded":
        return _STEPS[NextSafeStepKind.DOCTOR]
    if fact_count == 0:
        return _STEPS[NextSafeStepKind.SCAN]
    return _STEPS[NextSafeStepKind.RECONCILE]


def _validate_count(name: str, value: int) -> None:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
