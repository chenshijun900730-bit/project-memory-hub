from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from project_memory_hub.domain import (
    BehaviorMemoryInput,
    MemoryKind,
    Namespace,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository
from project_memory_hub.storage.promotions import PromotionRepository


_NAMESPACE = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol")


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    return database


@pytest.fixture
def repositories(
    database: Database, tmp_path: Path
) -> tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID]:
    root = tmp_path / "synthetic-project"
    root.mkdir()
    project = ProjectRepository(database).register(
        ProjectCandidate(canonical_path=root, display_name="Synthetic")
    )
    memories = MemoryRepository(database)
    facts = FactRepository(database)
    source_reference_id = uuid4()
    now = datetime.now(timezone.utc)
    with database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(source_reference_id),
                SourceAgent.CODEX.value,
                "selected-record",
                None,
                hashlib.sha256(b"selected-record").hexdigest(),
                now.isoformat(),
                "test-v1",
                now.isoformat(),
            ),
        )
    content = "always scope behavior recall"
    inserted = memories.insert(
        BehaviorMemoryInput(
            project_id=project.project_id,
            namespace=_NAMESPACE,
            task_fingerprint=hashlib.sha256(b"task").hexdigest(),
            memory_kind=MemoryKind.REUSABLE_LESSON,
            normalized_content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            source_reference_id=source_reference_id,
            created_at=now,
            confidence=1.0,
        )
    )
    assert inserted.record_id is not None
    promotions = PromotionRepository(database, memories, facts, Redactor())
    return promotions, facts, memories, project.project_id, inserted.record_id


def test_request_creates_no_fact_and_approval_is_explicit_idempotent(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
    database: Database,
) -> None:
    promotions, facts, _, project_id, memory_id = repositories

    requested = promotions.request_scoped(
        project_id, _NAMESPACE, memory_id, "  share namespace scoping  "
    )
    duplicate_request = promotions.request_scoped(
        project_id, _NAMESPACE, memory_id, "share namespace scoping"
    )

    assert requested.status == "pending"
    assert requested.approval_actor is None
    assert duplicate_request.promotion_id == requested.promotion_id
    assert facts.search(project_id, "", 20) == []

    approved = promotions.approve_scoped(
        project_id, _NAMESPACE, requested.promotion_id, "  local-owner  "
    )
    reapproved = promotions.approve_scoped(
        project_id, _NAMESPACE, requested.promotion_id, "local-owner"
    )

    assert approved.fact_id == reapproved.fact_id
    assert approved.category == "approved_shared_rule"
    assert approved.normalized_content == "share namespace scoping"
    assert approved.evidence_type == "user_approval"
    assert approved.confidence == 1.0
    evidence = json.loads(approved.evidence_reference)
    assert evidence == {
        "approval_actor": "local-owner",
        "model_id": "gpt-5.6-sol",
        "promotion_id": str(requested.promotion_id),
        "source_agent": "codex",
    }
    with database.connect(readonly=True) as connection:
        promotion = connection.execute(
            "select status, approval_actor, approved_at from memory_promotions"
        ).fetchone()
        fact_count = connection.execute("select count(*) from project_facts").fetchone()[0]
    assert promotion["status"] == "approved"
    assert promotion["approval_actor"] == "local-owner"
    assert promotion["approved_at"] is not None
    assert fact_count == 1


def test_approval_rolls_back_state_when_fact_insert_fails(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
    database: Database,
) -> None:
    promotions, _, _, project_id, memory_id = repositories
    requested = promotions.request_scoped(
        project_id, _NAMESPACE, memory_id, "share namespace scoping"
    )
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger fail_promotion_fact
            before insert on project_facts
            when new.category = 'approved_shared_rule'
            begin
                select raise(abort, 'synthetic promotion failure');
            end
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="synthetic promotion failure"):
        promotions.approve_scoped(project_id, _NAMESPACE, requested.promotion_id, "local-owner")

    with database.connect(readonly=True) as connection:
        promotion = connection.execute(
            "select status, approval_actor, approved_at from memory_promotions"
        ).fetchone()
        fact_count = connection.execute("select count(*) from project_facts").fetchone()[0]
    assert tuple(promotion) == ("pending", None, None)
    assert fact_count == 0


def test_promotion_boundaries_reject_blank_and_unknown_values(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
) -> None:
    promotions, _, _, project_id, memory_id = repositories
    with pytest.raises(ValueError, match="proposed_rule"):
        promotions.request_scoped(project_id, _NAMESPACE, memory_id, "   ")
    with pytest.raises(KeyError):
        promotions.request_scoped(project_id, _NAMESPACE, uuid4(), "safe rule")

    requested = promotions.request_scoped(project_id, _NAMESPACE, memory_id, "safe rule")
    with pytest.raises(ValueError, match="approval_actor"):
        promotions.approve_scoped(project_id, _NAMESPACE, requested.promotion_id, "   ")
    with pytest.raises(KeyError):
        promotions.approve_scoped(project_id, _NAMESPACE, uuid4(), "local-owner")


