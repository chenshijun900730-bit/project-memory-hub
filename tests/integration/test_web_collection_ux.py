from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ModelStatus,
    ProbeCapability,
    ProbeMetrics,
    ProbeMode,
    ProbeWarningCode,
    SourceProbeResult,
    StructureStatus,
)
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.web.app import create_app


_OPTIONAL_SOURCES = (
    SourceAgent.TRAE,
    SourceAgent.WORKBUDDY,
    SourceAgent.ZCODE,
    SourceAgent.QODERWORK,
    SourceAgent.CLAUDE_CODE,
)


def _container(tmp_path: Path):
    project_root = tmp_path / "projects"
    project_root.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config_path = runtime / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    probe_home = tmp_path / "probe-home"
    probe_home.mkdir()
    return build_container(config_path, probe_home=probe_home)


def _probe_result(
    source: SourceAgent,
    *,
    mode: ProbeMode = ProbeMode.LIGHT,
    installation_status: InstallationStatus = InstallationStatus.NOT_DETECTED,
    data_status: DataStatus = DataStatus.MISSING,
    model_status: ModelStatus = ModelStatus.NOT_CHECKED,
    structure_status: StructureStatus = StructureStatus.NOT_RUN,
    warning_codes: tuple[ProbeWarningCode, ...] = (ProbeWarningCode.SOURCE_MISSING,),
) -> SourceProbeResult:
    return SourceProbeResult(
        source_agent=source,
        mode=mode,
        installation_status=installation_status,
        data_status=data_status,
        capability=(
            ProbeCapability.STRUCTURE_METADATA
            if source is SourceAgent.TRAE
            else ProbeCapability.PRESENCE_AND_ACCESS
        ),
        structure_status=structure_status,
        model_status=model_status,
        ingestion_allowed=False,
        metrics=ProbeMetrics(),
        warning_codes=warning_codes,
        checked_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )


def _light_results() -> tuple[SourceProbeResult, ...]:
    return tuple(_probe_result(source) for source in _OPTIONAL_SOURCES)


def _structure_result() -> SourceProbeResult:
    return _probe_result(
        SourceAgent.TRAE,
        mode=ProbeMode.STRUCTURE,
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        model_status=ModelStatus.UNVERIFIABLE,
        structure_status=StructureStatus.PARTIAL,
        warning_codes=(ProbeWarningCode.MODEL_ID_UNVERIFIABLE,),
    )


class _ProbeLease:
    def __init__(self, owner: _ProbeService) -> None:
        self._owner = owner

    def run(self) -> SourceProbeResult:
        self._owner.structure_calls += 1
        return self._owner.structure_result

    def close(self) -> None:
        self._owner.close_calls += 1


class _ProbeService:
    def __init__(self) -> None:
        self.light_results = _light_results()
        self.structure_result = _structure_result()
        self.light_calls = 0
        self.reserve_calls = 0
        self.structure_calls = 0
        self.close_calls = 0

    def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
        self.light_calls += 1
        return self.light_results

    def reserve_structure(self, source: SourceAgent) -> _ProbeLease:
        assert source is SourceAgent.TRAE
        self.reserve_calls += 1
        return _ProbeLease(self)


async def _client(container) -> tuple[httpx.AsyncClient, str]:
    app = create_app(container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    token = LocalAccessToken.load_or_create(container.paths)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as bootstrap_client:
        boot = await bootstrap_client.get(f"/?token={token}", follow_redirects=False)
    return (
        httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
            cookies=boot.cookies,
        ),
        boot.headers["x-project-memory-hub-csrf"],
    )


