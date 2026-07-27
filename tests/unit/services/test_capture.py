from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import project_memory_hub.services.capture as capture_module
import project_memory_hub.storage.path_identity as path_identity_module
import project_memory_hub.storage.projects as projects_module
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    CapturePayload,
    CaptureResult,
    MemoryKind,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository
from project_memory_hub.storage.resolutions import IssueResolutionRepository


VERIFIED_AT = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
TARGET_NAMESPACE = Namespace(
    source_agent=SourceAgent.CODEX,
    model_id="gpt-5.6-sol",
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    return database


@pytest.fixture
def registered_project(database: Database, tmp_path: Path) -> tuple[ProjectRepository, Path]:
    root = tmp_path / "synthetic-project"
    root.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=root, display_name="Synthetic"))
    return projects, root


@pytest.fixture
def capture_service(
    database: Database,
    registered_project: tuple[ProjectRepository, Path],
) -> CaptureService:
    projects, _ = registered_project
    return CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        issue_resolutions=IssueResolutionRepository(),
    )


def _secret() -> str:
    return "sk-proj-" + ("Q" * 24)


def _payload(root: Path, **updates) -> CapturePayload:
    values = {
        "cwd": root,
        "namespace": Namespace(
            source_agent=SourceAgent.CODEX,
            model_id="gpt-5.6-sol",
        ),
        "source_record_id": "synthetic-record-1",
        "objective": "  keep the task scoped  ",
        "outcome": "  completed safely  ",
        "decisions": ["  use sqlite transactions  ", "   "],
        "failed_attempts": ["nested transaction failed"],
        "verified_commands": ["uv run pytest"],
        "changed_paths": [" src/example.py "],
        "preferences": ["prefer deterministic ranking"],
        "risks": ["namespace leakage"],
        "open_issues": ["none remaining"],
        "reusable_lessons": ["scope before ranking"],
    }
    values.update(updates)
    return CapturePayload.model_validate(values)


def _verification(
    payload: CapturePayload,
    **updates,
) -> NamespaceVerification:
    values = {
        "namespace": payload.namespace,
        "source_record_id": payload.source_record_id,
        "verified_by": (
            "codex_adapter"
            if payload.namespace.source_agent is SourceAgent.CODEX
            else "chatgpt_adapter"
        ),
        "verified_at": datetime.now(timezone.utc),
    }
    values.update(updates)
    return NamespaceVerification.model_validate(values)


def _counts(database: Database) -> dict[str, int]:
    with database.connect(readonly=True) as connection:
        return {
            table: connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "pending_captures",
                "pending_capture_history",
                "source_refs",
                "behavior_memories",
            )
        }


def _lifecycle(database: Database, memory_id: UUID) -> str:
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select lifecycle_state from behavior_memories where memory_id = ?",
            (str(memory_id).lower(),),
        ).fetchone()
    assert row is not None
    return str(row["lifecycle_state"])


def _resolution_audit_count(database: Database) -> int:
    with database.connect(readonly=True) as connection:
        return int(
            connection.execute("select count(*) from memory_issue_resolutions").fetchone()[0]
        )


def _capture_counts_with_audit(database: Database) -> dict[str, int]:
    with database.connect(readonly=True) as connection:
        return {
            table: int(connection.execute(f"select count(*) from {table}").fetchone()[0])
            for table in (
                "pending_captures",
                "pending_capture_history",
                "source_refs",
                "behavior_memories",
                "memory_issue_resolutions",
            )
        }


def _resolution_payload(
    root: Path,
    source_record_id: str,
    *,
    namespace: Namespace = TARGET_NAMESPACE,
    outcome: str = "",
    open_issues: tuple[str, ...] = (),
    resolved_open_issues: tuple[str, ...] = (),
) -> CapturePayload:
    return _payload(
        root,
        namespace=namespace,
        source_record_id=source_record_id,
        objective="",
        outcome=outcome,
        decisions=[],
        failed_attempts=[],
        verified_commands=[],
        changed_paths=[],
        preferences=[],
        risks=[],
        open_issues=list(open_issues),
        resolved_open_issues=list(resolved_open_issues),
        reusable_lessons=[],
    )


def _seed_open_issue(
    service: CaptureService,
    root: Path,
    text: str,
    source_record_id: str,
    *,
    namespace: Namespace = TARGET_NAMESPACE,
    verified_at: datetime = VERIFIED_AT,
) -> UUID:
    payload = _resolution_payload(
        root,
        source_record_id,
        namespace=namespace,
        open_issues=(text,),
    )
    result = service.capture(
        payload,
        _verification(payload, verified_at=verified_at),
    )
    assert result.status == "inserted"
    assert len(result.inserted_ids) == 1
    return result.inserted_ids[0]


def _last_observed_change(database: Database, root: Path) -> str | None:
    project = ProjectRepository(database).find_by_cwd(root)
    assert project is not None
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project.project_id).lower(),),
        ).fetchone()
    assert row is not None
    value = row["last_observed_change"]
    return None if value is None else str(value)


def test_prepare_verified_rejects_naive_verification_before_preparing_capture(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "naive-prepare-verification",
        outcome="timezone must be trusted",
    )
    verification = _verification(
        payload,
        verified_at=VERIFIED_AT.replace(tzinfo=None),
    )

    result = capture_service.prepare_verified(payload, verification)

    assert isinstance(result, CaptureResult)
    assert result.status == "rejected"


