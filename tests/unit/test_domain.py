from datetime import datetime, timezone
from pathlib import Path

import pytest
from project_memory_hub.domain import (
    CaptureResult,
    CapturePayload,
    Namespace,
    NamespaceVerification,
    NormalizedTaskRecord,
    RecallRequest,
    SourceAgent,
)
from pydantic import ValidationError


def namespace(model_id: str = "gpt-5") -> Namespace:
    return Namespace(source_agent=SourceAgent.CODEX, model_id=model_id)


def test_namespace_rejects_blank_model_id() -> None:
    with pytest.raises(ValidationError):
        namespace("   ")


def test_namespace_rejects_model_id_that_requires_normalization() -> None:
    with pytest.raises(ValidationError):
        namespace("  gpt-5  ")


def test_namespace_rejects_non_utf8_model_id_without_leaking_encoder_error() -> None:
    with pytest.raises(ValidationError):
        namespace("model-\ud800")


def test_recall_request_rejects_blank_task(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        RecallRequest(cwd=tmp_path, task="  ", namespace=namespace())


def test_recall_request_strips_task(tmp_path: Path) -> None:
    request = RecallRequest(cwd=tmp_path, task="  summarize this project  ", namespace=namespace())
    assert request.task == "summarize this project"


@pytest.mark.parametrize("max_tokens", [127, 4097])
def test_recall_request_rejects_out_of_range_max_tokens(tmp_path: Path, max_tokens: int) -> None:
    with pytest.raises(ValidationError):
        RecallRequest(
            cwd=tmp_path,
            task="summarize",
            namespace=namespace(),
            max_tokens=max_tokens,
        )


@pytest.mark.parametrize("max_tokens", [128, 4096])
def test_recall_request_accepts_max_token_boundaries(tmp_path: Path, max_tokens: int) -> None:
    request = RecallRequest(
        cwd=tmp_path,
        task="summarize",
        namespace=namespace(),
        max_tokens=max_tokens,
    )
    assert request.max_tokens == max_tokens


def test_capture_payload_list_defaults_are_independent(tmp_path: Path) -> None:
    first = CapturePayload(
        cwd=tmp_path,
        namespace=namespace(),
        source_record_id="record-1",
        objective="Create a private memory hub",
        outcome="Scaffolded the domain",
    )
    second = CapturePayload(
        cwd=tmp_path,
        namespace=namespace(),
        source_record_id="record-2",
        objective="Verify list defaults",
        outcome="Defaults are independent",
    )

    list_fields = (
        "decisions",
        "failed_attempts",
        "verified_commands",
        "changed_paths",
        "preferences",
        "risks",
        "open_issues",
        "resolved_open_issues",
        "reusable_lessons",
    )
    assert all(getattr(first, field) == [] for field in list_fields)
    assert all(getattr(second, field) == [] for field in list_fields)

    first.decisions.append("Keep data local")
    assert second.decisions == []


def test_resolution_fields_are_backward_compatible(tmp_path: Path) -> None:
    namespace_value = namespace("gpt-5.6-sol")
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=namespace_value,
        source_record_id="legacy-record",
        objective="legacy objective",
        outcome="legacy outcome",
    )
    assert payload.resolved_open_issues == []

    verification = NamespaceVerification(
        namespace=namespace_value,
        source_record_id="legacy-record",
        verified_by="codex_adapter",
        verified_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    record = NormalizedTaskRecord(
        cwd=tmp_path,
        namespace=namespace_value,
        source_record_id="legacy-record",
        objective="legacy objective",
        outcome="legacy outcome",
        verification=verification,
    )
    assert record.resolved_open_issues == ()

    result = CaptureResult(status="resolved", resolved_count=2)
    assert result.model_dump(mode="json") == {
        "inserted_ids": [],
        "duplicate": False,
        "status": "resolved",
        "resolved_count": 2,
        "already_resolved_count": 0,
        "unmatched_resolution_count": 0,
    }
    assert CaptureResult(status="partial", unmatched_resolution_count=1).status == "partial"
