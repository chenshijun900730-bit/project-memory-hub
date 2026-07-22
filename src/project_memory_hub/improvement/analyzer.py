from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project_memory_hub.improvement.models import ProposalDraft, ProposalRisk


_MAX_COUNT = 2**31 - 1
_Count = Annotated[int, Field(strict=True, ge=0, le=_MAX_COUNT)]


class HealthSnapshot(BaseModel, frozen=True):
    model_config = ConfigDict(extra="forbid")

    discovery_failure_count: _Count
    permission_failure_count: _Count
    adapter_failure_count: _Count
    retry_failure_count: _Count
    retry_remaining_count: _Count
    inserted_count: _Count
    duplicate_count: _Count
    duplicate_candidate_count: _Count
    compaction_failure_count: _Count
    compaction_remaining_count: _Count


class ImprovementAnalyzer:
    def analyze(self, health: HealthSnapshot) -> list[ProposalDraft]:
        snapshot = _validated_snapshot(health)
        drafts: list[ProposalDraft] = []

        if snapshot.discovery_failure_count >= 1:
            drafts.append(
                _draft(
                    signature="analyzer.health.v1.discovery_health.gte_1",
                    title="Improve project discovery health",
                    description=(
                        "Discovery failures: "
                        f"{snapshot.discovery_failure_count}; permission failures: "
                        f"{snapshot.permission_failure_count}."
                    ),
                    risk="medium",
                )
            )

        if snapshot.adapter_failure_count >= 1:
            drafts.append(
                _draft(
                    signature="analyzer.health.v1.adapter_failure.gte_1",
                    title="Harden adapter ingestion failures",
                    description=(f"Adapter failures: {snapshot.adapter_failure_count}."),
                    risk="medium",
                )
            )

        retry_pressure = snapshot.retry_failure_count + snapshot.retry_remaining_count
        if retry_pressure >= 1:
            drafts.append(
                _draft(
                    signature="analyzer.health.v1.retry_backlog.gte_1",
                    title="Reduce retry backlog",
                    description=(
                        f"Retry failures: {snapshot.retry_failure_count}; "
                        f"retry items remaining: {snapshot.retry_remaining_count}."
                    ),
                    risk="medium",
                )
            )

        duplicate_sample = snapshot.inserted_count + snapshot.duplicate_count
        duplicate_basis_points = (
            snapshot.duplicate_count * 10_000 // duplicate_sample if duplicate_sample else 0
        )
        duplicate_pressure = snapshot.duplicate_candidate_count >= 1 or (
            duplicate_sample >= 20 and duplicate_basis_points >= 8_000
        )
        if duplicate_pressure:
            drafts.append(
                _draft(
                    signature="analyzer.health.v1.duplicate_pressure.gte_1",
                    title="Reduce duplicate pressure",
                    description=(
                        "Duplicate candidates: "
                        f"{snapshot.duplicate_candidate_count}; inserted: "
                        f"{snapshot.inserted_count}; duplicates: "
                        f"{snapshot.duplicate_count}; duplicate ratio basis points: "
                        f"{duplicate_basis_points}."
                    ),
                    risk="low",
                )
            )

        compaction_pressure = (
            snapshot.compaction_failure_count + snapshot.compaction_remaining_count
        )
        if compaction_pressure >= 1:
            drafts.append(
                _draft(
                    signature="analyzer.health.v1.compaction_health.gte_1",
                    title="Improve compaction health",
                    description=(
                        f"Compaction failures: {snapshot.compaction_failure_count}; "
                        "compaction items remaining: "
                        f"{snapshot.compaction_remaining_count}."
                    ),
                    risk="medium",
                )
            )

        return drafts


def _validated_snapshot(value: HealthSnapshot) -> HealthSnapshot:
    if type(value) is not HealthSnapshot:
        raise TypeError("health snapshot is required")
    try:
        return HealthSnapshot.model_validate(
            {name: getattr(value, name) for name in HealthSnapshot.model_fields}
        )
    except ValidationError:
        raise ValueError("health snapshot is invalid") from None


def _draft(*, signature: str, title: str, description: str, risk: ProposalRisk) -> ProposalDraft:
    return ProposalDraft(
        signature=signature,
        title=title,
        description=description,
        risk=risk,
        patch=None,
        verification_argv=(),
        target_version=None,
        origin="analyzer",
    )
