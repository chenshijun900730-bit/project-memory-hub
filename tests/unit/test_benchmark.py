from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path

import pytest

from project_memory_hub.benchmark import (
    BenchmarkEvidenceError,
    BenchmarkRun,
    CodexEventSummary,
    TokenUsage,
    canonical_json_bytes,
    read_codex_event_summary,
    read_codex_token_usage,
    summarize_paired_runs,
)


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n" for record in records
        ),
        encoding="utf-8",
    )


def _exec_event(
    *,
    input_tokens: object = 120,
    cached_input_tokens: object = 20,
    cache_write_input_tokens: object = 5,
    output_tokens: object = 30,
    reasoning_output_tokens: object = 10,
) -> dict[str, object]:
    return {
        "type": "turn.completed",
        "usage": {
            "cache_write_input_tokens": cache_write_input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
        },
    }


def _persistent_event(
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> dict[str, object]:
    outer_kind = "event_" + "msg"
    inner_kind = "token_" + "count"
    return {
        "payload": {
            "info": {
                "total_token_usage": {
                    "cache_write_input_tokens": 0,
                    "cached_input_tokens": cached_input_tokens,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "reasoning_output_tokens": 3,
                    "total_tokens": input_tokens + output_tokens,
                }
            },
            "type": inner_kind,
        },
        "type": outer_kind,
    }


def test_reads_fresh_exec_usage_and_does_not_double_count_reasoning(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(
        evidence,
        [
            {"type": "thread.started", "opaque": "private"},
            _exec_event(),
        ],
    )

    usage = read_codex_token_usage(evidence)

    assert usage == TokenUsage(
        input_tokens=120,
        cached_input_tokens=20,
        cache_write_input_tokens=5,
        uncached_input_tokens=95,
        output_tokens=30,
        reasoning_output_tokens=10,
        total_tokens=150,
    )


def test_event_summary_counts_only_allowlisted_metadata(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(
        evidence,
        [
            {"type": "thread.started", "thread_id": "private", "model_id": "model-observed"},
            {
                "type": "item.started",
                "item": {"id": "private-1", "type": "command_execution", "command": "private"},
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "private-1",
                    "type": "command_execution",
                    "exit_code": 1,
                    "aggregated_output": "private output",
                },
            },
            {
                "type": "item.started",
                "item": {"id": "private-2", "type": "mcp_tool_call"},
            },
            _exec_event(),
        ],
    )

    summary = read_codex_event_summary(evidence)

    assert summary == CodexEventSummary(
        usage=TokenUsage(
            input_tokens=120,
            cached_input_tokens=20,
            cache_write_input_tokens=5,
            uncached_input_tokens=95,
            output_tokens=30,
            reasoning_output_tokens=10,
            total_tokens=150,
        ),
        tool_call_count=2,
        failed_tool_call_count=1,
        observed_model_id="model-observed",
        completed_turn_count=1,
        terminal_error_count=0,
        evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
    )
    assert "private" not in repr(summary)


def test_event_summary_keeps_unreported_model_unknown(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(evidence, [{"type": "thread.started"}, _exec_event()])

    summary = read_codex_event_summary(evidence)

    assert summary.observed_model_id is None
    assert summary.completed_turn_count == 1
    assert summary.terminal_error_count == 0
    assert summary.evidence_sha256 == hashlib.sha256(evidence.read_bytes()).hexdigest()


def test_event_summary_counts_failed_terminal_events(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(
        evidence,
        [
            {"type": "thread.started"},
            {"type": "turn.failed", "error": {"message": "private"}},
            _exec_event(),
        ],
    )

    summary = read_codex_event_summary(evidence)

    assert summary.completed_turn_count == 1
    assert summary.terminal_error_count == 1


def test_reads_last_persisted_cumulative_snapshot_instead_of_summing(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(
        evidence,
        [
            _persistent_event(input_tokens=100, cached_input_tokens=20, output_tokens=10),
            _persistent_event(input_tokens=180, cached_input_tokens=60, output_tokens=25),
        ],
    )

    usage = read_codex_token_usage(evidence)

    assert usage.input_tokens == 180
    assert usage.cached_input_tokens == 60
    assert usage.uncached_input_tokens == 120
    assert usage.output_tokens == 25
    assert usage.total_tokens == 205


def test_missing_optional_usage_fields_remain_unknown(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(
        evidence,
        [{"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 4}}],
    )

    usage = read_codex_token_usage(evidence)

    assert usage.cached_input_tokens is None
    assert usage.cache_write_input_tokens is None
    assert usage.uncached_input_tokens is None
    assert usage.reasoning_output_tokens is None
    assert usage.total_tokens == 16


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("input_tokens", True),
        ("input_tokens", -1),
        ("input_tokens", 1.5),
        ("input_tokens", 2**63),
        ("reasoning_output_tokens", -1),
    ),
)
def test_rejects_invalid_usage_numbers(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    evidence = tmp_path / "events.jsonl"
    event = _exec_event()
    usage = event["usage"]
    assert isinstance(usage, dict)
    usage[field] = value
    _write_jsonl(evidence, [event])

    with pytest.raises(BenchmarkEvidenceError, match="token evidence rejected"):
        read_codex_token_usage(evidence)


def test_rejects_cumulative_count_regression(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(
        evidence,
        [
            _persistent_event(input_tokens=180, cached_input_tokens=60, output_tokens=25),
            _persistent_event(input_tokens=170, cached_input_tokens=50, output_tokens=20),
        ],
    )

    with pytest.raises(BenchmarkEvidenceError, match="token evidence rejected"):
        read_codex_token_usage(evidence)


def test_rejects_duplicate_json_keys_without_reflecting_document(tmp_path: Path) -> None:
    evidence = tmp_path / "events.jsonl"
    evidence.write_text(
        '{"type":"turn.completed","usage":{"input_tokens":1,"input_tokens":2}}\n',
        encoding="utf-8",
    )

    with pytest.raises(BenchmarkEvidenceError, match="token evidence rejected") as captured:
        read_codex_token_usage(evidence)

    assert "input_tokens" not in str(captured.value)


def test_rejects_symlink_non_utf8_and_oversized_lines(tmp_path: Path) -> None:
    target = tmp_path / "target.jsonl"
    _write_jsonl(target, [_exec_event()])
    symlink = tmp_path / "link.jsonl"
    symlink.symlink_to(target)

    with pytest.raises(BenchmarkEvidenceError, match="token evidence rejected"):
        read_codex_token_usage(symlink)

    invalid = tmp_path / "invalid.jsonl"
    invalid.write_bytes(b"\xff\n")
    with pytest.raises(BenchmarkEvidenceError, match="token evidence rejected"):
        read_codex_token_usage(invalid)

    oversized = tmp_path / "oversized.jsonl"
    oversized.write_bytes(b"{" + b" " * 128 + b"}\n")
    with pytest.raises(BenchmarkEvidenceError, match="token evidence rejected"):
        read_codex_token_usage(oversized, max_line_bytes=64)


def _run(
    *,
    task_id: str,
    condition: str,
    replicate: int = 1,
    success: bool = True,
    total_tokens: int | None = 100,
    duration_ms: int = 1000,
    fixture_commit: str = "a" * 40,
    requested_model_id: str = "gpt-5.6-sol",
    codex_cli_version: str = "codex-cli 0.146.0-alpha.3.1",
    reasoning_effort: str = "low",
    workspace_sha256: str = "c" * 64,
    memory_snapshot_sha256: str = "d" * 64,
    order: str = "AB",
    contamination_hits: int = 0,
    manifest_sha256: str = "c" * 64,
    harness_commit: str = "d" * 40,
    harness_sha256: str = "e" * 64,
    model_sandbox_profile_sha256: str = "f" * 64,
    validation_workspace_sha256: str = "1" * 64,
    validator_evidence_sha256: str = "2" * 64,
) -> BenchmarkRun:
    return BenchmarkRun(
        task_id=task_id,
        condition=condition,
        replicate=replicate,
        fixture_commit=fixture_commit,
        task_version=1,
        validator_sha256="b" * 64,
        timeout_seconds=600,
        requested_model_id=requested_model_id,
        codex_cli_version=codex_cli_version,
        reasoning_effort=reasoning_effort,
        workspace_sha256=workspace_sha256,
        memory_snapshot_sha256=memory_snapshot_sha256,
        order=order,
        success=success,
        invalid_reason=None,
        total_tokens=total_tokens,
        duration_ms=duration_ms,
        contamination_hits=contamination_hits,
        manifest_sha256=manifest_sha256,
        harness_commit=harness_commit,
        harness_sha256=harness_sha256,
        model_sandbox_profile_sha256=model_sandbox_profile_sha256,
        validation_workspace_sha256=validation_workspace_sha256,
        validator_evidence_sha256=validator_evidence_sha256,
    )


def test_paired_summary_reports_success_before_efficiency_and_keeps_failures() -> None:
    runs = [
        _run(task_id="T01", condition="baseline", total_tokens=200, duration_ms=2000),
        _run(task_id="T01", condition="pmh", total_tokens=150, duration_ms=1500),
        _run(task_id="T02", condition="baseline", success=False, total_tokens=40),
        _run(task_id="T02", condition="pmh", total_tokens=120),
    ]

    summary = summarize_paired_runs(runs)

    assert summary["run_count"] == 4
    assert summary["task_pair_count"] == 2
    assert summary["success"] == {
        "baseline": {"count": 1, "total": 2, "rate": 0.5},
        "pmh": {"count": 2, "total": 2, "rate": 1.0},
    }
    assert summary["comparable_token_pair_count"] == 1
    assert summary["median_paired_total_token_change"] == -0.25
    assert summary["median_paired_duration_change"] == -0.25
    assert summary["invalid_or_failed_run_count"] == 1


@pytest.mark.parametrize(
    "runs",
    (
        [
            _run(task_id="T01", condition="baseline"),
            _run(task_id="T01", condition="baseline"),
        ],
        [
            _run(task_id="T01", condition="baseline", fixture_commit="a" * 40),
            _run(task_id="T01", condition="pmh", fixture_commit="c" * 40),
        ],
        [
            _run(task_id="T01", condition="baseline", requested_model_id="model-a"),
            _run(task_id="T01", condition="pmh", requested_model_id="model-b"),
        ],
        [
            _run(task_id="T01", condition="baseline", codex_cli_version="codex-a"),
            _run(task_id="T01", condition="pmh", codex_cli_version="codex-b"),
        ],
        [
            _run(task_id="T01", condition="baseline", reasoning_effort="low"),
            _run(task_id="T01", condition="pmh", reasoning_effort="high"),
        ],
        [
            _run(task_id="T01", condition="baseline", workspace_sha256="a" * 64),
            _run(task_id="T01", condition="pmh", workspace_sha256="b" * 64),
        ],
        [
            _run(
                task_id="T01",
                condition="baseline",
                memory_snapshot_sha256="a" * 64,
            ),
            _run(
                task_id="T01",
                condition="pmh",
                memory_snapshot_sha256="b" * 64,
            ),
        ],
        [
            _run(task_id="T01", condition="baseline", order="AB"),
            _run(task_id="T01", condition="pmh", order="BA"),
        ],
        [
            _run(task_id="T01", condition="baseline", manifest_sha256="a" * 64),
            _run(task_id="T01", condition="pmh", manifest_sha256="b" * 64),
        ],
        [
            _run(task_id="T01", condition="baseline", harness_commit="a" * 40),
            _run(task_id="T01", condition="pmh", harness_commit="b" * 40),
        ],
        [
            _run(task_id="T01", condition="baseline", harness_sha256="a" * 64),
            _run(task_id="T01", condition="pmh", harness_sha256="b" * 64),
        ],
        [
            _run(
                task_id="T01",
                condition="baseline",
                model_sandbox_profile_sha256="a" * 64,
            ),
            _run(
                task_id="T01",
                condition="pmh",
                model_sandbox_profile_sha256="b" * 64,
            ),
        ],
        [
            _run(task_id="T01", condition="baseline"),
            _run(task_id="T01", condition="pmh", contamination_hits=1),
        ],
    ),
)
def test_paired_summary_rejects_incomplete_mismatched_or_contaminated_pairs(
    runs: list[BenchmarkRun],
) -> None:
    with pytest.raises(BenchmarkEvidenceError, match="benchmark result rejected"):
        summarize_paired_runs(runs)


def test_canonical_json_is_deterministic_and_has_one_trailing_newline() -> None:
    left = canonical_json_bytes({"z": [2, 1], "a": {"x": True}})
    right = canonical_json_bytes({"a": {"x": True}, "z": [2, 1]})

    assert left == right
    assert left.endswith(b"\n")
    assert not left.endswith(b"\n\n")


def test_rejects_path_replacement_during_read(tmp_path: Path, monkeypatch) -> None:
    evidence = tmp_path / "events.jsonl"
    _write_jsonl(evidence, [_exec_event()])
    original_stat = os.stat
    calls = 0

    def replaced(path, *args, **kwargs):
        nonlocal calls
        metadata = original_stat(path, *args, **kwargs)
        calls += 1
        if calls < 2:
            return metadata
        values = list(metadata)
        values[1] = metadata.st_ino + 1
        return os.stat_result(values)

    monkeypatch.setattr(os, "stat", replaced)

    with pytest.raises(BenchmarkEvidenceError, match="token evidence rejected"):
        read_codex_token_usage(evidence)
