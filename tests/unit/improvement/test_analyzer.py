from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest
from pydantic import ValidationError

import project_memory_hub.improvement.analyzer as analyzer_module
from project_memory_hub.improvement.analyzer import (
    HealthSnapshot,
    ImprovementAnalyzer,
)


FIELDS = (
    "discovery_failure_count",
    "permission_failure_count",
    "adapter_failure_count",
    "retry_failure_count",
    "retry_remaining_count",
    "inserted_count",
    "duplicate_count",
    "duplicate_candidate_count",
    "compaction_failure_count",
    "compaction_remaining_count",
)
MAX_COUNT = 2**31 - 1


def _snapshot(**updates: int) -> HealthSnapshot:
    values = dict.fromkeys(FIELDS, 0)
    values.update(updates)
    return HealthSnapshot(**values)


def test_health_snapshot_is_exact_frozen_strict_and_bounded() -> None:
    snapshot = _snapshot(discovery_failure_count=MAX_COUNT)

    assert tuple(HealthSnapshot.model_fields) == FIELDS
    assert snapshot.discovery_failure_count == MAX_COUNT
    with pytest.raises(ValidationError):
        snapshot.discovery_failure_count = 0  # type: ignore[misc]
    with pytest.raises(ValidationError):
        HealthSnapshot(**dict(snapshot.model_dump(), unknown_count=0))


@pytest.mark.parametrize("field", FIELDS)
@pytest.mark.parametrize(
    "invalid",
    (-1, 2**31, True, False, "1", Path("/private/project"), {"count": 1}),
)
def test_health_snapshot_rejects_non_integer_or_out_of_range_values(
    field: str, invalid: object
) -> None:
    values: dict[str, object] = dict.fromkeys(FIELDS, 0)
    values[field] = invalid

    with pytest.raises(ValidationError):
        HealthSnapshot(**values)


@pytest.mark.parametrize("invalid", (-1, 2**31, True, "1", Path("/private")))
def test_analyzer_revalidates_model_construct_bypass(invalid: object) -> None:
    snapshot = _snapshot().model_copy(update={"discovery_failure_count": invalid})

    with pytest.raises((TypeError, ValueError, ValidationError)):
        ImprovementAnalyzer().analyze(snapshot)


@pytest.mark.parametrize(
    ("updates", "signature"),
    (
        (
            {"discovery_failure_count": 1},
            "analyzer.health.v1.discovery_health.gte_1",
        ),
        (
            {"adapter_failure_count": 1},
            "analyzer.health.v1.adapter_failure.gte_1",
        ),
        (
            {"retry_failure_count": 1},
            "analyzer.health.v1.retry_backlog.gte_1",
        ),
        (
            {"retry_remaining_count": 1},
            "analyzer.health.v1.retry_backlog.gte_1",
        ),
        (
            {"duplicate_candidate_count": 1},
            "analyzer.health.v1.duplicate_pressure.gte_1",
        ),
        (
            {"compaction_failure_count": 1},
            "analyzer.health.v1.compaction_health.gte_1",
        ),
        (
            {"compaction_remaining_count": 1},
            "analyzer.health.v1.compaction_health.gte_1",
        ),
    ),
)
def test_each_minimum_threshold_emits_only_its_stable_draft(
    updates: dict[str, int], signature: str
) -> None:
    drafts = ImprovementAnalyzer().analyze(_snapshot(**updates))

    assert [draft.signature for draft in drafts] == [signature]


def test_zero_snapshot_and_below_minimum_duplicate_sample_emit_nothing() -> None:
    analyzer = ImprovementAnalyzer()

    assert analyzer.analyze(_snapshot()) == []
    assert analyzer.analyze(_snapshot(inserted_count=0, duplicate_count=19)) == []
    assert analyzer.analyze(_snapshot(permission_failure_count=MAX_COUNT)) == []


def test_duplicate_pressure_uses_exact_integer_8000_basis_point_boundary() -> None:
    analyzer = ImprovementAnalyzer()

    exact = analyzer.analyze(_snapshot(inserted_count=4, duplicate_count=16))
    below = analyzer.analyze(_snapshot(inserted_count=2001, duplicate_count=8000))

    assert [draft.signature for draft in exact] == ["analyzer.health.v1.duplicate_pressure.gte_1"]
    assert below == []
    assert "8000" in exact[0].description


def test_all_findings_have_fixed_order_static_titles_and_no_execution_capability() -> None:
    snapshot = _snapshot(
        discovery_failure_count=2,
        permission_failure_count=1,
        adapter_failure_count=3,
        retry_failure_count=4,
        retry_remaining_count=5,
        inserted_count=4,
        duplicate_count=16,
        duplicate_candidate_count=6,
        compaction_failure_count=7,
        compaction_remaining_count=8,
    )
    analyzer = ImprovementAnalyzer()

    first = analyzer.analyze(snapshot)
    second = analyzer.analyze(snapshot)

    assert first == second
    assert [draft.signature for draft in first] == [
        "analyzer.health.v1.discovery_health.gte_1",
        "analyzer.health.v1.adapter_failure.gte_1",
        "analyzer.health.v1.retry_backlog.gte_1",
        "analyzer.health.v1.duplicate_pressure.gte_1",
        "analyzer.health.v1.compaction_health.gte_1",
    ]
    assert [draft.title for draft in first] == [
        "Improve project discovery health",
        "Harden adapter ingestion failures",
        "Reduce retry backlog",
        "Reduce duplicate pressure",
        "Improve compaction health",
    ]
    assert len(first) == 5
    assert all(
        draft.origin == "analyzer"
        and draft.patch is None
        and draft.verification_argv == ()
        and draft.target_version is None
        and len(draft.description) <= 300
        for draft in first
    )
    combined = " ".join(draft.description for draft in first)
    for value in ("2", "1", "3", "4", "5", "16", "6", "7", "8"):
        assert value in combined


def test_analyzer_module_has_no_storage_sqlite_container_or_service_dependency() -> None:
    source = inspect.getsource(analyzer_module)
    tree = ast.parse(source)
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

    assert ImprovementAnalyzer().__dict__ == {}
    assert not any(
        dependency == "sqlite3"
        or dependency.startswith("project_memory_hub.storage")
        or dependency.startswith("project_memory_hub.container")
        or dependency.startswith("project_memory_hub.services")
        for dependency in imports
    )
