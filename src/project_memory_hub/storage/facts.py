from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from uuid import UUID, uuid4

from project_memory_hub.domain import FactRecord, ProjectFactInput
from project_memory_hub.storage.database import Database, ReadonlyDatabaseSnapshot


_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class FactRepository:
    def __init__(self, database: Database | ReadonlyDatabaseSnapshot) -> None:
        self._database = database

    def observe(self, project_id: UUID, fact: ProjectFactInput) -> FactRecord:
        prepared = _prepare_fact(fact)
        with self._database.transaction() as connection:
            record, _, _ = self._observe_on_connection(connection, project_id, prepared)
        return record

    def _observe_many(
        self, project_id: UUID, facts: tuple[ProjectFactInput, ...]
    ) -> tuple[tuple[FactRecord, ...], int]:
        records: list[FactRecord] = []
        stale_count = 0
        with self._database.transaction() as connection:
            for fact in facts:
                record, stale, _ = self._observe_on_connection(connection, project_id, fact)
                records.append(record)
                stale_count += stale
        return tuple(records), stale_count

    def _observe_many_with_changes(
        self,
        project_id: UUID,
        facts: tuple[ProjectFactInput, ...],
        *,
        before_observe: Callable[[sqlite3.Connection], None] | None = None,
        after_observe: Callable[[sqlite3.Connection], None] | None = None,
        on_effective_change: Callable[[sqlite3.Connection], None] | None = None,
    ) -> tuple[tuple[FactRecord, ...], int, int]:
        records: list[FactRecord] = []
        stale_count = 0
        changed_count = 0
        with self._database.transaction() as connection:
            if before_observe is not None:
                before_observe(connection)
            for fact in facts:
                record, stale, changed = self._observe_on_connection(connection, project_id, fact)
                records.append(record)
                stale_count += stale
                changed_count += int(changed)
            if changed_count and on_effective_change is not None:
                on_effective_change(connection)
            if after_observe is not None:
                after_observe(connection)
        return tuple(records), stale_count, changed_count

    def _observe_on_connection(
        self,
        connection: sqlite3.Connection,
        project_id: UUID,
        fact: ProjectFactInput,
    ) -> tuple[FactRecord, int, bool]:
        prepared = _prepare_fact(fact)
        project_id_text = str(project_id).lower()
        project = connection.execute(
            "select 1 from projects where project_id = ?", (project_id_text,)
        ).fetchone()
        if project is None:
            raise KeyError(project_id)

        exact = connection.execute(
            """
            select * from project_facts
            where project_id = ? and category = ? and normalized_content = ?
              and evidence_type = ? and evidence_reference = ?
            order by observed_at desc, fact_id
            limit 1
            """,
            (
                project_id_text,
                prepared.category,
                prepared.normalized_content,
                prepared.evidence_type,
                prepared.evidence_reference,
            ),
        ).fetchone()
        observed_at = _utc_iso(prepared.observed_at)
        observed_datetime = _parse_utc(observed_at)
        active_rows = connection.execute(
            """
            select *
            from project_facts
            where project_id = ? and category = ? and evidence_reference = ?
              and lifecycle_state = 'active' and stale_at is null
            """,
            (
                project_id_text,
                prepared.category,
                prepared.evidence_reference,
            ),
        ).fetchall()
        now = _utc_now()
        winner = _current_winner(active_rows)
        incoming_wins = (
            winner is None
            or prepared.normalized_content == winner["normalized_content"]
            or observed_datetime > _row_time(winner)
        )
        if incoming_wins:
            winning_content = prepared.normalized_content
        else:
            assert winner is not None
            winning_content = winner["normalized_content"]
        rows_to_stale = [row for row in active_rows if row["normalized_content"] != winning_content]
        rows_to_stale.sort(key=lambda row: (_row_time(row), row["fact_id"]), reverse=True)
        if rows_to_stale:
            connection.executemany(
                """
                update project_facts
                set lifecycle_state = 'cold', stale_at = ?
                where fact_id = ? and lifecycle_state = 'active' and stale_at is null
                """,
                [(now, row["fact_id"]) for row in rows_to_stale],
            )
        supersedes_fact_id = (
            rows_to_stale[0]["fact_id"] if incoming_wins and rows_to_stale else None
        )
        if exact is not None:
            exact_was_active = exact["lifecycle_state"] == "active" and exact["stale_at"] is None
            exact_time = _row_time(exact)
            replacement_time = observed_at if observed_datetime > exact_time else None
            replacement_confidence = (
                prepared.confidence if observed_datetime > exact_time else exact["confidence"]
            )
            lifecycle_state = "active" if incoming_wins else "cold"
            stale_at = None if incoming_wins else (exact["stale_at"] or now)
            connection.execute(
                """
                update project_facts
                set observed_at = coalesce(?, observed_at), confidence = ?,
                    supersedes_fact_id = coalesce(?, supersedes_fact_id),
                    stale_at = ?, lifecycle_state = ?
                where fact_id = ?
                """,
                (
                    replacement_time,
                    replacement_confidence,
                    supersedes_fact_id,
                    stale_at,
                    lifecycle_state,
                    exact["fact_id"],
                ),
            )
            row = connection.execute(
                "select * from project_facts where fact_id = ?", (exact["fact_id"],)
            ).fetchone()
            assert row is not None
            effective_change = bool(rows_to_stale) or (incoming_wins and not exact_was_active)
            return _fact_record(row), len(rows_to_stale), effective_change
        fact_id = str(uuid4()).lower()
        lifecycle_state = "active" if incoming_wins else "cold"
        stale_at = None if incoming_wins else now
        connection.execute(
            """
            insert into project_facts(
                fact_id, project_id, category, normalized_content, evidence_type,
                evidence_reference, observed_at, confidence, supersedes_fact_id,
                stale_at, lifecycle_state, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fact_id,
                project_id_text,
                prepared.category,
                prepared.normalized_content,
                prepared.evidence_type,
                prepared.evidence_reference,
                observed_at,
                prepared.confidence,
                supersedes_fact_id,
                stale_at,
                lifecycle_state,
                now,
            ),
        )
        row = connection.execute(
            "select * from project_facts where fact_id = ?", (fact_id,)
        ).fetchone()
        assert row is not None
        return _fact_record(row), len(rows_to_stale), incoming_wins

    def search(self, project_id: UUID, query: str, limit: int) -> list[FactRecord]:
        _validate_limit(limit)
        terms = _tokens(query)
        project_id_text = str(project_id).lower()
        with self._database.connect(readonly=True) as connection:
            if terms:
                expression = " AND ".join(f'"{term}"' for term in terms)
                rows = connection.execute(
                    """
                    select facts.*, bm25(project_facts_fts) as fts_rank
                    from project_facts_fts
                    join project_facts as facts
                      on facts.rowid = project_facts_fts.rowid
                    where facts.project_id = ?
                      and facts.lifecycle_state = 'active'
                      and facts.stale_at is null
                      and project_facts_fts match ?
                    order by fts_rank, facts.observed_at desc, facts.fact_id
                    limit ?
                    """,
                    (project_id_text, expression, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    select * from project_facts
                    where project_id = ? and lifecycle_state = 'active'
                      and stale_at is null
                    order by observed_at desc, fact_id
                    limit ?
                    """,
                    (project_id_text, limit),
                ).fetchall()
        return [_fact_record(row) for row in rows]


