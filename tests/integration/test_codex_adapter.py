import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import project_memory_hub.adapters.codex as codex_module
import project_memory_hub.storage.path_identity as path_identity_module
import project_memory_hub.storage.projects as projects_module
from project_memory_hub.adapters.base import (
    IngestionError,
    IngestionService,
    ReconcileRequiredError,
)
from project_memory_hub.adapters.codex import (
    CAPTURE_END,
    CAPTURE_START,
    CodexAdapter,
    CodexContextUnavailable,
    DiscoveryLimitExceeded,
)
from project_memory_hub.adapters.registry import AdapterRegistry
from project_memory_hub.domain import (
    AdapterBatch,
    AdapterCheckpoint,
    CapturePayload,
    CaptureResult,
    Namespace,
    NamespaceVerification,
    NormalizedTaskRecord,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.storage.checkpoints import (
    CheckpointConflictError,
    CheckpointRepository,
)
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository


FIXTURES = Path(__file__).parents[1] / "fixtures" / "codex"


def _line(record: dict) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def _session(session_id: str = "session-1") -> dict:
    return {
        "timestamp": "2026-07-12T00:00:00Z",
        "type": "session_meta",
        "payload": {
            "id": session_id,
            "session_id": session_id,
            "cwd": "/ignored/session/cwd",
            "model_provider": "PROVIDER_MUST_NOT_BE_MODEL",
        },
    }


def _session_with_ids(thread_id: str, session_id: str) -> dict:
    value = _session(session_id)
    value["payload"]["id"] = thread_id
    return value


def _context(turn: str, cwd: str, model: str, summary: str = "task") -> dict:
    return {
        "timestamp": "2026-07-12T00:00:01Z",
        "type": "turn_context",
        "payload": {
            "turn_id": turn,
            "cwd": cwd,
            "model": model,
            "summary": summary,
        },
    }


def _complete(turn: str, message: str) -> dict:
    if CAPTURE_START not in message:
        labels = message.splitlines()
        if not any(line.startswith("Objective:") for line in labels):
            labels.insert(0, "Objective: task")
        message = _managed_capture(*labels)
    return {
        "timestamp": "2026-07-12T00:00:02Z",
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn,
            "last_agent_message": message,
        },
    }


def _managed_capture(*lines: str) -> str:
    return "\n".join((CAPTURE_START, *lines, CAPTURE_END))


def _database(tmp_path: Path) -> Database:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    return database


def _ingestion_checkpoint(
    offset: int,
    *,
    scope: str = "session.jsonl",
) -> AdapterCheckpoint:
    return AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope=scope,
        cursor={"relative_path": scope, "offset": offset},
        parser_version="codex-v3",
    )


def _deferred_checkpoint(
    offset: int,
    *,
    scope: str = "session.jsonl",
    device: int = 101,
    inode: int = 202,
    prefix_sha256: str = "a" * 64,
) -> AdapterCheckpoint:
    return AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope=scope,
        cursor={
            "device": device,
            "inode": inode,
            "observed_size": offset,
            "offset": offset,
            "prefix_length": offset,
            "prefix_sha256": prefix_sha256,
            "parser_policy_sha256": "c" * 64,
            "relative_path": scope,
        },
        parser_version="codex-v3",
    )


def _ingestion_record(
    project: Path,
    source_record_id: str,
    *,
    model_id: str = "model-one",
    objective: str = "exact task",
    outcome: str = "exact outcome",
    changed_paths: tuple[str, ...] = (),
    open_issues: tuple[str, ...] = (),
    resolved_open_issues: tuple[str, ...] = (),
    verified_at: datetime | None = None,
) -> NormalizedTaskRecord:
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id=model_id)
    return NormalizedTaskRecord(
        cwd=project,
        namespace=namespace,
        source_record_id=source_record_id,
        objective=objective,
        outcome=outcome,
        changed_paths=changed_paths,
        open_issues=open_issues,
        resolved_open_issues=resolved_open_issues,
        verification=NamespaceVerification(
            namespace=namespace,
            source_record_id=source_record_id,
            verified_by="codex_adapter",
            verified_at=verified_at or datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
        ),
    )


def _capture_payload_from_record(record: NormalizedTaskRecord) -> CapturePayload:
    return CapturePayload(
        cwd=record.cwd,
        namespace=record.namespace,
        source_record_id=record.source_record_id,
        objective=record.objective,
        outcome=record.outcome,
        decisions=list(record.decisions),
        failed_attempts=list(record.failed_attempts),
        verified_commands=list(record.verified_commands),
        changed_paths=list(record.changed_paths),
        preferences=list(record.preferences),
        risks=list(record.risks),
        open_issues=list(record.open_issues),
        resolved_open_issues=list(record.resolved_open_issues),
        reusable_lessons=list(record.reusable_lessons),
    )


def _ingestion_adapter(
    records: tuple[NormalizedTaskRecord, ...],
    checkpoint: AdapterCheckpoint,
    *,
    warnings: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        source_agent=SourceAgent.CODEX,
        read_incremental=lambda _scope, _checkpoint: AdapterBatch(
            records=records,
            next_checkpoint=checkpoint,
            warnings=warnings,
        ),
    )


def _ingestion_counts(database: Database) -> dict[str, int]:
    with database.connect(readonly=True) as connection:
        return {
            table: int(connection.execute(f"select count(*) from {table}").fetchone()[0])
            for table in (
                "source_refs",
                "behavior_memories",
                "memory_issue_resolutions",
                "pending_captures",
                "pending_capture_history",
                "import_receipts",
                "checkpoints",
                "codex_deferred_records",
            )
        }


def test_completed_turn_produces_verified_normalized_record():
    adapter = CodexAdapter(FIXTURES, Redactor())

    batch = adapter.read_incremental("managed-completed-turn.jsonl", None)

    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.cwd == Path("/fixture/repo")
    assert record.namespace.source_agent is SourceAgent.CODEX
    assert record.namespace.model_id == "gpt-5.6-sol"
    assert record.source_record_id == "session-1:turn-1"
    assert record.outcome == "fixed cache"
    assert record.verified_commands == ("uv run pytest tests/test_cache.py -q",)
    assert record.verification.namespace == record.namespace
    assert record.verification.source_record_id == record.source_record_id
    assert record.verification.verified_by == "codex_adapter"


def test_explicit_labels_cover_every_structured_capture_field(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one", "context fallback"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: exact objective",
                    "Outcome: exact outcome",
                    "Decision: exact decision",
                    "Failed: exact failed attempt",
                    "Verified: uv run pytest -q",
                    "Changed: src/cache.py",
                    "Preference: exact preference",
                    "Risk: exact risk",
                    "Open issue: exact open issue",
                    "Lesson: exact reusable lesson",
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    record = batch.records[0]
    assert record.objective == "exact objective"
    assert record.outcome == "exact outcome"
    assert record.decisions == ("exact decision",)
    assert record.failed_attempts == ("exact failed attempt",)
    assert record.verified_commands == ("uv run pytest -q",)
    assert record.changed_paths == ("src/cache.py",)
    assert record.preferences == ("exact preference",)
    assert record.risks == ("exact risk",)
    assert record.open_issues == ("exact open issue",)
    assert record.reusable_lessons == ("exact reusable lesson",)


def test_resolved_issue_labels_preserve_order_and_parser_version(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: close known issues",
                    "Outcome: both issues were verified",
                    "Resolved issue: first exact old issue",
                    "Resolved issue: second exact old issue",
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert len(batch.records) == 1
    assert batch.records[0].resolved_open_issues == (
        "first exact old issue",
        "second exact old issue",
    )
    assert batch.next_checkpoint.parser_version == "codex-v3"
    assert batch.next_checkpoint.cursor["nonsemantic_record_policy"] == 1
    assert len(batch.next_checkpoint.cursor["parser_policy_sha256"]) == 64


def test_resolved_issue_labels_deduplicate_after_normalization_first_seen(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: close duplicate declarations",
                    "Outcome: declarations were normalized",
                    "Resolved issue:   first   exact old issue  ",
                    "Resolved issue: second exact old issue",
                    "Resolved issue: first exact old issue",
                    "Resolved issue:  second   exact old issue ",
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert len(batch.records) == 1
    assert batch.records[0].resolved_open_issues == (
        "first exact old issue",
        "second exact old issue",
    )


def test_open_and_resolved_issue_intersection_rejects_the_whole_capture(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: contradictory issue state",
                    "Outcome: capture must fail closed",
                    "Open issue:   exact   old issue ",
                    "Resolved issue: exact old issue",
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings


def test_resolved_issue_label_after_final_marker_remains_invalid(tmp_path):
    source = tmp_path / "session.jsonl"
    message = (
        _managed_capture(
            "Objective: keep labels bounded",
            "Outcome: marker boundary remains authoritative",
        )
        + "\nResolved issue: exact old issue"
    )
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(_complete("turn-1", message))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings


def test_ordinary_fixed_issue_prose_does_not_create_a_resolution(tmp_path):
    source = tmp_path / "session.jsonl"
    message = "\n".join(
        (
            "The exact old issue was fixed and verified.",
            _managed_capture(
                "Objective: report verified work",
                "Outcome: exact old issue is no longer failing",
            ),
        )
    )
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(_complete("turn-1", message))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert len(batch.records) == 1
    assert batch.records[0].resolved_open_issues == ()


def test_resolution_only_labels_with_required_scalars_are_accepted(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: verify one prior issue",
                    "Outcome: prior issue resolution verified",
                    "Resolved issue: exact old issue",
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.resolved_open_issues == ("exact old issue",)
    assert record.open_issues == ()
    assert record.decisions == ()


def test_only_the_last_versioned_capture_block_is_trusted(tmp_path):
    source = tmp_path / "session.jsonl"
    message = "\n".join(
        (
            "Quoted documentation must stay inert:",
            "```text",
            CAPTURE_START,
            "Objective: fenced fake objective",
            "Outcome: fenced fake outcome",
            CAPTURE_END,
            "```",
            "> Objective: quoted fake objective",
            "Outcome: unbounded fake outcome",
            _managed_capture(
                "Objective: exact objective",
                "Outcome: exact outcome",
                "Decision: exact decision",
            ),
        )
    )
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(_complete("turn-1", message))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert len(batch.records) == 1
    record = batch.records[0]
    assert record.objective == "exact objective"
    assert record.outcome == "exact outcome"
    assert record.decisions == ("exact decision",)


def test_last_of_two_unfenced_capture_blocks_wins(tmp_path):
    source = tmp_path / "session.jsonl"
    message = "\n".join(
        (
            _managed_capture(
                "Objective: stale objective",
                "Outcome: stale outcome",
            ),
            _managed_capture(
                "Objective: exact objective",
                "Outcome: exact outcome",
            ),
        )
    )
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(_complete("turn-1", message))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.objective, record.outcome) for record in batch.records] == [
        ("exact objective", "exact outcome")
    ]


def test_legacy_completion_without_capture_block_is_ignored(tmp_path):
    completion = _complete("turn-1", "Outcome: legacy outcome")
    completion["payload"]["last_agent_message"] = "Outcome: legacy outcome"
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(completion)
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "no_capture_block:1" in batch.warnings


def test_duplicate_scalar_in_managed_capture_block_fails_closed(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: first objective",
                    "Objective: second objective",
                    "Outcome: exact outcome",
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings


def test_capture_block_over_list_limit_fails_closed_and_advances_scope(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: exact objective",
                    "Outcome: exact outcome",
                    *(f"Decision: item-{index}" for index in range(101)),
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings
    assert batch.next_checkpoint.cursor["offset"] == source.stat().st_size


@pytest.mark.parametrize(
    "decision_lines",
    (
        ["Decision: " + (".env " * 1600).strip()],
        ["Decision: " + (".env " * 1000).strip()] * 8,
    ),
    ids=("post-redaction-field-expansion", "post-redaction-aggregate-expansion"),
)
def test_capture_block_post_redaction_expansion_fails_closed_and_advances_scope(
    tmp_path,
    decision_lines,
):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: exact objective",
                    "Outcome: exact outcome",
                    *decision_lines,
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings
    assert batch.next_checkpoint.cursor["offset"] == source.stat().st_size


def test_blocked_turn_never_exceeds_the_configured_checkpoint_context_bound(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-current"))
        + _line(_context("turn-current", "relative", "model-one"))
    )
    adapter = CodexAdapter(tmp_path, Redactor(), max_context_bytes=32)

    first = adapter.read_incremental("session.jsonl", None)
    resumed = adapter.read_incremental("session.jsonl", first.next_checkpoint)

    assert len(first.next_checkpoint.cursor["contexts_json"].encode("utf-8")) <= 32
    assert first.next_checkpoint.cursor["offset"] == source.stat().st_size
    assert resumed.next_checkpoint.cursor["offset"] == source.stat().st_size
    assert "source_restarted:1" not in resumed.warnings


@pytest.mark.parametrize("trailing", ("This was only an example.", "</pre>"))
def test_capture_block_with_trailing_user_prose_fails_closed(tmp_path, trailing):
    source = tmp_path / "session.jsonl"
    message = (
        _managed_capture(
            "Objective: exact objective",
            "Outcome: exact outcome",
        )
        + "\n"
        + trailing
    )
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(_complete("turn-1", message))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings


def test_capture_block_allows_only_the_trailing_memory_citation_directive(tmp_path):
    source = tmp_path / "session.jsonl"
    message = (
        _managed_capture(
            "Objective: exact objective",
            "Outcome: exact outcome",
        )
        + "\n"
        + "\n".join(
            (
                "<oai-mem-citation>",
                "<citation_entries>",
                "MEMORY.md:1-2|note=[safe note]",
                "</citation_entries>",
                "<rollout_ids>",
                "70000000-0000-4000-8000-00000000000a",
                "</rollout_ids>",
                "</oai-mem-citation>",
            )
        )
    )
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(_complete("turn-1", message))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.objective, record.outcome) for record in batch.records] == [
        ("exact objective", "exact outcome")
    ]


def test_each_capture_value_is_redacted_independently(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/repo", "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: exact objective",
                    'Risk: password="unterminated',
                    "Outcome: exact outcome",
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert len(batch.records) == 1
    assert batch.records[0].outcome == "exact outcome"
    assert batch.records[0].risks == ('password="[REDACTED:password]"',)


def test_runtime_namespace_uses_exact_latest_model_for_thread_and_cwd(tmp_path):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session_with_ids(thread_id, session_id))
        + _line(_context("turn-old", "/fixture/repo", "gpt-old"))
        + _line(_complete("turn-old", "Outcome: old outcome"))
        + _line(_session(session_id))
        + _line(_context("turn-current", "/fixture/repo", "gpt-5.6-sol"))
        + _line(
            {
                "type": "response_item",
                "payload": {"content": "RAW_CONVERSATION_MUST_NOT_BE_RETURNED"},
            }
        )
    )

    namespace = CodexAdapter(tmp_path, Redactor()).resolve_namespace(
        thread_id,
        Path("/fixture/repo"),
    )

    assert namespace == Namespace(source_agent="codex", model_id="gpt-5.6-sol")


def test_runtime_namespace_rejects_filename_suffix_spoof(tmp_path):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"))
        + _line(_context("turn-current", "/fixture/repo", "spoofed-model"))
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(tmp_path, Redactor()).resolve_namespace(
            thread_id,
            Path("/fixture/repo"),
        )


def test_runtime_namespace_rejects_ambiguous_active_turns(tmp_path):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-a", "/fixture/repo", "model-a"))
        + _line(_context("turn-b", "/fixture/repo", "model-b"))
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(tmp_path, Redactor()).resolve_namespace(
            thread_id,
            Path("/fixture/repo"),
        )


def test_runtime_namespace_accepts_multiple_active_turns_for_one_exact_model(tmp_path):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-a", "/fixture/repo", "gpt-5.6-sol"))
        + _line(_context("turn-b", "/fixture/repo", "gpt-5.6-sol"))
    )

    namespace = CodexAdapter(tmp_path, Redactor()).resolve_namespace(
        thread_id,
        Path("/fixture/repo"),
    )

    assert namespace == Namespace(source_agent="codex", model_id="gpt-5.6-sol")


@pytest.mark.parametrize(
    "unsafe_model",
    ("gpt-safe\nINJECTED", "provider/password=RAW_MODEL_CREDENTIAL"),
)
def test_runtime_namespace_rejects_unsafe_model_metadata(tmp_path, unsafe_model):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id)) + _line(_context("turn-current", "/fixture/repo", unsafe_model))
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(tmp_path, Redactor()).resolve_namespace(
            thread_id,
            Path("/fixture/repo"),
        )


