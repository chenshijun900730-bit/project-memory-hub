from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

from project_memory_hub.improvement.git_apply import (
    GitProposalApplier,
    GitProposalError,
    GitProposalRecoveryRequired,
)
from project_memory_hub.improvement.models import (
    ApplyResult,
    ProposalCreateResult,
    ProposalDraft,
    ProposalRecord,
    ProposalSummary,
)
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.storage.proposals import ProposalRepository


class ProposalApplyBusy(RuntimeError):
    """Another local proposal mutation currently owns the apply lock."""


class ProposalExecutionUnavailable(GitProposalError):
    """Proposal execution is not configured for this local installation."""


@dataclass(frozen=True, slots=True)
class ProposalCreatePreview:
    draft: ProposalDraft
    duplicate: ProposalRecord | None
    complete: bool
    unverified: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProposalActionPreview:
    record: ProposalRecord
    complete: bool
    mode: Literal["approve", "reject", "apply", "recovery", "rollback"]
    unverified: tuple[str, ...] = ()


class ProposalService:
    def __init__(
        self,
        proposals: ProposalRepository,
        applier: GitProposalApplier | None,
        apply_lock: ProcessLock,
    ) -> None:
        self._proposals = proposals
        self._applier = applier
        self._lock = apply_lock

    def list_summaries(self, *, limit: int = 100) -> tuple[ProposalSummary, ...]:
        return self._proposals.list_summaries(limit=limit)

    def get(self, proposal_id: UUID) -> ProposalRecord:
        return self._proposals.get(_proposal_id(proposal_id))

    def create(self, draft: ProposalDraft) -> ProposalCreateResult:
        return self._proposals.create(draft)

    def preview_create(self, draft: ProposalDraft) -> ProposalCreatePreview:
        prepared, duplicate = self._proposals.preview_create(draft)
        return ProposalCreatePreview(
            prepared,
            duplicate,
            False,
            ("database_write_boundary", "state_race"),
        )

    def preview_action(
        self,
        proposal_id: UUID,
        *,
        action: Literal["approve", "reject", "apply", "rollback"],
    ) -> ProposalActionPreview:
        selected = _proposal_id(proposal_id)
        if action == "approve":
            return ProposalActionPreview(
                self._proposals.preview_approve(selected),
                False,
                "approve",
                ("database_write_boundary", "state_race"),
            )
        if action == "reject":
            return ProposalActionPreview(
                self._proposals.preview_reject(selected),
                False,
                "reject",
                ("database_write_boundary", "state_race"),
            )
        if action not in {"apply", "rollback"}:
            raise ValueError("proposal preview action is invalid")

        if self._applier is None:
            raise ProposalExecutionUnavailable("proposal execution unavailable")
        if action == "apply":
            record = self._proposals.preview_apply(selected)
            unverified: tuple[str, ...]
            if record.status == "applying":
                self._applier.preview_recover(record)
                mode: Literal["apply", "recovery"] = "recovery"
                unverified = (
                    "commit_tree",
                    "cleanup_write_boundary",
                    "database_write_boundary",
                    "lock_race",
                )
            else:
                self._applier.preflight(record)
                mode = "apply"
                unverified = (
                    "database_write_boundary",
                    "git_write_boundary",
                    "lock_race",
                    "verification_command_execution",
                )
            return ProposalActionPreview(record, False, mode, unverified)

        record = self._proposals.preview_rollback(selected)
        self._applier.preview_rollback(record)
        return ProposalActionPreview(
            record,
            False,
            "rollback",
            (
                "commit_tree",
                "database_write_boundary",
                "lock_race",
            ),
        )

    def approve(self, proposal_id: UUID, *, actor: str) -> ProposalRecord:
        return self._proposals.approve(_proposal_id(proposal_id), actor=actor)

    def reject(self, proposal_id: UUID) -> ProposalRecord:
        selected = _proposal_id(proposal_id)
        record = self._proposals.get(selected)
        expected_status: Literal["draft", "approved"] = (
            "approved" if record.status == "approved" else "draft"
        )
        return self._proposals.reject(selected, expected_status=expected_status)

    def apply(self, proposal_id: UUID) -> ApplyResult:
        selected = _proposal_id(proposal_id)
        if self._applier is None:
            raise ProposalExecutionUnavailable("proposal execution unavailable")
        with self._lock.acquire() as outcome:
            if not outcome.acquired:
                raise ProposalApplyBusy("proposal apply is already running")
            record = self._proposals.get(selected)
            if record.status == "applying":
                try:
                    result = self._applier.recover(record)
                except GitProposalRecoveryRequired:
                    raise
                except GitProposalError:
                    self._proposals.mark_failed(
                        selected,
                        apply_attempt_id=_attempt_id(record),
                        failure_code="git_apply_failed",
                        verification_summary="verification failed",
                    )
                    raise
                self._proposals.mark_applied(
                    selected,
                    apply_attempt_id=_attempt_id(record),
                    applied_commit=result.applied_commit,
                    verification_summary=result.verification_summary,
                )
                return result
            if record.status != "approved":
                raise GitProposalError("proposal state rejected")

            plan = self._applier.preflight(record)
            attempt = uuid4()
            applying = self._proposals.begin_apply(
                selected,
                apply_attempt_id=attempt,
                repository_root=plan.repository_root,
                original_branch=plan.original_branch,
                base_commit=plan.base_commit,
                proposal_branch=plan.proposal_branch,
            )
            try:
                result = self._applier.apply(applying)
            except GitProposalRecoveryRequired:
                raise
            except GitProposalError:
                self._proposals.mark_failed(
                    selected,
                    apply_attempt_id=attempt,
                    failure_code="git_apply_failed",
                    verification_summary="verification failed",
                )
                raise
            self._proposals.mark_applied(
                selected,
                apply_attempt_id=attempt,
                applied_commit=result.applied_commit,
                verification_summary=result.verification_summary,
            )
            return result

    def rollback(self, proposal_id: UUID) -> ProposalRecord:
        selected = _proposal_id(proposal_id)
        if self._applier is None:
            raise ProposalExecutionUnavailable("proposal execution unavailable")
        with self._lock.acquire() as outcome:
            if not outcome.acquired:
                raise ProposalApplyBusy("proposal apply is already running")
            record = self._proposals.get(selected)
            if record.status != "applied":
                raise GitProposalError("proposal state rejected")
            self._applier.verify_rollback(record)
            return self._proposals.mark_rolled_back(selected)


def _proposal_id(value: UUID) -> UUID:
    if not isinstance(value, UUID):
        raise TypeError("proposal_id must be a UUID")
    return value


def _attempt_id(record: ProposalRecord) -> UUID:
    if not isinstance(record.apply_attempt_id, UUID):
        raise GitProposalError("apply attempt rejected")
    return record.apply_attempt_id
