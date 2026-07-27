from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from statistics import median
from typing import Any, NoReturn, cast


_MAX_TOKEN_COUNT = 2**63 - 1
_DEFAULT_MAX_FILE_BYTES = 16 * 1024 * 1024
_DEFAULT_MAX_LINE_BYTES = 1024 * 1024
_TASK_BOOTSTRAP_RESAMPLES = 10_000
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)


class BenchmarkEvidenceError(ValueError):
    """A benchmark input failed a bounded, non-disclosing validation rule."""


@dataclass(frozen=True, slots=True)
class TokenUsage:
    input_tokens: int | None
    cached_input_tokens: int | None
    cache_write_input_tokens: int | None
    uncached_input_tokens: int | None
    output_tokens: int | None
    reasoning_output_tokens: int | None
    total_tokens: int | None


@dataclass(frozen=True, slots=True)
class CodexEventSummary:
    usage: TokenUsage
    tool_call_count: int
    failed_tool_call_count: int
    observed_model_id: str | None
    completed_turn_count: int
    terminal_error_count: int
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class BenchmarkRun:
    task_id: str
    condition: str
    replicate: int
    fixture_commit: str
    task_version: int
    workspace_sha256: str
    memory_snapshot_sha256: str
    validator_sha256: str
    timeout_seconds: int
    codex_cli_version: str
    requested_model_id: str
    reasoning_effort: str
    order: str
    success: bool
    invalid_reason: str | None
    total_tokens: int | None
    duration_ms: int
    contamination_hits: int
    manifest_sha256: str
    harness_commit: str
    harness_sha256: str
    model_sandbox_profile_sha256: str
    validation_workspace_sha256: str
    validator_evidence_sha256: str


def canonical_json_bytes(document: object) -> bytes:
    """Encode a public benchmark document deterministically."""
    try:
        encoded = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise BenchmarkEvidenceError("benchmark result rejected") from None
    return (encoded + "\n").encode("utf-8")


def read_codex_token_usage(
    path: Path | str,
    *,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
) -> TokenUsage:
    """Read only cumulative token metadata from one explicit Codex JSONL evidence file."""
    document = _read_evidence_document(
        path,
        max_file_bytes=max_file_bytes,
        max_line_bytes=max_line_bytes,
    )
    return _parse_token_document(document, max_line_bytes)


def read_codex_event_summary(
    path: Path | str,
    *,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    max_line_bytes: int = _DEFAULT_MAX_LINE_BYTES,
) -> CodexEventSummary:
    """Return allowlisted numeric/model metadata without reflecting event bodies."""
    document = _read_evidence_document(
        path,
        max_file_bytes=max_file_bytes,
        max_line_bytes=max_line_bytes,
    )
    records = _decode_json_lines(document, max_line_bytes)
    usage = _token_usage_from_records(records)
    tool_calls = 0
    failed_tool_calls = 0
    completed_turns = 0
    terminal_errors = 0
    observed_model: str | None = None
    tool_types = {"command_execution", "mcp_tool_call", "web_search"}
    for record in records:
        event_type = record.get("type")
        if event_type == "thread.started" and observed_model is None:
            candidate = record.get("model_id", record.get("model"))
            if isinstance(candidate, str) and _safe_model_value(candidate):
                observed_model = candidate
        if event_type == "turn.completed":
            completed_turns += 1
        if event_type in {"turn.failed", "error"}:
            terminal_errors += 1
        if event_type not in {"item.started", "item.completed"}:
            continue
        item = record.get("item")
        if not isinstance(item, Mapping) or item.get("type") not in tool_types:
            continue
        if event_type == "item.started":
            tool_calls += 1
            continue
        exit_code = item.get("exit_code")
        if type(exit_code) is int and exit_code != 0:
            failed_tool_calls += 1
    return CodexEventSummary(
        usage=usage,
        tool_call_count=tool_calls,
        failed_tool_call_count=failed_tool_calls,
        observed_model_id=observed_model,
        completed_turn_count=completed_turns,
        terminal_error_count=terminal_errors,
        evidence_sha256=hashlib.sha256(document).hexdigest(),
    )