def _collection(document: str, name: str) -> str:
    match = re.search(
        rf'<section[^>]+data-source-collection="{name}"[^>]*>(.*?)</section>',
        document,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def _source_row(collection: str, source: SourceAgent) -> str:
    match = re.search(
        rf'<tr[^>]+data-source="{source.value}"[^>]*>(.*?)</tr>',
        collection,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


def test_sources_render_exactly_two_role_based_collections(tmp_path: Path) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = _ProbeService()  # type: ignore[assignment]
            client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert re.findall(r'data-source-collection="([^"]+)"', response.text) == [
        "ingestion",
        "probes",
    ]
    ingestion = _collection(response.text, "ingestion")
    probes = _collection(response.text, "probes")
    assert "Ingestion sources" in ingestion
    assert "Read-only probes" in probes
    assert ingestion.count("<table") == probes.count("<table") == 1
    ingestion_head = re.search(r"<thead>(.*?)</thead>", ingestion, flags=re.DOTALL)
    probe_head = re.search(r"<thead>(.*?)</thead>", probes, flags=re.DOTALL)
    assert ingestion_head is not None and probe_head is not None
    assert ingestion_head.group(1).count("<th") == 4
    assert probe_head.group(1).count("<th") == 7
    for source in (SourceAgent.CODEX, SourceAgent.CHATGPT):
        assert f'data-source="{source.value}"' in ingestion
        assert f'data-source="{source.value}"' not in probes
    for source in _OPTIONAL_SOURCES:
        assert f'data-source="{source.value}"' in probes
        assert f'data-source="{source.value}"' not in ingestion


def test_sources_template_consumes_only_presentation_groups() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    template = (repository_root / "src/project_memory_hub/web/templates/sources.html").read_text(
        encoding="utf-8"
    )
    routes = (repository_root / "src/project_memory_hub/web/routes.py").read_text(encoding="utf-8")

    assert "selectattr" not in template
    assert "rejectattr" not in template
    assert "source_groups.ingestion" in template
    assert "source_groups.probes" in template
    assert routes.count("group_source_records(") == 3


def test_collection_layout_has_mobile_labels_and_progressive_controls() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    static = repository_root / "src/project_memory_hub/web/static"
    templates = repository_root / "src/project_memory_hub/web/templates"
    sources = (templates / "sources.html").read_text(encoding="utf-8")
    projects = (templates / "projects.html").read_text(encoding="utf-8")
    css = (static / "app.css").read_text(encoding="utf-8")

    assert sources.count('class="mobile-field-label"') >= 6
    assert 'data-i18n="sources.ingestion_sources"' in sources
    assert 'data-i18n="sources.read_only_probes"' in sources
    assert 'class="project-browser-controls"' in projects
    assert ".mobile-field-label" in css
    assert "[data-source-collection] thead" in css
    assert ".project-browser-controls" in css
    assert ".project-results" in css


def test_sources_preserve_only_the_authorized_forms(tmp_path: Path) -> None:
    async def scenario() -> tuple[httpx.Response, str]:
        with _container(tmp_path) as container:
            container.source_probes = _ProbeService()  # type: ignore[assignment]
            client, csrf = await _client(container)
            async with client:
                return await client.get("/sources"), csrf

    response, csrf = asyncio.run(scenario())
    ingestion = _collection(response.text, "ingestion")
    probes = _collection(response.text, "probes")

    for source in (SourceAgent.CODEX, SourceAgent.CHATGPT):
        row = _source_row(ingestion, source)
        assert f'action="/sources/{source.value}/disable"' in row
        assert 'method="post"' in row
        assert f'name="csrf_token" value="{csrf}"' in row
    assert probes.count("<form") == 1
    trae = _source_row(probes, SourceAgent.TRAE)
    assert 'method="post" action="/sources/trae/probe"' in trae
    assert f'name="csrf_token" value="{csrf}"' in trae
    assert 'data-action="structure-probe"' in trae
    for source in _OPTIONAL_SOURCES:
        row = _source_row(probes, source)
        assert f"/sources/{source.value}/enable" not in row
        assert f"/sources/{source.value}/disable" not in row
        assert f"/sources/{source.value}/import" not in row
        assert ">Enable<" not in row
        assert ">Import<" not in row


def test_probe_warning_codes_exist_only_inside_closed_details(tmp_path: Path) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = _ProbeService()  # type: ignore[assignment]
            client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())
    probes = _collection(response.text, "probes")
    detail_blocks = re.findall(
        r'<details class="probe-warnings">(.*?)</details>',
        probes,
        flags=re.DOTALL,
    )

    assert len(detail_blocks) == len(_OPTIONAL_SOURCES)
    assert all("<summary" in block and "Warnings" in block for block in detail_blocks)
    assert all('class="warning-codes"' in block for block in detail_blocks)
    outside_details = re.sub(
        r'<details class="probe-warnings">.*?</details>',
        "",
        probes,
        flags=re.DOTALL,
    )
    assert "source_missing" not in outside_details
    assert 'class="warning-codes"' not in outside_details
    assert '<details class="probe-warnings" open' not in probes


def test_trae_structure_post_keeps_collection_contract(tmp_path: Path) -> None:
    async def scenario() -> tuple[httpx.Response, _ProbeService]:
        with _container(tmp_path) as container:
            probes = _ProbeService()
            container.source_probes = probes  # type: ignore[assignment]
            client, csrf = await _client(container)
            async with client:
                response = await client.post(
                    "/sources/trae/probe",
                    headers={
                        "origin": "http://127.0.0.1",
                        "x-csrf-token": csrf,
                    },
                    data={"csrf_token": csrf},
                )
            return response, probes

    response, probes = asyncio.run(scenario())

    assert response.status_code == 200
    assert probes.reserve_calls == probes.structure_calls == 1
    assert probes.light_calls == 1
    assert re.findall(r'data-source-collection="([^"]+)"', response.text) == [
        "ingestion",
        "probes",
    ]
    trae = _source_row(_collection(response.text, "probes"), SourceAgent.TRAE)
    assert "Unverifiable" in trae
    assert "Partial" in trae
    assert "model_id_unverifiable" in trae
    assert '<details class="probe-warnings">' in trae
    assert 'method="post" action="/sources/trae/probe"' in trae
    assert '<script src="/static/sources.js" defer></script>' in response.text
