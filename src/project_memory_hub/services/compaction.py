from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from uuid import UUID

from project_memory_hub.domain import (
    BehaviorMemoryRecord,
    MemoryKind,
    Namespace,
    ProjectRecord,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.storage.database import (
    Database,
    ReadonlyDatabaseSnapshot,
    strict_utc_epoch_us,
)
from project_memory_hub.storage.memories import MemoryRepository


_MAX_COUNT = 2**31 - 1
_DEFAULT_SOURCE_LIMIT = 10_000
_MAX_ENTRY_CHARS = 512
_MAX_ENTRY_BYTES = 2_048
_MAX_RETROSPECTIVE_CHARS = 262_144
_MAX_RETROSPECTIVE_BYTES = 1_048_576
_MAX_NAMESPACES_PER_PROJECT = 1_000
_MAX_INACTIVE_PROJECTS_PER_RUN = 1_000
_SHA256 = re.compile(r"[0-9a-f]{64}")
_FAILURE_DYNAMIC = re.compile(
    r"(?ix)(?:"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b|"
    r"\b[0-9a-f]{16,}\b|"
    r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:[.]\d+)?(?:z|[+-]\d{2}:?\d{2})?\b"
    r")"
)
_SECTION_ORDER = (
    (MemoryKind.VERIFIED_METHOD, "Verified methods"),
    (MemoryKind.OPEN_ISSUE, "Open issues"),
    (MemoryKind.FAILED_ATTEMPT, "Failed attempts"),
    (MemoryKind.RISK, "Risks"),
    (MemoryKind.PREFERENCE, "Preferences"),
    (MemoryKind.DECISION, "Decisions"),
    (MemoryKind.OUTCOME, "Outcomes"),
    (MemoryKind.REUSABLE_LESSON, "Reusable lessons"),
)
_MANDATORY_KINDS = frozenset(
    {
        MemoryKind.FAILED_ATTEMPT,
        MemoryKind.VERIFIED_METHOD,
        MemoryKind.OPEN_ISSUE,
        MemoryKind.RISK,
    }
)


class CompactionBoundsError(RuntimeError):
    """A bounded retrospective cannot safely represent the mandatory source set."""


class CompactionEligibilityChanged(RuntimeError):
    """The project changed after it was selected for inactivity compaction."""


@dataclass(frozen=True, slots=True)
class CompactionResult:
    project_id: UUID
    namespace: Namespace
    status: Literal["compacted", "noop", "dry_run"]
    source_count: int = 0
    source_reference_count: int = 0
    cold_count: int = 0
    retrospective_count: int = 0
    remaining_count: int = 0


@dataclass(frozen=True, slots=True)
class CompactionSummary:
    project_count: int = 0
    namespace_count: int = 0
    source_count: int = 0
    cold_count: int = 0
    retrospective_count: int = 0
    remaining_count: int = 0
    failure_count: int = 0


@dataclass(frozen=True, slots=True)
class _InactiveProject:
    record: ProjectRecord
    stored_timestamp: str
    stored_epoch_us: int


@dataclass(frozen=True, slots=True)
class _CompactionPlan:
    content: str
    source_ids: tuple[UUID, ...]
    source_reference_count: int