def _read_evidence_document(
    path: Path | str,
    *,
    max_file_bytes: int,
    max_line_bytes: int,
) -> bytes:
    if (
        type(max_file_bytes) is not int
        or type(max_line_bytes) is not int
        or max_file_bytes <= 0
        or max_line_bytes <= 0
        or max_line_bytes > max_file_bytes
    ):
        _reject_token_evidence()
    selected = Path(path)
    descriptor = -1
    try:
        before = os.stat(selected, follow_symlinks=False)
        _validate_metadata(before, max_file_bytes)
        descriptor = os.open(selected, _READ_FLAGS)
        opened = os.fstat(descriptor)
        _validate_metadata(opened, max_file_bytes)
        if _identity(before) != _identity(opened):
            _reject_token_evidence()
        document = _read_bounded(descriptor, max_file_bytes)
        after = os.stat(selected, follow_symlinks=False)
        if _identity(opened) != _identity(after):
            _reject_token_evidence()
        return document
    except BenchmarkEvidenceError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError):
        _reject_token_evidence()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise AssertionError("unreachable")


def summarize_paired_runs(runs: Sequence[BenchmarkRun]) -> dict[str, object]:
    """Validate paired runs and return success-first aggregate measurements."""
    if not runs:
        _reject_benchmark_result()
    grouped: dict[tuple[str, int], dict[str, BenchmarkRun]] = {}
    success = {
        "baseline": {"count": 0, "total": 0},
        "pmh": {"count": 0, "total": 0},
    }
    invalid_or_failed = 0
    for run in runs:
        if run.condition not in success:
            _reject_benchmark_result()
        if (
            not run.task_id
            or type(run.replicate) is not int
            or run.replicate <= 0
            or not run.workspace_sha256
            or not run.memory_snapshot_sha256
            or not _sha256_value(run.manifest_sha256)
            or not _commit_value(run.harness_commit)
            or not _sha256_value(run.harness_sha256)
            or not _sha256_value(run.model_sandbox_profile_sha256)
            or not _sha256_value(run.validation_workspace_sha256)
            or not _sha256_value(run.validator_evidence_sha256)
            or not run.codex_cli_version
            or not run.reasoning_effort
            or run.order not in {"AB", "BA"}
            or type(run.success) is not bool
            or type(run.duration_ms) is not int
            or run.duration_ms < 0
            or type(run.contamination_hits) is not int
            or run.contamination_hits != 0
            or (
                run.total_tokens is not None
                and (
                    type(run.total_tokens) is not int
                    or run.total_tokens < 0
                    or run.total_tokens > _MAX_TOKEN_COUNT
                )
            )
        ):
            _reject_benchmark_result()
        key = (run.task_id, run.replicate)
        condition_runs = grouped.setdefault(key, {})
        if run.condition in condition_runs:
            _reject_benchmark_result()
        condition_runs[run.condition] = run
        success[run.condition]["total"] += 1
        if run.success:
            success[run.condition]["count"] += 1
        if not run.success or run.invalid_reason is not None:
            invalid_or_failed += 1

    token_changes: list[float] = []
    duration_changes: list[float] = []
    token_changes_by_task: dict[str, list[float]] = {}
    duration_changes_by_task: dict[str, list[float]] = {}
    for (task_id, _replicate), condition_runs in sorted(grouped.items()):
        if set(condition_runs) != {"baseline", "pmh"}:
            _reject_benchmark_result()
        baseline = condition_runs["baseline"]
        pmh = condition_runs["pmh"]
        if _pair_identity(baseline) != _pair_identity(pmh):
            _reject_benchmark_result()
        comparable = (
            baseline.success
            and pmh.success
            and baseline.invalid_reason is None
            and pmh.invalid_reason is None
        )
        if not comparable:
            continue
        if (
            baseline.total_tokens is not None
            and pmh.total_tokens is not None
            and baseline.total_tokens > 0
        ):
            token_change = (pmh.total_tokens - baseline.total_tokens) / baseline.total_tokens
            token_changes.append(token_change)
            token_changes_by_task.setdefault(task_id, []).append(token_change)
        if baseline.duration_ms > 0:
            duration_change = (pmh.duration_ms - baseline.duration_ms) / baseline.duration_ms
            duration_changes.append(duration_change)
            duration_changes_by_task.setdefault(task_id, []).append(duration_change)

    success_summary: dict[str, dict[str, int | float]] = {}
    for condition, counts in success.items():
        total = counts["total"]
        if total <= 0:
            _reject_benchmark_result()
        success_summary[condition] = {
            "count": counts["count"],
            "total": total,
            "rate": counts["count"] / total,
        }
    task_ids = sorted({task_id for task_id, _replicate in grouped})
    per_task: list[dict[str, object]] = []
    token_task_values: list[tuple[str, float]] = []
    duration_task_values: list[tuple[str, float]] = []
    for task_id in task_ids:
        task_token_changes = token_changes_by_task.get(task_id, [])
        task_duration_changes = duration_changes_by_task.get(task_id, [])
        task_token_median = None if not task_token_changes else median(task_token_changes)
        task_duration_median = None if not task_duration_changes else median(task_duration_changes)
        if task_token_median is not None:
            token_task_values.append((task_id, task_token_median))
        if task_duration_median is not None:
            duration_task_values.append((task_id, task_duration_median))
        per_task.append(
            {
                "task_id": task_id,
                "comparable_token_pair_count": len(task_token_changes),
                "median_paired_total_token_change": task_token_median,
                "comparable_duration_pair_count": len(task_duration_changes),
                "median_paired_duration_change": task_duration_median,
            }
        )
    return {
        "run_count": len(runs),
        "task_pair_count": len(grouped),
        "analysis_unit": "task",
        "success": success_summary,
        "invalid_or_failed_run_count": invalid_or_failed,
        "comparable_token_pair_count": len(token_changes),
        "median_paired_total_token_change": (None if not token_changes else median(token_changes)),
        "comparable_token_task_count": len(token_task_values),
        "median_task_total_token_change": (
            None
            if not token_task_values
            else median(value for _task_id, value in token_task_values)
        ),
        "task_bootstrap_95pct_total_token_change": _task_bootstrap_interval(
            token_task_values,
            label="total-token-change",
        ),
        "comparable_duration_pair_count": len(duration_changes),
        "median_paired_duration_change": (
            None if not duration_changes else median(duration_changes)
        ),
        "comparable_duration_task_count": len(duration_task_values),
        "median_task_duration_change": (
            None
            if not duration_task_values
            else median(value for _task_id, value in duration_task_values)
        ),
        "task_bootstrap_95pct_duration_change": _task_bootstrap_interval(
            duration_task_values,
            label="duration-change",
        ),
        "per_task": per_task,
    }


