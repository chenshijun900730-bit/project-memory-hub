import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
import project_memory_hub.storage.path_identity as path_identity_module
from project_memory_hub.adapters.base import IngestionService
from project_memory_hub.adapters.codex import CAPTURE_END, CAPTURE_START, CodexAdapter
from project_memory_hub.cli import app
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import (
    CapturePayload,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.deferred_recovery import DeferredRecoveryService
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.deferred_records import (
    CodexDeferredLocator,
    CodexDeferredRecordRepository,
    DeferredRecoveryError,
)
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository


runner = CliRunner()


def _line(record: dict[str, object]) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def _session(session_id: str) -> dict[str, object]:
    return {
        "timestamp": "2026-07-19T00:00:00Z",
        "type": "session_meta",
        "payload": {"id": session_id, "session_id": session_id},
    }


def _context(turn_id: str, cwd: Path) -> dict[str, object]:
    return {
        "timestamp": "2026-07-19T00:00:01Z",
        "type": "turn_context",
        "payload": {
            "turn_id": turn_id,
            "cwd": str(cwd),
            "model": "gpt-test",
            "summary": "recover deferred capture",
        },
    }


def _complete(turn_id: str, outcome: str) -> dict[str, object]:
    capture = "\n".join(
        (
            CAPTURE_START,
            "Objective: recover exact deferred capture",
            f"Outcome: {outcome}",
            "Verified: exact source replay",
            CAPTURE_END,
        )
    )
    return {
        "timestamp": "2026-07-19T00:00:02Z",
        "type": "event_msg",
        "payload": {
            "type": "task_complete",
            "turn_id": turn_id,
            "last_agent_message": capture,
        },
    }


def _write_session(
    root: Path,
    scope: str,
    *,
    session_id: str,
    turn_id: str,
    cwd: Path,
    outcome: str,
) -> None:
    source = root / scope
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        _line(_session(session_id))
        + _line(_context(turn_id, cwd))
        + _line(_complete(turn_id, outcome)),
        encoding="utf-8",
    )


def _database(tmp_path: Path) -> Database:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    return database