@pytest.mark.parametrize(
    "bad_line",
    (
        json.dumps({"type": [], "payload": {}}, separators=(",", ":")) + "\n",
        '{"type":' + ("9" * 4301) + ',"payload":{}}\n',
        ("[" * 1200) + "0" + ("]" * 1200) + "\n",
    ),
)
def test_runtime_namespace_maps_hostile_json_values_to_context_unavailable(tmp_path, bad_line):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        bad_line
        + _line(_session(thread_id))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(tmp_path, Redactor()).resolve_namespace(
            thread_id,
            Path("/fixture/repo"),
        )


@pytest.mark.parametrize(
    "hidden_lifecycle",
    (
        _line(_complete("turn-current", "Outcome: " + ("x" * 512))),
        _line(
            _context(
                "turn-current",
                "/fixture/repo",
                "model-new",
                "x" * 512,
            )
        ),
        '{"type":"turn_context","payload":{"turn_id":"turn-current",'
        '"cwd":"/fixture/repo","model":"model-new"\n',
    ),
    ids=("oversized-completion", "oversized-context", "malformed-context"),
)
def test_runtime_namespace_rejects_unreadable_lifecycle_evidence(
    tmp_path,
    hidden_lifecycle,
):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-current", "/fixture/repo", "model-old"))
        + hidden_lifecycle
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(tmp_path, Redactor(), max_line_bytes=256).resolve_namespace(
            thread_id,
            Path("/fixture/repo"),
        )


@pytest.mark.parametrize(
    "ignored_record",
    (
        {
            "type": "response_item",
            "payload": {"content": "x" * 1024},
        },
        {
            "type": "event_msg",
            "payload": {"type": "mcp_tool_call_end", "content": "x" * 1024},
        },
    ),
    ids=("response-item", "ignored-event"),
)
def test_runtime_namespace_allows_bounded_oversized_nonsemantic_records(
    tmp_path,
    ignored_record,
):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(ignored_record)
    )

    namespace = CodexAdapter(tmp_path, Redactor(), max_line_bytes=512).resolve_namespace(
        thread_id,
        Path("/fixture/repo"),
    )

    assert namespace.model_id == "model-current"


@pytest.mark.parametrize(
    "ignored_record",
    (
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": "x" * 2048,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "image_generation_end",
                "result": "x" * 2048,
            },
        },
    ),
    ids=("custom-tool-output", "image-generation-end"),
)
def test_runtime_namespace_allows_known_nonsemantic_record_beyond_semantic_limit(
    tmp_path,
    ignored_record,
):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(ignored_record)
    )

    namespace = CodexAdapter(
        tmp_path,
        Redactor(),
        max_line_bytes=256,
        max_record_bytes=512,
        max_nonsemantic_record_bytes=4096,
        max_read_bytes=8192,
    ).resolve_namespace(thread_id, Path("/fixture/repo"))

    assert namespace.model_id == "model-current"


@pytest.mark.parametrize(
    "record_type,payload",
    (
        (
            "response_item",
            {
                "type": "custom_tool_call_output",
                "output": "x" * 4_194_304,
            },
        ),
        (
            "event_msg",
            {
                "type": "image_generation_end",
                "result": "x" * 4_194_304,
            },
        ),
    ),
    ids=("custom-tool-output", "image-generation-end"),
)
def test_runtime_namespace_default_limits_allow_confirmed_large_nonsemantic_records(
    tmp_path,
    record_type,
    payload,
):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line({"type": record_type, "payload": payload})
    )

    namespace = CodexAdapter(tmp_path, Redactor()).resolve_namespace(
        thread_id,
        Path("/fixture/repo"),
    )

    assert namespace.model_id == "model-current"


def test_runtime_namespace_rejects_semantic_record_beyond_semantic_limit(tmp_path):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    oversized_context = _context("turn-current", "/fixture/repo", "model-current")
    oversized_context["payload"]["padding"] = "x" * 2048
    source.write_text(_line(_session(thread_id)) + _line(oversized_context))

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(
            tmp_path,
            Redactor(),
            max_line_bytes=256,
            max_record_bytes=512,
            max_nonsemantic_record_bytes=4096,
            max_read_bytes=8192,
        ).resolve_namespace(thread_id, Path("/fixture/repo"))


def test_runtime_namespace_rejects_nonsemantic_record_beyond_extended_limit(tmp_path):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": "x" * 4096,
                },
            }
        )
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(
            tmp_path,
            Redactor(),
            max_line_bytes=256,
            max_record_bytes=512,
            max_nonsemantic_record_bytes=2048,
            max_read_bytes=8192,
        ).resolve_namespace(thread_id, Path("/fixture/repo"))


@pytest.mark.parametrize(
    "duplicate_record",
    (
        (
            '{"type":"turn_context","type":"response_item","payload":'
            '{"type":"custom_tool_call_output","output":"' + ("x" * 2048) + '"}}\n'
        ),
        (
            '{"type":"response_item","payload":'
            '{"type":"turn_context","type":"custom_tool_call_output","output":"'
            + ("x" * 2048)
            + '"}}\n'
        ),
    ),
    ids=("top-level-type", "payload-type"),
)
def test_runtime_namespace_rejects_duplicate_keys_in_extended_nonsemantic_record(
    tmp_path,
    duplicate_record,
):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + duplicate_record
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(
            tmp_path,
            Redactor(),
            max_line_bytes=256,
            max_record_bytes=512,
            max_nonsemantic_record_bytes=4096,
            max_read_bytes=8192,
        ).resolve_namespace(thread_id, Path("/fixture/repo"))


def test_runtime_namespace_rejects_ambiguous_turn_even_with_valid_sibling(tmp_path):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id))
        + _line(_context("turn-sibling", "/fixture/repo", "model-old"))
        + _line(_context("turn-current", "/fixture/repo", "model-new"))
        + _line(_context("turn-current", "/fixture/repo", "model-other"))
    )

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(tmp_path, Redactor()).resolve_namespace(
            thread_id,
            Path("/fixture/repo"),
        )


def test_runtime_namespace_rejects_snapshot_growth_during_resolution(
    tmp_path,
    monkeypatch,
):
    thread_id = "70000000-0000-4000-8000-00000000000a"
    source = tmp_path / f"rollout-2026-07-15-{thread_id}.jsonl"
    source.write_text(
        _line(_session(thread_id)) + _line(_context("turn-current", "/fixture/repo", "model-old"))
    )
    original = codex_module._runtime_model_from_descriptor

    def append_new_model(*args, **kwargs):
        model = original(*args, **kwargs)
        with source.open("a") as session_file:
            session_file.write(_line(_complete("turn-current", "Outcome: old outcome")))
            session_file.write(_line(_context("turn-new", "/fixture/repo", "model-new")))
        return model

    monkeypatch.setattr(codex_module, "_runtime_model_from_descriptor", append_new_model)

    with pytest.raises(CodexContextUnavailable, match="codex_context_unavailable"):
        CodexAdapter(tmp_path, Redactor()).resolve_namespace(
            thread_id,
            Path("/fixture/repo"),
        )


def test_repeated_identical_session_metadata_and_context_are_idempotent(tmp_path):
    source = tmp_path / "session.jsonl"
    context = _context("turn-current", "/fixture/repo", "gpt-5.6-sol")
    source.write_text(
        _line(_session("session-current"))
        + _line(context)
        + _line(
            _context(
                "turn-current",
                "/fixture/repo",
                "gpt-5.6-sol",
                "refreshed summary",
            )
        )
        + _line(_session("session-current"))
        + _line(_complete("turn-current", "Outcome: exact outcome"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("session-current:turn-current", "exact outcome")
    ]
    assert "ambiguous_turn:1" not in batch.warnings


def test_repeated_session_alias_with_conflicting_id_fails_closed(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session_with_ids("thread-a", "session-alias"))
        + _line(_context("turn-current", "/fixture/repo", "gpt-5.6-sol"))
        + _line(_session_with_ids("thread-b", "session-alias"))
        + _line(_context("turn-spoofed", "/fixture/repo", "gpt-5.6-sol"))
        + _line(_complete("turn-spoofed", "Outcome: must not import"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "ambiguous_session:1" in batch.warnings


@pytest.mark.parametrize(
    "unsafe_session_id,unsafe_turn_id,unsafe_model",
    (
        ("password=RAW_ID_SECRET", "turn-safe", "model-safe"),
        ("session-safe", "turn\nINJECTED", "model-safe"),
        ("session-safe", "turn-safe", "provider/password=RAW_MODEL_CREDENTIAL"),
    ),
    ids=("session-id", "turn-id", "model-id"),
)
def test_incremental_capture_rejects_unsafe_provenance_identifiers(
    tmp_path,
    unsafe_session_id,
    unsafe_turn_id,
    unsafe_model,
):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session(unsafe_session_id))
        + _line(_context(unsafe_turn_id, "/fixture/repo", unsafe_model))
        + _line(_complete(unsafe_turn_id, "Outcome: must not import"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert any(warning.startswith("unsafe_identifier:") for warning in batch.warnings)


@pytest.mark.parametrize(
    ("session_id", "turn_id"),
    (("password", "hunter2"), ("intranet", "PRIVATE_REPO.git")),
)
def test_incremental_capture_rejects_unsafe_composite_source_identifier(
    tmp_path,
    session_id,
    turn_id,
):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session(session_id))
        + _line(_context(turn_id, "/fixture/repo", "model-safe"))
        + _line(_complete(turn_id, "Outcome: must not import"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "unsafe_identifier:1" in batch.warnings


@pytest.mark.parametrize(
    "lifecycle_case",
    ("oversized-session", "malformed-context"),
)
def test_incremental_capture_invalidates_state_after_unreadable_lifecycle(
    tmp_path,
    lifecycle_case,
):
    if lifecycle_case == "oversized-session":
        value = _session("session-new")
        value["payload"]["padding"] = "x" * 1024
        hidden_lifecycle = _line(value)
    else:
        hidden_lifecycle = '{"type":"turn_context","payload":{"turn_id":"turn-current"\n'
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-old"))
        + _line(_context("turn-current", "/fixture/repo", "model-old"))
        + hidden_lifecycle
        + _line(_complete("turn-current", "Outcome: must not import"))
    )

    batch = CodexAdapter(tmp_path, Redactor(), max_line_bytes=512).read_incremental(
        "session.jsonl",
        None,
    )

    assert batch.records == ()
    assert "unsafe_lifecycle:1" in batch.warnings


@pytest.mark.parametrize(
    "ignored_record",
    (
        {
            "type": "response_item",
            "payload": {"content": "x" * 1024},
        },
        {
            "type": "event_msg",
            "payload": {"type": "image_generation_end", "content": "x" * 1024},
        },
    ),
    ids=("response-item", "ignored-event"),
)
def test_incremental_preserves_state_across_bounded_oversized_nonsemantic_records(
    tmp_path,
    ignored_record,
):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(ignored_record)
        + _line(_complete("turn-current", "Outcome: exact outcome"))
    )

    batch = CodexAdapter(tmp_path, Redactor(), max_line_bytes=512).read_incremental(
        "session.jsonl",
        None,
    )

    assert [(record.namespace.model_id, record.outcome) for record in batch.records] == [
        ("model-current", "exact outcome")
    ]
    assert "oversized_line:1" in batch.warnings
    assert not any(warning.startswith("unsafe_lifecycle:") for warning in batch.warnings)


@pytest.mark.parametrize(
    "ignored_record",
    (
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": "x" * 2048,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "image_generation_end",
                "result": "x" * 2048,
            },
        },
    ),
    ids=("custom-tool-output", "image-generation-end"),
)
def test_incremental_preserves_state_across_nonsemantic_record_beyond_semantic_limit(
    tmp_path,
    ignored_record,
):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(ignored_record)
        + _line(_complete("turn-current", "Outcome: exact outcome"))
    )

    batch = CodexAdapter(
        tmp_path,
        Redactor(),
        max_line_bytes=384,
        max_record_bytes=512,
        max_nonsemantic_record_bytes=4096,
        max_read_bytes=8192,
    ).read_incremental("session.jsonl", None)

    assert [(record.namespace.model_id, record.outcome) for record in batch.records] == [
        ("model-current", "exact outcome")
    ]
    assert "oversized_line:1" in batch.warnings
    assert not any(warning.startswith("unsafe_lifecycle:") for warning in batch.warnings)


@pytest.mark.parametrize(
    "override",
    (
        {"max_nonsemantic_record_bytes": 0},
        {"max_nonsemantic_record_bytes": 64},
        {"max_nonsemantic_record_bytes": 512, "max_read_bytes": 512},
        {
            "max_nonsemantic_record_bytes": 2048,
            "max_read_bytes": 4096,
            "max_runtime_scan_bytes": 1024,
        },
    ),
    ids=("non-positive", "below-semantic", "not-below-read", "above-runtime-scan"),
)
def test_codex_adapter_rejects_inconsistent_extended_nonsemantic_limits(tmp_path, override):
    limits = {
        "max_line_bytes": 64,
        "max_record_bytes": 128,
        "max_nonsemantic_record_bytes": 256,
        "max_read_bytes": 512,
        "max_runtime_scan_bytes": 4096,
    }
    limits.update(override)

    with pytest.raises(ValueError, match="adapter (?:byte )?limits"):
        CodexAdapter(tmp_path, Redactor(), **limits)


def test_checkpoint_policy_change_restarts_before_extended_nonsemantic_record(tmp_path):
    source = tmp_path / "session.jsonl"
    prefix = (
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": "x" * 2048,
                },
            }
        )
    )
    source.write_text(prefix)
    larger_policy = CodexAdapter(
        tmp_path,
        Redactor(),
        max_line_bytes=384,
        max_record_bytes=512,
        max_nonsemantic_record_bytes=4096,
        max_read_bytes=8192,
    )
    checkpoint = larger_policy.read_incremental("session.jsonl", None).next_checkpoint
    source.write_text(prefix + _line(_complete("turn-current", "Outcome: must not import")))
    smaller_policy = CodexAdapter(
        tmp_path,
        Redactor(),
        max_line_bytes=384,
        max_record_bytes=512,
        max_nonsemantic_record_bytes=1024,
        max_read_bytes=8192,
    )

    resumed = smaller_policy.read_incremental("session.jsonl", checkpoint)
    fresh = smaller_policy.read_incremental("session.jsonl", None)

    assert resumed.records == fresh.records == ()
    assert "source_restarted:1" in resumed.warnings


