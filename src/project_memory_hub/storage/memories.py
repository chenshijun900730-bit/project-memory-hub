from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from uuid import UUID, uuid4

from project_memory_hub.domain import (
    BehaviorMemoryInput,
    BehaviorMemoryRecord,
    InsertResult,
    LifecycleState,
    MemoryKind,
    Namespace,
)
from project_memory_hub.storage.database import Database, ReadonlyDatabaseSnapshot


_SHA256 = re.compile(r"[0-9a-f]{64}")
_TOKEN = re.compile(
    r"[A-Za-z0-9_]+|[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f"
    r"\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\ua960-\ua97f"
    r"\uac00-\ud7af\ud7b0-\ud7ff\uf900-\ufaff\uff65-\uff9f"
    r"\U0001aff0-\U0001afff\U0001b000-\U0001b0ff"
    r"\U0001b100-\U0001b12f\U0001b130-\U0001b16f"
    r"\U00020000-\U0002a6df\U0002a700-\U0002b73f"
    r"\U0002b740-\U0002b81f\U0002b820-\U0002ceaf"
    r"\U0002ceb0-\U0002ebef\U0002ebf0-\U0002ee5f"
    r"\U0002f800-\U0002fa1f\U00030000-\U0003134f"
    r"\U00031350-\U000323af]+"
)
# Bounds each namespace-scoped database load. Relevant rows older than this recency
# window are intentionally left for compaction/promotion rather than an unbounded scan.
_CANDIDATE_WINDOW = 1000
_COMPACTION_UPDATE_BATCH = 300
_COMPACTION_MAX_SOURCE_CHARS = 1_000_000
_COMPACTION_MAX_SOURCE_BYTES = 4_000_000
_COMPACTION_MANDATORY_KINDS = (
    MemoryKind.FAILED_ATTEMPT,
    MemoryKind.OPEN_ISSUE,
    MemoryKind.RISK,
    MemoryKind.VERIFIED_METHOD,
)
_COMPACTION_OPTIONAL_KINDS = (
    MemoryKind.DECISION,
    MemoryKind.OUTCOME,
    MemoryKind.PREFERENCE,
    MemoryKind.REUSABLE_LESSON,
)


