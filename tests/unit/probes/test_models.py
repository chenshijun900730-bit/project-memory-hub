from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime, timedelta, timezone
from math import inf, nan
import os

import pytest
from pydantic import ValidationError

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes import (
    DataStatus,
    ExpectedPathType,
    InstallationStatus,
    LightInspection,
    ModelStatus,
    ProbeBudget,
    ProbeCapability,
    ProbeMetrics,
    ProbeMode,
    ProbeWarningCode,
    RecognizedSchema,
    SourceDescriptor,
    SourceProbeRequest,
    SourceProbeResult,
    StructureInspection,
    StructureStatus,
    SystemProbeClock,
    TrustedAnchor,
    TrustedPath,
)


_INTEGER_BUDGET_FIELDS = (
    "max_depth",
    "max_entries",
    "max_candidate_files",
    "max_sqlite_candidates",
    "max_sqlite_file_bytes",
    "max_sqlite_total_bytes",
    "max_sqlite_vm_steps",
    "max_header_bytes",
    "max_total_header_bytes",
    "max_schema_identifiers",
    "light_max_targets_per_source",
)

_TIMEOUT_BUDGET_FIELDS = (
    "structure_timeout_seconds",
    "light_all_timeout_seconds",
)

_BUDGET_HARD_MAXIMUMS: tuple[tuple[str, int | float], ...] = (
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
    ("structure_timeout_seconds", 3.0),
    ("light_max_targets_per_source", 16),
    ("light_all_timeout_seconds", 2.0),
)

_INTEGER_METRIC_FIELDS = (
    "checked_installation_marker_count",
    "detected_installation_marker_count",
    "checked_data_root_count",
    "readable_data_root_count",
    "blocked_data_root_count",
    "missing_data_root_count",
    "rejected_data_root_count",
    "metadata_file_count",
    "sqlite_candidate_count",
    "schema_object_count",
    "bounded_record_count",
)


def _valid_result_dict() -> dict[str, object]:
    return {
        "source_agent": SourceAgent.TRAE,
        "mode": ProbeMode.LIGHT,
        "installation_status": InstallationStatus.NOT_DETECTED,
        "data_status": DataStatus.MISSING,
        "capability": ProbeCapability.STRUCTURE_METADATA,
        "structure_status": StructureStatus.NOT_RUN,
        "model_status": ModelStatus.NOT_CHECKED,
        "ingestion_allowed": False,
        "metrics": ProbeMetrics(),
        "warning_codes": (ProbeWarningCode.SOURCE_MISSING,),
        "checked_at": datetime(2026, 7, 17, tzinfo=UTC),
    }


def _trusted_directory() -> TrustedPath:
    return TrustedPath(
        anchor=TrustedAnchor.HOME,
        components=("Library", "Application Support", "Trae"),
        expected_type=ExpectedPathType.DIRECTORY,
    )


def _recognized_schema() -> RecognizedSchema:
    return RecognizedSchema(
        fingerprint="sha256:reviewed",
        session_identifier_fields=frozenset({"session_id"}),
        model_identifier_fields=frozenset({"model_id"}),
        bounded_count_query="SELECT COUNT(*) FROM reviewed_metadata",
    )


def _source_descriptor() -> SourceDescriptor:
    path = _trusted_directory()
    return SourceDescriptor(
        source_agent=SourceAgent.TRAE,
        installation_markers=(path,),
        data_roots=(path,),
        capability=ProbeCapability.STRUCTURE_METADATA,
        recognized_schemas=(_recognized_schema(),),
    )


