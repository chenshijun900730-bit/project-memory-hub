from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import project_memory_hub.adapters.chatgpt as chatgpt_module
import project_memory_hub.services.reconcile as reconcile_module
from project_memory_hub.adapters.chatgpt import (
    ChatGPTExportAdapter,
    ExplicitTaskExtractor,
    ProjectMatcher,
)
from project_memory_hub.domain import ProjectCandidate, ReconcileReport
from project_memory_hub.security.archive import UnsafeArchiveError
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.services.reconcile import ReconcileService
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository
from tests.fixtures.chatgpt.build_fixtures import build_export, conversation


def _database(tmp_path: Path, name: str = "runtime") -> tuple[Database, Path]:
    runtime = tmp_path / name
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    return database, runtime


def _adapter(
    tmp_path: Path,
    project: Path,
    **limits: int,
) -> tuple[Database, ChatGPTExportAdapter]:
    database, _runtime = _database(tmp_path)
    projects = ProjectRepository(database)
    projects.register(
        ProjectCandidate(
            canonical_path=project,
            display_name=project.name,
        )
    )
    redactor = Redactor()
    capture = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        redactor,
    )
    adapter = ChatGPTExportAdapter(
        matcher=ProjectMatcher(database),
        extractor=ExplicitTaskExtractor(redactor),
        capture=capture,
        checkpoints=CheckpointRepository(database),
        redactor=redactor,
        database=database,
        **limits,
    )
    return database, adapter


def _stored_report(database: Database) -> dict[str, object]:
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select value_json from app_state where name = 'last_reconcile_report'"
        ).fetchone()
    assert row is not None
    return json.loads(row["value_json"])


@pytest.mark.parametrize(
    ("limit_name", "invalid"),
    [
        ("max_numbered_members", 0),
        ("max_conversations", True),
        ("max_nodes_per_conversation", -1),
        ("max_conversation_bytes", 0),
    ],
)
def test_chatgpt_adapter_rejects_nonpositive_or_noninteger_limits(
    tmp_path: Path,
    limit_name: str,
    invalid: int,
) -> None:
    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="positive integers"):
        _adapter(tmp_path, project, **{limit_name: invalid})


