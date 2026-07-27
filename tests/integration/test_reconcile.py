import json
import os
import sqlite3
import stat
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
import project_memory_hub.services.reconcile as reconcile_module
import project_memory_hub.storage.path_identity as path_identity_module
from project_memory_hub.adapters.base import IngestionService, ReconcileRequiredError
from project_memory_hub.adapters.chatgpt import (
    ChatGPTExportAdapter,
    ExplicitTaskExtractor,
    ProjectMatcher,
)
from project_memory_hub.adapters.codex import CodexAdapter
from project_memory_hub.cli import app, _capture_with_transient_retry
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    ReconcileReport,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.services.reconcile import DiscoveryStageResult, ReconcileService
from project_memory_hub.services.retry_queue import RetryQueue
from project_memory_hub.storage.database import Database, ReadonlySnapshotChangedError
from project_memory_hub.storage.checkpoints import CheckpointConflictError, CheckpointRepository
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository
from tests.fixtures.chatgpt.build_fixtures import build_export, conversation


runner = CliRunner()


def _database(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    return database, runtime


def _stored_reconcile_report(database: Database) -> dict[str, object]:
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select value_json from app_state where name = 'last_reconcile_report'"
        ).fetchone()
    assert row is not None
    return json.loads(row[0])


def test_resolution_count_helpers_are_strict_bounded_and_backward_compatible() -> None:
    partial = CaptureResult(
        status="partial",
        inserted_ids=(uuid4(),),
        resolved_count=2,
        already_resolved_count=3,
        unmatched_resolution_count=4,
    )
    legacy = SimpleNamespace(capture_results=(partial,))

    assert reconcile_module._result_counts(legacy) == (1, 0)
    assert reconcile_module._resolution_counts(legacy) == (2, 3, 4)

    malformed_explicit = SimpleNamespace(
        resolved_count=True,
        already_resolved_count=-1,
        unmatched_resolution_count=2**40,
        capture_results=(partial,),
    )
    assert reconcile_module._resolution_counts(malformed_explicit) == (
        0,
        0,
        2**31 - 1,
    )

    partially_explicit = SimpleNamespace(
        resolved_count=7,
        capture_results=(partial,),
    )
    assert reconcile_module._resolution_counts(partially_explicit) == (7, 0, 0)

    unbounded_shape = SimpleNamespace(capture_results=[partial])
    assert reconcile_module._result_counts(unbounded_shape) == (0, 0)
    assert reconcile_module._resolution_counts(unbounded_shape) == (0, 0, 0)


def test_container_codex_aggregate_counts_only_successful_scopes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project,),
            enabled_sources=("codex",),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )

    def ingest(_self: object, _adapter: object, scope: str) -> object:
        if scope == "failed":
            raise RuntimeError("private")
        return SimpleNamespace(
            capture_results=(),
            warning_count=5,
            deferred_count=6,
            resolved_count=2,
            already_resolved_count=3,
            unmatched_resolution_count=4,
        )

    monkeypatch.setattr(IngestionService, "ingest", ingest)
    with build_container(config) as container:
        monkeypatch.setattr(
            container.codex_adapter,
            "discover_scopes",
            lambda: ("accepted", "failed"),
        )
        operation = container.reconcile._codex_runs[0]
        result = operation()

    assert result.failure_count == 1
    assert result.warning_count == 5
    assert result.deferred_count == 6
    assert result.resolved_count == 2
    assert result.already_resolved_count == 3
    assert result.unmatched_resolution_count == 4


@pytest.mark.parametrize(
    "transient_error",
    (ReadonlySnapshotChangedError, CheckpointConflictError),
)
def test_container_codex_retries_one_transient_scope_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient_error: type[RuntimeError],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project,),
            enabled_sources=("codex",),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    attempts = 0

    def ingest(_self: object, _adapter: object, _scope: str) -> object:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise transient_error("transient scope race")
        return SimpleNamespace(
            capture_results=(),
            warning_count=0,
            deferred_count=0,
            resolved_count=0,
            already_resolved_count=0,
            unmatched_resolution_count=0,
        )

    monkeypatch.setattr(IngestionService, "ingest", ingest)
    with build_container(config) as container:
        monkeypatch.setattr(
            container.codex_adapter,
            "discover_scopes",
            lambda: ("raced",),
        )
        operation = container.reconcile._codex_runs[0]
        result = operation()

    assert attempts == 2
    assert result.failure_count == 0


@pytest.mark.parametrize(
    "transient_error",
    (ReadonlySnapshotChangedError, CheckpointConflictError),
)
def test_container_codex_counts_one_failure_after_transient_retry_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    transient_error: type[RuntimeError],
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project,),
            enabled_sources=("codex",),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    attempts = 0

    def ingest(_self: object, _adapter: object, _scope: str) -> object:
        nonlocal attempts
        attempts += 1
        raise transient_error("persistent scope race")

    monkeypatch.setattr(IngestionService, "ingest", ingest)
    with build_container(config) as container:
        monkeypatch.setattr(
            container.codex_adapter,
            "discover_scopes",
            lambda: ("raced",),
        )
        operation = container.reconcile._codex_runs[0]
        result = operation()

    assert attempts == 2
    assert result.failure_count == 1