def test_codex_adapter_replays_only_an_exact_deferred_locator(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    missing = tmp_path / "missing-project"
    _write_session(
        sessions,
        "2026/session.jsonl",
        session_id="session-one",
        turn_id="turn-one",
        cwd=missing,
        outcome="exact recovered outcome",
    )
    adapter = CodexAdapter(sessions, Redactor())
    batch = adapter.read_incremental("2026/session.jsonl", None)
    locator = CodexDeferredLocator.from_checkpoint(
        "2026/session.jsonl",
        "session-one:turn-one",
        batch.next_checkpoint,
    )

    replayed = adapter.replay_deferred(locator)

    assert replayed.source_record_id == "session-one:turn-one"
    assert replayed.cwd == missing
    assert replayed.outcome == "exact recovered outcome"


def test_codex_adapter_accepts_darwin_device_drift_for_an_exact_deferred_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "2026/session.jsonl"
    _write_session(
        sessions,
        scope,
        session_id="session-one",
        turn_id="turn-one",
        cwd=tmp_path / "missing-project",
        outcome="exact recovered outcome",
    )
    adapter = CodexAdapter(sessions, Redactor())
    batch = adapter.read_incremental(scope, None)
    locator = CodexDeferredLocator.from_checkpoint(
        scope,
        "session-one:turn-one",
        batch.next_checkpoint,
    )
    drifted = replace(locator, source_device=locator.source_device + 1)
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")

    replayed = adapter.replay_deferred(drifted)

    assert replayed.source_record_id == "session-one:turn-one"


def test_codex_adapter_rejects_device_drift_outside_darwin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "2026/session.jsonl"
    _write_session(
        sessions,
        scope,
        session_id="session-one",
        turn_id="turn-one",
        cwd=tmp_path / "missing-project",
        outcome="exact recovered outcome",
    )
    adapter = CodexAdapter(sessions, Redactor())
    batch = adapter.read_incremental(scope, None)
    locator = CodexDeferredLocator.from_checkpoint(
        scope,
        "session-one:turn-one",
        batch.next_checkpoint,
    )
    drifted = replace(locator, source_device=locator.source_device + 1)
    monkeypatch.setattr(path_identity_module.sys, "platform", "linux")

    with pytest.raises(DeferredRecoveryError, match="source_changed"):
        adapter.replay_deferred(drifted)


def test_codex_adapter_rejects_a_deferred_locator_after_prefix_change(
    tmp_path: Path,
) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    scope = "session.jsonl"
    _write_session(
        sessions,
        scope,
        session_id="session-one",
        turn_id="turn-one",
        cwd=tmp_path / "missing-project",
        outcome="exact recovered outcome",
    )
    adapter = CodexAdapter(sessions, Redactor())
    batch = adapter.read_incremental(scope, None)
    locator = CodexDeferredLocator.from_checkpoint(
        scope,
        "session-one:turn-one",
        batch.next_checkpoint,
    )
    source = sessions / scope
    original = source.read_text(encoding="utf-8")
    source.write_text(original.replace("exact recovered outcome", "tampered outcome value"))

    with pytest.raises(DeferredRecoveryError, match="source_changed"):
        adapter.replay_deferred(locator)


def test_explicit_rebind_recovers_same_scope_darwin_device_variants_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    missing = tmp_path / "deleted-original-project"
    scope = "rollout-device-session.jsonl"
    source_record_id = "device-session:device-turn"
    _write_session(
        sessions,
        scope,
        session_id="device-session",
        turn_id="device-turn",
        cwd=missing,
        outcome="one recovered memory",
    )

    projects = ProjectRepository(database)
    memories = MemoryRepository(database)
    checkpoints = CheckpointRepository(database)
    capture = CaptureService(database, projects, memories, Redactor())
    adapter = CodexAdapter(sessions, Redactor())
    assert (
        IngestionService(capture, checkpoints, database, projects)
        .ingest(
            adapter,
            scope,
        )
        .deferred_count
        == 1
    )

    deferred = CodexDeferredRecordRepository()
    original = deferred.records_for_source(database, source_record_id)
    assert len(original) == 1
    drifted = replace(
        original[0].locator,
        source_device=original[0].locator.source_device + 1,
    )
    with database.transaction() as connection:
        assert deferred.defer_on_connection(connection, drifted) is True

    target = tmp_path / "registered-target"
    target.mkdir()
    projects.register(ProjectCandidate(canonical_path=target, display_name="target"))
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    recovery = DeferredRecoveryService(database, projects, capture, adapter)

    preview = recovery.recover(
        source_record_id=source_record_id,
        target_project=target,
    )
    assert preview.status == "ready"
    report = recovery.recover(
        source_record_id=source_record_id,
        target_project=target,
        apply=True,
    )

    assert report.status == "recovered"
    assert report.locator_count == 2
    assert report.recovered_locator_count == 2
    assert report.capture_status == "inserted"
    with database.connect(readonly=True) as connection:
        states = connection.execute(
            "select state from codex_deferred_records order by deferred_id"
        ).fetchall()
        source_ref_count = connection.execute(
            """
            select count(*) from source_refs
            where source_agent = ? and source_record_id = ?
            """,
            (SourceAgent.CODEX.value, source_record_id),
        ).fetchone()[0]
    assert [row["state"] for row in states] == ["recovered", "recovered"]
    assert source_ref_count == 1


def test_same_scope_device_variant_recovery_rolls_back_every_write_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = _database(tmp_path)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    missing = tmp_path / "deleted-original-project"
    scope = "rollout-rollback-session.jsonl"
    source_record_id = "rollback-session:rollback-turn"
    _write_session(
        sessions,
        scope,
        session_id="rollback-session",
        turn_id="rollback-turn",
        cwd=missing,
        outcome="must roll back",
    )

    projects = ProjectRepository(database)
    memories = MemoryRepository(database)
    checkpoints = CheckpointRepository(database)
    capture = CaptureService(database, projects, memories, Redactor())
    adapter = CodexAdapter(sessions, Redactor())
    assert (
        IngestionService(capture, checkpoints, database, projects)
        .ingest(
            adapter,
            scope,
        )
        .deferred_count
        == 1
    )

    deferred = CodexDeferredRecordRepository()
    original = deferred.records_for_source(database, source_record_id)
    drifted = replace(
        original[0].locator,
        source_device=original[0].locator.source_device + 1,
    )
    with database.transaction() as connection:
        assert deferred.defer_on_connection(connection, drifted) is True

    target = tmp_path / "registered-target"
    target.mkdir()
    projects.register(ProjectCandidate(canonical_path=target, display_name="target"))
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")

    def fail_after_one_locator(
        connection,
        records,
        *,
        recovered_at: str,
    ) -> int:
        connection.execute(
            """
            update codex_deferred_records
            set state = 'recovered', recovered_at = ?
            where deferred_id = ?
            """,
            (recovered_at, records[0].deferred_id),
        )
        raise sqlite3.IntegrityError("injected locator finalization failure")

    monkeypatch.setattr(
        CodexDeferredRecordRepository,
        "mark_recovered_on_connection",
        staticmethod(fail_after_one_locator),
    )

    with pytest.raises(DeferredRecoveryError, match="rejected"):
        DeferredRecoveryService(database, projects, capture, adapter).recover(
            source_record_id=source_record_id,
            target_project=target,
            apply=True,
        )

    with database.connect(readonly=True) as connection:
        states = connection.execute(
            "select state from codex_deferred_records order by deferred_id"
        ).fetchall()
        source_ref_count = connection.execute("select count(*) from source_refs").fetchone()[0]
        memory_count = connection.execute("select count(*) from behavior_memories").fetchone()[0]
    assert [row["state"] for row in states] == ["pending", "pending"]
    assert source_ref_count == 0
    assert memory_count == 0


def test_explicit_rebind_recovers_duplicate_locators_once_and_marks_all_recovered(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    missing = tmp_path / "deleted-original-project"
    scopes = ("rollout-shared-session.jsonl", "fork.jsonl")
    for scope in scopes:
        _write_session(
            sessions,
            scope,
            session_id="shared-session",
            turn_id="shared-turn",
            cwd=missing,
            outcome="one recovered memory",
        )

    projects = ProjectRepository(database)
    memories = MemoryRepository(database)
    checkpoints = CheckpointRepository(database)
    capture = CaptureService(database, projects, memories, Redactor())
    adapter = CodexAdapter(sessions, Redactor())
    ingestion = IngestionService(capture, checkpoints, database, projects)
    for scope in scopes:
        result = ingestion.ingest(adapter, scope)
        assert result.deferred_count == 1

    target = tmp_path / "registered-target"
    target.mkdir()
    projects.register(ProjectCandidate(canonical_path=target, display_name="target"))
    recovery = DeferredRecoveryService(database, projects, capture, adapter)

    preview = recovery.recover(
        source_record_id="shared-session:shared-turn",
        target_project=target,
    )
    assert preview.status == "ready"
    assert preview.recovered_locator_count == 0

    report = recovery.recover(
        source_record_id="shared-session:shared-turn",
        target_project=target,
        apply=True,
    )

    assert report.status == "recovered"
    assert report.locator_count == 2
    assert report.recovered_locator_count == 2
    assert report.capture_status == "inserted"
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from codex_deferred_records where state = 'recovered'"
            ).fetchone()[0]
            == 2
        )
        assert (
            connection.execute(
                "select count(*) from source_refs where source_agent = ? and source_record_id = ?",
                (SourceAgent.CODEX.value, "shared-session:shared-turn"),
            ).fetchone()[0]
            == 1
        )
        rows = connection.execute(
            "select normalized_content from behavior_memories order by normalized_content"
        ).fetchall()
    assert [row[0] for row in rows] == [
        "exact source replay",
        "one recovered memory",
    ]

    repeated = recovery.recover(
        source_record_id="shared-session:shared-turn",
        target_project=target,
        apply=True,
    )
    assert repeated.status == "already_recovered"
    assert repeated.recovered_locator_count == 0


