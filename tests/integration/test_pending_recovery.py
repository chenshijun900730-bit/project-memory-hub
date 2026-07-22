import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import project_memory_hub.services.capture as capture_module
from project_memory_hub.adapters.codex import CAPTURE_END, CAPTURE_START, CodexAdapter
from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService, PreparedVerifiedCapture
from project_memory_hub.services.pending_recovery import (
    PendingRecoveryError,
    PendingRecoveryMapping,
    PendingRecoveryService,
)
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository


def _line(record: dict[str, object]) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def _capture(outcome: str) -> str:
    return "\n".join(
        (
            CAPTURE_START,
            "Objective: recover pending evidence",
            f"Outcome: {outcome}",
            CAPTURE_END,
        )
    )


def _write_two_turn_session(source: Path, project: Path) -> None:
    records: list[dict[str, object]] = [
        {
            "timestamp": "2026-07-19T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "session-one", "session_id": "session-one"},
        }
    ]
    for index, outcome in ((1, "first exact outcome"), (2, "second exact outcome")):
        records.extend(
            (
                {
                    "timestamp": f"2026-07-19T00:00:0{index}Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": f"turn-{index}",
                        "cwd": str(project),
                        "model": "gpt-test",
                        "summary": "pending recovery",
                    },
                },
                {
                    "timestamp": f"2026-07-19T00:00:1{index}Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": f"turn-{index}",
                        "last_agent_message": _capture(outcome),
                    },
                },
            )
        )
    source.write_text("".join(_line(record) for record in records), encoding="utf-8")


def _database(tmp_path: Path) -> Database:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    return database


def _verification(payload: CapturePayload, second: int) -> NamespaceVerification:
    return NamespaceVerification(
        namespace=payload.namespace,
        source_record_id=payload.source_record_id,
        verified_by="codex_adapter",
        verified_at=datetime(2026, 7, 19, 0, 0, second, tzinfo=timezone.utc),
    )


def test_pending_recovery_replays_one_scope_once_and_verifies_exact_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    registered = projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = datetime(2026, 7, 19, 0, 30, tzinfo=timezone.utc)
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock,
    )
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
    for index, outcome in ((1, "first exact outcome"), (2, "second exact outcome")):
        assert (
            capture.capture(
                CapturePayload(
                    cwd=project,
                    namespace=namespace,
                    source_record_id=f"legacy-source-{index}",
                    objective="recover pending evidence",
                    outcome=outcome,
                )
            ).status
            == "pending_verification"
        )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "rollout-session-one.jsonl"
    _write_two_turn_session(sessions / scope, project)
    with database.connect(readonly=True) as connection:
        pending = connection.execute(
            """
            select pending_id, structured_hash from pending_captures
            where verification_state = 'pending'
            order by source_record_id
            """
        ).fetchall()
    mappings = tuple(
        PendingRecoveryMapping(
            pending_id=row["pending_id"],
            scope=scope,
            source_record_id=f"session-one:turn-{index}",
            expected_structured_hash=row["structured_hash"],
        )
        for index, row in enumerate(pending, start=1)
    )
    cleanup_calls = 0
    original_cleanup = capture_module._cleanup_pending_capture_history_on_connection

    def count_cleanup(connection):
        nonlocal cleanup_calls
        cleanup_calls += 1
        return original_cleanup(connection)

    monkeypatch.setattr(
        capture_module,
        "_cleanup_pending_capture_history_on_connection",
        count_cleanup,
    )
    recovery = PendingRecoveryService(
        database,
        projects,
        capture,
        CodexAdapter(sessions, Redactor()),
    )

    preview = recovery.recover(mappings, apply=False)
    report = recovery.recover(mappings, apply=True)

    assert preview.status == "ready"
    assert preview.verified_count == 0
    assert report.status == "recovered"
    assert report.requested_count == 2
    assert report.verified_count == 2
    assert report.source_count == 2
    assert cleanup_calls == 1
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0
        history = connection.execute(
            """
            select final_state, source_reference_id
            from pending_capture_history order by pending_id
            """
        ).fetchall()
        assert len(history) == 2
        assert {row["final_state"] for row in history} == {"verified"}
        assert all(row["source_reference_id"] is not None for row in history)
        assert "structured_payload_json" not in {
            row["name"] for row in connection.execute("pragma table_info(pending_capture_history)")
        }
        assert (
            connection.execute(
                "select count(*) from source_refs where capture_project_id = ?",
                (str(registered.project_id).lower(),),
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "select count(*) from import_receipts where source_agent = 'codex'"
            ).fetchone()[0]
            == 2
        )