def test_reconcile_propagates_resolution_counts_and_explicit_warning_count(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    first_archive = inbox / "a.zip"
    second_archive = inbox / "b.zip"
    first_archive.write_bytes(b"first")
    second_archive.write_bytes(b"second")
    partial = CaptureResult(status="partial", inserted_ids=(uuid4(),))

    def import_chatgpt(path: Path) -> object:
        if path == first_archive:
            return SimpleNamespace(
                imported_count=1,
                duplicate_count=0,
                warnings=("resolution_not_found:3",),
                warning_count=3,
                resolved_count=5,
                already_resolved_count=6,
                unmatched_resolution_count=3,
            )
        return SimpleNamespace(
            imported_count=0,
            duplicate_count=1,
            warnings=("legacy_warning_a", "legacy_warning_b"),
            resolved_count=7,
            already_resolved_count=8,
            unmatched_resolution_count=0,
        )

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(
            lambda: SimpleNamespace(
                capture_results=(partial,),
                failure_count=0,
                warning_count=4,
                deferred_count=2,
                resolved_count=2,
                already_resolved_count=3,
                unmatched_resolution_count=4,
            ),
        ),
        chatgpt_import=import_chatgpt,
        chatgpt_inbox=lambda: (second_archive, first_archive),
    )

    report = service.run(force=True)
    stored = _stored_reconcile_report(database)
    metrics = stored["stage_metrics"]

    assert report.inserted_count == 2
    assert report.duplicate_count == 1
    assert report.warning_count == 9
    assert metrics["codex_0"] == {
        "already_resolved_count": 3,
        "deferred_count": 2,
        "duplicate_count": 0,
        "failure_count": 0,
        "inserted_count": 1,
        "resolved_count": 2,
        "unmatched_resolution_count": 4,
        "warning_count": 4,
    }
    assert metrics["chatgpt"] == {
        "already_resolved_count": 14,
        "archive_count": 2,
        "duplicate_count": 1,
        "failure_count": 0,
        "inserted_count": 1,
        "resolved_count": 12,
        "unmatched_resolution_count": 3,
        "warning_count": 5,
    }
    assert stored["stages"]["codex_0"] == "warn"
    assert stored["stage_errors"]["codex_0"] == "resolution_not_found"
    assert stored["stages"]["chatgpt"] == "warn"
    assert stored["stage_errors"]["chatgpt"] == "resolution_not_found"


def test_codex_deferred_warning_records_daily_success_without_catchup(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    run_at = datetime(2026, 7, 17, 3, 30, tzinfo=timezone.utc)
    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(
            lambda: SimpleNamespace(
                capture_results=(),
                failure_count=0,
                warning_count=1,
                deferred_count=1,
            ),
        ),
        now=lambda: run_at,
    )

    report = service.run(force=True)
    stored = _stored_reconcile_report(database)

    assert report.status == "degraded"
    assert report.warning_count == 1
    assert stored["stages"]["codex_0"] == "warn"
    assert stored["stage_metrics"]["codex_0"]["deferred_count"] == 1
    assert stored["stage_metrics"]["codex_0"]["failure_count"] == 0
    assert stored["stage_metrics"]["app_state"]["success_count"] == 1
    assert service.should_run(now=run_at + timedelta(hours=1)) is False


def test_reconcile_resolution_metrics_are_fixed_on_errors_and_disabled_stage(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    first_archive = tmp_path / "a.zip"
    second_archive = tmp_path / "b.zip"
    first_archive.write_bytes(b"first")
    second_archive.write_bytes(b"second")

    def import_chatgpt(path: Path) -> object:
        if path == first_archive:
            return SimpleNamespace(
                imported_count=1,
                duplicate_count=0,
                warnings=("resolution_not_found:2",),
                warning_count=2,
                resolved_count=0,
                already_resolved_count=0,
                unmatched_resolution_count=2,
            )
        raise RuntimeError("private")

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(lambda: (_ for _ in ()).throw(RuntimeError("private")),),
        chatgpt_import=import_chatgpt,
        chatgpt_inbox=lambda: (first_archive, second_archive),
    )
    service.run(force=True)
    stored = _stored_reconcile_report(database)

    for stage in ("codex_0", "chatgpt"):
        assert {
            "resolved_count",
            "already_resolved_count",
            "unmatched_resolution_count",
        } <= set(stored["stage_metrics"][stage])
    assert stored["stage_metrics"]["codex_0"]["resolved_count"] == 0
    assert stored["stage_metrics"]["chatgpt"]["unmatched_resolution_count"] == 2
    assert stored["stages"]["chatgpt"] == "error"
    assert stored["stage_errors"]["chatgpt"] == "archive_import_failed"

    disabled_root = tmp_path / "disabled"
    disabled_root.mkdir()
    disabled_database, disabled_runtime = _database(disabled_root)
    disabled = ReconcileService.minimal(
        disabled_database,
        ProcessLock(disabled_runtime / "lock"),
    )
    disabled.run(force=True)
    disabled_metrics = _stored_reconcile_report(disabled_database)["stage_metrics"]["chatgpt"]
    assert disabled_metrics["resolved_count"] == 0
    assert disabled_metrics["already_resolved_count"] == 0
    assert disabled_metrics["unmatched_resolution_count"] == 0


@pytest.mark.parametrize(
    ("higher", "lower", "expected_error"),
    (
        ("registry", "failure", "project_registry_reconcile_required"),
        ("failure", "resolution", "archive_import_failed"),
        ("resolution", "warning", "resolution_not_found"),
    ),
)
@pytest.mark.parametrize("reverse", (False, True))
def test_chatgpt_stage_error_priority_is_archive_order_independent(
    tmp_path: Path,
    higher: str,
    lower: str,
    expected_error: str,
    reverse: bool,
) -> None:
    database, runtime = _database(tmp_path)
    first_archive = tmp_path / "a.zip"
    second_archive = tmp_path / "b.zip"
    first_archive.write_bytes(b"first")
    second_archive.write_bytes(b"second")
    kinds = (lower, higher) if reverse else (higher, lower)
    by_archive = dict(zip((first_archive, second_archive), kinds, strict=True))

    def import_chatgpt(path: Path) -> object:
        kind = by_archive[path]
        if kind == "registry":
            raise ReconcileRequiredError("private")
        if kind == "failure":
            raise RuntimeError("private")
        if kind == "resolution":
            return SimpleNamespace(
                imported_count=0,
                duplicate_count=0,
                warnings=("resolution_not_found:2",),
                warning_count=2,
                resolved_count=0,
                already_resolved_count=0,
                unmatched_resolution_count=2,
            )
        return SimpleNamespace(
            imported_count=0,
            duplicate_count=0,
            warnings=("legacy_warning",),
            warning_count=1,
            resolved_count=0,
            already_resolved_count=0,
            unmatched_resolution_count=0,
        )

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        chatgpt_import=import_chatgpt,
        chatgpt_inbox=lambda: (second_archive, first_archive),
    )

    service.run(force=True)
    stored = _stored_reconcile_report(database)

    assert stored["stage_errors"]["chatgpt"] == expected_error


