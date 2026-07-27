from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from project_memory_hub.domain import (
    AdapterBatch,
    AdapterCheckpoint,
    CapturePayload,
    CaptureResult,
    NormalizedTaskRecord,
    ProjectRecord,
    SourceAgent,
)
from project_memory_hub.security.identifiers import (
    safe_model_identifier,
    safe_persisted_identifier,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import (
    CaptureService,
    PreparedVerifiedCapture,
    _ProjectIdentityChanged,
)
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.deferred_records import (
    CodexDeferredLocator,
    CodexDeferredRecordRepository,
    DeferredRecordCapacityError,
)
from project_memory_hub.storage.projects import (
    ProjectRegistryChangedError,
    ProjectRepository,
)
from project_memory_hub.storage.path_identity import PathIdentity
from project_memory_hub.utf8 import (
    InvalidUtf8Text,
    contains_unsafe_text_control,
    strict_utf8_size,
)


_CODEX_CWD_MAX_CHARS = 4096
_CODEX_CWD_MAX_BYTES = 16_384


class SourceAdapter(Protocol):
    source_agent: SourceAgent

    def discover_scopes(self) -> tuple[str, ...]: ...

    def read_incremental(
        self, scope: str, checkpoint: AdapterCheckpoint | None
    ) -> AdapterBatch: ...


class IngestionError(RuntimeError):
    """A trusted adapter record could not be safely committed."""


class ReconcileRequiredError(IngestionError):
    """Ingestion must wait until the project registry is reconciled."""


@dataclass(frozen=True, slots=True)
class IngestionResult:
    capture_results: tuple[CaptureResult, ...]
    checkpoint: AdapterCheckpoint
    warning_count: int = 0
    resolved_count: int = 0
    already_resolved_count: int = 0
    unmatched_resolution_count: int = 0
    deferred_count: int = 0


class IngestionService:
    def __init__(
        self,
        capture: CaptureService,
        checkpoints: CheckpointRepository,
        database: Database | None = None,
        projects: ProjectRepository | None = None,
    ) -> None:
        selected_database = checkpoints.database if database is None else database
        if not isinstance(selected_database, Database):
            raise TypeError("Codex ingestion requires a writable database")
        self._capture = capture
        self._checkpoints = checkpoints
        self._database = selected_database
        self._projects = projects or ProjectRepository(selected_database)
        self._deferred_records = CodexDeferredRecordRepository()
        self._deferred_redactor = Redactor()

    def ingest(self, adapter: SourceAdapter, scope: str) -> IngestionResult:
        source_agent = SourceAgent(adapter.source_agent)
        checkpoint = _checkpoint_snapshot(self._checkpoints.get(source_agent, scope))
        adapter_checkpoint = _checkpoint_snapshot(checkpoint)
        batch = adapter.read_incremental(scope, adapter_checkpoint)
        next_checkpoint = batch.next_checkpoint.model_copy(deep=True)

        source_ids = tuple(record.source_record_id for record in batch.records)
        if len(source_ids) != len(set(source_ids)):
            raise IngestionError("duplicate adapter source record")
        try:
            with self._database.connect(readonly=True) as connection:
                expected_registry_generation = self._projects.registry_generation_on_connection(
                    connection
                )
        except ProjectRegistryChangedError:
            raise ReconcileRequiredError("project registry requires reconcile") from None

        prepared_captures: list[PreparedVerifiedCapture] = []
        deferred_locators: list[CodexDeferredLocator] = []
        for record in batch.records:
            if record.namespace.source_agent != source_agent:
                raise IngestionError("adapter source namespace mismatch")
            payload = CapturePayload(
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
            prepared = self._capture.prepare_verified(payload, record.verification)
            if isinstance(prepared, CaptureResult):
                if source_agent is SourceAgent.CODEX and prepared.status == "project_not_found":
                    try:
                        deferred_source_record_id = _verified_codex_deferred_source_record_id(
                            record,
                            self._deferred_redactor,
                        )
                    except (TypeError, ValueError):
                        raise IngestionError("capture preparation rejected: rejected") from None
                    try:
                        deferred_locators.append(
                            CodexDeferredLocator.from_checkpoint(
                                scope,
                                deferred_source_record_id,
                                next_checkpoint,
                            )
                        )
                    except ValueError:
                        raise IngestionError("invalid deferred locator") from None
                    continue
                raise IngestionError(f"capture preparation rejected: {prepared.status}")
            prepared_captures.append(prepared)

        results: list[CaptureResult] = []
        touched_projects: dict[str, tuple[ProjectRecord, PathIdentity]] = {}
        try:
            with self._database.transaction() as connection:
                generation = expected_registry_generation
                self._projects.require_records_current_on_connection(
                    connection,
                    generation,
                    (),
                )
                prior_codex_receipt_proofs = (
                    self._checkpoints.prior_codex_receipt_proofs_on_connection(
                        connection,
                        tuple(prepared.source_record_id for prepared in prepared_captures),
                    )
                    if source_agent is SourceAgent.CODEX
                    else (None,) * len(prepared_captures)
                )
                for prepared, prior_codex_receipt_proof in zip(
                    prepared_captures,
                    prior_codex_receipt_proofs,
                    strict=True,
                ):
                    project = prepared.project
                    project_id = str(project.project_id).lower()
                    current_project = (project, prepared.live_identity)
                    previous_project = touched_projects.setdefault(project_id, current_project)
                    if (
                        previous_project[0].canonical_path != project.canonical_path
                        or previous_project[1] != prepared.live_identity
                    ):
                        raise ProjectRegistryChangedError("project registry changed")
                    self._require_touched_projects_current(
                        connection,
                        generation,
                        tuple(touched_projects.values()),
                    )
                    if prior_codex_receipt_proof is None:
                        result = self._capture.capture_prepared_on_connection(
                            connection,
                            prepared,
                        )
                    else:
                        result = self._capture.capture_prepared_on_connection(
                            connection,
                            prepared,
                            prior_codex_receipt_proof=prior_codex_receipt_proof,
                        )
                    if result.status not in {
                        "inserted",
                        "duplicate",
                        "resolved",
                        "partial",
                    }:
                        raise IngestionError("capture was not accepted")
                    results.append(result)

                try:
                    for locator in deferred_locators:
                        self._deferred_records.defer_on_connection(connection, locator)
                except DeferredRecordCapacityError:
                    raise IngestionError("deferred capacity exceeded") from None

                self._require_touched_projects_current(
                    connection,
                    generation,
                    tuple(touched_projects.values()),
                )
                self._checkpoints.commit_on_connection(
                    connection,
                    source_agent,
                    scope,
                    expected_checkpoint=checkpoint,
                    next_checkpoint=next_checkpoint,
                    source_record_ids=tuple(
                        prepared.source_record_id for prepared in prepared_captures
                    ),
                )
                self._require_touched_projects_current(
                    connection,
                    generation,
                    tuple(touched_projects.values()),
                )
        except (ProjectRegistryChangedError, _ProjectIdentityChanged):
            raise ReconcileRequiredError("project registry requires reconcile") from None

        resolved_count = sum(result.resolved_count for result in results)
        already_resolved_count = sum(result.already_resolved_count for result in results)
        unmatched_resolution_count = sum(result.unmatched_resolution_count for result in results)
        return IngestionResult(
            capture_results=tuple(results),
            checkpoint=next_checkpoint,
            warning_count=(
                len(batch.warnings) + unmatched_resolution_count + len(deferred_locators)
            ),
            resolved_count=resolved_count,
            already_resolved_count=already_resolved_count,
            unmatched_resolution_count=unmatched_resolution_count,
            deferred_count=len(deferred_locators),
        )

    def _require_touched_projects_current(
        self,
        connection: sqlite3.Connection,
        expected_generation: int,
        projects: tuple[tuple[ProjectRecord, PathIdentity], ...],
    ) -> None:
        self._projects.require_records_current_on_connection(
            connection,
            expected_generation,
            tuple(project for project, _identity in projects),
        )
        for project, expected_live_identity in projects:
            live_identity = self._projects._record_live_identity_on_connection(
                connection,
                project,
            )
            if live_identity != expected_live_identity:
                raise ProjectRegistryChangedError("project registry changed")


def _checkpoint_snapshot(
    checkpoint: AdapterCheckpoint | None,
) -> AdapterCheckpoint | None:
    return None if checkpoint is None else checkpoint.model_copy(deep=True)


def _verified_codex_deferred_source_record_id(
    record: NormalizedTaskRecord,
    redactor: Redactor,
) -> str:
    cwd = record.cwd
    if not isinstance(cwd, Path):
        raise ValueError("invalid Codex deferred cwd")
    cwd_text = str(cwd)
    try:
        invalid_cwd = (
            not cwd_text
            or cwd_text != cwd_text.strip()
            or not cwd.is_absolute()
            or os.path.normpath(cwd_text) != cwd_text
            or len(cwd_text) > _CODEX_CWD_MAX_CHARS
            or strict_utf8_size(cwd_text) > _CODEX_CWD_MAX_BYTES
            or contains_unsafe_text_control(cwd_text)
        )
    except InvalidUtf8Text:
        raise ValueError("invalid Codex deferred cwd") from None
    if invalid_cwd:
        raise ValueError("invalid Codex deferred cwd")
    verification = record.verification
    verified_at = verification.verified_at
    if (
        verification.namespace != record.namespace
        or verification.source_record_id != record.source_record_id
        or verification.verified_by != "codex_adapter"
        or verified_at.tzinfo is None
        or verified_at.utcoffset() is None
    ):
        raise ValueError("unverified Codex deferred record")
    safe_model_identifier(record.namespace.model_id, redactor)
    return safe_persisted_identifier(
        record.source_record_id,
        "source_record_id",
        redactor,
    )