def test_checkpoint_policy_change_restarts_before_context_record_limit(tmp_path):
    source = tmp_path / "session.jsonl"
    prefix = (
        _line(_session("session-current"))
        + _line(_context("turn-1", "/fixture/repo", "model-current"))
        + _line(_context("turn-2", "/fixture/repo", "model-current"))
    )
    source.write_text(prefix)

    def drain(
        adapter: CodexAdapter,
        checkpoint: AdapterCheckpoint | None,
    ) -> tuple[list[NormalizedTaskRecord], list[str], AdapterCheckpoint]:
        records: list[NormalizedTaskRecord] = []
        warnings: list[str] = []
        previous_offset = -1
        while True:
            batch = adapter.read_incremental("session.jsonl", checkpoint)
            records.extend(batch.records)
            warnings.extend(batch.warnings)
            checkpoint = batch.next_checkpoint
            offset = int(checkpoint.cursor["offset"])
            assert offset > previous_offset
            if offset >= source.stat().st_size:
                return records, warnings, checkpoint
            previous_offset = offset

    larger_policy = CodexAdapter(tmp_path, Redactor(), max_records=2)
    _initial_records, _initial_warnings, checkpoint = drain(larger_policy, None)
    source.write_text(prefix + _line(_complete("turn-1", "Outcome: must not import")))
    smaller_policy = CodexAdapter(tmp_path, Redactor(), max_records=1)

    resumed_records, resumed_warnings, _resumed_checkpoint = drain(
        smaller_policy,
        checkpoint,
    )
    fresh_records, _fresh_warnings, _fresh_checkpoint = drain(smaller_policy, None)

    assert resumed_records == fresh_records == []
    assert "source_restarted:1" in resumed_warnings


def test_incremental_checkpoint_can_cross_a_record_larger_than_the_read_window(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-old"))
        + _line(_context("turn-old", "/fixture/repo", "model-old"))
        + _line({"type": "compacted", "payload": {"content": "x" * 1600}})
        + _line(_session("session-new"))
        + _line(_context("turn-new", "/fixture/repo", "model-new"))
        + _line(_complete("turn-new", "Outcome: exact outcome"))
    )
    adapter = CodexAdapter(
        tmp_path,
        Redactor(),
        max_line_bytes=384,
        max_record_bytes=512,
        max_read_bytes=768,
    )
    checkpoint = None
    records = []
    offsets = []

    for _ in range(6):
        batch = adapter.read_incremental("session.jsonl", checkpoint)
        records.extend(batch.records)
        checkpoint = batch.next_checkpoint
        offsets.append(checkpoint.cursor["offset"])
        if records:
            break

    assert [(record.source_record_id, record.namespace.model_id) for record in records] == [
        ("session-new:turn-new", "model-new")
    ]
    assert offsets == sorted(offsets)
    assert len(set(offsets)) == len(offsets)


def test_incremental_read_accepts_verified_append_only_growth(tmp_path, monkeypatch):
    source = tmp_path / "session.jsonl"
    initial_document = (
        _line(_session("session-current"))
        + _line(_context("turn-initial", "/fixture/repo", "model-current"))
        + _line(_complete("turn-initial", "Outcome: initial outcome"))
    )
    ready_document = _line(_context("turn-ready", "/fixture/repo", "model-current")) + _line(
        _complete("turn-ready", "Outcome: ready outcome")
    )
    concurrent_document = _line(
        _context("turn-concurrent", "/fixture/repo", "model-current")
    ) + _line(_complete("turn-concurrent", "Outcome: concurrent outcome"))
    source.write_text(initial_document)
    adapter = CodexAdapter(tmp_path, Redactor())
    initial = adapter.read_incremental("session.jsonl", None)
    initial_offset = initial.next_checkpoint.cursor["offset"]
    assert initial_offset == len(initial_document.encode())
    assert initial_offset > 0

    with source.open("a", encoding="utf-8") as session_file:
        session_file.write(ready_document)
    original_root_matches = codex_module._root_matches
    appended = False

    def append_after_snapshot(path, descriptor):
        nonlocal appended
        if not appended:
            with source.open("a", encoding="utf-8") as session_file:
                session_file.write(concurrent_document)
            appended = True
        return original_root_matches(path, descriptor)

    monkeypatch.setattr(codex_module, "_root_matches", append_after_snapshot)

    try:
        resumed = adapter.read_incremental("session.jsonl", initial.next_checkpoint)
    except PermissionError as error:
        pytest.fail(f"append-only growth was rejected: {error}")

    assert [record.source_record_id for record in resumed.records] == ["session-current:turn-ready"]
    assert resumed.next_checkpoint.cursor["offset"] == len(
        (initial_document + ready_document).encode()
    )

    final = adapter.read_incremental("session.jsonl", resumed.next_checkpoint)

    assert [record.source_record_id for record in final.records] == [
        "session-current:turn-concurrent"
    ]


def test_incremental_read_rejects_prefix_rewrite_hidden_by_growth(tmp_path, monkeypatch):
    source = tmp_path / "session.jsonl"
    initial_document = (
        _line(_session("session-current"))
        + _line(_context("turn-initial", "/fixture/repo", "model-current"))
        + _line(_complete("turn-initial", "Outcome: initial outcome"))
    )
    rewritten_initial_document = initial_document.replace(
        "session-current",
        "session-mutated",
        1,
    )
    ready_document = _line(_context("turn-ready", "/fixture/repo", "model-current"))
    concurrent_document = _line(_context("turn-concurrent", "/fixture/repo", "model-current"))
    source.write_text(initial_document)
    adapter = CodexAdapter(tmp_path, Redactor())
    initial = adapter.read_incremental("session.jsonl", None)
    initial_offset = initial.next_checkpoint.cursor["offset"]
    assert initial_offset == len(initial_document.encode())
    assert initial_offset > 0

    with source.open("a", encoding="utf-8") as session_file:
        session_file.write(ready_document)
    original_root_matches = codex_module._root_matches
    mutated = False

    def rewrite_after_snapshot(path, descriptor):
        nonlocal mutated
        if not mutated:
            source.write_text(rewritten_initial_document + ready_document + concurrent_document)
            mutated = True
        return original_root_matches(path, descriptor)

    monkeypatch.setattr(codex_module, "_root_matches", rewrite_after_snapshot)

    with pytest.raises(PermissionError, match="session scope changed"):
        adapter.read_incremental("session.jsonl", initial.next_checkpoint)