def test_process_lock_is_private_nonblocking_and_released(tmp_path):
    _database_value, runtime = _database(tmp_path)
    lock = ProcessLock(runtime / "reconcile.lock")

    with lock.acquire() as first:
        assert first.acquired is True
        assert stat.S_IMODE((runtime / "reconcile.lock").stat().st_mode) == 0o600
        with ProcessLock(runtime / "reconcile.lock").acquire() as second:
            assert second.acquired is False
            assert second.status == "already_running"
    (runtime / "reconcile.lock").chmod(0o666)
    with ProcessLock(runtime / "reconcile.lock").acquire() as third:
        assert third.acquired is True
        assert stat.S_IMODE((runtime / "reconcile.lock").stat().st_mode) == 0o600


def test_process_lock_rejects_symlink_and_releases_after_exception(tmp_path):
    _database_value, runtime = _database(tmp_path)
    target = runtime / "target"
    target.write_text("safe")
    (runtime / "linked.lock").symlink_to(target)

    with pytest.raises(PermissionError):
        with ProcessLock(runtime / "linked.lock").acquire():
            pass
    lock = ProcessLock(runtime / "reconcile.lock")
    with pytest.raises(RuntimeError):
        with lock.acquire() as outcome:
            assert outcome.acquired
            raise RuntimeError("synthetic")
    with lock.acquire() as recovered:
        assert recovered.acquired


def test_process_lock_contends_across_processes_and_rejects_hardlinks(
    tmp_path,
    monkeypatch,
):
    _database_value, runtime = _database(tmp_path)
    lock_path = runtime / "reconcile.lock"
    lock = ProcessLock(lock_path)
    code = "\n".join(
        (
            "from pathlib import Path",
            "from project_memory_hub.services.locking import ProcessLock",
            f"with ProcessLock(Path({str(lock_path)!r})).acquire() as result:",
            "    print(result.status)",
        )
    )

    with lock.acquire() as outcome:
        assert outcome.acquired
        child = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": str(Path.cwd() / "src")},
        )
    assert child.stdout.strip() == "already_running"

    hardlink = runtime / "hardlink.lock"
    os.link(lock_path, hardlink)
    with pytest.raises(PermissionError):
        with ProcessLock(hardlink).acquire():
            pass

    real_uid = os.getuid()
    monkeypatch.setattr(
        "project_memory_hub.services.locking.os.getuid",
        lambda: real_uid + 1,
    )
    with pytest.raises(PermissionError):
        with ProcessLock(runtime / "ownership.lock").acquire():
            pass


def test_retry_queue_redacts_omits_cwd_and_atomically_replays(tmp_path):
    database, _runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    redactor = Redactor()
    memories = MemoryRepository(database)
    capture = CaptureService(database, projects, memories, redactor)
    queue = RetryQueue(database, projects, redactor)
    secret = "SUPER_PRIVATE_RETRY_SECRET"
    payload = CapturePayload(
        cwd=project,
        namespace=Namespace(source_agent="codex", model_id="gpt-5"),
        source_record_id="retry-1",
        objective="retry task",
        outcome=f"password={secret}",
    )

    queue.enqueue(payload, "operational_failure")
    with database.connect(readonly=True) as connection:
        stored = connection.execute("select payload_json, reason_code from retry_items").fetchone()
    assert secret not in stored["payload_json"]
    assert str(project) not in stored["payload_json"]
    assert stored["reason_code"] == "operational_failure"

    report = queue.drain(capture)

    assert report.completed_count == 1
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from retry_items").fetchone()[0] == 0
        pending = connection.execute(
            "select structured_payload_json from pending_captures"
        ).fetchone()[0]
    assert secret not in pending
    assert "[REDACTED:password]" in pending


def test_should_run_uses_exact_24_hour_boundary_and_future_is_due(tmp_path):
    database, runtime = _database(tmp_path)
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"), now=lambda: now)

    service.record_success(now)

    assert service.should_run(now=now + timedelta(hours=23, minutes=59)) is False
    assert service.should_run(now=now + timedelta(hours=24)) is True
    assert service.should_run(now=now - timedelta(seconds=1)) is True


def test_malformed_success_state_is_due(tmp_path):
    database, runtime = _database(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            ("last_reconcile_success", "not-json", "2026-07-13T00:00:00Z"),
        )
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))

    assert service.should_run() is True


def test_schema_catchup_state_overrides_a_fresh_success_timestamp(tmp_path):
    database, runtime = _database(tmp_path)
    now = datetime(2026, 7, 13, tzinfo=timezone.utc)
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"), now=lambda: now)
    service.record_success(now)
    with database.transaction() as connection:
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            ("reconcile_catchup_required", '{"required":true}', now.isoformat()),
        )

    assert service.should_run(now=now) is True


@pytest.mark.parametrize("failure_mode", ("reported", "exception"))
def test_codex_parser_policy_catchup_remains_due_after_codex_failure(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    database, runtime = _database(tmp_path)
    run_at = datetime(2026, 7, 22, 3, 30, tzinfo=timezone.utc)
    with database.transaction() as connection:
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            (
                "codex_reconcile_catchup_required",
                '{"required":true}',
                run_at.isoformat(),
            ),
        )

    def codex_run() -> object:
        if failure_mode == "exception":
            raise RuntimeError("private")
        return SimpleNamespace(
            capture_results=(),
            deferred_count=0,
            failure_count=1,
            warning_count=0,
        )

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(codex_run,),
        now=lambda: run_at,
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert service.should_run(now=run_at + timedelta(hours=1)) is True
    with database.connect(readonly=True) as connection:
        dedicated_catchup = connection.execute(
            "select count(*) from app_state where name = 'codex_reconcile_catchup_required'"
        ).fetchone()[0]
        success_count = connection.execute(
            "select count(*) from app_state where name = 'last_reconcile_success'"
        ).fetchone()[0]
    assert dedicated_catchup == 1
    assert success_count == 0


