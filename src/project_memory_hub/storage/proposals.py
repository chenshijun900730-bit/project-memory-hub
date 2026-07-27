from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import ValidationError

from project_memory_hub.improvement.models import (
    ApplyResult,
    CreatableProposalOrigin,
    ProposalCreateResult,
    ProposalDraft,
    ProposalOrigin,
    ProposalRecord,
    ProposalRisk,
    ProposalStatus,
    ProposalSummary,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.storage.database import Database, ReadonlyDatabaseSnapshot

__all__ = [
    "ApplyResult",
    "CorruptProposalRecord",
    "CreatableProposalOrigin",
    "InvalidProposalOrigin",
    "InvalidProposalTransition",
    "ProposalCreateResult",
    "ProposalDraft",
    "ProposalError",
    "ProposalOrigin",
    "ProposalRecord",
    "ProposalRepository",
    "ProposalRisk",
    "ProposalStatus",
    "ProposalSummary",
    "UnsafeProposalPatch",
]

_CAPABLE_ORIGINS = frozenset({"local_cli", "codex_task", "control_panel"})
_ACTIVE_STATUSES = ("draft", "approved", "applying")
_SIGNATURE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_FAILURE_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_COMMIT = re.compile(r"[0-9a-f]{40,64}\Z")
_MAX_PATCH_BYTES = 256 * 1024
_MAX_TITLE_CHARS = 200
_MAX_DESCRIPTION_CHARS = 2_000
_MAX_SUMMARY_CHARS = 2_000
_MAX_RAW_METADATA_BYTES = 32 * 1024
_MAX_ARG_COUNT = 64
_MAX_ARG_BYTES = 1_024
_MAX_ARGV_BYTES = 16 * 1024
_MAX_PATH_BYTES = 8 * 1024
_MAX_REF_BYTES = 1_024
_MAX_LIST = 1_000


class ProposalError(ValueError):
    """Base class for stable proposal validation failures."""


class UnsafeProposalPatch(ProposalError):
    """The patch is too large, malformed, or contains secret material."""


class InvalidProposalOrigin(ProposalError):
    """The proposal origin lacks the requested capability."""


class InvalidProposalTransition(ProposalError):
    """The requested proposal state transition is not allowed."""


class CorruptProposalRecord(ProposalError):
    """Persisted proposal metadata failed closed validation."""


class ProposalRepository:
    def __init__(
        self,
        database: Database | ReadonlyDatabaseSnapshot,
        redactor: Redactor | None = None,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._redactor = redactor or Redactor()
        self._now: Callable[[], datetime] = now or (lambda: datetime.now(timezone.utc))

    def preview_create(
        self,
        draft: ProposalDraft,
    ) -> tuple[ProposalDraft, ProposalRecord | None]:
        """Validate, normalize, and classify active dedupe without writing."""
        prepared = self._prepare_draft(draft)
        with self._database.connect(readonly=True) as connection:
            row = connection.execute(
                """
                select * from improvement_proposals
                where signature = ?
                  and approval_status in ('draft', 'approved', 'applying')
                order by created_at, proposal_id
                limit 1
                """,
                (prepared.signature,),
            ).fetchone()
        if row is None:
            return prepared, None
        record = self._record_from_row(row)
        if record.origin != prepared.origin:
            raise ProposalError("active proposal origin conflict")
        return prepared, record

    def preview_approve(self, proposal_id: UUID) -> ProposalRecord:
        """Validate an approval transition without changing persisted state."""
        record = self.get(_uuid(proposal_id, "proposal_id"))
        self._require_capable_origin(record, "approve")
        self._require_state(record, "draft", "approved")
        self._transition_timestamp(record)
        return record

    def preview_reject(self, proposal_id: UUID) -> ProposalRecord:
        """Validate a rejection transition without changing persisted state."""
        record = self.get(_uuid(proposal_id, "proposal_id"))
        if record.status not in {"draft", "approved"}:
            raise InvalidProposalTransition(f"invalid transition {record.status} -> rejected")
        self._transition_timestamp(record)
        return record

    def preview_apply(self, proposal_id: UUID) -> ProposalRecord:
        """Validate the persisted side of apply or recovery without writing."""
        record = self.get(_uuid(proposal_id, "proposal_id"))
        self._require_capable_origin(record, "apply")
        if record.status not in {"approved", "applying"}:
            raise InvalidProposalTransition(f"invalid transition {record.status} -> applying")
        self._require_executable(record)
        self._transition_timestamp(record)
        return record

    def preview_rollback(self, proposal_id: UUID) -> ProposalRecord:
        """Validate the persisted rollback transition without writing."""
        record = self.get(_uuid(proposal_id, "proposal_id"))
        self._require_capable_origin(record, "apply")
        self._require_state(record, "applied", "rolled_back")
        self._require_executable(record)
        self._transition_timestamp(record)
        return record

    def create(self, draft: ProposalDraft) -> ProposalCreateResult:
        prepared = self._prepare_draft(draft)
        proposal_id = uuid4()
        timestamp = _utc_iso(self._utc_now())
        values = (
            str(proposal_id),
            prepared.signature,
            prepared.title,
            prepared.description,
            prepared.patch,
            prepared.risk,
            _argv_document(prepared.verification_argv),
            "",
            "draft",
            prepared.target_version,
            None,
            timestamp,
            None,
            prepared.origin,
            None,
            timestamp,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
        )
        with self._database.transaction() as connection:
            try:
                connection.execute(
                    """
                    insert into improvement_proposals(
                        proposal_id, signature, title, description, patch, risk,
                        verification_argv_json, verification_summary,
                        approval_status, target_version, rollback_ref, created_at,
                        approved_at, origin, approval_actor, updated_at,
                        apply_attempt_id, repository_root, original_branch,
                        base_commit, proposal_branch, applied_commit, applied_at,
                        rolled_back_at, failure_code
                    ) values (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    values,
                )
            except sqlite3.IntegrityError:
                # Classify the partial active-signature conflict while the same
                # BEGIN IMMEDIATE lock is still held. Releasing the lock before
                # reading the winner would let it transition out of the active
                # set and turn a legitimate dedupe into a spurious failure.
                row = connection.execute(
                    """
                    select * from improvement_proposals
                    where signature = ?
                      and approval_status in ('draft', 'approved', 'applying')
                    order by created_at, proposal_id
                    limit 1
                    """,
                    (prepared.signature,),
                ).fetchone()
                if row is None:
                    raise ProposalError("proposal insert rejected") from None
                record = self._record_from_row(row)
                if record.origin != prepared.origin:
                    raise ProposalError("active proposal origin conflict") from None
                return ProposalCreateResult(
                    inserted=False,
                    duplicate=True,
                    record=record,
                )
            row = self._row_on_connection(connection, proposal_id)
            return ProposalCreateResult(
                inserted=True,
                duplicate=False,
                record=self._record_from_row(row),
            )

    def get(self, proposal_id: UUID) -> ProposalRecord:
        selected = _uuid(proposal_id, "proposal_id")
        with self._database.connect(readonly=True) as connection:
            row = connection.execute(
                "select * from improvement_proposals where proposal_id = ?",
                (str(selected),),
            ).fetchone()
        if row is None:
            raise KeyError(selected)
        return self._record_from_row(row)

    def list_summaries(self, *, limit: int = 100) -> tuple[ProposalSummary, ...]:
        if type(limit) is not int or not 1 <= limit <= _MAX_LIST:
            raise ValueError("proposal list limit is invalid")
        with self._database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select * from improvement_proposals
                order by created_at desc, proposal_id
                limit ?
                """,
                (limit,),
            ).fetchall()
        summaries: list[ProposalSummary] = []
        for row in rows:
            try:
                record = self._record_from_row(row)
            except (CorruptProposalRecord, UnsafeProposalPatch):
                continue
            summaries.append(
                ProposalSummary(
                    proposal_id=record.proposal_id,
                    signature=record.signature,
                    title=record.title,
                    description=record.description,
                    risk=record.risk,
                    status=record.status,
                    origin=record.origin,
                    created_at=record.created_at,
                    updated_at=record.updated_at,
                )
            )
        return tuple(summaries)

    def approve(self, proposal_id: UUID, *, actor: str) -> ProposalRecord:
        selected = _uuid(proposal_id, "proposal_id")
        safe_actor = self._exact_text(actor, "approval_actor", 200)
        with self._database.transaction() as connection:
            record = self._load_valid_on_connection(connection, selected)
            self._require_capable_origin(record, "approve")
            self._require_state(record, "draft", "approved")
            timestamp = self._transition_timestamp(record)
            cursor = connection.execute(
                """
                update improvement_proposals
                set approval_status = 'approved', approval_actor = ?,
                    approved_at = ?, updated_at = ?
                where proposal_id = ? and approval_status = 'draft'
                """,
                (safe_actor, timestamp, timestamp, str(selected)),
            )
            self._require_updated(cursor, "draft", "approved")
            return self._record_from_row(self._row_on_connection(connection, selected))

    def reject(
        self,
        proposal_id: UUID,
        *,
        expected_status: Literal["draft", "approved"],
    ) -> ProposalRecord:
        selected = _uuid(proposal_id, "proposal_id")
        if expected_status not in {"draft", "approved"}:
            raise ValueError("expected proposal status is invalid")
        with self._database.transaction() as connection:
            record = self._load_valid_on_connection(connection, selected)
            if record.status not in {"draft", "approved"}:
                raise InvalidProposalTransition(f"invalid transition {record.status} -> rejected")
            if record.status != expected_status:
                raise InvalidProposalTransition(f"invalid transition {record.status} -> rejected")
            timestamp = self._transition_timestamp(record)
            cursor = connection.execute(
                """
                update improvement_proposals
                set approval_status = 'rejected', updated_at = ?
                where proposal_id = ? and approval_status = ?
                """,
                (timestamp, str(selected), expected_status),
            )
            self._require_updated(cursor, expected_status, "rejected")
            return self._record_from_row(self._row_on_connection(connection, selected))

    def begin_apply(
        self,
        proposal_id: UUID,
        *,
        apply_attempt_id: UUID,
        repository_root: Path,
        original_branch: str,
        base_commit: str,
        proposal_branch: str,
    ) -> ProposalRecord:
        selected = _uuid(proposal_id, "proposal_id")
        attempt = _uuid(apply_attempt_id, "apply_attempt_id")
        root = _absolute_path(repository_root)
        original = _git_ref(original_branch, "original_branch")
        base = _commit(base_commit, "base_commit")
        branch = _git_ref(proposal_branch, "proposal_branch")
        with self._database.transaction() as connection:
            record = self._load_valid_on_connection(connection, selected)
            self._require_capable_origin(record, "apply")
            self._require_state(record, "approved", "applying")
            self._require_executable(record)
            timestamp = self._transition_timestamp(record)
            cursor = connection.execute(
                """
                update improvement_proposals
                set approval_status = 'applying', apply_attempt_id = ?,
                    repository_root = ?, original_branch = ?, base_commit = ?,
                    proposal_branch = ?, updated_at = ?
                where proposal_id = ? and approval_status = 'approved'
                """,
                (
                    str(attempt),
                    str(root),
                    original,
                    base,
                    branch,
                    timestamp,
                    str(selected),
                ),
            )
            self._require_updated(cursor, "approved", "applying")
            return self._record_from_row(self._row_on_connection(connection, selected))

    def mark_applied(
        self,
        proposal_id: UUID,
        *,
        apply_attempt_id: UUID,
        applied_commit: str,
        verification_summary: str,
    ) -> ProposalRecord:
        selected = _uuid(proposal_id, "proposal_id")
        attempt = _uuid(apply_attempt_id, "apply_attempt_id")
        applied = _commit(applied_commit, "applied_commit")
        summary = self._display_text(
            verification_summary,
            "verification_summary",
            _MAX_SUMMARY_CHARS,
            allow_blank=True,
        )
        with self._database.transaction() as connection:
            record = self._load_valid_on_connection(connection, selected)
            self._require_capable_origin(record, "apply")
            self._require_state(record, "applying", "applied")
            self._require_executable(record)
            if record.apply_attempt_id != attempt:
                raise InvalidProposalTransition("apply attempt mismatch")
            timestamp = self._transition_timestamp(record)
            cursor = connection.execute(
                """
                update improvement_proposals
                set approval_status = 'applied', applied_commit = ?,
                    applied_at = ?, verification_summary = ?, updated_at = ?
                where proposal_id = ? and approval_status = 'applying'
                  and apply_attempt_id = ?
                """,
                (applied, timestamp, summary, timestamp, str(selected), str(attempt)),
            )
            self._require_updated(cursor, "applying", "applied")
            return self._record_from_row(self._row_on_connection(connection, selected))

    def mark_failed(
        self,
        proposal_id: UUID,
        *,
        apply_attempt_id: UUID,
        failure_code: str,
        verification_summary: str,
    ) -> ProposalRecord:
        selected = _uuid(proposal_id, "proposal_id")
        attempt = _uuid(apply_attempt_id, "apply_attempt_id")
        if not isinstance(failure_code, str) or _FAILURE_CODE.fullmatch(failure_code) is None:
            raise ValueError("failure_code is invalid")
        summary = self._display_text(
            verification_summary,
            "verification_summary",
            _MAX_SUMMARY_CHARS,
            allow_blank=True,
        )
        with self._database.transaction() as connection:
            record = self._load_valid_on_connection(connection, selected)
            self._require_capable_origin(record, "apply")
            self._require_state(record, "applying", "failed")
            self._require_executable(record)
            if record.apply_attempt_id != attempt:
                raise InvalidProposalTransition("apply attempt mismatch")
            timestamp = self._transition_timestamp(record)
            cursor = connection.execute(
                """
                update improvement_proposals
                set approval_status = 'failed', failure_code = ?,
                    verification_summary = ?, updated_at = ?
                where proposal_id = ? and approval_status = 'applying'
                  and apply_attempt_id = ?
                """,
                (
                    failure_code,
                    summary,
                    timestamp,
                    str(selected),
                    str(attempt),
                ),
            )
            self._require_updated(cursor, "applying", "failed")
            return self._record_from_row(self._row_on_connection(connection, selected))

    def mark_rolled_back(self, proposal_id: UUID) -> ProposalRecord:
        selected = _uuid(proposal_id, "proposal_id")
        with self._database.transaction() as connection:
            record = self._load_valid_on_connection(connection, selected)
            self._require_capable_origin(record, "apply")
            self._require_state(record, "applied", "rolled_back")
            self._require_executable(record)
            timestamp = self._transition_timestamp(record)
            cursor = connection.execute(
                """
                update improvement_proposals
                set approval_status = 'rolled_back', rolled_back_at = ?,
                    updated_at = ?
                where proposal_id = ? and approval_status = 'applied'
                """,
                (timestamp, timestamp, str(selected)),
            )
            self._require_updated(cursor, "applied", "rolled_back")
            return self._record_from_row(self._row_on_connection(connection, selected))

    def _prepare_draft(self, draft: ProposalDraft) -> ProposalDraft:
        if not isinstance(draft, ProposalDraft):
            raise TypeError("proposal draft is required")
        patch = self._safe_patch(draft.patch)
        origin = draft.origin
        if origin not in {"local_cli", "codex_task", "control_panel", "analyzer"}:
            raise InvalidProposalOrigin("proposal origin cannot create drafts")
        argv = self._safe_argv(draft.verification_argv)
        target_version = self._optional_exact_text(draft.target_version, "target_version", 128)
        if origin == "analyzer":
            if patch is not None or argv or target_version is not None:
                raise InvalidProposalOrigin("analyzer proposals cannot carry execution capability")
        elif patch is not None and not argv:
            raise ProposalError("proposal verification command is required")
        signature = self._signature(draft.signature)
        title = self._display_text(draft.title, "title", _MAX_TITLE_CHARS, allow_blank=False)
        description = self._display_text(
            draft.description,
            "description",
            _MAX_DESCRIPTION_CHARS,
            allow_blank=False,
        )
        if draft.risk not in {"low", "medium", "high"}:
            raise ProposalError("proposal risk is invalid")
        try:
            return ProposalDraft(
                signature=signature,
                title=title,
                description=description,
                risk=draft.risk,
                patch=patch,
                verification_argv=argv,
                target_version=target_version,
                origin=origin,
            )
        except ValidationError:
            raise ProposalError("proposal draft is invalid") from None

    def _record_from_row(self, row: sqlite3.Row) -> ProposalRecord:
        try:
            proposal_id = _canonical_stored_uuid(row["proposal_id"])
            signature = self._signature(row["signature"])
            title = self._stored_display_text(row["title"], "title", _MAX_TITLE_CHARS)
            description = self._stored_display_text(
                row["description"], "description", _MAX_DESCRIPTION_CHARS
            )
            patch = self._safe_patch(row["patch"])
            risk = row["risk"]
            if risk not in {"low", "medium", "high"}:
                raise CorruptProposalRecord("proposal record is invalid")
            argv = self._stored_argv(row["verification_argv_json"])
            summary = self._stored_display_text(
                row["verification_summary"],
                "verification_summary",
                _MAX_SUMMARY_CHARS,
                allow_blank=True,
            )
            status = row["approval_status"]
            if status not in {
                "draft",
                "approved",
                "applying",
                "applied",
                "rejected",
                "failed",
                "rolled_back",
            }:
                raise CorruptProposalRecord("proposal record is invalid")
            origin = row["origin"]
            if origin not in {
                "legacy",
                "local_cli",
                "codex_task",
                "control_panel",
                "analyzer",
            }:
                raise CorruptProposalRecord("proposal record is invalid")
            target_version = self._stored_optional_exact_text(
                row["target_version"], "target_version", 128
            )
            rollback_ref = self._stored_optional_exact_text(
                row["rollback_ref"], "rollback_ref", _MAX_REF_BYTES
            )
            created_at = _stored_timestamp(row["created_at"])
            approved_at = _stored_optional_timestamp(row["approved_at"])
            approval_actor = self._stored_optional_exact_text(
                row["approval_actor"], "approval_actor", 200
            )
            updated_at = _stored_timestamp(row["updated_at"])
            attempt_id = _stored_optional_uuid(row["apply_attempt_id"])
            repository_root = _stored_optional_absolute_path(row["repository_root"])
            original_branch = _stored_optional_ref(row["original_branch"])
            base_commit = _stored_optional_commit(row["base_commit"])
            proposal_branch = _stored_optional_ref(row["proposal_branch"])
            applied_commit = _stored_optional_commit(row["applied_commit"])
            applied_at = _stored_optional_timestamp(row["applied_at"])
            rolled_back_at = _stored_optional_timestamp(row["rolled_back_at"])
            failure_code = _stored_optional_failure_code(row["failure_code"])
            self._stored_invariants(
                status=status,
                origin=origin,
                patch=patch,
                argv=argv,
                target_version=target_version,
                created_at=created_at,
                updated_at=updated_at,
                approval_actor=approval_actor,
                approved_at=approved_at,
                attempt_id=attempt_id,
                repository_root=repository_root,
                original_branch=original_branch,
                base_commit=base_commit,
                proposal_branch=proposal_branch,
                applied_commit=applied_commit,
                applied_at=applied_at,
                rolled_back_at=rolled_back_at,
                failure_code=failure_code,
            )
            return ProposalRecord(
                proposal_id=proposal_id,
                signature=signature,
                title=title,
                description=description,
                patch=patch,
                risk=risk,
                verification_argv=argv,
                verification_summary=summary,
                status=status,
                target_version=target_version,
                rollback_ref=rollback_ref,
                created_at=created_at,
                approved_at=approved_at,
                origin=origin,
                approval_actor=approval_actor,
                updated_at=updated_at,
                apply_attempt_id=attempt_id,
                repository_root=repository_root,
                original_branch=original_branch,
                base_commit=base_commit,
                proposal_branch=proposal_branch,
                applied_commit=applied_commit,
                applied_at=applied_at,
                rolled_back_at=rolled_back_at,
                failure_code=failure_code,
            )
        except UnsafeProposalPatch:
            raise
        except (KeyError, TypeError, ValueError, ValidationError, ProposalError):
            raise CorruptProposalRecord("proposal record is invalid") from None

    def _stored_invariants(
        self,
        *,
        status: str,
        origin: str,
        patch: str | None,
        argv: tuple[str, ...],
        target_version: str | None,
        created_at: datetime,
        updated_at: datetime,
        approval_actor: str | None,
        approved_at: datetime | None,
        attempt_id: UUID | None,
        repository_root: Path | None,
        original_branch: str | None,
        base_commit: str | None,
        proposal_branch: str | None,
        applied_commit: str | None,
        applied_at: datetime | None,
        rolled_back_at: datetime | None,
        failure_code: str | None,
    ) -> None:
        if updated_at < created_at:
            raise CorruptProposalRecord("proposal record is invalid")
        if origin not in _CAPABLE_ORIGINS and status not in {"draft", "rejected"}:
            raise CorruptProposalRecord("proposal record is invalid")
        if origin == "analyzer" and (patch is not None or argv or target_version is not None):
            raise CorruptProposalRecord("proposal record is invalid")
        if origin in _CAPABLE_ORIGINS and patch is not None and not argv:
            raise CorruptProposalRecord("proposal record is invalid")
        approval_pair = approval_actor is not None and approved_at is not None
        if (approval_actor is None) != (approved_at is None):
            raise CorruptProposalRecord("proposal record is invalid")
        if approved_at is not None and not created_at <= approved_at <= updated_at:
            raise CorruptProposalRecord("proposal record is invalid")
        execution = (
            attempt_id,
            repository_root,
            original_branch,
            base_commit,
            proposal_branch,
        )
        has_execution = all(value is not None for value in execution)
        if any(value is not None for value in execution) and not has_execution:
            raise CorruptProposalRecord("proposal record is invalid")
        if origin not in _CAPABLE_ORIGINS and (approval_pair or has_execution):
            raise CorruptProposalRecord("proposal record is invalid")
        if applied_at is not None and (
            approved_at is None or not approved_at <= applied_at <= updated_at
        ):
            raise CorruptProposalRecord("proposal record is invalid")
        if rolled_back_at is not None and (
            applied_at is None or not applied_at <= rolled_back_at <= updated_at
        ):
            raise CorruptProposalRecord("proposal record is invalid")
        if status == "draft":
            if (
                approval_pair
                or has_execution
                or applied_commit
                or applied_at
                or rolled_back_at
                or failure_code
            ):
                raise CorruptProposalRecord("proposal record is invalid")
        elif status == "approved":
            if (
                not approval_pair
                or has_execution
                or applied_commit
                or applied_at
                or rolled_back_at
                or failure_code
            ):
                raise CorruptProposalRecord("proposal record is invalid")
        elif status == "rejected":
            if has_execution or applied_commit or applied_at or rolled_back_at or failure_code:
                raise CorruptProposalRecord("proposal record is invalid")
        elif status == "applying":
            if (
                not approval_pair
                or not has_execution
                or patch is None
                or not argv
                or applied_commit
                or applied_at
                or rolled_back_at
                or failure_code
            ):
                raise CorruptProposalRecord("proposal record is invalid")
        elif status == "failed":
            if (
                not approval_pair
                or not has_execution
                or patch is None
                or not argv
                or failure_code is None
            ):
                raise CorruptProposalRecord("proposal record is invalid")
            if applied_commit is not None or applied_at is not None or rolled_back_at is not None:
                raise CorruptProposalRecord("proposal record is invalid")
        elif status in {"applied", "rolled_back"}:
            if (
                not approval_pair
                or not has_execution
                or patch is None
                or not argv
                or applied_commit is None
                or applied_at is None
                or failure_code is not None
            ):
                raise CorruptProposalRecord("proposal record is invalid")
            if status == "applied" and rolled_back_at is not None:
                raise CorruptProposalRecord("proposal record is invalid")
            if status == "rolled_back" and rolled_back_at is None:
                raise CorruptProposalRecord("proposal record is invalid")

    def _load_valid_on_connection(
        self, connection: sqlite3.Connection, proposal_id: UUID
    ) -> ProposalRecord:
        return self._record_from_row(self._row_on_connection(connection, proposal_id))

    @staticmethod
    def _row_on_connection(connection: sqlite3.Connection, proposal_id: UUID) -> sqlite3.Row:
        row: sqlite3.Row | None = connection.execute(
            "select * from improvement_proposals where proposal_id = ?",
            (str(proposal_id),),
        ).fetchone()
        if row is None:
            raise KeyError(proposal_id)
        return row

    @staticmethod
    def _require_state(record: ProposalRecord, expected: str, target: str) -> None:
        if record.status != expected:
            raise InvalidProposalTransition(f"invalid transition {record.status} -> {target}")

    @staticmethod
    def _require_updated(cursor: sqlite3.Cursor, expected: str, target: str) -> None:
        if cursor.rowcount != 1:
            raise InvalidProposalTransition(f"invalid transition {expected} -> {target}")

    @staticmethod
    def _require_capable_origin(record: ProposalRecord, action: str) -> None:
        if record.origin not in _CAPABLE_ORIGINS:
            raise InvalidProposalOrigin(f"proposal origin cannot {action}")

    @staticmethod
    def _require_executable(record: ProposalRecord) -> None:
        if record.patch is None or not record.verification_argv:
            raise InvalidProposalTransition("proposal has no executable patch")

    def _transition_timestamp(self, record: ProposalRecord) -> str:
        selected = self._utc_now()
        if selected < record.updated_at:
            raise InvalidProposalTransition("proposal clock moved backward")
        return _utc_iso(selected)

    def _signature(self, value: object) -> str:
        if not isinstance(value, str) or _SIGNATURE.fullmatch(value) is None:
            raise ProposalError("proposal signature is invalid")
        result = self._redactor.redact(value)
        if result.text != value or "input_truncated" in result.findings:
            raise ProposalError("proposal signature is invalid")
        return value

    def _safe_patch(self, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise UnsafeProposalPatch("proposal patch is invalid")
        encoded = value.encode("utf-8")
        if not encoded or len(encoded) > _MAX_PATCH_BYTES or "\x00" in value:
            raise UnsafeProposalPatch("proposal patch is invalid")
        result = self._redactor.redact(value)
        if result.text != value or "input_truncated" in result.findings:
            raise UnsafeProposalPatch("proposal patch contains secret material")
        return value

    def _safe_argv(self, value: object) -> tuple[str, ...]:
        if not isinstance(value, (tuple, list)) or len(value) > _MAX_ARG_COUNT:
            raise ProposalError("verification argv is invalid")
        arguments: list[str] = []
        total = 0
        for argument in value:
            if not isinstance(argument, str) or not argument or "\x00" in argument:
                raise ProposalError("verification argv is invalid")
            size = len(argument.encode("utf-8"))
            total += size
            if size > _MAX_ARG_BYTES or total > _MAX_ARGV_BYTES:
                raise ProposalError("verification argv is invalid")
            result = self._redactor.redact(argument)
            if result.text != argument or "input_truncated" in result.findings:
                raise ProposalError("verification argv is invalid")
            arguments.append(argument)
        return tuple(arguments)

    def _stored_argv(self, document: object) -> tuple[str, ...]:
        if not isinstance(document, str) or len(document.encode("utf-8")) > _MAX_ARGV_BYTES:
            raise CorruptProposalRecord("proposal record is invalid")
        try:
            value = json.loads(document)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise CorruptProposalRecord("proposal record is invalid") from None
        try:
            return self._safe_argv(value)
        except ProposalError:
            raise CorruptProposalRecord("proposal record is invalid") from None

    def _display_text(
        self,
        value: object,
        field_name: str,
        max_chars: int,
        *,
        allow_blank: bool,
    ) -> str:
        if not isinstance(value, str):
            raise ProposalError(f"{field_name} is invalid")
        if len(value.encode("utf-8")) > _MAX_RAW_METADATA_BYTES:
            raise ProposalError(f"{field_name} is invalid")
        if any(
            unicodedata.category(character) == "Cf"
            or (unicodedata.category(character) == "Cc" and not character.isspace())
            for character in value
        ):
            raise ProposalError(f"{field_name} is invalid")
        normalized = " ".join(value.split())
        redacted = " ".join(self._redactor.redact(normalized).text.split())
        if not redacted and not allow_blank:
            raise ProposalError(f"{field_name} is invalid")
        if len(redacted) > max_chars:
            raise ProposalError(f"{field_name} exceeds display limit")
        if not redacted and not allow_blank:
            raise ProposalError(f"{field_name} is invalid")
        return redacted

    def _stored_display_text(
        self,
        value: object,
        field_name: str,
        max_chars: int,
        *,
        allow_blank: bool = False,
    ) -> str:
        try:
            safe = self._display_text(value, field_name, max_chars, allow_blank=allow_blank)
        except ProposalError:
            raise CorruptProposalRecord("proposal record is invalid") from None
        if safe != value:
            raise CorruptProposalRecord("proposal record is invalid")
        return safe

    def _exact_text(self, value: object, field_name: str, max_chars: int) -> str:
        if not isinstance(value, str):
            raise ProposalError(f"{field_name} is invalid")
        normalized = " ".join(value.split())
        if not normalized or normalized != value or len(value) > max_chars:
            raise ProposalError(f"{field_name} is invalid")
        result = self._redactor.redact(value)
        if result.text != value or "input_truncated" in result.findings:
            raise ProposalError(f"{field_name} is invalid")
        return value

    def _optional_exact_text(self, value: object, field_name: str, max_chars: int) -> str | None:
        if value is None:
            return None
        return self._exact_text(value, field_name, max_chars)

    def _stored_optional_exact_text(
        self, value: object, field_name: str, max_chars: int
    ) -> str | None:
        try:
            return self._optional_exact_text(value, field_name, max_chars)
        except ProposalError:
            raise CorruptProposalRecord("proposal record is invalid") from None

    def _utc_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise RuntimeError("proposal clock is invalid")
        return value.astimezone(timezone.utc)


def _argv_document(argv: tuple[str, ...]) -> str:
    return json.dumps(argv, ensure_ascii=False, separators=(",", ":"))


def _uuid(value: object, field_name: str) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError(f"{field_name} must be a UUID")
    return value


def _canonical_stored_uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise CorruptProposalRecord("proposal record is invalid")
    parsed = UUID(value)
    if str(parsed) != value.lower():
        raise CorruptProposalRecord("proposal record is invalid")
    return parsed


def _stored_optional_uuid(value: object) -> UUID | None:
    if value is None:
        return None
    return _canonical_stored_uuid(value)


def _absolute_path(value: object) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError("repository_root is invalid")
    document = str(value)
    if "\x00" in document or len(document.encode("utf-8")) > _MAX_PATH_BYTES:
        raise ValueError("repository_root is invalid")
    return value


def _stored_optional_absolute_path(value: object) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CorruptProposalRecord("proposal record is invalid")
    try:
        return _absolute_path(Path(value))
    except (TypeError, ValueError):
        raise CorruptProposalRecord("proposal record is invalid") from None


def _git_ref(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} is invalid")
    if (
        not value
        or value != value.strip()
        or value.startswith("-")
        or "\x00" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or len(value.encode("utf-8")) > _MAX_REF_BYTES
    ):
        raise ValueError(f"{field_name} is invalid")
    return value


def _stored_optional_ref(value: object) -> str | None:
    if value is None:
        return None
    try:
        return _git_ref(value, "git_ref")
    except (TypeError, ValueError):
        raise CorruptProposalRecord("proposal record is invalid") from None


def _commit(value: object, field_name: str) -> str:
    if not isinstance(value, str) or _COMMIT.fullmatch(value) is None:
        raise ValueError(f"{field_name} is invalid")
    return value


def _stored_optional_commit(value: object) -> str | None:
    if value is None:
        return None
    try:
        return _commit(value, "commit")
    except ValueError:
        raise CorruptProposalRecord("proposal record is invalid") from None


def _stored_optional_failure_code(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _FAILURE_CODE.fullmatch(value) is None:
        raise CorruptProposalRecord("proposal record is invalid")
    return value


def _stored_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 128:
        raise CorruptProposalRecord("proposal record is invalid")
    try:
        normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)
    except (TypeError, ValueError):
        raise CorruptProposalRecord("proposal record is invalid") from None
    if parsed.tzinfo is None:
        raise CorruptProposalRecord("proposal record is invalid")
    return parsed.astimezone(timezone.utc)


def _stored_optional_timestamp(value: object) -> datetime | None:
    if value is None:
        return None
    return _stored_timestamp(value)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