class MemoryRepository:
    def __init__(self, database: Database | ReadonlyDatabaseSnapshot) -> None:
        self._database = database

    def insert(self, memory: BehaviorMemoryInput) -> InsertResult:
        prepared = _prepare_memory(memory)
        with self._database.transaction() as connection:
            return self._insert_on_connection(connection, prepared)

    def _insert_on_connection(
        self,
        connection: sqlite3.Connection,
        memory: BehaviorMemoryInput,
    ) -> InsertResult:
        prepared = _prepare_memory(memory)
        project_id = str(prepared.project_id).lower()
        project = connection.execute(
            "select 1 from projects where project_id = ?", (project_id,)
        ).fetchone()
        if project is None:
            raise KeyError(prepared.project_id)
        source = connection.execute(
            "select source_agent from source_refs where source_reference_id = ?",
            (str(prepared.source_reference_id).lower(),),
        ).fetchone()
        if source is None:
            raise KeyError(prepared.source_reference_id)
        if source["source_agent"] != prepared.namespace.source_agent.value:
            raise ValueError("source_reference_id namespace mismatch")

        memory_id = str(uuid4()).lower()
        cursor = connection.execute(
            """
            insert or ignore into behavior_memories(
                memory_id, project_id, source_agent, model_id, task_fingerprint,
                memory_kind, normalized_content, content_hash,
                source_reference_id, created_at, confidence, lifecycle_state
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
            """,
            (
                memory_id,
                project_id,
                prepared.namespace.source_agent.value,
                prepared.namespace.model_id,
                prepared.task_fingerprint,
                prepared.memory_kind.value,
                prepared.normalized_content,
                prepared.content_hash,
                str(prepared.source_reference_id).lower(),
                _utc_iso(prepared.created_at),
                prepared.confidence,
            ),
        )
        inserted = cursor.rowcount == 1
        row = connection.execute(
            """
            select memory_id from behavior_memories
            where project_id = ? and source_agent = ? and model_id = ?
              and task_fingerprint = ? and memory_kind = ? and content_hash = ?
            """,
            (
                project_id,
                prepared.namespace.source_agent.value,
                prepared.namespace.model_id,
                prepared.task_fingerprint,
                prepared.memory_kind.value,
                prepared.content_hash,
            ),
        ).fetchone()
        assert row is not None
        return InsertResult(
            inserted=inserted,
            duplicate=not inserted,
            record_id=row["memory_id"],
        )

    def search(
        self,
        project_id: UUID,
        namespace: Namespace,
        query: str,
        limit: int,
    ) -> list[BehaviorMemoryRecord]:
        _validate_limit(limit)
        terms = _tokens(query)
        with self._database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select *
                from behavior_memories
                where project_id = ?
                  and source_agent = ?
                  and model_id = ?
                  and lifecycle_state = 'active'
                order by created_at desc, memory_id
                limit ?
                """,
                (
                    str(project_id).lower(),
                    namespace.source_agent.value,
                    namespace.model_id,
                    _CANDIDATE_WINDOW,
                ),
            ).fetchall()

        records = [_memory_record(row) for row in rows]
        if not terms:
            ranked = sorted(
                records,
                key=lambda row: (
                    -_timestamp(row.created_at),
                    str(row.memory_id),
                ),
            )
            return ranked[:limit]

        scored = []
        for record in records:
            content = record.normalized_content.casefold()
            score = sum(content.count(term) for term in terms)
            if score:
                scored.append((score, record))
        scored.sort(
            key=lambda item: (
                -item[0],
                -_timestamp(item[1].created_at),
                str(item[1].memory_id),
            )
        )
        return [record for _, record in scored[:limit]]

    def get_by_id(self, memory_id: UUID) -> BehaviorMemoryRecord:
        with self._database.connect(readonly=True) as connection:
            return self._get_by_id_on_connection(connection, memory_id)

    def list_scoped(
        self,
        project_id: UUID,
        namespace: Namespace,
        *,
        limit: int = 100,
    ) -> tuple[BehaviorMemoryRecord, ...]:
        _validate_limit(limit)
        with self._database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select * from behavior_memories
                where project_id = ? and source_agent = ? and model_id = ?
                  and lifecycle_state <> 'deleted'
                order by created_at desc, memory_id
                limit ?
                """,
                (
                    str(project_id).lower(),
                    namespace.source_agent.value,
                    namespace.model_id,
                    limit,
                ),
            ).fetchall()
        return tuple(_memory_record(row) for row in rows)

    def get_scoped(
        self,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
    ) -> BehaviorMemoryRecord:
        with self._database.connect(readonly=True) as connection:
            return self._get_scoped_on_connection(connection, project_id, namespace, memory_id)

    def _get_scoped_on_connection(
        self,
        connection: sqlite3.Connection,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
    ) -> BehaviorMemoryRecord:
        row = connection.execute(
            """
            select * from behavior_memories
            where memory_id = ? and project_id = ?
              and source_agent = ? and model_id = ?
            """,
            (
                str(memory_id).lower(),
                str(project_id).lower(),
                namespace.source_agent.value,
                namespace.model_id,
            ),
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return _memory_record(row)

    def set_lifecycle_scoped(
        self,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
        lifecycle_state: LifecycleState,
    ) -> BehaviorMemoryRecord:
        selected = LifecycleState(lifecycle_state)
        if selected not in {LifecycleState.ARCHIVED, LifecycleState.DELETED}:
            raise ValueError("unsupported control lifecycle")
        with self._database.transaction() as connection:
            cursor = connection.execute(
                """
                update behavior_memories set lifecycle_state = ?
                where memory_id = ? and project_id = ?
                  and source_agent = ? and model_id = ?
                  and lifecycle_state <> 'deleted'
                """,
                (
                    selected.value,
                    str(memory_id).lower(),
                    str(project_id).lower(),
                    namespace.source_agent.value,
                    namespace.model_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(memory_id)
            return self._get_scoped_on_connection(connection, project_id, namespace, memory_id)

    def _get_by_id_on_connection(
        self, connection: sqlite3.Connection, memory_id: UUID
    ) -> BehaviorMemoryRecord:
        row = connection.execute(
            "select * from behavior_memories where memory_id = ?",
            (str(memory_id).lower(),),
        ).fetchone()
        if row is None:
            raise KeyError(memory_id)
        return _memory_record(row)

    def list_compaction_namespaces(self, project_id: UUID) -> tuple[Namespace, ...]:
        with self._database.connect(readonly=True) as connection:
            return self._list_compaction_namespaces_on_connection(
                connection, project_id, limit=1_000
            )

    @staticmethod
    def _list_compaction_namespaces_on_connection(
        connection: sqlite3.Connection,
        project_id: UUID,
        *,
        limit: int,
    ) -> tuple[Namespace, ...]:
        if type(limit) is not int or not 1 <= limit <= 10_001:
            raise ValueError("namespace limit must be between 1 and 10001")
        project = str(project_id).lower()
        selected: list[Namespace] = []
        cursor: tuple[str, str] | None = None
        while len(selected) < limit:
            if cursor is None:
                row = connection.execute(
                    """
                    select source_agent, model_id
                    from behavior_memories
                         indexed by idx_behavior_memories_active_namespace
                    where project_id = ? and lifecycle_state = 'active'
                      and memory_kind <> 'retrospective'
                    order by source_agent, model_id
                    limit 1
                    """,
                    (project,),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    select source_agent, model_id
                    from behavior_memories
                         indexed by idx_behavior_memories_active_namespace
                    where project_id = ? and lifecycle_state = 'active'
                      and memory_kind <> 'retrospective'
                      and source_agent = ? and model_id > ?
                    order by model_id
                    limit 1
                    """,
                    (project, *cursor),
                ).fetchone()
                if row is None:
                    row = connection.execute(
                        """
                        select source_agent, model_id
                        from behavior_memories
                             indexed by idx_behavior_memories_active_namespace
                        where project_id = ? and lifecycle_state = 'active'
                          and memory_kind <> 'retrospective'
                          and source_agent > ?
                        order by source_agent, model_id
                        limit 1
                        """,
                        (project, cursor[0]),
                    ).fetchone()
            if row is None:
                break
            namespace = Namespace(
                source_agent=row["source_agent"],
                model_id=row["model_id"],
            )
            selected.append(namespace)
            cursor = (namespace.source_agent.value, namespace.model_id)
        return tuple(selected)

    @staticmethod
    def _select_compaction_sources_on_connection(
        connection: sqlite3.Connection,
        project_id: UUID,
        namespace: Namespace,
        *,
        limit: int,
    ) -> tuple[tuple[BehaviorMemoryRecord, ...], int, int]:
        if type(limit) is not int or not 1 <= limit <= 10_000:
            raise ValueError("compaction limit must be between 1 and 10000")
        parameters = (
            str(project_id).lower(),
            namespace.source_agent.value,
            namespace.model_id,
        )
        mandatory_ids = _compaction_identifiers(
            connection,
            parameters,
            _COMPACTION_MANDATORY_KINDS,
            limit=limit + 1,
        )
        if len(mandatory_ids) > limit:
            return (), limit + 1, limit + 1

        optional_capacity = limit - len(mandatory_ids)
        optional_ids = _compaction_identifiers(
            connection,
            parameters,
            _COMPACTION_OPTIONAL_KINDS,
            limit=optional_capacity + 1,
        )
        optional_overflow = len(optional_ids) > optional_capacity
        selected_ids = (*mandatory_ids, *optional_ids[:optional_capacity])
        total = len(selected_ids) + int(optional_overflow)
        if not selected_ids:
            return (), total, len(mandatory_ids)
        records = _compaction_records(
            connection,
            parameters,
            selected_ids,
        )
        return (
            records,
            int(total),
            len(mandatory_ids),
        )

    def _store_compaction_on_connection(
        self,
        connection: sqlite3.Connection,
        project_id: UUID,
        namespace: Namespace,
        source_ids: tuple[UUID, ...],
        normalized_content: str,
        created_at: datetime,
    ) -> tuple[InsertResult, int]:
        content = _required_text(normalized_content, "normalized_content")
        ordered_ids = tuple(sorted({str(item).lower() for item in source_ids}))
        if not ordered_ids:
            raise ValueError("compaction requires source rows")
        if len(ordered_ids) != len(source_ids):
            raise ValueError("compaction source rows must be unique")

        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        model_digest = hashlib.sha256(namespace.model_id.encode("utf-8")).hexdigest()
        included_digest = hashlib.sha256("\n".join(ordered_ids).encode("ascii")).hexdigest()
        namespace_digest = hashlib.sha256(
            "\0".join(
                (
                    str(project_id).lower(),
                    namespace.source_agent.value,
                    model_digest,
                    included_digest,
                )
            ).encode("ascii")
        ).hexdigest()
        source_record_id = f"compaction-v1:{namespace_digest}"
        task_fingerprint = hashlib.sha256(
            f"compaction-v1\0{namespace.source_agent.value}\0"
            f"{model_digest}\0{included_digest}".encode("ascii")
        ).hexdigest()
        source_reference_id = str(uuid4()).lower()
        timestamp = _utc_iso(created_at)
        connection.execute(
            """
            insert or ignore into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, ?, ?, null, ?, ?, 'compaction-v1', ?)
            """,
            (
                source_reference_id,
                namespace.source_agent.value,
                source_record_id,
                content_hash,
                timestamp,
                timestamp,
            ),
        )
        source_row = connection.execute(
            """
            select source_reference_id, source_path, parser_version
            from source_refs
            where source_agent = ? and source_record_id = ? and content_hash = ?
            """,
            (namespace.source_agent.value, source_record_id, content_hash),
        ).fetchone()
        if (
            source_row is None
            or source_row["source_path"] is not None
            or source_row["parser_version"] != "compaction-v1"
        ):
            raise RuntimeError("compaction provenance collision")
        try:
            raw_source_reference_id = source_row["source_reference_id"]
            source_reference_id = str(UUID(raw_source_reference_id)).lower()
        except (TypeError, ValueError, AttributeError):
            raise RuntimeError("compaction provenance collision") from None
        if source_reference_id != raw_source_reference_id:
            raise RuntimeError("compaction provenance collision")

        memory_id = str(uuid4()).lower()
        cursor = connection.execute(
            """
            insert or ignore into behavior_memories(
                memory_id, project_id, source_agent, model_id, task_fingerprint,
                memory_kind, normalized_content, content_hash,
                source_reference_id, created_at, confidence, lifecycle_state
            ) values (?, ?, ?, ?, ?, 'retrospective', ?, ?, ?, ?, 1.0, 'active')
            """,
            (
                memory_id,
                str(project_id).lower(),
                namespace.source_agent.value,
                namespace.model_id,
                task_fingerprint,
                content,
                content_hash,
                source_reference_id,
                timestamp,
            ),
        )
        inserted = cursor.rowcount == 1
        retrospective = connection.execute(
            """
            select memory_id, normalized_content, source_reference_id
            from behavior_memories
            where project_id = ? and source_agent = ? and model_id = ?
              and lifecycle_state = 'active' and memory_kind = 'retrospective'
              and task_fingerprint = ? and content_hash = ?
            """,
            (
                str(project_id).lower(),
                namespace.source_agent.value,
                namespace.model_id,
                task_fingerprint,
                content_hash,
            ),
        ).fetchone()
        if (
            retrospective is None
            or retrospective["normalized_content"] != content
            or retrospective["source_reference_id"] != source_reference_id
        ):
            raise RuntimeError("compaction retrospective collision")

        cold_count = self._cold_compaction_sources_on_connection(
            connection,
            project_id,
            namespace,
            tuple(UUID(item) for item in ordered_ids),
        )
        if cold_count != len(ordered_ids):
            raise RuntimeError("compaction source set changed")
        return (
            InsertResult(
                inserted=inserted,
                duplicate=not inserted,
                record_id=retrospective["memory_id"],
            ),
            cold_count,
        )

    @staticmethod
    def _cold_compaction_sources_on_connection(
        connection: sqlite3.Connection,
        project_id: UUID,
        namespace: Namespace,
        source_ids: tuple[UUID, ...],
    ) -> int:
        cold_count = 0
        for start in range(0, len(source_ids), _COMPACTION_UPDATE_BATCH):
            batch = source_ids[start : start + _COMPACTION_UPDATE_BATCH]
            placeholders = ",".join("?" for _ in batch)
            cursor = connection.execute(
                f"""
                update behavior_memories
                set lifecycle_state = 'cold'
                where project_id = ? and source_agent = ? and model_id = ?
                  and lifecycle_state = 'active'
                  and memory_kind <> 'retrospective'
                  and memory_id in ({placeholders})
                """,
                (
                    str(project_id).lower(),
                    namespace.source_agent.value,
                    namespace.model_id,
                    *(str(item).lower() for item in batch),
                ),
            )
            cold_count += cursor.rowcount
        return cold_count


def _compaction_identifiers(
    connection: sqlite3.Connection,
    namespace_parameters: tuple[str, str, str],
    kinds: tuple[MemoryKind, ...],
    *,
    limit: int,
) -> tuple[str, ...]:
    if type(limit) is not int or limit <= 0:
        raise ValueError("compaction identifier limit must be positive")
    placeholders = ",".join("?" for _ in kinds)
    rows = connection.execute(
        f"""
        select memory_id
        from behavior_memories
        where project_id = ? and source_agent = ? and model_id = ?
          and lifecycle_state = 'active'
          and memory_kind in ({placeholders})
        order by memory_kind, created_at desc, memory_id
        limit ?
        """,
        (
            *namespace_parameters,
            *(kind.value for kind in kinds),
            limit,
        ),
    ).fetchall()
    return tuple(row["memory_id"] for row in rows)


def _compaction_records(
    connection: sqlite3.Connection,
    namespace_parameters: tuple[str, str, str],
    selected_ids: tuple[str, ...],
) -> tuple[BehaviorMemoryRecord, ...]:
    ordered_ids = tuple(dict.fromkeys(selected_ids))
    if len(ordered_ids) != len(selected_ids):
        raise RuntimeError("compaction source identifiers are not unique")
    records: dict[str, BehaviorMemoryRecord] = {}
    for start in range(0, len(ordered_ids), _COMPACTION_UPDATE_BATCH):
        batch = ordered_ids[start : start + _COMPACTION_UPDATE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        oversized = connection.execute(
            f"""
            select 1
            from behavior_memories
            where project_id = ? and source_agent = ? and model_id = ?
              and lifecycle_state = 'active'
              and memory_kind <> 'retrospective'
              and memory_id in ({placeholders})
              and (
                length(normalized_content) > ?
                or length(cast(normalized_content as blob)) > ?
              )
            limit 1
            """,
            (
                *namespace_parameters,
                *batch,
                _COMPACTION_MAX_SOURCE_CHARS,
                _COMPACTION_MAX_SOURCE_BYTES,
            ),
        ).fetchone()
        if oversized is not None:
            raise RuntimeError("compaction source field limit exceeded")
        rows = connection.execute(
            f"""
            select *
            from behavior_memories
            where project_id = ? and source_agent = ? and model_id = ?
              and lifecycle_state = 'active'
              and memory_kind <> 'retrospective'
              and memory_id in ({placeholders})
            """,
            (*namespace_parameters, *batch),
        ).fetchall()
        for row in rows:
            record = _memory_record(row)
            records[str(record.memory_id).lower()] = record

    if set(records) != set(ordered_ids):
        raise RuntimeError("compaction source set changed")
    return tuple(records[memory_id] for memory_id in ordered_ids)


def _prepare_memory(memory: BehaviorMemoryInput) -> BehaviorMemoryInput:
    values = memory.model_dump()
    values["normalized_content"] = _required_text(memory.normalized_content, "normalized_content")
    values["task_fingerprint"] = _required_hash(memory.task_fingerprint, "task_fingerprint")
    values["content_hash"] = _required_hash(memory.content_hash, "content_hash")
    expected = hashlib.sha256(values["normalized_content"].encode("utf-8")).hexdigest()
    if values["content_hash"] != expected:
        raise ValueError("content_hash does not match normalized_content")
    return BehaviorMemoryInput.model_validate(values)


def _memory_record(row: sqlite3.Row) -> BehaviorMemoryRecord:
    return BehaviorMemoryRecord.model_validate(
        {
            "memory_id": row["memory_id"],
            "project_id": row["project_id"],
            "namespace": {
                "source_agent": row["source_agent"],
                "model_id": row["model_id"],
            },
            "task_fingerprint": row["task_fingerprint"],
            "memory_kind": row["memory_kind"],
            "normalized_content": row["normalized_content"],
            "content_hash": row["content_hash"],
            "source_reference_id": row["source_reference_id"],
            "created_at": row["created_at"],
            "confidence": row["confidence"],
            "lifecycle_state": row["lifecycle_state"],
        }
    )


def _required_text(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def _required_hash(value: str, field_name: str) -> str:
    stripped = _required_text(value, field_name)
    if _SHA256.fullmatch(stripped) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 hash")
    return stripped


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def _tokens(query: str) -> tuple[str, ...]:
    if not isinstance(query, str):
        raise TypeError("query must be text")
    return tuple(dict.fromkeys(term.casefold() for term in _TOKEN.findall(query)))


def _timestamp(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