def test_codex_parser_policy_catchup_remains_due_when_codex_is_disabled(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    run_at = datetime(2026, 7, 22, 3, 30, tzinfo=timezone.utc)
    with database.transaction() as connection:
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            (
                "codex_reconcile_catchup_required",
                '{"required":true}',
                run_at.isoformat(),
            ),
        )
    service = ReconcileService.minimal(
        database,
        ProcessLock(runtime / "lock"),
        now=lambda: run_at,
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert report.stages["codex_catchup"] == "warn"
    assert service.should_run(now=run_at + timedelta(hours=1)) is True


def test_successful_codex_parser_policy_catchup_clears_the_dedicated_marker(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    run_at = datetime(2026, 7, 22, 3, 30, tzinfo=timezone.utc)
    with database.transaction() as connection:
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            (
                "codex_reconcile_catchup_required",
                '{"required":true}',
                run_at.isoformat(),
            ),
        )
    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(
            lambda: SimpleNamespace(
                capture_results=(),
                deferred_count=0,
                failure_count=0,
                warning_count=0,
            ),
        ),
        now=lambda: run_at,
    )

    report = service.run(force=True)

    assert report.status == "success"
    assert service.should_run(now=run_at + timedelta(hours=1)) is False
    with database.connect(readonly=True) as connection:
        remaining = connection.execute(
            "select count(*) from app_state where name = 'codex_reconcile_catchup_required'"
        ).fetchone()[0]
    assert remaining == 0


def test_concurrent_codex_catchup_change_cannot_be_cleared_by_stale_completion(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    run_at = datetime(2026, 7, 22, 3, 30, tzinfo=timezone.utc)
    with database.transaction() as connection:
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            (
                "codex_reconcile_catchup_required",
                '{"required":true}',
                run_at.isoformat(),
            ),
        )

    def codex_run() -> object:
        with database.transaction() as connection:
            connection.execute(
                "update app_state set updated_at = ? where name = ?",
                (
                    (run_at + timedelta(minutes=1)).isoformat(),
                    "codex_reconcile_catchup_required",
                ),
            )
        return SimpleNamespace(
            capture_results=(),
            deferred_count=0,
            failure_count=0,
            warning_count=0,
        )

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(codex_run,),
        now=lambda: run_at,
    )

    report = service.run(force=True)

    assert report.status == "failed"
    assert report.stages["app_state"] == "error"
    assert service.should_run(now=run_at + timedelta(hours=1)) is True
    with database.connect(readonly=True) as connection:
        dedicated_catchup = connection.execute(
            "select updated_at from app_state where name = 'codex_reconcile_catchup_required'"
        ).fetchone()
        success_count = connection.execute(
            "select count(*) from app_state where name = 'last_reconcile_success'"
        ).fetchone()[0]
    assert dedicated_catchup is not None
    assert dedicated_catchup["updated_at"] == (run_at + timedelta(minutes=1)).isoformat()
    assert success_count == 0


def test_record_success_refuses_to_clear_untrusted_project_catchup(tmp_path: Path) -> None:
    database, runtime = _database(tmp_path)
    project_path = tmp_path / "untrusted-project"
    project_path.mkdir()
    project = ProjectRepository(database).register(
        ProjectCandidate(canonical_path=project_path, display_name="Untrusted")
    )
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = null, path_inode = null where project_id = ?",
            (str(project.project_id),),
        )
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            (
                "reconcile_catchup_required",
                '{"required":true}',
                "2026-07-13T00:00:00Z",
            ),
        )
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))

    with pytest.raises(RuntimeError, match="registry"):
        service.record_success(datetime(2026, 7, 13, tzinfo=timezone.utc))

    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from app_state where name = 'reconcile_catchup_required'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "select count(*) from app_state where name = 'last_reconcile_success'"
            ).fetchone()[0]
            == 0
        )


def test_record_success_rejects_in_process_identity_change_after_darwin_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, runtime = _database(tmp_path)
    project_path = tmp_path / "darwin-device-drift"
    project_path.mkdir()
    project = ProjectRepository(database).register(
        ProjectCandidate(canonical_path=project_path, display_name="Darwin drift")
    )
    metadata = project_path.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project.project_id)),
        )
    identities = iter(
        (
            (metadata.st_dev, metadata.st_ino),
            (metadata.st_dev + 2, metadata.st_ino),
        )
    )
    latest = (metadata.st_dev + 2, metadata.st_ino)

    def changing_identity(_path: Path):
        return next(identities, latest)

    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        path_identity_module,
        "complete_directory_identity",
        changing_identity,
    )
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))

    with pytest.raises(RuntimeError):
        service.record_success(datetime(2026, 7, 13, tzinfo=timezone.utc))


def test_record_success_accepts_stable_persisted_darwin_device_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, runtime = _database(tmp_path)
    project_path = tmp_path / "stable-darwin-device-drift"
    project_path.mkdir()
    project = ProjectRepository(database).register(
        ProjectCandidate(canonical_path=project_path, display_name="Stable drift")
    )
    metadata = project_path.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project.project_id)),
        )
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))

    service.record_success(datetime(2026, 7, 13, tzinfo=timezone.utc))

    assert service.should_run(now=datetime(2026, 7, 13, tzinfo=timezone.utc)) is False


def test_reconcile_keeps_catchup_due_while_enabled_project_identity_is_untrusted(tmp_path):
    database, runtime = _database(tmp_path)
    project_path = tmp_path / "legacy-project"
    project_path.mkdir()
    projects = ProjectRepository(database)
    project = projects.register(
        ProjectCandidate(canonical_path=project_path, display_name="Legacy")
    )
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = null, path_inode = null where project_id = ?",
            (str(project.project_id),),
        )
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            (
                "reconcile_catchup_required",
                '{"required":true}',
                "2026-07-13T00:00:00Z",
            ),
        )
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))

    blocked = service.run(force=True)

    assert blocked.status == "degraded"
    assert blocked.stages["project_registry"] == "warn"
    assert service.should_run() is True
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from app_state where name='reconcile_catchup_required'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "select count(*) from app_state where name='last_reconcile_success'"
            ).fetchone()[0]
            == 0
        )

    projects.set_enabled(project.project_id, False)
    recovered = service.run(force=True)

    assert recovered.status == "success"
    assert recovered.stages["project_registry"] == "pass"
    assert service.should_run() is False

    projects.set_enabled(project.project_id, True)

    assert service.should_run() is True


