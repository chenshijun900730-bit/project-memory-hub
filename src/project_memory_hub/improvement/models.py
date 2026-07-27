from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


ProposalOrigin = Literal["local_cli", "codex_task", "control_panel", "analyzer", "legacy"]
CreatableProposalOrigin = Literal["local_cli", "codex_task", "control_panel", "analyzer"]
ProposalStatus = Literal[
    "draft", "approved", "applying", "applied", "rejected", "failed", "rolled_back"
]
ProposalRisk = Literal["low", "medium", "high"]


class ProposalDraft(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    signature: str
    title: str
    description: str
    risk: ProposalRisk
    patch: str | None = Field(default=None, repr=False, exclude=True)
    verification_argv: tuple[str, ...] = ()
    target_version: str | None = None
    origin: CreatableProposalOrigin


class ProposalRecord(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    signature: str
    title: str
    description: str
    patch: str | None = Field(default=None, repr=False, exclude=True)
    risk: ProposalRisk
    verification_argv: tuple[str, ...]
    verification_summary: str
    status: ProposalStatus
    target_version: str | None
    rollback_ref: str | None
    created_at: datetime
    approved_at: datetime | None
    origin: ProposalOrigin
    approval_actor: str | None
    updated_at: datetime
    apply_attempt_id: UUID | None
    repository_root: Path | None
    original_branch: str | None
    base_commit: str | None
    proposal_branch: str | None
    applied_commit: str | None
    applied_at: datetime | None
    rolled_back_at: datetime | None
    failure_code: str | None


class ProposalSummary(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    signature: str
    title: str
    description: str
    risk: ProposalRisk
    status: ProposalStatus
    origin: ProposalOrigin
    created_at: datetime
    updated_at: datetime


class ProposalCreateResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    inserted: bool
    duplicate: bool
    record: ProposalRecord


class ApplyResult(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    proposal_id: UUID
    repository_root: Path
    original_branch: str
    base_commit: str
    proposal_branch: str
    applied_commit: str
    verification_summary: str