def test_replay_records_anchors_nonsemantic_policy_before_incomplete_extended_tail(tmp_path):
    source = tmp_path / "session.jsonl"
    complete_prefix = (
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(_complete("turn-current", "Outcome: exact outcome"))
    )
    incomplete_nonsemantic = (
        '{"type":"response_item","payload":'
        '{"type":"custom_tool_call_output","output":"' + ("x" * 2048)
    )
    source.write_text(complete_prefix + incomplete_nonsemantic)
    adapter = CodexAdapter(
        tmp_path,
        Redactor(),
        max_line_bytes=384,
        max_record_bytes=512,
        max_nonsemantic_record_bytes=4096,
        max_read_bytes=8192,
    )

    batch = adapter.replay_records(
        "session.jsonl",
        ("session-current:turn-current",),
    )

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("session-current:turn-current", "exact outcome")
    ]
    prefix_bytes = complete_prefix.encode()
    metadata = source.stat()
    expected_anchor = {
        "device": int(metadata.st_dev),
        "inode": int(metadata.st_ino),
        "nonsemantic_record_policy": 1,
        "parser_policy_sha256": hashlib.sha256(
            json.dumps(
                {
                    "max_context_bytes": 1_048_576,
                    "max_line_bytes": 384,
                    "max_nonsemantic_record_bytes": 4096,
                    "max_record_bytes": 512,
                    "max_records": 10_000,
                    "nonsemantic_record_policy": 1,
                    "parser_version": "codex-v3",
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest(),
        "parser_version": "codex-v3",
        "prefix_length": len(prefix_bytes),
        "prefix_sha256": hashlib.sha256(prefix_bytes).hexdigest(),
        "scope": "session.jsonl",
    }
    assert (
        batch.source_hash
        == hashlib.sha256(
            json.dumps(expected_anchor, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
    )


@pytest.mark.parametrize(
    "terminal_case,warning_category",
    (("missing-message", "incomplete_completion"), ("bad-timestamp", "invalid_timestamp")),
)
def test_invalid_terminal_consumes_context_before_duplicate_completion(
    tmp_path,
    terminal_case,
    warning_category,
):
    invalid = _complete("turn-current", "Outcome: invalid terminal")
    if terminal_case == "missing-message":
        invalid["payload"].pop("last_agent_message")
    else:
        invalid["timestamp"] = "not-a-timestamp"
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(invalid)
        + _line(_complete("turn-current", "Outcome: must not import"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert any(warning.startswith(f"{warning_category}:") for warning in batch.warnings)
    assert "orphan_completion:1" in batch.warnings


def test_invalid_repeated_context_blocks_the_old_model(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-old"))
        + _line(
            _context(
                "turn-current",
                "/fixture/repo",
                "provider/password=RAW_MODEL_CREDENTIAL",
            )
        )
        + _line(_complete("turn-current", "Outcome: must not import"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "unsafe_identifier:1" in batch.warnings
    assert "ambiguous_completion:1" in batch.warnings


def test_incremental_recovers_only_after_a_new_valid_session_epoch(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-old"))
        + _line(_context("turn-old", "/fixture/repo", "model-old"))
        + '{"type":"turn_context","payload":{"turn_id":"turn-old"\n'
        + _line(_complete("turn-old", "Outcome: must not import"))
        + _line(_session("session-new"))
        + _line(_context("turn-new", "/fixture/repo", "model-new"))
        + _line(_complete("turn-new", "Outcome: exact outcome"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.source_record_id, record.namespace.model_id) for record in batch.records] == [
        ("session-new:turn-new", "model-new")
    ]
    assert "unsafe_lifecycle:1" in batch.warnings


def test_capture_suffix_has_an_explicit_line_budget():
    assert codex_module._valid_capture_suffix([""] * 512)
    assert not codex_module._valid_capture_suffix([""] * 513)


def test_aborted_turn_produces_no_record():
    adapter = CodexAdapter(FIXTURES, Redactor())

    batch = adapter.read_incremental("aborted-turn.jsonl", None)

    assert batch.records == ()


def test_discovery_returns_sorted_fixture_scopes():
    adapter = CodexAdapter(FIXTURES, Redactor())

    assert adapter.discover_scopes() == (
        "aborted-turn.jsonl",
        "completed-turn.jsonl",
        "managed-completed-turn.jsonl",
    )


def test_v1_checkpoint_state_is_rebuilt_for_current_completion(tmp_path):
    source = tmp_path / "session.jsonl"
    prefix = (
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "gpt-5.6-sol"))
        + _line(_session("session-current"))
    )
    source.write_text(prefix + _line(_complete("turn-current", "Outcome: exact outcome")))
    metadata = source.stat()
    prefix_bytes = prefix.encode()
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope="session.jsonl",
        cursor={
            "contexts_json": "{}",
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "observed_size": len(prefix_bytes),
            "offset": len(prefix_bytes),
            "relative_path": "session.jsonl",
            "session_id": "session-current",
            "prefix_length": len(prefix_bytes),
            "prefix_sha256": hashlib.sha256(prefix_bytes).hexdigest(),
        },
        parser_version="codex-v1",
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", checkpoint)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("session-current:turn-current", "exact outcome")
    ]
    assert "source_restarted:1" in batch.warnings


def test_v2_checkpoint_without_oversized_line_state_is_rebuilt(tmp_path):
    source = tmp_path / "session.jsonl"
    prefix = (
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line({"type": "response_item", "payload": {"content": "x" * 300_000}})
    )
    source.write_text(prefix + _line(_complete("turn-current", "Outcome: exact outcome")))
    metadata = source.stat()
    prefix_bytes = prefix.encode()
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope="session.jsonl",
        cursor={
            "contexts_json": "{}",
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "observed_size": len(prefix_bytes),
            "offset": len(prefix_bytes),
            "relative_path": "session.jsonl",
            "session_id": "",
            "session_meta_id": "",
            "prefix_length": len(prefix_bytes),
            "prefix_sha256": hashlib.sha256(prefix_bytes).hexdigest(),
        },
        parser_version="codex-v2",
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", checkpoint)

    assert [(record.source_record_id, record.namespace.model_id) for record in batch.records] == [
        ("session-current:turn-current", "model-current")
    ]
    assert "source_restarted:1" in batch.warnings


def test_v3_checkpoint_without_extended_nonsemantic_policy_is_rebuilt(tmp_path):
    source = tmp_path / "session.jsonl"
    prefix = (
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-current"))
        + _line(
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "output": "x" * 2048,
                },
            }
        )
    )
    source.write_text(prefix + _line(_complete("turn-current", "Outcome: exact outcome")))
    metadata = source.stat()
    prefix_bytes = prefix.encode()
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope="session.jsonl",
        cursor={
            "contexts_json": "{}",
            "device": int(metadata.st_dev),
            "inode": int(metadata.st_ino),
            "observed_size": len(prefix_bytes),
            "offset": len(prefix_bytes),
            "relative_path": "session.jsonl",
            "session_id": "",
            "session_meta_id": "",
            "discarding_oversized_line": 0,
            "prefix_length": len(prefix_bytes),
            "prefix_sha256": hashlib.sha256(prefix_bytes).hexdigest(),
        },
        parser_version="codex-v3",
    )

    batch = CodexAdapter(
        tmp_path,
        Redactor(),
        max_line_bytes=384,
        max_record_bytes=512,
        max_nonsemantic_record_bytes=4096,
        max_read_bytes=8192,
    ).read_incremental("session.jsonl", checkpoint)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("session-current:turn-current", "exact outcome")
    ]
    assert "source_restarted:1" in batch.warnings


def test_saved_offset_resumes_with_checkpointed_session_state(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", "/fixture/one", "model-one"))
        + _line(_complete("turn-1", "Outcome: first"))
    )
    adapter = CodexAdapter(tmp_path, Redactor())
    first = adapter.read_incremental("session.jsonl", None)
    source.write_text(
        source.read_text()
        + _line(_context("turn-2", "/fixture/two", "model-two"))
        + _line(_complete("turn-2", "Outcome: second"))
    )

    resumed = adapter.read_incremental("session.jsonl", first.next_checkpoint)

    assert [record.source_record_id for record in resumed.records] == ["session-1:turn-2"]
    assert resumed.records[0].namespace.model_id == "model-two"
    assert resumed.records[0].cwd == Path("/fixture/two")


def test_unsafe_checkpoint_lifecycle_is_rebuilt_from_the_verified_prefix(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("session-current"))
        + _line(_context("turn-current", "/fixture/repo", "model-safe"))
    )
    adapter = CodexAdapter(tmp_path, Redactor())
    first = adapter.read_incremental("session.jsonl", None)
    contexts = json.loads(first.next_checkpoint.cursor["contexts_json"])
    contexts["turn-current"]["model"] = "provider/password=RAW_MODEL_CREDENTIAL"
    poisoned = AdapterCheckpoint(
        adapter=first.next_checkpoint.adapter,
        scope=first.next_checkpoint.scope,
        cursor={
            **first.next_checkpoint.cursor,
            "contexts_json": json.dumps(contexts, separators=(",", ":")),
        },
        parser_version=first.next_checkpoint.parser_version,
    )
    with source.open("a") as session_file:
        session_file.write(_line(_complete("turn-current", "Outcome: exact outcome")))

    resumed = adapter.read_incremental("session.jsonl", poisoned)

    assert [(record.namespace.model_id, record.outcome) for record in resumed.records] == [
        ("model-safe", "exact outcome")
    ]
    assert "source_restarted:1" in resumed.warnings
    assert "RAW_MODEL_CREDENTIAL" not in repr(resumed)


def test_incomplete_final_line_is_not_checkpointed_until_newline(tmp_path):
    prefix = _line(_session()) + _line(_context("turn-1", "/fixture/repo", "model-one"))
    completion = _line(_complete("turn-1", "Outcome: complete")).rstrip("\n")
    source = tmp_path / "session.jsonl"
    source.write_text(prefix + completion)
    adapter = CodexAdapter(tmp_path, Redactor())

    partial = adapter.read_incremental("session.jsonl", None)

    assert partial.records == ()
    assert partial.next_checkpoint.cursor["offset"] == len(prefix.encode())
    with source.open("a") as file:
        file.write("\n")
    completed = adapter.read_incremental("session.jsonl", partial.next_checkpoint)
    assert [record.outcome for record in completed.records] == ["complete"]


@pytest.mark.parametrize("mode", ["replacement", "truncation"])
def test_replacement_or_truncation_restarts_without_skipping(tmp_path, mode):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("old"))
        + _line(
            _context(
                "old-turn",
                "/old",
                "old-model",
                summary="old task " * 40,
            )
        )
        + _line(_complete("old-turn", "Outcome: old"))
    )
    adapter = CodexAdapter(tmp_path, Redactor())
    checkpoint = adapter.read_incremental("session.jsonl", None).next_checkpoint
    replacement_text = (
        _line(_session("new"))
        + _line(_context("new-turn", "/new", "new-model"))
        + _line(_complete("new-turn", "Outcome: new"))
    )
    if mode == "replacement":
        replacement = tmp_path / "replacement"
        replacement.write_text(replacement_text)
        os.replace(replacement, source)
    else:
        source.write_text(replacement_text)

    batch = adapter.read_incremental("session.jsonl", checkpoint)

    assert [record.source_record_id for record in batch.records] == ["new:new-turn"]
    assert "source_restarted:1" in batch.warnings


def test_unknown_malformed_and_oversized_records_warn_without_text(tmp_path):
    secret = "WARNING_MUST_NOT_CONTAIN_SECRET"
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + "{malformed "
        + secret
        + "}\n"
        + json.dumps({"padding": secret * 30})
        + "\n"
        + _line(
            {
                "timestamp": "2026-07-12T00:00:02Z",
                "type": "event_msg",
                "payload": {"type": "mystery_event", "detail": secret},
            }
        )
    )
    adapter = CodexAdapter(tmp_path, Redactor(), max_line_bytes=256)

    batch = adapter.read_incremental("session.jsonl", None)

    assert set(batch.warnings) == {
        "malformed_json:1",
        "oversized_line:1",
        "unknown_event:1",
        "unsafe_lifecycle:3",
    }
    assert secret not in repr(batch.warnings)


@pytest.mark.parametrize(
    ("bad_line", "warning_prefix"),
    (
        (
            json.dumps({"type": [], "payload": {}}, separators=(",", ":")) + "\n",
            "malformed_record:",
        ),
        (
            '{"type":' + ("9" * 4301) + ',"payload":{}}\n',
            "malformed_json:",
        ),
        (("[" * 1200) + "0" + ("]" * 1200) + "\n", "malformed_json:"),
    ),
)
def test_bad_json_value_cannot_block_a_later_complete_session(
    tmp_path,
    bad_line,
    warning_prefix,
):
    source = tmp_path / "session.jsonl"
    source.write_text(
        bad_line
        + _line(_session("recovered-session"))
        + _line(_context("recovered-turn", "/repo", "model"))
        + _line(_complete("recovered-turn", "Outcome: recovered"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("recovered-session:recovered-turn", "recovered")
    ]
    assert any(warning.startswith(warning_prefix) for warning in batch.warnings)
    assert batch.next_checkpoint.cursor["offset"] == source.stat().st_size


@pytest.mark.parametrize(
    "bad_outcome",
    ("bad-\ud800-text", "bad-\x1b]0;PMH-PWN\x07-text"),
)
def test_unsafe_completion_text_is_rejected_without_blocking_later_session(
    tmp_path,
    bad_outcome,
):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("bad-session"))
        + _line(_context("bad-turn", "/repo", "model"))
        + _line(_complete("bad-turn", f"Outcome: {bad_outcome}"))
        + _line(_session("recovered-session"))
        + _line(_context("recovered-turn", "/repo", "model"))
        + _line(_complete("recovered-turn", "Outcome: recovered"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("recovered-session:recovered-turn", "recovered")
    ]
    assert "invalid_capture_block:1" in batch.warnings
    assert batch.next_checkpoint.cursor["offset"] == source.stat().st_size


@pytest.mark.parametrize(
    "invalid_timestamp",
    ("0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"),
)
def test_overflowing_timestamp_cannot_block_a_later_complete_session(
    tmp_path,
    invalid_timestamp,
):
    bad_completion = _complete("bad-turn", "Outcome: rejected")
    bad_completion["timestamp"] = invalid_timestamp
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("bad-session"))
        + _line(_context("bad-turn", "/repo", "model"))
        + _line(bad_completion)
        + _line(_session("recovered-session"))
        + _line(_context("recovered-turn", "/repo", "model"))
        + _line(_complete("recovered-turn", "Outcome: recovered"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("recovered-session:recovered-turn", "recovered")
    ]
    assert "invalid_timestamp:1" in batch.warnings
    assert batch.next_checkpoint.cursor["offset"] == source.stat().st_size


@pytest.mark.parametrize(
    ("field", "invalid_value", "warning_prefix"),
    (
        ("cwd", "/repo/bad-\ud800", "invalid_unicode:"),
        ("cwd", "/repo/\x00tail", "malformed_context:"),
        ("summary", "bad-\ud800-summary", "invalid_unicode:"),
    ),
)
def test_invalid_context_text_cannot_block_a_later_complete_session(
    tmp_path,
    field,
    invalid_value,
    warning_prefix,
):
    bad_context = _context("bad-turn", "/repo", "model")
    bad_context["payload"][field] = invalid_value
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("bad-session"))
        + _line(bad_context)
        + _line(_complete("bad-turn", "Outcome: rejected"))
        + _line(_session("recovered-session"))
        + _line(_context("recovered-turn", "/repo", "model"))
        + _line(_complete("recovered-turn", "Outcome: recovered"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("recovered-session:recovered-turn", "recovered")
    ]
    assert any(warning.startswith(warning_prefix) for warning in batch.warnings)
    assert batch.next_checkpoint.cursor["offset"] == source.stat().st_size


def test_nul_cwd_isolated_before_ingestion_and_good_checkpoint_commits(tmp_path):
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    assert projects.find_by_cwd(Path(f"{project}\x00tail")) is None
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("bad-session"))
        + _line(_context("bad-turn", f"{project}\x00tail", "model"))
        + _line(_complete("bad-turn", "Outcome: rejected"))
        + _line(_session("recovered-session"))
        + _line(_context("recovered-turn", str(project), "model"))
        + _line(_complete("recovered-turn", "Outcome: RECOVERED_AFTER_NUL"))
    )
    checkpoints = CheckpointRepository(database)
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())

    result = IngestionService(capture, checkpoints, database, projects).ingest(
        CodexAdapter(tmp_path, Redactor()),
        "session.jsonl",
    )

    assert len(result.capture_results) == 1
    assert result.checkpoint.cursor["offset"] == source.stat().st_size
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") == result.checkpoint
    with database.connect(readonly=True) as connection:
        contents = {
            row[0]
            for row in connection.execute(
                "select normalized_content from behavior_memories"
            ).fetchall()
        }
    assert contents == {"RECOVERED_AFTER_NUL"}