def test_reconcile_completion_does_not_clear_concurrent_project_registry_catchup(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    project_path = tmp_path / "legacy-disabled-project"
    project_path.mkdir()
    projects = ProjectRepository(database)
    project = projects.register(
        ProjectCandidate(canonical_path=project_path, display_name="Legacy disabled")
    )
    projects.set_enabled(project.project_id, False)
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = null, path_inode = null where project_id = ?",
            (str(project.project_id),),
        )
        connection.execute("delete from app_state where name = 'reconcile_catchup_required'")
    archive = tmp_path / "enable-project.zip"
    archive.write_bytes(b"not-read-by-test")

    def enable_during_import(_archive: Path) -> object:
        projects.set_enabled(project.project_id, True)
        return SimpleNamespace(imported_count=0, duplicate_count=0, warnings=())

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        chatgpt_import=enable_during_import,
        chatgpt_inbox=lambda: (archive,),
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert report.stages["project_registry"] == "warn"
    assert service.should_run() is True
    with database.connect(readonly=True) as connection:
        stored = connection.execute(
            "select enabled, path_device, path_inode from projects where project_id = ?",
            (str(project.project_id),),
        ).fetchone()
        assert tuple(stored) == (1, None, None)
        assert (
            connection.execute(
                "select count(*) from app_state where name = 'reconcile_catchup_required'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "select count(*) from app_state where name = 'last_reconcile_success'"
            ).fetchone()[0]
            == 0
        )


def test_retry_failure_rolls_back_capture_and_keeps_retry(tmp_path, monkeypatch):
    database, _runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    redactor = Redactor()
    capture = CaptureService(database, projects, MemoryRepository(database), redactor)
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(
        CapturePayload(
            cwd=project,
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id="retry-fail",
            objective="retry",
            outcome="done",
        ),
        "operational_failure",
    )

    def fail_after_write(connection, payload, project_id):
        result = CaptureService._capture_untrusted_on_connection(
            capture, connection, payload, project_id
        )
        assert result.status == "pending_verification"
        raise RuntimeError("synthetic")

    monkeypatch.setattr(capture, "_capture_untrusted_on_connection", fail_after_write)

    report = queue.drain(capture)

    assert report.failed_count == 1
    with database.connect(readonly=True) as connection:
        retry = connection.execute("select attempts from retry_items").fetchone()
        assert retry["attempts"] == 1
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0


def test_retry_queue_rejects_unsafe_reasons_and_bounds_persisted_rows(
    tmp_path,
    monkeypatch,
):
    database, _runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    redactor = Redactor()
    capture = CaptureService(database, projects, MemoryRepository(database), redactor)
    queue = RetryQueue(database, projects, redactor)
    payload = CapturePayload(
        cwd=project,
        namespace=Namespace(source_agent="codex", model_id="gpt-5"),
        source_record_id="retry-bounds",
        objective="retry",
        outcome="done",
    )

    for reason in ("project_not_found", "validation_failure", "privacy_rejection"):
        with pytest.raises(ValueError, match="unsupported retry reason"):
            queue.enqueue(payload, reason)

    with database.transaction() as connection:
        connection.execute(
            """
            insert into retry_items(
                retry_id, payload_json, reason_code, created_at, attempts
            ) values ('oversized', ?, 'operational_failure', ?, ?)
            """,
            (
                "x" * (256 * 1024 + 1),
                "2026-07-13T00:00:00Z",
                2**31 - 1,
            ),
        )

    def json_loads_must_not_run(_value):
        raise AssertionError("oversized row reached json parser")

    monkeypatch.setattr(
        "project_memory_hub.services.retry_queue.json.loads",
        json_loads_must_not_run,
    )
    report = queue.drain(capture)

    assert (
        report.completed_count,
        report.failed_count,
    ) == (0, 1)
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select attempts from retry_items where retry_id = 'oversized'"
            ).fetchone()[0]
            == 2**31 - 1
        )


def test_capture_production_path_queues_only_transient_failure_and_reraises(tmp_path):
    lock_database = tmp_path / "locked.db"
    holder = sqlite3.connect(lock_database)
    contender = sqlite3.connect(lock_database, timeout=0)
    try:
        holder.execute("create table sample(value text)")
        holder.commit()
        holder.execute("begin exclusive")
        holder.execute("insert into sample values ('held')")
        with pytest.raises(sqlite3.OperationalError) as captured:
            contender.execute("insert into sample values ('blocked')")
        transient_error = captured.value
    finally:
        contender.close()
        holder.rollback()
        holder.close()

    queued = []
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent="codex", model_id="gpt-5"),
        source_record_id="retry-production",
        objective="retry",
        outcome="done",
    )

    class FailingCapture:
        @staticmethod
        def capture(_payload):
            raise transient_error

    class RecordingQueue:
        @staticmethod
        def enqueue(received, reason):
            queued.append((received, reason))

    container = SimpleNamespace(
        capture=FailingCapture(),
        retry_queue=RecordingQueue(),
    )
    with pytest.raises(sqlite3.OperationalError) as reraised:
        _capture_with_transient_retry(container, payload)

    assert reraised.value is transient_error
    assert queued == [(payload, "operational_failure")]


def test_retry_queue_prioritizes_never_attempted_rows_after_poison_failures(tmp_path):
    database, _runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    redactor = Redactor()
    capture = CaptureService(database, projects, MemoryRepository(database), redactor)
    queue = RetryQueue(database, projects, redactor, max_items_per_drain=2)

    for index in range(3):
        queue.enqueue(
            CapturePayload(
                cwd=project,
                namespace=Namespace(source_agent="codex", model_id="gpt-5"),
                source_record_id=f"retry-fair-{index}",
                objective="retry",
                outcome="done",
            ),
            "operational_failure",
        )
    with database.transaction() as connection:
        poisoned = connection.execute(
            "select retry_id from retry_items order by created_at, retry_id limit 2"
        ).fetchall()
        connection.executemany(
            "update retry_items set payload_json = '{}' where retry_id = ?",
            ((row["retry_id"],) for row in poisoned),
        )

    first = queue.drain(capture)
    second = queue.drain(capture)

    assert (first.completed_count, first.failed_count) == (0, 2)
    assert (second.completed_count, second.failed_count) == (1, 1)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 1


