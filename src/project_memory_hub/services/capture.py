from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Literal
from uuid import UUID, uuid4
from weakref import WeakValueDictionary

from project_memory_hub.domain import (
    BehaviorMemoryInput,
    CapturePayload,
    CaptureResult,
    MemoryKind,
    NamespaceVerification,
    ProjectRecord,
    SourceAgent,
)
from project_memory_hub.security.identifiers import (
    safe_model_identifier,
    safe_persisted_identifier,
)
from project_memory_hub.security.capture_privacy import CapturePrivacyCanonicalizer
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.storage.checkpoints import _PriorCodexReceiptProof
from project_memory_hub.storage.database import Database, ReadonlyDatabaseSnapshot
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.path_identity import PathIdentity
from project_memory_hub.storage.projects import ProjectRepository
from project_memory_hub.storage.resolutions import IssueResolutionRepository


_MAPPINGS = (
    ("decisions", MemoryKind.DECISION),
    ("failed_attempts", MemoryKind.FAILED_ATTEMPT),
    ("verified_commands", MemoryKind.VERIFIED_METHOD),
    ("preferences", MemoryKind.PREFERENCE),
    ("risks", MemoryKind.RISK),
    ("open_issues", MemoryKind.OPEN_ISSUE),
    ("reusable_lessons", MemoryKind.REUSABLE_LESSON),
)
_PENDING_MATCH_LIMIT = 1_000
_MAX_PENDING_PER_PROJECT = 512
_MAX_PENDING_GLOBAL = 10_000
_MAX_PENDING_HISTORY = 50_000
_PENDING_HISTORY_CLEANUP_BATCH = 1_000


class _IncompatibleSourceProvenance(RuntimeError):
    """A schema-level source collision cannot be trusted as capture provenance."""


class _ProjectIdentityChanged(RuntimeError):
    """The resolved project no longer owns the registered path identity."""


class PendingCaptureCapacityError(RuntimeError):
    """The durable pending-capture quarantine reached its bounded capacity."""


class PendingCaptureHistoryCapacityError(RuntimeError):
    """Finalized pending-capture audit history could not be pruned safely."""


@dataclass(frozen=True, slots=True, weakref_slot=True)
class PreparedVerifiedCapture:
    project: ProjectRecord
    live_identity: PathIdentity
    payload: CapturePayload
    verification: NamespaceVerification
    source_record_id: str
    structured: dict[str, object]
    structured_hash: str
    mapped_rows: tuple[tuple[MemoryKind, str], ...]
    resolved_open_issues: tuple[str, ...]
    captured_at: datetime
    task_fingerprint: str


@dataclass(frozen=True, slots=True)
class _PreparedCanonicalCapture:
    structured: dict[str, object]
    canonical: str
    structured_hash: str
    mapped_rows: tuple[tuple[MemoryKind, str], ...]
    resolved_open_issues: tuple[str, ...]