def test_chatgpt_archive_count_limit_rejects_before_any_receipt_or_memory(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database, adapter = _adapter(tmp_path, project, max_conversations=1)
    archive = build_export(
        tmp_path / "too-many.zip",
        {
            "conversations.json": [
                conversation(
                    "first",
                    user_text=f"In {project} fix first.py",
                    assistant_text="Outcome: fixed first",
                ),
                conversation(
                    "second",
                    user_text=f"In {project} fix second.py",
                    assistant_text="Outcome: fixed second",
                ),
            ]
        },
    )

    with pytest.raises(UnsafeArchiveError, match="conversation count limit exceeded"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0


def test_chatgpt_node_and_text_limits_are_receipted_without_blocking_valid_work(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database, adapter = _adapter(
        tmp_path,
        project,
        max_nodes_per_conversation=2,
        max_conversation_bytes=300,
    )
    too_many_nodes = conversation(
        "too-many-nodes",
        user_text=f"In {project} fix nodes.py",
        assistant_text="Outcome: unreachable",
    )
    too_many_nodes["mapping"]["extra"] = {
        "id": "extra",
        "parent": None,
        "children": [],
        "message": None,
    }
    too_much_text = conversation(
        "too-much-text",
        user_text=f"In {project} fix text.py " + ("x" * 1_000),
        assistant_text="Outcome: unreachable",
    )
    valid = conversation(
        "valid",
        user_text=f"In {project} fix ok.py",
        assistant_text="Outcome: fixed",
    )
    archive = build_export(
        tmp_path / "bounded.zip",
        {"conversations.json": [too_many_nodes, too_much_text, valid]},
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    assert report.processed_conversation_ids == (
        "too-many-nodes",
        "too-much-text",
        "valid",
    )
    assert set(report.warnings) == {
        "conversation_node_limit:1",
        "conversation_text_limit:1",
    }
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 3
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_chatgpt_normalizer_exceptions_are_isolated_from_later_conversations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    database, adapter = _adapter(tmp_path, project)
    archive = build_export(
        tmp_path / "normalizer-exception.zip",
        {
            "conversations.json": [
                conversation(
                    "broken",
                    user_text=f"In {project} fix broken.py",
                    assistant_text="Outcome: unreachable",
                ),
                conversation(
                    "valid",
                    user_text=f"In {project} fix valid.py",
                    assistant_text="Outcome: fixed",
                ),
            ]
        },
    )
    original = adapter._normalize_conversation

    def normalize(value: object):
        if isinstance(value, dict) and value.get("id") == "broken":
            raise OverflowError("PRIVATE_NORMALIZER_MARKER")
        return original(value)

    monkeypatch.setattr(adapter, "_normalize_conversation", normalize)

    report = adapter.import_zip(archive, dry_run=dry_run)

    assert report.imported_count == 1
    assert report.warnings == ("malformed_conversation:1",)
    assert "PRIVATE_NORMALIZER_MARKER" not in repr(report)
    with database.connect(readonly=True) as connection:
        expected_receipts = 0 if dry_run else 2
        expected_memories = 0 if dry_run else 1
        assert (
            connection.execute("select count(*) from import_receipts").fetchone()[0]
            == expected_receipts
        )
        assert (
            connection.execute("select count(*) from behavior_memories").fetchone()[0]
            == expected_memories
        )


@pytest.mark.parametrize(
    "corruption",
    [
        "non_text_node_id",
        "non_mapping_node",
        "mismatched_node_id",
        "non_text_child",
        "non_text_parent",
        "empty_mapping",
    ],
)
def test_chatgpt_normalizer_rejects_malformed_node_shapes(
    tmp_path: Path,
    corruption: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _database_value, adapter = _adapter(tmp_path, project)
    value = conversation(
        "malformed",
        user_text="Fix malformed nodes",
        assistant_text="Outcome: unreachable",
    )
    value = copy.deepcopy(value)
    if corruption == "non_text_node_id":
        value["mapping"][1] = value["mapping"].pop("u1")
    elif corruption == "non_mapping_node":
        value["mapping"]["u1"] = []
    elif corruption == "mismatched_node_id":
        value["mapping"]["u1"]["id"] = "other"
    elif corruption == "non_text_child":
        value["mapping"]["u1"]["children"] = [1]
    elif corruption == "non_text_parent":
        value["mapping"]["a1"]["parent"] = 1
    else:
        value["mapping"] = {}

    with pytest.raises(chatgpt_module._ConversationError) as raised:
        adapter._normalize_conversation(value)

    assert raised.value.code == "malformed_conversation"


@pytest.mark.parametrize(
    "message_corruption",
    ["missing_message", "invalid_author", "invalid_parts", "empty_parts", "unsupported_role"],
)
def test_chatgpt_normalizer_ignores_nonvisible_or_malformed_message_payloads(
    tmp_path: Path,
    message_corruption: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _database_value, adapter = _adapter(tmp_path, project)
    value = conversation(
        "malformed-message",
        user_text="Fix message",
        assistant_text="Outcome: unreachable",
    )
    for node in value["mapping"].values():
        message = node["message"]
        if message_corruption == "missing_message":
            node["message"] = None
        elif message_corruption == "invalid_author":
            message["author"] = None
        elif message_corruption == "invalid_parts":
            message["content"]["parts"] = None
        elif message_corruption == "empty_parts":
            message["content"]["parts"] = [None]
        else:
            message["author"]["role"] = "tool"

    with pytest.raises(chatgpt_module._ConversationError) as raised:
        adapter._normalize_conversation(value)

    assert raised.value.code == "malformed_conversation"


@pytest.mark.parametrize(
    ("title", "code"),
    [
        ("invalid surrogate: \ud800", "invalid_unicode"),
        ("unsafe control: \x00", "unsafe_text_control"),
    ],
)
def test_chatgpt_normalizer_rejects_invalid_unicode_and_controls(
    tmp_path: Path,
    title: str,
    code: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _database_value, adapter = _adapter(tmp_path, project)
    value = conversation(
        "invalid-title",
        title=title,
        user_text="Fix title",
        assistant_text="Outcome: unreachable",
    )

    with pytest.raises(chatgpt_module._ConversationError) as raised:
        adapter._normalize_conversation(value)

    assert raised.value.code == code


def test_reconcile_fails_closed_for_lock_clock_and_schedule_exceptions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, runtime = _database(tmp_path)

    class BrokenLock:
        @contextmanager
        def acquire(self):
            raise RuntimeError("private lock failure")
            yield

    lock_report = ReconcileService(database, BrokenLock()).run(force=True)  # type: ignore[arg-type]
    assert lock_report.status == "failed"
    assert lock_report.stages == {"lock": "error"}

    clock_service = ReconcileService(
        database,
        ProcessLock(runtime / "clock.lock"),
        now=lambda: datetime(2026, 7, 18),
    )
    clock_report = clock_service.run(force=True)
    assert clock_report.status == "failed"
    assert clock_report.stages == {"clock": "error"}

    schedule_service = ReconcileService(
        database,
        ProcessLock(runtime / "schedule.lock"),
        now=lambda: datetime(2026, 7, 18, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        schedule_service,
        "should_run",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("private schedule failure")),
    )
    schedule_report = schedule_service.run()
    assert schedule_report.status == "failed"
    assert schedule_report.stages == {"schedule": "error"}
    assert "private" not in repr((lock_report, clock_report, schedule_report))


def test_reconcile_project_registry_and_pending_failures_are_sanitized_and_due(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, runtime = _database(tmp_path)
    service = ReconcileService.minimal(database, ProcessLock(runtime / "lock"))
    monkeypatch.setattr(
        service,
        "_enabled_project_registry_snapshot",
        lambda: (_ for _ in ()).throw(RuntimeError("PRIVATE_REGISTRY_MARKER")),
    )
    monkeypatch.setattr(
        service,
        "_expire_pending",
        lambda _now: (_ for _ in ()).throw(RuntimeError("PRIVATE_PENDING_MARKER")),
    )

    report = service.run(force=True)

    assert report.status == "failed"
    assert report.stages["project_registry"] == "error"
    assert report.stages["pending"] == "error"
    assert service.should_run() is True
    stored = _stored_report(database)
    assert stored["stage_errors"]["project_registry"] == "project_identity_check_failed"
    assert stored["stage_errors"]["pending"] == "pending_expiry_failed"
    assert "PRIVATE_" not in json.dumps(stored)


def test_reconcile_preserves_partial_chatgpt_counts_when_one_archive_fails(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    archives = tuple(tmp_path / name for name in ("c.zip", "a.zip", "b.zip"))
    calls: list[str] = []

    def import_archive(path: Path) -> object:
        calls.append(path.name)
        if path.name == "b.zip":
            raise RuntimeError("PRIVATE_ARCHIVE_MARKER")
        if path.name == "a.zip":
            return SimpleNamespace(
                imported_count=2,
                duplicate_count=1,
                warning_count=1,
                resolved_count=1,
                already_resolved_count=2,
                unmatched_resolution_count=0,
            )
        return SimpleNamespace(
            imported_count=3,
            duplicate_count=0,
            warning_count=0,
            resolved_count=4,
            already_resolved_count=0,
            unmatched_resolution_count=0,
        )

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        chatgpt_import=import_archive,
        chatgpt_inbox=lambda: archives,
    )

    report = service.run(force=True)

    assert calls == ["a.zip", "b.zip", "c.zip"]
    assert report.status == "degraded"
    assert report.inserted_count == 5
    assert report.duplicate_count == 1
    assert report.stages["chatgpt"] == "error"
    stored = _stored_report(database)
    assert stored["stage_metrics"]["chatgpt"] == {
        "already_resolved_count": 2,
        "archive_count": 3,
        "duplicate_count": 1,
        "failure_count": 1,
        "inserted_count": 5,
        "resolved_count": 5,
        "unmatched_resolution_count": 0,
        "warning_count": 1,
    }
    assert stored["stage_errors"]["chatgpt"] == "archive_import_failed"
    assert "PRIVATE_ARCHIVE_MARKER" not in json.dumps(stored)


@pytest.mark.parametrize("mode", ["reported_failure", "exception"])
def test_reconcile_compaction_failures_stay_due_and_expose_only_fixed_codes(
    tmp_path: Path,
    mode: str,
) -> None:
    database, runtime = _database(tmp_path)

    def compact(_now: datetime) -> object:
        if mode == "exception":
            raise RuntimeError("PRIVATE_COMPACTION_MARKER")
        return SimpleNamespace(
            cold_count=3,
            failure_count=2,
            namespace_count=4,
            project_count=5,
            remaining_count=0,
            retrospective_count=6,
            source_count=7,
        )

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        compact=compact,
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert report.stages["compaction"] == "error"
    assert service.should_run() is True
    stored = _stored_report(database)
    expected_failure_count = 1 if mode == "exception" else 2
    assert stored["stage_metrics"]["compaction"]["failure_count"] == expected_failure_count
    assert stored["stage_errors"]["compaction"] == "compaction_failed"
    assert "PRIVATE_COMPACTION_MARKER" not in json.dumps(stored)


def test_reconcile_report_value_drops_unsafe_stages_metrics_and_errors() -> None:
    report = ReconcileReport.model_construct(
        run_id=uuid4(),
        status="failed",
        inserted_count=-1,
        duplicate_count=True,
        warning_count=2**40,
        stages={
            "ok": "warn",
            "bad-name": "error",
            "bad_status": "unknown",
            "x" * 65: "pass",
        },
    )

    value = reconcile_module._report_value(
        report,
        "2026-07-18T00:00:00Z",
        {
            "ok": {"good": 3, "negative": -1, "boolean": True, "bad-name": 9},
            "missing": {"good": 7},
            "bad_status": [],  # type: ignore[dict-item]
        },
        {
            "ok": "safe_error",
            "missing": "safe_error",
            "bad-name": "unsafe",
            "bad_status": "x" * 65,
        },
    )

    assert value["inserted_count"] == 0
    assert value["duplicate_count"] == 0
    assert value["warning_count"] == 2**31 - 1
    assert value["stages"] == {"ok": "warn"}
    assert value["stage_metrics"] == {"ok": {"boolean": 0, "good": 3, "negative": 0}}
    assert value["stage_errors"] == {"ok": "safe_error"}


def test_reconcile_defensive_helpers_reject_malformed_inputs() -> None:
    class BrokenLength:
        def __len__(self) -> int:
            raise OverflowError

    assert reconcile_module._sequence_count("three") == 0
    assert reconcile_module._sequence_count(BrokenLength()) == 0
    with pytest.raises(ValueError, match="timezone-aware"):
        reconcile_module._utc(datetime(2026, 7, 18))
    with pytest.raises(ValueError, match="timestamp must be text"):
        reconcile_module._parse_timestamp(None)
