from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from project_memory_hub.adapters.codex import CodexAdapter
from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    NormalizedTaskRecord,
    SourceAgent,
)
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.deferred_records import (
    CodexDeferredRecord,
    CodexDeferredRecordRepository,
    DeferredRecoveryError,
)
from project_memory_hub.storage.projects import ProjectRepository


@dataclass(frozen=True, slots=True)
class DeferredRecoveryReport:
    status: str
    locator_count: int
    recovered_locator_count: int
    capture_status: str


class DeferredRecoveryService:
    """Recover an exact Codex capture through an explicit project rebind."""

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
        self._records = CodexDeferredRecordRepository()
        self._checkpoints = CheckpointRepository(database)

    def recover(
        self,
        *,
        source_record_id: str,
        target_project: Path,
        apply: bool = False,
    ) -> DeferredRecoveryReport:
        project = self._projects.find_by_cwd(Path(target_project))
        if project is None or project.canonical_path != Path(target_project):
            raise DeferredRecoveryError("project_not_found")
        records = self._records.records_for_source(self._database, source_record_id)
        if not records:
            raise DeferredRecoveryError("deferred_not_found")
        pending = tuple(record for record in records if record.state == "pending")
        if not pending:
            if not self._source_ref_targets_project(source_record_id, str(project.project_id)):
                raise DeferredRecoveryError("ambiguous_source")
            return DeferredRecoveryReport(
                status="already_recovered",
                locator_count=len(records),
                recovered_locator_count=0,
                capture_status="duplicate",
            )

        try:
            replayed = tuple(
                (record, self._adapter.replay_deferred(record.locator)) for record in records
            )
            canonical_pair = _canonical_replay(source_record_id, replayed)
            canonical_record = canonical_pair[1]
            expected_signature = _record_signature(canonical_record)
            if any(
                _record_signature(candidate) != expected_signature
                for _record, candidate in replayed
            ):
                raise DeferredRecoveryError("ambiguous_source")
            rebound = canonical_record.model_copy(update={"cwd": project.canonical_path})
            prepared = self._capture.prepare_verified(
                _capture_payload(rebound),
                rebound.verification,
            )
            if isinstance(prepared, CaptureResult):
                code = "project_not_found" if prepared.status == "project_not_found" else "rejected"
                raise DeferredRecoveryError(code)
            try:
                self._capture.validate_prepared_readonly(prepared)
            except RuntimeError:
                raise DeferredRecoveryError("rejected") from None
        except DeferredRecoveryError as error:
            if apply and error.code in {
                "project_not_found",
                "source_unavailable",
                "source_changed",
                "replay_limit",
                "ambiguous_source",
                "rejected",
            }:
                self._records.record_attempt(self._database, pending, error.code)
            raise

        if not apply:
            return DeferredRecoveryReport(
                status="ready",
                locator_count=len(records),
                recovered_locator_count=0,
                capture_status="validated",
            )

        recovered_at = _utc_now()
        source_hash = _deferred_source_hash(canonical_pair[0])
        try:
            with self._database.transaction() as connection:
                result = self._capture.capture_prepared_on_connection(connection, prepared)
                if result.status not in {"inserted", "duplicate", "resolved", "partial"}:
                    raise DeferredRecoveryError("rejected")
                if not self._checkpoints.receipt_exists_on_connection(
                    connection,
                    source_hash,
                    source_record_id,
                    SourceAgent.CODEX,
                ):
                    self._checkpoints.commit_import_receipt_on_connection(
                        connection,
                        source_hash,
                        source_record_id,
                        SourceAgent.CODEX,
                    )
                changed = self._records.mark_recovered_on_connection(
                    connection,
                    pending,
                    recovered_at=recovered_at,
                )
        except DeferredRecoveryError:
            raise
        except Exception:
            raise DeferredRecoveryError("rejected") from None
        return DeferredRecoveryReport(
            status="recovered",
            locator_count=len(records),
            recovered_locator_count=changed,
            capture_status=result.status,
        )

    def _source_ref_targets_project(
        self,
        source_record_id: str,
        project_id: str,
    ) -> bool:
        with self._database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select capture_project_id from source_refs
                where source_agent = ? and source_record_id = ?
                order by source_reference_id
                limit 2
                """,
                (SourceAgent.CODEX.value, source_record_id),
            ).fetchall()
        return len(rows) == 1 and rows[0]["capture_project_id"] == project_id.lower()


def _canonical_replay(
    source_record_id: str,
    replayed: tuple[tuple[CodexDeferredRecord, NormalizedTaskRecord], ...],
) -> tuple[CodexDeferredRecord, NormalizedTaskRecord]:
    session_id = source_record_id.split(":", 1)[0]
    canonical = tuple(
        pair
        for pair in replayed
        if PurePosixPath(pair[0].locator.scope).name.endswith(f"{session_id}.jsonl")
    )
    if not canonical:
        raise DeferredRecoveryError("ambiguous_source")
    anchor = _locator_anchor(canonical[0][0])
    if any(_locator_anchor(pair[0]) != anchor for pair in canonical[1:]):
        raise DeferredRecoveryError("ambiguous_source")
    return canonical[0]


def _locator_anchor(record: CodexDeferredRecord) -> tuple[str, int, int, str, str]:
    locator = record.locator
    return (
        locator.scope,
        locator.source_inode,
        locator.prefix_length,
        locator.prefix_sha256,
        locator.parser_version,
    )


def _record_signature(record: NormalizedTaskRecord) -> str:
    value = record.model_dump(mode="json", exclude={"verification"})
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


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


def _deferred_source_hash(record: CodexDeferredRecord) -> str:
    locator = record.locator
    value = {
        "parser_version": locator.parser_version,
        "prefix_length": locator.prefix_length,
        "prefix_sha256": locator.prefix_sha256,
        "scope": locator.scope,
        "source_device": locator.source_device,
        "source_inode": locator.source_inode,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