class CaptureService:
    def __init__(
        self,
        database: Database | ReadonlyDatabaseSnapshot,
        projects: ProjectRepository,
        memories: MemoryRepository,
        redactor: Redactor,
        pending_ttl_days: int = 7,
        verification_window_hours: int = 24,
        now: Callable[[], datetime] | None = None,
        issue_resolutions: IssueResolutionRepository | None = None,
    ) -> None:
        if type(pending_ttl_days) is not int or pending_ttl_days <= 0:
            raise ValueError("pending_ttl_days must be a positive integer")
        if type(verification_window_hours) is not int or verification_window_hours <= 0:
            raise ValueError("verification_window_hours must be a positive integer")
        self._database = database
        self._projects = projects
        self._memories = memories
        self._redactor = redactor
        self._canonicalizer = CapturePrivacyCanonicalizer(redactor)
        self._issue_resolutions = (
            issue_resolutions if issue_resolutions is not None else IssueResolutionRepository()
        )
        self._pending_ttl_days = pending_ttl_days
        self._verification_window = timedelta(hours=verification_window_hours)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._prepared_lock = Lock()
        self._prepared_once: WeakValueDictionary[int, PreparedVerifiedCapture] = (
            WeakValueDictionary()
        )

    def capture(
        self,
        payload: CapturePayload,
        verification: NamespaceVerification | None = None,
    ) -> CaptureResult:
        if verification is not None:
            prepared = self.prepare_verified(payload, verification)
            if isinstance(prepared, CaptureResult):
                return prepared
            try:
                with self._database.transaction() as connection:
                    self._require_current_project(
                        connection,
                        prepared.project,
                        prepared.live_identity,
                    )
                    result = self.capture_prepared_on_connection(connection, prepared)
                    self._require_current_project(
                        connection,
                        prepared.project,
                        prepared.live_identity,
                    )
                    return result
            except _IncompatibleSourceProvenance:
                return CaptureResult(status="rejected")
            except _ProjectIdentityChanged:
                return CaptureResult(status="project_not_found")

        project = self._projects.find_by_cwd(payload.cwd)
        if project is None:
            return CaptureResult(status="project_not_found")
        live_identity = self._projects.record_live_identity(project)
        if live_identity is None:
            return CaptureResult(status="project_not_found")

        safe_model_identifier(payload.namespace.model_id, self._redactor)
        source_record_id = safe_persisted_identifier(
            payload.source_record_id,
            "source_record_id",
            self._redactor,
        )
        prepared_canonical = self._prepare_canonical(
            payload,
            project.canonical_path,
            stored=False,
        )
        if isinstance(prepared_canonical, CaptureResult):
            return prepared_canonical
        return self._store_pending(
            project,
            live_identity,
            payload,
            source_record_id,
            prepared_canonical.canonical,
            prepared_canonical.structured_hash,
        )

    def prepare_verified(
        self,
        payload: CapturePayload,
        verification: NamespaceVerification,
    ) -> PreparedVerifiedCapture | CaptureResult:
        verified_at = verification.verified_at
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            return CaptureResult(status="rejected")
        project = self._projects.find_by_cwd(payload.cwd)
        if project is None:
            return CaptureResult(status="project_not_found")
        live_identity = self._projects.record_live_identity(project)
        if live_identity is None:
            return CaptureResult(status="project_not_found")

        safe_model_identifier(payload.namespace.model_id, self._redactor)
        source_record_id = safe_persisted_identifier(
            payload.source_record_id,
            "source_record_id",
            self._redactor,
        )
        prepared_canonical = self._prepare_canonical(
            payload,
            project.canonical_path,
            stored=False,
        )
        if isinstance(prepared_canonical, CaptureResult):
            return prepared_canonical
        if not _verification_matches(payload, source_record_id, verification):
            return CaptureResult(status="rejected")

        captured_at = self._trusted_now()
        task_fingerprint = _task_fingerprint(
            project,
            payload,
            source_record_id,
            prepared_canonical.structured,
        )
        prepared = PreparedVerifiedCapture(
            project=project,
            live_identity=live_identity,
            payload=payload,
            verification=verification,
            source_record_id=source_record_id,
            structured=prepared_canonical.structured,
            structured_hash=prepared_canonical.structured_hash,
            mapped_rows=prepared_canonical.mapped_rows,
            resolved_open_issues=prepared_canonical.resolved_open_issues,
            captured_at=captured_at,
            task_fingerprint=task_fingerprint,
        )
        with self._prepared_lock:
            self._prepared_once[id(prepared)] = prepared
        return prepared

    def capture_prepared_on_connection(
        self,
        connection: sqlite3.Connection,
        prepared: PreparedVerifiedCapture,
        *,
        prior_codex_receipt_proof: _PriorCodexReceiptProof | None = None,
        exact_pending_id: UUID | None = None,
        defer_pending_history_cleanup: bool = False,
    ) -> CaptureResult:
        if not connection.in_transaction:
            raise ValueError("active transaction required")
        self._consume_prepared(prepared)
        self._require_current_project(
            connection,
            prepared.project,
            prepared.live_identity,
        )
        self._require_prepared_consistency(connection, prepared)
        payload = prepared.payload
        pending_id = self._matching_pending_id(
            connection,
            prepared.project.project_id,
            payload,
            prepared.structured_hash,
            prepared.verification.verified_at,
            exact_pending_id=exact_pending_id,
        )
        if exact_pending_id is not None and pending_id is None:
            raise _IncompatibleSourceProvenance
        pending_correlation_id = (
            None if pending_id is None else self._pending_correlation_id(connection, pending_id)
        )
        source_reference_id, source_reference_created = self._source_ref(
            connection,
            payload,
            prepared.verification,
            prepared.source_record_id,
            prepared.structured_hash,
            prepared.captured_at,
            prepared.project.project_id,
            payload.namespace.model_id,
            prior_codex_receipt_proof,
            pending_correlation_id=pending_correlation_id,
            bind_existing_correlation=exact_pending_id is not None,
        )
        if not source_reference_created:
            # A trusted source may predate an arbitrary later pending row. Only the
            # forensic recovery path supplies an exact pending id, so an ordinary
            # source replay must not confer verification on a newer untrusted row.
            if exact_pending_id is not None:
                assert pending_id is not None
                self._archive_verified_pending(
                    connection,
                    pending_id,
                    prepared.captured_at,
                    source_reference_id,
                    cleanup_history=not defer_pending_history_cleanup,
                )
            return CaptureResult(duplicate=True, status="duplicate")

        inserted_ids: list[UUID] = []
        for kind, content in prepared.mapped_rows:
            result = self._memories._insert_on_connection(
                connection,
                BehaviorMemoryInput(
                    project_id=prepared.project.project_id,
                    namespace=payload.namespace,
                    task_fingerprint=prepared.task_fingerprint,
                    memory_kind=kind,
                    normalized_content=content,
                    content_hash=_sha256(content),
                    source_reference_id=source_reference_id,
                    created_at=prepared.captured_at,
                    confidence=1.0,
                ),
            )
            if result.inserted:
                assert result.record_id is not None
                inserted_ids.append(result.record_id)

        resolution = self._issue_resolutions.apply_on_connection(
            connection,
            project_id=prepared.project.project_id,
            namespace=payload.namespace,
            source_reference_id=source_reference_id,
            declarations=prepared.resolved_open_issues,
            verified_at=prepared.verification.verified_at,
            resolved_at=prepared.captured_at,
        )
        if inserted_ids or resolution.resolved_count or resolution.unmatched_resolution_count:
            self._projects._advance_last_observed_change_on_connection(
                connection,
                prepared.project.project_id,
                prepared.captured_at,
                as_of=prepared.captured_at,
            )

        if pending_id is not None:
            self._archive_verified_pending(
                connection,
                pending_id,
                prepared.captured_at,
                source_reference_id,
                cleanup_history=not defer_pending_history_cleanup,
            )

        status: Literal["partial", "inserted", "resolved", "duplicate"]
        if resolution.unmatched_resolution_count:
            status = "partial"
        elif inserted_ids:
            status = "inserted"
        elif resolution.resolved_count:
            status = "resolved"
        else:
            status = "duplicate"
        return CaptureResult(
            inserted_ids=tuple(inserted_ids),
            duplicate=status == "duplicate",
            status=status,
            resolved_count=resolution.resolved_count,
            already_resolved_count=resolution.already_resolved_count,
            unmatched_resolution_count=resolution.unmatched_resolution_count,
        )

    def validate_prepared_readonly(
        self,
        prepared: PreparedVerifiedCapture,
    ) -> None:
        self._require_prepared_registered(prepared)
        with self._database.connect(readonly=True) as connection:
            self._require_current_project(
                connection,
                prepared.project,
                prepared.live_identity,
            )
            self._require_prepared_consistency(connection, prepared)
            payload = prepared.payload
            self._existing_source_ref(
                connection,
                payload,
                prepared.verification,
                prepared.source_record_id,
                prepared.structured_hash,
                prepared.project.project_id,
                payload.namespace.model_id,
            )

    def _consume_prepared(self, prepared: PreparedVerifiedCapture) -> None:
        with self._prepared_lock:
            registered = self._prepared_once.pop(id(prepared), None)
        if registered is not prepared:
            raise _IncompatibleSourceProvenance

    def _require_prepared_registered(
        self,
        prepared: PreparedVerifiedCapture,
    ) -> None:
        with self._prepared_lock:
            registered = self._prepared_once.get(id(prepared))
        if registered is not prepared:
            raise _IncompatibleSourceProvenance

    def _require_prepared_consistency(
        self,
        connection: sqlite3.Connection,
        prepared: PreparedVerifiedCapture,
    ) -> None:
        payload = prepared.payload
        verified_at = prepared.verification.verified_at
        if verified_at.tzinfo is None or verified_at.utcoffset() is None:
            raise _IncompatibleSourceProvenance
        try:
            safe_model_identifier(payload.namespace.model_id, self._redactor)
            source_record_id = safe_persisted_identifier(
                payload.source_record_id,
                "source_record_id",
                self._redactor,
            )
        except ValueError:
            raise _IncompatibleSourceProvenance from None
        if source_record_id != prepared.source_record_id or not _verification_matches(
            payload,
            prepared.source_record_id,
            prepared.verification,
        ):
            raise _IncompatibleSourceProvenance
        if not _cwd_selects_project_on_connection(
            connection,
            payload.cwd,
            prepared.project,
        ):
            raise _ProjectIdentityChanged
        try:
            canonical = self._prepare_canonical(
                payload,
                prepared.project.canonical_path,
                stored=False,
            )
        except ValueError:
            raise _IncompatibleSourceProvenance from None
        if (
            isinstance(canonical, CaptureResult)
            or canonical.structured != prepared.structured
            or canonical.structured_hash != prepared.structured_hash
            or canonical.mapped_rows != prepared.mapped_rows
            or canonical.resolved_open_issues != prepared.resolved_open_issues
            or _task_fingerprint(
                prepared.project,
                payload,
                prepared.source_record_id,
                canonical.structured,
            )
            != prepared.task_fingerprint
        ):
            raise _IncompatibleSourceProvenance

    def _matching_pending_id(
        self,
        connection: sqlite3.Connection,
        project_id: UUID,
        payload: CapturePayload,
        structured_hash: str,
        verified_at: datetime,
        *,
        exact_pending_id: UUID | None = None,
    ) -> str | None:
        verified_utc = _utc_datetime(verified_at)
        coarse_window_days = (
            self._verification_window + timedelta(hours=1)
        ).total_seconds() / 86_400
        exact_clause = ""
        parameters: list[object] = [
            str(project_id).lower(),
            payload.namespace.source_agent.value,
            payload.namespace.model_id,
            structured_hash,
        ]
        if exact_pending_id is not None:
            exact_clause = " and pending_id = ?"
            parameters.append(str(exact_pending_id).lower())
        parameters.extend(
            (
                _utc_iso(verified_utc),
                coarse_window_days,
                _utc_iso(verified_utc),
                coarse_window_days,
                _PENDING_MATCH_LIMIT + 1,
            )
        )
        rows = connection.execute(
            f"""
            select pending_id, created_at from pending_captures
            where project_id = ? and claimed_source_agent = ?
              and claimed_model_id = ? and structured_hash = ?
              and verification_state = 'pending'
              {exact_clause}
              and julianday(created_at) >= julianday(?) - ?
              and julianday(created_at) <= julianday(?) + ?
            order by julianday(created_at), pending_id
            limit ?
            """,
            tuple(parameters),
        ).fetchall()
        if len(rows) > _PENDING_MATCH_LIMIT:
            return None
        matches: list[tuple[datetime, str]] = []
        for row in rows:
            try:
                created_at = _parse_timestamp(row["created_at"])
            except (TypeError, ValueError):
                continue
            difference = created_at - verified_utc
            if -self._verification_window <= difference <= self._verification_window:
                matches.append((created_at, row["pending_id"]))
        if not matches:
            return None
        return min(matches, key=lambda item: (item[0], item[1]))[1]

    @staticmethod
    def _pending_correlation_id(
        connection: sqlite3.Connection,
        pending_id: str,
    ) -> str:
        row = connection.execute(
            "select source_record_id from pending_captures where pending_id = ?",
            (pending_id,),
        ).fetchone()
        if row is None or not isinstance(row["source_record_id"], str):
            raise _IncompatibleSourceProvenance
        return row["source_record_id"]

    @staticmethod
    def _archive_verified_pending(
        connection: sqlite3.Connection,
        pending_id: str,
        finalized_at: datetime,
        source_reference_id: UUID,
        *,
        cleanup_history: bool = True,
    ) -> None:
        archived = _archive_pending_capture_on_connection(
            connection,
            pending_id,
            final_state="verified",
            finalized_at=finalized_at,
            source_reference_id=source_reference_id,
        )
        if not archived:
            raise _IncompatibleSourceProvenance
        if cleanup_history:
            _cleanup_pending_capture_history_on_connection(connection)

    @staticmethod
    def _mapped_rows(
        structured: dict[str, object],
    ) -> list[tuple[MemoryKind, str]]:
        rows: list[tuple[MemoryKind, str]] = []
        for field_name, memory_kind in _MAPPINGS:
            values = structured[field_name]
            assert isinstance(values, list)
            rows.extend((memory_kind, value) for value in values if value)
        outcome = structured["outcome"]
        assert isinstance(outcome, str)
        if outcome:
            rows.append((MemoryKind.OUTCOME, outcome))
        return rows

    def _prepare_canonical(
        self,
        payload: CapturePayload,
        project_path: Path,
        *,
        stored: bool,
    ) -> _PreparedCanonicalCapture | CaptureResult:
        structured = (
            self._canonicalizer.stored_structure(payload, project_path)
            if stored
            else self._canonicalizer.structure(payload, project_path)
        )
        raw_resolutions = structured.get("resolved_open_issues", [])
        assert isinstance(raw_resolutions, list)
        if any(not resolution for resolution in raw_resolutions):
            return CaptureResult(status="rejected")

        seen_resolutions: set[str] = set()
        normalized_resolutions: list[str] = []
        for resolution in raw_resolutions:
            assert isinstance(resolution, str)
            if resolution in seen_resolutions:
                continue
            seen_resolutions.add(resolution)
            normalized_resolutions.append(resolution)
        if normalized_resolutions:
            structured["resolved_open_issues"] = normalized_resolutions
        else:
            structured.pop("resolved_open_issues", None)

        open_issues = structured["open_issues"]
        assert isinstance(open_issues, list)
        if set(open_issues).intersection(seen_resolutions):
            return CaptureResult(status="rejected")

        mapped_rows = tuple(self._mapped_rows(structured))
        resolved_open_issues = tuple(normalized_resolutions)
        if not mapped_rows and not resolved_open_issues:
            return CaptureResult(status="rejected")
        canonical = json.dumps(
            structured,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return _PreparedCanonicalCapture(
            structured=structured,
            canonical=canonical,
            structured_hash=_sha256(canonical),
            mapped_rows=mapped_rows,
            resolved_open_issues=resolved_open_issues,
        )

    def _store_pending(
        self,
        project: ProjectRecord,
        live_identity: PathIdentity,
        payload: CapturePayload,
        source_record_id: str,
        canonical: str,
        structured_hash: str,
    ) -> CaptureResult:
        now = self._trusted_now()
        try:
            with self._database.transaction() as connection:
                self._require_current_project(connection, project, live_identity)
                result = self._store_pending_on_connection(
                    connection,
                    project.project_id,
                    payload,
                    source_record_id,
                    canonical,
                    structured_hash,
                    now,
                )
                self._require_current_project(connection, project, live_identity)
                return result
        except _ProjectIdentityChanged:
            return CaptureResult(status="project_not_found")

    def _require_current_project(
        self,
        connection: sqlite3.Connection,
        project: ProjectRecord,
        expected_live_identity: PathIdentity,
    ) -> None:
        live_identity = self._projects._record_live_identity_on_connection(
            connection,
            project,
        )
        if live_identity != expected_live_identity:
            raise _ProjectIdentityChanged

    def _capture_untrusted_on_connection(
        self,
        connection: sqlite3.Connection,
        payload: CapturePayload,
        project_id: UUID,
    ) -> CaptureResult:
        project_id_text = str(project_id).lower()
        project = connection.execute(
            "select canonical_path from projects where project_id = ? and enabled = 1",
            (project_id_text,),
        ).fetchone()
        if project is None:
            return CaptureResult(status="project_not_found")
        safe_model_identifier(payload.namespace.model_id, self._redactor)
        source_record_id = safe_persisted_identifier(
            payload.source_record_id,
            "source_record_id",
            self._redactor,
        )
        prepared_canonical = self._prepare_canonical(
            payload,
            Path(project["canonical_path"]),
            stored=True,
        )
        if isinstance(prepared_canonical, CaptureResult):
            return prepared_canonical
        return self._store_pending_on_connection(
            connection,
            project_id,
            payload,
            source_record_id,
            prepared_canonical.canonical,
            prepared_canonical.structured_hash,
            self._trusted_now(),
        )

    def _trusted_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("timestamp must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _store_pending_on_connection(
        self,
        connection: sqlite3.Connection,
        project_id: UUID,
        payload: CapturePayload,
        source_record_id: str,
        canonical: str,
        structured_hash: str,
        now: datetime,
    ) -> CaptureResult:
        project_id_text = str(project_id).lower()
        existing = connection.execute(
            """
            select 1 from pending_captures
            where project_id = ? and claimed_source_agent = ?
              and claimed_model_id = ? and source_record_id = ?
              and structured_hash = ?
            limit 1
            """,
            (
                project_id_text,
                payload.namespace.source_agent.value,
                payload.namespace.model_id,
                source_record_id,
                structured_hash,
            ),
        ).fetchone()
        if existing is None:
            existing = connection.execute(
                """
                select 1 from pending_capture_history
                where project_id = ? and claimed_source_agent = ?
                  and claimed_model_id = ? and source_record_id = ?
                  and structured_hash = ?
                limit 1
                """,
                (
                    project_id_text,
                    payload.namespace.source_agent.value,
                    payload.namespace.model_id,
                    source_record_id,
                    structured_hash,
                ),
            ).fetchone()
        if existing is None:
            # History is bounded, so use the separately bound local correlation.
            # The trusted source record id can be a different session:turn value.
            existing = connection.execute(
                """
                select 1 from source_refs
                where source_agent = ? and capture_correlation_id = ?
                  and content_hash = ? and parser_version = 'capture-v1'
                  and source_path is null and capture_project_id = ?
                  and capture_model_id = ?
                limit 1
                """,
                (
                    payload.namespace.source_agent.value,
                    source_record_id,
                    structured_hash,
                    project_id_text,
                    payload.namespace.model_id,
                ),
            ).fetchone()
        if existing is not None:
            return CaptureResult(duplicate=True, status="pending_verification")
        project_count = connection.execute(
            """
            select count(*) from pending_captures
            where project_id = ? and verification_state = 'pending'
            """,
            (project_id_text,),
        ).fetchone()[0]
        global_count = connection.execute(
            "select count(*) from pending_captures where verification_state = 'pending'"
        ).fetchone()[0]
        if project_count >= _MAX_PENDING_PER_PROJECT or global_count >= _MAX_PENDING_GLOBAL:
            raise PendingCaptureCapacityError("pending capture capacity exceeded")
        cursor = connection.execute(
            """
            insert or ignore into pending_captures(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_payload_json, structured_hash,
                created_at, expires_at, verification_state
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                str(uuid4()).lower(),
                project_id_text,
                payload.namespace.source_agent.value,
                payload.namespace.model_id,
                source_record_id,
                canonical,
                structured_hash,
                _utc_iso(now),
                _utc_iso(now + timedelta(days=self._pending_ttl_days)),
            ),
        )
        return CaptureResult(
            duplicate=cursor.rowcount == 0,
            status="pending_verification",
        )

    @staticmethod
    def _existing_source_ref(
        connection: sqlite3.Connection,
        payload: CapturePayload,
        verification: NamespaceVerification,
        source_record_id: str,
        structured_hash: str,
        project_id: UUID,
        model_id: str,
        prior_codex_receipt_proof: _PriorCodexReceiptProof | None = None,
    ) -> UUID | None:
        if prior_codex_receipt_proof is not None:
            if type(prior_codex_receipt_proof) is not _PriorCodexReceiptProof or not (
                prior_codex_receipt_proof.matches(
                    connection,
                    payload.namespace.source_agent,
                    source_record_id,
                )
            ):
                raise _IncompatibleSourceProvenance
        rows = connection.execute(
            """
            select source_reference_id, source_agent, content_hash,
                   parser_version, source_path,
                   capture_project_id, capture_model_id,
                   strict_utc_epoch_us(source_timestamp) as source_timestamp_epoch,
                   strict_utc_epoch_us(?) as verification_timestamp_epoch
            from source_refs
            where source_agent = ? and source_record_id = ?
            order by source_reference_id
            limit 2
            """,
            (
                _utc_iso(verification.verified_at),
                payload.namespace.source_agent.value,
                source_record_id,
            ),
        ).fetchall()
        if len(rows) > 1:
            raise _IncompatibleSourceProvenance
        if not rows:
            if prior_codex_receipt_proof is not None:
                raise _IncompatibleSourceProvenance
            return None
        row = rows[0]
        source_timestamp_epoch = row["source_timestamp_epoch"]
        verification_timestamp_epoch = row["verification_timestamp_epoch"]
        timestamp_matches = (
            source_timestamp_epoch is not None
            and verification_timestamp_epoch is not None
            and source_timestamp_epoch == verification_timestamp_epoch
        )
        timestamp_is_proven_forward_replay = (
            prior_codex_receipt_proof is not None
            and source_timestamp_epoch is not None
            and verification_timestamp_epoch is not None
            and verification_timestamp_epoch > source_timestamp_epoch
        )
        if (
            row["source_agent"] != payload.namespace.source_agent.value
            or row["content_hash"] != structured_hash
            or row["parser_version"] != "capture-v1"
            or row["source_path"] is not None
            or row["capture_project_id"] != str(project_id).lower()
            or row["capture_model_id"] != model_id
            or not (timestamp_matches or timestamp_is_proven_forward_replay)
        ):
            raise _IncompatibleSourceProvenance
        persisted_id = row["source_reference_id"]
        if not isinstance(persisted_id, str):
            raise _IncompatibleSourceProvenance
        try:
            source_reference_id = UUID(persisted_id)
        except (AttributeError, TypeError, ValueError):
            raise _IncompatibleSourceProvenance from None
        if str(source_reference_id).lower() != persisted_id:
            raise _IncompatibleSourceProvenance
        return source_reference_id

    @staticmethod
    def _source_ref(
        connection: sqlite3.Connection,
        payload: CapturePayload,
        verification: NamespaceVerification,
        source_record_id: str,
        structured_hash: str,
        captured_at: datetime,
        project_id: UUID,
        model_id: str,
        prior_codex_receipt_proof: _PriorCodexReceiptProof | None = None,
        *,
        pending_correlation_id: str | None = None,
        bind_existing_correlation: bool = False,
    ) -> tuple[UUID, bool]:
        existing_source_reference_id = CaptureService._existing_source_ref(
            connection,
            payload,
            verification,
            source_record_id,
            structured_hash,
            project_id,
            model_id,
            prior_codex_receipt_proof,
        )
        if existing_source_reference_id is not None:
            if bind_existing_correlation and pending_correlation_id is not None:
                CaptureService._bind_source_ref_correlation(
                    connection,
                    existing_source_reference_id,
                    pending_correlation_id,
                )
            return existing_source_reference_id, False
        source_reference_id = uuid4()
        try:
            connection.execute(
                """
                insert into source_refs(
                    source_reference_id, source_agent, source_record_id, source_path,
                    content_hash, source_timestamp, parser_version, created_at,
                    capture_project_id, capture_model_id, capture_correlation_id
                ) values (?, ?, ?, null, ?, ?, 'capture-v1', ?, ?, ?, ?)
                """,
                (
                    str(source_reference_id).lower(),
                    payload.namespace.source_agent.value,
                    source_record_id,
                    structured_hash,
                    _utc_iso(verification.verified_at),
                    _utc_iso(captured_at),
                    str(project_id).lower(),
                    model_id,
                    pending_correlation_id,
                ),
            )
        except sqlite3.IntegrityError:
            raise _IncompatibleSourceProvenance from None
        return source_reference_id, True

    @staticmethod
    def _bind_source_ref_correlation(
        connection: sqlite3.Connection,
        source_reference_id: UUID,
        correlation_id: str,
    ) -> None:
        source_reference = str(source_reference_id).lower()
        row = connection.execute(
            """
            select capture_correlation_id from source_refs
            where source_reference_id = ?
            """,
            (source_reference,),
        ).fetchone()
        if row is None:
            raise _IncompatibleSourceProvenance
        existing = row["capture_correlation_id"]
        if existing is not None:
            if existing != correlation_id:
                raise _IncompatibleSourceProvenance
            return
        try:
            updated = connection.execute(
                """
                update source_refs set capture_correlation_id = ?
                where source_reference_id = ? and capture_correlation_id is null
                """,
                (correlation_id, source_reference),
            ).rowcount
        except sqlite3.IntegrityError:
            raise _IncompatibleSourceProvenance from None
        if updated != 1:
            raise _IncompatibleSourceProvenance


def _archive_pending_capture_on_connection(
    connection: sqlite3.Connection,
    pending_id: str,
    *,
    final_state: Literal["verified", "expired", "rejected"],
    finalized_at: datetime,
    source_reference_id: UUID | None = None,
) -> bool:
    if not connection.in_transaction:
        raise ValueError("active transaction required")
    if final_state not in {"verified", "expired", "rejected"}:
        raise ValueError("invalid pending final state")
    source_reference = None if source_reference_id is None else str(source_reference_id).lower()
    inserted = connection.execute(
        """
        insert into pending_capture_history(
            pending_id, project_id, claimed_source_agent, claimed_model_id,
            source_record_id, structured_hash, created_at, expires_at,
            finalized_at, final_state, source_reference_id
        )
        select
            pending_id, project_id, claimed_source_agent, claimed_model_id,
            source_record_id, structured_hash, created_at, expires_at,
            ?, ?, ?
        from pending_captures
        where pending_id = ? and verification_state = 'pending'
        """,
        (
            _utc_iso(finalized_at),
            final_state,
            source_reference,
            pending_id,
        ),
    ).rowcount
    if inserted == 0:
        return False
    if inserted != 1:
        raise RuntimeError("pending capture finalization was ambiguous")
    deleted = connection.execute(
        """
        delete from pending_captures
        where pending_id = ? and verification_state = 'pending'
        """,
        (pending_id,),
    ).rowcount
    if deleted != 1:
        raise RuntimeError("pending capture finalization was not atomic")
    return True


def _cleanup_pending_capture_history_on_connection(
    connection: sqlite3.Connection,
) -> int:
    if not connection.in_transaction:
        raise ValueError("active transaction required")
    history_count = int(
        connection.execute("select count(*) from pending_capture_history").fetchone()[0]
    )
    excess = max(0, history_count - _MAX_PENDING_HISTORY)
    if excess == 0:
        return 0
    if excess > _PENDING_HISTORY_CLEANUP_BATCH:
        raise PendingCaptureHistoryCapacityError("pending capture history cleanup batch exceeded")
    deleted = connection.execute(
        """
        delete from pending_capture_history
        where pending_id in (
            select pending_id from pending_capture_history
            order by strict_utc_epoch_us(finalized_at), finalized_at, pending_id
            limit ?
        )
        """,
        (excess,),
    ).rowcount
    if deleted != excess:
        raise PendingCaptureHistoryCapacityError("pending capture history cleanup was incomplete")
    return deleted


def _task_fingerprint(
    project: ProjectRecord,
    payload: CapturePayload,
    source_record_id: str,
    structured: dict[str, object],
) -> str:
    objective = structured["objective"]
    assert isinstance(objective, str)
    return _sha256(
        json.dumps(
            [
                str(project.project_id).lower(),
                payload.namespace.source_agent.value,
                payload.namespace.model_id,
                source_record_id,
                objective,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )


def _cwd_selects_project_on_connection(
    connection: sqlite3.Connection,
    cwd: Path,
    project: ProjectRecord,
) -> bool:
    lexical = Path(cwd)
    lexical_text = str(lexical)
    if not lexical.is_absolute() or os.path.normpath(lexical_text) != lexical_text:
        return False
    try:
        resolved_cwd = lexical.resolve(strict=True)
    except (OSError, RuntimeError):
        return False
    if not resolved_cwd.is_dir():
        return False
    rows = connection.execute(
        """
        select project_id, canonical_path, enabled
        from projects order by canonical_path, project_id
        """
    ).fetchall()
    matches: list[tuple[int, str, str, bool]] = []
    for row in rows:
        canonical_path = str(row["canonical_path"])
        project_id = str(row["project_id"])
        try:
            resolved_root = Path(canonical_path).resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not resolved_root.is_dir():
            continue
        if resolved_cwd == resolved_root or resolved_root in resolved_cwd.parents:
            matches.append(
                (
                    len(resolved_root.parts),
                    canonical_path,
                    project_id,
                    bool(row["enabled"]),
                )
            )
    if not matches:
        return False
    _depth, _path, selected_project_id, enabled = max(
        matches,
        key=lambda item: (item[0], item[1], item[2]),
    )
    return enabled and selected_project_id == str(project.project_id).lower()


def _verification_matches(
    payload: CapturePayload,
    source_record_id: str,
    verification: NamespaceVerification,
) -> bool:
    expected_adapter = {
        SourceAgent.CODEX: "codex_adapter",
        SourceAgent.CHATGPT: "chatgpt_adapter",
    }.get(payload.namespace.source_agent)
    return (
        verification.namespace == payload.namespace
        and verification.source_record_id == source_record_id
        and verification.verified_by == expected_adapter
    )


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_iso(value: datetime) -> str:
    return _utc_datetime(value).isoformat().replace("+00:00", "Z")


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)