def test_promotion_redacts_rule_and_actor_before_persistence(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
    database: Database,
) -> None:
    promotions, _, _, project_id, memory_id = repositories
    secret = "sk-proj-" + ("R" * 24)

    requested = promotions.request_scoped(
        project_id, _NAMESPACE, memory_id, f"share after removing {secret}"
    )
    approved = promotions.approve_scoped(
        project_id, _NAMESPACE, requested.promotion_id, f"owner {secret}"
    )

    with database.connect(readonly=True) as connection:
        persisted = "\n".join(
            str(value)
            for table in ("memory_promotions", "project_facts")
            for row in connection.execute(f"select * from {table}").fetchall()
            for value in row
            if value is not None
        )
    if secret in persisted:
        pytest.fail("synthetic secret persisted", pytrace=False)
    if secret in approved.normalized_content:
        pytest.fail("synthetic secret remained in the approved rule", pytrace=False)
    if secret in approved.evidence_reference:
        pytest.fail("synthetic secret remained in approval evidence", pytrace=False)


@pytest.mark.parametrize(
    "table,column,unsafe_namespace,error_field",
    [
        ("memory_promotions", "proposed_rule", _NAMESPACE, "proposed_rule"),
        (
            "behavior_memories",
            "model_id",
            Namespace(
                source_agent=SourceAgent.CODEX,
                model_id="Bearer TOPSECRETTOKEN123456789",
            ),
            "model_id",
        ),
        ("behavior_memories", "source_agent", _NAMESPACE, "source_agent"),
    ],
)
def test_approval_rejects_corrupted_persisted_metadata_atomically(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
    database: Database,
    table: str,
    column: str,
    unsafe_namespace: Namespace,
    error_field: str,
) -> None:
    promotions, _, _, project_id, memory_id = repositories
    requested = promotions.request_scoped(project_id, _NAMESPACE, memory_id, "safe shared rule")
    bearer = "Bearer TOPSECRETTOKEN123456789"
    row_id_column = "promotion_id" if table == "memory_promotions" else "memory_id"
    row_id = requested.promotion_id if table == "memory_promotions" else memory_id
    with database.transaction() as connection:
        connection.execute(
            f"update {table} set {column} = ? where {row_id_column} = ?",
            (bearer, str(row_id)),
        )

    with pytest.raises(ValueError, match=error_field):
        promotions.approve_scoped(
            project_id,
            unsafe_namespace,
            requested.promotion_id,
            "local-owner",
        )

    with database.connect(readonly=True) as connection:
        promotion = connection.execute(
            "select status, approval_actor, approved_at from memory_promotions "
            "where promotion_id = ?",
            (str(requested.promotion_id),),
        ).fetchone()
        fact_count = connection.execute(
            "select count(*) from project_facts where project_id = ?",
            (str(project_id),),
        ).fetchone()[0]
    assert tuple(promotion) == ("pending", None, None)
    assert fact_count == 0


def test_reapproval_rejects_corrupted_persisted_actor_without_rewriting_facts(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
    database: Database,
) -> None:
    promotions, _, _, project_id, memory_id = repositories
    requested = promotions.request_scoped(project_id, _NAMESPACE, memory_id, "safe shared rule")
    original = promotions.approve_scoped(
        project_id, _NAMESPACE, requested.promotion_id, "local-owner"
    )
    bearer = "Bearer TOPSECRETTOKEN123456789"
    with database.transaction() as connection:
        connection.execute(
            "update memory_promotions set approval_actor = ? where promotion_id = ?",
            (bearer, str(requested.promotion_id)),
        )

    with pytest.raises(ValueError, match="approval_actor"):
        promotions.approve_scoped(project_id, _NAMESPACE, requested.promotion_id, "local-owner")

    with database.connect(readonly=True) as connection:
        promotion = connection.execute(
            "select status, approval_actor from memory_promotions where promotion_id = ?",
            (str(requested.promotion_id),),
        ).fetchone()
        facts = connection.execute(
            "select fact_id, normalized_content, evidence_reference "
            "from project_facts where project_id = ?",
            (str(project_id),),
        ).fetchall()
    assert tuple(promotion) == ("approved", bearer)
    assert len(facts) == 1
    assert facts[0]["fact_id"] == str(original.fact_id)
    assert bearer not in " ".join(str(value) for value in facts[0])


@pytest.mark.parametrize(
    "project_id,namespace",
    [
        (uuid4(), _NAMESPACE),
        (
            None,
            Namespace(source_agent=SourceAgent.CHATGPT, model_id=_NAMESPACE.model_id),
        ),
        (
            None,
            Namespace(source_agent=SourceAgent.CODEX, model_id="different-model"),
        ),
    ],
)
def test_public_promotion_operations_require_the_complete_namespace(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
    project_id: UUID | None,
    namespace: Namespace,
) -> None:
    promotions, _, _, actual_project_id, memory_id = repositories
    selected_project_id = project_id or actual_project_id

    with pytest.raises(KeyError):
        promotions.request_scoped(
            selected_project_id,
            namespace,
            memory_id,
            "safe shared rule",
        )

    requested = promotions.request_scoped(
        actual_project_id,
        _NAMESPACE,
        memory_id,
        "safe shared rule",
    )
    with pytest.raises(KeyError):
        promotions.approve_scoped(
            selected_project_id,
            namespace,
            requested.promotion_id,
            "local-owner",
        )


def test_unscoped_promotion_entrypoints_do_not_exist(
    repositories: tuple[PromotionRepository, FactRepository, MemoryRepository, UUID, UUID],
) -> None:
    promotions, _, _, _, _ = repositories
    assert not hasattr(promotions, "request")
    assert not hasattr(promotions, "approve")