def test_unverified_resolution_only_capture_is_pending_without_resolution_side_effects(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    old_issue_id = _seed_open_issue(
        capture_service,
        root,
        "exact old issue",
        "old-issue-source",
    )
    before = _counts(database)
    payload = _resolution_payload(
        root,
        "unverified-resolution-only",
        resolved_open_issues=("exact old issue",),
    )

    result = capture_service.capture(payload)

    assert result.status == "pending_verification"
    assert result.duplicate is False
    assert _lifecycle(database, old_issue_id) == "active"
    assert _resolution_audit_count(database) == 0
    assert _counts(database) == {
        **before,
        "pending_captures": before["pending_captures"] + 1,
        "pending_capture_history": 0,
    }


def test_pending_capture_capacity_is_atomic_and_still_allows_exact_duplicates(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = registered_project
    monkeypatch.setattr(capture_module, "_MAX_PENDING_PER_PROJECT", 1)
    monkeypatch.setattr(capture_module, "_MAX_PENDING_GLOBAL", 10)
    first = _payload(root, source_record_id="capacity-first")

    inserted = capture_service.capture(first)
    duplicate = capture_service.capture(first)
    with pytest.raises(capture_module.PendingCaptureCapacityError):
        capture_service.capture(_payload(root, source_record_id="capacity-second"))

    assert inserted.status == "pending_verification"
    assert inserted.duplicate is False
    assert duplicate.status == "pending_verification"
    assert duplicate.duplicate is True
    assert _counts(database)["pending_captures"] == 1


def test_concurrent_pending_captures_cannot_overshoot_active_capacity(
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects, root = registered_project
    monkeypatch.setattr(capture_module, "_MAX_PENDING_PER_PROJECT", 1)
    monkeypatch.setattr(capture_module, "_MAX_PENDING_GLOBAL", 1)
    callers_ready = threading.Barrier(2)
    original_store_pending = CaptureService._store_pending

    def synchronized_store_pending(self, *args, **kwargs):
        try:
            callers_ready.wait(timeout=5)
        except threading.BrokenBarrierError:
            pytest.fail("concurrent capture did not reach the write boundary", pytrace=False)
        return original_store_pending(self, *args, **kwargs)

    monkeypatch.setattr(CaptureService, "_store_pending", synchronized_store_pending)

    def capture_one(index: int) -> str:
        service = CaptureService(
            database,
            projects,
            MemoryRepository(database),
            Redactor(),
        )
        try:
            result = service.capture(
                _payload(root, source_record_id=f"concurrent-capacity-{index}")
            )
        except capture_module.PendingCaptureCapacityError:
            return "capacity"
        return result.status

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(capture_one, range(2)))

    assert sorted(outcomes) == ["capacity", "pending_verification"]
    assert _counts(database)["pending_captures"] == 1


def test_finalized_history_does_not_consume_active_pending_capacity(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = registered_project
    monkeypatch.setattr(capture_module, "_MAX_PENDING_PER_PROJECT", 1)
    monkeypatch.setattr(capture_module, "_MAX_PENDING_GLOBAL", 10)
    first = _payload(root, source_record_id="history-capacity-first")

    assert capture_service.capture(first).status == "pending_verification"
    assert capture_service.capture(first, _verification(first)).status == "inserted"
    try:
        second = capture_service.capture(_payload(root, source_record_id="history-capacity-second"))
    except capture_module.PendingCaptureCapacityError:
        pytest.fail("finalized history consumed active pending capacity", pytrace=False)

    assert second.status == "pending_verification"
    assert second.duplicate is False
    with database.connect(readonly=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        assert "pending_capture_history" in tables
        active_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
        history = connection.execute(
            """
            select final_state, source_reference_id
            from pending_capture_history
            """
        ).fetchall()
        history_columns = {
            row["name"] for row in connection.execute("pragma table_info(pending_capture_history)")
        }

    assert active_count == 1
    assert [row["final_state"] for row in history] == ["verified"]
    assert history[0]["source_reference_id"] is not None
    assert "structured_payload_json" not in history_columns


def test_retained_history_preserves_exact_pending_duplicate_semantics(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _payload(root, source_record_id="retained-history-duplicate")

    assert capture_service.capture(payload).duplicate is False
    assert capture_service.capture(payload, _verification(payload)).status == "inserted"
    duplicate = capture_service.capture(payload)

    assert duplicate.status == "pending_verification"
    assert duplicate.duplicate is True
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0
        assert connection.execute("select count(*) from pending_capture_history").fetchone()[0] == 1


def test_pending_history_hard_cap_evicts_oldest_finalized_rows_deterministically(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = registered_project
    monkeypatch.setattr(capture_module, "_MAX_PENDING_HISTORY", 2, raising=False)
    monkeypatch.setattr(capture_module, "_PENDING_HISTORY_CLEANUP_BATCH", 2, raising=False)
    base = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    clock = iter(
        [
            timestamp
            for index in range(3)
            for timestamp in (
                base + timedelta(seconds=index * 2),
                base + timedelta(seconds=index * 2 + 1),
            )
        ]
        + [base + timedelta(seconds=6)]
    )
    monkeypatch.setattr(capture_service, "_now", lambda: next(clock))
    archived_ids: list[str] = []

    for index in range(3):
        payload = _payload(
            root,
            source_record_id=f"history-cleanup-{index}",
            outcome=f"history cleanup outcome {index}",
        )
        assert capture_service.capture(payload).status == "pending_verification"
        with database.connect(readonly=True) as connection:
            archived_ids.append(
                connection.execute(
                    "select pending_id from pending_captures where source_record_id = ?",
                    (payload.source_record_id,),
                ).fetchone()[0]
            )
        assert (
            capture_service.capture(
                payload,
                _verification(payload, verified_at=base + timedelta(seconds=index * 2)),
            ).status
            == "inserted"
        )

    active_payload = _payload(root, source_record_id="history-cleanup-active")
    assert capture_service.capture(active_payload).status == "pending_verification"

    with database.connect(readonly=True) as connection:
        retained = tuple(
            row["pending_id"]
            for row in connection.execute(
                """
                select pending_id from pending_capture_history
                order by strict_utc_epoch_us(finalized_at), pending_id
                """
            )
        )
        active_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
        trusted_counts = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ("source_refs", "behavior_memories", "import_receipts")
        )

    assert retained == tuple(archived_ids[1:])
    assert active_count == 1
    assert trusted_counts == (3, 24, 0)


def test_evicted_verified_history_still_prevents_requeueing_the_same_capture(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = registered_project
    monkeypatch.setattr(capture_module, "_MAX_PENDING_HISTORY", 1, raising=False)
    monkeypatch.setattr(capture_module, "_PENDING_HISTORY_CLEANUP_BATCH", 1, raising=False)
    base = datetime(2026, 7, 19, 0, 0, tzinfo=timezone.utc)
    clock = iter(base + timedelta(seconds=index) for index in range(6))
    monkeypatch.setattr(capture_service, "_now", lambda: next(clock))
    evicted = _payload(root, source_record_id="thread-a")
    trusted_evicted = _payload(root, source_record_id="session-a:turn-a")
    retained = _payload(
        root,
        source_record_id="thread-b",
        outcome="retained verified outcome",
    )
    trusted_retained = _payload(
        root,
        source_record_id="session-b:turn-b",
        outcome="retained verified outcome",
    )

    assert capture_service.capture(evicted).status == "pending_verification"
    assert (
        capture_service.capture(
            trusted_evicted,
            _verification(trusted_evicted, verified_at=base),
        ).status
        == "inserted"
    )
    assert capture_service.capture(retained).status == "pending_verification"
    assert (
        capture_service.capture(
            trusted_retained,
            _verification(trusted_retained, verified_at=base + timedelta(seconds=2)),
        ).status
        == "inserted"
    )

    duplicate = capture_service.capture(evicted)
    independent = capture_service.capture(_payload(root, source_record_id="thread-c"))

    assert duplicate.status == "pending_verification"
    assert duplicate.duplicate is True
    assert independent.status == "pending_verification"
    assert independent.duplicate is False
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 1
        assert (
            connection.execute("select source_record_id from pending_capture_history").fetchone()[0]
            == retained.source_record_id
        )
        assert (
            connection.execute(
                "select capture_correlation_id from source_refs where source_record_id = ?",
                (trusted_evicted.source_record_id,),
            ).fetchone()[0]
            == evicted.source_record_id
        )


def test_pending_capture_global_capacity_is_enforced_across_projects(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects, first_root = registered_project
    second_root = tmp_path / "second-project"
    second_root.mkdir()
    projects.register(ProjectCandidate(canonical_path=second_root, display_name="Second"))
    monkeypatch.setattr(capture_module, "_MAX_PENDING_PER_PROJECT", 10)
    monkeypatch.setattr(capture_module, "_MAX_PENDING_GLOBAL", 1)

    assert (
        capture_service.capture(
            _payload(first_root, source_record_id="global-capacity-first")
        ).status
        == "pending_verification"
    )
    with pytest.raises(capture_module.PendingCaptureCapacityError):
        capture_service.capture(_payload(second_root, source_record_id="global-capacity-second"))

    assert _counts(database)["pending_captures"] == 1


def test_resolution_declarations_are_normalized_and_deduplicated_first_seen(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "deduplicated-resolution-declarations",
        resolved_open_issues=(
            "  first   old issue  ",
            "second old issue",
            "first old issue",
            " second   old   issue ",
        ),
    )

    result = capture_service.capture(payload)

    assert result.status == "pending_verification"
    with database.connect(readonly=True) as connection:
        row = connection.execute("select structured_payload_json from pending_captures").fetchone()
    assert row is not None
    stored = json.loads(row["structured_payload_json"])
    assert stored["resolved_open_issues"] == ["first old issue", "second old issue"]


@pytest.mark.parametrize("verified", (False, True), ids=("unverified", "verified"))
def test_blank_normalized_resolution_rejects_the_whole_capture(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    verified: bool,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "blank-resolution",
        outcome="new behavior that must not be written",
        resolved_open_issues=(" \t\n ",),
    )

    result = capture_service.capture(
        payload,
        _verification(payload) if verified else None,
    )

    assert result.status == "rejected"
    assert _capture_counts_with_audit(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
    }


@pytest.mark.parametrize("verified", (False, True), ids=("unverified", "verified"))
def test_open_and_resolved_contradiction_rejects_before_any_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    verified: bool,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "contradictory-issue-declaration",
        open_issues=("  exact   old issue ",),
        resolved_open_issues=("exact old issue",),
    )

    result = capture_service.capture(
        payload,
        _verification(payload) if verified else None,
    )

    assert result.status == "rejected"
    assert result.inserted_ids == ()
    assert _capture_counts_with_audit(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
    }


@pytest.mark.parametrize(
    (
        "new_memory",
        "resolution_case",
        "expected_status",
        "expected_counts",
    ),
    (
        (True, "none", "inserted", (0, 0, 0)),
        (True, "matched", "inserted", (1, 0, 0)),
        (True, "not-found", "partial", (0, 0, 1)),
        (False, "matched", "resolved", (1, 0, 0)),
        (False, "not-found", "partial", (0, 0, 1)),
        (False, "already-resolved", "duplicate", (0, 1, 0)),
    ),
    ids=(
        "memory-only",
        "memory-and-resolution",
        "memory-and-not-found",
        "resolution-only",
        "not-found-only",
        "already-resolved-only",
    ),
)
def test_verified_capture_status_matrix_and_resolution_counts(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    new_memory: bool,
    resolution_case: str,
    expected_status: str,
    expected_counts: tuple[int, int, int],
) -> None:
    _, root = registered_project
    target_id: UUID | None = None
    declarations: tuple[str, ...] = ()
    if resolution_case in {"matched", "already-resolved"}:
        target_id = _seed_open_issue(
            capture_service,
            root,
            "exact old issue",
            f"matrix-target-{resolution_case}",
            verified_at=VERIFIED_AT - timedelta(seconds=2),
        )
        declarations = ("exact old issue",)
    elif resolution_case == "not-found":
        declarations = ("unknown old issue",)

    if resolution_case == "already-resolved":
        first_resolution = _resolution_payload(
            root,
            "matrix-first-resolution",
            resolved_open_issues=declarations,
        )
        first_result = capture_service.capture(
            first_resolution,
            _verification(
                first_resolution,
                verified_at=VERIFIED_AT - timedelta(seconds=1),
            ),
        )
        assert first_result.status == "resolved"
        assert first_result.resolved_count == 1

    payload = _resolution_payload(
        root,
        f"matrix-final-{new_memory}-{resolution_case}",
        outcome="new captured outcome" if new_memory else "",
        resolved_open_issues=declarations,
    )

    result = capture_service.capture(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )

    assert result.status == expected_status
    assert (
        result.resolved_count,
        result.already_resolved_count,
        result.unmatched_resolution_count,
    ) == expected_counts
    assert result.duplicate is (expected_status == "duplicate")
    assert len(result.inserted_ids) == int(new_memory)
    if target_id is not None:
        assert _lifecycle(database, target_id) == "archived"


def test_complete_verified_source_replay_has_zero_resolution_counts(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    target_id = _seed_open_issue(
        capture_service,
        root,
        "exact old issue",
        "complete-replay-target",
        verified_at=VERIFIED_AT - timedelta(seconds=1),
    )
    payload = _resolution_payload(
        root,
        "complete-resolution-replay",
        resolved_open_issues=("exact old issue",),
    )
    verification = _verification(payload, verified_at=VERIFIED_AT)

    first = capture_service.capture(payload, verification)
    replay = capture_service.capture(payload, verification)

    assert first.status == "resolved"
    assert first.resolved_count == 1
    assert replay.status == "duplicate"
    assert replay.duplicate is True
    assert replay.inserted_ids == ()
    assert (
        replay.resolved_count,
        replay.already_resolved_count,
        replay.unmatched_resolution_count,
    ) == (0, 0, 0)
    assert _lifecycle(database, target_id) == "archived"
    assert _resolution_audit_count(database) == 1


@pytest.mark.parametrize(
    "timestamp_mismatch",
    ("source-row", "verification", "naive-verification"),
)
def test_source_replay_rejects_unproven_verification_timestamp(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    timestamp_mismatch: str,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "timestamp-provenance-replay",
        outcome="timestamp provenance captured",
    )
    verification = _verification(payload, verified_at=VERIFIED_AT)
    assert capture_service.capture(payload, verification).status == "inserted"
    replay_verification = verification
    if timestamp_mismatch == "source-row":
        with database.transaction() as connection:
            connection.execute(
                "update source_refs set source_timestamp = ? where source_record_id = ?",
                (
                    (VERIFIED_AT + timedelta(microseconds=1)).isoformat(),
                    payload.source_record_id,
                ),
            )
    elif timestamp_mismatch == "verification":
        replay_verification = _verification(
            payload,
            verified_at=VERIFIED_AT + timedelta(microseconds=1),
        )
    else:
        replay_verification = _verification(
            payload,
            verified_at=VERIFIED_AT.replace(tzinfo=None),
        )
    before = _capture_counts_with_audit(database)

    replay = capture_service.capture(payload, replay_verification)

    assert replay.status == "rejected"
    assert replay.duplicate is False
    assert _capture_counts_with_audit(database) == before


def test_same_agent_source_record_with_different_content_is_rejected(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    first = _resolution_payload(
        root,
        "immutable-source-content",
        outcome="first canonical content",
    )
    assert (
        capture_service.capture(
            first,
            _verification(first, verified_at=VERIFIED_AT),
        ).status
        == "inserted"
    )
    changed = _resolution_payload(
        root,
        first.source_record_id,
        outcome="changed canonical content",
    )
    before = _capture_counts_with_audit(database)

    replay = capture_service.capture(
        changed,
        _verification(changed, verified_at=VERIFIED_AT),
    )

    assert replay.status == "rejected"
    assert replay.duplicate is False
    assert _capture_counts_with_audit(database) == before


def test_chatgpt_receipt_does_not_relax_public_or_readonly_timestamp_validation(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    namespace = Namespace(
        source_agent=SourceAgent.CHATGPT,
        model_id="gpt-5.6-chatgpt",
    )
    payload = _resolution_payload(
        root,
        "conversation:strict-timestamp",
        namespace=namespace,
        outcome="ChatGPT source stays strict",
    )
    original_verification = _verification(payload, verified_at=VERIFIED_AT)
    assert capture_service.capture(payload, original_verification).status == "inserted"
    CheckpointRepository(database).commit_import_receipt(
        "a" * 64,
        payload.source_record_id,
        SourceAgent.CHATGPT,
    )
    later_verification = _verification(
        payload,
        verified_at=VERIFIED_AT + timedelta(seconds=1),
    )
    prepared = capture_service.prepare_verified(payload, later_verification)
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        capture_service.validate_prepared_readonly(prepared)
    replay = capture_service.capture(payload, later_verification)

    assert replay.status == "rejected"
    assert replay.duplicate is False
    assert _capture_counts_with_audit(database) == before


def test_prior_codex_receipt_proof_cannot_be_reused_on_another_transaction(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "session:transaction-bound-proof",
        outcome="proof stays on its live transaction",
    )
    original = _verification(payload, verified_at=VERIFIED_AT)
    assert capture_service.capture(payload, original).status == "inserted"
    checkpoints = CheckpointRepository(database)
    checkpoints.commit_import_receipt(
        "b" * 64,
        payload.source_record_id,
        SourceAgent.CODEX,
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT + timedelta(seconds=1)),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    with database.transaction() as first_connection:
        proof = checkpoints.prior_codex_receipt_proof_on_connection(
            first_connection,
            payload.source_record_id,
        )
    assert proof is not None
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        with database.transaction() as second_connection:
            capture_service.capture_prepared_on_connection(
                second_connection,
                prepared,
                prior_codex_receipt_proof=proof,
            )

    assert _capture_counts_with_audit(database) == before


def test_duck_typed_receipt_proof_cannot_bypass_missing_receipt(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "session:forged-proof",
        outcome="runtime proof types stay closed",
    )
    original = _verification(payload, verified_at=VERIFIED_AT)
    assert capture_service.capture(payload, original).status == "inserted"
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT + timedelta(seconds=1)),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    before = _capture_counts_with_audit(database)

    class ForgedProof:
        @staticmethod
        def matches(*_args: object) -> bool:
            return True

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            capture_service.capture_prepared_on_connection(
                connection,
                prepared,
                prior_codex_receipt_proof=ForgedProof(),  # type: ignore[arg-type]
            )

    assert _capture_counts_with_audit(database) == before


@pytest.mark.parametrize(
    "provenance_mismatch",
    ("agent", "content", "parser", "path", "uuid"),
)
def test_prior_codex_receipt_proof_does_not_relax_non_timestamp_provenance(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    provenance_mismatch: str,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        f"session:proof-{provenance_mismatch}",
        outcome="all non-time provenance stays exact",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT + timedelta(seconds=1)),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    source_agent = payload.namespace.source_agent.value
    content_hash = prepared.structured_hash
    parser_version = "capture-v1"
    source_path = None
    source_reference_id = "00000000-0000-4000-8000-000000000123"
    if provenance_mismatch == "agent":
        source_agent = SourceAgent.CHATGPT.value
    elif provenance_mismatch == "content":
        content_hash = "0" * 64
    elif provenance_mismatch == "parser":
        parser_version = "legacy-v1"
    elif provenance_mismatch == "path":
        source_path = "legacy/source.jsonl"
    else:
        source_reference_id = "malformed-uuid"
    with database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_reference_id,
                source_agent,
                payload.source_record_id,
                source_path,
                content_hash,
                VERIFIED_AT.isoformat(),
                parser_version,
                VERIFIED_AT.isoformat(),
                str(prepared.project.project_id).lower(),
                payload.namespace.model_id,
            ),
        )
    checkpoints = CheckpointRepository(database)
    checkpoints.commit_import_receipt(
        "c" * 64,
        payload.source_record_id,
        SourceAgent.CODEX,
    )
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            proof = checkpoints.prior_codex_receipt_proof_on_connection(
                connection,
                payload.source_record_id,
            )
            assert proof is not None
            capture_service.capture_prepared_on_connection(
                connection,
                prepared,
                prior_codex_receipt_proof=proof,
            )

    assert _capture_counts_with_audit(database) == before


def test_prior_codex_receipt_proof_rejects_ambiguous_source_rows(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "session:ambiguous-source-rows",
        outcome="one logical source has one row",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT + timedelta(seconds=1)),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    with database.transaction() as connection:
        for suffix, content_hash in (
            ("1", prepared.structured_hash),
            ("2", "0" * 64),
        ):
            connection.execute(
                """
                insert into source_refs(
                    source_reference_id, source_agent, source_record_id, source_path,
                    content_hash, source_timestamp, parser_version, created_at,
                    capture_project_id, capture_model_id
                ) values (?, ?, ?, null, ?, ?, 'capture-v1', ?, ?, ?)
                """,
                (
                    f"00000000-0000-4000-8000-00000000012{suffix}",
                    payload.namespace.source_agent.value,
                    payload.source_record_id,
                    content_hash,
                    VERIFIED_AT.isoformat(),
                    VERIFIED_AT.isoformat(),
                    str(prepared.project.project_id).lower(),
                    payload.namespace.model_id,
                ),
            )
    checkpoints = CheckpointRepository(database)
    checkpoints.commit_import_receipt(
        "d" * 64,
        payload.source_record_id,
        SourceAgent.CODEX,
    )
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            proof = checkpoints.prior_codex_receipt_proof_on_connection(
                connection,
                payload.source_record_id,
            )
            assert proof is not None
            capture_service.capture_prepared_on_connection(
                connection,
                prepared,
                prior_codex_receipt_proof=proof,
            )

    assert _capture_counts_with_audit(database) == before


def test_source_replay_does_not_verify_a_pending_capture_created_after_source(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "source-before-pending",
        outcome="captured before pending",
    )
    verification = _verification(payload, verified_at=VERIFIED_AT)
    assert capture_service.capture(payload, verification).status == "inserted"
    with database.transaction() as connection:
        source = connection.execute(
            """
            select capture_project_id, content_hash from source_refs
            where source_agent = ? and source_record_id = ?
            """,
            (payload.namespace.source_agent.value, payload.source_record_id),
        ).fetchone()
        created_at = VERIFIED_AT + timedelta(seconds=1)
        connection.execute(
            """
            insert into pending_captures(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_payload_json, structured_hash,
                created_at, expires_at, verification_state
            ) values (?, ?, ?, ?, ?, '{}', ?, ?, ?, 'pending')
            """,
            (
                str(uuid4()).lower(),
                source["capture_project_id"],
                payload.namespace.source_agent.value,
                payload.namespace.model_id,
                payload.source_record_id,
                source["content_hash"],
                created_at.isoformat(),
                (created_at + timedelta(days=7)).isoformat(),
            ),
        )

    replay = capture_service.capture(payload, verification)

    assert replay.status == "duplicate"
    with database.connect(readonly=True) as connection:
        state = connection.execute("select verification_state from pending_captures").fetchone()[0]
    assert state == "pending"


def test_exact_recovery_cannot_rebind_an_existing_source_correlation(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    pending_a = _resolution_payload(
        root,
        "thread-a",
        outcome="one trusted source correlation",
    )
    trusted = _resolution_payload(
        root,
        "session-a:turn-a",
        outcome="one trusted source correlation",
    )
    verification = _verification(trusted)
    assert capture_service.capture(pending_a).status == "pending_verification"
    assert capture_service.capture(trusted, verification).status == "inserted"

    pending_b = _resolution_payload(
        root,
        "thread-b",
        outcome="one trusted source correlation",
    )
    assert capture_service.capture(pending_b).status == "pending_verification"
    with database.connect(readonly=True) as connection:
        pending_id = UUID(
            connection.execute(
                "select pending_id from pending_captures where source_record_id = ?",
                (pending_b.source_record_id,),
            ).fetchone()[0]
        )
    prepared = capture_service.prepare_verified(trusted, verification)
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            capture_service.capture_prepared_on_connection(
                connection,
                prepared,
                exact_pending_id=pending_id,
            )

    assert _capture_counts_with_audit(database) == before
    with database.connect(readonly=True) as connection:
        correlation = connection.execute(
            "select capture_correlation_id from source_refs where source_record_id = ?",
            (trusted.source_record_id,),
        ).fetchone()[0]
        active_correlation = connection.execute(
            "select source_record_id from pending_captures"
        ).fetchone()[0]
    assert correlation == pending_a.source_record_id
    assert active_correlation == pending_b.source_record_id


def test_exact_recovery_cannot_use_receipt_to_relax_backward_timestamp(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    base = datetime.now(timezone.utc)
    trusted = _resolution_payload(
        root,
        "session-receipted:turn-trusted",
        outcome="one timestamp-bound trusted source",
    )
    trusted_verification = _verification(
        trusted,
        verified_at=base + timedelta(seconds=1),
    )
    assert capture_service.capture(trusted, trusted_verification).status == "inserted"
    pending = _resolution_payload(
        root,
        "thread-pending-recovery",
        outcome="one timestamp-bound trusted source",
    )
    assert capture_service.capture(pending).status == "pending_verification"
    with database.connect(readonly=True) as connection:
        pending_id = UUID(
            connection.execute(
                "select pending_id from pending_captures where source_record_id = ?",
                (pending.source_record_id,),
            ).fetchone()[0]
        )
    checkpoints = CheckpointRepository(database)
    checkpoints.commit_import_receipt(
        "e" * 64,
        trusted.source_record_id,
        SourceAgent.CODEX,
    )
    earlier_verification = _verification(trusted, verified_at=base)
    prepared = capture_service.prepare_verified(trusted, earlier_verification)
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            proof = checkpoints.prior_codex_receipt_proof_on_connection(
                connection,
                trusted.source_record_id,
            )
            assert proof is not None
            capture_service.capture_prepared_on_connection(
                connection,
                prepared,
                prior_codex_receipt_proof=proof,
                exact_pending_id=pending_id,
            )

    assert _capture_counts_with_audit(database) == before
    with database.connect(readonly=True) as connection:
        source = connection.execute(
            """
            select capture_correlation_id from source_refs
            where source_record_id = ?
            """,
            (trusted.source_record_id,),
        ).fetchone()
        active_pending = connection.execute(
            """
            select verification_state from pending_captures
            where pending_id = ?
            """,
            (str(pending_id).lower(),),
        ).fetchone()
    assert source["capture_correlation_id"] is None
    assert active_pending["verification_state"] == "pending"


def test_last_observed_change_advances_only_for_new_memory_resolution_or_not_found(
    database: Database,
    registered_project: tuple[ProjectRepository, Path],
) -> None:
    projects, root = registered_project
    clock = [VERIFIED_AT]
    service = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
        now=lambda: clock[0],
    )
    target_id = _seed_open_issue(
        service,
        root,
        "exact old issue",
        "observed-change-target",
        verified_at=VERIFIED_AT - timedelta(seconds=1),
    )
    after_insert = _last_observed_change(database, root)
    assert after_insert is not None

    clock[0] = VERIFIED_AT + timedelta(seconds=10)
    resolution_payload = _resolution_payload(
        root,
        "observed-change-resolution",
        resolved_open_issues=("exact old issue",),
    )
    resolution_verification = _verification(
        resolution_payload,
        verified_at=VERIFIED_AT,
    )
    resolved = service.capture(resolution_payload, resolution_verification)
    after_resolution = _last_observed_change(database, root)
    assert resolved.status == "resolved"
    assert _lifecycle(database, target_id) == "archived"
    assert after_resolution != after_insert

    clock[0] = VERIFIED_AT + timedelta(seconds=20)
    already_payload = _resolution_payload(
        root,
        "observed-change-already",
        resolved_open_issues=("exact old issue",),
    )
    already = service.capture(
        already_payload,
        _verification(already_payload, verified_at=VERIFIED_AT + timedelta(seconds=1)),
    )
    assert already.status == "duplicate"
    assert already.already_resolved_count == 1
    assert _last_observed_change(database, root) == after_resolution

    clock[0] = VERIFIED_AT + timedelta(seconds=30)
    replay = service.capture(resolution_payload, resolution_verification)
    assert replay.status == "duplicate"
    assert replay.already_resolved_count == 0
    assert _last_observed_change(database, root) == after_resolution

    clock[0] = VERIFIED_AT + timedelta(seconds=40)
    missing_payload = _resolution_payload(
        root,
        "observed-change-not-found",
        resolved_open_issues=("unknown old issue",),
    )
    missing = service.capture(
        missing_payload,
        _verification(missing_payload, verified_at=VERIFIED_AT + timedelta(seconds=2)),
    )
    assert missing.status == "partial"
    assert missing.unmatched_resolution_count == 1
    assert _last_observed_change(database, root) != after_resolution


def test_resolution_archives_only_the_exact_project_source_and_model_namespace(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
) -> None:
    projects, root = registered_project
    other_root = tmp_path / "other-project"
    other_root.mkdir()
    projects.register(ProjectCandidate(canonical_path=other_root, display_name="Other"))
    other_agent = Namespace(
        source_agent=SourceAgent.CHATGPT,
        model_id=TARGET_NAMESPACE.model_id,
    )
    other_model = Namespace(
        source_agent=SourceAgent.CODEX,
        model_id="gpt-5.7-sol",
    )
    target_id = _seed_open_issue(
        capture_service,
        root,
        "exact old issue",
        "namespace-target",
        verified_at=VERIFIED_AT - timedelta(seconds=1),
    )
    other_project_id = _seed_open_issue(
        capture_service,
        other_root,
        "exact old issue",
        "namespace-other-project",
        verified_at=VERIFIED_AT - timedelta(seconds=1),
    )
    other_agent_id = _seed_open_issue(
        capture_service,
        root,
        "exact old issue",
        "namespace-other-agent",
        namespace=other_agent,
        verified_at=VERIFIED_AT - timedelta(seconds=1),
    )
    other_model_id = _seed_open_issue(
        capture_service,
        root,
        "exact old issue",
        "namespace-other-model",
        namespace=other_model,
        verified_at=VERIFIED_AT - timedelta(seconds=1),
    )
    payload = _resolution_payload(
        root,
        "namespace-resolution",
        resolved_open_issues=("exact old issue",),
    )

    result = capture_service.capture(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )

    assert result.status == "resolved"
    assert result.resolved_count == 1
    assert _lifecycle(database, target_id) == "archived"
    assert _lifecycle(database, other_project_id) == "active"
    assert _lifecycle(database, other_agent_id) == "active"
    assert _lifecycle(database, other_model_id) == "active"


def test_resolution_leaves_future_target_active_and_records_not_found(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    future_target_id = _seed_open_issue(
        capture_service,
        root,
        "future old issue",
        "future-target",
        verified_at=VERIFIED_AT + timedelta(microseconds=1),
    )
    payload = _resolution_payload(
        root,
        "future-resolution-attempt",
        resolved_open_issues=("future old issue",),
    )

    result = capture_service.capture(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )

    assert result.status == "partial"
    assert result.resolved_count == 0
    assert result.unmatched_resolution_count == 1
    assert _lifecycle(database, future_target_id) == "active"
    assert _resolution_audit_count(database) == 1


def test_same_source_reference_target_is_not_archived_on_source_replay(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "same-source-resolution",
        resolved_open_issues=("same source issue",),
    )
    verification = _verification(payload, verified_at=VERIFIED_AT)
    prepare = getattr(capture_service, "prepare_verified", None)
    assert callable(prepare), "CaptureService.prepare_verified is required"
    prepared = prepare(payload, verification)
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    source_reference_id = uuid4()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, ?, ?, null, ?, ?, 'capture-v1', ?, ?, ?)
            """,
            (
                str(source_reference_id).lower(),
                payload.namespace.source_agent.value,
                payload.source_record_id,
                prepared.structured_hash,
                VERIFIED_AT.isoformat(),
                prepared.captured_at.isoformat(),
                str(prepared.project.project_id).lower(),
                payload.namespace.model_id,
            ),
        )
        inserted = MemoryRepository(database)._insert_on_connection(
            connection,
            BehaviorMemoryInput(
                project_id=prepared.project.project_id,
                namespace=payload.namespace,
                task_fingerprint=hashlib.sha256(b"same-source-target").hexdigest(),
                memory_kind=MemoryKind.OPEN_ISSUE,
                normalized_content="same source issue",
                content_hash=hashlib.sha256(b"same source issue").hexdigest(),
                source_reference_id=source_reference_id,
                created_at=VERIFIED_AT,
                confidence=1.0,
            ),
        )
    assert inserted.record_id is not None

    result = capture_service.capture(payload, verification)

    assert result.status == "duplicate"
    assert result.duplicate is True
    assert (
        result.resolved_count,
        result.already_resolved_count,
        result.unmatched_resolution_count,
    ) == (0, 0, 0)
    assert _lifecycle(database, inserted.record_id) == "active"
    assert _resolution_audit_count(database) == 0


@pytest.mark.parametrize("reuse_scope", ("project", "model"))
def test_same_source_and_hash_reuse_across_project_or_exact_model_is_rejected(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
    reuse_scope: str,
) -> None:
    projects, root = registered_project
    first = _resolution_payload(
        root,
        "shared-source-and-hash",
        outcome="shared canonical outcome",
    )
    assert (
        capture_service.capture(
            first,
            _verification(first, verified_at=VERIFIED_AT),
        ).status
        == "inserted"
    )
    if reuse_scope == "project":
        second_root = tmp_path / "source-reuse-other-project"
        second_root.mkdir()
        projects.register(
            ProjectCandidate(canonical_path=second_root, display_name="Source reuse other")
        )
        namespace = TARGET_NAMESPACE
    else:
        second_root = root
        namespace = Namespace(
            source_agent=SourceAgent.CODEX,
            model_id="gpt-5.7-sol",
        )
    second = _resolution_payload(
        second_root,
        first.source_record_id,
        namespace=namespace,
        outcome="shared canonical outcome",
    )
    before = _capture_counts_with_audit(database)

    result = capture_service.capture(
        second,
        _verification(second, verified_at=VERIFIED_AT),
    )

    assert result.status == "rejected"
    assert result.duplicate is False
    assert _capture_counts_with_audit(database) == before


def test_new_capture_source_persists_exact_project_model_and_verification_time(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    projects, root = registered_project
    payload = _resolution_payload(
        root,
        "source-provenance-fields",
        outcome="source provenance captured",
    )
    verified_at = datetime(2026, 7, 16, 20, 0, tzinfo=timezone(timedelta(hours=8)))

    result = capture_service.capture(
        payload,
        _verification(payload, verified_at=verified_at),
    )

    assert result.status == "inserted"
    project = projects.find_by_cwd(root)
    assert project is not None
    with database.connect(readonly=True) as connection:
        source = connection.execute(
            """
            select capture_project_id, capture_model_id, source_timestamp
            from source_refs where source_record_id = ?
            """,
            (payload.source_record_id,),
        ).fetchone()
    assert source is not None
    assert source["capture_project_id"] == str(project.project_id).lower()
    assert source["capture_model_id"] == payload.namespace.model_id
    assert source["source_timestamp"] == "2026-07-16T12:00:00Z"


def test_null_legacy_capture_provenance_is_rejected_without_guessing(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "null-legacy-provenance",
        outcome="must not attach to legacy source",
    )
    assert capture_service.capture(payload).status == "pending_verification"
    with database.transaction() as connection:
        pending = connection.execute("select structured_hash from pending_captures").fetchone()
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, ?, ?, null, ?, ?, 'capture-v1', ?, null, null)
            """,
            (
                "00000000-0000-4000-8000-000000000099",
                payload.namespace.source_agent.value,
                payload.source_record_id,
                pending["structured_hash"],
                VERIFIED_AT.isoformat(),
                VERIFIED_AT.isoformat(),
            ),
        )
    before = _capture_counts_with_audit(database)

    result = capture_service.capture(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )

    assert result.status == "rejected"
    assert result.duplicate is False
    assert _capture_counts_with_audit(database) == before
    with database.connect(readonly=True) as connection:
        state = connection.execute("select verification_state from pending_captures").fetchone()[0]
    assert state == "pending"


def test_capture_prepared_on_outer_transaction_rolls_back_every_task5_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    target_id = _seed_open_issue(
        capture_service,
        root,
        "rollback old issue",
        "rollback-target",
        verified_at=VERIFIED_AT - timedelta(seconds=1),
    )
    payload = _resolution_payload(
        root,
        "rollback-resolution",
        outcome="new memory rolled back",
        resolved_open_issues=("rollback old issue",),
    )
    verification = _verification(payload, verified_at=VERIFIED_AT)
    prepare = getattr(capture_service, "prepare_verified", None)
    capture_prepared = getattr(capture_service, "capture_prepared_on_connection", None)
    assert callable(prepare), "CaptureService.prepare_verified is required"
    assert callable(capture_prepared), "CaptureService.capture_prepared_on_connection is required"
    prepared = prepare(payload, verification)
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    before = _capture_counts_with_audit(database)

    class SentinelRollback(RuntimeError):
        pass

    with pytest.raises(SentinelRollback):
        with database.transaction() as connection:
            result = capture_prepared(connection, prepared)
            assert result.status == "inserted"
            assert result.resolved_count == 1
            assert (
                connection.execute(
                    "select lifecycle_state from behavior_memories where memory_id = ?",
                    (str(target_id).lower(),),
                ).fetchone()[0]
                == "archived"
            )
            assert (
                connection.execute("select count(*) from memory_issue_resolutions").fetchone()[0]
                == 1
            )
            raise SentinelRollback

    assert _capture_counts_with_audit(database) == before
    assert _lifecycle(database, target_id) == "active"


def test_validate_prepared_readonly_does_not_consume_or_write_before_live_capture(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "readonly-preflight-before-live",
        outcome="readonly preflight preserves prepared capture",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    before = _capture_counts_with_audit(database)

    def reject_transaction(_self):
        raise AssertionError("readonly preflight opened a write transaction")

    with monkeypatch.context() as patcher:
        patcher.setattr(Database, "transaction", reject_transaction)
        capture_service.validate_prepared_readonly(prepared)

    assert _capture_counts_with_audit(database) == before
    with database.transaction() as connection:
        result = capture_service.capture_prepared_on_connection(connection, prepared)

    assert result.status == "inserted"


def test_validate_prepared_readonly_rejects_replaced_object_without_any_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "readonly-preflight-replaced-object",
        outcome="only the registered prepared object is trusted",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    replaced = replace(
        prepared,
        captured_at=prepared.captured_at + timedelta(microseconds=1),
    )
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        capture_service.validate_prepared_readonly(replaced)

    assert _capture_counts_with_audit(database) == before


@pytest.mark.parametrize("mutation", ("namespace", "source-record", "project"))
def test_capture_prepared_rejects_payload_mutation_before_any_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
    mutation: str,
) -> None:
    projects, root = registered_project
    payload = _resolution_payload(
        root,
        "prepared-payload-integrity",
        outcome="prepared payload must stay bound",
    )
    verification = _verification(payload, verified_at=VERIFIED_AT)
    prepared = capture_service.prepare_verified(payload, verification)
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    if mutation == "namespace":
        prepared.payload.namespace = Namespace(
            source_agent=SourceAgent.CHATGPT,
            model_id="gpt-5.6-sol",
        )
    elif mutation == "source-record":
        prepared.payload.source_record_id = "tampered-source-record"
    else:
        other_root = tmp_path / "prepared-other-project"
        other_root.mkdir()
        projects.register(
            ProjectCandidate(canonical_path=other_root, display_name="Prepared other")
        )
        prepared.payload.cwd = other_root
    before = _capture_counts_with_audit(database)

    with pytest.raises(RuntimeError):
        with database.transaction() as connection:
            capture_service.capture_prepared_on_connection(connection, prepared)

    assert _capture_counts_with_audit(database) == before


@pytest.mark.parametrize("time_field", ("captured-at", "verified-at"))
def test_capture_prepared_rejects_replaced_time_snapshot_before_any_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    time_field: str,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "prepared-time-integrity",
        outcome="prepared time must stay trusted",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    replacement_time = datetime(
        2030,
        1,
        2,
        3,
        4,
        5,
        tzinfo=timezone(timedelta(hours=8)),
    )
    if time_field == "captured-at":
        replaced = replace(prepared, captured_at=replacement_time)
    else:
        replaced_verification = prepared.verification.model_copy(
            update={"verified_at": replacement_time}
        )
        replaced = replace(prepared, verification=replaced_verification)
    before = _capture_counts_with_audit(database)

    with database.transaction() as connection:
        before_changes = connection.total_changes
        with pytest.raises(RuntimeError):
            capture_service.capture_prepared_on_connection(connection, replaced)
        assert connection.total_changes == before_changes

    assert _capture_counts_with_audit(database) == before


def test_capture_prepared_rejects_replaced_project_snapshot_before_any_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "prepared-project-integrity",
        outcome="prepared project must stay trusted",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    forged_root = tmp_path / "forged-prepared-project"
    forged_root.mkdir()
    replaced_project = prepared.project.model_copy(update={"canonical_path": forged_root})
    replaced = replace(prepared, project=replaced_project)
    before = _capture_counts_with_audit(database)

    with database.transaction() as connection:
        before_changes = connection.total_changes
        with pytest.raises(RuntimeError):
            capture_service.capture_prepared_on_connection(connection, replaced)
        assert connection.total_changes == before_changes

    assert _capture_counts_with_audit(database) == before


def test_capture_prepared_rejects_live_project_identity_drift_before_any_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "prepared-live-project-identity",
        outcome="live project identity must stay current",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    displaced = tmp_path / "prepared-live-project-displaced"
    root.rename(displaced)
    root.mkdir()
    before = _capture_counts_with_audit(database)

    with database.transaction() as connection:
        before_changes = connection.total_changes
        with pytest.raises(RuntimeError):
            capture_service.capture_prepared_on_connection(connection, prepared)
        assert connection.total_changes == before_changes

    assert _capture_counts_with_audit(database) == before


def test_capture_prepared_rejects_device_change_after_darwin_persisted_drift(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects, root = registered_project
    project = projects.find_by_cwd(root)
    assert project is not None
    metadata = root.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project.project_id)),
        )
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    payload = _resolution_payload(
        root,
        "prepared-darwin-device-change",
        outcome="in-process device changes must fail closed",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    before = _capture_counts_with_audit(database)
    monkeypatch.setattr(
        path_identity_module,
        "complete_directory_identity",
        lambda _path: (metadata.st_dev + 2, metadata.st_ino),
    )
    monkeypatch.setattr(
        projects_module,
        "complete_directory_identity",
        lambda _path: (metadata.st_dev + 2, metadata.st_ino),
    )

    with database.transaction() as connection:
        before_changes = connection.total_changes
        with pytest.raises(RuntimeError):
            capture_service.capture_prepared_on_connection(connection, prepared)
        assert connection.total_changes == before_changes

    assert _capture_counts_with_audit(database) == before


def test_capture_prepared_snapshot_is_single_use(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _resolution_payload(
        root,
        "prepared-single-use",
        outcome="prepared snapshot consumed once",
    )
    prepared = capture_service.prepare_verified(
        payload,
        _verification(payload, verified_at=VERIFIED_AT),
    )
    assert prepared.__class__.__name__ == "PreparedVerifiedCapture"
    with database.transaction() as connection:
        assert (
            capture_service.capture_prepared_on_connection(connection, prepared).status
            == "inserted"
        )
    before = _capture_counts_with_audit(database)

    with database.transaction() as connection:
        before_changes = connection.total_changes
        with pytest.raises(RuntimeError):
            capture_service.capture_prepared_on_connection(connection, prepared)
        assert connection.total_changes == before_changes

    assert _capture_counts_with_audit(database) == before


def test_matching_verification_creates_one_memory_per_nonempty_structured_item(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _payload(root)

    result = capture_service.capture(payload, _verification(payload))

    assert result.status == "inserted"
    assert result.duplicate is False
    assert len(result.inserted_ids) == 8
    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select memory_id, memory_kind, normalized_content, confidence
            from behavior_memories order by rowid
            """
        ).fetchall()
    assert [row["memory_kind"] for row in rows] == [
        MemoryKind.DECISION.value,
        MemoryKind.FAILED_ATTEMPT.value,
        MemoryKind.VERIFIED_METHOD.value,
        MemoryKind.PREFERENCE.value,
        MemoryKind.RISK.value,
        MemoryKind.OPEN_ISSUE.value,
        MemoryKind.REUSABLE_LESSON.value,
        MemoryKind.OUTCOME.value,
    ]
    assert [row["memory_id"] for row in rows] == [
        str(memory_id) for memory_id in result.inserted_ids
    ]
    assert rows[0]["normalized_content"] == "use sqlite transactions"
    assert all(row["confidence"] == 1.0 for row in rows)


def test_verified_capture_is_exactly_once_and_redacts_before_all_persistence(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    secret = _secret()
    payload = _payload(
        root,
        objective=f"protect {secret}",
        outcome=f"removed {secret}",
        decisions=[f"never store {secret}"],
        failed_attempts=[],
        verified_commands=[],
        changed_paths=[f"notes/{secret}.md"],
        preferences=[],
        risks=[],
        open_issues=[],
        reusable_lessons=[],
    )
    verification = _verification(payload)

    first = capture_service.capture(payload, verification)
    repeated = capture_service.capture(payload, verification)

    assert first.status == "inserted"
    assert repeated.status == "duplicate"
    assert repeated.duplicate is True
    assert repeated.inserted_ids == ()
    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 1,
        "behavior_memories": 2,
    }
    with database.connect(readonly=True) as connection:
        persisted = "\n".join(
            str(value)
            for table in ("source_refs", "behavior_memories", "pending_captures")
            for row in connection.execute(f"select * from {table}").fetchall()
            for value in row
            if value is not None
        )
    if secret in persisted:
        pytest.fail("synthetic secret persisted", pytrace=False)


def test_unknown_project_returns_typed_result_and_writes_nothing(
    database: Database,
    tmp_path: Path,
) -> None:
    unknown = tmp_path / "unknown-project"
    unknown.mkdir()
    projects = ProjectRepository(database)
    service = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
    )

    result = service.capture(_payload(unknown))

    assert result.status == "project_not_found"
    assert result.duplicate is False
    assert result.inserted_ids == ()
    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


def test_capture_does_not_fall_back_to_outer_project_when_inner_is_disabled(
    database: Database,
    tmp_path: Path,
) -> None:
    outer = tmp_path / "workspace"
    inner = outer / "packages" / "inner"
    inner.mkdir(parents=True)
    projects = ProjectRepository(database)
    outer_record = projects.register(ProjectCandidate(canonical_path=outer, display_name="Outer"))
    inner_record = projects.register(ProjectCandidate(canonical_path=inner, display_name="Inner"))
    projects.set_enabled(inner_record.project_id, False)
    service = CaptureService(
        database,
        projects,
        MemoryRepository(database),
        Redactor(),
    )

    result = service.capture(_payload(inner))

    assert result.status == "project_not_found"
    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from pending_captures where project_id = ?",
                (str(outer_record.project_id),),
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("verified", (False, True))
def test_capture_rejects_a_project_replaced_after_cwd_resolution(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    projects, root = registered_project
    original_find = projects.find_by_cwd
    displaced = tmp_path / "synthetic-project-displaced"

    def replace_after_find(cwd: Path):
        project = original_find(cwd)
        assert project is not None
        root.rename(displaced)
        root.mkdir()
        return project

    monkeypatch.setattr(projects, "find_by_cwd", replace_after_find)
    payload = _payload(root)

    result = capture_service.capture(
        payload,
        _verification(payload) if verified else None,
    )

    assert result.status == "project_not_found"
    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


def test_verified_capture_rolls_back_if_project_is_replaced_after_memory_write(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _projects, root = registered_project
    memories = capture_service._memories
    original_insert = memories._insert_on_connection
    displaced = tmp_path / "synthetic-project-displaced"
    replaced = False

    def replace_after_insert(*args, **kwargs):
        nonlocal replaced
        result = original_insert(*args, **kwargs)
        if not replaced:
            replaced = True
            root.rename(displaced)
            root.mkdir()
        return result

    monkeypatch.setattr(memories, "_insert_on_connection", replace_after_insert)
    payload = _payload(root)

    result = capture_service.capture(payload, _verification(payload))

    assert result.status == "project_not_found"
    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


def test_unverified_capture_is_pending_and_matching_adapter_verifies_once(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _payload(
        root,
        failed_attempts=[],
        verified_commands=[],
        preferences=[],
        risks=[],
        open_issues=[],
        reusable_lessons=[],
    )

    pending = capture_service.capture(payload)
    duplicate_pending = capture_service.capture(payload)

    assert pending.status == "pending_verification"
    assert pending.duplicate is False
    assert duplicate_pending.status == "pending_verification"
    assert duplicate_pending.duplicate is True
    project = ProjectRepository(database).find_by_cwd(root)
    assert project is not None
    assert (
        MemoryRepository(database).search(project.project_id, payload.namespace, "sqlite", 20) == []
    )

    verification = _verification(payload)
    verified = capture_service.capture(payload, verification)
    repeated = capture_service.capture(payload, verification)

    assert verified.status == "inserted"
    assert len(verified.inserted_ids) == 2
    assert repeated.status == "duplicate"
    assert repeated.inserted_ids == ()
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0
        history = connection.execute(
            "select final_state, source_reference_id from pending_capture_history"
        ).fetchone()
        history_columns = {
            row["name"] for row in connection.execute("pragma table_info(pending_capture_history)")
        }
    assert history is not None
    assert history["final_state"] == "verified"
    assert history["source_reference_id"] is not None
    assert "structured_payload_json" not in history_columns


@pytest.mark.parametrize(
    "updates,error_field",
    (
        (
            {
                "namespace": Namespace(
                    source_agent=SourceAgent.CODEX,
                    model_id="provider/password=RAW_MODEL_CREDENTIAL",
                )
            },
            "model_id",
        ),
        ({"source_record_id": "password=RAW_SOURCE_SECRET"}, "source_record_id"),
        ({"source_record_id": "intranet:PRIVATE_REPO.git"}, "source_record_id"),
        ({"source_record_id": ".env"}, "source_record_id"),
        ({"source_record_id": "id_rsa"}, "source_record_id"),
    ),
    ids=(
        "model-id",
        "source-record-id-secret",
        "source-record-id-remote",
        "source-record-id-env",
        "source-record-id-private-key",
    ),
)
def test_untrusted_capture_rejects_unsafe_provenance_before_persistence(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    updates: dict[str, object],
    error_field: str,
) -> None:
    _, root = registered_project

    payload = _payload(root, **updates)
    for verification in (None, _verification(payload)):
        with pytest.raises(ValueError, match=f"invalid {error_field}"):
            capture_service.capture(payload, verification)

    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


@pytest.mark.parametrize(
    "verification_updates",
    [
        {"source_record_id": "different-record"},
        {
            "namespace": Namespace(
                source_agent=SourceAgent.CODEX,
                model_id="different-model",
            )
        },
        {"verified_by": "chatgpt_adapter"},
    ],
)
def test_verification_mismatch_rejects_without_changing_pending(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    verification_updates: dict[str, object],
) -> None:
    _, root = registered_project
    payload = _payload(root)
    capture_service.capture(payload)
    with database.connect(readonly=True) as connection:
        before = tuple(connection.execute("select * from pending_captures").fetchone())

    result = capture_service.capture(
        payload,
        _verification(payload, **verification_updates),
    )

    assert result.status == "rejected"
    assert result.inserted_ids == ()
    with database.connect(readonly=True) as connection:
        after = tuple(connection.execute("select * from pending_captures").fetchone())
    assert after == before
    assert _counts(database) == {
        "pending_captures": 1,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


def test_empty_redacted_behavior_rejects_without_writes(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _payload(
        root,
        outcome="  ",
        decisions=["  "],
        failed_attempts=[],
        verified_commands=[],
        preferences=[],
        risks=[],
        open_issues=[],
        reusable_lessons=[],
    )

    pending = capture_service.capture(payload)
    verified = capture_service.capture(payload, _verification(payload))

    assert pending.status == "rejected"
    assert verified.status == "rejected"
    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


def test_failed_multirow_capture_rolls_back_source_and_memories(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _payload(
        root,
        verified_commands=[],
        preferences=[],
        risks=[],
        open_issues=[],
        reusable_lessons=[],
    )
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger fail_second_capture_row
            before insert on behavior_memories
            when new.memory_kind = 'failed_attempt'
            begin
                select raise(abort, 'synthetic capture failure');
            end
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic capture failure"):
        capture_service.capture(payload, _verification(payload))

    assert _counts(database) == {
        "pending_captures": 0,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


def test_failed_verified_pending_capture_keeps_pending_row_unchanged(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    payload = _payload(
        root,
        verified_commands=[],
        preferences=[],
        risks=[],
        open_issues=[],
        reusable_lessons=[],
    )
    capture_service.capture(payload)
    with database.connect(readonly=True) as connection:
        before = tuple(connection.execute("select * from pending_captures").fetchone())
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger fail_pending_capture_row
            before insert on behavior_memories
            when new.memory_kind = 'failed_attempt'
            begin
                select raise(abort, 'synthetic pending capture failure');
            end
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic pending capture failure"):
        capture_service.capture(payload, _verification(payload))

    with database.connect(readonly=True) as connection:
        after = tuple(connection.execute("select * from pending_captures").fetchone())
    assert after == before
    assert _counts(database) == {
        "pending_captures": 1,
        "pending_capture_history": 0,
        "source_refs": 0,
        "behavior_memories": 0,
    }


def test_secret_bearing_unverified_capture_never_persists_fixture_value(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
) -> None:
    _, root = registered_project
    secret = _secret()
    payload = _payload(
        root,
        objective=f"protect {secret}",
        outcome=f"removed {secret}",
        decisions=[f"never store {secret}"],
        failed_attempts=[],
        verified_commands=[],
        changed_paths=[f"notes/{secret}.md"],
        preferences=[],
        risks=[],
        open_issues=[],
        reusable_lessons=[],
    )

    result = capture_service.capture(payload)

    assert result.status == "pending_verification"
    with database.connect(readonly=True) as connection:
        pending = connection.execute(
            "select structured_payload_json from pending_captures"
        ).fetchone()
        persisted = "\n".join(
            str(value)
            for table in ("pending_captures", "source_refs", "behavior_memories")
            for row in connection.execute(f"select * from {table}").fetchall()
            for value in row
            if value is not None
        )
    assert pending is not None
    assert str(root) not in pending["structured_payload_json"]
    assert '"cwd"' not in pending["structured_payload_json"]
    assert '"namespace"' not in pending["structured_payload_json"]
    if secret in persisted:
        pytest.fail("synthetic secret persisted", pytrace=False)


@pytest.mark.parametrize(
    ("source_reference_id", "parser_version", "source_path"),
    [
        ("00000000-0000-4000-8000-000000000001", "legacy-v1", None),
        (
            "00000000-0000-4000-8000-000000000001",
            "capture-v1",
            "legacy/source.json",
        ),
        ("malformed-uuid", "capture-v1", None),
    ],
)
def test_incompatible_source_provenance_rejects_and_preserves_pending(
    capture_service: CaptureService,
    registered_project: tuple[ProjectRepository, Path],
    database: Database,
    source_reference_id: str,
    parser_version: str,
    source_path: str | None,
) -> None:
    _, root = registered_project
    payload = _payload(root)
    capture_service.capture(payload)
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        pending = connection.execute(
            """
            select structured_hash from pending_captures
            where source_record_id = ?
            """,
            (payload.source_record_id,),
        ).fetchone()
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_reference_id,
                payload.namespace.source_agent.value,
                payload.source_record_id,
                source_path,
                pending["structured_hash"],
                now,
                parser_version,
                now,
            ),
        )
    with database.connect(readonly=True) as connection:
        before = tuple(connection.execute("select * from pending_captures").fetchone())

    result = capture_service.capture(payload, _verification(payload))

    assert result.status == "rejected"
    assert result.inserted_ids == ()
    assert result.duplicate is False
    with database.connect(readonly=True) as connection:
        after = tuple(connection.execute("select * from pending_captures").fetchone())
    assert after == before
    assert _counts(database) == {
        "pending_captures": 1,
        "pending_capture_history": 0,
        "source_refs": 1,
        "behavior_memories": 0,
    }


def test_pending_ttl_must_be_positive(
    database: Database,
    registered_project: tuple[ProjectRepository, Path],
) -> None:
    projects, _ = registered_project
    with pytest.raises(ValueError, match="pending_ttl_days"):
        CaptureService(
            database,
            projects,
            MemoryRepository(database),
            Redactor(),
            pending_ttl_days=0,
        )