def test_known_nonsemantic_codex_records_are_ignored_without_warning(tmp_path):
    source = tmp_path / "session.jsonl"
    ignored_records = (
        "response_item",
        "tool_stdout",
        "tool_stderr",
        "base_instructions",
        "world_state",
        "compacted_history",
        "compacted",
        "inter_agent_communication_metadata",
    )
    ignored_events = (
        "agent_message",
        "agent_reasoning",
        "context_compacted",
        "image_generation_end",
        "mcp_tool_call_end",
        "patch_apply_end",
        "sub_agent_activity",
        "task_started",
        "thread_settings_applied",
        "token_count",
        "user_message",
        "web_search_end",
    )
    source.write_text(
        _line(_session())
        + "".join(
            _line({"type": record_type, "payload": {"ignored": True}})
            for record_type in ignored_records
        )
        + "".join(
            _line(
                {
                    "type": "event_msg",
                    "payload": {"type": event_type, "ignored": True},
                }
            )
            for event_type in ignored_events
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert batch.warnings == ()


def test_turn_contexts_and_completions_do_not_mix(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_complete("before-context", "Outcome: ignored"))
        + _line(_context("turn-a", "/repo/a", "model-a", "task a"))
        + _line(_context("turn-b", "/repo/b", "model-b", "task b"))
        + _line(
            {
                "timestamp": "2026-07-12T00:00:02Z",
                "type": "response_item",
                "payload": {"text": "Outcome: ignored response item"},
            }
        )
        + _line(_complete("turn-b", "Outcome: result b"))
        + _line(_complete("turn-a", "Outcome: result a"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert [
        (str(record.cwd), record.namespace.model_id, record.outcome) for record in batch.records
    ] == [
        ("/repo/b", "model-b", "result b"),
        ("/repo/a", "model-a", "result a"),
    ]
    assert all("ignored" not in record.outcome for record in batch.records)


def test_new_session_metadata_discards_pending_prior_session_turns(tmp_path):
    source = tmp_path / "sessions.jsonl"
    source.write_text(
        _line(_session("session-a"))
        + _line(_context("turn-a", "/repo/a", "model-a"))
        + _line(_session("session-b"))
        + _line(_context("turn-b", "/repo/b", "model-b"))
        + _line(_complete("turn-a", "Outcome: result a"))
        + _line(_complete("turn-b", "Outcome: result b"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("sessions.jsonl", None)

    assert [record.source_record_id for record in batch.records] == [
        "session-b:turn-b",
    ]


def test_completion_is_redacted_before_ingestion_capture_spy(tmp_path):
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", str(project), "model"))
        + _line(
            _complete(
                "turn-1",
                "Outcome: stored\nRisk: password=SUPER_PRIVATE_PASSWORD",
            )
        )
    )
    adapter = CodexAdapter(tmp_path, Redactor())
    checkpoint_repository = CheckpointRepository(database)
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    seen = []

    class CaptureSpy:
        def prepare_verified(self, payload, verification):
            seen.append((payload, verification))
            return capture.prepare_verified(payload, verification)

        def capture_prepared_on_connection(self, connection, prepared):
            return capture.capture_prepared_on_connection(connection, prepared)

    IngestionService(
        CaptureSpy(),
        checkpoint_repository,
        database,
        projects,
    ).ingest(adapter, "session.jsonl")

    assert len(seen) == 1
    assert "SUPER_PRIVATE_PASSWORD" not in repr(seen)
    assert "[REDACTED:password]" in seen[0][0].risks[0]


def test_sensitive_changed_path_has_direct_and_codex_trusted_hash_parity(tmp_path):
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    namespace = Namespace(source_agent="codex", model_id="model-one")
    direct = CapturePayload(
        cwd=project,
        namespace=namespace,
        source_record_id="direct-sensitive-change",
        objective="exact objective",
        outcome="exact outcome",
        changed_paths=[".env"],
    )
    assert capture.capture(direct).status == "pending_verification"

    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn-1", str(project), "model-one"))
        + _line(
            _complete(
                "turn-1",
                _managed_capture(
                    "Objective: exact objective",
                    "Outcome: exact outcome",
                    "Changed: .env",
                ),
            )
        )
    )
    record = (
        CodexAdapter(tmp_path, Redactor())
        .read_incremental(
            "session.jsonl",
            None,
        )
        .records[0]
    )
    trusted = CapturePayload(
        cwd=record.cwd,
        namespace=record.namespace,
        source_record_id=record.source_record_id,
        objective=record.objective,
        outcome=record.outcome,
        changed_paths=list(record.changed_paths),
    )
    assert capture.capture(trusted, record.verification).status == "inserted"

    with database.connect(readonly=True) as connection:
        pending = connection.execute(
            """
            select structured_payload_json, structured_hash from pending_captures
            where source_record_id = ?
            """,
            (direct.source_record_id,),
        ).fetchone()
        trusted_hash = connection.execute(
            "select content_hash from source_refs where source_record_id = ?",
            (record.source_record_id,),
        ).fetchone()[0]
    assert json.loads(pending["structured_payload_json"])["changed_paths"] == []
    assert record.changed_paths == (".env",)
    assert pending["structured_hash"] == trusted_hash


def test_capture_failure_leaves_checkpoint_and_receipts_unchanged(tmp_path):
    checkpoint_repository = CheckpointRepository(_database(tmp_path))
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope="session.jsonl",
        cursor={"relative_path": "session.jsonl", "offset": 10},
        parser_version="codex-v1",
    )
    record = NormalizedTaskRecord(
        cwd=Path("/repo"),
        namespace=Namespace(source_agent="codex", model_id="model"),
        source_record_id="session:turn",
        objective="task",
        outcome="done",
        verification=NamespaceVerification(
            namespace=Namespace(source_agent="codex", model_id="model"),
            source_record_id="session:turn",
            verified_by="codex_adapter",
            verified_at=datetime.now(timezone.utc),
        ),
    )
    adapter = SimpleNamespace(
        source_agent=SourceAgent.CODEX,
        read_incremental=lambda _scope, _checkpoint: AdapterBatch(
            records=(record,), next_checkpoint=checkpoint
        ),
    )

    class FailingCapture:
        def prepare_verified(self, _payload, _verification):
            raise RuntimeError("capture failed")

    with pytest.raises(RuntimeError, match="capture failed"):
        IngestionService(
            FailingCapture(),
            checkpoint_repository,
            checkpoint_repository.database,
            ProjectRepository(checkpoint_repository.database),
        ).ingest(adapter, "session.jsonl")

    assert checkpoint_repository.get(SourceAgent.CODEX, "session.jsonl") is None
    with checkpoint_repository.database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0


def test_rejected_capture_does_not_advance_checkpoint(tmp_path):
    checkpoint_repository = CheckpointRepository(_database(tmp_path))
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope="session.jsonl",
        cursor={"relative_path": "session.jsonl", "offset": 10},
        parser_version="codex-v1",
    )
    record = NormalizedTaskRecord(
        cwd=Path("/repo"),
        namespace=Namespace(source_agent="codex", model_id="model"),
        source_record_id="session:turn",
        objective="task",
        outcome="done",
        verification=NamespaceVerification(
            namespace=Namespace(source_agent="codex", model_id="model"),
            source_record_id="session:turn",
            verified_by="codex_adapter",
            verified_at=datetime.now(timezone.utc),
        ),
    )
    adapter = SimpleNamespace(
        source_agent=SourceAgent.CODEX,
        read_incremental=lambda _scope, _checkpoint: AdapterBatch(
            records=(record,), next_checkpoint=checkpoint
        ),
    )
    capture = SimpleNamespace(
        prepare_verified=lambda _payload, _verification: CaptureResult(status="rejected")
    )

    with pytest.raises(IngestionError):
        IngestionService(
            capture,
            checkpoint_repository,
            checkpoint_repository.database,
            ProjectRepository(checkpoint_repository.database),
        ).ingest(adapter, "session.jsonl")
    assert checkpoint_repository.get(SourceAgent.CODEX, "session.jsonl") is None


def test_non_codex_project_not_found_remains_a_hard_failure(tmp_path: Path) -> None:
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)
    namespace = Namespace(source_agent=SourceAgent.CHATGPT, model_id="model")
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CHATGPT,
        scope="export.zip",
        cursor={"offset": 1},
        parser_version="chatgpt-v1",
    )
    record = NormalizedTaskRecord(
        cwd=tmp_path / "missing",
        namespace=namespace,
        source_record_id="conversation:turn",
        objective="task",
        outcome="done",
        verification=NamespaceVerification(
            namespace=namespace,
            source_record_id="conversation:turn",
            verified_by="chatgpt_adapter",
            verified_at=datetime.now(timezone.utc),
        ),
    )
    adapter = SimpleNamespace(
        source_agent=SourceAgent.CHATGPT,
        read_incremental=lambda _scope, _checkpoint: AdapterBatch(
            records=(record,),
            next_checkpoint=checkpoint,
        ),
    )
    capture = SimpleNamespace(
        prepare_verified=lambda _payload, _verification: CaptureResult(status="project_not_found")
    )

    with pytest.raises(IngestionError, match="capture preparation rejected"):
        IngestionService(
            capture,
            checkpoints,
            database,
            ProjectRepository(database),
        ).ingest(adapter, "export.zip")

    assert _ingestion_counts(database)["codex_deferred_records"] == 0
    assert checkpoints.get(SourceAgent.CHATGPT, "export.zip") is None


@pytest.mark.parametrize(
    "mismatch",
    (
        "verification_model",
        "verification_agent",
        "verification_record_id",
        "verification_actor",
        "verification_naive_time",
        "unsafe_source_record_id",
        "unsafe_model_id",
    ),
)
def test_missing_project_requires_strict_codex_verification_before_defer(
    tmp_path: Path,
    mismatch: str,
) -> None:
    source_record_id = (
        "session:/private/path" if mismatch == "unsafe_source_record_id" else "session:turn"
    )
    model_id = "provider.example/model" if mismatch == "unsafe_model_id" else "model-one"
    record = _ingestion_record(
        tmp_path / "missing",
        source_record_id,
        model_id=model_id,
    )
    verification = record.verification
    if mismatch == "verification_model":
        verification = verification.model_copy(
            update={"namespace": Namespace(source_agent=SourceAgent.CODEX, model_id="other-model")}
        )
    elif mismatch == "verification_agent":
        verification = verification.model_copy(
            update={
                "namespace": Namespace(
                    source_agent=SourceAgent.CHATGPT,
                    model_id=model_id,
                )
            }
        )
    elif mismatch == "verification_record_id":
        verification = verification.model_copy(update={"source_record_id": "other:turn"})
    elif mismatch == "verification_actor":
        verification = verification.model_copy(update={"verified_by": "chatgpt_adapter"})
    elif mismatch == "verification_naive_time":
        verification = verification.model_copy(update={"verified_at": datetime(2026, 7, 17, 0, 0)})
    record = record.model_copy(update={"verification": verification})
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)

    with pytest.raises(IngestionError, match="capture preparation rejected"):
        IngestionService(
            CaptureService(
                database,
                ProjectRepository(database),
                MemoryRepository(database),
                Redactor(),
            ),
            checkpoints,
            database,
            ProjectRepository(database),
        ).ingest(
            _ingestion_adapter((record,), _deferred_checkpoint(300)),
            "session.jsonl",
        )

    assert _ingestion_counts(database)["codex_deferred_records"] == 0
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None


@pytest.mark.parametrize(
    "cwd",
    (
        Path("relative/project"),
        Path("/tmp/project/../missing"),
        Path("/tmp/project\x00tail"),
        Path("/" + "a" * 4097),
    ),
)
def test_missing_project_requires_valid_codex_cwd_before_defer(
    tmp_path: Path,
    cwd: Path,
) -> None:
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)

    with pytest.raises(IngestionError, match="capture preparation rejected"):
        IngestionService(
            CaptureService(
                database,
                ProjectRepository(database),
                MemoryRepository(database),
                Redactor(),
            ),
            checkpoints,
            database,
            ProjectRepository(database),
        ).ingest(
            _ingestion_adapter(
                (_ingestion_record(cwd, "session:turn"),),
                _deferred_checkpoint(300),
            ),
            "session.jsonl",
        )

    assert _ingestion_counts(database)["codex_deferred_records"] == 0
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None


def test_missing_project_is_deferred_without_starving_later_record(tmp_path: Path) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    missing = tmp_path / "deleted-project"
    missing_marker = "MISSING_PRIVATE_OBJECTIVE"
    missing_outcome = "MISSING_PRIVATE_OUTCOME"
    missing_record = _ingestion_record(
        missing,
        "session:missing",
        objective=missing_marker,
        outcome=missing_outcome,
        changed_paths=("private/changed.py",),
        resolved_open_issues=("old issue must not be resolved",),
    )
    accepted_record = _ingestion_record(project, "session:accepted")
    checkpoint = _deferred_checkpoint(300)
    checkpoints = CheckpointRepository(database)

    result = IngestionService(
        CaptureService(database, projects, MemoryRepository(database), Redactor()),
        checkpoints,
        database,
        projects,
    ).ingest(
        _ingestion_adapter((missing_record, accepted_record), checkpoint),
        "session.jsonl",
    )

    assert result.deferred_count == 1
    assert result.warning_count == 1
    assert len(result.capture_results) == 1
    assert result.capture_results[0].status == "inserted"
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") == checkpoint
    with database.connect(readonly=True) as connection:
        counts = {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "behavior_memories",
                "source_refs",
                "memory_issue_resolutions",
                "pending_captures",
                "pending_capture_history",
                "import_receipts",
                "codex_deferred_records",
            )
        }
        deferred = connection.execute("select * from codex_deferred_records").fetchone()
        receipts = tuple(
            row[0]
            for row in connection.execute(
                "select source_record_id from import_receipts order by source_record_id"
            ).fetchall()
        )
    assert counts == {
        "behavior_memories": 1,
        "source_refs": 1,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 1,
        "codex_deferred_records": 1,
    }
    assert receipts == ("session:accepted",)
    assert deferred is not None
    assert deferred["source_record_id"] == "session:missing"
    assert deferred["prefix_length"] == 300
    assert deferred["prefix_sha256"] == "a" * 64
    assert deferred["parser_policy_sha256"] == "c" * 64
    serialized = "|".join(str(value) for value in deferred)
    assert str(missing) not in serialized
    assert missing_marker not in serialized
    assert missing_outcome not in serialized
    assert "private/changed.py" not in serialized


def test_deferred_batch_rolls_back_capture_receipt_and_checkpoint_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    real_commit = checkpoints.commit_on_connection

    def fail_after_checkpoint(*args, **kwargs):
        real_commit(*args, **kwargs)
        raise sqlite3.IntegrityError("injected deferred checkpoint failure")

    monkeypatch.setattr(checkpoints, "commit_on_connection", fail_after_checkpoint)

    with pytest.raises(sqlite3.IntegrityError, match="injected deferred checkpoint failure"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter(
                (
                    _ingestion_record(tmp_path / "missing", "session:missing"),
                    _ingestion_record(project, "session:accepted"),
                ),
                _deferred_checkpoint(300),
            ),
            "session.jsonl",
        )

    assert _ingestion_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "checkpoints": 0,
        "codex_deferred_records": 0,
    }


def test_missing_project_registered_after_prepare_requires_reconcile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    missing = tmp_path / "missing"
    checkpoints = CheckpointRepository(database)
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    real_prepare = capture.prepare_verified

    def register_after_missing(payload, verification):
        result = real_prepare(payload, verification)
        if isinstance(result, CaptureResult) and result.status == "project_not_found":
            missing.mkdir()
            projects.register(
                ProjectCandidate(canonical_path=missing, display_name="registered concurrently")
            )
        return result

    monkeypatch.setattr(capture, "prepare_verified", register_after_missing)

    with pytest.raises(ReconcileRequiredError, match="project registry requires reconcile"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter(
                (_ingestion_record(missing, "session:missing"),),
                _deferred_checkpoint(300),
            ),
            "session.jsonl",
        )

    assert _ingestion_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "checkpoints": 0,
        "codex_deferred_records": 0,
    }


@pytest.mark.parametrize(
    "cursor_patch",
    (
        {"device": True},
        {"device": 2**63},
        {"inode": -1},
        {"prefix_length": 299},
        {"observed_size": -1},
        {"observed_size": 2**63, "offset": 2**63, "prefix_length": 2**63},
        {"prefix_sha256": "A" * 64},
        {"relative_path": "other.jsonl"},
    ),
)
def test_missing_project_with_invalid_locator_fails_closed(
    tmp_path: Path,
    cursor_patch: dict[str, object],
) -> None:
    database = _database(tmp_path)
    checkpoint = _deferred_checkpoint(300)
    checkpoint.cursor.update(cursor_patch)  # type: ignore[arg-type]
    checkpoints = CheckpointRepository(database)

    with pytest.raises(IngestionError, match="deferred locator"):
        IngestionService(
            CaptureService(
                database,
                ProjectRepository(database),
                MemoryRepository(database),
                Redactor(),
            ),
            checkpoints,
            database,
            ProjectRepository(database),
        ).ingest(
            _ingestion_adapter(
                (_ingestion_record(tmp_path / "missing", "session:missing"),),
                checkpoint,
            ),
            "session.jsonl",
        )

    assert _ingestion_counts(database)["codex_deferred_records"] == 0
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None


def test_missing_project_with_unknown_parser_version_fails_closed(tmp_path: Path) -> None:
    database = _database(tmp_path)
    checkpoint = _deferred_checkpoint(300).model_copy(update={"parser_version": "codex-v2"})
    checkpoints = CheckpointRepository(database)

    with pytest.raises(IngestionError, match="deferred locator"):
        IngestionService(
            CaptureService(
                database,
                ProjectRepository(database),
                MemoryRepository(database),
                Redactor(),
            ),
            checkpoints,
            database,
            ProjectRepository(database),
        ).ingest(
            _ingestion_adapter(
                (_ingestion_record(tmp_path / "missing", "session:missing"),),
                checkpoint,
            ),
            "session.jsonl",
        )

    assert _ingestion_counts(database)["codex_deferred_records"] == 0
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None


