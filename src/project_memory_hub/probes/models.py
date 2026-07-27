from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from project_memory_hub.domain import SourceAgent


_INTEGER_BUDGET_HARD_MAXIMUMS = (
    ("max_depth", 4),
    ("max_entries", 2_048),
    ("max_candidate_files", 64),
    ("max_sqlite_candidates", 4),
    ("max_sqlite_file_bytes", 64 * 1024 * 1024),
    ("max_sqlite_total_bytes", 128 * 1024 * 1024),
    ("max_sqlite_vm_steps", 100_000),
    ("max_header_bytes", 64),
    ("max_total_header_bytes", 4 * 1024),
    ("max_schema_identifiers", 2_048),
    ("light_max_targets_per_source", 16),
)

_TIMEOUT_BUDGET_HARD_MAXIMUMS = (
    ("structure_timeout_seconds", 3.0),
    ("light_all_timeout_seconds", 2.0),
)


class ProbeMode(StrEnum):
    LIGHT = "light"
    STRUCTURE = "structure"


class InstallationStatus(StrEnum):
    DETECTED = "detected"
    NOT_DETECTED = "not_detected"


class DataStatus(StrEnum):
    READABLE = "readable"
    BLOCKED = "blocked"
    MISSING = "missing"
    REJECTED = "rejected"


class ProbeCapability(StrEnum):
    PRESENCE_AND_ACCESS = "presence_and_access"
    STRUCTURE_METADATA = "structure_metadata"


class StructureStatus(StrEnum):
    NOT_RUN = "not_run"
    RECOGNIZED = "recognized"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ModelStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    UNVERIFIABLE = "unverifiable"


class ProbeWarningCode(StrEnum):
    SOURCE_MISSING = "source_missing"
    PERMISSION_BLOCKED = "permission_blocked"
    SYMLINK_REJECTED = "symlink_rejected"
    UNSAFE_FILE_TYPE = "unsafe_file_type"
    UNSUPPORTED_FORMAT = "unsupported_format"
    MALFORMED_METADATA = "malformed_metadata"
    INVALID_UTF8 = "invalid_utf8"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROBE_TIMEOUT = "probe_timeout"
    SOURCE_CHANGED = "source_changed"
    MODEL_ID_UNVERIFIABLE = "model_id_unverifiable"
    PROBE_BUSY = "probe_busy"
    PROBE_FAILED = "probe_failed"


@dataclass(frozen=True, slots=True)
class ProbeBudget:
    max_depth: int = 4
    max_entries: int = 2_048
    max_candidate_files: int = 64
    max_sqlite_candidates: int = 4
    max_sqlite_file_bytes: int = 64 * 1024 * 1024
    max_sqlite_total_bytes: int = 128 * 1024 * 1024
    max_sqlite_vm_steps: int = 100_000
    max_header_bytes: int = 64
    max_total_header_bytes: int = 4 * 1024
    max_schema_identifiers: int = 2_048
    structure_timeout_seconds: float = 3.0
    light_max_targets_per_source: int = 16
    light_all_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name, integer_maximum in _INTEGER_BUDGET_HARD_MAXIMUMS:
            integer_value = getattr(self, name)
            if type(integer_value) is not int or integer_value <= 0:
                raise ValueError("probe budget integers must be positive")
            if integer_value > integer_maximum:
                raise ValueError("probe budget exceeds hard maximum")
        for name, timeout_maximum in _TIMEOUT_BUDGET_HARD_MAXIMUMS:
            timeout_value = getattr(self, name)
            if (
                type(timeout_value) is not float
                or not isfinite(timeout_value)
                or timeout_value <= 0
            ):
                raise ValueError("probe budget timeouts must be positive floats")
            if timeout_value > timeout_maximum:
                raise ValueError("probe budget exceeds hard maximum")


class SourceProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_agent: SourceAgent
    mode: ProbeMode = ProbeMode.LIGHT


class ProbeMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    checked_installation_marker_count: int = Field(default=0, ge=0, le=2**31 - 1)
    detected_installation_marker_count: int = Field(default=0, ge=0, le=2**31 - 1)
    checked_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    readable_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    blocked_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    missing_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    rejected_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    metadata_file_count: int = Field(default=0, ge=0, le=2**31 - 1)
    sqlite_candidate_count: int = Field(default=0, ge=0, le=2**31 - 1)
    schema_object_count: int = Field(default=0, ge=0, le=2**31 - 1)
    bounded_record_count: int | None = Field(default=None, ge=0, le=2**31 - 1)
    has_session_identifier: bool = False
    has_model_identifier_field: bool = False


class SourceProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_agent: SourceAgent
    mode: ProbeMode
    installation_status: InstallationStatus
    data_status: DataStatus
    capability: ProbeCapability
    structure_status: StructureStatus
    model_status: ModelStatus
    ingestion_allowed: Literal[False] = False
    metrics: ProbeMetrics
    warning_codes: tuple[ProbeWarningCode, ...] = ()
    checked_at: datetime

    @field_validator("ingestion_allowed", mode="before")
    @classmethod
    def require_literal_false(cls, value: object) -> Literal[False]:
        if value is not False:
            raise ValueError("ingestion_allowed must be false")
        return False

    @field_validator("warning_codes")
    @classmethod
    def normalize_warnings(
        cls, value: tuple[ProbeWarningCode, ...]
    ) -> tuple[ProbeWarningCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("checked_at")
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LightInspection:
    installation_status: InstallationStatus
    data_status: DataStatus
    metrics: ProbeMetrics
    warning_codes: tuple[ProbeWarningCode, ...] = ()

    def __post_init__(self) -> None:
        _validate_inspection_fields(
            self.installation_status,
            self.data_status,
            self.metrics,
            self.warning_codes,
        )


@dataclass(frozen=True, slots=True)
class StructureInspection:
    installation_status: InstallationStatus
    data_status: DataStatus
    structure_status: StructureStatus
    metrics: ProbeMetrics
    warning_codes: tuple[ProbeWarningCode, ...] = ()

    def __post_init__(self) -> None:
        _validate_inspection_fields(
            self.installation_status,
            self.data_status,
            self.metrics,
            self.warning_codes,
        )
        if not isinstance(self.structure_status, StructureStatus):
            raise TypeError("structure_status must be a StructureStatus")


def _validate_inspection_fields(
    installation_status: object,
    data_status: object,
    metrics: object,
    warning_codes: object,
) -> None:
    if not isinstance(installation_status, InstallationStatus):
        raise TypeError("installation_status must be an InstallationStatus")
    if not isinstance(data_status, DataStatus):
        raise TypeError("data_status must be a DataStatus")
    if type(metrics) is not ProbeMetrics:
        raise TypeError("metrics must be a ProbeMetrics instance")
    if type(warning_codes) is not tuple:
        raise TypeError("warning_codes must be an exact tuple")
    if any(not isinstance(item, ProbeWarningCode) for item in warning_codes):
        raise TypeError("warning_codes must contain only ProbeWarningCode values")
