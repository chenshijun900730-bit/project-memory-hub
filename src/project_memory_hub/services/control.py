from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

from starlette.datastructures import UploadFile

from project_memory_hub.config import AppConfig, ConfigConflictError, ConfigRevision
from project_memory_hub.discovery.policy import validate_project_root_scope
from project_memory_hub.domain import LifecycleState, Namespace, SourceAgent
from project_memory_hub.improvement.git_apply import GitProposalError
from project_memory_hub.improvement.models import ProposalRecord
from project_memory_hub.improvement.service import ProposalApplyBusy
from project_memory_hub.integration.automation import (
    AutomationInspector,
    DesiredAutomation,
    InstallationIdentity,
)
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ModelStatus,
    ProbeCapability,
    ProbeWarningCode,
    SourceProbeResult,
    StructureStatus,
)
from project_memory_hub.security.archive import ArchiveLimits
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.storage.projects import ProjectControlRecord
from project_memory_hub.storage.proposals import ProposalError

if TYPE_CHECKING:
    from project_memory_hub.container import ServiceContainer


_REGISTERED_SOURCES = (SourceAgent.CODEX, SourceAgent.CHATGPT)
_OPTIONAL_SOURCES = (
    SourceAgent.TRAE,
    SourceAgent.WORKBUDDY,
    SourceAgent.ZCODE,
    SourceAgent.QODERWORK,
    SourceAgent.CLAUDE_CODE,
)
_SOURCE_LABELS = {
    SourceAgent.CODEX: "Codex",
    SourceAgent.CHATGPT: "ChatGPT",
    SourceAgent.TRAE: "Trae",
    SourceAgent.WORKBUDDY: "WorkBuddy",
    SourceAgent.ZCODE: "Zcode",
    SourceAgent.QODERWORK: "QoderWork",
    SourceAgent.CLAUDE_CODE: "Claude Code",
}
_OPTIONAL_CAPABILITIES = {
    SourceAgent.TRAE: ProbeCapability.STRUCTURE_METADATA,
    SourceAgent.WORKBUDDY: ProbeCapability.PRESENCE_AND_ACCESS,
    SourceAgent.ZCODE: ProbeCapability.PRESENCE_AND_ACCESS,
    SourceAgent.QODERWORK: ProbeCapability.PRESENCE_AND_ACCESS,
    SourceAgent.CLAUDE_CODE: ProbeCapability.PRESENCE_AND_ACCESS,
}
_INSTALLATION_VIEWS: dict[
    InstallationStatus,
    tuple[str, Literal["detected", "not-detected"]],
] = {
    InstallationStatus.DETECTED: ("Detected", "detected"),
    InstallationStatus.NOT_DETECTED: ("Not detected", "not-detected"),
}
_DATA_VIEWS: dict[
    DataStatus,
    tuple[str, Literal["readable", "blocked", "missing", "rejected"]],
] = {
    DataStatus.READABLE: ("Readable", "readable"),
    DataStatus.BLOCKED: ("Permission blocked", "blocked"),
    DataStatus.MISSING: ("Missing", "missing"),
    DataStatus.REJECTED: ("Rejected", "rejected"),
}
_MODEL_VIEWS: dict[
    ModelStatus,
    tuple[str, Literal["not-checked", "unverifiable"]],
] = {
    ModelStatus.NOT_CHECKED: ("Not checked", "not-checked"),
    ModelStatus.UNVERIFIABLE: ("Unverifiable", "unverifiable"),
}
_CAPABILITY_LABELS = {
    ProbeCapability.PRESENCE_AND_ACCESS: "Presence and access check",
    ProbeCapability.STRUCTURE_METADATA: "Structure metadata check",
}
_STRUCTURE_LABELS = {
    StructureStatus.NOT_RUN: "Not run",
    StructureStatus.RECOGNIZED: "Recognized",
    StructureStatus.PARTIAL: "Partial",
    StructureStatus.UNSUPPORTED: "Unsupported",
}
_WARNING_LABELS = {
    ProbeWarningCode.SOURCE_MISSING: "source_missing",
    ProbeWarningCode.PERMISSION_BLOCKED: "permission_blocked",
    ProbeWarningCode.SYMLINK_REJECTED: "symlink_rejected",
    ProbeWarningCode.UNSAFE_FILE_TYPE: "unsafe_file_type",
    ProbeWarningCode.UNSUPPORTED_FORMAT: "unsupported_format",
    ProbeWarningCode.MALFORMED_METADATA: "malformed_metadata",
    ProbeWarningCode.INVALID_UTF8: "invalid_utf8",
    ProbeWarningCode.BUDGET_EXCEEDED: "budget_exceeded",
    ProbeWarningCode.PROBE_TIMEOUT: "probe_timeout",
    ProbeWarningCode.SOURCE_CHANGED: "source_changed",
    ProbeWarningCode.MODEL_ID_UNVERIFIABLE: "model_id_unverifiable",
    ProbeWarningCode.PROBE_BUSY: "probe_busy",
    ProbeWarningCode.PROBE_FAILED: "probe_failed",
}
_PROBE_ERROR_VIEWS: dict[
    Literal["probe_busy"],
    tuple[str, Literal["probe-busy"], ProbeWarningCode],
] = {
    "probe_busy": ("Probe busy", "probe-busy", ProbeWarningCode.PROBE_BUSY),
}
_TIME_PATTERN = re.compile(r"(?P<hour>[01][0-9]|2[0-3]):(?P<minute>[0-5][0-9])\Z")
_SAFE_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})\Z"
)
_UPLOAD_CHUNK = 64 * 1024
_MAX_PROJECT_ROOTS = 32
_MAX_PROJECT_ROOT_CHARS = 4096
_MAX_METADATA_CHARS = 200
_MAX_MEMORY_CONTENT_CHARS = 4000
_MAX_PROPOSAL_DESCRIPTION_CHARS = 600
_MAX_PROPOSAL_SUMMARY_CHARS = 600
_CAPABLE_PROPOSAL_ORIGINS = frozenset({"local_cli", "codex_task", "control_panel"})