@pytest.mark.parametrize(
    "scope",
    (
        "../secret.jsonl",
        "/absolute/session.jsonl",
        "sessions\\session.jsonl",
        "session.txt",
    ),
)
def test_missing_project_with_invalid_codex_scope_fails_closed(
    tmp_path: Path,
    scope: str,
) -> None:
    database = _database(tmp_path)
    checkpoint = _deferred_checkpoint(300, scope=scope)
    checkpoints = CheckpointRepository(database)

    with pytest.raises(IngestionError, match="deferred locator"):
        IngestionService(
            CaptureService(
                database,
                ProjectRepository(database),
                MemoryRepository(database),
                Redactor(),
            ),
            checkpoints,
            database,
            ProjectRepository(database),
        ).ingest(
            _ingestion_adapter(
                (_ingestion_record(tmp_path / "missing", "session:missing"),),
                checkpoint,
            ),
            scope,
        )

    assert _ingestion_counts(database)["codex_deferred_records"] == 0
    assert checkpoints.get(SourceAgent.CODEX, scope) is None


def test_duplicate_batch_source_record_id_fails_before_defer(tmp_path: Path) -> None:
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)
    record = _ingestion_record(tmp_path / "missing", "session:duplicate")

    with pytest.raises(IngestionError, match="duplicate adapter source record"):
        IngestionService(
            CaptureService(
                database,
                ProjectRepository(database),
                MemoryRepository(database),
                Redactor(),
            ),
            checkpoints,
            database,
            ProjectRepository(database),
        ).ingest(
            _ingestion_adapter((record, record), _deferred_checkpoint(300)),
            "session.jsonl",
        )

    assert _ingestion_counts(database)["codex_deferred_records"] == 0
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None


def test_deferred_locator_keeps_first_anchor_on_idempotent_replay(tmp_path: Path) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    checkpoints = CheckpointRepository(database)
    ingestion = IngestionService(
        CaptureService(database, projects, MemoryRepository(database), Redactor()),
        checkpoints,
        database,
        projects,
    )
    record = _ingestion_record(tmp_path / "missing", "session:missing")

    ingestion.ingest(
        _ingestion_adapter((record,), _deferred_checkpoint(300, prefix_sha256="a" * 64)),
        "session.jsonl",
    )
    ingestion.ingest(
        _ingestion_adapter((record,), _deferred_checkpoint(500, prefix_sha256="b" * 64)),
        "session.jsonl",
    )

    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            "select prefix_length, prefix_sha256 from codex_deferred_records"
        ).fetchall()
    assert [(row["prefix_length"], row["prefix_sha256"]) for row in rows] == [(300, "a" * 64)]


def test_deferred_capacity_exceeded_rolls_back_the_batch(tmp_path: Path) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    checkpoints = CheckpointRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    records = (
        _ingestion_record(project, "session:accepted"),
        *(
            _ingestion_record(tmp_path / "missing", f"session:missing-{index}")
            for index in range(257)
        ),
    )

    with pytest.raises(IngestionError, match="deferred capacity exceeded"):
        IngestionService(
            CaptureService(database, projects, MemoryRepository(database), Redactor()),
            checkpoints,
            database,
            projects,
        ).ingest(
            _ingestion_adapter(records, _deferred_checkpoint(300)),
            "session.jsonl",
        )

    assert _ingestion_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "checkpoints": 0,
        "codex_deferred_records": 0,
    }
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None


def test_deferred_global_capacity_exceeded_rolls_back_one_new_record(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    timestamp = "2026-07-17T00:00:00Z"
    rows = tuple(
        (
            f"{index:08x}-0000-4000-8000-{index:012x}",
            SourceAgent.CODEX.value,
            f"seed/{index // 250}.jsonl",
            f"seed:{index}",
            "codex-v3",
            "c" * 64,
            1,
            index // 250,
            100,
            "a" * 64,
            "project_not_found",
            "pending",
            timestamp,
            timestamp,
            1,
            "project_not_found",
            None,
        )
        for index in range(10_000)
    )
    with database.transaction() as connection:
        connection.executemany(
            """
            insert into codex_deferred_records(
                deferred_id, source_agent, scope, source_record_id,
                parser_version, parser_policy_sha256, source_device, source_inode,
                prefix_length, prefix_sha256, reason_code, state,
                first_seen_at, last_attempt_at, attempt_count,
                last_error_code, recovered_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )

    projects = ProjectRepository(database)
    checkpoints = CheckpointRepository(database)
    scope = "overflow.jsonl"
    with pytest.raises(IngestionError, match="deferred capacity exceeded"):
        IngestionService(
            CaptureService(database, projects, MemoryRepository(database), Redactor()),
            checkpoints,
            database,
            projects,
        ).ingest(
            _ingestion_adapter(
                (_ingestion_record(tmp_path / "missing", "overflow:missing"),),
                _deferred_checkpoint(300, scope=scope),
            ),
            scope,
        )

    assert _ingestion_counts(database)["codex_deferred_records"] == 10_000
    assert checkpoints.get(SourceAgent.CODEX, scope) is None


def test_lifecycle_update_before_checkpoint_failure_rolls_back_the_whole_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    memories = MemoryRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    registered = projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = [datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)]
    capture = CaptureService(
        database,
        projects,
        memories,
        Redactor(),
        now=lambda: clock[0],
    )
    target_record = _ingestion_record(
        project,
        "seed:target",
        objective="",
        outcome="",
        open_issues=("exact old issue",),
        verified_at=clock[0],
    )
    target = capture.capture(
        CapturePayload(
            cwd=target_record.cwd,
            namespace=target_record.namespace,
            source_record_id=target_record.source_record_id,
            objective=target_record.objective,
            outcome=target_record.outcome,
            open_issues=list(target_record.open_issues),
        ),
        target_record.verification,
    )
    assert target.status == "inserted"
    target_id = target.inserted_ids[0]
    clock[0] += timedelta(seconds=2)
    record = _ingestion_record(
        project,
        "session:resolve",
        resolved_open_issues=("exact old issue",),
        verified_at=clock[0] - timedelta(seconds=1),
    )
    pending_payload = CapturePayload(
        cwd=record.cwd,
        namespace=record.namespace,
        source_record_id=record.source_record_id,
        objective=record.objective,
        outcome=record.outcome,
        resolved_open_issues=list(record.resolved_open_issues),
    )
    assert capture.capture(pending_payload).status == "pending_verification"
    before = _ingestion_counts(database)
    with database.connect(readonly=True) as connection:
        observed_before = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(registered.project_id).lower(),),
        ).fetchone()[0]
    checkpoint = _ingestion_checkpoint(10)
    adapter = _ingestion_adapter((record,), checkpoint)
    checkpoints = CheckpointRepository(database)

    def fail_checkpoint(*_args, **_kwargs):
        raise sqlite3.IntegrityError("injected checkpoint failure")

    monkeypatch.setattr(
        CheckpointRepository,
        "commit_on_connection",
        fail_checkpoint,
        raising=False,
    )

    with pytest.raises(sqlite3.IntegrityError, match="injected checkpoint failure"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            adapter,
            "session.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before
    with database.connect(readonly=True) as connection:
        target_state = connection.execute(
            "select lifecycle_state from behavior_memories where memory_id = ?",
            (str(target_id).lower(),),
        ).fetchone()[0]
        pending_state = connection.execute(
            "select verification_state from pending_captures"
        ).fetchone()[0]
        observed_after = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(registered.project_id).lower(),),
        ).fetchone()[0]
    assert target_state == "active"
    assert pending_state == "pending"
    assert observed_after == observed_before


def test_checkpoint_cas_conflict_after_adapter_read_preserves_competing_value(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    original = _ingestion_checkpoint(10)
    competing = _ingestion_checkpoint(20)
    desired = _ingestion_checkpoint(30)
    checkpoints.commit(SourceAgent.CODEX, "session.jsonl", original)
    record = _ingestion_record(project, "session:cas-conflict")
    before = _ingestion_counts(database)

    class RacingAdapter:
        source_agent = SourceAgent.CODEX

        def read_incremental(self, _scope, checkpoint):
            assert checkpoint == original
            checkpoints.commit(SourceAgent.CODEX, "session.jsonl", competing)
            return AdapterBatch(records=(record,), next_checkpoint=desired)

    with pytest.raises(RuntimeError, match="checkpoint conflict") as raised:
        IngestionService(
            capture,
            checkpoints,
            database,
            projects,
        ).ingest(RacingAdapter(), "session.jsonl")

    assert raised.type.__name__ == "CheckpointConflictError"
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") == competing
    assert _ingestion_counts(database) == before


def test_checkpoint_cas_rejects_raw_boolean_cursor_before_any_batch_write(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    original = _ingestion_checkpoint(1)
    desired = _ingestion_checkpoint(2)
    checkpoints.commit(SourceAgent.CODEX, "session.jsonl", original)
    record = _ingestion_record(project, "session:raw-boolean-cursor")
    before = _ingestion_counts(database)

    class RawCursorAdapter:
        source_agent = SourceAgent.CODEX

        def read_incremental(self, _scope, checkpoint):
            assert checkpoint == original
            with database.transaction() as connection:
                connection.execute(
                    """
                    update checkpoints set cursor_json = ?
                    where adapter = ? and scope = ?
                    """,
                    (
                        json.dumps(
                            {"relative_path": "session.jsonl", "offset": True},
                            separators=(",", ":"),
                        ),
                        SourceAgent.CODEX.value,
                        "session.jsonl",
                    ),
                )
            return AdapterBatch(records=(record,), next_checkpoint=desired)

    with pytest.raises(CheckpointConflictError, match="checkpoint conflict"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            RawCursorAdapter(),
            "session.jsonl",
        )

    assert _ingestion_counts(database) == before
    with database.connect(readonly=True) as connection:
        raw_cursor = json.loads(
            connection.execute(
                """
                select cursor_json from checkpoints
                where adapter = ? and scope = ?
                """,
                (SourceAgent.CODEX.value, "session.jsonl"),
            ).fetchone()[0]
        )
    assert raw_cursor["offset"] is True


@pytest.mark.parametrize("invalid_value", (True, 1.0, None, [], {}))
def test_checkpoint_read_rejects_non_string_or_exact_integer_cursor_values(
    tmp_path: Path,
    invalid_value: object,
) -> None:
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)
    with database.transaction() as connection:
        connection.execute(
            """
            insert into checkpoints(adapter, scope, cursor_json, parser_version, updated_at)
            values (?, ?, ?, ?, ?)
            """,
            (
                SourceAgent.CODEX.value,
                "session.jsonl",
                json.dumps({"offset": invalid_value}, separators=(",", ":")),
                "codex-v3",
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    with pytest.raises(ValueError, match="checkpoint cursor values must be strings or integers"):
        checkpoints.get(SourceAgent.CODEX, "session.jsonl")


def test_codex_ingestion_rejects_chatgpt_namespace_before_any_write(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    checkpoints = CheckpointRepository(database)
    namespace = Namespace(source_agent=SourceAgent.CHATGPT, model_id="exact-chatgpt-model")
    source_record_id = "conversation:wrong-adapter"
    record = NormalizedTaskRecord(
        cwd=project,
        namespace=namespace,
        source_record_id=source_record_id,
        objective="exact task",
        outcome="exact outcome",
        verification=NamespaceVerification(
            namespace=namespace,
            source_record_id=source_record_id,
            verified_by="chatgpt_adapter",
            verified_at=datetime.now(timezone.utc),
        ),
    )
    adapter = _ingestion_adapter((record,), _ingestion_checkpoint(10))
    before = _ingestion_counts(database)

    with pytest.raises(IngestionError, match="adapter source namespace mismatch"):
        IngestionService(
            CaptureService(database, projects, MemoryRepository(database), Redactor()),
            checkpoints,
            database,
            projects,
        ).ingest(adapter, "session.jsonl")

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before


def test_receipt_insert_failure_rolls_back_capture_and_checkpoint(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    record = _ingestion_record(project, "session:receipt-failure")
    checkpoint = _ingestion_checkpoint(10)
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger fail_codex_receipt
            before insert on import_receipts
            begin
                select raise(abort, 'injected receipt failure');
            end
            """
        )
    before = _ingestion_counts(database)

    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter((record,), checkpoint),
            "session.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before


def test_connection_scoped_checkpoint_commit_obeys_outer_rollback(tmp_path: Path) -> None:
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)
    checkpoint = _ingestion_checkpoint(10)

    class SentinelRollback(RuntimeError):
        pass

    with pytest.raises(SentinelRollback):
        with database.transaction() as connection:
            checkpoints.commit_on_connection(
                connection,
                SourceAgent.CODEX,
                "session.jsonl",
                expected_checkpoint=None,
                next_checkpoint=checkpoint,
                source_record_ids=("session:turn",),
            )
            raise SentinelRollback

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database)["import_receipts"] == 0


def test_public_import_receipt_replay_is_idempotent_but_scoped_insert_is_strict(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)
    source_hash = "d" * 64
    source_record_id = "conversation:strict-receipt"

    checkpoints.commit_import_receipt(
        source_hash,
        source_record_id,
        SourceAgent.CHATGPT,
    )
    checkpoints.commit_import_receipt(
        source_hash,
        source_record_id,
        SourceAgent.CHATGPT,
    )

    with database.transaction() as connection:
        with pytest.raises(CheckpointConflictError, match="checkpoint conflict"):
            checkpoints.commit_import_receipt_on_connection(
                connection,
                source_hash,
                source_record_id,
                SourceAgent.CHATGPT,
            )
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 1


