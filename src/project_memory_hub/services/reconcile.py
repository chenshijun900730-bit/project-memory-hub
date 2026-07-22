from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from project_memory_hub.adapters.base import ReconcileRequiredError
from project_memory_hub.domain import ProjectRecord, ReconcileReport
from project_memory_hub.improvement.analyzer import HealthSnapshot, ImprovementAnalyzer
from project_memory_hub.improvement.models import (
    ProposalCreateResult,
    ProposalDraft,
)
from project_memory_hub.services.capture import (
    CaptureService,
    _archive_pending_capture_on_connection,
    _cleanup_pending_capture_history_on_connection,
)
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.services.retry_queue import RetryQueue
from project_memory_hub.storage.database import Database
import project_memory_hub.storage.path_identity as path_identity_module


_SUCCESS_STATE = "last_reconcile_success"
_REPORT_STATE = "last_reconcile_report"
_CATCHUP_STATE = "reconcile_catchup_required"
_CODEX_CATCHUP_STATE = "codex_reconcile_catchup_required"
_MAX_COUNT = 2**31 - 1
_PENDING_EXPIRY_BATCH = 1_000
_REPORT_MAX_STAGES = 128
_MAX_IMPROVEMENT_DRAFTS = 5
_CHATGPT_STAGE_ERROR_PRIORITY = {
    "archive_import_warning": 1,
    "resolution_not_found": 2,
    "archive_import_failed": 3,
    "project_registry_reconcile_required": 4,
    "inbox_read_failed": 5,
    "inbox_rejected": 6,
}


@dataclass(frozen=True, slots=True)
class DiscoveryStageResult:
    projects: tuple[ProjectRecord, ...]
    failure_count: int = 0
    permission_failure_count: int = 0
    duplicate_candidate_count: int = 0

    def __post_init__(self) -> None:
        for value in (
            self.failure_count,
            self.permission_failure_count,
            self.duplicate_candidate_count,
        ):
            if type(value) is not int or not 0 <= value <= _MAX_COUNT:
                raise ValueError("discovery count must be a bounded integer")


@dataclass(frozen=True, slots=True)
class PendingExpiryReport:
    expired_count: int = 0
    remaining_count: int = 0


@dataclass(frozen=True, slots=True)
class _ProjectRegistrySnapshot:
    generation: int
    untrusted_count: int
    live_identities: tuple[tuple[str, str, path_identity_module.PathIdentity], ...]


class InboxRejectedError(RuntimeError):
    """Raised when an enabled inbox exists but is not a safe readable directory."""


class _ProjectRegistryChanged(RuntimeError):
    """The project registry changed after its reconcile stage was verified."""