def test_pending_recovery_rejects_one_trusted_record_across_two_scopes_without_writes(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: datetime(2026, 7, 19, 0, 30, tzinfo=timezone.utc),
    )
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
    for source_record_id in ("legacy-source-one", "legacy-source-two"):
        assert (
            capture.capture(
                CapturePayload(
                    cwd=project,
                    namespace=namespace,
                    source_record_id=source_record_id,
                    objective="recover pending evidence",
                    outcome="first exact outcome",
                )
            ).status
            == "pending_verification"
        )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scopes = ("rollout-one.jsonl", "rollout-two.jsonl")
    for scope in scopes:
        _write_two_turn_session(sessions / scope, project)
    with database.connect(readonly=True) as connection:
        pending = connection.execute(
            """
            select pending_id, structured_hash from pending_captures
            order by source_record_id
            """
        ).fetchall()
        before_counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "pending_captures",
                "pending_capture_history",
                "source_refs",
                "behavior_memories",
                "import_receipts",
            )
        )
    mappings = tuple(
        PendingRecoveryMapping(
            pending_id=row["pending_id"],
            scope=scope,
            source_record_id="session-one:turn-1",
            expected_structured_hash=row["structured_hash"],
        )
        for row, scope in zip(pending, scopes, strict=True)
    )
    recovery = PendingRecoveryService(
        database,
        projects,
        capture,
        CodexAdapter(sessions, Redactor()),
    )

    with pytest.raises(PendingRecoveryError, match="ambiguous_source"):
        recovery.recover(mappings, apply=True)

    with database.connect(readonly=True) as connection:
        after_counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "pending_captures",
                "pending_capture_history",
                "source_refs",
                "behavior_memories",
                "import_receipts",
            )
        )
    assert after_counts == before_counts


def test_pending_recovery_preview_rejects_an_incompatible_existing_source_ref(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = datetime(2026, 7, 19, 0, 30, tzinfo=timezone.utc)
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock,
    )
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
    collision = CapturePayload(
        cwd=project,
        namespace=namespace,
        source_record_id="session-one:turn-1",
        objective="recover pending evidence",
        outcome="incompatible persisted outcome",
    )
    assert capture.capture(collision, _verification(collision, 11)).status == "inserted"
    pending_payload = collision.model_copy(
        update={
            "source_record_id": "legacy-pending",
            "outcome": "first exact outcome",
        }
    )
    assert capture.capture(pending_payload).status == "pending_verification"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "rollout-session-one.jsonl"
    _write_two_turn_session(sessions / scope, project)
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            """
            select pending_id, structured_hash from pending_captures
            where source_record_id = 'legacy-pending'
            """
        ).fetchone()
    mapping = PendingRecoveryMapping(
        pending_id=row["pending_id"],
        scope=scope,
        source_record_id="session-one:turn-1",
        expected_structured_hash=row["structured_hash"],
    )

    with pytest.raises(PendingRecoveryError, match="rejected"):
        PendingRecoveryService(
            database,
            projects,
            capture,
            CodexAdapter(sessions, Redactor()),
        ).recover((mapping,), apply=False)


@pytest.mark.parametrize("apply", (False, True))
def test_pending_recovery_keeps_backward_timestamp_strict_even_with_receipt(
    tmp_path: Path,
    apply: bool,
) -> None:
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = datetime(2026, 7, 19, 0, 30, tzinfo=timezone.utc)
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock,
    )
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
    trusted = CapturePayload(
        cwd=project,
        namespace=namespace,
        source_record_id="session-one:turn-1",
        objective="recover pending evidence",
        outcome="first exact outcome",
    )
    assert capture.capture(trusted, _verification(trusted, 12)).status == "inserted"
    CheckpointRepository(database).commit_import_receipt(
        "f" * 64,
        trusted.source_record_id,
        SourceAgent.CODEX,
    )
    pending = trusted.model_copy(update={"source_record_id": "legacy-backward-pending"})
    assert capture.capture(pending).status == "pending_verification"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "rollout-session-one.jsonl"
    _write_two_turn_session(sessions / scope, project)
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            """
            select pending_id, structured_hash from pending_captures
            where source_record_id = ?
            """,
            (pending.source_record_id,),
        ).fetchone()
        before = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "pending_captures",
                "pending_capture_history",
                "source_refs",
                "behavior_memories",
                "import_receipts",
            )
        )
    mapping = PendingRecoveryMapping(
        pending_id=row["pending_id"],
        scope=scope,
        source_record_id=trusted.source_record_id,
        expected_structured_hash=row["structured_hash"],
    )

    with pytest.raises(PendingRecoveryError, match="rejected"):
        PendingRecoveryService(
            database,
            projects,
            capture,
            CodexAdapter(sessions, Redactor()),
        ).recover((mapping,), apply=apply)

    with database.connect(readonly=True) as connection:
        after = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "pending_captures",
                "pending_capture_history",
                "source_refs",
                "behavior_memories",
                "import_receipts",
            )
        )
        pending_state = connection.execute(
            """
            select verification_state from pending_captures
            where pending_id = ?
            """,
            (str(mapping.pending_id).lower(),),
        ).fetchone()[0]
    assert after == before
    assert pending_state == "pending"


