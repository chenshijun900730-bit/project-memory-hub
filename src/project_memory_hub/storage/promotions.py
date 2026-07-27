from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from uuid import UUID, uuid4

from project_memory_hub.domain import (
    BehaviorMemoryRecord,
    FactRecord,
    Namespace,
    ProjectFactInput,
    PromotionRecord,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.memories import MemoryRepository


class PromotionRepository:
    def __init__(
        self,
        database: Database,
        memories: MemoryRepository,
        facts: FactRepository,
        redactor: Redactor,
    ) -> None:
        self._database = database
        self._memories = memories
        self._facts = facts
        self._redactor = redactor

    def request_scoped(
        self,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
        proposed_rule: str,
    ) -> PromotionRecord:
        rule = self._safe_required(proposed_rule, "proposed_rule")
        self._exact_safe_persisted(
            namespace.source_agent.value, "source_agent", enum_type=SourceAgent
        )
        self._exact_safe_persisted(namespace.model_id, "model_id")
        with self._database.transaction() as connection:
            self._memories._get_scoped_on_connection(connection, project_id, namespace, memory_id)
            return self._request_on_connection(connection, memory_id, rule)

    @staticmethod
    def _request_on_connection(
        connection: sqlite3.Connection,
        memory_id: UUID,
        rule: str,
    ) -> PromotionRecord:
        existing = connection.execute(
            """
            select * from memory_promotions
            where memory_id = ? and proposed_rule = ? and status = 'pending'
            order by requested_at, promotion_id limit 1
            """,
            (str(memory_id).lower(), rule),
        ).fetchone()
        if existing is not None:
            return _promotion_record(existing)
        promotion_id = str(uuid4()).lower()
        connection.execute(
            """
            insert into memory_promotions(
                promotion_id, memory_id, proposed_rule, requester,
                approval_actor, requested_at, approved_at, status
            ) values (?, ?, ?, 'local_user', null, ?, null, 'pending')
            """,
            (promotion_id, str(memory_id).lower(), rule, _utc_now()),
        )
        row = connection.execute(
            "select * from memory_promotions where promotion_id = ?",
            (promotion_id,),
        ).fetchone()
        assert row is not None
        return _promotion_record(row)

    def approve_scoped(
        self,
        project_id: UUID,
        namespace: Namespace,
        promotion_id: UUID,
        approval_actor: str,
    ) -> FactRecord:
        actor = self._safe_required(approval_actor, "approval_actor")
        with self._database.transaction() as connection:
            promotion = connection.execute(
                """
                select promotion.*, memory.source_agent as memory_source_agent,
                       memory.model_id as memory_model_id
                from memory_promotions as promotion
                join behavior_memories as memory
                  on memory.memory_id = promotion.memory_id
                where promotion.promotion_id = ? and memory.project_id = ?
                """,
                (
                    str(promotion_id).lower(),
                    str(project_id).lower(),
                ),
            ).fetchone()
            if promotion is None:
                raise KeyError(promotion_id)
            stored_source_agent = self._exact_safe_persisted(
                promotion["memory_source_agent"],
                "source_agent",
                enum_type=SourceAgent,
            )
            stored_model_id = self._exact_safe_persisted(promotion["memory_model_id"], "model_id")
            if (
                stored_source_agent != namespace.source_agent.value
                or stored_model_id != namespace.model_id
            ):
                raise KeyError(promotion_id)
            memory = self._memories._get_scoped_on_connection(
                connection,
                project_id,
                namespace,
                UUID(promotion["memory_id"]),
            )
            return self._approve_on_connection(connection, promotion_id, promotion, memory, actor)

    def _approve_on_connection(
        self,
        connection: sqlite3.Connection,
        promotion_id: UUID,
        promotion: sqlite3.Row,
        memory: BehaviorMemoryRecord,
        actor: str,
    ) -> FactRecord:
        rule = self._exact_safe_persisted(promotion["proposed_rule"], "proposed_rule")
        source_agent = self._exact_safe_persisted(
            memory.namespace.source_agent.value,
            "source_agent",
            enum_type=SourceAgent,
        )
        model_id = self._exact_safe_persisted(memory.namespace.model_id, "model_id")
        if promotion["status"] == "approved":
            actor = self._exact_safe_persisted(promotion["approval_actor"], "approval_actor")
        elif promotion["status"] != "pending":
            raise ValueError("promotion is not pending")
        evidence_reference = json.dumps(
            {
                "approval_actor": actor,
                "model_id": model_id,
                "promotion_id": str(promotion_id).lower(),
                "source_agent": source_agent,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        observed_at = datetime.now(timezone.utc)
        if promotion["status"] == "pending":
            connection.execute(
                """
                update memory_promotions
                set status = 'approved', approval_actor = ?, approved_at = ?
                where promotion_id = ? and status = 'pending'
                """,
                (actor, _utc_iso(observed_at), str(promotion_id).lower()),
            )
        fact, _, _ = self._facts._observe_on_connection(
            connection,
            memory.project_id,
            ProjectFactInput(
                category="approved_shared_rule",
                normalized_content=rule,
                evidence_type="user_approval",
                evidence_reference=evidence_reference,
                observed_at=observed_at,
                confidence=1.0,
            ),
        )
        return fact

    def _safe_required(self, value: str, field_name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{field_name} must be text")
        normalized = " ".join(value.split())
        redacted = " ".join(self._redactor.redact(normalized).text.split())
        if not redacted:
            raise ValueError(f"{field_name} must not be blank")
        return redacted

    def _exact_safe_persisted(
        self,
        value: object,
        field_name: str,
        *,
        enum_type: type[SourceAgent] | None = None,
    ) -> str:
        if not isinstance(value, str):
            raise ValueError(f"{field_name} is unsafe")
        normalized = " ".join(value.split())
        if not normalized or normalized != value:
            raise ValueError(f"{field_name} is unsafe")
        redacted = " ".join(self._redactor.redact(value).text.split())
        if redacted != value:
            raise ValueError(f"{field_name} is unsafe")
        if enum_type is not None:
            try:
                enum_type(value)
            except ValueError:
                raise ValueError(f"{field_name} is unsafe") from None
        return value


def _promotion_record(row: sqlite3.Row) -> PromotionRecord:
    return PromotionRecord.model_validate(
        {
            "promotion_id": row["promotion_id"],
            "memory_id": row["memory_id"],
            "proposed_rule": row["proposed_rule"],
            "status": row["status"],
            "approval_actor": row["approval_actor"],
        }
    )


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return _utc_iso(datetime.now(timezone.utc))