class ReconcileService:
    def __init__(
        self,
        database: Database,
        lock: ProcessLock,
        *,
        discover: Callable[[], Iterable[ProjectRecord] | DiscoveryStageResult] | None = None,
        scan_fact: Callable[[ProjectRecord], object] | None = None,
        retry_queue: RetryQueue | None = None,
        retry_capture: CaptureService | None = None,
        codex_runs: Iterable[Callable[[], object]] = (),
        chatgpt_import: Callable[[Path], object] | None = None,
        chatgpt_inbox: Callable[[], Iterable[Path]] | None = None,
        compact: Callable[[datetime], object] | None = None,
        improvement_analyzer: Callable[[HealthSnapshot], Iterable[ProposalDraft]] | None = None,
        improvement_draft_sink: Callable[[ProposalDraft], object] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._lock = lock
        self._discover = discover
        self._scan_fact = scan_fact
        self._retry_queue = retry_queue
        self._retry_capture = retry_capture
        self._codex_runs = tuple(codex_runs)
        self._chatgpt_import = chatgpt_import
        self._chatgpt_inbox = chatgpt_inbox
        self._compact = compact
        self._improvement_analyzer = improvement_analyzer
        self._improvement_draft_sink = improvement_draft_sink
        self._now = now or (lambda: datetime.now(timezone.utc))

    @classmethod
    def minimal(
        cls,
        database: Database,
        lock: ProcessLock,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> ReconcileService:
        return cls(database, lock, now=now)

    def should_run(self, *, now: datetime | None = None) -> bool:
        selected_now = _utc(now if now is not None else self._now())
        with self._database.connect(readonly=True) as connection:
            rows = {
                row["name"]: row["value_json"]
                for row in connection.execute(
                    "select name, value_json from app_state where name in (?, ?, ?)",
                    (_SUCCESS_STATE, _CATCHUP_STATE, _CODEX_CATCHUP_STATE),
                ).fetchall()
            }
        if _CATCHUP_STATE in rows or _CODEX_CATCHUP_STATE in rows:
            return True
        value_json = rows.get(_SUCCESS_STATE)
        if value_json is None:
            return True
        try:
            value = json.loads(value_json)
            if not isinstance(value, dict) or set(value) != {"timestamp"}:
                return True
            timestamp = _parse_timestamp(value["timestamp"])
        except (TypeError, ValueError, json.JSONDecodeError):
            return True
        if timestamp > selected_now:
            return True
        return selected_now - timestamp >= timedelta(hours=24)

    def _codex_catchup_token(self) -> tuple[str, str] | None:
        with self._database.connect(readonly=True) as connection:
            row = connection.execute(
                "select value_json, updated_at from app_state where name = ?",
                (_CODEX_CATCHUP_STATE,),
            ).fetchone()
        if row is None:
            return None
        value_json = row["value_json"]
        updated_at = row["updated_at"]
        if value_json != '{"required":true}' or not isinstance(updated_at, str):
            raise RuntimeError("Codex catch-up state is invalid")
        return value_json, updated_at

    def record_success(self, when: datetime | None = None) -> None:
        timestamp = _iso(_utc(when if when is not None else self._now()))
        registry_snapshot = self._enabled_project_registry_snapshot()
        if registry_snapshot.untrusted_count:
            raise _ProjectRegistryChanged("project registry is untrusted")
        with self._database.transaction() as connection:
            self._require_project_registry_current(connection, registry_snapshot)
            connection.execute("delete from app_state where name = ?", (_CATCHUP_STATE,))
            self._upsert_state(
                connection,
                _SUCCESS_STATE,
                {"timestamp": timestamp},
                timestamp,
            )
            self._require_project_registry_current(connection, registry_snapshot)

    def run(self, force: bool = False) -> ReconcileReport:
        run_id = uuid4()
        try:
            with self._lock.acquire() as lock_outcome:
                if not lock_outcome.acquired:
                    return ReconcileReport(run_id=run_id, status="already_running")
                return self._run_locked(run_id, force)
        except Exception:
            return ReconcileReport(
                run_id=run_id,
                status="failed",
                warning_count=1,
                stages={"lock": "error"},
            )

    def _run_locked(self, run_id: UUID, force: bool) -> ReconcileReport:
        try:
            run_now = _utc(self._now())
        except Exception:
            return self._failed(run_id, {"clock": "error"}, warnings=1)
        if not force:
            try:
                if not self.should_run(now=run_now):
                    return ReconcileReport(run_id=run_id, status="skipped")
            except Exception:
                return self._failed(run_id, {"schedule": "error"}, warnings=1)
        try:
            codex_catchup_token = self._codex_catchup_token()
        except Exception:
            return self._failed(run_id, {"schedule": "error"}, warnings=1)

        stages: dict[str, str] = {"lock": "pass"}
        stage_metrics: dict[str, dict[str, int]] = {"lock": {"acquired_count": 1}}
        stage_errors: dict[str, str] = {}
        inserted = duplicates = warnings = 0
        core_failed = False
        degraded = False
        catchup_pending = codex_catchup_token is not None and not self._codex_runs
        projects: tuple[ProjectRecord, ...] = ()
        permission_failures = 0
        duplicate_candidates = 0
        adapter_failures = 0
        retry_failures = 0
        retry_remaining = 0
        compaction_failures = 0
        compaction_remaining = 0
        project_registry_snapshot: _ProjectRegistrySnapshot | None = None

        if catchup_pending:
            warnings = _add_count(warnings, 1)
            degraded = True
            stages["codex_catchup"] = "warn"
            stage_metrics["codex_catchup"] = {
                "failure_count": 0,
                "pending_count": 1,
            }
            stage_errors["codex_catchup"] = "codex_source_disabled"

        try:
            discovered = self._discover() if self._discover is not None else ()
            if isinstance(discovered, DiscoveryStageResult):
                projects = discovered.projects
                failures = discovered.failure_count
                permission_failures = discovered.permission_failure_count
                duplicate_candidates = discovered.duplicate_candidate_count
            else:
                projects = tuple(discovered)
                failures = 0
            if failures:
                warnings = _add_count(warnings, failures)
                core_failed = True
                stages["discover"] = "error"
                stage_errors["discover"] = "project_discovery_incomplete"
            else:
                stages["discover"] = "pass"
        except Exception:
            failures = 1
            warnings = _add_count(warnings, 1)
            core_failed = True
            stages["discover"] = "error"
            stage_errors["discover"] = "project_discovery_failed"
        stage_metrics["discover"] = {
            "duplicate_candidate_count": duplicate_candidates,
            "failure_count": failures,
            "permission_failure_count": permission_failures,
            "project_count": len(projects),
        }

        fact_failures = 0
        fact_warnings = 0
        if self._scan_fact is not None:
            for project in projects:
                try:
                    fact_report = self._scan_fact(project)
                    fact_warnings = _add_count(
                        fact_warnings,
                        _sequence_count(getattr(fact_report, "warnings", ())),
                    )
                except Exception:
                    fact_failures = _add_count(fact_failures, 1)
        warnings = _add_count(warnings, fact_failures + fact_warnings)
        stage_metrics["facts"] = {
            "failure_count": fact_failures,
            "project_count": len(projects),
            "warning_count": fact_warnings,
        }
        if fact_failures:
            core_failed = True
            stages["facts"] = "error"
            stage_errors["facts"] = "project_fact_scan_failed"
        elif fact_warnings:
            degraded = True
            stages["facts"] = "warn"
            stage_errors["facts"] = "project_fact_scan_warning"
        else:
            stages["facts"] = "pass"

        if self._retry_queue is not None and self._retry_capture is not None:
            try:
                retry = self._retry_queue.drain(self._retry_capture)
                retry_failures = _safe_count(retry.failed_count)
                retry_remaining = _safe_count(retry.remaining_count)
                warnings = _add_count(warnings, retry_failures)
                stage_metrics["retry"] = {
                    "completed_count": _safe_count(retry.completed_count),
                    "failure_count": retry_failures,
                    "remaining_count": retry_remaining,
                }
                if retry_remaining:
                    if not retry_failures:
                        warnings = _add_count(warnings, 1)
                    catchup_pending = True
                    degraded = True
                    stages["retry"] = "warn"
                    stage_errors["retry"] = "retry_backlog_remaining"
                elif retry_failures:
                    degraded = True
                    stages["retry"] = "warn"
                    stage_errors["retry"] = "retry_items_deferred"
                else:
                    stages["retry"] = "pass"
            except Exception:
                retry_failures = 1
                retry_remaining = 0
                warnings = _add_count(warnings, 1)
                core_failed = True
                stages["retry"] = "error"
                stage_metrics["retry"] = {
                    "completed_count": 0,
                    "failure_count": 1,
                    "remaining_count": 0,
                }
                stage_errors["retry"] = "retry_drain_failed"
        else:
            stages["retry"] = "pass"
            stage_metrics["retry"] = {
                "completed_count": 0,
                "failure_count": 0,
                "remaining_count": 0,
            }

        for index, operation in enumerate(self._codex_runs):
            stage = f"codex_{index}"
            try:
                result = operation()
                result_inserted, result_duplicates = _result_counts(result)
                (
                    result_resolved,
                    result_already_resolved,
                    result_unmatched_resolution,
                ) = _resolution_counts(result)
                inserted = _add_count(inserted, result_inserted)
                duplicates = _add_count(duplicates, result_duplicates)
                operation_warnings = _safe_count(getattr(result, "warning_count", 0))
                operation_deferred = _safe_count(getattr(result, "deferred_count", 0))
                operation_failures = _safe_count(getattr(result, "failure_count", 0))
                adapter_failures = _add_count(adapter_failures, operation_failures)
                warnings = _add_count(
                    warnings,
                    operation_warnings + operation_failures,
                )
                stage_metrics[stage] = {
                    "already_resolved_count": result_already_resolved,
                    "deferred_count": operation_deferred,
                    "duplicate_count": result_duplicates,
                    "failure_count": operation_failures,
                    "inserted_count": result_inserted,
                    "resolved_count": result_resolved,
                    "unmatched_resolution_count": result_unmatched_resolution,
                    "warning_count": operation_warnings,
                }
                if operation_failures:
                    degraded = True
                    if codex_catchup_token is not None:
                        catchup_pending = True
                    stages[stage] = "warn"
                    stage_errors[stage] = "adapter_scope_failed"
                elif result_unmatched_resolution:
                    degraded = True
                    stages[stage] = "warn"
                    stage_errors[stage] = "resolution_not_found"
                elif operation_warnings:
                    degraded = True
                    stages[stage] = "warn"
                    stage_errors[stage] = "adapter_run_warning"
                else:
                    stages[stage] = "pass"
            except Exception:
                stages[stage] = "error"
                warnings = _add_count(warnings, 1)
                degraded = True
                if codex_catchup_token is not None:
                    catchup_pending = True
                adapter_failures = _add_count(adapter_failures, 1)
                stage_metrics[stage] = {
                    "already_resolved_count": 0,
                    "deferred_count": 0,
                    "duplicate_count": 0,
                    "failure_count": 1,
                    "inserted_count": 0,
                    "resolved_count": 0,
                    "unmatched_resolution_count": 0,
                    "warning_count": 0,
                }
                stage_errors[stage] = "adapter_run_failed"

        try:
            project_registry_snapshot = self._enabled_project_registry_snapshot()
            untrusted_projects = project_registry_snapshot.untrusted_count
            stage_metrics["project_registry"] = {
                "failure_count": 0,
                "untrusted_count": untrusted_projects,
            }
            if untrusted_projects:
                warnings = _add_count(warnings, 1)
                degraded = True
                catchup_pending = True
                stages["project_registry"] = "warn"
                stage_errors["project_registry"] = "project_identity_reconcile_required"
            else:
                stages["project_registry"] = "pass"
        except Exception:
            warnings = _add_count(warnings, 1)
            core_failed = True
            stages["project_registry"] = "error"
            stage_metrics["project_registry"] = {
                "failure_count": 1,
                "untrusted_count": 0,
            }
            stage_errors["project_registry"] = "project_identity_check_failed"

        if self._chatgpt_import is not None and self._chatgpt_inbox is not None:
            archive_failures = archive_warnings = 0
            archive_inserted = archive_duplicates = 0
            archive_resolved = archive_already_resolved = archive_unmatched_resolution = 0
            try:
                archives = tuple(sorted(self._chatgpt_inbox(), key=lambda item: item.name))
            except InboxRejectedError:
                archives = ()
                stages["chatgpt"] = "error"
                warnings = _add_count(warnings, 1)
                degraded = True
                catchup_pending = True
                archive_failures = 1
                stage_errors["chatgpt"] = _preferred_chatgpt_stage_error(
                    stage_errors.get("chatgpt"),
                    "inbox_rejected",
                )
            except Exception:
                archives = ()
                stages["chatgpt"] = "error"
                warnings = _add_count(warnings, 1)
                degraded = True
                archive_failures = 1
                stage_errors["chatgpt"] = _preferred_chatgpt_stage_error(
                    stage_errors.get("chatgpt"),
                    "inbox_read_failed",
                )
            else:
                stages["chatgpt"] = "pass"
            for archive in archives:
                try:
                    report = self._chatgpt_import(archive)
                    result_inserted, result_duplicates = _result_counts(report)
                    inserted = _add_count(inserted, result_inserted)
                    duplicates = _add_count(duplicates, result_duplicates)
                    archive_inserted = _add_count(archive_inserted, result_inserted)
                    archive_duplicates = _add_count(archive_duplicates, result_duplicates)
                    (
                        import_resolved,
                        import_already_resolved,
                        import_unmatched_resolution,
                    ) = _resolution_counts(report)
                    archive_resolved = _add_count(archive_resolved, import_resolved)
                    archive_already_resolved = _add_count(
                        archive_already_resolved,
                        import_already_resolved,
                    )
                    archive_unmatched_resolution = _add_count(
                        archive_unmatched_resolution,
                        import_unmatched_resolution,
                    )
                    missing_warning_count = object()
                    explicit_warning_count = getattr(
                        report,
                        "warning_count",
                        missing_warning_count,
                    )
                    if explicit_warning_count is not missing_warning_count:
                        import_warnings = _safe_count(explicit_warning_count)
                    else:
                        import_warnings = _sequence_count(getattr(report, "warnings", ()))
                    warnings = _add_count(warnings, import_warnings)
                    archive_warnings = _add_count(archive_warnings, import_warnings)
                except ReconcileRequiredError:
                    stages["chatgpt"] = "error"
                    warnings = _add_count(warnings, 1)
                    degraded = True
                    catchup_pending = True
                    archive_failures = _add_count(archive_failures, 1)
                    stage_errors["chatgpt"] = _preferred_chatgpt_stage_error(
                        stage_errors.get("chatgpt"),
                        "project_registry_reconcile_required",
                    )
                except Exception:
                    stages["chatgpt"] = "error"
                    warnings = _add_count(warnings, 1)
                    degraded = True
                    archive_failures = _add_count(archive_failures, 1)
                    stage_errors["chatgpt"] = _preferred_chatgpt_stage_error(
                        stage_errors.get("chatgpt"),
                        "archive_import_failed",
                    )
            if stages["chatgpt"] != "error":
                if archive_unmatched_resolution:
                    stages["chatgpt"] = "warn"
                    degraded = True
                    stage_errors["chatgpt"] = _preferred_chatgpt_stage_error(
                        stage_errors.get("chatgpt"),
                        "resolution_not_found",
                    )
                elif archive_warnings:
                    stages["chatgpt"] = "warn"
                    degraded = True
                    stage_errors["chatgpt"] = _preferred_chatgpt_stage_error(
                        stage_errors.get("chatgpt"),
                        "archive_import_warning",
                    )
            stage_metrics["chatgpt"] = {
                "already_resolved_count": archive_already_resolved,
                "archive_count": len(archives),
                "duplicate_count": archive_duplicates,
                "failure_count": archive_failures,
                "inserted_count": archive_inserted,
                "resolved_count": archive_resolved,
                "unmatched_resolution_count": archive_unmatched_resolution,
                "warning_count": archive_warnings,
            }
            adapter_failures = _add_count(adapter_failures, archive_failures)
        else:
            stages["chatgpt"] = "pass"
            stage_metrics["chatgpt"] = {
                "already_resolved_count": 0,
                "archive_count": 0,
                "duplicate_count": 0,
                "failure_count": 0,
                "inserted_count": 0,
                "resolved_count": 0,
                "unmatched_resolution_count": 0,
                "warning_count": 0,
            }

        try:
            pending = self._expire_pending(run_now)
            stage_metrics["pending"] = {
                "expired_count": pending.expired_count,
                "failure_count": 0,
                "remaining_count": pending.remaining_count,
            }
            if pending.remaining_count:
                warnings = _add_count(warnings, 1)
                degraded = True
                catchup_pending = True
                stages["pending"] = "warn"
                stage_errors["pending"] = "pending_backlog_remaining"
            else:
                stages["pending"] = "pass"
        except Exception:
            stages["pending"] = "error"
            warnings = _add_count(warnings, 1)
            core_failed = True
            stage_metrics["pending"] = {
                "expired_count": 0,
                "failure_count": 1,
                "remaining_count": 0,
            }
            stage_errors["pending"] = "pending_expiry_failed"

        compaction_skipped = core_failed or degraded or catchup_pending
        if self._compact is None or compaction_skipped:
            stages["compaction"] = "pass"
            stage_metrics["compaction"] = {
                "cold_count": 0,
                "failure_count": 0,
                "namespace_count": 0,
                "project_count": 0,
                "remaining_count": 0,
                "retrospective_count": 0,
                "skipped_count": int(compaction_skipped),
                "source_count": 0,
            }
        else:
            try:
                compacted = self._compact(run_now)
                compaction_failures = _safe_count(getattr(compacted, "failure_count", 0))
                compaction_remaining = _safe_count(getattr(compacted, "remaining_count", 0))
                stage_metrics["compaction"] = {
                    "cold_count": _safe_count(getattr(compacted, "cold_count", 0)),
                    "failure_count": compaction_failures,
                    "namespace_count": _safe_count(getattr(compacted, "namespace_count", 0)),
                    "project_count": _safe_count(getattr(compacted, "project_count", 0)),
                    "remaining_count": compaction_remaining,
                    "retrospective_count": _safe_count(
                        getattr(compacted, "retrospective_count", 0)
                    ),
                    "skipped_count": 0,
                    "source_count": _safe_count(getattr(compacted, "source_count", 0)),
                }
                if compaction_failures:
                    warnings = _add_count(warnings, compaction_failures)
                    degraded = True
                    catchup_pending = True
                    stages["compaction"] = "error"
                    stage_errors["compaction"] = "compaction_failed"
                elif compaction_remaining:
                    warnings = _add_count(warnings, 1)
                    degraded = True
                    catchup_pending = True
                    stages["compaction"] = "warn"
                    stage_errors["compaction"] = "compaction_backlog_remaining"
                else:
                    stages["compaction"] = "pass"
            except Exception:
                compaction_failures = 1
                compaction_remaining = 0
                warnings = _add_count(warnings, 1)
                degraded = True
                catchup_pending = True
                stages["compaction"] = "error"
                stage_metrics["compaction"] = {
                    "cold_count": 0,
                    "failure_count": 1,
                    "namespace_count": 0,
                    "project_count": 0,
                    "remaining_count": 0,
                    "retrospective_count": 0,
                    "skipped_count": 0,
                    "source_count": 0,
                }
                stage_errors["compaction"] = "compaction_failed"

        improvement_metrics = {
            "analyzed_count": 0,
            "created_count": 0,
            "duplicate_count": 0,
            "failure_count": 0,
            "skipped_count": 0,
        }
        if self._improvement_analyzer is None:
            stages["improvement"] = "pass"
            improvement_metrics["skipped_count"] = 1
        else:
            try:
                health = HealthSnapshot(
                    discovery_failure_count=_safe_count(failures),
                    permission_failure_count=_safe_count(permission_failures),
                    adapter_failure_count=_safe_count(adapter_failures),
                    retry_failure_count=_safe_count(retry_failures),
                    retry_remaining_count=_safe_count(retry_remaining),
                    inserted_count=_safe_count(inserted),
                    duplicate_count=_safe_count(duplicates),
                    duplicate_candidate_count=_safe_count(duplicate_candidates),
                    compaction_failure_count=_safe_count(compaction_failures),
                    compaction_remaining_count=_safe_count(compaction_remaining),
                )
                drafts = _validated_improvement_drafts(self._improvement_analyzer(health), health)
                improvement_metrics["analyzed_count"] = len(drafts)
                if drafts and self._improvement_draft_sink is None:
                    raise ValueError("improvement draft sink is unavailable")
                for draft in drafts:
                    result = self._improvement_draft_sink(draft)  # type: ignore[misc]
                    validated_result = _validated_create_result(result)
                    improvement_metrics["created_count"] = _add_count(
                        improvement_metrics["created_count"],
                        int(validated_result.inserted),
                    )
                    improvement_metrics["duplicate_count"] = _add_count(
                        improvement_metrics["duplicate_count"],
                        int(validated_result.duplicate),
                    )
                stages["improvement"] = "pass"
            except Exception:
                warnings = _add_count(warnings, 1)
                degraded = True
                stages["improvement"] = "warn"
                improvement_metrics["failure_count"] = 1
                stage_errors["improvement"] = "improvement_analysis_failed"
        stage_metrics["improvement"] = improvement_metrics

        if core_failed:
            stages["app_state"] = "pass"
            stage_metrics["app_state"] = {
                "failure_count": 0,
                "report_count": 1,
                "success_count": 0,
            }
            report = self._failed(
                run_id,
                stages,
                inserted=inserted,
                duplicates=duplicates,
                warnings=warnings,
            )
            try:
                self._record_incomplete(
                    run_now,
                    report,
                    stage_metrics,
                    stage_errors,
                )
            except Exception:
                failed_stages = dict(stages)
                failed_stages["app_state"] = "error"
                return self._failed(
                    run_id,
                    failed_stages,
                    inserted=inserted,
                    duplicates=duplicates,
                    warnings=_add_count(warnings, 1),
                )
            return report

        if catchup_pending:
            stages["app_state"] = "pass"
            stage_metrics["app_state"] = {
                "failure_count": 0,
                "report_count": 1,
                "success_count": 0,
            }
            report = ReconcileReport(
                run_id=run_id,
                status="degraded",
                inserted_count=inserted,
                duplicate_count=duplicates,
                warning_count=warnings,
                stages=stages,
            )
            try:
                self._record_incomplete(
                    run_now,
                    report,
                    stage_metrics,
                    stage_errors,
                )
            except Exception:
                failed_stages = dict(stages)
                failed_stages["app_state"] = "error"
                return self._failed(
                    run_id,
                    failed_stages,
                    inserted=inserted,
                    duplicates=duplicates,
                    warnings=_add_count(warnings, 1),
                )
            return report

        status: Literal["degraded", "success"] = "degraded" if degraded else "success"
        stages["app_state"] = "pass"
        stage_metrics["app_state"] = {
            "failure_count": 0,
            "report_count": 1,
            "success_count": 1,
        }
        report = ReconcileReport(
            run_id=run_id,
            status=status,
            inserted_count=inserted,
            duplicate_count=duplicates,
            warning_count=warnings,
            stages=stages,
        )
        try:
            self._record_completion(
                run_now,
                report,
                stage_metrics,
                stage_errors,
                project_registry_snapshot,
                codex_catchup_token,
            )
        except _ProjectRegistryChanged:
            drift_stages = dict(stages)
            drift_stages["project_registry"] = "warn"
            drift_stages["app_state"] = "pass"
            drift_metrics = dict(stage_metrics)
            drift_metrics["project_registry"] = {
                **drift_metrics.get("project_registry", {}),
                "drift_count": 1,
            }
            drift_metrics["app_state"] = {
                "failure_count": 0,
                "report_count": 1,
                "success_count": 0,
            }
            drift_errors = dict(stage_errors)
            drift_errors["project_registry"] = "project_registry_changed"
            drift_report = ReconcileReport(
                run_id=run_id,
                status="degraded",
                inserted_count=inserted,
                duplicate_count=duplicates,
                warning_count=_add_count(warnings, 1),
                stages=drift_stages,
            )
            try:
                self._record_incomplete(
                    run_now,
                    drift_report,
                    drift_metrics,
                    drift_errors,
                )
            except Exception:
                failed_stages = dict(drift_stages)
                failed_stages["app_state"] = "error"
                return self._failed(
                    run_id,
                    failed_stages,
                    inserted=inserted,
                    duplicates=duplicates,
                    warnings=_add_count(warnings, 2),
                )
            return drift_report
        except Exception:
            failed_stages = dict(stages)
            failed_stages["app_state"] = "error"
            failed_report = self._failed(
                run_id,
                failed_stages,
                inserted=inserted,
                duplicates=duplicates,
                warnings=_add_count(warnings, 1),
            )
            failed_metrics = dict(stage_metrics)
            failed_metrics["app_state"] = {
                "failure_count": 1,
                "report_count": 0,
                "success_count": 0,
            }
            failed_errors = dict(stage_errors)
            failed_errors["app_state"] = "app_state_write_failed"
            try:
                self._record_report(
                    run_now,
                    failed_report,
                    failed_metrics,
                    failed_errors,
                )
            except Exception:
                pass
            return failed_report
        return report

    def _record_completion(
        self,
        when: datetime,
        report: ReconcileReport,
        stage_metrics: dict[str, dict[str, int]],
        stage_errors: dict[str, str],
        expected_registry_snapshot: _ProjectRegistrySnapshot | None,
        expected_codex_catchup: tuple[str, str] | None,
    ) -> None:
        timestamp = _iso(_utc(when))
        report_value = _report_value(
            report,
            timestamp,
            stage_metrics,
            stage_errors,
        )
        with self._database.transaction() as connection:
            self._require_project_registry_current(
                connection,
                expected_registry_snapshot,
            )
            connection.execute("delete from app_state where name = ?", (_CATCHUP_STATE,))
            if expected_codex_catchup is not None:
                cursor = connection.execute(
                    """
                    delete from app_state
                    where name = ? and value_json = ? and updated_at = ?
                    """,
                    (_CODEX_CATCHUP_STATE, *expected_codex_catchup),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("Codex catch-up state changed")
            self._upsert_state(
                connection,
                _SUCCESS_STATE,
                {"timestamp": timestamp},
                timestamp,
            )
            self._upsert_state(
                connection,
                _REPORT_STATE,
                report_value,
                timestamp,
            )
            self._require_project_registry_current(
                connection,
                expected_registry_snapshot,
            )

    def _record_report(
        self,
        when: datetime,
        report: ReconcileReport,
        stage_metrics: dict[str, dict[str, int]],
        stage_errors: dict[str, str],
    ) -> None:
        timestamp = _iso(_utc(when))
        with self._database.transaction() as connection:
            self._upsert_state(
                connection,
                _REPORT_STATE,
                _report_value(
                    report,
                    timestamp,
                    stage_metrics,
                    stage_errors,
                ),
                timestamp,
            )

    def _record_incomplete(
        self,
        when: datetime,
        report: ReconcileReport,
        stage_metrics: dict[str, dict[str, int]],
        stage_errors: dict[str, str],
    ) -> None:
        timestamp = _iso(_utc(when))
        with self._database.transaction() as connection:
            self._upsert_state(
                connection,
                _CATCHUP_STATE,
                {"required": True},
                timestamp,
            )
            self._upsert_state(
                connection,
                _REPORT_STATE,
                _report_value(
                    report,
                    timestamp,
                    stage_metrics,
                    stage_errors,
                ),
                timestamp,
            )

    @staticmethod
    def _upsert_state(
        connection: sqlite3.Connection,
        name: str,
        value: object,
        timestamp: str,
    ) -> None:
        connection.execute(
            """
            insert into app_state(name, value_json, updated_at)
            values (?, ?, ?)
            on conflict(name) do update set
                value_json = excluded.value_json,
                updated_at = excluded.updated_at
            """,
            (
                name,
                json.dumps(value, sort_keys=True, separators=(",", ":")),
                timestamp,
            ),
        )

    def _expire_pending(self, now: datetime) -> PendingExpiryReport:
        selected_now = _utc(now)
        with self._database.transaction() as connection:
            rows = self._pending_expiry_candidates(
                connection,
                selected_now,
                _PENDING_EXPIRY_BATCH + 1,
            )
            expired = 0
            for row in rows[:_PENDING_EXPIRY_BATCH]:
                archived = _archive_pending_capture_on_connection(
                    connection,
                    row["pending_id"],
                    final_state="expired",
                    finalized_at=selected_now,
                )
                if not archived:
                    continue
                expired += 1
            if expired:
                _cleanup_pending_capture_history_on_connection(connection)
            remaining = len(
                self._pending_expiry_candidates(
                    connection,
                    selected_now,
                    _PENDING_EXPIRY_BATCH + 1,
                )
            )
        return PendingExpiryReport(expired, remaining)

    def _enabled_project_registry_snapshot(self) -> _ProjectRegistrySnapshot:
        with self._database.connect(readonly=True) as connection:
            generation = self._project_registry_generation(connection)
            rows = connection.execute(
                """
                select project_id, canonical_path, path_device, path_inode
                from projects where enabled = 1
                order by project_id
                """
            ).fetchall()
            if self._project_registry_generation(connection) != generation:
                raise _ProjectRegistryChanged
        count = 0
        live_identities: list[tuple[str, str, path_identity_module.PathIdentity]] = []
        for row in rows:
            live_identity = path_identity_module.validated_persisted_directory_identity(
                Path(row["canonical_path"]),
                row["path_device"],
                row["path_inode"],
            )
            if live_identity is None:
                count = _add_count(count, 1)
                continue
            live_identities.append((row["project_id"], row["canonical_path"], live_identity))
        with self._database.connect(readonly=True) as connection:
            if self._project_registry_generation(connection) != generation:
                raise _ProjectRegistryChanged
        return _ProjectRegistrySnapshot(generation, count, tuple(live_identities))

    @classmethod
    def _require_project_registry_current(
        cls,
        connection: sqlite3.Connection,
        expected_snapshot: _ProjectRegistrySnapshot | None,
    ) -> None:
        if (
            expected_snapshot is None
            or cls._project_registry_generation(connection) != expected_snapshot.generation
        ):
            raise _ProjectRegistryChanged
        rows = connection.execute(
            """
            select project_id, canonical_path
            from projects where enabled = 1
            order by project_id
            """
        ).fetchall()
        if len(rows) != len(expected_snapshot.live_identities):
            raise _ProjectRegistryChanged
        for row, expected in zip(rows, expected_snapshot.live_identities, strict=True):
            project_id, canonical_path, live_identity = expected
            if (
                row["project_id"] != project_id
                or row["canonical_path"] != canonical_path
                or path_identity_module.complete_directory_identity(Path(canonical_path))
                != live_identity
            ):
                raise _ProjectRegistryChanged
        if cls._project_registry_generation(connection) != expected_snapshot.generation:
            raise _ProjectRegistryChanged

    @staticmethod
    def _project_registry_generation(connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "select generation from project_registry_state where singleton = 1"
        ).fetchone()
        if row is None or type(row["generation"]) is not int or row["generation"] < 0:
            raise _ProjectRegistryChanged
        return row["generation"]

    @staticmethod
    def _pending_expiry_candidates(
        connection: sqlite3.Connection,
        now: datetime,
        limit: int,
    ) -> list[sqlite3.Row]:
        # Stored expiry values are UTC ISO strings. The next-second range is only an
        # indexed coarse bound; Python performs the exact subsecond comparison.
        next_second = (now + timedelta(seconds=1)).replace(microsecond=0)
        rows = connection.execute(
            """
            select pending_id, expires_at
            from pending_captures
            where verification_state = 'pending'
              and expires_at < ?
            order by expires_at
            limit ?
            """,
            (_iso(next_second), limit),
        ).fetchall()
        return [row for row in rows if _parse_timestamp(row["expires_at"]) <= now]

    @staticmethod
    def _failed(
        run_id: UUID,
        stages: dict[str, str],
        *,
        inserted: int = 0,
        duplicates: int = 0,
        warnings: int = 0,
    ) -> ReconcileReport:
        return ReconcileReport(
            run_id=run_id,
            status="failed",
            inserted_count=_safe_count(inserted),
            duplicate_count=_safe_count(duplicates),
            warning_count=_safe_count(warnings),
            stages=stages,
        )


def _result_counts(value: object) -> tuple[int, int]:
    direct_inserted = getattr(value, "inserted_count", None)
    if type(direct_inserted) is not int:
        direct_inserted = getattr(value, "imported_count", None)
    direct_duplicates = getattr(value, "duplicate_count", None)
    if type(direct_inserted) is int or type(direct_duplicates) is int:
        return _safe_count(direct_inserted), _safe_count(direct_duplicates)
    capture_results = getattr(value, "capture_results", ())
    if type(capture_results) is not tuple:
        return 0, 0
    inserted = 0
    duplicates = 0
    for item in capture_results:
        inserted_ids = getattr(item, "inserted_ids", None)
        inserted += type(inserted_ids) is tuple and bool(inserted_ids)
        duplicates += getattr(item, "status", None) == "duplicate"
    return _safe_count(inserted), _safe_count(duplicates)


def _resolution_counts(value: object) -> tuple[int, int, int]:
    field_names = (
        "resolved_count",
        "already_resolved_count",
        "unmatched_resolution_count",
    )
    missing = object()
    direct = tuple(getattr(value, name, missing) for name in field_names)
    if any(item is not missing for item in direct):
        return (
            _safe_count(direct[0]),
            _safe_count(direct[1]),
            _safe_count(direct[2]),
        )

    capture_results = getattr(value, "capture_results", ())
    if type(capture_results) is not tuple:
        return 0, 0, 0
    totals = [0, 0, 0]
    for result in capture_results:
        for index, name in enumerate(field_names):
            totals[index] = _add_count(totals[index], getattr(result, name, 0))
    return totals[0], totals[1], totals[2]


def _preferred_chatgpt_stage_error(current: str | None, candidate: str) -> str:
    current_priority = _CHATGPT_STAGE_ERROR_PRIORITY.get(current or "", 0)
    candidate_priority = _CHATGPT_STAGE_ERROR_PRIORITY.get(candidate, 0)
    return candidate if candidate_priority > current_priority else (current or candidate)


def _validated_improvement_drafts(
    value: object, health: HealthSnapshot
) -> tuple[ProposalDraft, ...]:
    if not isinstance(value, Iterable):
        raise ValueError("improvement analyzer result is invalid")
    try:
        iterator = iter(value)
    except TypeError:
        raise ValueError("improvement analyzer result is invalid") from None
    candidates: list[object] = []
    for _index in range(_MAX_IMPROVEMENT_DRAFTS + 1):
        try:
            candidate = next(iterator)
        except StopIteration:
            break
        candidates.append(candidate)
    if len(candidates) > _MAX_IMPROVEMENT_DRAFTS:
        raise ValueError("improvement analyzer result exceeds limit")
    canonical = tuple(ImprovementAnalyzer().analyze(health))
    if len(candidates) != len(canonical):
        raise ValueError("improvement analyzer result is noncanonical")
    return tuple(
        _validated_analyzer_draft(candidate, expected)
        for candidate, expected in zip(candidates, canonical, strict=True)
    )


def _validated_analyzer_draft(value: object, expected: ProposalDraft) -> ProposalDraft:
    if type(value) is not ProposalDraft:
        raise ValueError("improvement analyzer draft is invalid")
    if set(value.__dict__) != set(expected.__dict__):
        raise ValueError("improvement analyzer draft is invalid")
    for field in ProposalDraft.model_fields:
        candidate_value = getattr(value, field)
        expected_value = getattr(expected, field)
        if type(candidate_value) is not type(expected_value) or candidate_value != expected_value:
            raise ValueError("improvement analyzer draft is invalid")
    return expected


def _validated_create_result(value: object) -> ProposalCreateResult:
    if type(value) is not ProposalCreateResult:
        raise ValueError("improvement draft sink result is invalid")
    if type(value.inserted) is not bool or type(value.duplicate) is not bool:
        raise ValueError("improvement draft sink result is invalid")
    try:
        validated = ProposalCreateResult.model_validate(value.model_dump(mode="python"))
    except Exception:
        raise ValueError("improvement draft sink result is invalid") from None
    if validated != value or validated.inserted == validated.duplicate:
        raise ValueError("improvement draft sink result is invalid")
    return validated


def _report_value(
    report: ReconcileReport,
    timestamp: str,
    stage_metrics: dict[str, dict[str, int]],
    stage_errors: dict[str, str],
) -> dict[str, object]:
    safe_stages = [
        (key, value)
        for key, value in sorted(report.stages.items())
        if _safe_stage_name(key) and value in {"pass", "warn", "error"}
    ][:_REPORT_MAX_STAGES]
    allowed_stage_names = {key for key, _value in safe_stages}
    safe_metrics: dict[str, dict[str, int]] = {}
    for stage, metrics in sorted(stage_metrics.items()):
        if stage not in allowed_stage_names or not isinstance(metrics, dict):
            continue
        safe_metrics[stage] = {
            name: _safe_count(value)
            for name, value in sorted(metrics.items())
            if _safe_stage_name(name)
        }
    safe_errors = {
        stage: code
        for stage, code in sorted(stage_errors.items())
        if stage in allowed_stage_names and _safe_stage_name(code) and len(code) <= 64
    }
    return {
        "duplicate_count": _safe_count(report.duplicate_count),
        "inserted_count": _safe_count(report.inserted_count),
        "run_id": str(report.run_id).lower(),
        "stage_errors": safe_errors,
        "stage_metrics": safe_metrics,
        "stages": dict(safe_stages),
        "status": report.status,
        "timestamp": timestamp,
        "warning_count": _safe_count(report.warning_count),
    }


def _sequence_count(value: object) -> int:
    if isinstance(value, (str, bytes, bytearray)):
        return 0
    try:
        return _safe_count(len(value))  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_count(value: object) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, _MAX_COUNT)


def _add_count(left: int, right: int) -> int:
    return min(_safe_count(left) + _safe_count(right), _MAX_COUNT)


def _safe_stage_name(value: str) -> bool:
    return (
        bool(value)
        and len(value) <= 64
        and all(
            character.isascii() and (character.isalnum() or character == "_") for character in value
        )
    )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be text")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)