class ControlInputError(ValueError):
    """A stable invalid local-control request."""


class UnavailableSourceError(ControlInputError):
    """The source has no registered implementation in this release."""


@dataclass(frozen=True, slots=True)
class SourceProbeControlRecord:
    detected_label: str
    detected_class: Literal["detected", "not-detected"]
    health_label: str
    health_class: Literal["readable", "blocked", "missing", "rejected", "probe-busy"]
    model_label: str
    model_class: Literal["not-checked", "unverifiable"]
    capability_label: str
    structure_label: str
    warning_codes: tuple[str, ...]
    behavior_import_locked: bool
    behavior_class: Literal["locked"]
    can_run_structure: bool


@dataclass(frozen=True, slots=True)
class SourceControlRecord:
    source_agent: SourceAgent
    label: str
    enabled: bool
    available: bool
    status: str
    runtime_status: str
    probe: SourceProbeControlRecord | None


@dataclass(frozen=True, slots=True)
class OverviewSnapshot:
    project_count: int
    fact_count: int
    behavior_count: int
    permission_error_count: int
    pending_confirmation_count: int
    last_reconcile_success: str
    recall_size: str = "not recorded"
    last_discovery: str = "not recorded"
    last_compaction: str = "not recorded"


@dataclass(frozen=True, slots=True)
class ProposalMetadata:
    proposal_id: UUID
    title: str
    description: str
    risk: str
    status: str
    origin: str
    created_at: str
    updated_at: str
    patch_bytes: int
    patch_sha256: str | None
    verification_labels: tuple[str, ...]
    verification_summary: str
    review_complete: bool
    can_approve: bool
    can_reject: bool
    can_apply: bool
    can_recover: bool
    can_rollback: bool


@dataclass(frozen=True, slots=True)
class PendingPromotionMetadata:
    promotion_id: UUID
    project_id: UUID
    source_agent: str
    model_id: str
    proposed_rule: str
    requested_at: str
    approvable: bool


@dataclass(frozen=True, slots=True)
class SharedFactMetadata:
    fact_id: UUID
    category: str
    content: str
    evidence_type: str
    evidence_reference: str
    observed_at: str
    confidence: float


@dataclass(frozen=True, slots=True)
class BehaviorMemoryMetadata:
    memory_id: UUID
    project_id: UUID
    source_agent: str
    model_id: str
    memory_kind: str
    lifecycle_state: str
    lifecycle_label: str
    normalized_content: str
    created_at: str
    confidence: float
    actions_allowed: bool


@dataclass(frozen=True, slots=True)
class DiscoveryIssueMetadata:
    path: str
    code: str
    affected_capability: str
    remediation: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class DuplicateCandidateMetadata:
    fingerprint_kind: str
    candidate_paths: tuple[str, ...]
    observed_at: str


@dataclass(frozen=True, slots=True)
class DiscoveryHealthSnapshot:
    issues: tuple[DiscoveryIssueMetadata, ...]
    duplicates: tuple[DuplicateCandidateMetadata, ...]