def test_deferred_recovery_preview_rejects_an_incompatible_existing_source_ref(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    missing = tmp_path / "deleted-original-project"
    scope = "rollout-colliding-session.jsonl"
    _write_session(
        sessions,
        scope,
        session_id="colliding-session",
        turn_id="colliding-turn",
        cwd=missing,
        outcome="replayed exact outcome",
    )
    projects = ProjectRepository(database)
    memories = MemoryRepository(database)
    checkpoints = CheckpointRepository(database)
    capture = CaptureService(database, projects, memories, Redactor())
    adapter = CodexAdapter(sessions, Redactor())
    assert (
        IngestionService(capture, checkpoints, database, projects)
        .ingest(
            adapter,
            scope,
        )
        .deferred_count
        == 1
    )
    target = tmp_path / "registered-target"
    target.mkdir()
    projects.register(ProjectCandidate(canonical_path=target, display_name="target"))
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
    colliding_payload = CapturePayload(
        cwd=target,
        namespace=namespace,
        source_record_id="colliding-session:colliding-turn",
        objective="recover exact deferred capture",
        outcome="different already persisted outcome",
    )
    assert (
        capture.capture(
            colliding_payload,
            NamespaceVerification(
                namespace=namespace,
                source_record_id=colliding_payload.source_record_id,
                verified_by="codex_adapter",
                verified_at=datetime(2026, 7, 19, 0, 0, 2, tzinfo=timezone.utc),
            ),
        ).status
        == "inserted"
    )

    with pytest.raises(DeferredRecoveryError, match="rejected"):
        DeferredRecoveryService(database, projects, capture, adapter).recover(
            source_record_id="colliding-session:colliding-turn",
            target_project=target,
            apply=False,
        )


def test_deferred_recover_cli_defaults_to_preview_and_requires_explicit_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(parents=True)
    scope = "rollout-cli-session.jsonl"
    missing = tmp_path / "missing"
    _write_session(
        sessions,
        scope,
        session_id="cli-session",
        turn_id="cli-turn",
        cwd=missing,
        outcome="cli recovered memory",
    )
    database = _database(tmp_path)
    project_root = tmp_path / "projects"
    target = project_root / "target"
    target.mkdir(parents=True)
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=target, display_name="target"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    adapter = CodexAdapter(sessions, Redactor())
    IngestionService(
        capture,
        CheckpointRepository(database),
        database,
        projects,
    ).ingest(adapter, scope)
    config = tmp_path / "runtime" / "config.toml"
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX,),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    request = {
        "source_record_id": "cli-session:cli-turn",
        "target_project": str(target),
    }
    with build_container(config, codex_sessions_root=sessions) as container:
        assert (
            DeferredRecoveryService(
                container.database,
                container.projects,
                container.capture,
                container.codex_adapter,
            )
            .recover(
                source_record_id="cli-session:cli-turn",
                target_project=target,
            )
            .status
            == "ready"
        )
    monkeypatch.setattr(
        cli_module,
        "build_container",
        lambda config_path: build_container(config_path, codex_sessions_root=sessions),
    )

    preview = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "deferred",
            "recover",
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps(request),
        env={"HOME": str(home)},
    )
    applied = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "deferred",
            "recover",
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps({**request, "apply": True}),
        env={"HOME": str(home)},
    )

    assert preview.exit_code == 0, (preview.stdout, preview.exception)
    assert json.loads(preview.stdout)["status"] == "ready"
    assert json.loads(preview.stdout)["recovered_locator_count"] == 0
    assert applied.exit_code == 0, (applied.stdout, applied.exception)
    assert json.loads(applied.stdout)["status"] == "recovered"
    assert json.loads(applied.stdout)["recovered_locator_count"] == 1