def _prepare_fact(fact: ProjectFactInput) -> ProjectFactInput:
    values = fact.model_dump()
    for name in (
        "category",
        "normalized_content",
        "evidence_type",
        "evidence_reference",
    ):
        values[name] = _required_text(values[name], name)
    return ProjectFactInput.model_validate(values)


def _fact_record(row: sqlite3.Row) -> FactRecord:
    return FactRecord.model_validate(
        {
            "fact_id": row["fact_id"],
            "project_id": row["project_id"],
            "category": row["category"],
            "normalized_content": row["normalized_content"],
            "evidence_type": row["evidence_type"],
            "evidence_reference": row["evidence_reference"],
            "observed_at": row["observed_at"],
            "confidence": row["confidence"],
            "lifecycle_state": row["lifecycle_state"],
        }
    )


def _required_text(value: str, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{field_name} must not be blank")
    return stripped


def _validate_limit(limit: int) -> None:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")


def _tokens(query: str) -> tuple[str, ...]:
    if not isinstance(query, str):
        raise TypeError("query must be text")
    return tuple(dict.fromkeys(term.casefold() for term in _TOKEN.findall(query)))


def _utc_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _row_time(row: sqlite3.Row) -> datetime:
    return _parse_utc(row["observed_at"])


def _current_winner(rows: list[sqlite3.Row]) -> sqlite3.Row | None:
    if not rows:
        return None
    newest_time = max(_row_time(row) for row in rows)
    newest = [row for row in rows if _row_time(row) == newest_time]
    return min(newest, key=lambda row: (row["created_at"], row["fact_id"]))


def _utc_now() -> str:
    return _utc_iso(datetime.now(timezone.utc))