def test_connection_scoped_import_receipt_preserves_trigger_integrity_error(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    checkpoints = CheckpointRepository(database)
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger fail_strict_import_receipt
            before insert on import_receipts
            begin
                select raise(abort, 'injected strict receipt failure');
            end
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected strict receipt failure"):
        with database.transaction() as connection:
            checkpoints.commit_import_receipt_on_connection(
                connection,
                "e" * 64,
                "conversation:trigger-failure",
                SourceAgent.CHATGPT,
            )

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0


def test_multi_project_final_guard_rolls_back_when_earlier_path_is_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    first_project = tmp_path / "first-project"
    second_project = tmp_path / "second-project"
    first_project.mkdir()
    second_project.mkdir()
    projects.register(ProjectCandidate(canonical_path=first_project, display_name="first"))
    projects.register(ProjectCandidate(canonical_path=second_project, display_name="second"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    records = (
        _ingestion_record(first_project, "session:first"),
        _ingestion_record(second_project, "session:second"),
    )
    checkpoint = _ingestion_checkpoint(20)
    before = _ingestion_counts(database)
    real_commit = getattr(CheckpointRepository, "commit_on_connection", None)

    def replace_earlier_project(
        self,
        connection,
        adapter,
        scope,
        *,
        expected_checkpoint,
        next_checkpoint,
        source_record_ids=(),
    ):
        assert real_commit is not None
        real_commit(
            self,
            connection,
            adapter,
            scope,
            expected_checkpoint=expected_checkpoint,
            next_checkpoint=next_checkpoint,
            source_record_ids=source_record_ids,
        )
        first_project.rename(tmp_path / "first-project-replaced")
        first_project.mkdir()

    monkeypatch.setattr(
        CheckpointRepository,
        "commit_on_connection",
        replace_earlier_project,
        raising=False,
    )

    try:
        with pytest.raises(ReconcileRequiredError, match="project registry requires reconcile"):
            IngestionService(capture, checkpoints, database, projects).ingest(
                _ingestion_adapter(records, checkpoint),
                "session.jsonl",
            )
    finally:
        first_project.rmdir()
        (tmp_path / "first-project-replaced").rename(first_project)

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before


def test_project_drift_between_batch_guard_and_capture_is_reconcile_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    replacement = tmp_path / "project-replaced"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    record = _ingestion_record(project, "session:guard-capture-drift")
    before = _ingestion_counts(database)
    real_capture = capture.capture_prepared_on_connection

    def replace_after_guard(connection, prepared):
        project.rename(replacement)
        project.mkdir()
        return real_capture(connection, prepared)

    monkeypatch.setattr(
        capture,
        "capture_prepared_on_connection",
        replace_after_guard,
    )

    try:
        with pytest.raises(ReconcileRequiredError, match="project registry requires reconcile"):
            IngestionService(capture, checkpoints, database, projects).ingest(
                _ingestion_adapter((record,), _ingestion_checkpoint(10)),
                "session.jsonl",
            )
    finally:
        project.rmdir()
        replacement.rename(project)

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before


def test_darwin_device_drift_after_last_capture_rolls_back_before_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    record = _ingestion_record(project, "session:device-drift-before-checkpoint")
    checkpoint = _ingestion_checkpoint(10)
    before = _ingestion_counts(database)
    real_capture = capture.capture_prepared_on_connection
    real_identity = projects_module.complete_directory_identity
    state = {"drifted": False}

    def drifted_identity(path: Path):
        identity = real_identity(path)
        if identity is None or not state["drifted"]:
            return identity
        return (identity[0] + 1, identity[1])

    def drift_after_capture(connection, prepared):
        result = real_capture(connection, prepared)
        state["drifted"] = True
        return result

    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        projects_module,
        "complete_directory_identity",
        drifted_identity,
    )
    monkeypatch.setattr(
        capture,
        "capture_prepared_on_connection",
        drift_after_capture,
    )

    with pytest.raises(ReconcileRequiredError, match="project registry requires reconcile"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter((record,), checkpoint),
            "session.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before


def test_darwin_device_drift_after_checkpoint_sql_rolls_back_the_batch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    record = _ingestion_record(project, "session:device-drift-after-checkpoint")
    checkpoint = _ingestion_checkpoint(10)
    before = _ingestion_counts(database)
    real_commit = CheckpointRepository.commit_on_connection
    real_identity = projects_module.complete_directory_identity
    state = {"drifted": False}

    def drifted_identity(path: Path):
        identity = real_identity(path)
        if identity is None or not state["drifted"]:
            return identity
        return (identity[0] + 1, identity[1])

    def drift_after_checkpoint(
        self,
        connection,
        adapter,
        scope,
        *,
        expected_checkpoint,
        next_checkpoint,
        source_record_ids=(),
    ):
        real_commit(
            self,
            connection,
            adapter,
            scope,
            expected_checkpoint=expected_checkpoint,
            next_checkpoint=next_checkpoint,
            source_record_ids=source_record_ids,
        )
        state["drifted"] = True

    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        projects_module,
        "complete_directory_identity",
        drifted_identity,
    )
    monkeypatch.setattr(
        CheckpointRepository,
        "commit_on_connection",
        drift_after_checkpoint,
    )

    with pytest.raises(ReconcileRequiredError, match="project registry requires reconcile"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter((record,), checkpoint),
            "session.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before


def test_generation_drift_after_checkpoint_sql_rolls_back_every_batch_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    registered = projects.register(
        ProjectCandidate(canonical_path=project, display_name="stable-name")
    )
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    record = _ingestion_record(project, "session:generation-drift")
    checkpoint = _ingestion_checkpoint(10)
    before = _ingestion_counts(database)
    with database.connect(readonly=True) as connection:
        generation_before = connection.execute(
            "select generation from project_registry_state where singleton = 1"
        ).fetchone()[0]
    real_commit = getattr(CheckpointRepository, "commit_on_connection", None)

    def mutate_registry(
        self,
        connection,
        adapter,
        scope,
        *,
        expected_checkpoint,
        next_checkpoint,
        source_record_ids=(),
    ):
        assert real_commit is not None
        real_commit(
            self,
            connection,
            adapter,
            scope,
            expected_checkpoint=expected_checkpoint,
            next_checkpoint=next_checkpoint,
            source_record_ids=source_record_ids,
        )
        connection.execute(
            "update projects set display_name = 'drifted-name' where project_id = ?",
            (str(registered.project_id).lower(),),
        )

    monkeypatch.setattr(
        CheckpointRepository,
        "commit_on_connection",
        mutate_registry,
        raising=False,
    )

    with pytest.raises(ReconcileRequiredError, match="project registry requires reconcile"):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter((record,), checkpoint),
            "session.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before
    with database.connect(readonly=True) as connection:
        project_row = connection.execute(
            "select display_name from projects where project_id = ?",
            (str(registered.project_id).lower(),),
        ).fetchone()
        generation_after = connection.execute(
            "select generation from project_registry_state where singleton = 1"
        ).fetchone()[0]
    assert project_row["display_name"] == "stable-name"
    assert generation_after == generation_before


def test_committed_batch_replay_is_duplicate_with_zero_resolution_counts(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = [datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)]
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock[0],
    )
    target = _ingestion_record(
        project,
        "seed:replay-target",
        objective="",
        outcome="",
        open_issues=("exact old issue",),
        verified_at=clock[0],
    )
    seeded = capture.capture(
        CapturePayload(
            cwd=project,
            namespace=target.namespace,
            source_record_id=target.source_record_id,
            objective="",
            outcome="",
            open_issues=list(target.open_issues),
        ),
        target.verification,
    )
    assert seeded.status == "inserted"
    clock[0] += timedelta(seconds=2)
    record = _ingestion_record(
        project,
        "session:replayed-resolution",
        resolved_open_issues=("exact old issue",),
        verified_at=clock[0] - timedelta(seconds=1),
    )
    checkpoint = _ingestion_checkpoint(10)
    adapter = _ingestion_adapter((record,), checkpoint)
    checkpoints = CheckpointRepository(database)
    ingestion = IngestionService(capture, checkpoints, database, projects)

    first = ingestion.ingest(adapter, "session.jsonl")
    receipt_count = _ingestion_counts(database)["import_receipts"]
    replay = ingestion.ingest(adapter, "session.jsonl")

    assert first.resolved_count == 1
    assert replay.capture_results[0].status == "duplicate"
    assert replay.capture_results[0].duplicate is True
    assert (
        replay.resolved_count,
        replay.already_resolved_count,
        replay.unmatched_resolution_count,
    ) == (0, 0, 0)
    assert _ingestion_counts(database)["import_receipts"] == receipt_count == 1
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") == checkpoint


def test_multiple_prior_receipts_allow_later_cross_scope_replay_without_capture_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    registered = projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    now = [datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)]
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: now[0],
    )
    seed = _ingestion_record(
        project,
        "seed:cross-scope-target",
        objective="",
        outcome="",
        open_issues=("exact old issue",),
        verified_at=now[0] - timedelta(seconds=2),
    )
    seeded = capture.capture(
        _capture_payload_from_record(seed),
        seed.verification,
    )
    assert seeded.status == "inserted"
    target_id = seeded.inserted_ids[0]
    record = _ingestion_record(
        project,
        "session:cross-scope-resolution",
        resolved_open_issues=("exact old issue",),
        verified_at=now[0],
    )
    checkpoints = CheckpointRepository(database)
    ingestion = IngestionService(capture, checkpoints, database, projects)
    first_checkpoint = _ingestion_checkpoint(10, scope="original.jsonl")
    first = ingestion.ingest(
        _ingestion_adapter((record,), first_checkpoint),
        "original.jsonl",
    )
    assert first.resolved_count == 1
    checkpoints.commit_import_receipt(
        "f" * 64,
        record.source_record_id,
        SourceAgent.CODEX,
    )
    unverified_replay = capture.capture(_capture_payload_from_record(record))
    assert unverified_replay.status == "pending_verification"
    assert unverified_replay.duplicate is False
    before = _ingestion_counts(database)
    with database.connect(readonly=True) as connection:
        observed_before = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(registered.project_id).lower(),),
        ).fetchone()[0]

    resolution_calls = 0

    def reject_resolution_replay(*_args, **_kwargs):
        nonlocal resolution_calls
        resolution_calls += 1
        raise AssertionError("duplicate replay called the resolution repository")

    monkeypatch.setattr(
        capture._issue_resolutions,
        "apply_on_connection",
        reject_resolution_replay,
    )
    later_verification = record.verification.model_copy(
        update={"verified_at": now[0] + timedelta(seconds=1)}
    )
    later_record = record.model_copy(update={"verification": later_verification})
    replay_checkpoint = _ingestion_checkpoint(20, scope="copy.jsonl")

    replay = ingestion.ingest(
        _ingestion_adapter((later_record,), replay_checkpoint),
        "copy.jsonl",
    )

    assert replay.capture_results[0].status == "duplicate"
    assert replay.capture_results[0].duplicate is True
    assert (
        replay.resolved_count,
        replay.already_resolved_count,
        replay.unmatched_resolution_count,
    ) == (0, 0, 0)
    assert resolution_calls == 0
    assert checkpoints.get(SourceAgent.CODEX, "copy.jsonl") == replay_checkpoint
    after = _ingestion_counts(database)
    assert after == {
        **before,
        "import_receipts": before["import_receipts"] + 1,
        "checkpoints": before["checkpoints"] + 1,
    }
    with database.connect(readonly=True) as connection:
        target_state = connection.execute(
            "select lifecycle_state from behavior_memories where memory_id = ?",
            (str(target_id).lower(),),
        ).fetchone()[0]
        pending_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
        observed_after = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(registered.project_id).lower(),),
        ).fetchone()[0]
    assert target_state == "archived"
    assert pending_count == 1
    assert observed_after == observed_before


def test_unreceipted_source_timestamp_mismatch_stays_strict_for_codex_ingestion(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    original = _ingestion_record(
        project,
        "session:unreceipted-strict",
        verified_at=datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc),
    )
    assert (
        capture.capture(
            _capture_payload_from_record(original),
            original.verification,
        ).status
        == "inserted"
    )
    later = original.model_copy(
        update={
            "verification": original.verification.model_copy(
                update={"verified_at": original.verification.verified_at + timedelta(seconds=1)}
            )
        }
    )
    before = _ingestion_counts(database)

    with pytest.raises(RuntimeError):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter(
                (later,),
                _ingestion_checkpoint(10, scope="copy.jsonl"),
            ),
            "copy.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "copy.jsonl") is None
    assert _ingestion_counts(database) == before


def test_prior_receipt_allows_identical_codex_duplicate_to_move_backwards(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    verified_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: verified_at,
    )
    checkpoints = CheckpointRepository(database)
    ingestion = IngestionService(capture, checkpoints, database, projects)
    original = _ingestion_record(
        project,
        "session:backwards-timestamp",
        verified_at=verified_at,
    )
    ingestion.ingest(
        _ingestion_adapter(
            (original,),
            _ingestion_checkpoint(10, scope="original.jsonl"),
        ),
        "original.jsonl",
    )
    pending = _capture_payload_from_record(original).model_copy(
        update={"source_record_id": "thread:backwards-pending"}
    )
    assert capture.capture(pending).status == "pending_verification"
    with database.connect(readonly=True) as connection:
        original_source = connection.execute(
            """
            select source_timestamp, capture_correlation_id from source_refs
            where source_record_id = ?
            """,
            (original.source_record_id,),
        ).fetchone()
    earlier = original.model_copy(
        update={
            "verification": original.verification.model_copy(
                update={"verified_at": original.verification.verified_at - timedelta(seconds=1)}
            )
        }
    )
    before = _ingestion_counts(database)
    copy_checkpoint = _ingestion_checkpoint(20, scope="copy.jsonl")

    replay = ingestion.ingest(
        _ingestion_adapter((earlier,), copy_checkpoint),
        "copy.jsonl",
    )

    assert replay.capture_results[0].status == "duplicate"
    assert replay.capture_results[0].duplicate is True
    assert checkpoints.get(SourceAgent.CODEX, "copy.jsonl") == copy_checkpoint
    assert _ingestion_counts(database) == {
        **before,
        "import_receipts": before["import_receipts"] + 1,
        "checkpoints": before["checkpoints"] + 1,
    }
    with database.connect(readonly=True) as connection:
        state = connection.execute(
            "select verification_state from pending_captures where source_record_id = ?",
            (pending.source_record_id,),
        ).fetchone()[0]
        replayed_source = connection.execute(
            """
            select source_timestamp, capture_correlation_id from source_refs
            where source_record_id = ?
            """,
            (original.source_record_id,),
        ).fetchone()
    assert state == "pending"
    assert tuple(replayed_source) == tuple(original_source)