class ControlPanelService:
    def __init__(self, container: ServiceContainer) -> None:
        self._container = container

    def overview(self) -> OverviewSnapshot:
        with self._container.database.connect(readonly=True) as connection:
            project_count = _count(connection, "projects")
            fact_count = _count(connection, "project_facts")
            behavior_count = _count(connection, "behavior_memories")
            permission_errors = connection.execute(
                """
                select count(*) from (
                    select canonical_path as path
                    from projects where permission_status <> 'ok'
                    union
                    select path from discovery_issues
                    where code = 'blocked_permission'
                )
                """
            ).fetchone()[0]
            pending = connection.execute(
                """
                select count(*) from app_state
                where name like 'chatgpt_confirmation:%'
                """
            ).fetchone()[0]
            row = connection.execute(
                "select value_json from app_state where name = 'last_reconcile_success'"
            ).fetchone()
        return OverviewSnapshot(
            project_count=project_count,
            fact_count=fact_count,
            behavior_count=behavior_count,
            permission_error_count=int(permission_errors),
            pending_confirmation_count=int(pending),
            last_reconcile_success=_safe_reconcile_timestamp(row),
        )

    def sources(
        self,
        probe_results: Sequence[SourceProbeResult],
        *,
        probe_error: Literal["probe_busy"] | None = None,
    ) -> tuple[SourceControlRecord, ...]:
        enabled = frozenset(self._container.config_manager.load().enabled_sources)
        runtime_enabled = frozenset(self._container.config.enabled_sources)
        registered = frozenset(_REGISTERED_SOURCES)
        probes_by_source = _index_probe_results(probe_results)
        return tuple(
            SourceControlRecord(
                source_agent=source,
                label=_SOURCE_LABELS[source],
                enabled=source in enabled and source in registered,
                available=source in registered,
                status=(
                    "Enabled"
                    if source in enabled and source in registered
                    else "Disabled"
                    if source in registered
                    else "Unavailable"
                ),
                runtime_status=(
                    "Enabled"
                    if source in runtime_enabled and source in registered
                    else "Disabled"
                    if source in registered
                    else "Unavailable"
                ),
                probe=(
                    None
                    if source in registered
                    else _probe_control_record(
                        source,
                        probes_by_source.get(source),
                        probe_error=probe_error,
                    )
                ),
            )
            for source in SourceAgent
        )

    def set_source_enabled(self, source: SourceAgent, enabled: bool) -> AppConfig:
        selected = SourceAgent(source)
        if selected not in _REGISTERED_SOURCES:
            raise UnavailableSourceError("source implementation unavailable")
        current, revision = self._container.config_manager.load_with_revision()
        enabled_set = set(current.enabled_sources)
        if enabled:
            enabled_set.add(selected)
        else:
            enabled_set.discard(selected)
            if not enabled_set.intersection(_REGISTERED_SOURCES):
                raise ControlInputError("at least one registered source is required")
        updated = AppConfig(
            project_roots=current.project_roots,
            enabled_sources=tuple(
                source for source in _REGISTERED_SOURCES if source in enabled_set
            ),
            inactive_days=current.inactive_days,
            max_recall_tokens=current.max_recall_tokens,
            daily_reconcile_time=current.daily_reconcile_time,
            setup_completed=current.setup_completed,
            codex_project_id=current.codex_project_id,
            improvement_repository_root=current.improvement_repository_root,
            improvement_verification_commands=(current.improvement_verification_commands),
        )
        try:
            self._container.config_manager.save(updated, expected_revision=revision)
        except ConfigConflictError:
            raise ControlInputError("configuration changed") from None
        return updated

    def projects(self) -> tuple[ProjectControlRecord, ...]:
        return self._container.projects.list_control()

    def discovery_health(self) -> DiscoveryHealthSnapshot:
        snapshot = self._container.discovery_findings.snapshot()
        return DiscoveryHealthSnapshot(
            issues=tuple(
                DiscoveryIssueMetadata(
                    path=_bounded_metadata(str(issue.path), self._container.redactor),
                    code=issue.code,
                    affected_capability=issue.affected_capability,
                    remediation=_bounded_metadata(issue.remediation, self._container.redactor),
                    observed_at=_bounded_metadata(issue.observed_at, self._container.redactor),
                )
                for issue in snapshot.issues
            ),
            duplicates=tuple(
                DuplicateCandidateMetadata(
                    fingerprint_kind=duplicate.fingerprint_kind,
                    candidate_paths=tuple(
                        _bounded_metadata(str(path), self._container.redactor)
                        for path in duplicate.candidate_paths
                    ),
                    observed_at=_bounded_metadata(duplicate.observed_at, self._container.redactor),
                )
                for duplicate in snapshot.duplicates
            ),
        )

    def set_project_enabled(self, project_id: UUID, enabled: bool) -> None:
        self._container.projects.set_enabled(project_id, enabled)

    def relink_project(self, project_id: UUID, new_path: str) -> None:
        path = _validated_root(new_path)
        self._container.projects.relink(project_id, path)

    def memories(
        self, project_id: UUID, namespace: Namespace
    ) -> tuple[BehaviorMemoryMetadata, ...]:
        self._container.projects.get(project_id)
        namespace_safe = _namespace_is_exact_safe(namespace, self._container.redactor)
        memories = self._container.memories.list_scoped(project_id, namespace, limit=100)
        resolved_target_ids: frozenset[UUID] = frozenset()
        if memories:
            with self._container.database.connect(readonly=True) as connection:
                resolved_target_ids = self._container.issue_resolutions.resolved_target_ids_scoped(
                    connection,
                    project_id=project_id,
                    namespace=namespace,
                    memory_ids=tuple(memory.memory_id for memory in memories),
                )
        return tuple(
            BehaviorMemoryMetadata(
                memory_id=memory.memory_id,
                project_id=memory.project_id,
                source_agent=_bounded_metadata(
                    memory.namespace.source_agent.value, self._container.redactor
                ),
                model_id=_bounded_metadata(memory.namespace.model_id, self._container.redactor),
                memory_kind=memory.memory_kind.value,
                lifecycle_state=memory.lifecycle_state.value,
                lifecycle_label=(
                    "Resolved"
                    if memory.lifecycle_state == LifecycleState.ARCHIVED
                    and memory.memory_id in resolved_target_ids
                    else memory.lifecycle_state.value.title()
                ),
                normalized_content=_bounded_display_text(
                    memory.normalized_content,
                    self._container.redactor,
                    max_chars=_MAX_MEMORY_CONTENT_CHARS,
                ),
                created_at=_bounded_metadata(
                    memory.created_at.isoformat(), self._container.redactor
                ),
                confidence=memory.confidence,
                actions_allowed=(
                    namespace_safe
                    and _namespace_is_exact_safe(memory.namespace, self._container.redactor)
                ),
            )
            for memory in memories
        )

    def display_model_id(self, model_id: str | None) -> str:
        if model_id is None or not model_id:
            return ""
        return _bounded_metadata(model_id, self._container.redactor)

    def shared_facts(self, project_id: UUID) -> tuple[SharedFactMetadata, ...]:
        self._container.projects.get(project_id)
        return tuple(
            SharedFactMetadata(
                fact_id=fact.fact_id,
                category=_bounded_metadata(fact.category, self._container.redactor),
                content=_bounded_metadata(fact.normalized_content, self._container.redactor),
                evidence_type=_bounded_metadata(fact.evidence_type, self._container.redactor),
                evidence_reference=_bounded_metadata(
                    fact.evidence_reference, self._container.redactor
                ),
                observed_at=_bounded_metadata(
                    fact.observed_at.isoformat(), self._container.redactor
                ),
                confidence=fact.confidence,
            )
            for fact in self._container.facts.search(project_id, "", 100)
        )

    def archive_memory(
        self,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
        confirmation: str,
    ) -> None:
        self._require_safe_namespace(namespace)
        if confirmation != "ARCHIVE":
            raise ControlInputError("archive confirmation required")
        self._container.memories.set_lifecycle_scoped(
            project_id, namespace, memory_id, LifecycleState.ARCHIVED
        )

    def delete_memory(
        self,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
        confirmation: str,
    ) -> None:
        self._require_safe_namespace(namespace)
        if confirmation != "DELETE":
            raise ControlInputError("delete confirmation required")
        self._container.memories.set_lifecycle_scoped(
            project_id, namespace, memory_id, LifecycleState.DELETED
        )

    def request_promotion(
        self,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
        proposed_rule: str,
    ) -> None:
        self._require_safe_namespace(namespace)
        self._container.promotions.request_scoped(project_id, namespace, memory_id, proposed_rule)

    def approve_promotion(
        self,
        project_id: UUID,
        namespace: Namespace,
        promotion_id: UUID,
        confirmation: str,
    ) -> None:
        self._require_safe_namespace(namespace)
        if confirmation != "APPROVE":
            raise ControlInputError("approval confirmation required")
        self._container.promotions.approve_scoped(
            project_id,
            namespace,
            promotion_id,
            "local-control-panel",
        )

    def proposals(self) -> tuple[ProposalMetadata, ...]:
        records: list[ProposalMetadata] = []
        for summary in self._container.proposal_service.list_summaries(limit=100):
            try:
                proposal = self._container.proposal_service.get(summary.proposal_id)
            except (KeyError, ProposalError):
                continue
            records.append(self._proposal_metadata(proposal))
        return tuple(records)

    def approve_proposal(self, proposal_id: UUID, confirmation: str) -> None:
        self._require_proposal_confirmation(confirmation, "APPROVE")
        try:
            proposal = self._container.proposal_service.get(proposal_id)
            self._require_proposal_action(proposal, "approve")
            self._container.proposal_service.approve(
                proposal_id,
                actor="local-control-panel",
            )
        except KeyError:
            raise
        except (ControlInputError, GitProposalError, ProposalApplyBusy, ProposalError):
            raise ControlInputError("proposal action rejected") from None

    def reject_proposal(self, proposal_id: UUID, confirmation: str) -> None:
        self._require_proposal_confirmation(confirmation, "REJECT")
        try:
            proposal = self._container.proposal_service.get(proposal_id)
            self._require_proposal_action(proposal, "reject")
            self._container.proposal_service.reject(proposal_id)
        except KeyError:
            raise
        except (ControlInputError, GitProposalError, ProposalApplyBusy, ProposalError):
            raise ControlInputError("proposal action rejected") from None

    def apply_proposal(self, proposal_id: UUID, confirmation: str) -> None:
        self._require_proposal_confirmation(confirmation, "APPLY")
        try:
            proposal = self._container.proposal_service.get(proposal_id)
            action = "recover" if proposal.status == "applying" else "apply"
            self._require_proposal_action(proposal, action)
            self._container.proposal_service.apply(proposal_id)
        except KeyError:
            raise
        except (ControlInputError, GitProposalError, ProposalApplyBusy, ProposalError):
            raise ControlInputError("proposal action rejected") from None

    def rollback_proposal(self, proposal_id: UUID, confirmation: str) -> None:
        self._require_proposal_confirmation(confirmation, "ROLLBACK")
        try:
            proposal = self._container.proposal_service.get(proposal_id)
            self._require_proposal_action(proposal, "rollback")
            self._container.proposal_service.rollback(proposal_id)
        except KeyError:
            raise
        except (ControlInputError, GitProposalError, ProposalApplyBusy, ProposalError):
            raise ControlInputError("proposal action rejected") from None

    def _proposal_metadata(self, proposal: ProposalRecord) -> ProposalMetadata:
        title = _bounded_metadata(proposal.title, self._container.redactor)
        description = _bounded_display_text(
            proposal.description,
            self._container.redactor,
            max_chars=_MAX_PROPOSAL_DESCRIPTION_CHARS,
        )
        verification_summary = _bounded_display_text(
            proposal.verification_summary,
            self._container.redactor,
            max_chars=_MAX_PROPOSAL_SUMMARY_CHARS,
        )
        review_complete = (
            _review_text_complete(
                proposal.title,
                title,
                self._container.redactor,
            )
            and _review_text_complete(
                proposal.description,
                description,
                self._container.redactor,
            )
            and _review_text_complete(
                proposal.verification_summary,
                verification_summary,
                self._container.redactor,
                allow_blank=True,
            )
        )
        verification_labels = self._verification_labels(proposal)
        executable = (
            self._container.proposal_applier is not None
            and proposal.patch is not None
            and bool(verification_labels)
            and self._proposal_execution_metadata_consistent(proposal)
        )
        patch_bytes = len(proposal.patch.encode("utf-8")) if proposal.patch is not None else 0
        return ProposalMetadata(
            proposal_id=proposal.proposal_id,
            title=title,
            description=description,
            risk=proposal.risk,
            status=proposal.status,
            origin=proposal.origin,
            created_at=proposal.created_at.isoformat(),
            updated_at=proposal.updated_at.isoformat(),
            patch_bytes=patch_bytes,
            patch_sha256=(
                hashlib.sha256(proposal.patch.encode("utf-8")).hexdigest()
                if proposal.patch is not None
                else None
            ),
            verification_labels=verification_labels,
            verification_summary=verification_summary,
            review_complete=review_complete,
            can_approve=(
                review_complete
                and proposal.status == "draft"
                and proposal.origin in _CAPABLE_PROPOSAL_ORIGINS
            ),
            can_reject=(review_complete and proposal.status in {"draft", "approved"}),
            can_apply=(review_complete and proposal.status == "approved" and executable),
            can_recover=(review_complete and proposal.status == "applying" and executable),
            can_rollback=(review_complete and proposal.status == "applied" and executable),
        )

    def _verification_labels(self, proposal: ProposalRecord) -> tuple[str, ...]:
        return tuple(
            f"Configured check {index}"
            for index, command in enumerate(
                self._container.config.improvement_verification_commands,
                start=1,
            )
            if tuple(command) == proposal.verification_argv
        )

    def _proposal_execution_metadata_consistent(
        self,
        proposal: ProposalRecord,
    ) -> bool:
        if proposal.status not in {"applying", "applied", "rolled_back"}:
            return True
        configured_root = self._container.config.improvement_repository_root
        if configured_root is None or proposal.repository_root is None:
            return False
        try:
            root_matches = proposal.repository_root.resolve(strict=True) == configured_root.resolve(
                strict=True
            )
        except (OSError, RuntimeError):
            return False
        expected_branch = f"codex/memory-hub-proposal-{proposal.proposal_id.hex}"
        if (
            not root_matches
            or proposal.proposal_branch != expected_branch
            or proposal.original_branch == expected_branch
            or proposal.base_commit is None
        ):
            return False
        if proposal.status in {"applied", "rolled_back"}:
            return (
                proposal.applied_commit is not None
                and proposal.applied_commit != proposal.base_commit
            )
        return proposal.applied_commit is None

    def _require_proposal_action(self, proposal: ProposalRecord, action: str) -> None:
        metadata = self._proposal_metadata(proposal)
        allowed = {
            "approve": metadata.can_approve,
            "reject": metadata.can_reject,
            "apply": metadata.can_apply,
            "recover": metadata.can_recover,
            "rollback": metadata.can_rollback,
        }
        if action not in allowed or not allowed[action]:
            raise ControlInputError("proposal action unavailable")

    @staticmethod
    def _require_proposal_confirmation(value: str, expected: str) -> None:
        if value != expected:
            raise ControlInputError("proposal confirmation rejected")

    def pending_promotions(self) -> tuple[PendingPromotionMetadata, ...]:
        with self._container.database.connect(readonly=True) as connection:
            rows = connection.execute(
                """
                select promotion.promotion_id, promotion.proposed_rule,
                       promotion.requested_at, memory.project_id,
                       memory.source_agent, memory.model_id
                from memory_promotions as promotion
                join behavior_memories as memory
                  on memory.memory_id = promotion.memory_id
                where promotion.status = 'pending'
                order by promotion.requested_at, promotion.promotion_id
                limit 100
                """
            ).fetchall()
        records = []
        for row in rows:
            display_source_agent = _bounded_metadata(row["source_agent"], self._container.redactor)
            display_model_id = _bounded_metadata(row["model_id"], self._container.redactor)
            display_rule = _bounded_metadata(row["proposed_rule"], self._container.redactor)
            display_requested_at = _bounded_metadata(row["requested_at"], self._container.redactor)
            exact_source_agent = _exact_safe_metadata(row["source_agent"], self._container.redactor)
            try:
                source_agent_is_known = (
                    SourceAgent(exact_source_agent).value == display_source_agent
                    if exact_source_agent is not None
                    else False
                )
            except ValueError:
                source_agent_is_known = False
            records.append(
                PendingPromotionMetadata(
                    promotion_id=UUID(row["promotion_id"]),
                    project_id=UUID(row["project_id"]),
                    source_agent=display_source_agent,
                    model_id=display_model_id,
                    proposed_rule=display_rule,
                    requested_at=display_requested_at,
                    approvable=(
                        source_agent_is_known
                        and _exact_safe_metadata(row["model_id"], self._container.redactor)
                        == display_model_id
                        and _exact_safe_metadata(row["proposed_rule"], self._container.redactor)
                        == display_rule
                        and _exact_safe_metadata(row["requested_at"], self._container.redactor)
                        == display_requested_at
                    ),
                )
            )
        return tuple(records)

    def _require_safe_namespace(self, namespace: Namespace) -> None:
        if not _namespace_is_exact_safe(namespace, self._container.redactor):
            raise ControlInputError("unsafe namespace metadata")

    def settings(self) -> AppConfig:
        return self._container.config_manager.load()

    def automation_status(self) -> str:
        try:
            identity = InstallationIdentity.discover()
            if identity is None:
                return "drifted"
            config = self._container.config_manager.load()
            desired = DesiredAutomation.daily_reconcile(
                local_time=config.daily_reconcile_time,
                repository_root=identity.repository_root,
                launcher=identity.launcher,
                project_id=config.codex_project_id,
            )
            inspection = AutomationInspector(Path.home() / ".codex" / "automations").inspect(
                desired
            )
            return inspection.status
        except Exception:
            return "drifted"

    def save_settings(
        self,
        *,
        project_roots: list[str],
        enabled_sources: list[str],
        inactive_days: str,
        max_recall_tokens: str,
        daily_reconcile_time: str,
        setup_completed: bool | None = None,
        expected_revision: ConfigRevision | None = None,
    ) -> AppConfig:
        if not 1 <= len(project_roots) <= _MAX_PROJECT_ROOTS:
            raise ControlInputError("project roots are invalid")
        roots = tuple(_validated_root(value) for value in project_roots)
        if len(set(roots)) != len(roots):
            raise ControlInputError("project roots are invalid")
        try:
            days = int(inactive_days)
            tokens = int(max_recall_tokens)
        except (TypeError, ValueError):
            raise ControlInputError("numeric settings are invalid") from None
        if not 1 <= days <= 3650 or not 128 <= tokens <= 800:
            raise ControlInputError("numeric settings are out of range")
        if _TIME_PATTERN.fullmatch(daily_reconcile_time) is None:
            raise ControlInputError("daily reconcile time is invalid")
        try:
            selected_sources = tuple(SourceAgent(value) for value in enabled_sources)
        except ValueError:
            raise UnavailableSourceError("source implementation unavailable") from None
        if (
            not selected_sources
            or len(set(selected_sources)) != len(selected_sources)
            or any(source not in _REGISTERED_SOURCES for source in selected_sources)
        ):
            raise UnavailableSourceError("source implementation unavailable")
        selected = frozenset(selected_sources)
        current, revision = self._container.config_manager.load_with_revision()
        config = AppConfig(
            project_roots=roots,
            enabled_sources=tuple(source for source in _REGISTERED_SOURCES if source in selected),
            inactive_days=days,
            max_recall_tokens=tokens,
            daily_reconcile_time=daily_reconcile_time,
            setup_completed=(
                current.setup_completed if setup_completed is None else setup_completed
            ),
            codex_project_id=current.codex_project_id,
            improvement_repository_root=current.improvement_repository_root,
            improvement_verification_commands=(current.improvement_verification_commands),
        )
        if expected_revision is not None and revision != expected_revision:
            if config == current:
                return current
            raise ControlInputError("configuration changed")
        try:
            self._container.config_manager.save(config, expected_revision=revision)
        except ConfigConflictError:
            raise ControlInputError("configuration changed") from None
        return config

    async def import_chatgpt(
        self,
        upload: UploadFile,
        *,
        dry_run: bool,
    ) -> Any:
        if SourceAgent.CHATGPT not in self._container.config.enabled_sources:
            raise UnavailableSourceError("source is disabled")
        upload_dir = self._container.paths.imports / "web-uploads"
        _ensure_private_directory(upload_dir)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".chatgpt-",
            suffix=".zip",
            dir=upload_dir,
        )
        temporary_path = Path(temporary_name)
        total = 0
        limits = ArchiveLimits()
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise PermissionError("temporary import file rejected")
            with os.fdopen(descriptor, "wb") as output:
                descriptor = -1
                while True:
                    chunk = await upload.read(_UPLOAD_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limits.max_total_bytes:
                        raise ControlInputError("archive exceeds upload limit")
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if total == 0:
                raise ControlInputError("archive is empty")
            return await asyncio.to_thread(
                self._container.chatgpt_adapter.import_zip,
                temporary_path,
                dry_run=dry_run,
            )
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                await upload.close()
            finally:
                temporary_path.unlink(missing_ok=True)


def _index_probe_results(
    probe_results: Sequence[SourceProbeResult],
) -> dict[SourceAgent, SourceProbeResult]:
    indexed: dict[SourceAgent, SourceProbeResult] = {}
    duplicates: set[SourceAgent] = set()
    for result in probe_results:
        source = result.source_agent
        if source not in _OPTIONAL_SOURCES or source in duplicates:
            continue
        if source in indexed:
            indexed.pop(source)
            duplicates.add(source)
            continue
        indexed[source] = result
    return indexed


def _probe_control_record(
    source: SourceAgent,
    result: SourceProbeResult | None,
    *,
    probe_error: Literal["probe_busy"] | None,
) -> SourceProbeControlRecord:
    if source not in _OPTIONAL_SOURCES:
        raise ValueError("probe control source is not optional")
    if probe_error is not None and probe_error not in _PROBE_ERROR_VIEWS:
        raise ValueError("unsupported probe control error")
    if probe_error == "probe_busy" and source is SourceAgent.TRAE:
        return _fallback_probe_control_record(
            source,
            probe_error=probe_error,
        )
    if result is None:
        return _fallback_probe_control_record(
            source,
            warning_codes=(() if probe_error is not None else (ProbeWarningCode.PROBE_FAILED,)),
        )

    expected_capability = _OPTIONAL_CAPABILITIES[source]
    try:
        if result.source_agent is not source or result.capability is not expected_capability:
            raise KeyError("probe result contract mismatch")
        detected_label, detected_class = _INSTALLATION_VIEWS[result.installation_status]
        health_label, health_class = _DATA_VIEWS[result.data_status]
        model_label, model_class = _MODEL_VIEWS[result.model_status]
        structure_label = _STRUCTURE_LABELS[result.structure_status]
        warning_codes = tuple(
            sorted({_WARNING_LABELS[warning] for warning in result.warning_codes})
        )
    except (AttributeError, KeyError, TypeError):
        return _fallback_probe_control_record(
            source,
            warning_codes=(ProbeWarningCode.PROBE_FAILED,),
        )

    return SourceProbeControlRecord(
        detected_label=detected_label,
        detected_class=detected_class,
        health_label=health_label,
        health_class=health_class,
        model_label=model_label,
        model_class=model_class,
        capability_label=_CAPABILITY_LABELS[expected_capability],
        structure_label=structure_label,
        warning_codes=warning_codes,
        behavior_import_locked=True,
        behavior_class="locked",
        can_run_structure=(
            source is SourceAgent.TRAE and result.data_status is DataStatus.READABLE
        ),
    )


def _fallback_probe_control_record(
    source: SourceAgent,
    *,
    probe_error: Literal["probe_busy"] | None = None,
    warning_codes: tuple[ProbeWarningCode, ...] = (),
) -> SourceProbeControlRecord:
    capability = _OPTIONAL_CAPABILITIES[source]
    detected_label, detected_class = _INSTALLATION_VIEWS[InstallationStatus.NOT_DETECTED]
    model_label, model_class = _MODEL_VIEWS[ModelStatus.NOT_CHECKED]
    health_label: str
    health_class: Literal["missing", "probe-busy"]
    if probe_error is None:
        health_label, mapped_health_class = _DATA_VIEWS[DataStatus.MISSING]
        if mapped_health_class != "missing":
            raise RuntimeError("missing probe view mapping is invalid")
        health_class = mapped_health_class
    else:
        health_label, health_class, error_warning = _PROBE_ERROR_VIEWS[probe_error]
        warning_codes = (*warning_codes, error_warning)
    return SourceProbeControlRecord(
        detected_label=detected_label,
        detected_class=detected_class,
        health_label=health_label,
        health_class=health_class,
        model_label=model_label,
        model_class=model_class,
        capability_label=_CAPABILITY_LABELS[capability],
        structure_label=_STRUCTURE_LABELS[StructureStatus.NOT_RUN],
        warning_codes=tuple(sorted({_WARNING_LABELS[warning] for warning in warning_codes})),
        behavior_import_locked=True,
        behavior_class="locked",
        can_run_structure=False,
    )


def _count(connection: sqlite3.Connection, table: str) -> int:
    if table not in {"projects", "project_facts", "behavior_memories"}:
        raise ValueError("unsupported count")
    return int(connection.execute(f"select count(*) from {table}").fetchone()[0])


def _safe_reconcile_timestamp(row: sqlite3.Row | None) -> str:
    if row is None:
        return "not recorded"
    try:
        value = json.loads(row["value_json"])
    except (TypeError, json.JSONDecodeError):
        return "not recorded"
    if not isinstance(value, dict) or set(value) != {"timestamp"}:
        return "not recorded"
    timestamp = value["timestamp"]
    if not isinstance(timestamp, str) or _SAFE_TIMESTAMP.fullmatch(timestamp) is None:
        return "not recorded"
    return timestamp


def _validated_root(value: str) -> Path:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_PROJECT_ROOT_CHARS
        or any(character in value for character in ("\x00", "\r", "\n"))
    ):
        raise ControlInputError("project root is invalid")
    selected = Path(value.strip()).expanduser()
    if not selected.is_absolute():
        raise ControlInputError("project root must be absolute")
    current = Path(selected.anchor)
    for component in selected.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except OSError:
            raise ControlInputError("project root does not exist") from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ControlInputError("project root symlink rejected")
    try:
        resolved = selected.resolve(strict=True)
    except (OSError, RuntimeError):
        raise ControlInputError("project root does not exist") from None
    if not resolved.is_dir():
        raise ControlInputError("project root is not a directory")
    try:
        return validate_project_root_scope(resolved)
    except ValueError:
        raise ControlInputError("project root is too broad") from None


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise PermissionError("private upload directory rejected")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        path.chmod(0o700)