def test_probe_enums_have_exact_stable_values() -> None:
    assert tuple(ProbeMode) == (ProbeMode.LIGHT, ProbeMode.STRUCTURE)
    assert tuple(InstallationStatus) == (
        InstallationStatus.DETECTED,
        InstallationStatus.NOT_DETECTED,
    )
    assert tuple(DataStatus) == (
        DataStatus.READABLE,
        DataStatus.BLOCKED,
        DataStatus.MISSING,
        DataStatus.REJECTED,
    )
    assert tuple(ProbeCapability) == (
        ProbeCapability.PRESENCE_AND_ACCESS,
        ProbeCapability.STRUCTURE_METADATA,
    )
    assert tuple(StructureStatus) == (
        StructureStatus.NOT_RUN,
        StructureStatus.RECOGNIZED,
        StructureStatus.PARTIAL,
        StructureStatus.UNSUPPORTED,
    )
    assert tuple(ModelStatus) == (ModelStatus.NOT_CHECKED, ModelStatus.UNVERIFIABLE)
    assert tuple(ProbeWarningCode) == (
        ProbeWarningCode.SOURCE_MISSING,
        ProbeWarningCode.PERMISSION_BLOCKED,
        ProbeWarningCode.SYMLINK_REJECTED,
        ProbeWarningCode.UNSAFE_FILE_TYPE,
        ProbeWarningCode.UNSUPPORTED_FORMAT,
        ProbeWarningCode.MALFORMED_METADATA,
        ProbeWarningCode.INVALID_UTF8,
        ProbeWarningCode.BUDGET_EXCEEDED,
        ProbeWarningCode.PROBE_TIMEOUT,
        ProbeWarningCode.SOURCE_CHANGED,
        ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
        ProbeWarningCode.PROBE_BUSY,
        ProbeWarningCode.PROBE_FAILED,
    )


def test_probe_budget_has_exact_frozen_defaults() -> None:
    budget = ProbeBudget()
    assert asdict(budget) == {
        "max_depth": 4,
        "max_entries": 2_048,
        "max_candidate_files": 64,
        "max_sqlite_candidates": 4,
        "max_sqlite_file_bytes": 64 * 1024 * 1024,
        "max_sqlite_total_bytes": 128 * 1024 * 1024,
        "max_sqlite_vm_steps": 100_000,
        "max_header_bytes": 64,
        "max_total_header_bytes": 4 * 1024,
        "max_schema_identifiers": 2_048,
        "structure_timeout_seconds": 3.0,
        "light_max_targets_per_source": 16,
        "light_all_timeout_seconds": 2.0,
    }
    with pytest.raises(FrozenInstanceError):
        budget.max_depth = 8  # type: ignore[misc]