def _task_bootstrap_interval(
    values: Sequence[tuple[str, float]],
    *,
    label: str,
) -> dict[str, int | float | str] | None:
    if len(values) < 2:
        return None
    encoded = "\n".join(f"{task_id}\0{value.hex()}" for task_id, value in sorted(values)).encode(
        "utf-8"
    )
    seed = hashlib.sha256(label.encode("ascii") + b"\0" + encoded).digest()
    observations = tuple(value for _task_id, value in values)
    count = len(observations)
    medians: list[float] = []
    for sample in range(_TASK_BOOTSTRAP_RESAMPLES):
        selected: list[float] = []
        for position in range(count):
            digest = hashlib.sha256(
                seed + sample.to_bytes(4, "big") + position.to_bytes(2, "big")
            ).digest()
            selected.append(observations[int.from_bytes(digest[:8], "big") % count])
        medians.append(median(selected))
    medians.sort()
    lower_index = int((_TASK_BOOTSTRAP_RESAMPLES - 1) * 0.025)
    upper_index = ceil((_TASK_BOOTSTRAP_RESAMPLES - 1) * 0.975)
    return {
        "method": "task_median_percentile_bootstrap_v1",
        "resamples": _TASK_BOOTSTRAP_RESAMPLES,
        "lower": medians[lower_index],
        "upper": medians[upper_index],
    }


def _validate_metadata(metadata: os.stat_result, max_file_bytes: int) -> None:
    mode = metadata.st_mode
    if (
        not stat.S_ISREG(mode)
        or metadata.st_uid != os.getuid()
        or mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_size <= 0
        or metadata.st_size > max_file_bytes
    ):
        _reject_token_evidence()


