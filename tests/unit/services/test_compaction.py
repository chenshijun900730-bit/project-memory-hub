from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
import project_memory_hub.services.compaction as compaction_module
import project_memory_hub.storage.database as database_module
from project_memory_hub.cli import app
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    CapturePayload,
    MemoryKind,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.compaction import (
    CompactionBoundsError,
    CompactionService,
)
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.services.project_facts import ProjectFactService
from project_memory_hub.services.reconcile import ReconcileService
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository


UTC = timezone.utc
NOW = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
CODEX = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5")
runner = CliRunner()


class TracingDatabase:
    def __init__(self, path: Path) -> None:
        self._database = Database(path)
        self.path = self._database.path
        self.traces: list[str] = []

    def initialize(self) -> None:
        self._database.initialize()

    @contextmanager
    def connect(self, readonly: bool = False):
        with self._database.connect(readonly=readonly) as connection:
            connection.set_trace_callback(self.traces.append)
            yield connection

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


class ProgressDatabase:
    def __init__(self, database: Database, *, max_callbacks: int = 500) -> None:
        self._database = database
        self.path = database.path
        self.max_callbacks = max_callbacks
        self.callbacks = 0

    @contextmanager
    def connect(self, readonly: bool = False):
        with self._database.connect(readonly=readonly) as connection:
            connection.set_progress_handler(self._progress, 100)
            try:
                yield connection
            finally:
                connection.set_progress_handler(None, 0)

    @contextmanager
    def transaction(self):
        with self._database.transaction() as connection:
            connection.set_progress_handler(self._progress, 100)
            try:
                yield connection
            finally:
                connection.set_progress_handler(None, 0)

    def _progress(self) -> int:
        self.callbacks += 1
        return int(self.callbacks > self.max_callbacks)


def _stack(tmp_path: Path, *, tracing: bool = False):
    database = (
        TracingDatabase(tmp_path / "memory.db") if tracing else Database(tmp_path / "memory.db")
    )
    database.initialize()
    root = tmp_path / "project"
    root.mkdir()
    projects = ProjectRepository(database)
    project = projects.register(ProjectCandidate(canonical_path=root, display_name="project"))
    memories = MemoryRepository(database)
    service = CompactionService(
        database,
        memories,
        Redactor(),
        inactive_days=21,
        now=lambda: NOW,
    )
    return database, root, projects, project, memories, service


def _set_activity(database, project_id: UUID, value: str | None, state="active"):
    with database.transaction() as connection:
        connection.execute(
            "update projects set last_observed_change = ?, inactivity_state = ? "
            "where project_id = ?",
            (value, state, str(project_id).lower()),
        )


