from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID, uuid4

from project_memory_hub.domain import Namespace


_TARGET_BATCH_SIZE = 256
_DISPLAY_ID_LIMIT = 100


@dataclass(frozen=True, slots=True)
class ResolutionApplyResult:
    resolved_count: int = 0
    already_resolved_count: int = 0
    unmatched_resolution_count: int = 0


class IssueResolutionRepository:
    def apply_on_connection(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: UUID,
        namespace: Namespace,
        source_reference_id: UUID,
        declarations: tuple[str, ...],
        verified_at: datetime,
        resolved_at: datetime,
    ) -> ResolutionApplyResult:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        project_id_text = str(project_id).lower()
        source_reference_id_text = str(source_reference_id).lower()
        source_agent = namespace.source_agent.value
        model_id = namespace.model_id
        verified_at_text = _utc_iso(verified_at)
        resolved_at_text = _utc_iso(resolved_at)
        self._validate_source_provenance(
            connection,
            project_id=project_id_text,
            source_agent=source_agent,
            model_id=model_id,
            source_reference_id=source_reference_id_text,
            verified_at=verified_at_text,
        )

        resolved_count = 0
        already_resolved_count = 0
        unmatched_resolution_count = 0
        for declaration in declarations:
            content_hash = hashlib.sha256(declaration.encode()).hexdigest().lower()
            declaration_resolved_count = 0
            while True:
                rows = connection.execute(
                    """
                    select bm.memory_id
                    from behavior_memories as bm
                    join source_refs as source
                      on source.source_reference_id = bm.source_reference_id
                    where bm.project_id = ?
                      and bm.source_agent = ?
                      and bm.model_id = ?
                      and bm.memory_kind = 'open_issue'
                      and bm.lifecycle_state = 'active'
                      and bm.content_hash = ?
                      and bm.normalized_content = ?
                      and bm.source_reference_id <> ?
                      and strict_utc_epoch_us(source.source_timestamp) is not null
                      and strict_utc_epoch_us(source.source_timestamp)
                          <= strict_utc_epoch_us(?)
                    order by bm.created_at, bm.memory_id
                    limit ?
                    """,
                    (
                        project_id_text,
                        source_agent,
                        model_id,
                        content_hash,
                        declaration,
                        source_reference_id_text,
                        verified_at_text,
                        _TARGET_BATCH_SIZE,
                    ),
                ).fetchall()
                if not rows:
                    break

                for row in rows:
                    target_memory_id = UUID(str(row["memory_id"]))
                    cursor = connection.execute(
                        """
                        update behavior_memories
                        set lifecycle_state = 'archived'
                        where memory_id = ?
                          and project_id = ?
                          and source_agent = ?
                          and model_id = ?
                          and memory_kind = 'open_issue'
                          and lifecycle_state = 'active'
                        """,
                        (
                            str(target_memory_id).lower(),
                            project_id_text,
                            source_agent,
                            model_id,
                        ),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError("resolution target changed during apply")
                    connection.execute(
                        """
                        insert into memory_issue_resolutions(
                            resolution_id, project_id, source_agent, model_id,
                            target_content_hash, target_memory_id,
                            source_reference_id, status, resolved_at
                        ) values (?, ?, ?, ?, ?, ?, ?, 'resolved', ?)
                        """,
                        (
                            str(uuid4()).lower(),
                            project_id_text,
                            source_agent,
                            model_id,
                            content_hash,
                            str(target_memory_id).lower(),
                            source_reference_id_text,
                            resolved_at_text,
                        ),
                    )
                    declaration_resolved_count += 1

            if declaration_resolved_count:
                resolved_count += declaration_resolved_count
                continue

            already_resolved = connection.execute(
                """
                select 1
                from memory_issue_resolutions as resolution
                join behavior_memories as target
                  on target.memory_id = resolution.target_memory_id
                where resolution.project_id = ?
                  and resolution.source_agent = ?
                  and resolution.model_id = ?
                  and resolution.status = 'resolved'
                  and resolution.target_content_hash = ?
                  and target.normalized_content = ?
                limit 1
                """,
                (
                    project_id_text,
                    source_agent,
                    model_id,
                    content_hash,
                    declaration,
                ),
            ).fetchone()
            if already_resolved is not None:
                already_resolved_count += 1
                continue

            cursor = connection.execute(
                """
                insert or ignore into memory_issue_resolutions(
                    resolution_id, project_id, source_agent, model_id,
                    target_content_hash, target_memory_id,
                    source_reference_id, status, resolved_at
                ) values (?, ?, ?, ?, ?, null, ?, 'not_found', ?)
                """,
                (
                    str(uuid4()).lower(),
                    project_id_text,
                    source_agent,
                    model_id,
                    content_hash,
                    source_reference_id_text,
                    resolved_at_text,
                ),
            )
            if cursor.rowcount == 1:
                unmatched_resolution_count += 1

        return ResolutionApplyResult(
            resolved_count=resolved_count,
            already_resolved_count=already_resolved_count,
            unmatched_resolution_count=unmatched_resolution_count,
        )

    def resolved_target_ids_scoped(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: UUID,
        namespace: Namespace,
        memory_ids: Sequence[UUID],
    ) -> frozenset[UUID]:
        if not memory_ids:
            return frozenset()
        if len(memory_ids) > _DISPLAY_ID_LIMIT:
            raise ValueError("memory_ids must contain at most 100 ids")
        question_marks = ",".join("?" for _memory_id in memory_ids)
        rows = connection.execute(
            f"""select target_memory_id
                from memory_issue_resolutions
                where project_id = ? and source_agent = ? and model_id = ?
                  and status = 'resolved'
                  and target_memory_id in ({question_marks})""",
            (
                str(project_id).lower(),
                namespace.source_agent.value,
                namespace.model_id,
                *(str(memory_id).lower() for memory_id in memory_ids),
            ),
        ).fetchall()
        return frozenset(UUID(str(row["target_memory_id"])) for row in rows)

    @staticmethod
    def _validate_source_provenance(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        source_agent: str,
        model_id: str,
        source_reference_id: str,
        verified_at: str,
    ) -> None:
        row = connection.execute(
            """
            select source_agent, capture_project_id, capture_model_id,
                   strict_utc_epoch_us(source_timestamp) as source_timestamp_epoch,
                   strict_utc_epoch_us(?) as verified_at_epoch
            from source_refs
            where source_reference_id = ?
            """,
            (verified_at, source_reference_id),
        ).fetchone()
        if (
            row is None
            or row["source_agent"] != source_agent
            or row["capture_project_id"] != project_id
            or row["capture_model_id"] != model_id
            or row["source_timestamp_epoch"] is None
            or row["source_timestamp_epoch"] != row["verified_at_epoch"]
        ):
            raise ValueError("source_reference_id provenance mismatch")


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