class CompactionService:
    def __init__(
        self,
        database: Database | ReadonlyDatabaseSnapshot,
        memories: MemoryRepository,
        redactor: Redactor,
        *,
        inactive_days: int = 21,
        now: Callable[[], datetime] | None = None,
        source_limit: int = _DEFAULT_SOURCE_LIMIT,
    ) -> None:
        if type(inactive_days) is not int or inactive_days <= 0:
            raise ValueError("inactive_days must be a positive integer")
        if type(source_limit) is not int or not 1 <= source_limit <= 10_000:
            raise ValueError("source_limit must be between 1 and 10000")
        self._database = database
        self._memories = memories
        self._redactor = redactor
        self._inactive_days = inactive_days
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._source_limit = source_limit

    def find_inactive(self, as_of: datetime) -> list[ProjectRecord]:
        return [
            snapshot.record
            for snapshot in self._inactive_projects(
                as_of,
                limit=_MAX_INACTIVE_PROJECTS_PER_RUN,
            )
        ]

    def compact(
        self,
        project_id: UUID,
        namespace: Namespace,
        *,
        dry_run: bool = False,
        _expected: _InactiveProject | None = None,
        _as_of: datetime | None = None,
    ) -> CompactionResult:
        if dry_run:
            with self._database.connect(readonly=True) as connection:
                self._require_project_on_connection(connection, project_id)
                records, total, mandatory_total = (
                    self._memories._select_compaction_sources_on_connection(
                        connection,
                        project_id,
                        namespace,
                        limit=self._source_limit,
                    )
                )
                return self._preview_result(
                    project_id,
                    namespace,
                    records,
                    total,
                    mandatory_total,
                )

        created_at = _strict_utc(self._now() if _as_of is None else _as_of)
        with self._database.transaction() as connection:
            self._require_project_on_connection(connection, project_id)
            if _expected is not None:
                self._assert_eligible_on_connection(
                    connection,
                    _expected,
                    created_at,
                )
            records, total, mandatory_total = (
                self._memories._select_compaction_sources_on_connection(
                    connection,
                    project_id,
                    namespace,
                    limit=self._source_limit,
                )
            )
            self._require_bounded_source_set(total, mandatory_total)
            if not records:
                return CompactionResult(
                    project_id=project_id,
                    namespace=namespace,
                    status="noop",
                )
            plan = self._build_bounded_plan(records)
            inserted, cold_count = self._memories._store_compaction_on_connection(
                connection,
                project_id,
                namespace,
                plan.source_ids,
                plan.content,
                created_at,
            )
            return CompactionResult(
                project_id=project_id,
                namespace=namespace,
                status="compacted",
                source_count=len(plan.source_ids),
                source_reference_count=plan.source_reference_count,
                cold_count=cold_count,
                retrospective_count=int(inserted.inserted),
                remaining_count=_bounded_count(max(0, total - cold_count)),
            )

    def compact_project(
        self,
        project_id: UUID,
        *,
        dry_run: bool = False,
        _expected: _InactiveProject | None = None,
        _as_of: datetime | None = None,
    ) -> CompactionSummary:
        selected_now = _strict_utc(self._now() if _as_of is None else _as_of)
        owns_snapshot = _expected is None
        expected = _expected
        if expected is None:
            expected = self._inactive_project(project_id, selected_now)
            if expected is None:
                return CompactionSummary()

        with self._database.connect(readonly=True) as connection:
            self._require_project_on_connection(connection, project_id)
            self._assert_eligible_on_connection(connection, expected, selected_now)
            discovered = self._memories._list_compaction_namespaces_on_connection(
                connection,
                project_id,
                limit=_MAX_NAMESPACES_PER_PROJECT + 1,
            )
        namespace_backlog = len(discovered) > _MAX_NAMESPACES_PER_PROJECT
        namespaces = discovered[:_MAX_NAMESPACES_PER_PROJECT]

        totals = CompactionSummary(
            project_count=1,
            namespace_count=len(namespaces),
            remaining_count=int(namespace_backlog),
        )
        for namespace in namespaces:
            try:
                result = self.compact(
                    project_id,
                    namespace,
                    dry_run=dry_run,
                    _expected=expected,
                    _as_of=selected_now,
                )
            except Exception:
                totals = _merge_summaries(
                    totals,
                    CompactionSummary(failure_count=1),
                )
                continue
            totals = _merge_summaries(
                totals,
                CompactionSummary(
                    source_count=result.source_count,
                    cold_count=result.cold_count,
                    retrospective_count=result.retrospective_count,
                    remaining_count=result.remaining_count,
                ),
            )

        if (
            owns_snapshot
            and not dry_run
            and totals.failure_count == 0
            and totals.remaining_count == 0
        ):
            try:
                marked = self._mark_inactive(expected, selected_now)
            except Exception:
                marked = False
            if not marked:
                totals = _merge_summaries(
                    totals,
                    CompactionSummary(failure_count=1),
                )
        return totals

    def compact_all_inactive(
        self,
        as_of: datetime | None = None,
        *,
        dry_run: bool = False,
    ) -> CompactionSummary:
        selected_now = _strict_utc(self._now() if as_of is None else as_of)
        discovered = self._inactive_projects(
            selected_now,
            limit=_MAX_INACTIVE_PROJECTS_PER_RUN + 1,
        )
        project_backlog = len(discovered) > _MAX_INACTIVE_PROJECTS_PER_RUN
        snapshots = discovered[:_MAX_INACTIVE_PROJECTS_PER_RUN]
        totals = CompactionSummary(remaining_count=int(project_backlog))
        for snapshot in snapshots:
            try:
                result = self.compact_project(
                    snapshot.record.project_id,
                    dry_run=dry_run,
                    _expected=snapshot,
                    _as_of=selected_now,
                )
                totals = _merge_summaries(totals, result)
                if not dry_run and result.failure_count == 0 and result.remaining_count == 0:
                    if not self._mark_inactive(snapshot, selected_now):
                        totals = _merge_summaries(totals, CompactionSummary(failure_count=1))
            except Exception:
                totals = _merge_summaries(
                    totals,
                    CompactionSummary(project_count=1, failure_count=1),
                )
        return totals

    def compact_newly_inactive(self, as_of: datetime) -> CompactionSummary:
        return self.compact_all_inactive(as_of, dry_run=False)

    def _preview_result(
        self,
        project_id: UUID,
        namespace: Namespace,
        records: tuple[BehaviorMemoryRecord, ...],
        total: int,
        mandatory_total: int,
    ) -> CompactionResult:
        self._require_bounded_source_set(total, mandatory_total)
        if not records:
            return CompactionResult(
                project_id=project_id,
                namespace=namespace,
                status="dry_run",
            )
        plan = self._build_bounded_plan(records)
        return CompactionResult(
            project_id=project_id,
            namespace=namespace,
            status="dry_run",
            source_count=len(plan.source_ids),
            source_reference_count=plan.source_reference_count,
            remaining_count=_bounded_count(max(0, total - len(plan.source_ids))),
        )

    def _require_bounded_source_set(self, total: int, mandatory_total: int) -> None:
        if total > self._source_limit + 1:
            raise CompactionBoundsError("compaction source accounting exceeded")
        if mandatory_total > self._source_limit:
            raise CompactionBoundsError("mandatory compaction source limit exceeded")

    def _build_plan(self, records: tuple[BehaviorMemoryRecord, ...]) -> _CompactionPlan:
        if not records:
            raise ValueError("compaction plan requires source rows")
        source_ids: list[UUID] = []
        groups: dict[tuple[MemoryKind, str], list[BehaviorMemoryRecord]] = {}
        for record in records:
            if record.memory_kind is MemoryKind.RETROSPECTIVE:
                raise RuntimeError("retrospective cannot be a compaction source")
            expected_hash = hashlib.sha256(record.normalized_content.encode("utf-8")).hexdigest()
            if (
                _SHA256.fullmatch(record.content_hash) is None
                or expected_hash != record.content_hash
            ):
                raise RuntimeError("compaction source hash mismatch")
            source_ids.append(record.memory_id)
            group_key = (
                _failure_signature(record.normalized_content)
                if record.memory_kind is MemoryKind.FAILED_ATTEMPT
                else record.content_hash
            )
            groups.setdefault((record.memory_kind, group_key), []).append(record)

        section_lines: list[str] = []
        all_source_refs: set[UUID] = set()
        for kind, label in _SECTION_ORDER:
            selected_groups = [
                group for (group_kind, _key), group in groups.items() if group_kind is kind
            ]
            if not selected_groups:
                continue
            representatives = [self._newest(group) for group in selected_groups]
            representatives.sort(key=_deterministic_memory_order)
            section_refs = {item.source_reference_id for group in selected_groups for item in group}
            all_source_refs.update(section_refs)
            section_lines.append(
                f"## {label} (items={len(representatives)}, source_references={len(section_refs)})"
            )
            for item in representatives:
                section_lines.append(
                    f"- {self._safe_entry(item.normalized_content, mandatory=kind in _MANDATORY_KINDS)}"
                )
            section_lines.append("")

        if not section_lines:
            raise RuntimeError("compaction source kinds are unsupported")
        all_source_refs.update(record.source_reference_id for record in records)
        document = "\n".join(
            (
                "# Namespace retrospective",
                f"Source rows: {len(records)}",
                f"Source references: {len(all_source_refs)}",
                "",
                *section_lines,
            )
        ).rstrip()
        final_result = self._redactor.redact(document)
        if "input_truncated" in final_result.findings:
            raise CompactionBoundsError("retrospective redaction limit exceeded")
        final = final_result.text.strip()
        if (
            not final
            or len(final) > _MAX_RETROSPECTIVE_CHARS
            or len(final.encode("utf-8")) > _MAX_RETROSPECTIVE_BYTES
        ):
            raise CompactionBoundsError("retrospective field limit exceeded")
        return _CompactionPlan(
            content=final,
            source_ids=tuple(sorted(source_ids, key=str)),
            source_reference_count=len(all_source_refs),
        )

    def _build_bounded_plan(
        self,
        records: tuple[BehaviorMemoryRecord, ...],
    ) -> _CompactionPlan:
        mandatory = tuple(record for record in records if record.memory_kind in _MANDATORY_KINDS)
        optional = tuple(record for record in records if record.memory_kind not in _MANDATORY_KINDS)
        if not optional:
            return self._build_plan(mandatory)

        best = self._build_plan(mandatory) if mandatory else None
        lower = 1
        upper = len(optional)
        while lower <= upper:
            selected = (lower + upper) // 2
            try:
                candidate = self._build_plan((*mandatory, *optional[:selected]))
            except CompactionBoundsError:
                upper = selected - 1
            else:
                best = candidate
                lower = selected + 1
        if best is None:
            raise CompactionBoundsError("optional retrospective entry cannot fit")
        return best

    def _safe_entry(self, value: str, *, mandatory: bool) -> str:
        result = self._redactor.redact(value)
        if "input_truncated" in result.findings:
            raise CompactionBoundsError("retrospective source limit exceeded")
        redacted = " ".join(result.text.split())
        if mandatory and (
            len(redacted) > _MAX_ENTRY_CHARS or len(redacted.encode("utf-8")) > _MAX_ENTRY_BYTES
        ):
            raise CompactionBoundsError("mandatory retrospective entry limit exceeded")
        bounded = _truncate_utf8(redacted, _MAX_ENTRY_CHARS, _MAX_ENTRY_BYTES)
        if not bounded:
            raise CompactionBoundsError("retrospective entry is empty")
        return bounded

    @staticmethod
    def _newest(group: list[BehaviorMemoryRecord]) -> BehaviorMemoryRecord:
        return max(
            group,
            key=lambda item: (
                _timestamp(item.created_at),
                item.normalized_content.casefold(),
                item.content_hash,
            ),
        )

    def _inactive_projects(
        self,
        as_of: datetime,
        *,
        limit: int,
    ) -> list[_InactiveProject]:
        if type(limit) is not int or not 1 <= limit <= 10_001:
            raise ValueError("inactive project limit must be between 1 and 10001")
        selected_now = _strict_utc(as_of)
        cutoff = selected_now - timedelta(days=self._inactive_days)
        cutoff_epoch_us = strict_utc_epoch_us(_iso(cutoff))
        if cutoff_epoch_us is None:
            raise RuntimeError("inactive cutoff cannot be indexed")
        selected: list[_InactiveProject] = []
        cursor: tuple[int, str] | None = None
        columns = """
            project_id, canonical_path, display_name,
            discovery_status, permission_status,
            last_observed_change, inactivity_state,
            last_observed_change_epoch_us
        """
        with self._database.connect(readonly=True) as connection:
            while len(selected) < limit:
                if cursor is None:
                    row = connection.execute(
                        f"""
                        select {columns}
                        from projects indexed by idx_projects_active_observed_epoch
                        where enabled = 1 and inactivity_state = 'active'
                          and last_observed_change_epoch_us is not null
                          and last_observed_change_epoch_us <= ?
                        order by last_observed_change_epoch_us, project_id
                        limit 1
                        """,
                        (cutoff_epoch_us,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        f"""
                        select {columns}
                        from projects indexed by idx_projects_active_observed_epoch
                        where enabled = 1 and inactivity_state = 'active'
                          and last_observed_change_epoch_us is not null
                          and last_observed_change_epoch_us = ?
                          and project_id > ?
                        order by project_id
                        limit 1
                        """,
                        cursor,
                    ).fetchone()
                    if row is None:
                        row = connection.execute(
                            f"""
                            select {columns}
                            from projects
                                 indexed by idx_projects_active_observed_epoch
                            where enabled = 1 and inactivity_state = 'active'
                              and last_observed_change_epoch_us is not null
                              and last_observed_change_epoch_us > ?
                              and last_observed_change_epoch_us <= ?
                            order by last_observed_change_epoch_us, project_id
                            limit 1
                            """,
                            (cursor[0], cutoff_epoch_us),
                        ).fetchone()
                if row is None:
                    break
                cursor = (
                    int(row["last_observed_change_epoch_us"]),
                    row["project_id"],
                )
                snapshot = self._inactive_snapshot_from_row(
                    row,
                    selected_now,
                    cutoff,
                )
                if snapshot is not None:
                    selected.append(snapshot)
        return selected

    def _inactive_project(
        self,
        project_id: UUID,
        as_of: datetime,
    ) -> _InactiveProject | None:
        selected_now = _strict_utc(as_of)
        cutoff = selected_now - timedelta(days=self._inactive_days)
        with self._database.connect(readonly=True) as connection:
            row = connection.execute(
                """
                select project_id, canonical_path, display_name,
                       discovery_status, permission_status,
                       last_observed_change, last_observed_change_epoch_us,
                       inactivity_state, enabled
                from projects
                where project_id = ?
                """,
                (str(project_id).lower(),),
            ).fetchone()
        if row is None or row["enabled"] != 1:
            raise KeyError(project_id)
        if row["inactivity_state"] != "active":
            return None
        return self._inactive_snapshot_from_row(row, selected_now, cutoff)

    @staticmethod
    def _inactive_snapshot_from_row(
        row: sqlite3.Row,
        selected_now: datetime,
        cutoff: datetime,
    ) -> _InactiveProject | None:
        stored = row["last_observed_change"]
        stored_epoch_us = row["last_observed_change_epoch_us"]
        try:
            observed = _parse_strict_utc(stored)
            parsed_epoch_us = strict_utc_epoch_us(stored)
            if (
                type(stored_epoch_us) is not int
                or parsed_epoch_us is None
                or stored_epoch_us != parsed_epoch_us
            ):
                return None
            if observed > selected_now or observed > cutoff:
                return None
            record = ProjectRecord.model_validate(
                {
                    "project_id": row["project_id"],
                    "canonical_path": row["canonical_path"],
                    "display_name": row["display_name"],
                    "discovery_status": row["discovery_status"],
                    "permission_status": row["permission_status"],
                    "last_observed_change": observed,
                }
            )
        except (TypeError, ValueError):
            return None
        return _InactiveProject(record, stored, stored_epoch_us)

    @staticmethod
    def _require_project_on_connection(connection: sqlite3.Connection, project_id: UUID) -> None:
        row = connection.execute(
            "select 1 from projects where project_id = ? and enabled = 1",
            (str(project_id).lower(),),
        ).fetchone()
        if row is None:
            raise KeyError(project_id)

    def _assert_eligible_on_connection(
        self,
        connection: sqlite3.Connection,
        expected: _InactiveProject,
        as_of: datetime,
    ) -> None:
        row = connection.execute(
            """
            select enabled, inactivity_state, last_observed_change,
                   last_observed_change_epoch_us
            from projects where project_id = ?
            """,
            (str(expected.record.project_id).lower(),),
        ).fetchone()
        if (
            row is None
            or row["enabled"] != 1
            or row["inactivity_state"] != "active"
            or row["last_observed_change"] != expected.stored_timestamp
            or row["last_observed_change_epoch_us"] != expected.stored_epoch_us
            or strict_utc_epoch_us(row["last_observed_change"]) != expected.stored_epoch_us
        ):
            raise CompactionEligibilityChanged("project activity changed")
        observed = _parse_strict_utc(row["last_observed_change"])
        selected_now = _strict_utc(as_of)
        if observed > selected_now - timedelta(days=self._inactive_days):
            raise CompactionEligibilityChanged("project is no longer inactive")

    def _mark_inactive(self, expected: _InactiveProject, as_of: datetime) -> bool:
        selected_now = _strict_utc(as_of)
        with self._database.transaction() as connection:
            self._assert_eligible_on_connection(connection, expected, selected_now)
            cursor = connection.execute(
                """
                update projects
                set inactivity_state = 'inactive', updated_at = ?
                where project_id = ? and enabled = 1
                  and inactivity_state = 'active'
                  and last_observed_change = ?
                  and last_observed_change_epoch_us = ?
                """,
                (
                    _iso(selected_now),
                    str(expected.record.project_id).lower(),
                    expected.stored_timestamp,
                    expected.stored_epoch_us,
                ),
            )
            return cursor.rowcount == 1


def _failure_signature(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    normalized = _FAILURE_DYNAMIC.sub("#", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _deterministic_memory_order(record: BehaviorMemoryRecord) -> tuple[object, ...]:
    return (
        -_timestamp(record.created_at),
        record.normalized_content.casefold(),
        record.content_hash,
    )


def _timestamp(value: datetime) -> float:
    return _strict_utc(value).timestamp()


def _truncate_utf8(value: str, max_chars: int, max_bytes: int) -> str:
    if len(value) <= max_chars and len(value.encode("utf-8")) <= max_bytes:
        return value
    candidate = value[: max(0, max_chars - 1)]
    while candidate and len((candidate + "…").encode("utf-8")) > max_bytes:
        candidate = candidate[:-1]
    return (candidate.rstrip() + "…") if candidate else "…"


def _strict_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _parse_strict_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _strict_utc(parsed)


def _iso(value: datetime) -> str:
    return _strict_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _bounded_count(value: int) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, _MAX_COUNT)


def _add_count(left: int, right: int) -> int:
    return min(_bounded_count(left) + _bounded_count(right), _MAX_COUNT)


def _merge_summaries(left: CompactionSummary, right: CompactionSummary) -> CompactionSummary:
    return CompactionSummary(
        project_count=_add_count(left.project_count, right.project_count),
        namespace_count=_add_count(left.namespace_count, right.namespace_count),
        source_count=_add_count(left.source_count, right.source_count),
        cold_count=_add_count(left.cold_count, right.cold_count),
        retrospective_count=_add_count(left.retrospective_count, right.retrospective_count),
        remaining_count=_add_count(left.remaining_count, right.remaining_count),
        failure_count=_add_count(left.failure_count, right.failure_count),
    )