def _source_ref(database, source_agent: SourceAgent, label: str) -> UUID:
    source_reference_id = uuid4()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, ?, ?, null, ?, ?, 'test-v1', ?)
            """,
            (
                str(source_reference_id).lower(),
                source_agent.value,
                label,
                hashlib.sha256(label.encode()).hexdigest(),
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    return source_reference_id


def _insert_memory(
    database,
    memories: MemoryRepository,
    project_id: UUID,
    namespace: Namespace,
    kind: MemoryKind,
    content: str,
    label: str,
    *,
    created_at: datetime = NOW,
):
    return memories.insert(
        BehaviorMemoryInput(
            project_id=project_id,
            namespace=namespace,
            task_fingerprint=hashlib.sha256(f"task:{label}".encode()).hexdigest(),
            memory_kind=kind,
            normalized_content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            source_reference_id=_source_ref(database, namespace.source_agent, f"source:{label}"),
            created_at=created_at,
            confidence=1.0,
        )
    )


def _rows(database, project_id: UUID):
    with database.connect(readonly=True) as connection:
        return connection.execute(
            "select * from behavior_memories where project_id = ? "
            "order by source_agent, model_id, memory_kind, created_at, memory_id",
            (str(project_id).lower(),),
        ).fetchall()


def _bulk_insert(
    database,
    project_id: UUID,
    namespace: Namespace,
    kind: MemoryKind,
    count: int,
    *,
    prefix: str,
) -> None:
    with database.transaction() as connection:
        for index in range(count):
            source_reference_id = str(uuid4()).lower()
            memory_id = str(uuid4()).lower()
            content = f"{prefix} {index}"
            content_hash = hashlib.sha256(content.encode()).hexdigest()
            connection.execute(
                """
                insert into source_refs(
                    source_reference_id, source_agent, source_record_id,
                    source_path, content_hash, source_timestamp,
                    parser_version, created_at
                ) values (?, ?, ?, null, ?, ?, 'test-v1', ?)
                """,
                (
                    source_reference_id,
                    namespace.source_agent.value,
                    f"{prefix}-source-{index}",
                    hashlib.sha256(f"source-{index}".encode()).hexdigest(),
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )
            connection.execute(
                """
                insert into behavior_memories(
                    memory_id, project_id, source_agent, model_id,
                    task_fingerprint, memory_kind, normalized_content,
                    content_hash, source_reference_id, created_at,
                    confidence, lifecycle_state
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1.0, 'active')
                """,
                (
                    memory_id,
                    str(project_id).lower(),
                    namespace.source_agent.value,
                    namespace.model_id,
                    hashlib.sha256(f"task-{prefix}-{index}".encode()).hexdigest(),
                    kind.value,
                    content,
                    content_hash,
                    source_reference_id,
                    NOW.isoformat(),
                ),
            )


@pytest.mark.parametrize(
    ("age", "expected"),
    (
        (timedelta(days=20, seconds=86399), False),
        (timedelta(days=21), True),
        (timedelta(days=22), True),
    ),
)
def test_find_inactive_uses_exact_aware_utc_boundary(tmp_path, age, expected):
    database, _root, _projects, project, _memories, service = _stack(tmp_path)
    observed = NOW - age
    offset = timezone(timedelta(hours=8))
    _set_activity(
        database,
        project.project_id,
        observed.astimezone(offset).isoformat(),
    )

    found = service.find_inactive(NOW)

    assert (found == [found[0]]) is expected if expected else found == []
    if expected:
        assert found[0].project_id == project.project_id
        assert found[0].last_observed_change == observed


@pytest.mark.parametrize(
    "unsafe_value",
    (None, "not-a-time", "2026-06-01T00:00:00", "2026-07-14T00:00:00Z"),
)
def test_find_inactive_conservatively_skips_unknown_naive_malformed_or_future(
    tmp_path, unsafe_value
):
    database, _root, _projects, project, _memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, unsafe_value)

    assert service.find_inactive(NOW) == []


def test_verified_capture_advances_observation_but_duplicate_and_pending_do_not(
    tmp_path,
):
    database, root, projects, project, memories, _service = _stack(tmp_path)
    capture = CaptureService(database, projects, memories, Redactor())
    payload = CapturePayload(
        cwd=root,
        namespace=CODEX,
        source_record_id="capture-1",
        objective="fix cache",
        outcome="cache fixed",
        verified_commands=["uv run pytest"],
    )
    verification = NamespaceVerification(
        namespace=CODEX,
        source_record_id="capture-1",
        verified_by="codex_adapter",
        verified_at=NOW,
    )

    pending = capture.capture(payload)
    with database.connect(readonly=True) as connection:
        after_pending = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    inserted = capture.capture(payload, verification)
    with database.connect(readonly=True) as connection:
        first_observed = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    duplicate = capture.capture(payload, verification)
    with database.connect(readonly=True) as connection:
        second_observed = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    assert pending.status == "pending_verification"
    assert after_pending is None
    assert inserted.status == "inserted"
    assert first_observed is not None
    assert duplicate.status == "duplicate"
    assert second_observed == first_observed
    observed = datetime.fromisoformat(first_observed.replace("Z", "+00:00"))
    real_service = CompactionService(
        database,
        memories,
        Redactor(),
        inactive_days=21,
        now=lambda: observed + timedelta(days=21),
    )
    assert [
        item.project_id for item in real_service.find_inactive(observed + timedelta(days=21))
    ] == [project.project_id]


def test_observation_api_rejects_naive_future_and_does_not_move_backwards(tmp_path):
    database, _root, projects, project, _memories, _service = _stack(tmp_path)
    old = NOW - timedelta(days=2)

    assert projects.advance_last_observed_change(project.project_id, old, as_of=NOW)
    assert not projects.advance_last_observed_change(
        project.project_id, NOW.replace(tzinfo=None), as_of=NOW
    )
    assert not projects.advance_last_observed_change(
        project.project_id, NOW + timedelta(seconds=1), as_of=NOW
    )
    assert not projects.advance_last_observed_change(
        project.project_id, old - timedelta(seconds=1), as_of=NOW
    )
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()
    assert datetime.fromisoformat(row[0].replace("Z", "+00:00")) == old


def test_equal_timestamp_verified_insert_reactivates_inactive_project_atomically(
    tmp_path,
):
    database, root, projects, project, memories, _service = _stack(tmp_path)
    _set_activity(database, project.project_id, NOW.isoformat(), state="inactive")
    payload = CapturePayload(
        cwd=root,
        namespace=CODEX,
        source_record_id="equal-time-insert",
        objective="resume project",
        outcome="resumed",
        open_issues=["fresh active issue"],
    )
    verification = NamespaceVerification(
        namespace=CODEX,
        source_record_id="equal-time-insert",
        verified_by="codex_adapter",
        verified_at=NOW,
    )

    result = CaptureService(
        database,
        projects,
        memories,
        Redactor(),
        now=lambda: NOW,
    ).capture(payload, verification)

    with database.connect(readonly=True) as connection:
        state = connection.execute(
            "select last_observed_change, inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()
        active_count = connection.execute(
            "select count(*) from behavior_memories "
            "where project_id = ? and lifecycle_state = 'active'",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    assert result.status == "inserted"
    assert state["inactivity_state"] == "active"
    assert datetime.fromisoformat(state["last_observed_change"].replace("Z", "+00:00")) == NOW
    assert active_count == 2


def test_effective_fact_change_advances_observation_but_identical_scan_and_dry_run_do_not(
    tmp_path,
):
    database, root, projects, project, _memories, _service = _stack(tmp_path)
    manifest = root / "pyproject.toml"
    manifest.write_text('[project]\nname = "first"\n', encoding="utf-8")
    first_time = NOW - timedelta(hours=2)
    first_service = ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
        now=lambda: first_time,
    )
    first_service.scan(project)
    with database.connect(readonly=True) as connection:
        first_observed = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    identical = ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
        now=lambda: NOW - timedelta(hours=1),
    )
    identical.scan(project)
    manifest.write_text('[project]\nname = "second"\n', encoding="utf-8")
    changed = ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
        now=lambda: NOW,
    )
    changed.scan(project, dry_run=True)
    with database.connect(readonly=True) as connection:
        before_change = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    changed.scan(project)
    with database.connect(readonly=True) as connection:
        after_change = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    assert first_observed == before_change
    assert datetime.fromisoformat(first_observed.replace("Z", "+00:00")) == first_time
    assert datetime.fromisoformat(after_change.replace("Z", "+00:00")) == NOW


def test_compaction_is_namespace_preserving_deduplicated_redacted_and_idempotent(
    tmp_path,
):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    namespaces = (
        CODEX,
        Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5-mini"),
        Namespace(source_agent=SourceAgent.CHATGPT, model_id="gpt-5"),
    )
    for index, namespace in enumerate(namespaces):
        marker = f"namespace-{index}"
        _insert_memory(
            database,
            memories,
            project.project_id,
            namespace,
            MemoryKind.VERIFIED_METHOD,
            f"{marker} run tests",
            f"{marker}-method-old",
            created_at=NOW - timedelta(hours=2),
        )
        _insert_memory(
            database,
            memories,
            project.project_id,
            namespace,
            MemoryKind.VERIFIED_METHOD,
            f"{marker} run tests",
            f"{marker}-method-new",
            created_at=NOW - timedelta(hours=1),
        )
        _insert_memory(
            database,
            memories,
            project.project_id,
            namespace,
            MemoryKind.OPEN_ISSUE,
            f"{marker} unresolved sk-proj-abcdefghijklmnop",
            f"{marker}-issue",
        )
        _insert_memory(
            database,
            memories,
            project.project_id,
            namespace,
            MemoryKind.FAILED_ATTEMPT,
            f"{marker} timeout in build 123",
            f"{marker}-failure-a",
        )
        _insert_memory(
            database,
            memories,
            project.project_id,
            namespace,
            MemoryKind.FAILED_ATTEMPT,
            f"{marker} timeout in build 456",
            f"{marker}-failure-b",
        )

    summary = service.compact_project(project.project_id)
    repeated = service.compact_project(project.project_id)
    rows = _rows(database, project.project_id)
    retrospectives = [row for row in rows if row["memory_kind"] == "retrospective"]

    assert summary.namespace_count == 3
    assert summary.source_count == 15
    assert summary.cold_count == 15
    assert summary.retrospective_count == 3
    assert repeated.source_count == repeated.retrospective_count == 0
    assert len(retrospectives) == 3
    assert {(row["source_agent"], row["model_id"]) for row in retrospectives} == {
        (item.source_agent.value, item.model_id) for item in namespaces
    }
    for row in retrospectives:
        content = row["normalized_content"]
        own_index = namespaces.index(
            Namespace(source_agent=row["source_agent"], model_id=row["model_id"])
        )
        assert f"namespace-{own_index}" in content
        assert all(f"namespace-{other}" not in content for other in range(3) if other != own_index)
        assert "## Verified methods" in content
        assert "## Open issues" in content
        assert "## Failed attempts" in content
        assert "Source references:" in content
        assert "[REDACTED:api_key]" in content
        assert "sk-proj-abcdefghijklmnop" not in content
        assert row["lifecycle_state"] == "active"
    assert all(
        row["lifecycle_state"] == "cold" for row in rows if row["memory_kind"] != "retrospective"
    )
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from project_facts").fetchone()[0] == 0


def test_failed_attempts_that_only_differ_after_entry_bound_fail_without_colding(
    tmp_path,
):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    shared_prefix = "same failed command prefix " + ("x" * 600)
    for suffix in ("ALPHA", "BETA"):
        _insert_memory(
            database,
            memories,
            project.project_id,
            CODEX,
            MemoryKind.FAILED_ATTEMPT,
            f"{shared_prefix} {suffix}",
            f"failed-{suffix.casefold()}",
        )

    with pytest.raises(CompactionBoundsError, match="mandatory retrospective"):
        service.compact(project.project_id, CODEX)

    rows = _rows(database, project.project_id)
    assert len(rows) == 2
    assert all(row["lifecycle_state"] == "active" for row in rows)
    assert all(row["memory_kind"] == "failed_attempt" for row in rows)


def test_failure_signature_preserves_semantic_http_status_codes(tmp_path):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    for status in (404, 500):
        _insert_memory(
            database,
            memories,
            project.project_id,
            CODEX,
            MemoryKind.FAILED_ATTEMPT,
            f"request failed with HTTP {status}",
            f"http-{status}",
        )

    result = service.compact(project.project_id, CODEX)
    retrospective = next(
        row for row in _rows(database, project.project_id) if row["memory_kind"] == "retrospective"
    )

    assert result.cold_count == 2
    assert "items=2" in retrospective["normalized_content"]
    assert "HTTP 404" in retrospective["normalized_content"]
    assert "HTTP 500" in retrospective["normalized_content"]


def test_failure_signature_preserves_long_semantic_error_codes(tmp_path):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    for error_code in (1_000_000_001, 1_000_000_002):
        _insert_memory(
            database,
            memories,
            project.project_id,
            CODEX,
            MemoryKind.FAILED_ATTEMPT,
            f"vendor error code {error_code}",
            f"vendor-{error_code}",
        )

    service.compact(project.project_id, CODEX)
    retrospective = next(
        row for row in _rows(database, project.project_id) if row["memory_kind"] == "retrospective"
    )

    assert "items=2" in retrospective["normalized_content"]
    assert "1000000001" in retrospective["normalized_content"]
    assert "1000000002" in retrospective["normalized_content"]


def test_dry_run_and_existing_retrospective_never_write_or_become_a_source(tmp_path):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.RETROSPECTIVE,
        "older retrospective",
        "older-retrospective",
    )
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "unresolved issue",
        "issue",
    )
    before = [tuple(row) for row in _rows(database, project.project_id)]

    preview = service.compact(project.project_id, CODEX, dry_run=True)
    after_preview = [tuple(row) for row in _rows(database, project.project_id)]
    service.compact(project.project_id, CODEX)
    rows = _rows(database, project.project_id)

    assert preview.status == "dry_run"
    assert preview.source_count == 1
    assert before == after_preview
    older = next(row for row in rows if row["normalized_content"] == "older retrospective")
    issue = next(row for row in rows if row["normalized_content"] == "unresolved issue")
    assert older["lifecycle_state"] == "active"
    assert issue["lifecycle_state"] == "cold"


def test_compaction_sql_reasserts_exact_namespace_for_content_select_and_cold_update(
    tmp_path,
):
    database, _root, _projects, project, memories, service = _stack(tmp_path, tracing=True)
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "scoped issue",
        "scoped",
    )
    database.traces.clear()

    service.compact(project.project_id, CODEX)

    statements = [" ".join(item.casefold().split()) for item in database.traces]
    content_select = next(
        item
        for item in statements
        if "select * from behavior_memories" in item and "memory_kind <> 'retrospective'" in item
    )
    cold_update = next(
        item
        for item in statements
        if "update behavior_memories" in item and "set lifecycle_state = 'cold'" in item
    )
    for statement in (content_select, cold_update):
        assert "project_id =" in statement
        assert "source_agent =" in statement
        assert "model_id =" in statement
        assert "lifecycle_state = 'active'" in statement
        assert "memory_kind <> 'retrospective'" in statement
        assert str(project.project_id).lower() in statement
        assert "codex" in statement
        assert "gpt-5" in statement
    assert all("match" not in item for item in statements if "behavior_memories" in item)


def test_compaction_insert_and_cold_transition_roll_back_together(tmp_path, monkeypatch):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "rollback issue",
        "rollback",
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic cold failure")

    monkeypatch.setattr(memories, "_cold_compaction_sources_on_connection", fail)
    with pytest.raises(RuntimeError, match="synthetic cold failure"):
        service.compact(project.project_id, CODEX)

    rows = _rows(database, project.project_id)
    assert [(row["memory_kind"], row["lifecycle_state"]) for row in rows] == [
        ("open_issue", "active")
    ]
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from source_refs where parser_version = 'compaction-v1'"
            ).fetchone()[0]
            == 0
        )


def test_concurrent_compaction_creates_one_retrospective_and_colds_once(tmp_path):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    for index in range(8):
        _insert_memory(
            database,
            memories,
            project.project_id,
            CODEX,
            MemoryKind.OPEN_ISSUE,
            f"issue {index}",
            f"concurrent-{index}",
        )
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker():
        try:
            barrier.wait()
            results.append(service.compact(project.project_id, CODEX))
        except BaseException as error:  # pragma: no cover - assertion reports details
            errors.append(error)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    rows = _rows(database, project.project_id)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert sum(item.retrospective_count for item in results) == 1
    assert sum(row["memory_kind"] == "retrospective" for row in rows) == 1
    assert (
        sum(
            row["memory_kind"] != "retrospective" and row["lifecycle_state"] == "cold"
            for row in rows
        )
        == 8
    )


def test_mandatory_overflow_fails_closed_but_large_optional_set_batches_updates(
    tmp_path,
):
    database, _root, _projects, project, memories, _service = _stack(tmp_path)
    _bulk_insert(
        database,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        401,
        prefix="mandatory",
    )
    bounded = CompactionService(
        database,
        memories,
        Redactor(),
        source_limit=400,
        now=lambda: NOW,
    )

    with pytest.raises(CompactionBoundsError):
        bounded.compact(project.project_id, CODEX)
    rows = _rows(database, project.project_id)
    assert len(rows) == 401
    assert all(row["lifecycle_state"] == "active" for row in rows)
    assert all(row["memory_kind"] != "retrospective" for row in rows)

    with database.transaction() as connection:
        connection.execute("delete from behavior_memories")
        connection.execute("delete from source_refs")
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "x" * 513,
        "oversized-mandatory",
    )
    with pytest.raises(CompactionBoundsError, match="mandatory retrospective"):
        bounded.compact(project.project_id, CODEX)
    oversized_rows = _rows(database, project.project_id)
    assert [(row["memory_kind"], row["lifecycle_state"]) for row in oversized_rows] == [
        ("open_issue", "active")
    ]

    with database.transaction() as connection:
        connection.execute("delete from behavior_memories")
        connection.execute("delete from source_refs")
    _bulk_insert(
        database,
        project.project_id,
        CODEX,
        MemoryKind.DECISION,
        1001,
        prefix="optional",
    )
    large = CompactionService(database, memories, Redactor(), now=lambda: NOW)

    result = large.compact(project.project_id, CODEX)
    rows = _rows(database, project.project_id)
    retrospective = next(row for row in rows if row["memory_kind"] == "retrospective")

    assert result.cold_count == 1001
    assert result.remaining_count == 0
    assert result.retrospective_count == 1
    assert sum(row["lifecycle_state"] == "cold" for row in rows) == 1001
    assert sum(row["memory_kind"] == "retrospective" for row in rows) == 1
    assert len(retrospective["normalized_content"]) <= 262_144
    assert len(retrospective["normalized_content"].encode("utf-8")) <= 1_048_576


def test_optional_overflow_makes_one_bounded_batch_per_run_without_orphans(
    tmp_path,
):
    database, _root, _projects, project, _memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    _bulk_insert(
        database,
        project.project_id,
        CODEX,
        MemoryKind.DECISION,
        10_001,
        prefix="o",
    )

    first = service.compact_all_inactive(NOW)
    with database.connect(readonly=True) as connection:
        first_state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    second = service.compact_all_inactive(NOW)
    rows = _rows(database, project.project_id)
    with database.connect(readonly=True) as connection:
        second_state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    assert first.source_count == first.cold_count == 10_000
    assert first.retrospective_count == 1
    assert first.remaining_count == 1
    assert first.failure_count == 0
    assert first_state == "active"
    assert second.source_count == second.cold_count == 1
    assert second.retrospective_count == 1
    assert second.remaining_count == 0
    assert second.failure_count == 0
    assert second_state == "inactive"
    assert sum(row["memory_kind"] == "retrospective" for row in rows) == 2
    assert (
        sum(row["memory_kind"] == "decision" and row["lifecycle_state"] == "cold" for row in rows)
        == 10_001
    )


def test_optional_content_volume_compacts_in_render_bounded_prefixes_until_complete(
    tmp_path,
):
    database, _root, _projects, project, _memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    _bulk_insert(
        database,
        project.project_id,
        CODEX,
        MemoryKind.DECISION,
        600,
        prefix="x" * 490,
    )
    expected = {f"{'x' * 490} {index}" for index in range(600)}

    results = []
    for _ in range(10):
        result = service.compact_all_inactive(NOW)
        results.append(result)
        if result.remaining_count == 0 and result.failure_count == 0:
            break

    rows = _rows(database, project.project_id)
    sources = [row for row in rows if row["memory_kind"] == "decision"]
    retrospectives = [row for row in rows if row["memory_kind"] == "retrospective"]
    represented = {
        line[2:]
        for row in retrospectives
        for line in row["normalized_content"].splitlines()
        if line.startswith("- ")
    }
    with database.connect(readonly=True) as connection:
        compaction_refs = connection.execute(
            """
            select count(*), count(distinct source_record_id)
            from source_refs where parser_version = 'compaction-v1'
            """
        ).fetchone()
        state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    assert 2 <= len(results) < 10
    assert all(result.failure_count == 0 for result in results)
    assert all(result.source_count == result.cold_count > 0 for result in results)
    assert all(result.retrospective_count == 1 for result in results)
    assert all(result.remaining_count > 0 for result in results[:-1])
    assert results[-1].remaining_count == 0
    assert sum(result.cold_count for result in results) == 600
    assert len(sources) == 600
    assert all(row["lifecycle_state"] == "cold" for row in sources)
    assert len(retrospectives) == len(results)
    assert represented == expected
    assert all(
        len(row["normalized_content"]) <= 262_144
        and len(row["normalized_content"].encode("utf-8")) <= 1_048_576
        for row in retrospectives
    )
    assert tuple(compaction_refs) == (len(results), len(results))
    assert state == "inactive"


def test_compaction_overflow_stops_before_full_scans_and_makes_progress(tmp_path):
    database, _root, _projects, project, memories, _service = _stack(tmp_path)
    source_reference_id = _source_ref(database, SourceAgent.CODEX, "vm-progress")
    content = "bounded optional overflow"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    with database.transaction() as connection:
        connection.execute(
            """
            with recursive sequence(value) as (
                select 1
                union all
                select value + 1 from sequence where value < 50000
            )
            insert into behavior_memories(
                memory_id, project_id, source_agent, model_id,
                task_fingerprint, memory_kind, normalized_content,
                content_hash, source_reference_id, created_at,
                confidence, lifecycle_state
            )
            select printf('%08x-0000-4000-8000-%012x', value, value),
                   ?, 'codex', 'gpt-5', printf('%064x', value),
                   'decision', ?, ?, ?, ?, 1.0, 'active'
            from sequence
            """,
            (
                str(project.project_id).lower(),
                content,
                content_hash,
                str(source_reference_id).lower(),
                NOW.isoformat(),
            ),
        )
    measured = ProgressDatabase(database, max_callbacks=500)
    service = CompactionService(
        measured,
        MemoryRepository(measured),
        Redactor(),
        source_limit=1,
        now=lambda: NOW,
    )

    result = service.compact(project.project_id, CODEX)

    assert result.source_count == result.cold_count == 1
    assert result.retrospective_count == 1
    assert result.remaining_count == 1
    assert measured.callbacks <= measured.max_callbacks


def test_hostile_namespace_text_and_other_projects_remain_active(tmp_path):
    database, _root, projects, project, memories, service = _stack(tmp_path)
    hostile = Namespace(source_agent=SourceAgent.CODEX, model_id="m' OR 1=1 --")
    other_root = tmp_path / "other"
    other_root.mkdir()
    other = projects.register(ProjectCandidate(canonical_path=other_root, display_name="other"))
    for selected_project, namespace, label in (
        (project, hostile, "target"),
        (project, CODEX, "other-model"),
        (
            project,
            Namespace(source_agent=SourceAgent.CHATGPT, model_id=hostile.model_id),
            "other-agent",
        ),
        (other, hostile, "other-project"),
    ):
        _insert_memory(
            database,
            memories,
            selected_project.project_id,
            namespace,
            MemoryKind.OPEN_ISSUE,
            label,
            label,
        )

    service.compact(project.project_id, hostile)

    target = _rows(database, project.project_id)
    foreign = _rows(database, other.project_id)
    assert (
        next(row for row in target if row["normalized_content"] == "target")["lifecycle_state"]
        == "cold"
    )
    assert all(
        row["lifecycle_state"] == "active"
        for row in target
        if row["normalized_content"] in {"other-model", "other-agent"}
    )
    assert all(row["lifecycle_state"] == "active" for row in foreign)


def test_malformed_source_hash_and_hostile_provenance_collision_roll_back(tmp_path):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    inserted = _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "hash protected",
        "hash-protected",
    )
    assert inserted.record_id is not None
    with database.transaction() as connection:
        connection.execute(
            "update behavior_memories set content_hash = ? where memory_id = ?",
            ("0" * 64, str(inserted.record_id).lower()),
        )
    with pytest.raises(RuntimeError, match="hash mismatch"):
        service.compact(project.project_id, CODEX)
    with database.transaction() as connection:
        connection.execute(
            "update behavior_memories set content_hash = ? where memory_id = ?",
            (
                hashlib.sha256(b"hash protected").hexdigest(),
                str(inserted.record_id).lower(),
            ),
        )
    with database.connect(readonly=True) as connection:
        records, _total, _mandatory = memories._select_compaction_sources_on_connection(
            connection, project.project_id, CODEX, limit=400
        )
    plan = service._build_plan(records)
    ordered_ids = tuple(sorted(str(item).lower() for item in plan.source_ids))
    model_digest = hashlib.sha256(CODEX.model_id.encode()).hexdigest()
    included_digest = hashlib.sha256("\n".join(ordered_ids).encode("ascii")).hexdigest()
    namespace_digest = hashlib.sha256(
        "\0".join(
            (
                str(project.project_id).lower(),
                CODEX.source_agent.value,
                model_digest,
                included_digest,
            )
        ).encode("ascii")
    ).hexdigest()
    source_record_id = f"compaction-v1:{namespace_digest}"
    content_hash = hashlib.sha256(plan.content.encode()).hexdigest()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, 'codex', ?, null, ?, ?, 'hostile-v1', ?)
            """,
            (
                str(uuid4()).lower(),
                source_record_id,
                content_hash,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )

    with pytest.raises(RuntimeError, match="provenance collision"):
        service.compact(project.project_id, CODEX)

    rows = _rows(database, project.project_id)
    assert [(row["memory_kind"], row["lifecycle_state"]) for row in rows] == [
        ("open_issue", "active")
    ]