def _identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _read_bounded(descriptor: int, max_file_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, max_file_bytes - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_file_bytes:
            _reject_token_evidence()
    if total <= 0:
        _reject_token_evidence()
    return b"".join(chunks)


def _parse_token_document(document: bytes, max_line_bytes: int) -> TokenUsage:
    return _token_usage_from_records(_decode_json_lines(document, max_line_bytes))


def _decode_json_lines(
    document: bytes,
    max_line_bytes: int,
) -> list[dict[str, object]]:
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        _reject_token_evidence()
    records: list[dict[str, object]] = []
    for line in text.splitlines():
        raw = line.encode("utf-8")
        if len(raw) > max_line_bytes:
            _reject_token_evidence()
        if not line.strip():
            continue
        try:
            parsed = json.loads(
                line,
                object_pairs_hook=_unique_object,
                parse_constant=_reject_json_constant,
            )
        except (json.JSONDecodeError, ValueError, TypeError):
            _reject_token_evidence()
        if not isinstance(parsed, dict):
            _reject_token_evidence()
        records.append(cast(dict[str, object], parsed))
    return records


def _token_usage_from_records(records: Sequence[Mapping[str, object]]) -> TokenUsage:
    snapshots: list[tuple[bool, TokenUsage]] = []
    for record in records:
        extracted = _extract_usage(record)
        if extracted is not None:
            snapshots.append(extracted)
    if not snapshots:
        _reject_token_evidence()

    previous_persistent: TokenUsage | None = None
    for persistent, usage in snapshots:
        if persistent and previous_persistent is not None:
            _require_monotonic(previous_persistent, usage)
        if persistent:
            previous_persistent = usage
    return snapshots[-1][1]


def _safe_model_value(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and len(value) <= 513
        and all(ord(character) >= 32 and ord(character) != 127 for character in value)
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError("duplicate key")
        document[key] = value
    return document


def _reject_json_constant(_value: str) -> NoReturn:
    raise ValueError("non-finite number")


def _extract_usage(document: Mapping[str, object]) -> tuple[bool, TokenUsage] | None:
    event_type = document.get("type")
    if event_type == "turn.completed":
        usage = document.get("usage")
        if not isinstance(usage, Mapping):
            _reject_token_evidence()
        return (False, _token_usage(cast(Mapping[str, object], usage)))
    if event_type != "event_msg":
        return None
    payload = document.get("payload")
    if not isinstance(payload, Mapping) or payload.get("type") != "token_count":
        return None
    info = payload.get("info")
    if not isinstance(info, Mapping):
        _reject_token_evidence()
    usage = cast(Mapping[str, object], info).get("total_token_usage")
    if not isinstance(usage, Mapping):
        _reject_token_evidence()
    return (True, _token_usage(cast(Mapping[str, object], usage)))


def _token_usage(document: Mapping[str, object]) -> TokenUsage:
    values = {field: _optional_count(document, field) for field in _TOKEN_FIELDS}
    input_tokens = values["input_tokens"]
    cached = values["cached_input_tokens"]
    cache_write = values["cache_write_input_tokens"]
    output_tokens = values["output_tokens"]
    reasoning = values["reasoning_output_tokens"]
    supplied_total = values["total_tokens"]

    total = supplied_total
    if input_tokens is not None and output_tokens is not None:
        computed_total = input_tokens + output_tokens
        if computed_total > _MAX_TOKEN_COUNT:
            _reject_token_evidence()
        if supplied_total is not None and supplied_total != computed_total:
            _reject_token_evidence()
        total = computed_total
    if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
        _reject_token_evidence()

    uncached: int | None = None
    if input_tokens is not None and cached is not None and cache_write is not None:
        uncached = input_tokens - cached - cache_write
        if uncached < 0:
            _reject_token_evidence()
    return TokenUsage(
        input_tokens=input_tokens,
        cached_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        uncached_input_tokens=uncached,
        output_tokens=output_tokens,
        reasoning_output_tokens=reasoning,
        total_tokens=total,
    )


def _optional_count(document: Mapping[str, object], field: str) -> int | None:
    value = document.get(field)
    if value is None:
        return None
    if type(value) is not int or value < 0 or value > _MAX_TOKEN_COUNT:
        _reject_token_evidence()
    return value


def _require_monotonic(previous: TokenUsage, current: TokenUsage) -> None:
    for field in (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    ):
        old = getattr(previous, field)
        new = getattr(current, field)
        if old is not None and new is not None and new < old:
            _reject_token_evidence()


def _pair_identity(run: BenchmarkRun) -> tuple[object, ...]:
    return (
        run.manifest_sha256,
        run.harness_commit,
        run.harness_sha256,
        run.model_sandbox_profile_sha256,
        run.fixture_commit,
        run.task_version,
        run.workspace_sha256,
        run.memory_snapshot_sha256,
        run.validator_sha256,
        run.timeout_seconds,
        run.codex_cli_version,
        run.requested_model_id,
        run.reasoning_effort,
        run.order,
    )


def _sha256_value(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _commit_value(value: str) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject_token_evidence() -> NoReturn:
    raise BenchmarkEvidenceError("token evidence rejected")


def _reject_benchmark_result() -> NoReturn:
    raise BenchmarkEvidenceError("benchmark result rejected")