def test_prior_receipt_without_source_ref_fails_closed_without_checkpoint_or_new_receipt(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    source_record_id = "session:receipt-without-source"
    checkpoints.commit_import_receipt(
        "f" * 64,
        source_record_id,
        SourceAgent.CODEX,
    )
    record = _ingestion_record(project, source_record_id)
    before = _ingestion_counts(database)

    with pytest.raises(RuntimeError):
        IngestionService(capture, checkpoints, database, projects).ingest(
            _ingestion_adapter((record,), _ingestion_checkpoint(10)),
            "session.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") is None
    assert _ingestion_counts(database) == before


@pytest.mark.parametrize("reuse_scope", ("project", "model", "content"))
def test_multiple_prior_receipts_do_not_relax_project_model_or_content_provenance(
    tmp_path: Path,
    reuse_scope: str,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    first_project = tmp_path / "first-project"
    first_project.mkdir()
    projects.register(ProjectCandidate(canonical_path=first_project, display_name="first"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    ingestion = IngestionService(capture, checkpoints, database, projects)
    first_record = _ingestion_record(first_project, "session:shared-source")
    first_checkpoint = _ingestion_checkpoint(10)
    ingestion.ingest(
        _ingestion_adapter((first_record,), first_checkpoint),
        "session.jsonl",
    )
    checkpoints.commit_import_receipt(
        "f" * 64,
        first_record.source_record_id,
        SourceAgent.CODEX,
    )
    if reuse_scope == "project":
        second_project = tmp_path / "second-project"
        second_project.mkdir()
        projects.register(ProjectCandidate(canonical_path=second_project, display_name="second"))
        model_id = first_record.namespace.model_id
        outcome = first_record.outcome
    elif reuse_scope == "model":
        second_project = first_project
        model_id = "model-two"
        outcome = first_record.outcome
    else:
        second_project = first_project
        model_id = first_record.namespace.model_id
        outcome = "changed canonical content"
    second_record = _ingestion_record(
        second_project,
        first_record.source_record_id,
        model_id=model_id,
        outcome=outcome,
        verified_at=first_record.verification.verified_at + timedelta(seconds=1),
    )
    before = _ingestion_counts(database)
    copy_checkpoint = _ingestion_checkpoint(20, scope="copy.jsonl")

    with pytest.raises(RuntimeError):
        ingestion.ingest(
            _ingestion_adapter((second_record,), copy_checkpoint),
            "copy.jsonl",
        )

    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") == first_checkpoint
    assert checkpoints.get(SourceAgent.CODEX, "copy.jsonl") is None
    assert _ingestion_counts(database) == before


def test_normal_resolution_can_update_last_observed_without_project_guard_conflict(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    registered = projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = [datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)]
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock[0],
    )
    target = _ingestion_record(
        project,
        "seed:last-observed-target",
        objective="",
        outcome="",
        open_issues=("exact old issue",),
        verified_at=clock[0],
    )
    capture.capture(
        CapturePayload(
            cwd=project,
            namespace=target.namespace,
            source_record_id=target.source_record_id,
            objective="",
            outcome="",
            open_issues=list(target.open_issues),
        ),
        target.verification,
    )
    with database.connect(readonly=True) as connection:
        observed_before = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(registered.project_id).lower(),),
        ).fetchone()[0]
    clock[0] += timedelta(seconds=2)
    resolution = _ingestion_record(
        project,
        "session:last-observed-resolution",
        resolved_open_issues=("exact old issue",),
        verified_at=clock[0] - timedelta(seconds=1),
    )
    checkpoints = CheckpointRepository(database)

    result = IngestionService(capture, checkpoints, database, projects).ingest(
        _ingestion_adapter((resolution,), _ingestion_checkpoint(10)),
        "session.jsonl",
    )

    assert result.resolved_count == 1
    assert result.capture_results[0].status == "inserted"
    with database.connect(readonly=True) as connection:
        observed_after = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(registered.project_id).lower(),),
        ).fetchone()[0]
    assert observed_after != observed_before


def test_unmatched_resolution_counts_as_warning_and_commits_partial_capture(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    project = tmp_path / "project"
    project.mkdir()
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    checkpoints = CheckpointRepository(database)
    record = _ingestion_record(
        project,
        "session:unmatched-resolution",
        resolved_open_issues=("unknown exact old issue",),
    )

    result = IngestionService(capture, checkpoints, database, projects).ingest(
        _ingestion_adapter(
            (record,),
            _ingestion_checkpoint(10),
            warnings=("parser_warning:1",),
        ),
        "session.jsonl",
    )

    assert result.capture_results[0].status == "partial"
    assert result.resolved_count == 0
    assert result.already_resolved_count == 0
    assert result.unmatched_resolution_count == 1
    assert result.warning_count == 2
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") == result.checkpoint


def test_adapter_cannot_mutate_the_expected_checkpoint_cas_snapshot(tmp_path: Path) -> None:
    database = _database(tmp_path)
    projects = ProjectRepository(database)
    checkpoints = CheckpointRepository(database)
    original = _ingestion_checkpoint(10)
    desired = _ingestion_checkpoint(20)
    checkpoints.commit(SourceAgent.CODEX, "session.jsonl", original)

    class MutatingAdapter:
        source_agent = SourceAgent.CODEX

        def read_incremental(self, _scope, checkpoint):
            assert checkpoint is not None
            checkpoint.cursor["offset"] = 999
            return AdapterBatch(records=(), next_checkpoint=desired)

    result = IngestionService(
        CaptureService(database, projects, MemoryRepository(database), Redactor()),
        checkpoints,
        database,
        projects,
    ).ingest(MutatingAdapter(), "session.jsonl")

    assert result.checkpoint == desired
    assert checkpoints.get(SourceAgent.CODEX, "session.jsonl") == desired


def test_checkpoint_commit_is_canonical_and_atomic_with_receipts(tmp_path):
    repository = CheckpointRepository(_database(tmp_path))
    checkpoint = AdapterCheckpoint(
        adapter=SourceAgent.CODEX,
        scope="scope.jsonl",
        cursor={"z": 2, "a": "first"},
        parser_version="codex-v1",
    )

    repository.commit(
        SourceAgent.CODEX,
        "scope.jsonl",
        checkpoint,
        source_record_ids=("session:turn",),
    )

    assert repository.get(SourceAgent.CODEX, "scope.jsonl") == checkpoint
    with repository.database.connect(readonly=True) as connection:
        row = connection.execute(
            "select cursor_json from checkpoints where adapter = ? and scope = ?",
            ("codex", "scope.jsonl"),
        ).fetchone()
        assert row["cursor_json"] == '{"a":"first","z":2}'
        assert (
            connection.execute(
                "select count(*) from import_receipts where source_record_id = ?",
                ("session:turn",),
            ).fetchone()[0]
            == 1
        )


def test_registry_exposes_only_registered_enabled_sources():
    codex = SimpleNamespace(source_agent=SourceAgent.CODEX)
    disabled_chatgpt = SimpleNamespace(source_agent=SourceAgent.CHATGPT)
    registry = AdapterRegistry(
        adapters=(codex, disabled_chatgpt),
        enabled_sources=(SourceAgent.CODEX, SourceAgent.TRAE),
    )

    assert registry.enabled() == (codex,)


def test_symlinked_jsonl_is_not_discovered(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text(_line(_session()))
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "linked.jsonl").symlink_to(outside)

    assert CodexAdapter(sessions, Redactor()).discover_scopes() == ()


def test_symlinked_sessions_root_parent_is_rejected(tmp_path):
    real_parent = tmp_path / "real-parent"
    sessions = real_parent / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "session.jsonl").write_text(_line(_session()))
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    adapter = CodexAdapter(linked_parent / "sessions", Redactor())

    with pytest.raises(PermissionError):
        adapter.discover_scopes()
    with pytest.raises(PermissionError):
        adapter.read_incremental("session.jsonl", None)


def test_same_inode_nonshrinking_rewrite_restarts_from_zero(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session("old-session"))
        + _line(_context("shared-turn", "/old", "old-model"))
        + _line(_complete("shared-turn", "Outcome: old result"))
    )
    adapter = CodexAdapter(tmp_path, Redactor())
    checkpoint = adapter.read_incremental("session.jsonl", None).next_checkpoint
    inode = source.stat().st_ino
    replacement = (
        _line(_session("new-session"))
        + _line(_context("shared-turn", "/new", "new-model"))
        + _line(_complete("shared-turn", "Outcome: new result"))
        + _line(
            {
                "timestamp": "2026-07-12T00:00:03Z",
                "type": "response_item",
                "payload": {"padding": "x" * 2000},
            }
        )
    )
    source.write_text(replacement)
    assert source.stat().st_ino == inode
    assert source.stat().st_size >= checkpoint.cursor["observed_size"]

    batch = adapter.read_incremental("session.jsonl", checkpoint)

    assert "source_restarted:1" in batch.warnings
    assert [(record.source_record_id, record.namespace.model_id) for record in batch.records] == [
        ("new-session:shared-turn", "new-model")
    ]


def test_early_prefix_rewrite_with_unchanged_tail_cannot_reuse_old_context(tmp_path):
    source = tmp_path / "session.jsonl"
    old_prefix = _line(_session("old-session")) + _line(
        _context("shared-turn", "/old", "old-model")
    )
    unchanged_tail = _line(
        {
            "timestamp": "2026-07-12T00:00:02Z",
            "type": "response_item",
            "payload": {"padding": "x" * 6000},
        }
    )
    source.write_text(old_prefix + unchanged_tail)
    adapter = CodexAdapter(tmp_path, Redactor())
    checkpoint = adapter.read_incremental("session.jsonl", None).next_checkpoint
    inode = source.stat().st_ino
    new_prefix = _line(_session("new-session")) + _line(
        _context("shared-turn", "/new", "new-model")
    )
    assert len(new_prefix.encode()) == len(old_prefix.encode())
    source.write_text(
        new_prefix + unchanged_tail + _line(_complete("shared-turn", "Outcome: new result"))
    )
    assert source.stat().st_ino == inode

    batch = adapter.read_incremental("session.jsonl", checkpoint)

    assert "source_restarted:1" in batch.warnings
    assert [(record.source_record_id, record.namespace.model_id) for record in batch.records] == [
        ("new-session:shared-turn", "new-model")
    ]


@pytest.mark.parametrize("field", ["session_id", "turn_id", "cwd", "model", "summary"])
def test_oversized_provenance_fields_never_enter_checkpoint(tmp_path, field):
    oversized = "PRIVATE_OVERSIZED_MARKER" * 1000
    session = _session()
    context = _context("turn", "/repo", "model", "summary")
    if field == "session_id":
        session["payload"]["session_id"] = oversized
        session["payload"]["id"] = oversized
    else:
        context["payload"][field] = oversized
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(session) + _line(context) + _line(_complete("turn", "Outcome: must not import"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    warning_category = "field_too_large" if field in {"cwd", "summary"} else "unsafe_identifier"
    assert any(warning.startswith(f"{warning_category}:") for warning in batch.warnings)
    assert oversized not in json.dumps(batch.next_checkpoint.cursor)
    assert len(json.dumps(batch.next_checkpoint.cursor)) < 20_000


def test_turn_summary_is_validated_but_never_persisted_in_checkpoint(tmp_path):
    private_summary = "PRIVATE_SUMMARY_MUST_NOT_PERSIST"
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session()) + _line(_context("turn", "/repo", "model", private_summary))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    checkpoint_json = json.dumps(batch.next_checkpoint.cursor)
    contexts = json.loads(batch.next_checkpoint.cursor["contexts_json"])
    assert private_summary not in checkpoint_json
    assert set(contexts["turn"]) == {"blocked", "cwd", "model", "session_id"}


def test_post_canonicalization_expansion_rejects_block_and_advances_checkpoint(tmp_path):
    expanding_decisions = [
        " ".join(
            f"a:{hashlib.sha256(f'{line}-{index}'.encode()).hexdigest()[:6]}.git"
            for index in range(400)
        )
        for line in range(24)
    ]
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + _line(_context("turn", "/repo", "model"))
        + _line(
            _complete(
                "turn",
                _managed_capture(
                    "Objective: bounded input",
                    "Outcome: bounded input",
                    *(f"Decision: {decision}" for decision in expanding_decisions),
                ),
            )
        )
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("session.jsonl", None)

    assert batch.records == ()
    assert "invalid_capture_block:1" in batch.warnings
    assert batch.next_checkpoint.cursor["offset"] == source.stat().st_size


def test_context_aggregate_cap_fails_closed_without_huge_cursor(tmp_path):
    source = tmp_path / "session.jsonl"
    source.write_text(
        _line(_session())
        + "".join(
            _line(_context(f"turn-{index}", "/repo", "model", "summary" * 20))
            for index in range(20)
        )
    )

    batch = CodexAdapter(
        tmp_path,
        Redactor(),
        max_context_bytes=512,
    ).read_incremental("session.jsonl", None)

    assert any(warning.startswith("context_limit_exceeded:") for warning in batch.warnings)
    assert len(batch.next_checkpoint.cursor["contexts_json"].encode()) <= 512


def test_new_session_clears_old_turn_and_duplicate_turn_fails_closed(tmp_path):
    source = tmp_path / "sessions.jsonl"
    source.write_text(
        _line(_session("session-a"))
        + _line(_context("shared", "/repo/a", "model-a"))
        + _line(_session("session-b"))
        + _line(_complete("shared", "Outcome: stale must not import"))
        + _line(_context("duplicate", "/repo/b1", "model-b1"))
        + _line(_context("duplicate", "/repo/b2", "model-b2"))
        + _line(_complete("duplicate", "Outcome: ambiguous must not import"))
        + _line(_context("valid", "/repo/b", "model-b"))
        + _line(_complete("valid", "Outcome: valid result"))
    )

    batch = CodexAdapter(tmp_path, Redactor()).read_incremental("sessions.jsonl", None)

    assert [(record.source_record_id, record.outcome) for record in batch.records] == [
        ("session-b:valid", "valid result")
    ]
    assert "ambiguous_turn:1" in batch.warnings
    assert "orphan_completion:1" in batch.warnings


@pytest.mark.parametrize(
    ("max_entries", "max_scopes"),
    [(3, 10), (10, 2)],
)
def test_discovery_limits_fail_closed_deterministically(tmp_path, max_entries, max_scopes):
    for name in ("a.jsonl", "b.jsonl", "c.jsonl", "d.jsonl"):
        (tmp_path / name).write_text(_line(_session(name)))
    adapter = CodexAdapter(
        tmp_path,
        Redactor(),
        max_discovery_entries=max_entries,
        max_scopes=max_scopes,
    )

    with pytest.raises(DiscoveryLimitExceeded, match="discovery_limit_exceeded"):
        adapter.discover_scopes()