def test_cold_update_trigger_abort_rolls_back_retrospective_and_provenance(tmp_path):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "trigger protected",
        "trigger",
    )
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger reject_compaction_cold
            before update of lifecycle_state on behavior_memories
            when new.lifecycle_state = 'cold'
            begin
                select raise(abort, 'cold rejected');
            end
            """
        )

    with pytest.raises(Exception, match="cold rejected"):
        service.compact(project.project_id, CODEX)

    rows = _rows(database, project.project_id)
    assert [(row["memory_kind"], row["lifecycle_state"]) for row in rows] == [
        ("open_issue", "active")
    ]
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from source_refs where parser_version = 'compaction-v1'"
            ).fetchone()[0]
            == 0
        )


def test_all_inactive_marks_only_after_success_and_reconcile_does_not_recompact(
    tmp_path,
):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "reconcile issue",
        "reconcile",
    )
    lock = ProcessLock(tmp_path / "reconcile.lock")
    reconcile = ReconcileService(
        database,
        lock,
        compact=service.compact_newly_inactive,
        now=lambda: NOW,
    )

    first = reconcile.run(force=True)
    second = reconcile.run(force=True)

    assert first.status == second.status == "success"
    assert first.stages["compaction"] == second.stages["compaction"] == "pass"
    rows = _rows(database, project.project_id)
    assert sum(row["memory_kind"] == "retrospective" for row in rows) == 1
    with database.connect(readonly=True) as connection:
        state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    assert state == "inactive"


@pytest.mark.parametrize("bad_model", ("a-bad", "z-bad"))
def test_namespace_failures_do_not_starve_or_hide_committed_namespace_progress(tmp_path, bad_model):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    good_model = "z-good" if bad_model == "a-bad" else "a-good"
    bad = Namespace(source_agent=SourceAgent.CODEX, model_id=bad_model)
    good = Namespace(source_agent=SourceAgent.CODEX, model_id=good_model)
    bad_content = "corrupted namespace source"
    inserted_bad = _insert_memory(
        database,
        memories,
        project.project_id,
        bad,
        MemoryKind.OPEN_ISSUE,
        bad_content,
        f"bad-{bad_model}",
    )
    _insert_memory(
        database,
        memories,
        project.project_id,
        good,
        MemoryKind.OPEN_ISSUE,
        "healthy namespace source",
        f"good-{good_model}",
    )
    with database.transaction() as connection:
        connection.execute(
            "update behavior_memories set content_hash = ? where memory_id = ?",
            ("0" * 64, str(inserted_bad.record_id).lower()),
        )

    first = service.compact_all_inactive(NOW)
    first_rows = _rows(database, project.project_id)
    with database.connect(readonly=True) as connection:
        first_state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    assert first.project_count == 1
    assert first.namespace_count == 2
    assert first.source_count == first.cold_count == 1
    assert first.retrospective_count == 1
    assert first.failure_count == 1
    assert first_state == "active"
    assert (
        next(row for row in first_rows if row["model_id"] == good_model)["lifecycle_state"]
        == "cold"
    )
    assert (
        next(row for row in first_rows if row["model_id"] == bad_model)["lifecycle_state"]
        == "active"
    )

    with database.transaction() as connection:
        connection.execute(
            "update behavior_memories set content_hash = ? where memory_id = ?",
            (
                hashlib.sha256(bad_content.encode()).hexdigest(),
                str(inserted_bad.record_id).lower(),
            ),
        )
    second = service.compact_all_inactive(NOW)
    with database.connect(readonly=True) as connection:
        second_state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    assert second.source_count == second.cold_count == 1
    assert second.retrospective_count == 1
    assert second.failure_count == 0
    assert second_state == "inactive"
    assert (
        sum(row["memory_kind"] == "retrospective" for row in _rows(database, project.project_id))
        == 2
    )


def test_direct_project_compaction_requires_inactive_snapshot_and_marks_completion(
    tmp_path,
):
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, NOW.isoformat())
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "fresh project issue",
        "fresh-project",
    )

    skipped = service.compact_project(project.project_id)
    rows_after_skip = _rows(database, project.project_id)

    assert skipped == compaction_module.CompactionSummary()
    assert [(row["memory_kind"], row["lifecycle_state"]) for row in rows_after_skip] == [
        ("open_issue", "active")
    ]

    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    completed = service.compact_project(project.project_id)
    with database.connect(readonly=True) as connection:
        state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]

    assert completed.source_count == completed.cold_count == 1
    assert completed.retrospective_count == 1
    assert completed.failure_count == 0
    assert state == "inactive"


def test_namespace_enumeration_is_capped_and_resumes_on_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(compaction_module, "_MAX_NAMESPACES_PER_PROJECT", 1, raising=False)
    database, _root, _projects, project, memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    for model in ("a-model", "z-model"):
        namespace = Namespace(source_agent=SourceAgent.CODEX, model_id=model)
        _insert_memory(
            database,
            memories,
            project.project_id,
            namespace,
            MemoryKind.DECISION,
            f"decision for {model}",
            model,
        )

    first = service.compact_all_inactive(NOW)
    second = service.compact_all_inactive(NOW)

    assert first.namespace_count == 1
    assert first.remaining_count == 1
    assert first.failure_count == 0
    assert second.namespace_count == 1
    assert second.remaining_count == 0
    assert second.failure_count == 0
    assert (
        sum(row["memory_kind"] == "retrospective" for row in _rows(database, project.project_id))
        == 2
    )


def test_namespace_enumeration_seeks_past_large_groups_within_vm_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(compaction_module, "_MAX_NAMESPACES_PER_PROJECT", 1)
    database, _root, _projects, project, _memories, _service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=21)).isoformat())
    source_reference_id = _source_ref(database, SourceAgent.CODEX, "namespace-budget")
    content = "namespace budget"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    with database.transaction() as connection:
        connection.execute(
            """
            with recursive sequence(value) as (
                select 1
                union all
                select value + 1 from sequence where value < 50000
            )
            insert into behavior_memories(
                memory_id, project_id, source_agent, model_id,
                task_fingerprint, memory_kind, normalized_content,
                content_hash, source_reference_id, created_at,
                confidence, lifecycle_state
            )
            select printf('%08x-0000-4000-8000-%012x', value, value),
                   ?, 'codex', 'gpt-5', printf('%064x', value),
                   'decision', ?, ?, ?, ?, 1.0, 'active'
            from sequence
            """,
            (
                str(project.project_id).lower(),
                content,
                content_hash,
                str(source_reference_id).lower(),
                NOW.isoformat(),
            ),
        )
    measured = ProgressDatabase(database, max_callbacks=400)
    service = CompactionService(
        measured,
        MemoryRepository(measured),
        Redactor(),
        source_limit=1,
        now=lambda: NOW,
    )

    result = service.compact_project(project.project_id, dry_run=True)

    assert result.namespace_count == 1
    assert result.source_count == 1
    assert result.remaining_count == 1
    assert result.failure_count == 0
    assert measured.callbacks <= measured.max_callbacks


def test_inactive_project_enumeration_is_paged_and_resumes_on_next_run(tmp_path, monkeypatch):
    monkeypatch.setattr(compaction_module, "_MAX_INACTIVE_PROJECTS_PER_RUN", 1, raising=False)
    database, root, projects, project, memories, service = _stack(tmp_path)
    other_root = root.parent / "other-project"
    other_root.mkdir()
    other = projects.register(
        ProjectCandidate(canonical_path=other_root, display_name="other-project")
    )
    for selected, label in ((project, "first"), (other, "second")):
        _set_activity(database, selected.project_id, (NOW - timedelta(days=21)).isoformat())
        _insert_memory(
            database,
            memories,
            selected.project_id,
            CODEX,
            MemoryKind.DECISION,
            f"{label} decision",
            f"project-{label}",
        )

    first = service.compact_all_inactive(NOW)
    second = service.compact_all_inactive(NOW)

    assert first.project_count == 1
    assert first.remaining_count == 1
    assert second.project_count == 1
    assert second.remaining_count == 0
    with database.connect(readonly=True) as connection:
        states = {
            row["inactivity_state"]
            for row in connection.execute(
                "select inactivity_state from projects where project_id in (?, ?)",
                (str(project.project_id).lower(), str(other.project_id).lower()),
            ).fetchall()
        }
    assert states == {"inactive"}


@pytest.mark.parametrize(
    "bulk_timestamp",
    (
        NOW.isoformat(),
        (NOW - timedelta(days=21)).replace(tzinfo=None).isoformat(),
        "2026-02-30T12:00:00+00:00",
    ),
)
def test_inactive_enumeration_uses_indexed_cutoff_before_strict_validation(
    tmp_path, monkeypatch, bulk_timestamp
):
    monkeypatch.setattr(compaction_module, "_MAX_INACTIVE_PROJECTS_PER_RUN", 1)
    database, _root, _projects, _project, _memories, _service = _stack(tmp_path)
    with database.transaction() as connection:
        connection.execute(
            """
            with recursive sequence(value) as (
                select 1
                union all
                select value + 1 from sequence where value < 50000
            )
            insert into projects(
                project_id, canonical_path, display_name,
                last_observed_change
            )
            select printf('%08x-0000-4000-8000-%012x', value, value),
                   printf('/tmp/fresh-project-%d', value),
                   printf('fresh-project-%d', value), ?
            from sequence
            """,
            (bulk_timestamp,),
        )
        connection.execute(
            """
            insert into projects(
                project_id, canonical_path, display_name,
                last_observed_change
            ) values (?, '/tmp/old-project', 'old-project', ?)
            """,
            (
                "ffffffff-ffff-4fff-8fff-ffffffffffff",
                (NOW - timedelta(days=21)).isoformat(),
            ),
        )
    measured = ProgressDatabase(database, max_callbacks=400)
    service = CompactionService(
        measured,
        MemoryRepository(measured),
        Redactor(),
        now=lambda: NOW,
    )

    found = service.find_inactive(NOW)

    assert [str(project.project_id) for project in found] == [
        "ffffffff-ffff-4fff-8fff-ffffffffffff"
    ]
    assert measured.callbacks <= measured.max_callbacks


def test_multiple_inactivity_cycles_exclude_prior_retrospective_and_zero_source_marks(
    tmp_path,
):
    database, root, projects, project, memories, service = _stack(tmp_path)
    _set_activity(database, project.project_id, (NOW - timedelta(days=30)).isoformat())
    _insert_memory(
        database,
        memories,
        project.project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "cycle A",
        "cycle-a",
    )

    first = service.compact_all_inactive(NOW)
    assert first.retrospective_count == 1
    payload = CapturePayload(
        cwd=root,
        namespace=CODEX,
        source_record_id="cycle-b",
        objective="continue",
        outcome="cycle B",
        risks=["new risk"],
    )
    verification = NamespaceVerification(
        namespace=CODEX,
        source_record_id="cycle-b",
        verified_by="codex_adapter",
        verified_at=NOW,
    )
    inserted = CaptureService(database, projects, memories, Redactor()).capture(
        payload, verification
    )
    assert inserted.status == "inserted"
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select last_observed_change, inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()
    observed = datetime.fromisoformat(row["last_observed_change"].replace("Z", "+00:00"))
    assert row["inactivity_state"] == "active"
    second = service.compact_all_inactive(observed + timedelta(days=21))
    immediate = service.compact_all_inactive(observed + timedelta(days=21))
    rows = _rows(database, project.project_id)
    retrospectives = [row for row in rows if row["memory_kind"] == "retrospective"]

    assert second.retrospective_count == 1
    assert immediate.retrospective_count == 0
    assert len(retrospectives) == 2
    first_retro = next(row for row in retrospectives if "cycle A" in row["normalized_content"])
    second_retro = next(row for row in retrospectives if "cycle B" in row["normalized_content"])
    assert "cycle B" not in first_retro["normalized_content"]
    assert "cycle A" not in second_retro["normalized_content"]
    assert all(row["lifecycle_state"] == "active" for row in retrospectives)

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    empty = projects.register(ProjectCandidate(canonical_path=empty_root, display_name="empty"))
    _set_activity(database, empty.project_id, (NOW - timedelta(days=21)).isoformat())
    empty_result = service.compact_all_inactive(NOW)
    with database.connect(readonly=True) as connection:
        empty_state = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(empty.project_id).lower(),),
        ).fetchone()[0]
    assert empty_result.project_count == 1
    assert empty_state == "inactive"


def test_activity_change_between_namespace_transactions_prevents_inactive_cas_and_retries(
    tmp_path, monkeypatch
):
    database, _root, projects, project, memories, service = _stack(tmp_path)
    old = NOW - timedelta(days=21)
    _set_activity(database, project.project_id, old.isoformat())
    second_namespace = Namespace(source_agent=SourceAgent.CHATGPT, model_id="gpt-5")
    for namespace, label in ((CODEX, "first"), (second_namespace, "second")):
        _insert_memory(
            database,
            memories,
            project.project_id,
            namespace,
            MemoryKind.OPEN_ISSUE,
            label,
            label,
        )
    original = service.compact
    calls = 0

    def racing_compact(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            assert projects.advance_last_observed_change(project.project_id, NOW, as_of=NOW)
        return result

    monkeypatch.setattr(service, "compact", racing_compact)
    raced = service.compact_all_inactive(NOW)
    with database.connect(readonly=True) as connection:
        state_after_race = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    assert raced.failure_count == 1
    assert state_after_race == "active"
    assert (
        sum(
            row["lifecycle_state"] == "cold"
            for row in _rows(database, project.project_id)
            if row["memory_kind"] != "retrospective"
        )
        == 1
    )

    monkeypatch.setattr(service, "compact", original)
    retried = service.compact_all_inactive(NOW + timedelta(days=21))
    with database.connect(readonly=True) as connection:
        state_after_retry = connection.execute(
            "select inactivity_state from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()[0]
    assert retried.failure_count == 0
    assert state_after_retry == "inactive"
    assert (
        sum(
            row["lifecycle_state"] == "cold"
            for row in _rows(database, project.project_id)
            if row["memory_kind"] != "retrospective"
        )
        == 2
    )


def test_reconcile_compaction_failure_is_degraded_immediately_due_and_skipped_on_core_failure(
    tmp_path,
):
    database, _root, _projects, _project, _memories, _service = _stack(tmp_path)
    calls = []

    def fail(as_of):
        calls.append(as_of)
        raise RuntimeError("synthetic")

    lock = ProcessLock(tmp_path / "reconcile.lock")
    failed = ReconcileService(
        database,
        lock,
        compact=fail,
        now=lambda: NOW,
    ).run(force=True)
    due = ReconcileService(
        database,
        lock,
        compact=lambda as_of: SimpleNamespace(
            project_count=0,
            namespace_count=0,
            source_count=0,
            cold_count=0,
            retrospective_count=0,
            remaining_count=0,
        ),
        now=lambda: NOW + timedelta(minutes=1),
    ).run(force=False)
    skipped_calls = []
    core_failed = ReconcileService(
        database,
        lock,
        discover=lambda: (_ for _ in ()).throw(RuntimeError("discovery")),
        compact=lambda as_of: skipped_calls.append(as_of),
        now=lambda: NOW + timedelta(days=1),
    ).run(force=True)

    assert failed.status == "degraded"
    assert failed.stages["compaction"] == "error"
    assert due.status == "success"
    assert len(calls) == 1
    assert core_failed.status == "failed"
    assert skipped_calls == []


def test_reconcile_skips_compaction_after_any_degraded_ingestion_stage(tmp_path):
    database, _root, _projects, _project, _memories, _service = _stack(tmp_path)
    calls = []

    def adapter():
        return SimpleNamespace(capture_results=(), failure_count=0, warning_count=1)

    report = ReconcileService(
        database,
        ProcessLock(tmp_path / "reconcile.lock"),
        codex_runs=(adapter,),
        compact=lambda as_of: calls.append(as_of),
        now=lambda: NOW,
    ).run(force=True)

    assert report.status == "degraded"
    assert report.stages["codex_0"] == "warn"
    assert report.stages["compaction"] == "pass"
    assert calls == []


def test_compact_cli_requires_one_selector_and_outputs_only_bounded_counts(tmp_path, monkeypatch):
    marker = "PRIVATE_BEHAVIOR_MARKER"
    project_id = uuid4()
    calls = []

    class FakeCompaction:
        def compact_project(self, selected, *, dry_run=False):
            calls.append((selected, dry_run))
            return SimpleNamespace(
                project_count=1,
                namespace_count=2,
                source_count=3,
                cold_count=0,
                retrospective_count=0,
                remaining_count=0,
            )

        def compact_all_inactive(self, *, dry_run=False):
            calls.append(("all", dry_run))
            return SimpleNamespace(
                project_count=1,
                namespace_count=2,
                source_count=3,
                cold_count=0,
                retrospective_count=0,
                remaining_count=0,
            )

    fake = SimpleNamespace(compaction=FakeCompaction(), close=lambda: None)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)
    monkeypatch.setattr(cli_module, "build_readonly_compaction_container", lambda _path: fake)

    missing = runner.invoke(app, ["compact", "--format", "json"])
    conflict = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / marker / "config.toml"),
            "compact",
            "--project",
            str(project_id),
            "--all-inactive",
            "--format",
            "json",
        ],
    )
    preview = runner.invoke(
        app,
        [
            "compact",
            "--project",
            str(project_id),
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert missing.exit_code == conflict.exit_code == 4
    assert json.loads(missing.stdout)["error"]["code"] == "invalid_input"
    assert json.loads(conflict.stdout)["error"]["code"] == "invalid_input"
    assert marker not in conflict.stdout
    assert preview.exit_code == 0
    payload = json.loads(preview.stdout)
    assert payload == {
        "cold_count": 0,
        "dry_run": True,
        "namespace_count": 2,
        "project_count": 1,
        "remaining_count": 0,
        "retrospective_count": 0,
        "source_count": 3,
        "status": "ok",
    }
    assert marker not in preview.stdout
    assert calls == [(project_id, True)]


def test_compact_cli_dry_run_uses_readonly_snapshot_without_file_or_database_writes(
    tmp_path,
):
    project_root = tmp_path / "private-project-marker"
    project_root.mkdir()
    config_path = tmp_path / "runtime" / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    with build_container(config_path) as container:
        project = container.projects.register(
            ProjectCandidate(
                canonical_path=project_root,
                display_name=project_root.name,
            )
        )
        _insert_memory(
            container.database,
            container.memories,
            project.project_id,
            CODEX,
            MemoryKind.OPEN_ISSUE,
            "dry run issue",
            "dry-run-real",
        )
    database_path = config_path.parent / "memory.db"

    def metadata(path: Path):
        value = os.stat(path)
        return value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns

    before_metadata = {path: metadata(path) for path in (config_path, database_path)}
    with Database(database_path).connect(readonly=True) as connection:
        before_projects = [
            tuple(row) for row in connection.execute("select * from projects order by project_id")
        ]
        before_rows = [
            tuple(row)
            for row in connection.execute(
                "select * from behavior_memories order by memory_id"
            ).fetchall()
        ]

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "compact",
            "--project",
            str(project.project_id),
            "--dry-run",
            "--format",
            "json",
        ],
    )

    after_metadata = {path: metadata(path) for path in (config_path, database_path)}
    with Database(database_path).connect(readonly=True) as connection:
        after_projects = [
            tuple(row) for row in connection.execute("select * from projects order by project_id")
        ]
        after_rows = [
            tuple(row)
            for row in connection.execute(
                "select * from behavior_memories order by memory_id"
            ).fetchall()
        ]
    assert result.exit_code == 0
    assert json.loads(result.stdout)["dry_run"] is True
    assert str(project.project_id) not in result.stdout
    assert "private-project-marker" not in result.stdout
    assert before_metadata == after_metadata
    assert before_projects == after_projects
    assert before_rows == after_rows


def test_compact_cli_dry_run_migrates_real_v4_snapshot_only_in_memory(tmp_path, monkeypatch):
    project_root = tmp_path / "legacy-project-marker"
    project_root.mkdir()
    config_path = tmp_path / "runtime" / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 4)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(config_path.parent / "memory.db")
    database.initialize()
    project_id = uuid4()
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            (
                str(project_id),
                str(project_root),
                project_root.name,
            ),
        )
    memories = MemoryRepository(database)
    _set_activity(
        database,
        project_id,
        datetime(2020, 1, 1, tzinfo=UTC).isoformat(),
    )
    _insert_memory(
        database,
        memories,
        project_id,
        CODEX,
        MemoryKind.OPEN_ISSUE,
        "legacy dry run issue",
        "legacy-dry-run-real",
    )
    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)

    def physical_state(path: Path):
        state = []
        for candidate in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
            Path(f"{path}-journal"),
        ):
            if not candidate.exists():
                state.append(None)
                continue
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
            metadata = candidate.stat()
            state.append(
                (
                    digest,
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_nlink,
                    metadata.st_uid,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
        return tuple(state)

    def disk_contract():
        with database.connect(readonly=True) as connection:
            return (
                tuple(
                    row[0]
                    for row in connection.execute(
                        "select version from schema_migrations order by version"
                    )
                ),
                tuple(row["name"] for row in connection.execute("pragma table_info(projects)")),
                tuple(
                    sorted(row["name"] for row in connection.execute("pragma index_list(projects)"))
                ),
                tuple(
                    tuple(row)
                    for row in connection.execute("select * from projects order by project_id")
                ),
            )

    database_path = database.path
    before_contract = disk_contract()
    before_physical = physical_state(database_path)
    before_config = physical_state(config_path)[0]

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "compact",
            "--all-inactive",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    after_contract = disk_contract()
    after_physical = physical_state(database_path)
    after_config = physical_state(config_path)[0]
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "cold_count": 0,
        "dry_run": True,
        "namespace_count": 1,
        "project_count": 1,
        "remaining_count": 0,
        "retrospective_count": 0,
        "source_count": 1,
        "status": "ok",
    }
    assert str(project_id) not in result.stdout
    assert project_root.name not in result.stdout
    assert before_contract == after_contract
    assert before_contract[0] == (1, 2, 3, 4)
    assert "last_observed_change_epoch_us" not in before_contract[1]
    assert before_physical == after_physical
    assert before_config == after_config


def test_compaction_backlog_keeps_reconcile_due(tmp_path, monkeypatch):
    database, _root, _projects, _project, _memories, _service = _stack(tmp_path)
    summary = SimpleNamespace(
        project_count=1,
        namespace_count=1,
        source_count=400,
        cold_count=400,
        retrospective_count=1,
        remaining_count=1,
    )
    lock = ProcessLock(tmp_path / "reconcile.lock")
    first = ReconcileService(
        database,
        lock,
        compact=lambda _as_of: summary,
        now=lambda: NOW,
    ).run(force=True)

    assert first.status == "degraded"
    assert first.stages["compaction"] == "warn"
    assert ReconcileService.minimal(
        database, lock, now=lambda: NOW + timedelta(minutes=1)
    ).should_run()
