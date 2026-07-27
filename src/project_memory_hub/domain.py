from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from project_memory_hub.utf8 import strict_utf8_size


class SourceAgent(StrEnum):
    CODEX = "codex"
    CHATGPT = "chatgpt"
    TRAE = "trae"
    WORKBUDDY = "workbuddy"
    ZCODE = "zcode"
    QODERWORK = "qoderwork"
    CLAUDE_CODE = "claude_code"


class Namespace(BaseModel, frozen=True):
    source_agent: SourceAgent
    model_id: str

    @field_validator("model_id")
    @classmethod
    def require_exact_bounded_model_id(cls, value: str) -> str:
        if (
            not value
            or value != value.strip()
            or len(value) > 513
            or strict_utf8_size(value) > 2_049
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("model_id must not be blank")
        return value


class MemoryKind(StrEnum):
    DECISION = "decision"
    FAILED_ATTEMPT = "failed_attempt"
    VERIFIED_METHOD = "verified_method"
    PREFERENCE = "preference"
    RISK = "risk"
    OPEN_ISSUE = "open_issue"
    REUSABLE_LESSON = "reusable_lesson"
    OUTCOME = "outcome"
    RETROSPECTIVE = "retrospective"


class LifecycleState(StrEnum):
    ACTIVE = "active"
    COLD = "cold"
    ARCHIVED = "archived"
    DELETED = "deleted"


class ProjectCandidate(BaseModel, frozen=True):
    canonical_path: Path
    display_name: str
    git_root: Path | None = None
    git_remote_fingerprint: str | None = None
    manifest_fingerprint: str | None = None
    markers: tuple[str, ...] = ()


class DiscoveryIssue(BaseModel, frozen=True):
    path: Path
    code: str
    remediation: str


class DiscoveryResult(BaseModel, frozen=True):
    candidates: tuple[ProjectCandidate, ...]
    issues: tuple[DiscoveryIssue, ...]


class RedactionResult(BaseModel, frozen=True):
    text: str
    findings: tuple[str, ...] = ()


class ProjectRecord(BaseModel, frozen=True):
    project_id: UUID
    canonical_path: Path
    display_name: str
    discovery_status: str
    permission_status: str
    last_observed_change: datetime | None = None


class ProjectFactInput(BaseModel, frozen=True):
    category: str
    normalized_content: str
    evidence_type: str
    evidence_reference: str
    observed_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)


class FactRecord(ProjectFactInput, frozen=True):
    fact_id: UUID
    project_id: UUID
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE


class BehaviorMemoryInput(BaseModel, frozen=True):
    project_id: UUID
    namespace: Namespace
    task_fingerprint: str
    memory_kind: MemoryKind
    normalized_content: str
    content_hash: str
    source_reference_id: UUID
    created_at: datetime
    confidence: float = Field(ge=0.0, le=1.0)


class BehaviorMemoryRecord(BehaviorMemoryInput, frozen=True):
    memory_id: UUID
    lifecycle_state: LifecycleState = LifecycleState.ACTIVE


class RecallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cwd: Path
    task: str
    namespace: Namespace
    max_tokens: int = Field(default=800, ge=128, le=4096)

    @field_validator("task")
    @classmethod
    def strip_non_empty_task(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("task must not be blank")
        return stripped


class CapturePayload(BaseModel):
    cwd: Path
    namespace: Namespace
    source_record_id: str
    objective: str
    outcome: str
    decisions: list[str] = Field(default_factory=list)
    failed_attempts: list[str] = Field(default_factory=list)
    verified_commands: list[str] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_issues: list[str] = Field(default_factory=list)
    resolved_open_issues: list[str] = Field(default_factory=list)
    reusable_lessons: list[str] = Field(default_factory=list)


class NamespaceVerification(BaseModel, frozen=True):
    namespace: Namespace
    source_record_id: str
    verified_by: Literal["codex_adapter", "chatgpt_adapter"]
    verified_at: datetime


class AdapterHealth(BaseModel, frozen=True):
    status: Literal["pass", "warn", "fail"]
    details: tuple[str, ...] = ()


class AdapterCheckpoint(BaseModel, frozen=True):
    adapter: SourceAgent
    scope: str
    cursor: dict[str, str | int]
    parser_version: str


class NormalizedTaskRecord(BaseModel, frozen=True):
    cwd: Path
    namespace: Namespace
    source_record_id: str
    objective: str
    outcome: str
    decisions: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    verified_commands: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    preferences: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    open_issues: tuple[str, ...] = ()
    resolved_open_issues: tuple[str, ...] = ()
    reusable_lessons: tuple[str, ...] = ()
    verification: NamespaceVerification


class AdapterBatch(BaseModel, frozen=True):
    records: tuple[NormalizedTaskRecord, ...]
    next_checkpoint: AdapterCheckpoint
    warnings: tuple[str, ...] = ()


class CaptureResult(BaseModel, frozen=True):
    inserted_ids: tuple[UUID, ...] = ()
    duplicate: bool = False
    status: Literal[
        "inserted",
        "duplicate",
        "resolved",
        "partial",
        "pending_verification",
        "project_not_found",
        "rejected",
    ]
    resolved_count: int = Field(default=0, ge=0)
    already_resolved_count: int = Field(default=0, ge=0)
    unmatched_resolution_count: int = Field(default=0, ge=0)


class RecallBrief(BaseModel, frozen=True):
    text: str
    estimated_tokens: int
    selected_ids: tuple[UUID, ...]
    omitted_count: int
    warnings: tuple[str, ...] = ()


class ReconcileReport(BaseModel, frozen=True):
    run_id: UUID
    status: Literal["success", "degraded", "failed", "skipped", "already_running"]
    inserted_count: int = 0
    duplicate_count: int = 0
    warning_count: int = 0
    stages: dict[str, str] = Field(default_factory=dict)


class InsertResult(BaseModel, frozen=True):
    inserted: bool
    duplicate: bool
    record_id: UUID | None = None


class FactScanReport(BaseModel, frozen=True):
    project_id: UUID
    observed_count: int
    stale_count: int
    warnings: tuple[str, ...] = ()


class PromotionRecord(BaseModel, frozen=True):
    promotion_id: UUID
    memory_id: UUID
    proposed_rule: str
    status: Literal["pending", "approved", "rejected"]
    approval_actor: str | None = None
