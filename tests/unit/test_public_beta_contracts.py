from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from project_memory_hub.adapters.registry import AdapterRegistry
from project_memory_hub.config import AppConfig
from project_memory_hub.domain import Namespace, SourceAgent
from project_memory_hub.probes.builtin import OPTIONAL_PROBE_SOURCES
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ModelStatus,
    ProbeCapability,
    ProbeMetrics,
    ProbeMode,
    SourceProbeResult,
    StructureStatus,
)
from project_memory_hub.services import recall as recall_module
from project_memory_hub.security import web as web_security
from project_memory_hub.storage.memories import MemoryRepository


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = PROJECT_ROOT / "src/project_memory_hub/storage/migrations"


class _EmptyCursor:
    def fetchall(self) -> list[object]:
        return []


class _RecordingConnection:
    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, statement: str, parameters: tuple[object, ...]) -> _EmptyCursor:
        self.statements.append((statement, parameters))
        return _EmptyCursor()


class _RecordingDatabase:
    def __init__(self) -> None:
        self.connection = _RecordingConnection()

    @contextmanager
    def connect(self, *, readonly: bool = False) -> Iterator[_RecordingConnection]:
        assert readonly is True
        yield self.connection


def test_schema_contract_stops_at_migration_thirteen() -> None:
    assert {path.name for path in MIGRATIONS.glob("*.sql")} == {
        "0001_initial.sql",
        "0002_import_receipt_source_agent.sql",
        "0003_compaction_kind_order.sql",
        "0004_compaction_enumeration_indexes.sql",
        "0005_strict_observation_epoch.sql",
        "0006_discovery_findings.sql",
        "0007_improvement_proposal_execution.sql",
        "0008_project_path_identity.sql",
        "0009_explicit_issue_resolution.sql",
        "0010_codex_deferred_records.sql",
        "0011_pending_capture_history.sql",
        "0012_capture_correlation.sql",
        "0013_codex_deferred_parser_policy.sql",
    }


def test_recall_product_ceiling_remains_800_tokens(tmp_path: Path) -> None:
    assert recall_module._PRODUCT_MAX_RECALL_TOKENS == 800
    defaults = AppConfig.defaults(tmp_path)
    assert defaults.max_recall_tokens == 800
    assert defaults.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)


def test_behavior_candidates_are_scoped_before_in_memory_ranking() -> None:
    database = _RecordingDatabase()
    repository = MemoryRepository(cast(Any, database))
    project_id = uuid4()
    namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-public-beta")

    assert repository.search(project_id, namespace, "rank me", limit=5) == []

    assert len(database.connection.statements) == 1
    statement, parameters = database.connection.statements[0]
    normalized = " ".join(statement.split()).casefold()
    assert "where project_id = ?" in normalized
    assert "and source_agent = ?" in normalized
    assert "and model_id = ?" in normalized
    assert normalized.index("and source_agent = ?") < normalized.index("order by")
    assert normalized.index("and model_id = ?") < normalized.index("order by")
    assert parameters[:3] == (
        str(project_id).lower(),
        SourceAgent.CODEX.value,
        "gpt-public-beta",
    )


def test_default_ingestion_registry_enables_only_codex_and_chatgpt() -> None:
    assert tuple(SourceAgent) == (
        SourceAgent.CODEX,
        SourceAgent.CHATGPT,
        *OPTIONAL_PROBE_SOURCES,
    )
    adapters = tuple(SimpleNamespace(source_agent=source) for source in SourceAgent)

    enabled = AdapterRegistry(cast(Any, adapters)).enabled()

    assert tuple(adapter.source_agent for adapter in enabled) == (
        SourceAgent.CODEX,
        SourceAgent.CHATGPT,
    )


@pytest.mark.parametrize("source_agent", OPTIONAL_PROBE_SOURCES)
def test_optional_source_results_can_never_allow_ingestion(source_agent: SourceAgent) -> None:
    fields = {
        "source_agent": source_agent,
        "mode": ProbeMode.LIGHT,
        "installation_status": InstallationStatus.NOT_DETECTED,
        "data_status": DataStatus.MISSING,
        "capability": ProbeCapability.PRESENCE_AND_ACCESS,
        "structure_status": StructureStatus.NOT_RUN,
        "model_status": ModelStatus.NOT_CHECKED,
        "metrics": ProbeMetrics(),
        "checked_at": datetime(2026, 1, 1, tzinfo=UTC),
    }

    assert SourceProbeResult(**fields, ingestion_allowed=False).ingestion_allowed is False
    with pytest.raises(ValidationError, match="ingestion_allowed"):
        SourceProbeResult(**fields, ingestion_allowed=True)


def test_public_web_route_inventory_is_frozen() -> None:
    routes = (PROJECT_ROOT / "src/project_memory_hub/web/routes.py").read_text(encoding="utf-8")
    declared = set(re.findall(r'@router\.(get|post)\("([^"]+)"\)', routes))

    assert declared == {
        ("get", "/"),
        ("get", "/imports"),
        ("get", "/memories"),
        ("get", "/projects"),
        ("get", "/proposals"),
        ("get", "/setup"),
        ("get", "/settings"),
        ("get", "/sources"),
        ("post", "/imports/chatgpt"),
        ("post", "/memories/{memory_id}/archive"),
        ("post", "/memories/{memory_id}/delete"),
        ("post", "/memories/{memory_id}/promote"),
        ("post", "/projects/{project_id}/disable"),
        ("post", "/projects/{project_id}/enable"),
        ("post", "/projects/{project_id}/relink"),
        ("post", "/promotions/{promotion_id}/approve"),
        ("post", "/proposals/{proposal_id}/apply"),
        ("post", "/proposals/{proposal_id}/approve"),
        ("post", "/proposals/{proposal_id}/reject"),
        ("post", "/proposals/{proposal_id}/rollback"),
        ("post", "/settings"),
        ("post", "/setup/complete"),
        ("post", "/setup/configure"),
        ("post", "/sources/trae/probe"),
        ("post", "/sources/{source}/disable"),
        ("post", "/sources/{source}/enable"),
    }


def test_public_web_security_headers_are_frozen() -> None:
    assert web_security._SECURITY_HEADERS == {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; base-uri 'none'; connect-src 'self'; "
            "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; "
            "object-src 'none'; script-src 'self'; style-src 'self'"
        ),
        "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
        "Referrer-Policy": "same-origin",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