def test_pending_recovery_compatible_duplicate_verifies_only_the_mapped_pending_row(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = datetime(2026, 7, 19, 0, 30, tzinfo=timezone.utc)
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock,
    )
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
    verified_payload = CapturePayload(
        cwd=project,
        namespace=namespace,
        source_record_id="session-one:turn-1",
        objective="recover pending evidence",
        outcome="first exact outcome",
    )
    assert (
        capture.capture(
            verified_payload,
            _verification(verified_payload, 11),
        ).status
        == "inserted"
    )
    for source_record_id in ("legacy-selected", "legacy-unmapped"):
        assert (
            capture.capture(
                verified_payload.model_copy(update={"source_record_id": source_record_id})
            ).status
            == "pending_verification"
        )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "rollout-session-one.jsonl"
    _write_two_turn_session(sessions / scope, project)
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            """
            select pending_id, structured_hash from pending_captures
            where source_record_id = 'legacy-selected'
            """
        ).fetchone()
    mapping = PendingRecoveryMapping(
        pending_id=row["pending_id"],
        scope=scope,
        source_record_id="session-one:turn-1",
        expected_structured_hash=row["structured_hash"],
    )
    recovery = PendingRecoveryService(
        database,
        projects,
        capture,
        CodexAdapter(sessions, Redactor()),
    )

    assert recovery.recover((mapping,), apply=False).status == "ready"
    report = recovery.recover((mapping,), apply=True)

    assert report.status == "recovered"
    assert report.verified_count == 1
    with database.connect(readonly=True) as connection:
        active = {
            row["source_record_id"]: row["verification_state"]
            for row in connection.execute(
                """
                select source_record_id, verification_state from pending_captures
                order by source_record_id
                """
            ).fetchall()
        }
        archived = connection.execute(
            """
            select source_record_id, final_state, source_reference_id
            from pending_capture_history
            """
        ).fetchall()
        assert (
            connection.execute(
                """
            select count(*) from source_refs
            where source_record_id = 'session-one:turn-1'
            """
            ).fetchone()[0]
            == 1
        )
    assert active == {"legacy-unmapped": "pending"}
    assert [row["source_record_id"] for row in archived] == ["legacy-selected"]
    assert archived[0]["final_state"] == "verified"
    assert archived[0]["source_reference_id"] is not None


def test_pending_recovery_rolls_back_the_whole_batch_when_the_second_capture_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    clock = datetime(2026, 7, 19, 0, 30, tzinfo=timezone.utc)
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock,
    )
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
    for index, outcome in ((1, "first exact outcome"), (2, "second exact outcome")):
        assert (
            capture.capture(
                CapturePayload(
                    cwd=project,
                    namespace=namespace,
                    source_record_id=f"legacy-source-{index}",
                    objective="recover pending evidence",
                    outcome=outcome,
                )
            ).status
            == "pending_verification"
        )
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "rollout-session-one.jsonl"
    _write_two_turn_session(sessions / scope, project)
    with database.connect(readonly=True) as connection:
        pending = connection.execute(
            """
            select pending_id, structured_hash from pending_captures
            where verification_state = 'pending'
            order by source_record_id
            """
        ).fetchall()
        before_counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ("source_refs", "behavior_memories", "import_receipts")
        )
    mappings = tuple(
        PendingRecoveryMapping(
            pending_id=row["pending_id"],
            scope=scope,
            source_record_id=f"session-one:turn-{index}",
            expected_structured_hash=row["structured_hash"],
        )
        for index, row in enumerate(pending, start=1)
    )
    recovery = PendingRecoveryService(
        database,
        projects,
        capture,
        CodexAdapter(sessions, Redactor()),
    )
    original_capture = capture.capture_prepared_on_connection
    call_count = 0

    def fail_after_second_write(
        connection: sqlite3.Connection,
        prepared: PreparedVerifiedCapture,
        **kwargs: Any,
    ) -> CaptureResult:
        nonlocal call_count
        call_count += 1
        result = original_capture(connection, prepared, **kwargs)
        if call_count == 2:
            raise RuntimeError("injected second-capture failure")
        return result

    monkeypatch.setattr(
        capture,
        "capture_prepared_on_connection",
        fail_after_second_write,
    )

    with pytest.raises(PendingRecoveryError, match="rejected"):
        recovery.recover(mappings, apply=True)

    with database.connect(readonly=True) as connection:
        after_counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "source_refs",
                "behavior_memories",
                "import_receipts",
                "pending_capture_history",
            )
        )
        states = connection.execute(
            "select verification_state from pending_captures order by pending_id"
        ).fetchall()
    assert call_count == 2
    assert after_counts == (*before_counts, 0)
    assert [row["verification_state"] for row in states] == ["pending", "pending"]
