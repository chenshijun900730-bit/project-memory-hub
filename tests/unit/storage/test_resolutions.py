from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from project_memory_hub.domain import Namespace, SourceAgent
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.resolutions import (
    IssueResolutionRepository,
    ResolutionApplyResult,
)


VERIFIED_AT = datetime(2026, 7, 16, tzinfo=timezone.utc)
TARGET_NAMESPACE = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol")


@pytest.fixture
def connection(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.connect() as connection:
        yield connection


def _insert_project(connection: sqlite3.Connection, project_id: UUID) -> None:
    connection.execute(
        "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
        (
            str(project_id).lower(),
            f"/tmp/resolution-{project_id}",
            f"resolution-{project_id}",
        ),
    )


def _insert_source(
    connection: sqlite3.Connection,
    source_reference_id: UUID,
    *,
    project_id: UUID,
    namespace: Namespace,
    source_timestamp: datetime | str = VERIFIED_AT,
) -> None:
    source_record_id = f"record-{source_reference_id}"
    timestamp = (
        source_timestamp.astimezone(timezone.utc).isoformat()
        if isinstance(source_timestamp, datetime)
        else source_timestamp
    )
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
            namespace.source_agent.value,
            source_record_id,
            hashlib.sha256(source_record_id.encode()).hexdigest(),
            timestamp,
            timestamp,
            str(project_id).lower(),
            namespace.model_id,
        ),
    )


def _insert_open_issue(
    connection: sqlite3.Connection,
    memory_id: UUID,
    *,
    project_id: UUID,
    namespace: Namespace,
    source_reference_id: UUID,
    normalized_content: str,
    content_hash: str | None = None,
    lifecycle_state: str = "active",
    created_at: datetime = VERIFIED_AT,
) -> None:
    connection.execute(
        """
        insert into behavior_memories(
            memory_id, project_id, source_agent, model_id, task_fingerprint,
            memory_kind, normalized_content, content_hash, source_reference_id,
            created_at, confidence, lifecycle_state
        ) values (?, ?, ?, ?, ?, 'open_issue', ?, ?, ?, ?, 1.0, ?)
        """,
        (
            str(memory_id).lower(),
            str(project_id).lower(),
            namespace.source_agent.value,
            namespace.model_id,
            hashlib.sha256(f"task-{memory_id}".encode()).hexdigest(),
            normalized_content,
            content_hash or hashlib.sha256(normalized_content.encode()).hexdigest(),
            str(source_reference_id).lower(),
            created_at.astimezone(timezone.utc).isoformat(),
            lifecycle_state,
        ),
    )


def _insert_resolved_audit(
    connection: sqlite3.Connection,
    *,
    project_id: UUID,
    namespace: Namespace,
    target_memory_id: UUID,
    source_reference_id: UUID,
    target_content_hash: str,
) -> None:
    connection.execute(
        """
        insert into memory_issue_resolutions(
            resolution_id, project_id, source_agent, model_id,
            target_content_hash, target_memory_id, source_reference_id,
            status, resolved_at
        ) values (?, ?, ?, ?, ?, ?, ?, 'resolved', ?)
        """,
        (
            str(uuid4()).lower(),
            str(project_id).lower(),
            namespace.source_agent.value,
            namespace.model_id,
            target_content_hash,
            str(target_memory_id).lower(),
            str(source_reference_id).lower(),
            VERIFIED_AT.isoformat(),
        ),
    )


def _states(connection: sqlite3.Connection, memory_ids: tuple[UUID, ...]) -> set[str]:
    question_marks = ",".join("?" for _memory_id in memory_ids)
    rows = connection.execute(
        f"select lifecycle_state from behavior_memories where memory_id in ({question_marks})",
        tuple(str(memory_id).lower() for memory_id in memory_ids),
    ).fetchall()
    return {str(row["lifecycle_state"]) for row in rows}