def _bounded_metadata(value: object, redactor: Redactor) -> str:
    return _bounded_display_text(value, redactor, max_chars=_MAX_METADATA_CHARS)


def _bounded_display_text(value: object, redactor: Redactor, *, max_chars: int) -> str:
    if not isinstance(value, str):
        return "not recorded"
    normalized = " ".join(redactor.redact(value).text.split())
    if not normalized:
        return "not recorded"
    return normalized[:max_chars]


def _review_text_complete(
    value: str,
    displayed: str,
    redactor: Redactor,
    *,
    allow_blank: bool = False,
) -> bool:
    if not value:
        return allow_blank
    result = redactor.redact(value)
    return not result.findings and result.text == value and displayed == value


def _exact_safe_metadata(value: object, redactor: Redactor) -> str | None:
    if not isinstance(value, str) or not value or len(value) > _MAX_METADATA_CHARS:
        return None
    result = redactor.redact(value)
    if result.text != value or " ".join(value.split()) != value:
        return None
    return value


def _namespace_is_exact_safe(namespace: Namespace, redactor: Redactor) -> bool:
    source_agent = namespace.source_agent.value
    return (
        _exact_safe_metadata(source_agent, redactor) == source_agent
        and _exact_safe_metadata(namespace.model_id, redactor) == namespace.model_id
    )