def test_capture_transient_retry_persists_after_real_database_lock_releases(tmp_path):
    database, _runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    queue = RetryQueue(database, projects, Redactor())
    holder = sqlite3.connect(database.path, check_same_thread=False)
    contender = sqlite3.connect(database.path, timeout=0)
    try:
        holder.execute("begin immediate")
        with pytest.raises(sqlite3.OperationalError) as captured:
            contender.execute("begin immediate")
        transient_error = captured.value
        payload = CapturePayload(
            cwd=project,
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id="retry-real-lock",
            objective="retry",
            outcome="done",
        )

        class FailingCapture:
            @staticmethod
            def capture(_payload):
                raise transient_error

        releaser = threading.Thread(target=lambda: (time.sleep(0.05), holder.rollback()))
        releaser.start()
        with pytest.raises(sqlite3.OperationalError) as reraised:
            _capture_with_transient_retry(
                SimpleNamespace(capture=FailingCapture(), retry_queue=queue),
                payload,
            )
        releaser.join(timeout=1)
        assert not releaser.is_alive()
        assert reraised.value is transient_error
        holder.close()
        contender.close()
        with database.connect(readonly=True) as connection:
            assert connection.execute("select count(*) from retry_items").fetchone()[0] == 1
    finally:
        contender.close()
        holder.close()


def test_reconcile_orders_stages_and_isolates_optional_failures(tmp_path):
    database, runtime = _database(tmp_path)
    events = []
    project = type("Project", (), {})()

    def discover():
        events.append("discover")
        return (project,)

    def facts(_project):
        events.append("facts")
        return type("Facts", (), {"observed_count": 0})()

    def codex_fail():
        events.append("codex-fail")
        raise RuntimeError("private")

    def codex_ok():
        events.append("codex-ok")
        return type("Result", (), {"capture_results": ()})()

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("b.zip", "a.zip"):
        (inbox / name).write_bytes(b"x")

    def import_chatgpt(path):
        events.append(f"chatgpt:{path.name}")
        return type("Report", (), {"imported_count": 0, "duplicate_count": 0})()

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        discover=discover,
        scan_fact=facts,
        codex_runs=(codex_fail, codex_ok),
        chatgpt_import=import_chatgpt,
        chatgpt_inbox=lambda: tuple(inbox.iterdir()),
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert events == [
        "discover",
        "facts",
        "codex-fail",
        "codex-ok",
        "chatgpt:a.zip",
        "chatgpt:b.zip",
    ]
    assert service.should_run() is False


def test_reconcile_isolates_projects_and_scope_warnings_but_core_failure_wins(
    tmp_path,
):
    database, runtime = _database(tmp_path)
    events = []
    first = object()
    second = object()

    def scan(project):
        events.append("first" if project is first else "second")
        if project is first:
            raise RuntimeError("private path")
        return SimpleNamespace(observed_count=9, warnings=())

    def codex_scope_batch():
        events.append("codex")
        return SimpleNamespace(capture_results=(), warning_count=1)

    def inbox():
        events.append("chatgpt")
        return ()

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        discover=lambda: DiscoveryStageResult((first, second)),
        scan_fact=scan,
        codex_runs=(codex_scope_batch,),
        chatgpt_import=lambda _path: None,
        chatgpt_inbox=inbox,
    )

    report = service.run(force=True)

    assert report.status == "failed"
    assert report.inserted_count == 0
    assert events == ["first", "second", "codex", "chatgpt"]
    assert service.should_run() is True
    with database.connect(readonly=True) as connection:
        stored = json.loads(
            connection.execute(
                "select value_json from app_state where name = 'last_reconcile_report'"
            ).fetchone()[0]
        )
    assert stored["status"] == "failed"
    assert stored["stage_metrics"]["facts"]["failure_count"] == 1
    assert stored["stage_errors"]["facts"] == "project_fact_scan_failed"
    assert "private path" not in json.dumps(stored)


def test_reconcile_catches_inbox_failure_and_records_one_injected_timestamp(tmp_path):
    database, runtime = _database(tmp_path)
    expected_now = datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)
    calls = 0

    def now():
        nonlocal calls
        calls += 1
        return expected_now

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        chatgpt_import=lambda _path: None,
        chatgpt_inbox=lambda: (_ for _ in ()).throw(RuntimeError("private")),
        now=now,
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert calls == 1
    with database.connect(readonly=True) as connection:
        success = json.loads(
            connection.execute(
                "select value_json from app_state where name = 'last_reconcile_success'"
            ).fetchone()[0]
        )
        stored_report = json.loads(
            connection.execute(
                "select value_json from app_state where name = 'last_reconcile_report'"
            ).fetchone()[0]
        )
    assert success["timestamp"] == stored_report["timestamp"] == "2026-07-13T09:30:00Z"
    assert "private" not in json.dumps(stored_report)


def test_chatgpt_registry_drift_keeps_reconcile_catchup_due(tmp_path: Path) -> None:
    database, runtime = _database(tmp_path)
    now = datetime(2026, 7, 13, 9, 30, tzinfo=timezone.utc)
    archive = tmp_path / "drifted.zip"
    archive.write_bytes(b"not-read-by-test")

    def require_reconcile(_archive: Path) -> object:
        raise ReconcileRequiredError("project registry requires reconcile")

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        chatgpt_import=require_reconcile,
        chatgpt_inbox=lambda: (archive,),
        now=lambda: now,
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert report.stages["chatgpt"] == "error"
    assert service.should_run(now=now + timedelta(hours=1)) is True
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from app_state where name = 'reconcile_catchup_required'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "select count(*) from app_state where name = 'last_reconcile_success'"
            ).fetchone()[0]
            == 0
        )
        stored_report = json.loads(
            connection.execute(
                "select value_json from app_state where name = 'last_reconcile_report'"
            ).fetchone()[0]
        )
    assert stored_report["stage_errors"]["chatgpt"] == "project_registry_reconcile_required"


