from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path

import pytest

from project_memory_hub.web.presentation import (
    NextSafeStepKind,
    group_source_records,
    select_next_safe_step,
)


_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATES = _REPOSITORY_ROOT / "src/project_memory_hub/web/templates"


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    label: str
    available: bool
    probe: object | None


def test_source_records_are_grouped_by_capability_without_replacement() -> None:
    codex = _SourceRecord(label="Codex", available=True, probe=None)
    chatgpt = _SourceRecord(label="ChatGPT", available=True, probe=None)
    trae = _SourceRecord(label="Trae", available=False, probe=object())
    claude = _SourceRecord(label="Claude Code", available=False, probe=object())

    groups = group_source_records((codex, trae, chatgpt, claude))

    assert groups.ingestion == (codex, chatgpt)
    assert groups.probes == (trae, claude)
    assert groups.ingestion[0] is codex
    assert groups.probes[0] is trae
    with pytest.raises(FrozenInstanceError):
        groups.ingestion = ()  # type: ignore[misc]


@pytest.mark.parametrize(
    "record",
    (
        _SourceRecord(label="ambiguous ingestion", available=True, probe=object()),
        _SourceRecord(label="ambiguous probe", available=False, probe=None),
    ),
)
def test_source_grouping_rejects_contradictory_capability_state(
    record: _SourceRecord,
) -> None:
    with pytest.raises(ValueError, match="source capability state"):
        group_source_records((record,))


@pytest.mark.parametrize(
    ("project_count", "fact_count", "permission_error_count", "startup_status", "kind", "command"),
    (
        (
            0,
            0,
            0,
            "not_due",
            NextSafeStepKind.DISCOVER,
            "memory-hub discover --dry-run --format json",
        ),
        (
            1,
            0,
            0,
            "complete",
            NextSafeStepKind.SCAN,
            'memory-hub scan --cwd "$PWD" --dry-run --format json',
        ),
        (
            1,
            1,
            1,
            "complete",
            NextSafeStepKind.DOCTOR,
            "memory-hub doctor --format json",
        ),
        (
            1,
            1,
            0,
            "degraded",
            NextSafeStepKind.DOCTOR,
            "memory-hub doctor --format json",
        ),
        (
            1,
            1,
            0,
            "complete",
            NextSafeStepKind.RECONCILE,
            "memory-hub reconcile --if-due --format json",
        ),
    ),
)
def test_next_safe_step_uses_only_existing_overview_state(
    project_count: int,
    fact_count: int,
    permission_error_count: int,
    startup_status: str,
    kind: NextSafeStepKind,
    command: str,
) -> None:
    step = select_next_safe_step(
        project_count=project_count,
        fact_count=fact_count,
        permission_error_count=permission_error_count,
        startup_status=startup_status,
    )

    assert step.kind is kind
    assert step.command == command
    assert step.reason
    assert step.success_condition


def test_next_safe_step_is_frozen() -> None:
    step = select_next_safe_step(
        project_count=1,
        fact_count=1,
        permission_error_count=0,
        startup_status="complete",
    )

    with pytest.raises(FrozenInstanceError):
        step.command = "memory-hub doctor --format json"  # type: ignore[misc]


def test_scan_step_requires_the_target_registered_project_directory() -> None:
    step = select_next_safe_step(
        project_count=1,
        fact_count=0,
        permission_error_count=0,
        startup_status="complete",
    )

    assert "registered project directory" in step.reason


def test_reconcile_success_condition_matches_real_cli_statuses() -> None:
    step = select_next_safe_step(
        project_count=1,
        fact_count=1,
        permission_error_count=0,
        startup_status="complete",
    )

    assert "success" in step.success_condition
    assert "skipped" in step.success_condition
    assert "complete" not in step.success_condition
    assert "not_due" not in step.success_condition


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("project_count", -1),
        ("fact_count", -1),
        ("permission_error_count", -1),
        ("project_count", True),
    ),
)
def test_next_safe_step_rejects_invalid_counts(field: str, value: object) -> None:
    counts: dict[str, object] = {
        "project_count": 1,
        "fact_count": 1,
        "permission_error_count": 0,
    }
    counts[field] = value

    with pytest.raises(ValueError, match=field):
        select_next_safe_step(
            project_count=counts["project_count"],  # type: ignore[arg-type]
            fact_count=counts["fact_count"],  # type: ignore[arg-type]
            permission_error_count=counts["permission_error_count"],  # type: ignore[arg-type]
            startup_status="complete",
        )


def test_presentation_module_has_no_runtime_or_data_layer_imports() -> None:
    module_path = _REPOSITORY_ROOT / "src/project_memory_hub/web/presentation.py"
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    forbidden_fragments = ("storage", "adapter", "reconcile", "subprocess")
    assert not any(fragment in name for name in imported for fragment in forbidden_fragments)


def test_empty_state_macro_has_reason_one_step_and_success_contract() -> None:
    template = (_TEMPLATES / "_empty_state.html").read_text(encoding="utf-8")

    assert 'class="empty empty-state"' in template
    assert template.count("data-empty-reason") == 1
    assert template.count("data-empty-next-step") == 1
    assert template.count("data-empty-success") == 1
    assert template.count("<code>") == 1


@pytest.mark.parametrize("template_name", ("memories.html", "proposals.html"))
def test_task_three_templates_have_no_unstructured_empty_state(template_name: str) -> None:
    template = (_TEMPLATES / template_name).read_text(encoding="utf-8")

    assert 'class="empty"' not in template
    assert "empty_state(" in template


@pytest.mark.parametrize("template_name", ("sources.html", "imports.html"))
def test_active_collection_pages_do_not_add_empty_branches(template_name: str) -> None:
    template = (_TEMPLATES / template_name).read_text(encoding="utf-8")

    assert 'class="empty"' not in template
    assert "empty_state(" not in template


def test_memories_template_guides_exact_runtime_namespace_resolution() -> None:
    template = (_TEMPLATES / "memories.html").read_text(encoding="utf-8")

    assert 'memory-hub codex-context --cwd "$PWD" --format json' in template
    assert "stored_models" not in template
    assert "model_options" not in template
