from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, Literal
from uuid import UUID

from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import ServiceContainer, build_container
from project_memory_hub.demo.runtime import DemoWorkspace
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    MemoryKind,
    Namespace,
    ProjectCandidate,
    ProjectFactInput,
    SourceAgent,
)
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.probes.base import ProbeClock, ProbeFilesystem, SourceDescriptor
from project_memory_hub.probes.builtin import OPTIONAL_PROBE_SOURCES, builtin_descriptors
from project_memory_hub.probes.filesystem import PathSafetyPolicy
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ModelStatus,
    ProbeBudget,
    ProbeMetrics,
    ProbeMode,
    ProbeWarningCode,
    SourceProbeRequest,
    SourceProbeResult,
    StructureStatus,
)
from project_memory_hub.probes.service import SourceProbeRegistry, SourceProbeService
from project_memory_hub.services.reconcile import ReconcileService
from project_memory_hub.storage.proposals import ProposalRepository


DEMO_LABEL: Final = "DEMO DATA"
SEED_VERSION: Final = 1
FIXED_DEMO_TIME: Final = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
CODEX_NAMESPACE: Final = Namespace(
    source_agent=SourceAgent.CODEX,
    model_id="demo-codex-model-v1",
)
CHATGPT_NAMESPACE: Final = Namespace(
    source_agent=SourceAgent.CHATGPT,
    model_id="demo-chatgpt-model-v1",
)
OPTIONAL_SOURCE_AGENTS: Final = OPTIONAL_PROBE_SOURCES

PROJECT_ID: Final = UUID("10000000-0000-4000-8000-000000000001")
FACT_IDS: Final = (
    UUID("11000000-0000-4000-8000-000000000001"),
    UUID("11000000-0000-4000-8000-000000000002"),
)
SOURCE_REFERENCE_IDS: Final = (
    UUID("20000000-0000-4000-8000-000000000001"),
    UUID("20000000-0000-4000-8000-000000000002"),
)
MEMORY_IDS: Final = (
    UUID("30000000-0000-4000-8000-000000000001"),
    UUID("30000000-0000-4000-8000-000000000002"),
    UUID("30000000-0000-4000-8000-000000000003"),
    UUID("30000000-0000-4000-8000-000000000004"),
)
PROPOSAL_ID: Final = UUID("40000000-0000-4000-8000-000000000001")
SYNTHETIC_UUIDS: Final = frozenset(
    (PROJECT_ID, *FACT_IDS, *SOURCE_REFERENCE_IDS, *MEMORY_IDS, PROPOSAL_ID)
)
_EXPECTED_SCHEMA_VERSIONS: Final = tuple(range(1, 14))
_EXPECTED_ROW_COUNTS: Final = {
    "projects": 1,
    "project_facts": 2,
    "source_refs": 2,
    "behavior_memories": 4,
    "improvement_proposals": 1,
}
_EXPECTED_NAMESPACE_COUNTS: Final = frozenset(
    (
        (CODEX_NAMESPACE.source_agent.value, CODEX_NAMESPACE.model_id, 2),
        (CHATGPT_NAMESPACE.source_agent.value, CHATGPT_NAMESPACE.model_id, 2),
    )
)

_FIXED_TIME_TEXT = "2026-07-18T12:00:00.000000Z"


@dataclass(frozen=True, slots=True)
class DemoSourceState:
    source_agent: SourceAgent
    ingestion_allowed: bool
    model_status: Literal["verified", "not_checked", "unverifiable"]