def test_reconcile_app_state_completion_is_atomic(tmp_path):
    database, runtime = _database(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger reject_reconcile_report
            before insert on app_state
            when NEW.name = 'last_reconcile_report'
            begin
                select raise(abort, 'private failure');
            end
            """
        )
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))

    report = service.run(force=True)

    assert report.status == "failed"
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from app_state where name in (?, ?)",
                ("last_reconcile_success", "last_reconcile_report"),
            ).fetchone()[0]
            == 0
        )


def test_expired_pending_moves_to_non_content_history_idempotently(tmp_path):
    database, runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())
    result = capture.capture(
        CapturePayload(
            cwd=project,
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id="pending-expire",
            objective="pending",
            outcome="done",
        )
    )
    assert result.status == "pending_verification"
    with database.transaction() as connection:
        connection.execute(
            "update pending_captures set expires_at = ?",
            ("2020-01-01T00:00:00Z",),
        )
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))

    service.run(force=True)
    service.run(force=True)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0
        history = connection.execute(
            """
            select final_state, expires_at, source_reference_id
            from pending_capture_history
            """
        ).fetchall()
        assert [row["final_state"] for row in history] == ["expired"]
        assert history[0]["expires_at"] == "2020-01-01T00:00:00Z"
        assert history[0]["source_reference_id"] is None
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert (
            connection.execute(
                "select count(*) from app_state where name like 'pending_confirmation:%'"
            ).fetchone()[0]
            == 0
        )


def test_adapter_verification_resolves_exactly_one_matching_pending_without_id_match(
    tmp_path,
):
    database, _runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())

    def payload(record_id):
        return CapturePayload(
            cwd=project,
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id=record_id,
            objective="same objective",
            outcome="same outcome",
        )

    capture.capture(payload("pending-a"))
    capture.capture(payload("pending-b"))
    verified_at = datetime.now(timezone.utc)
    verified = capture.capture(
        payload("trusted-record"),
        NamespaceVerification(
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id="trusted-record",
            verified_by="codex_adapter",
            verified_at=verified_at,
        ),
    )
    duplicate = capture.capture(
        payload("trusted-record"),
        NamespaceVerification(
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id="trusted-record",
            verified_by="codex_adapter",
            verified_at=verified_at,
        ),
    )

    assert verified.status == "inserted"
    assert duplicate.status == "duplicate"
    with database.connect(readonly=True) as connection:
        active_states = [
            row[0]
            for row in connection.execute(
                "select verification_state from pending_captures order by pending_id"
            ).fetchall()
        ]
        history_states = [
            row[0]
            for row in connection.execute(
                "select final_state from pending_capture_history order by pending_id"
            ).fetchall()
        ]
    assert active_states == ["pending"]
    assert history_states == ["verified"]


def test_second_reconcile_over_unchanged_codex_and_chatgpt_inputs_inserts_zero(
    tmp_path,
):
    database, runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    redactor = Redactor()
    capture = CaptureService(database, projects, MemoryRepository(database), redactor)
    checkpoints = CheckpointRepository(database)
    ingestion = IngestionService(capture, checkpoints)

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    session = sessions / "session.jsonl"
    session.write_text(
        "".join(
            json.dumps(value, separators=(",", ":")) + "\n"
            for value in (
                {
                    "timestamp": "2026-07-13T00:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "session-reconcile"},
                },
                {
                    "timestamp": "2026-07-13T00:00:01Z",
                    "type": "turn_context",
                    "payload": {
                        "turn_id": "turn-1",
                        "cwd": str(project),
                        "model": "gpt-5",
                        "summary": "fix cache",
                    },
                },
                {
                    "timestamp": "2026-07-13T00:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": (
                            "<!-- project-memory-hub:capture:v1:start -->\n"
                            "Objective: fix cache\n"
                            "Outcome: Codex fixed cache\n"
                            "<!-- project-memory-hub:capture:v1:end -->"
                        ),
                    },
                },
            )
        )
    )
    codex = CodexAdapter(sessions, redactor)

    def ingest_codex():
        results = []
        for scope in codex.discover_scopes():
            results.extend(ingestion.ingest(codex, scope).capture_results)
        return SimpleNamespace(capture_results=tuple(results), warning_count=0)

    chatgpt = ChatGPTExportAdapter(
        matcher=ProjectMatcher(database),
        extractor=ExplicitTaskExtractor(redactor),
        capture=capture,
        checkpoints=checkpoints,
        redactor=redactor,
    )
    archive = build_export(
        tmp_path / "export.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-reconcile",
                    user_text=f"In {project} fix chatgpt.py",
                    assistant_text="Outcome: ChatGPT fixed cache",
                )
            ]
        },
    )
    service = ReconcileService(
        database,
        ProcessLock(runtime / "reconcile.lock"),
        codex_runs=(ingest_codex,),
        chatgpt_import=chatgpt.import_zip,
        chatgpt_inbox=lambda: (archive,),
    )

    first = service.run(force=True)
    with database.connect(readonly=True) as connection:
        first_counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "behavior_memories",
                "project_facts",
                "import_receipts",
                "pending_captures",
            )
        ) + (
            connection.execute(
                "select count(*) from app_state where name like '%confirmation:%'"
            ).fetchone()[0],
        )
    second = service.run(force=True)
    with database.connect(readonly=True) as connection:
        second_counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "behavior_memories",
                "project_facts",
                "import_receipts",
                "pending_captures",
            )
        ) + (
            connection.execute(
                "select count(*) from app_state where name like '%confirmation:%'"
            ).fetchone()[0],
        )

    assert first.status == second.status == "success"
    assert first.inserted_count == 2
    assert second.inserted_count == 0
    assert second_counts == first_counts


def test_adapter_verification_does_not_resolve_pending_outside_time_window(tmp_path):
    database, _runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    capture = CaptureService(database, projects, MemoryRepository(database), Redactor())

    def payload(record_id):
        return CapturePayload(
            cwd=project,
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id=record_id,
            objective="bounded verification",
            outcome="done",
        )

    capture.capture(payload("old-pending"))
    with database.transaction() as connection:
        connection.execute("update pending_captures set created_at = '2020-01-01T00:00:00Z'")
    verified_at = datetime.now(timezone.utc)
    verified = capture.capture(
        payload("trusted-outside-window"),
        NamespaceVerification(
            namespace=Namespace(source_agent="codex", model_id="gpt-5"),
            source_record_id="trusted-outside-window",
            verified_by="codex_adapter",
            verified_at=verified_at,
        ),
    )

    assert verified.status == "inserted"
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute("select verification_state from pending_captures").fetchone()[0]
            == "pending"
        )


def _config(tmp_path, project):
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project,),
            enabled_sources=("chatgpt",),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    return config


def test_cli_if_due_skips_and_chatgpt_dry_run_writes_no_rows(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    config = _config(tmp_path, project)
    with build_container(config) as container:
        container.projects.register(
            ProjectCandidate(canonical_path=project, display_name="demo-repo")
        )
        container.reconcile.record_success()
    archive = build_export(
        tmp_path / "export.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-dry",
                    user_text=f"In {project} fix dry.py",
                    assistant_text="Outcome: dry",
                )
            ]
        },
    )

    skipped = runner.invoke(
        app,
        ["--config", str(config), "reconcile", "--if-due", "--format", "json"],
    )
    dry = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "import",
            "chatgpt",
            str(archive),
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert skipped.exit_code == dry.exit_code == 0
    assert json.loads(skipped.stdout)["status"] == "skipped"
    assert json.loads(dry.stdout)["dry_run"] is True
    with build_container(config) as container:
        with container.database.connect(readonly=True) as connection:
            assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
            assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0


def test_cli_chatgpt_dry_run_preserves_runtime_metadata_and_never_initializes(
    tmp_path,
):
    project = tmp_path / "demo-repo"
    project.mkdir()
    config = _config(tmp_path, project)
    with build_container(config) as container:
        container.projects.register(
            ProjectCandidate(canonical_path=project, display_name="demo-repo")
        )
    archive = build_export(
        tmp_path / "export.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-metadata",
                    user_text=f"In {project} fix metadata.py",
                    assistant_text="Outcome: unchanged",
                )
            ]
        },
    )
    runtime = config.parent
    tracked = (
        config,
        runtime / "memory.db",
        runtime / "memory.db-wal",
        runtime / "memory.db-shm",
    )
    before = _metadata_snapshot(runtime, tracked)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "import",
            "chatgpt",
            str(archive),
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert _metadata_snapshot(runtime, tracked) == before

    absent_runtime = tmp_path / "absent-runtime"
    absent = runner.invoke(
        app,
        [
            "--config",
            str(absent_runtime / "config.toml"),
            "import",
            "chatgpt",
            str(archive),
            "--dry-run",
            "--format",
            "json",
        ],
    )
    assert absent.exit_code == 2
    assert json.loads(absent.stdout)["error"]["code"] == "permission_denied"
    assert not absent_runtime.exists()


def test_disabled_chatgpt_import_and_invalid_reconcile_flags_write_nothing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project,),
            enabled_sources=("codex",),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    archive = tmp_path / "export.zip"
    archive.write_bytes(b"not read")
    before = _metadata_snapshot(config.parent, (config,))

    disabled = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "import",
            "chatgpt",
            str(archive),
            "--format",
            "json",
        ],
    )

    assert disabled.exit_code == 2
    assert json.loads(disabled.stdout)["error"]["code"] == "source_disabled"
    assert _metadata_snapshot(config.parent, (config,)) == before

    absent_runtime = tmp_path / "invalid-flags-runtime"
    invalid = runner.invoke(
        app,
        [
            "--config",
            str(absent_runtime / "config.toml"),
            "reconcile",
            "--force",
            "--if-due",
            "--format",
            "json",
        ],
    )
    assert invalid.exit_code == 4
    assert not absent_runtime.exists()


def test_if_due_checks_freshness_after_process_lock(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = _config(tmp_path, project)
    with build_container(config) as container:
        container.reconcile.record_success()
        with container.process_lock.acquire() as locked:
            assert locked.acquired
            result = runner.invoke(
                app,
                [
                    "--config",
                    str(config),
                    "reconcile",
                    "--if-due",
                    "--format",
                    "json",
                ],
            )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "already_running"


def test_failed_reconcile_cli_is_nonzero_and_generic(monkeypatch, tmp_path):
    marker = "PRIVATE_RECONCILE_FAILURE"
    fake = SimpleNamespace(
        reconcile=SimpleNamespace(
            run=lambda force: ReconcileReport(
                run_id=uuid4(),
                status="failed",
                warning_count=1,
                stages={"facts": "error"},
            )
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)

    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / marker / "config.toml"),
            "reconcile",
            "--force",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert marker not in result.stdout


def test_chatgpt_import_cli_omits_archive_controlled_identifiers_and_paths(tmp_path):
    project = tmp_path / "private-project-marker"
    project.mkdir()
    config = _config(tmp_path, project)
    with build_container(config) as container:
        container.projects.register(
            ProjectCandidate(canonical_path=project, display_name=project.name)
        )
    marker = "PRIVATE_CONVERSATION_ID_MARKER"
    archive = build_export(
        tmp_path / "private-archive-marker.zip",
        {
            "conversations.json": [
                conversation(
                    marker,
                    user_text=f"In {project} fix output.py",
                    assistant_text="Outcome: output fixed",
                )
            ]
        },
    )

    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "import",
            "chatgpt",
            str(archive),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)
    assert output == {
        "already_resolved_count": 0,
        "confirmation_count": 0,
        "dry_run": False,
        "duplicate_count": 0,
        "imported_count": 1,
        "resolved_count": 0,
        "status": "ok",
        "unmatched_resolution_count": 0,
        "warning_count": 0,
    }
    assert marker not in result.stdout
    assert str(project) not in result.stdout
    assert archive.name not in result.stdout


def test_chatgpt_dry_run_fails_closed_on_live_wal_without_metadata_changes(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = _config(tmp_path, project)
    with build_container(config):
        pass
    database_path = config.parent / "memory.db"
    writer = sqlite3.connect(database_path)
    try:
        writer.execute("pragma wal_autocheckpoint=0")
        writer.execute(
            "insert into app_state(name, value_json, updated_at) values (?, '{}', ?)",
            ("live-wal", "2026-07-13T00:00:00Z"),
        )
        writer.commit()
        tracked = (
            config,
            database_path,
            Path(f"{database_path}-wal"),
            Path(f"{database_path}-shm"),
        )
        before = _metadata_snapshot(config.parent, tracked)

        result = runner.invoke(
            app,
            [
                "--config",
                str(config),
                "import",
                "chatgpt",
                str(tmp_path / "unread.zip"),
                "--dry-run",
                "--format",
                "json",
            ],
        )

        assert result.exit_code == 1
        assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
        assert _metadata_snapshot(config.parent, tracked) == before
    finally:
        writer.close()


def _metadata_snapshot(root, tracked):
    paths = tuple(sorted(str(path.relative_to(root)) for path in root.rglob("*")))
    metadata = {}
    for path in tracked:
        if not path.exists():
            metadata[path.name] = None
            continue
        value = path.stat()
        metadata[path.name] = (
            value.st_ino,
            value.st_size,
            value.st_mtime_ns,
            stat.S_IMODE(value.st_mode),
        )
    return paths, metadata
