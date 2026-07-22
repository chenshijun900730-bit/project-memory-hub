from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

from project_memory_hub.services import capture as capture_module
from project_memory_hub.adapters.codex import CodexAdapter
from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    NormalizedTaskRecord,
    SourceAgent,
)
from project_memory_hub.security.identifiers import safe_persisted_identifier
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService, PreparedVerifiedCapture
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database, strict_utc_epoch_us
from project_memory_hub.storage.projects import ProjectRepository


_MAX_PENDING_RECOVERY_MAPPINGS = 64
_VERIFICATION_WINDOW = timedelta(hours=24)


class PendingRecoveryError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PendingRecoveryMapping(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    pending_id: UUID
    scope: str
    source_record_id: str
    expected_structured_hash: str

    @field_validator("source_record_id")
    @classmethod
    def validate_source_record_id(cls, value: str) -> str:
        try:
            return safe_persisted_identifier(value, "source_record_id", Redactor())
        except ValueError:
            raise ValueError("source_record_id is invalid") from None

    @field_validator("expected_structured_hash")
    @classmethod
    def validate_structured_hash(cls, value: str) -> str:
        if (
            len(value) != 64
            or value != value.lower()
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("expected_structured_hash is invalid")
        return value


@dataclass(frozen=True, slots=True)
class PendingRecoveryReport:
    status: str
    requested_count: int
    verified_count: int
    source_count: int


@dataclass(frozen=True, slots=True)
class _PendingRow:
    pending_id: UUID
    project_id: UUID
    claimed_model_id: str
    structured_hash: str
    created_at: datetime


class PendingRecoveryService:
    """Rebuild trusted source references for fully attested pending captures."""

    def __init__(
        self,
        database: Database,
        projects: ProjectRepository,
        capture: CaptureService,
        adapter: CodexAdapter,
    ) -> None:
        self._database = database
        self._projects = projects
        self._capture = capture
        self._adapter = adapter
        self._checkpoints = CheckpointRepository(database)

    def recover(
        self,
        mappings: tuple[PendingRecoveryMapping, ...],
        *,
        apply: bool = False,
    ) -> PendingRecoveryReport:
        if not mappings or len(mappings) > _MAX_PENDING_RECOVERY_MAPPINGS:
            raise PendingRecoveryError("replay_limit")
        pending_ids = tuple(mapping.pending_id for mapping in mappings)
        if len(pending_ids) != len(set(pending_ids)):
            raise PendingRecoveryError("ambiguous_source")
        trusted_source_record_ids = tuple(mapping.source_record_id for mapping in mappings)
        if len(trusted_source_record_ids) != len(set(trusted_source_record_ids)):
            raise PendingRecoveryError("ambiguous_source")

        pending_rows = self._pending_rows(pending_ids)
        grouped: dict[str, list[PendingRecoveryMapping]] = defaultdict(list)
        for mapping in mappings:
            row = pending_rows[mapping.pending_id]
            if row.structured_hash != mapping.expected_structured_hash:
                raise PendingRecoveryError("structured_hash_mismatch")
            grouped[mapping.scope].append(mapping)

        replayed: dict[tuple[str, str], NormalizedTaskRecord] = {}
        source_hashes: dict[str, str] = {}
        for scope, scoped_mappings in sorted(grouped.items()):
            batch = self._adapter.replay_records(
                scope,
                tuple(mapping.source_record_id for mapping in scoped_mappings),
            )
            source_hashes[scope] = batch.source_hash
            for mapping, record in zip(scoped_mappings, batch.records, strict=True):
                replayed[(scope, mapping.source_record_id)] = record

        prepared: list[tuple[PendingRecoveryMapping, _PendingRow, PreparedVerifiedCapture]] = []
        for mapping in mappings:
            row = pending_rows[mapping.pending_id]
            record = replayed[(mapping.scope, mapping.source_record_id)]
            project = self._projects.find_by_cwd(record.cwd)
            if (
                project is None
                or project.project_id != row.project_id
                or record.namespace.model_id != row.claimed_model_id
                or record.verification.namespace != record.namespace
            ):
                raise PendingRecoveryError("provenance_mismatch")
            difference = _utc(record.verification.verified_at) - row.created_at
            if not -_VERIFICATION_WINDOW <= difference <= _VERIFICATION_WINDOW:
                raise PendingRecoveryError("verification_window_mismatch")
            candidate = self._capture.prepare_verified(
                _capture_payload(record),
                record.verification,
            )
            if isinstance(candidate, CaptureResult):
                raise PendingRecoveryError("rejected")
            if (
                candidate.project.project_id != row.project_id
                or candidate.structured_hash != row.structured_hash
            ):
                raise PendingRecoveryError("structured_hash_mismatch")
            try:
                self._capture.validate_prepared_readonly(candidate)
            except RuntimeError:
                raise PendingRecoveryError("rejected") from None
            prepared.append((mapping, row, candidate))

        if not apply:
            return PendingRecoveryReport(
                status="ready",
                requested_count=len(mappings),
                verified_count=0,
                source_count=len(prepared),
            )

        try:
            with self._database.transaction() as connection:
                for mapping, _row, candidate in prepared:
                    result = self._capture.capture_prepared_on_connection(
                        connection,
                        candidate,
                        exact_pending_id=mapping.pending_id,
                        defer_pending_history_cleanup=True,
                    )
                    if result.status not in {
                        "duplicate",
                        "inserted",
                        "resolved",
                        "partial",
                    }:
                        raise PendingRecoveryError("rejected")
                    source_hash = source_hashes[mapping.scope]
                    if not self._checkpoints.receipt_exists_on_connection(
                        connection,
                        source_hash,
                        mapping.source_record_id,
                        SourceAgent.CODEX,
                    ):
                        self._checkpoints.commit_import_receipt_on_connection(
                            connection,
                            source_hash,
                            mapping.source_record_id,
                            SourceAgent.CODEX,
                        )
                capture_module._cleanup_pending_capture_history_on_connection(connection)
                states = {
                    row["pending_id"]: row["final_state"]
                    for row in connection.execute(
                        """
                        select pending_id, final_state from pending_capture_history
                        where pending_id in (select value from json_each(?))
                        """,
                        (
                            _json_array(
                                tuple(str(mapping.pending_id).lower() for mapping in mappings)
                            ),
                        ),
                    ).fetchall()
                }
                if any(
                    states.get(str(mapping.pending_id).lower()) != "verified"
                    for mapping in mappings
                ):
                    raise PendingRecoveryError("pending_match_failed")
        except PendingRecoveryError:
            raise
        except Exception:
            raise PendingRecoveryError("rejected") from None
        return PendingRecoveryReport(
            status="recovered",
            requested_count=len(mappings),
            verified_count=len(mappings),
            source_count=len(prepared),
        )

    def _pending_rows(
        self,
        pending_ids: tuple[UUID, ...],
    ) -> dict[UUID, _PendingRow]:
        selected: dict[UUID, _PendingRow] = {}
        with self._database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select pending_id, project_id, claimed_model_id,
                       structured_hash, created_at, verification_state
                from pending_captures
                where pending_id in (select value from json_each(?))
                """,
                (_json_array(tuple(str(value).lower() for value in pending_ids)),),
            ).fetchall()
        for row in rows:
            try:
                pending_id = UUID(row["pending_id"])
                project_id = UUID(row["project_id"])
                created_at = _parse_utc(row["created_at"])
            except (TypeError, ValueError):
                raise PendingRecoveryError("rejected") from None
            if (
                str(pending_id).lower() != row["pending_id"]
                or str(project_id).lower() != row["project_id"]
                or row["verification_state"] != "pending"
            ):
                raise PendingRecoveryError("rejected")
            selected[pending_id] = _PendingRow(
                pending_id=pending_id,
                project_id=project_id,
                claimed_model_id=row["claimed_model_id"],
                structured_hash=row["structured_hash"],
                created_at=created_at,
            )
        if set(selected) != set(pending_ids):
            raise PendingRecoveryError("pending_not_found")
        return selected


def _capture_payload(record: NormalizedTaskRecord) -> CapturePayload:
    return CapturePayload(
        cwd=record.cwd,
        namespace=record.namespace,
        source_record_id=record.source_record_id,
        objective=record.objective,
        outcome=record.outcome,
        decisions=list(record.decisions),
        failed_attempts=list(record.failed_attempts),
        verified_commands=list(record.verified_commands),
        changed_paths=list(record.changed_paths),
        preferences=list(record.preferences),
        risks=list(record.risks),
        open_issues=list(record.open_issues),
        resolved_open_issues=list(record.resolved_open_issues),
        reusable_lessons=list(record.reusable_lessons),
    )


def _parse_utc(value: object) -> datetime:
    if strict_utc_epoch_us(value) is None or not isinstance(value, str):
        raise ValueError("invalid timestamp")
    parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise PendingRecoveryError("rejected")
    return value.astimezone(timezone.utc)


def _json_array(values: tuple[str, ...]) -> str:
    import json

    return json.dumps(values, ensure_ascii=True, separators=(",", ":"))