@dataclass(frozen=True, slots=True)
class DemoInventory:
    label: str
    seed_version: int
    generated_at: datetime
    project_id: UUID
    project_name: str
    namespaces: tuple[Namespace, ...]
    fact_ids: tuple[UUID, ...]
    memory_ids: tuple[UUID, ...]
    proposal_id: UUID
    source_states: tuple[DemoSourceState, ...]
    synthetic_uuid_allowlist: frozenset[UUID]
    reconcile_receipt: str

    def to_json_bytes(self) -> bytes:
        document = {
            "fact_ids": [str(value) for value in self.fact_ids],
            "generated_at": _utc_text(self.generated_at),
            "label": self.label,
            "memory_ids": [str(value) for value in self.memory_ids],
            "namespaces": [
                {
                    "model_id": namespace.model_id,
                    "source_agent": namespace.source_agent.value,
                }
                for namespace in self.namespaces
            ],
            "project_id": str(self.project_id),
            "project_name": self.project_name,
            "proposal_id": str(self.proposal_id),
            "reconcile_receipt": self.reconcile_receipt,
            "seed_version": self.seed_version,
            "source_states": [
                {
                    "ingestion_allowed": state.ingestion_allowed,
                    "model_status": state.model_status,
                    "source_agent": state.source_agent.value,
                }
                for state in self.source_states
            ],
            "synthetic_uuid_allowlist": [
                str(value) for value in sorted(self.synthetic_uuid_allowlist, key=str)
            ],
        }
        return (
            json.dumps(
                document,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")


class _DemoSeedAdapter:
    """Narrow deterministic-ID seam around the existing repositories."""

    _ID_UPDATES = {
        "project": "update projects set project_id = ?, created_at = ?, updated_at = ? "
        "where project_id = ?",
        "fact": "update project_facts set fact_id = ?, created_at = ? where fact_id = ?",
        "memory": "update behavior_memories set memory_id = ? where memory_id = ?",
        "proposal": "update improvement_proposals set proposal_id = ? where proposal_id = ?",
    }

    def __init__(self, container: ServiceContainer) -> None:
        self._container = container

    def replace_identifier(
        self,
        kind: Literal["project", "fact", "memory", "proposal"],
        current: UUID,
        fixed: UUID,
    ) -> None:
        query = self._ID_UPDATES[kind]
        values: tuple[str, ...]
        if kind == "project":
            values = (str(fixed), _FIXED_TIME_TEXT, _FIXED_TIME_TEXT, str(current))
        elif kind == "fact":
            values = (str(fixed), _FIXED_TIME_TEXT, str(current))
        else:
            values = (str(fixed), str(current))
        with self._container.database.transaction() as connection:
            cursor = connection.execute(query, values)
            if cursor.rowcount != 1:
                raise RuntimeError("demo identifier replacement failed")

    def insert_source_reference(
        self,
        source_reference_id: UUID,
        namespace: Namespace,
        source_record_id: str,
    ) -> None:
        content_hash = _sha256(f"{namespace.source_agent.value}:{source_record_id}")
        with self._container.database.transaction() as connection:
            cursor = connection.execute(
                """
                insert into source_refs(
                    source_reference_id, source_agent, source_record_id, source_path,
                    content_hash, source_timestamp, parser_version, created_at,
                    capture_project_id, capture_model_id
                ) values (?, ?, ?, null, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(source_reference_id),
                    namespace.source_agent.value,
                    source_record_id,
                    content_hash,
                    _FIXED_TIME_TEXT,
                    "demo-seed-v1",
                    _FIXED_TIME_TEXT,
                    str(PROJECT_ID),
                    namespace.model_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("demo source reference insertion failed")


@dataclass(frozen=True, slots=True)
class _FixedProbeClock(ProbeClock):
    def now(self) -> datetime:
        return FIXED_DEMO_TIME

    def monotonic(self) -> float:
        return 100.0


class _FixedDemoProbe:
    def __init__(self, descriptor: SourceDescriptor) -> None:
        self.descriptor = descriptor

    def probe(
        self,
        request: SourceProbeRequest,
        *,
        filesystem: ProbeFilesystem,
        budget: ProbeBudget,
        clock: ProbeClock,
        checked_at: datetime,
        deadline: float,
    ) -> SourceProbeResult:
        del filesystem, budget, clock, deadline
        structure = request.mode is ProbeMode.STRUCTURE
        return SourceProbeResult(
            source_agent=request.source_agent,
            mode=request.mode,
            installation_status=InstallationStatus.NOT_DETECTED,
            data_status=DataStatus.MISSING,
            capability=self.descriptor.capability,
            structure_status=StructureStatus.NOT_RUN,
            model_status=ModelStatus.UNVERIFIABLE if structure else ModelStatus.NOT_CHECKED,
            ingestion_allowed=False,
            metrics=ProbeMetrics(),
            warning_codes=(
                (ProbeWarningCode.SOURCE_MISSING, ProbeWarningCode.MODEL_ID_UNVERIFIABLE)
                if structure
                else (ProbeWarningCode.SOURCE_MISSING,)
            ),
            checked_at=checked_at,
        )


def build_demo_container(workspace: DemoWorkspace) -> ServiceContainer:
    """Build a Web-safe container that cannot reconcile real local sources."""
    if not isinstance(workspace, DemoWorkspace):
        raise TypeError("validated DemoWorkspace required")
    workspace.validate_runtime()
    paths = workspace.paths
    runtime_dir = paths.root
    config_path = runtime_dir / "config.toml"
    probe_home = paths.imports / "probe-home"
    probe_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    codex_sessions = paths.imports / "codex-sessions"
    codex_sessions.mkdir(mode=0o700, exist_ok=True)
    container = build_container(
        config_path,
        probe_home=probe_home,
        codex_sessions_root=codex_sessions,
        discovery_home=probe_home,
    )
    workspace.validate_runtime()
    container.reconcile = ReconcileService.minimal(
        container.database,
        container.process_lock,
        now=lambda: FIXED_DEMO_TIME,
    )
    registry = SourceProbeRegistry(
        tuple(_FixedDemoProbe(descriptor) for descriptor in builtin_descriptors())
    )
    container.source_probes = SourceProbeService(
        registry,
        PathSafetyPolicy(home=probe_home),
        ProbeBudget(),
        _FixedProbeClock(),
    )
    return container


def seed_demo_database(workspace: DemoWorkspace) -> DemoInventory:
    """Populate the isolated current schema with deterministic fictional records."""
    if not isinstance(workspace, DemoWorkspace):
        raise TypeError("validated DemoWorkspace required")
    workspace.validate_runtime()
    paths = workspace.paths
    config_path = paths.root / "config.toml"
    project_root = paths.imports / "fictional-projects"
    project_path = project_root / "northstar-notes"
    project_path.mkdir(mode=0o700, parents=True)
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )

    with build_demo_container(workspace) as container:
        adapter = _DemoSeedAdapter(container)
        created_project = container.projects.register(
            ProjectCandidate(
                canonical_path=project_path,
                display_name="Northstar Notes — DEMO DATA",
                markers=("synthetic-demo",),
            )
        )
        adapter.replace_identifier("project", created_project.project_id, PROJECT_ID)
        project = container.projects.get(PROJECT_ID)

        fact_inputs = (
            ProjectFactInput(
                category="stack",
                normalized_content="Python service with a local-only synthetic dashboard.",
                evidence_type="demo_manifest",
                evidence_reference="demo-seed-v1",
                observed_at=FIXED_DEMO_TIME,
                confidence=1.0,
            ),
            ProjectFactInput(
                category="workflow",
                normalized_content="Every improvement remains approval gated and reversible.",
                evidence_type="demo_manifest",
                evidence_reference="demo-seed-v1",
                observed_at=FIXED_DEMO_TIME,
                confidence=1.0,
            ),
        )
        for fixed_id, fact_input in zip(FACT_IDS, fact_inputs, strict=True):
            created_fact = container.facts.observe(project.project_id, fact_input)
            adapter.replace_identifier("fact", created_fact.fact_id, fixed_id)
            if (
                container.facts.search(project.project_id, fact_input.normalized_content, 10)[
                    0
                ].fact_id
                != fixed_id
            ):
                raise RuntimeError("demo fact verification failed")

        memory_specs = (
            (
                SOURCE_REFERENCE_IDS[0],
                CODEX_NAMESPACE,
                "demo-codex-record-v1",
                (
                    (
                        MEMORY_IDS[0],
                        MemoryKind.DECISION,
                        "Keep model namespaces strictly isolated.",
                    ),
                    (
                        MEMORY_IDS[1],
                        MemoryKind.OUTCOME,
                        "Synthetic release checks completed locally.",
                    ),
                ),
            ),
            (
                SOURCE_REFERENCE_IDS[1],
                CHATGPT_NAMESPACE,
                "demo-chatgpt-record-v1",
                (
                    (
                        MEMORY_IDS[2],
                        MemoryKind.PREFERENCE,
                        "Show a safe next step before advanced controls.",
                    ),
                    (
                        MEMORY_IDS[3],
                        MemoryKind.REUSABLE_LESSON,
                        "Verify privacy before publishing any asset.",
                    ),
                ),
            ),
        )
        for source_id, namespace, record_id, memories in memory_specs:
            adapter.insert_source_reference(source_id, namespace, record_id)
            task_fingerprint = _sha256(f"{namespace.source_agent.value}:demo-task-v1")
            for fixed_id, kind, content in memories:
                inserted = container.memories.insert(
                    BehaviorMemoryInput(
                        project_id=project.project_id,
                        namespace=namespace,
                        task_fingerprint=task_fingerprint,
                        memory_kind=kind,
                        normalized_content=content,
                        content_hash=_sha256(content),
                        source_reference_id=source_id,
                        created_at=FIXED_DEMO_TIME,
                        confidence=1.0,
                    )
                )
                if not inserted.inserted or inserted.record_id is None:
                    raise RuntimeError("demo memory insertion failed")
                adapter.replace_identifier("memory", inserted.record_id, fixed_id)
                if container.memories.get_by_id(fixed_id).namespace != namespace:
                    raise RuntimeError("demo memory verification failed")

        proposal_repository = ProposalRepository(
            container.database,
            container.redactor,
            now=lambda: FIXED_DEMO_TIME,
        )
        created_proposal = proposal_repository.create(
            ProposalDraft(
                signature="demo-safe-presentation-v1",
                title="Clarify the synthetic onboarding note",
                description="Review a fictional copy-only improvement before approval.",
                risk="low",
                origin="control_panel",
            )
        )
        adapter.replace_identifier("proposal", created_proposal.record.proposal_id, PROPOSAL_ID)
        if container.proposals.get(PROPOSAL_ID).status != "draft":
            raise RuntimeError("demo proposal verification failed")

        container.reconcile.record_success(FIXED_DEMO_TIME)
        if container.reconcile.should_run():
            raise RuntimeError("demo reconcile receipt failed")
        _validate_demo_seed_database(container)

    workspace.validate_runtime()
    return DemoInventory(
        label=DEMO_LABEL,
        seed_version=SEED_VERSION,
        generated_at=FIXED_DEMO_TIME,
        project_id=PROJECT_ID,
        project_name="Northstar Notes — DEMO DATA",
        namespaces=(CODEX_NAMESPACE, CHATGPT_NAMESPACE),
        fact_ids=FACT_IDS,
        memory_ids=MEMORY_IDS,
        proposal_id=PROPOSAL_ID,
        source_states=_source_states(),
        synthetic_uuid_allowlist=SYNTHETIC_UUIDS,
        reconcile_receipt="synthetic-fixed-clock",
    )


def _source_states() -> tuple[DemoSourceState, ...]:
    return (
        DemoSourceState(SourceAgent.CODEX, True, "verified"),
        DemoSourceState(SourceAgent.CHATGPT, True, "verified"),
        DemoSourceState(SourceAgent.TRAE, False, "unverifiable"),
        DemoSourceState(SourceAgent.WORKBUDDY, False, "not_checked"),
        DemoSourceState(SourceAgent.ZCODE, False, "not_checked"),
        DemoSourceState(SourceAgent.QODERWORK, False, "not_checked"),
        DemoSourceState(SourceAgent.CLAUDE_CODE, False, "not_checked"),
    )


def _validate_demo_seed_database(container: ServiceContainer) -> None:
    try:
        with container.database.connect(readonly=True) as connection:
            integrity = tuple(
                row[0] for row in connection.execute("pragma integrity_check").fetchall()
            )
            if integrity != ("ok",):
                raise ValueError("integrity")

            if connection.execute("pragma foreign_key_check").fetchone() is not None:
                raise ValueError("foreign keys")

            schema_versions = tuple(
                row[0]
                for row in connection.execute(
                    "select version from schema_migrations order by version"
                ).fetchall()
            )
            if schema_versions != _EXPECTED_SCHEMA_VERSIONS:
                raise ValueError("schema versions")

            for table, expected_count in _EXPECTED_ROW_COUNTS.items():
                actual_count = connection.execute(f"select count(*) from {table}").fetchone()[0]
                if actual_count != expected_count:
                    raise ValueError("row counts")

            namespace_counts = frozenset(
                (row[0], row[1], row[2])
                for row in connection.execute(
                    """
                    select source_agent, model_id, count(*)
                    from behavior_memories
                    group by source_agent, model_id
                    """
                ).fetchall()
            )
            if namespace_counts != _EXPECTED_NAMESPACE_COUNTS:
                raise ValueError("namespaces")

            mismatch = connection.execute(
                """
                select 1
                from behavior_memories as memory
                left join source_refs as source
                  on source.source_reference_id = memory.source_reference_id
                where source.source_reference_id is null
                   or memory.project_id is not source.capture_project_id
                   or memory.source_agent is not source.source_agent
                   or memory.model_id is not source.capture_model_id
                limit 1
                """
            ).fetchone()
            if mismatch is not None:
                raise ValueError("source provenance")
    except Exception:
        raise RuntimeError("demo seed database validation failed") from None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