def test_apply_archives_all_257_exact_targets_without_crossing_scope(
    connection: sqlite3.Connection,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    other_project_id = uuid4()
    other_agent_namespace = Namespace(
        source_agent=SourceAgent.CHATGPT,
        model_id=TARGET_NAMESPACE.model_id,
    )
    other_model_namespace = Namespace(
        source_agent=TARGET_NAMESPACE.source_agent,
        model_id="gpt-5.7",
    )
    _insert_project(connection, target_project_id)
    _insert_project(connection, other_project_id)

    current_source_id = uuid4()
    target_source_id = uuid4()
    other_project_source_id = uuid4()
    other_agent_source_id = uuid4()
    other_model_source_id = uuid4()
    _insert_source(
        connection,
        current_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
    )
    _insert_source(
        connection,
        target_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT - timedelta(days=1),
    )
    _insert_source(
        connection,
        other_project_source_id,
        project_id=other_project_id,
        namespace=TARGET_NAMESPACE,
    )
    _insert_source(
        connection,
        other_agent_source_id,
        project_id=target_project_id,
        namespace=other_agent_namespace,
    )
    _insert_source(
        connection,
        other_model_source_id,
        project_id=target_project_id,
        namespace=other_model_namespace,
    )

    target_ids = tuple(uuid4() for _index in range(257))
    for memory_id in target_ids:
        _insert_open_issue(
            connection,
            memory_id,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
            source_reference_id=target_source_id,
            normalized_content="exact old issue",
        )

    foreign_rows = (
        (other_project_id, TARGET_NAMESPACE, other_project_source_id),
        (target_project_id, other_agent_namespace, other_agent_source_id),
        (target_project_id, other_model_namespace, other_model_source_id),
    )
    foreign_ids: list[UUID] = []
    for project_id, namespace, source_reference_id in foreign_rows:
        memory_id = uuid4()
        foreign_ids.append(memory_id)
        _insert_open_issue(
            connection,
            memory_id,
            project_id=project_id,
            namespace=namespace,
            source_reference_id=source_reference_id,
            normalized_content="exact old issue",
        )

    result = repository.apply_on_connection(
        connection,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=current_source_id,
        declarations=("exact old issue",),
        verified_at=VERIFIED_AT,
        resolved_at=VERIFIED_AT,
    )

    assert result == ResolutionApplyResult(resolved_count=257)
    assert _states(connection, target_ids) == {"archived"}
    assert _states(connection, tuple(foreign_ids)) == {"active"}
    audit_rows = connection.execute(
        """
        select target_memory_id, status from memory_issue_resolutions
        where project_id = ? and source_agent = ? and model_id = ?
        """,
        (
            str(target_project_id).lower(),
            TARGET_NAMESPACE.source_agent.value,
            TARGET_NAMESPACE.model_id,
        ),
    ).fetchall()
    assert {UUID(row["target_memory_id"]) for row in audit_rows} == set(target_ids)
    assert {row["status"] for row in audit_rows} == {"resolved"}


def test_apply_excludes_same_source_and_future_sources_but_includes_time_boundary(
    connection: sqlite3.Connection,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    _insert_project(connection, target_project_id)
    current_source_id = uuid4()
    future_source_id = uuid4()
    boundary_source_id = uuid4()
    _insert_source(
        connection,
        current_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
    )
    _insert_source(
        connection,
        future_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT + timedelta(microseconds=1),
    )
    _insert_source(
        connection,
        boundary_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT,
    )
    same_source_target_id = uuid4()
    future_target_id = uuid4()
    boundary_target_id = uuid4()
    _insert_open_issue(
        connection,
        same_source_target_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=current_source_id,
        normalized_content="same text",
    )
    _insert_open_issue(
        connection,
        future_target_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=future_source_id,
        normalized_content="future issue",
    )
    _insert_open_issue(
        connection,
        boundary_target_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=boundary_source_id,
        normalized_content="boundary issue",
    )

    def apply_declaration(text: str) -> ResolutionApplyResult:
        return repository.apply_on_connection(
            connection,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
            source_reference_id=current_source_id,
            declarations=(text,),
            verified_at=VERIFIED_AT,
            resolved_at=VERIFIED_AT,
        )

    assert apply_declaration("same text").unmatched_resolution_count == 1
    assert _states(connection, (same_source_target_id,)) == {"active"}
    assert apply_declaration("future issue").unmatched_resolution_count == 1
    assert _states(connection, (future_target_id,)) == {"active"}
    assert apply_declaration("boundary issue") == ResolutionApplyResult(resolved_count=1)
    assert _states(connection, (boundary_target_id,)) == {"archived"}


@pytest.mark.parametrize(
    ("current_source_timestamp", "verified_at"),
    (
        (VERIFIED_AT, VERIFIED_AT + timedelta(seconds=1)),
        ("not-a-strict-utc-timestamp", VERIFIED_AT),
    ),
    ids=("inflated-verified-at", "malformed-source-timestamp"),
)
def test_apply_rejects_unverified_current_source_time_before_updates(
    connection: sqlite3.Connection,
    current_source_timestamp: datetime | str,
    verified_at: datetime,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    _insert_project(connection, target_project_id)
    current_source_id = uuid4()
    target_source_id = uuid4()
    _insert_source(
        connection,
        current_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=current_source_timestamp,
    )
    _insert_source(
        connection,
        target_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT - timedelta(days=1),
    )
    target_memory_id = uuid4()
    _insert_open_issue(
        connection,
        target_memory_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=target_source_id,
        normalized_content="exact old issue",
    )

    with pytest.raises(ValueError, match="source_reference_id provenance mismatch"):
        repository.apply_on_connection(
            connection,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
            source_reference_id=current_source_id,
            declarations=("exact old issue",),
            verified_at=verified_at,
            resolved_at=verified_at,
        )

    assert _states(connection, (target_memory_id,)) == {"active"}
    assert connection.execute("select count(*) from memory_issue_resolutions").fetchone()[0] == 0


def test_apply_accepts_equivalent_current_source_utc_offset(
    connection: sqlite3.Connection,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    _insert_project(connection, target_project_id)
    current_source_id = uuid4()
    target_source_id = uuid4()
    _insert_source(
        connection,
        current_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp="2026-07-16T08:00:00+08:00",
    )
    _insert_source(
        connection,
        target_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT - timedelta(days=1),
    )
    target_memory_id = uuid4()
    _insert_open_issue(
        connection,
        target_memory_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=target_source_id,
        normalized_content="exact old issue",
    )

    result = repository.apply_on_connection(
        connection,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=current_source_id,
        declarations=("exact old issue",),
        verified_at=VERIFIED_AT,
        resolved_at=VERIFIED_AT,
    )

    assert result == ResolutionApplyResult(resolved_count=1)
    assert _states(connection, (target_memory_id,)) == {"archived"}


def test_apply_requires_an_active_outer_transaction_before_writes(
    connection: sqlite3.Connection,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    _insert_project(connection, target_project_id)
    current_source_id = uuid4()
    target_source_id = uuid4()
    _insert_source(
        connection,
        current_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
    )
    _insert_source(
        connection,
        target_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT - timedelta(days=1),
    )
    target_memory_id = uuid4()
    _insert_open_issue(
        connection,
        target_memory_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=target_source_id,
        normalized_content="exact old issue",
    )
    connection.commit()
    assert connection.in_transaction is False

    with pytest.raises(ValueError, match="active transaction required"):
        repository.apply_on_connection(
            connection,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
            source_reference_id=current_source_id,
            declarations=("exact old issue",),
            verified_at=VERIFIED_AT,
            resolved_at=VERIFIED_AT,
        )

    assert _states(connection, (target_memory_id,)) == {"active"}
    assert connection.execute("select count(*) from memory_issue_resolutions").fetchone()[0] == 0


def test_apply_distinguishes_full_text_replay_collision_and_idempotent_not_found(
    connection: sqlite3.Connection,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    _insert_project(connection, target_project_id)
    target_source_id = uuid4()
    first_declaration_source_id = uuid4()
    new_source_id = uuid4()
    _insert_source(
        connection,
        target_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT - timedelta(days=1),
    )
    for source_reference_id in (first_declaration_source_id, new_source_id):
        _insert_source(
            connection,
            source_reference_id,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
        )

    exact_target_id = uuid4()
    _insert_open_issue(
        connection,
        exact_target_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=target_source_id,
        normalized_content="exact old issue",
    )

    def apply_declaration(
        source_reference_id: UUID,
        text: str,
    ) -> ResolutionApplyResult:
        return repository.apply_on_connection(
            connection,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
            source_reference_id=source_reference_id,
            declarations=(text,),
            verified_at=VERIFIED_AT,
            resolved_at=VERIFIED_AT,
        )

    assert apply_declaration(
        first_declaration_source_id, "exact old issue"
    ) == ResolutionApplyResult(resolved_count=1)
    replay = apply_declaration(new_source_id, "exact old issue")
    assert replay.already_resolved_count == 1
    assert replay.unmatched_resolution_count == 0

    collision_text = "hash collision text"
    collision_hash = hashlib.sha256(collision_text.encode()).hexdigest()
    collision_target_id = uuid4()
    _insert_open_issue(
        connection,
        collision_target_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=target_source_id,
        normalized_content="different full text",
        content_hash=collision_hash,
        lifecycle_state="archived",
    )
    _insert_resolved_audit(
        connection,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        target_memory_id=collision_target_id,
        source_reference_id=first_declaration_source_id,
        target_content_hash=collision_hash,
    )

    collision = apply_declaration(new_source_id, collision_text)
    assert collision.already_resolved_count == 0
    assert collision.unmatched_resolution_count == 1

    first_unmatched = apply_declaration(new_source_id, "unknown issue")
    repeated_unmatched = apply_declaration(new_source_id, "unknown issue")
    assert first_unmatched.unmatched_resolution_count == 1
    assert repeated_unmatched.unmatched_resolution_count == 0
    unknown_hash = hashlib.sha256(b"unknown issue").hexdigest()
    not_found_count = connection.execute(
        """
        select count(*) from memory_issue_resolutions
        where project_id = ? and source_agent = ? and model_id = ?
          and source_reference_id = ? and target_content_hash = ?
          and status = 'not_found'
        """,
        (
            str(target_project_id).lower(),
            TARGET_NAMESPACE.source_agent.value,
            TARGET_NAMESPACE.model_id,
            str(new_source_id).lower(),
            unknown_hash,
        ),
    ).fetchone()[0]
    assert not_found_count == 1


def test_apply_rejects_missing_ambiguous_and_foreign_source_provenance_before_updates(
    connection: sqlite3.Connection,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    other_project_id = uuid4()
    _insert_project(connection, target_project_id)
    _insert_project(connection, other_project_id)
    target_source_id = uuid4()
    _insert_source(
        connection,
        target_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_timestamp=VERIFIED_AT - timedelta(days=1),
    )
    target_memory_id = uuid4()
    _insert_open_issue(
        connection,
        target_memory_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        source_reference_id=target_source_id,
        normalized_content="exact old issue",
    )

    wrong_agent_source_id = uuid4()
    wrong_project_source_id = uuid4()
    wrong_model_source_id = uuid4()
    legacy_source_id = uuid4()
    _insert_source(
        connection,
        wrong_agent_source_id,
        project_id=target_project_id,
        namespace=Namespace(
            source_agent=SourceAgent.CHATGPT,
            model_id=TARGET_NAMESPACE.model_id,
        ),
    )
    _insert_source(
        connection,
        wrong_project_source_id,
        project_id=other_project_id,
        namespace=TARGET_NAMESPACE,
    )
    _insert_source(
        connection,
        wrong_model_source_id,
        project_id=target_project_id,
        namespace=Namespace(
            source_agent=TARGET_NAMESPACE.source_agent,
            model_id="gpt-5.7",
        ),
    )
    _insert_source(
        connection,
        legacy_source_id,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
    )
    connection.execute(
        """
        update source_refs
        set capture_project_id = null, capture_model_id = null
        where source_reference_id = ?
        """,
        (str(legacy_source_id).lower(),),
    )

    invalid_source_ids = (
        uuid4(),
        wrong_agent_source_id,
        wrong_project_source_id,
        wrong_model_source_id,
        legacy_source_id,
    )
    for invalid_source_id in invalid_source_ids:
        with pytest.raises(ValueError, match="source_reference_id provenance mismatch"):
            repository.apply_on_connection(
                connection,
                project_id=target_project_id,
                namespace=TARGET_NAMESPACE,
                source_reference_id=invalid_source_id,
                declarations=("exact old issue",),
                verified_at=VERIFIED_AT,
                resolved_at=VERIFIED_AT,
            )

    assert _states(connection, (target_memory_id,)) == {"active"}
    assert connection.execute("select count(*) from memory_issue_resolutions").fetchone()[0] == 0


def test_resolved_target_ids_scoped_is_exact_and_bounded(
    connection: sqlite3.Connection,
) -> None:
    repository = IssueResolutionRepository()
    target_project_id = uuid4()
    other_project_id = uuid4()
    _insert_project(connection, target_project_id)
    _insert_project(connection, other_project_id)
    other_agent_namespace = Namespace(
        source_agent=SourceAgent.CHATGPT,
        model_id=TARGET_NAMESPACE.model_id,
    )
    other_model_namespace = Namespace(
        source_agent=TARGET_NAMESPACE.source_agent,
        model_id="gpt-5.7",
    )

    scoped_rows = (
        (target_project_id, TARGET_NAMESPACE),
        (other_project_id, TARGET_NAMESPACE),
        (target_project_id, other_agent_namespace),
        (target_project_id, other_model_namespace),
    )
    resolved_ids: list[UUID] = []
    for project_id, namespace in scoped_rows:
        source_reference_id = uuid4()
        target_memory_id = uuid4()
        content = f"resolved-{target_memory_id}"
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        _insert_source(
            connection,
            source_reference_id,
            project_id=project_id,
            namespace=namespace,
        )
        _insert_open_issue(
            connection,
            target_memory_id,
            project_id=project_id,
            namespace=namespace,
            source_reference_id=source_reference_id,
            normalized_content=content,
            lifecycle_state="archived",
        )
        _insert_resolved_audit(
            connection,
            project_id=project_id,
            namespace=namespace,
            target_memory_id=target_memory_id,
            source_reference_id=source_reference_id,
            target_content_hash=content_hash,
        )
        resolved_ids.append(target_memory_id)

    assert (
        repository.resolved_target_ids_scoped(
            connection,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
            memory_ids=(),
        )
        == frozenset()
    )
    candidate_ids = (resolved_ids[0], *resolved_ids[1:], *(uuid4() for _index in range(96)))
    assert len(candidate_ids) == 100
    assert repository.resolved_target_ids_scoped(
        connection,
        project_id=target_project_id,
        namespace=TARGET_NAMESPACE,
        memory_ids=candidate_ids,
    ) == frozenset({resolved_ids[0]})

    with pytest.raises(ValueError, match="at most 100"):
        repository.resolved_target_ids_scoped(
            connection,
            project_id=target_project_id,
            namespace=TARGET_NAMESPACE,
            memory_ids=tuple(uuid4() for _index in range(101)),
        )