@pytest.mark.parametrize("field", _INTEGER_BUDGET_FIELDS)
@pytest.mark.parametrize("value", [True, False, 0, -1])
def test_integer_probe_budget_fields_reject_bool_zero_and_negative_values(
    field: str, value: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProbeBudget(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", _TIMEOUT_BUDGET_FIELDS)
@pytest.mark.parametrize("value", [True, False, 0.0, -1.0, 1])
def test_timeout_probe_budget_fields_reject_wrong_type_zero_and_negative_values(
    field: str, value: object
) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProbeBudget(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", _TIMEOUT_BUDGET_FIELDS)
@pytest.mark.parametrize("value", [nan, inf, -inf])
def test_timeout_probe_budget_fields_reject_non_finite_values(field: str, value: float) -> None:
    with pytest.raises(ValueError):
        ProbeBudget(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(("field", "maximum"), _BUDGET_HARD_MAXIMUMS)
def test_probe_budget_rejects_values_above_hard_maximum(field: str, maximum: int | float) -> None:
    increment: int | float = 1 if type(maximum) is int else 0.001
    with pytest.raises(ValueError, match="hard maximum"):
        ProbeBudget(**{field: maximum + increment})  # type: ignore[arg-type]


@pytest.mark.parametrize(("field", "maximum"), _BUDGET_HARD_MAXIMUMS)
def test_probe_budget_accepts_smaller_positive_values(field: str, maximum: int | float) -> None:
    smaller: int | float = maximum // 2 if type(maximum) is int else maximum / 2
    budget = ProbeBudget(**{field: smaller})  # type: ignore[arg-type]
    assert getattr(budget, field) == smaller


def test_source_probe_request_is_strict_frozen_and_forbids_extra_fields() -> None:
    request = SourceProbeRequest(source_agent=SourceAgent.TRAE)
    assert request.mode is ProbeMode.LIGHT

    with pytest.raises(ValidationError):
        SourceProbeRequest.model_validate({"source_agent": "trae"})
    with pytest.raises(ValidationError):
        SourceProbeRequest.model_validate({"source_agent": SourceAgent.TRAE, "unexpected": "value"})
    with pytest.raises(ValidationError):
        request.mode = ProbeMode.STRUCTURE


def test_probe_metrics_has_exact_defaults() -> None:
    assert ProbeMetrics().model_dump() == {
        "checked_installation_marker_count": 0,
        "detected_installation_marker_count": 0,
        "checked_data_root_count": 0,
        "readable_data_root_count": 0,
        "blocked_data_root_count": 0,
        "missing_data_root_count": 0,
        "rejected_data_root_count": 0,
        "metadata_file_count": 0,
        "sqlite_candidate_count": 0,
        "schema_object_count": 0,
        "bounded_record_count": None,
        "has_session_identifier": False,
        "has_model_identifier_field": False,
    }


@pytest.mark.parametrize("field", _INTEGER_METRIC_FIELDS)
def test_probe_metrics_reject_counts_above_the_public_bound(field: str) -> None:
    with pytest.raises(ValidationError):
        ProbeMetrics.model_validate({field: 2**31})


@pytest.mark.parametrize("field", _INTEGER_METRIC_FIELDS)
def test_probe_metrics_accept_counts_at_the_public_bound(field: str) -> None:
    metrics = ProbeMetrics.model_validate({field: 2**31 - 1})
    assert getattr(metrics, field) == 2**31 - 1


@pytest.mark.parametrize("field", _INTEGER_METRIC_FIELDS)
@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_probe_metrics_count_fields_reject_bool_float_and_string(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ProbeMetrics.model_validate({field: value})


@pytest.mark.parametrize("field", ["has_session_identifier", "has_model_identifier_field"])
@pytest.mark.parametrize("value", [0, 1, "false", "true"])
def test_probe_metrics_capability_fields_require_strict_booleans(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ProbeMetrics.model_validate({field: value})


def test_probe_metrics_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ProbeMetrics.model_validate({"raw_table_name": "sessions"})


def test_result_is_strict_sorted_utc_and_never_ingestable() -> None:
    result = SourceProbeResult(
        source_agent=SourceAgent.TRAE,
        mode=ProbeMode.STRUCTURE,
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        capability=ProbeCapability.STRUCTURE_METADATA,
        structure_status=StructureStatus.UNSUPPORTED,
        model_status=ModelStatus.UNVERIFIABLE,
        ingestion_allowed=False,
        metrics=ProbeMetrics(),
        warning_codes=(
            ProbeWarningCode.SOURCE_MISSING,
            ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
            ProbeWarningCode.SOURCE_MISSING,
        ),
        checked_at=datetime(2026, 7, 17, 8, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    assert result.ingestion_allowed is False
    assert result.warning_codes == (
        ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
        ProbeWarningCode.SOURCE_MISSING,
    )
    assert result.checked_at == datetime(2026, 7, 17, tzinfo=UTC)


@pytest.mark.parametrize("value", [True, 0, 1, "false"])
def test_result_rejects_non_false_ingestion_values(value: object) -> None:
    with pytest.raises(ValidationError):
        payload = _valid_result_dict()
        payload["ingestion_allowed"] = value
        SourceProbeResult.model_validate(payload)


def test_result_rejects_naive_time_strings_and_extra_fields() -> None:
    for field, value in (
        ("checked_at", datetime(2026, 7, 17)),
        ("checked_at", "2026-07-17T00:00:00Z"),
        ("source_agent", "trae"),
        ("warning_codes", [ProbeWarningCode.SOURCE_MISSING]),
    ):
        payload = _valid_result_dict()
        payload[field] = value
        with pytest.raises(ValidationError):
            SourceProbeResult.model_validate(payload)

    payload = _valid_result_dict()
    payload["absolute_path"] = "/private/user-data"
    with pytest.raises(ValidationError):
        SourceProbeResult.model_validate(payload)


def test_system_probe_clock_returns_aware_utc_now_and_monotonic_float() -> None:
    clock = SystemProbeClock()
    assert clock.now().tzinfo is UTC
    assert isinstance(clock.monotonic(), float)


@pytest.mark.parametrize(
    "component",
    ["", ".", "..", "a/b", "a\x00b", "a\nb", "a\x7fb", "a" * 256, "\ud800"],
)
def test_trusted_path_rejects_dynamic_components(component: str) -> None:
    with pytest.raises(ValueError):
        TrustedPath(
            anchor=TrustedAnchor.HOME,
            components=(component,),
            expected_type=ExpectedPathType.DIRECTORY,
        )


def test_trusted_path_rejects_non_string_components_explicitly() -> None:
    with pytest.raises(TypeError, match="exact strings"):
        TrustedPath(
            anchor=TrustedAnchor.HOME,
            components=(42,),  # type: ignore[arg-type]
            expected_type=ExpectedPathType.DIRECTORY,
        )


def test_trusted_path_rejects_str_subclass_that_masks_parent_component() -> None:
    class MaliciousComponent(str):
        def __new__(cls) -> "MaliciousComponent":
            return super().__new__(cls, "..")

        def __hash__(self) -> int:
            return hash("safe")

        def __eq__(self, other: object) -> bool:
            return False

        def __contains__(self, item: object) -> bool:
            return False

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter("safe")

        def encode(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return b"safe"

    component = MaliciousComponent()
    assert str.__eq__(os.fspath(component), "..") is True
    with pytest.raises(TypeError, match="exact strings"):
        TrustedPath(
            anchor=TrustedAnchor.HOME,
            components=(component,),
            expected_type=ExpectedPathType.DIRECTORY,
        )


def test_trusted_path_rejects_mutable_components_container() -> None:
    components = ["safe"]
    with pytest.raises(TypeError, match="exact tuple"):
        TrustedPath(
            anchor=TrustedAnchor.HOME,
            components=components,  # type: ignore[arg-type]
            expected_type=ExpectedPathType.DIRECTORY,
        )
    components.append("later-mutation")


@pytest.mark.parametrize(
    ("anchor", "expected_type"),
    [
        ("home", ExpectedPathType.DIRECTORY),
        (TrustedAnchor.HOME, "directory"),
    ],
)
def test_trusted_path_rejects_non_enum_scalar_values(anchor: object, expected_type: object) -> None:
    with pytest.raises(TypeError):
        TrustedPath(
            anchor=anchor,  # type: ignore[arg-type]
            components=("safe",),
            expected_type=expected_type,  # type: ignore[arg-type]
        )


def test_trusted_path_accepts_255_utf8_bytes_and_rejects_256() -> None:
    accepted = TrustedPath(
        anchor=TrustedAnchor.HOME,
        components=("a" * 255,),
        expected_type=ExpectedPathType.DIRECTORY,
    )
    assert accepted.components == ("a" * 255,)

    with pytest.raises(ValueError):
        TrustedPath(
            anchor=TrustedAnchor.HOME,
            components=("a" * 256,),
            expected_type=ExpectedPathType.DIRECTORY,
        )


def test_trusted_path_requires_components_and_is_frozen() -> None:
    with pytest.raises(ValueError):
        TrustedPath(
            anchor=TrustedAnchor.HOME,
            components=(),
            expected_type=ExpectedPathType.DIRECTORY,
        )

    path = TrustedPath(
        anchor=TrustedAnchor.HOME,
        components=("Library", "Application Support", "Trae"),
        expected_type=ExpectedPathType.DIRECTORY,
    )
    with pytest.raises(FrozenInstanceError):
        path.components = ("other",)  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("session_identifier_fields", {"session_id"}),
        ("model_identifier_fields", ["model_id"]),
    ],
)
def test_recognized_schema_rejects_non_exact_frozensets(field: str, value: object) -> None:
    payload: dict[str, object] = {"fingerprint": "sha256:reviewed", field: value}
    with pytest.raises(TypeError, match="exact frozenset"):
        RecognizedSchema(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("fingerprint", 42),
        ("bounded_count_query", 42),
        ("session_identifier_fields", frozenset({42})),
        ("model_identifier_fields", frozenset({42})),
    ],
)
def test_recognized_schema_rejects_invalid_scalar_and_member_types(
    field: str, value: object
) -> None:
    payload: dict[str, object] = {"fingerprint": "sha256:reviewed", field: value}
    with pytest.raises(TypeError):
        RecognizedSchema(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    ["installation_markers", "data_roots", "recognized_schemas"],
)
def test_source_descriptor_rejects_mutable_tuple_fields(field: str) -> None:
    path = _trusted_directory()
    payload: dict[str, object] = {
        "source_agent": SourceAgent.TRAE,
        "installation_markers": (path,),
        "data_roots": (path,),
        "capability": ProbeCapability.STRUCTURE_METADATA,
        "recognized_schemas": (_recognized_schema(),),
    }
    payload[field] = list(payload[field])  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="exact tuple"):
        SourceDescriptor(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_agent", "trae"),
        ("capability", "structure_metadata"),
        ("installation_markers", ("unsafe",)),
        ("data_roots", ("unsafe",)),
        ("recognized_schemas", ("unsafe",)),
    ],
)
def test_source_descriptor_rejects_invalid_scalar_and_member_types(
    field: str, value: object
) -> None:
    descriptor = _source_descriptor()
    payload: dict[str, object] = {
        "source_agent": descriptor.source_agent,
        "installation_markers": descriptor.installation_markers,
        "data_roots": descriptor.data_roots,
        "capability": descriptor.capability,
        "recognized_schemas": descriptor.recognized_schemas,
    }
    payload[field] = value
    with pytest.raises(TypeError):
        SourceDescriptor(**payload)  # type: ignore[arg-type]


@pytest.mark.parametrize("inspection_type", [LightInspection, StructureInspection])
def test_inspections_reject_mutable_warning_containers(inspection_type: type[object]) -> None:
    payload: dict[str, object] = {
        "installation_status": InstallationStatus.DETECTED,
        "data_status": DataStatus.READABLE,
        "metrics": ProbeMetrics(),
        "warning_codes": [ProbeWarningCode.SOURCE_MISSING],
    }
    if inspection_type is StructureInspection:
        payload["structure_status"] = StructureStatus.UNSUPPORTED
    with pytest.raises(TypeError, match="exact tuple"):
        inspection_type(**payload)  # type: ignore[call-arg]


@pytest.mark.parametrize("inspection_type", [LightInspection, StructureInspection])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("installation_status", "detected"),
        ("data_status", "readable"),
        ("metrics", {}),
        ("warning_codes", ("source_missing",)),
    ],
)
def test_inspections_reject_invalid_scalar_and_warning_member_types(
    inspection_type: type[object], field: str, value: object
) -> None:
    payload: dict[str, object] = {
        "installation_status": InstallationStatus.DETECTED,
        "data_status": DataStatus.READABLE,
        "metrics": ProbeMetrics(),
        "warning_codes": (ProbeWarningCode.SOURCE_MISSING,),
    }
    if inspection_type is StructureInspection:
        payload["structure_status"] = StructureStatus.UNSUPPORTED
    payload[field] = value
    with pytest.raises(TypeError):
        inspection_type(**payload)  # type: ignore[call-arg]


def test_structure_inspection_rejects_non_enum_structure_status() -> None:
    with pytest.raises(TypeError):
        StructureInspection(
            installation_status=InstallationStatus.DETECTED,
            data_status=DataStatus.READABLE,
            structure_status="unsupported",  # type: ignore[arg-type]
            metrics=ProbeMetrics(),
        )
