from __future__ import annotations

import asyncio
import errno
import hashlib
import inspect
import json
import re
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn
from uuid import UUID, uuid4

import httpx
import pytest

import project_memory_hub.discovery.scanner as scanner_module
import project_memory_hub.web.routes as routes_module
from project_memory_hub.adapters.base import ReconcileRequiredError
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    LifecycleState,
    MemoryKind,
    Namespace,
    ProjectCandidate,
    ProjectFactInput,
    SourceAgent,
)
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.probes.base import ProbeBusyError
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


def _container(tmp_path: Path):
    project_root = tmp_path / "projects"
    project_root.mkdir()
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    config_path = root / "config.toml"
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
    source_agent: SourceAgent,
    *,
    mode: ProbeMode = ProbeMode.LIGHT,
    installation_status: InstallationStatus = InstallationStatus.NOT_DETECTED,
    data_status: DataStatus = DataStatus.MISSING,
    model_status: ModelStatus = ModelStatus.NOT_CHECKED,
    structure_status: StructureStatus = StructureStatus.NOT_RUN,
    warning_codes: tuple[ProbeWarningCode, ...] = (ProbeWarningCode.SOURCE_MISSING,),
) -> SourceProbeResult:
    return SourceProbeResult(
        source_agent=source_agent,
        mode=mode,
        installation_status=installation_status,
        data_status=data_status,
        capability=(
            ProbeCapability.STRUCTURE_METADATA
            if source_agent is SourceAgent.TRAE
            else ProbeCapability.PRESENCE_AND_ACCESS
        ),
        structure_status=structure_status,
        model_status=model_status,
        ingestion_allowed=False,
        metrics=ProbeMetrics(),
        warning_codes=warning_codes,
        checked_at=datetime(2026, 7, 17, tzinfo=timezone.utc),
    )


def _trae_structure_result() -> SourceProbeResult:
    return _probe_result(
        SourceAgent.TRAE,
        mode=ProbeMode.STRUCTURE,
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        model_status=ModelStatus.UNVERIFIABLE,
        structure_status=StructureStatus.PARTIAL,
        warning_codes=(ProbeWarningCode.MODEL_ID_UNVERIFIABLE,),
    )


def _tree_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _optional_probe_results(
    replacements: dict[SourceAgent, SourceProbeResult] | None = None,
) -> tuple[SourceProbeResult, ...]:
    selected = replacements or {}
    return tuple(
        selected.get(source, _probe_result(source))
        for source in (
            SourceAgent.TRAE,
            SourceAgent.WORKBUDDY,
            SourceAgent.ZCODE,
            SourceAgent.QODERWORK,
            SourceAgent.CLAUDE_CODE,
        )
    )


def _source_row(response: httpx.Response, source: SourceAgent) -> str:
    match = re.search(
        rf'<tr data-source="{source.value}">(.*?)</tr>',
        response.text,
        flags=re.DOTALL,
    )
    assert match is not None
    return match.group(1)


class _RecordingLease:
    def __init__(self, owner: _RecordingProbeService) -> None:
        self._owner = owner
        self._state_lock = threading.Lock()
        self.state = "reserved"

    def run(self) -> SourceProbeResult:
        with self._state_lock:
            if self.state != "reserved":
                raise RuntimeError("recording lease is not runnable")
            self.state = "running"
        self._owner.calls.append("worker start")
        self._owner.structure_calls += 1
        if self._owner.started is not None:
            self._owner.started.set()
        try:
            if self._owner.release is not None and not self._owner.release.wait(timeout=5):
                raise TimeoutError("recording lease was not released")
            return self._owner.structure_result
        finally:
            with self._state_lock:
                self.state = "closed"
            self._owner.calls.append("worker close")
            self._owner.release_reservation()

    def close(self) -> None:
        with self._state_lock:
            if self.state != "reserved":
                return
            self.state = "closed"
        self._owner.calls.append("lease close")
        self._owner.release_reservation()


class _RecordingProbeService:
    def __init__(
        self,
        *,
        structure_result: SourceProbeResult | None = None,
        light_results: tuple[SourceProbeResult, ...] | None = None,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        busy: bool = False,
    ) -> None:
        self.structure_result = structure_result or _trae_structure_result()
        self.light_results = light_results or _optional_probe_results()
        self.started = started
        self.release = release
        self.busy = busy
        self.calls: list[str] = []
        self.reserve_calls = 0
        self.structure_calls = 0
        self.light_calls = 0
        self.leases: list[_RecordingLease] = []
        self._reservation_lock = threading.Lock()
        self._reserved = False

    def reserve_structure(self, source: SourceAgent) -> _RecordingLease:
        assert source is SourceAgent.TRAE
        self.calls.append("reserve")
        self.reserve_calls += 1
        with self._reservation_lock:
            if self.busy or self._reserved:
                raise ProbeBusyError("probe_busy")
            self._reserved = True
        lease = _RecordingLease(self)
        self.leases.append(lease)
        return lease

    def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
        if self.leases and self.leases[-1].state != "closed":
            raise AssertionError("light probe started before structure lease closed")
        self.calls.append("light")
        self.light_calls += 1
        return self.light_results

    def release_reservation(self) -> None:
        with self._reservation_lock:
            self._reserved = False


async def _client(container):
    app = create_app(container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    token = LocalAccessToken.load_or_create(container.paths)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as bootstrap_client:
        boot = await bootstrap_client.get(f"/?token={token}", follow_redirects=False)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        cookies=boot.cookies,
    )
    return app, client, boot.headers["x-project-memory-hub-csrf"]


async def _post_trae_probe(client: httpx.AsyncClient, csrf: str) -> httpx.Response:
    return await client.post(
        "/sources/trae/probe",
        headers=_unsafe(csrf),
        data={"csrf_token": csrf},
    )


def _register(container, path: Path, name: str = "Synthetic"):
    path.mkdir()
    return container.projects.register(ProjectCandidate(canonical_path=path, display_name=name))


def _insert_memory(
    container,
    project_id: UUID,
    *,
    model_id: str,
    content: str,
) -> UUID:
    source_reference_id = uuid4()
    now = datetime.now(timezone.utc)
    with container.database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, 'codex', ?, null, ?, ?, 'test-v1', ?)
            """,
            (
                str(source_reference_id),
                f"record-{model_id}",
                hashlib.sha256(model_id.encode()).hexdigest(),
                now.isoformat(),
                now.isoformat(),
            ),
        )
    result = container.memories.insert(
        BehaviorMemoryInput(
            project_id=project_id,
            namespace=Namespace(source_agent=SourceAgent.CODEX, model_id=model_id),
            task_fingerprint=hashlib.sha256(f"task-{model_id}".encode()).hexdigest(),
            memory_kind=MemoryKind.DECISION,
            normalized_content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            source_reference_id=source_reference_id,
            created_at=now,
            confidence=0.9,
        )
    )
    assert result.record_id is not None
    return result.record_id


def _insert_display_open_issue(
    container,
    project_id: UUID,
    *,
    namespace: Namespace,
    content: str,
    lifecycle_state: LifecycleState,
) -> UUID:
    source_reference_id = uuid4()
    memory_id = uuid4()
    now = datetime.now(timezone.utc)
    with container.database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, ?, ?, null, ?, ?, 'test-v1', ?, ?, ?)
            """,
            (
                str(source_reference_id),
                namespace.source_agent.value,
                f"display-record-{source_reference_id}",
                hashlib.sha256(str(source_reference_id).encode()).hexdigest(),
                now.isoformat(),
                now.isoformat(),
                str(project_id),
                namespace.model_id,
            ),
        )
        connection.execute(
            """
            insert into behavior_memories(
                memory_id, project_id, source_agent, model_id, task_fingerprint,
                memory_kind, normalized_content, content_hash, source_reference_id,
                created_at, confidence, lifecycle_state
            ) values (?, ?, ?, ?, ?, 'open_issue', ?, ?, ?, ?, 0.9, ?)
            """,
            (
                str(memory_id),
                str(project_id),
                namespace.source_agent.value,
                namespace.model_id,
                hashlib.sha256(f"display-task-{memory_id}".encode()).hexdigest(),
                content,
                hashlib.sha256(content.encode()).hexdigest(),
                str(source_reference_id),
                now.isoformat(),
                lifecycle_state.value,
            ),
        )
    return memory_id


def _insert_resolution_audit(
    container,
    project_id: UUID,
    *,
    namespace: Namespace,
    content: str,
    status: str,
    target_memory_id: UUID | None,
) -> None:
    source_reference_id = uuid4()
    now = datetime.now(timezone.utc)
    with container.database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, ?, ?, null, ?, ?, 'test-v1', ?, ?, ?)
            """,
            (
                str(source_reference_id),
                namespace.source_agent.value,
                f"resolution-record-{source_reference_id}",
                hashlib.sha256(str(source_reference_id).encode()).hexdigest(),
                now.isoformat(),
                now.isoformat(),
                str(project_id),
                namespace.model_id,
            ),
        )
        connection.execute(
            """
            insert into memory_issue_resolutions(
                resolution_id, project_id, source_agent, model_id,
                target_content_hash, target_memory_id, source_reference_id,
                status, resolved_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                str(project_id),
                namespace.source_agent.value,
                namespace.model_id,
                hashlib.sha256(content.encode()).hexdigest(),
                str(target_memory_id) if target_memory_id is not None else None,
                str(source_reference_id),
                status,
                now.isoformat(),
            ),
        )


def _unsafe(csrf: str) -> dict[str, str]:
    return {"origin": "http://127.0.0.1", "x-csrf-token": csrf}


def test_overview_and_sources_report_only_truthful_local_state(tmp_path: Path) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            _insert_memory(
                container,
                project.project_id,
                model_id="gpt-5",
                content="stored decision",
            )
            with container.database.transaction() as connection:
                connection.execute(
                    """
                    insert into app_state(name, value_json, updated_at)
                    values ('last_reconcile_success', ?, ?)
                    """,
                    (
                        json.dumps({"timestamp": "2026-07-13T03:30:00Z"}),
                        "2026-07-13T03:30:00Z",
                    ),
                )
            _app, client, csrf = await _client(container)
            async with client:
                overview = await client.get("/")
                sources = await client.get("/sources")
                unavailable = await client.post("/sources/trae/enable", headers=_unsafe(csrf))
                return overview, sources, unavailable

    overview, sources, unavailable = asyncio.run(scenario())
    assert overview.status_code == sources.status_code == 200
    assert "1 project" in overview.text
    assert "1 behavior memory" in overview.text
    assert "2026-07-13T03:30:00Z" in overview.text
    assert "Recall size" in overview.text and "not recorded" in overview.text
    for enabled in ("Codex", "ChatGPT"):
        assert enabled in sources.text
    for unavailable_name in (
        "Trae",
        "WorkBuddy",
        "Zcode",
        "QoderWork",
        "Claude Code",
    ):
        assert unavailable_name in sources.text
    assert sources.text.count("Unavailable") >= 5
    assert unavailable.status_code == 409


def test_language_switch_assets_are_global_and_local_only(tmp_path: Path) -> None:
    async def scenario() -> tuple[dict[str, httpx.Response], httpx.Response]:
        with _container(tmp_path) as container:
            _app, client, _csrf = await _client(container)
            async with client:
                pages = {
                    path: await client.get(path)
                    for path in (
                        "/",
                        "/sources",
                        "/projects",
                        "/memories",
                        "/imports",
                        "/proposals",
                        "/settings",
                    )
                }
                script = await client.get("/static/i18n.js")
            return pages, script

    pages, script = asyncio.run(scenario())

    for path, response in pages.items():
        assert response.status_code == 200, path
        assert '<html lang="en">' in response.text
        assert response.text.count('<script src="/static/i18n.js" defer></script>') == 1
        assert 'class="language-switch"' in response.text
        assert re.search(
            r'<button[^>]+type="button"[^>]+data-language-option="zh-CN"[^>]*>中文</button>',
            response.text,
        )
        assert re.search(
            r'<button[^>]+type="button"[^>]+data-language-option="en"[^>]*>English</button>',
            response.text,
        )
        assert 'data-i18n="nav.sources"' in response.text
        assert "onclick=" not in response.text
        csp = response.headers["content-security-policy"]
        assert "script-src 'self'" in csp
        assert "'unsafe-inline'" not in csp
        assert "'unsafe-eval'" not in csp
        script_sources = re.findall(r'<script[^>]+src="([^"]+)"', response.text)
        assert script_sources
        assert all(source.startswith("/static/") for source in script_sources)
    assert script.status_code == 200
    assert script.headers["content-type"].startswith(("application/javascript", "text/javascript"))
    assert "localStorage" in script.text
    assert "pmh-language" in script.text
    assert "fetch(" not in script.text
    assert "XMLHttpRequest" not in script.text
    assert "http://" not in script.text
    assert "https://" not in script.text


def test_sources_get_runs_only_light_probes(tmp_path: Path) -> None:
    class ProbeCalls:
        def __init__(self) -> None:
            self.light_calls = 0
            self.reserve_calls = 0
            self.structure_calls = 0
            self.light_thread: int | None = None

        def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
            self.light_calls += 1
            self.light_thread = threading.get_ident()
            return _optional_probe_results()

        def reserve_structure(self, _source: SourceAgent) -> None:
            self.reserve_calls += 1
            raise AssertionError("GET must not reserve a structure probe")

        def probe_one(self, _source: SourceAgent, *, mode: ProbeMode) -> None:
            if mode is ProbeMode.STRUCTURE:
                self.structure_calls += 1
            raise AssertionError("GET must not run an individual probe")

    probes = ProbeCalls()

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    request_thread = threading.get_ident()
    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert probes.light_calls == 1
    assert probes.light_thread is not None and probes.light_thread != request_thread
    assert probes.reserve_calls == 0
    assert probes.structure_calls == 0


def test_sources_get_localizes_probe_failure(tmp_path: Path) -> None:
    healthy = {
        source: _probe_result(
            source,
            installation_status=InstallationStatus.DETECTED,
            data_status=DataStatus.READABLE,
            warning_codes=(),
        )
        for source in (
            SourceAgent.TRAE,
            SourceAgent.WORKBUDDY,
            SourceAgent.ZCODE,
            SourceAgent.QODERWORK,
            SourceAgent.CLAUDE_CODE,
        )
    }
    healthy[SourceAgent.WORKBUDDY] = _probe_result(
        SourceAgent.WORKBUDDY,
        warning_codes=(ProbeWarningCode.PROBE_FAILED,),
    )

    class LocalizedFailure:
        def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
            return _optional_probe_results(healthy)

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = LocalizedFailure()  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "probe_failed" in _source_row(response, SourceAgent.WORKBUDDY)
    for source in (
        SourceAgent.TRAE,
        SourceAgent.ZCODE,
        SourceAgent.QODERWORK,
        SourceAgent.CLAUDE_CODE,
    ):
        row = _source_row(response, source)
        assert "Readable" in row
        assert "probe_failed" not in row


def test_registered_sources_preserve_enable_disable_controls(tmp_path: Path) -> None:
    class LightOnly:
        def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
            return _optional_probe_results()

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = LightOnly()  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    for source in (SourceAgent.CODEX, SourceAgent.CHATGPT):
        row = _source_row(response, source)
        assert "Available" in row
        assert "Desired: Enabled" in row
        assert "Runtime: Enabled" in row
        assert f'action="/sources/{source.value}/disable"' in row
        assert ">Disable</button>" in row


def test_optional_sources_never_render_enable_or_import_controls(tmp_path: Path) -> None:
    class LightOnly:
        def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
            return tuple(
                _probe_result(
                    source,
                    installation_status=InstallationStatus.DETECTED,
                    data_status=DataStatus.READABLE,
                    warning_codes=(),
                )
                for source in (
                    SourceAgent.TRAE,
                    SourceAgent.WORKBUDDY,
                    SourceAgent.ZCODE,
                    SourceAgent.QODERWORK,
                    SourceAgent.CLAUDE_CODE,
                )
            )

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = LightOnly()  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    for source in (
        SourceAgent.TRAE,
        SourceAgent.WORKBUDDY,
        SourceAgent.ZCODE,
        SourceAgent.QODERWORK,
        SourceAgent.CLAUDE_CODE,
    ):
        row = _source_row(response, source)
        assert f'action="/sources/{source.value}/enable"' not in row
        assert f'action="/sources/{source.value}/disable"' not in row
        assert "Import" not in row
        assert "Locked" in row
        if source is not SourceAgent.TRAE:
            assert "structure-probe" not in row
            assert "<form" not in row


@pytest.mark.parametrize(
    ("data_status", "button_enabled"),
    (
        (DataStatus.READABLE, True),
        (DataStatus.BLOCKED, False),
        (DataStatus.MISSING, False),
        (DataStatus.REJECTED, False),
    ),
)
def test_trae_button_requires_readable_data_root(
    tmp_path: Path,
    data_status: DataStatus,
    button_enabled: bool,
) -> None:
    result = _probe_result(
        SourceAgent.TRAE,
        installation_status=InstallationStatus.DETECTED,
        data_status=data_status,
        warning_codes=(),
    )

    class SelectedStatus:
        def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
            return _optional_probe_results({SourceAgent.TRAE: result})

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = SelectedStatus()  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())
    row = _source_row(response, SourceAgent.TRAE)
    match = re.search(r'<button[^>]+data-action="structure-probe"[^>]*>', row)

    assert response.status_code == 200
    assert match is not None
    assert (" disabled" not in match.group(0)) is button_enabled


def test_optional_source_labels_use_enum_whitelists(tmp_path: Path) -> None:
    results = {
        SourceAgent.TRAE: _probe_result(
            SourceAgent.TRAE,
            installation_status=InstallationStatus.DETECTED,
            data_status=DataStatus.READABLE,
            warning_codes=(),
        ),
        SourceAgent.WORKBUDDY: _probe_result(SourceAgent.WORKBUDDY),
        SourceAgent.ZCODE: _probe_result(
            SourceAgent.ZCODE,
            installation_status=InstallationStatus.DETECTED,
            data_status=DataStatus.BLOCKED,
            warning_codes=(ProbeWarningCode.PERMISSION_BLOCKED,),
        ),
        SourceAgent.QODERWORK: _probe_result(
            SourceAgent.QODERWORK,
            data_status=DataStatus.REJECTED,
            warning_codes=(ProbeWarningCode.SYMLINK_REJECTED,),
        ),
        SourceAgent.CLAUDE_CODE: _probe_result(SourceAgent.CLAUDE_CODE),
    }

    class WhitelistResults:
        def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
            return _optional_probe_results(results)

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = WhitelistResults()  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    expected = {
        SourceAgent.TRAE: ("detected", "Detected", "readable", "Readable"),
        SourceAgent.WORKBUDDY: ("not-detected", "Not detected", "missing", "Missing"),
        SourceAgent.ZCODE: (
            "detected",
            "Detected",
            "blocked",
            "Permission blocked",
        ),
        SourceAgent.QODERWORK: (
            "not-detected",
            "Not detected",
            "rejected",
            "Rejected",
        ),
        SourceAgent.CLAUDE_CODE: (
            "not-detected",
            "Not detected",
            "missing",
            "Missing",
        ),
    }
    for source, (detected_class, detected_label, health_class, health_label) in expected.items():
        row = _source_row(response, source)
        assert re.search(
            rf'<span[^>]*class="status {detected_class}"[^>]*>{re.escape(detected_label)}</span>',
            row,
        )
        assert re.search(
            rf'<span[^>]*class="status {health_class}"[^>]*>{re.escape(health_label)}</span>',
            row,
        )
        assert re.search(r'<span[^>]*class="status not-checked"[^>]*>Not checked</span>', row)
        assert re.search(r'<span[^>]*class="status locked"[^>]*>Locked</span>', row)
        assert "Not run" in row
        if source is SourceAgent.TRAE:
            assert "Structure metadata" in row
        else:
            assert "Presence and access check" in row


def test_optional_probe_results_fail_closed_when_missing_or_duplicate(
    tmp_path: Path,
) -> None:
    duplicate = _probe_result(
        SourceAgent.TRAE,
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        warning_codes=(),
    )

    class IncompleteResults:
        def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
            return (
                duplicate,
                duplicate,
                _probe_result(SourceAgent.ZCODE),
                _probe_result(SourceAgent.QODERWORK),
                _probe_result(SourceAgent.CLAUDE_CODE),
            )

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = IncompleteResults()  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.text.count("<tr data-source=") == len(SourceAgent)
    for source in (SourceAgent.TRAE, SourceAgent.WORKBUDDY):
        row = _source_row(response, source)
        assert re.search(r'<span[^>]*class="status not-detected"[^>]*>Not detected</span>', row)
        assert re.search(r'<span[^>]*class="status missing"[^>]*>Missing</span>', row)
        assert re.search(r'<span[^>]*class="status not-checked"[^>]*>Not checked</span>', row)
        assert "Not run" in row
        assert "probe_failed" in row
        if source is SourceAgent.TRAE:
            button = re.search(r'<button[^>]+data-action="structure-probe"[^>]*>', row)
            assert button is not None and " disabled" in button.group(0)


def test_trae_probe_post_renders_structure_result_without_redirect(tmp_path: Path) -> None:
    probes = _RecordingProbeService()

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                return await _post_trae_probe(client, csrf)

    response = asyncio.run(scenario())
    row = _source_row(response, SourceAgent.TRAE)

    assert response.status_code == 200
    assert "location" not in response.headers
    assert re.search(r'<span[^>]*class="status unverifiable"[^>]*>Unverifiable</span>', row)
    assert "Partial" in row
    assert "model_id_unverifiable" in row
    assert re.search(r'<span[^>]*class="status locked"[^>]*>Locked</span>', row)


def test_trae_probe_reserves_before_to_thread(tmp_path: Path) -> None:
    probes = _RecordingProbeService()

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                return await _post_trae_probe(client, csrf)

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert probes.calls == ["reserve", "worker start", "worker close", "light"]
    assert probes.reserve_calls == probes.structure_calls == probes.light_calls == 1


def test_trae_probe_response_waits_for_lease_close(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    probes = _RecordingProbeService(started=started, release=release)

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                request = asyncio.create_task(_post_trae_probe(client, csrf))
                try:
                    assert await asyncio.to_thread(started.wait, 1)
                    await asyncio.sleep(0)
                    assert not request.done()
                    assert probes.leases[-1].state == "running"
                    release.set()
                    response = await request
                finally:
                    release.set()
                assert probes.leases[-1].state == "closed"
                retry = probes.reserve_structure(SourceAgent.TRAE)
                retry.close()
                return response

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert probes.calls[:4] == ["reserve", "worker start", "worker close", "light"]


def test_trae_probe_scheduler_failure_closes_unstarted_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = _RecordingProbeService()
    scheduled: list[object] = []

    def reject_schedule(coroutine: object, *_args: object, **_kwargs: object) -> NoReturn:
        scheduled.append(coroutine)
        raise RuntimeError("synthetic scheduler failure")

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                with monkeypatch.context() as scoped:
                    scoped.setattr(routes_module.asyncio, "create_task", reject_schedule)
                    response = await _post_trae_probe(client, csrf)
            retry = probes.reserve_structure(SourceAgent.TRAE)
            retry.close()
            return response

    response = asyncio.run(scenario())

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("text/html")
    assert "Operation failed" in response.text
    assert "操作失败" in response.text
    assert "synthetic scheduler failure" not in response.text
    assert len(scheduled) == 1
    assert inspect.getcoroutinestate(scheduled[0]) == inspect.CORO_CLOSED
    assert probes.leases[0].state == "closed"
    assert probes.structure_calls == probes.light_calls == 0


def test_trae_probe_cancellation_waits_for_running_worker(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    probes = _RecordingProbeService(started=started, release=release)

    async def scenario() -> None:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                request = asyncio.create_task(_post_trae_probe(client, csrf))
                try:
                    assert await asyncio.to_thread(started.wait, 1)
                    request.cancel()
                    await asyncio.sleep(0)
                    assert not request.done()
                    assert probes.leases[-1].state == "running"
                    release.set()
                    with pytest.raises(asyncio.CancelledError):
                        await request
                finally:
                    release.set()
                    if not request.done():
                        try:
                            await request
                        except asyncio.CancelledError:
                            pass
            assert probes.leases[-1].state == "closed"
            retry = probes.reserve_structure(SourceAgent.TRAE)
            retry.close()

    asyncio.run(scenario())

    assert probes.structure_calls == 1
    assert probes.light_calls == 0


def test_trae_probe_busy_returns_409_without_starting_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = _RecordingProbeService(busy=True)

    def unexpected_to_thread(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("busy route created a worker")

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                with monkeypatch.context() as scoped:
                    scoped.setattr(routes_module.asyncio, "to_thread", unexpected_to_thread)
                    return await _post_trae_probe(client, csrf)

    response = asyncio.run(scenario())
    row = _source_row(response, SourceAgent.TRAE)

    assert response.status_code == 409
    assert "Probe busy" in row
    assert "probe_busy" in row
    assert "Locked" in row
    assert probes.reserve_calls == 1
    assert probes.structure_calls == probes.light_calls == 0


def test_trae_probe_followup_get_returns_light(tmp_path: Path) -> None:
    readable_light = _optional_probe_results(
        {
            SourceAgent.TRAE: _probe_result(
                SourceAgent.TRAE,
                installation_status=InstallationStatus.DETECTED,
                data_status=DataStatus.READABLE,
                warning_codes=(),
            )
        }
    )
    probes = _RecordingProbeService(light_results=readable_light)

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                structure = await _post_trae_probe(client, csrf)
                light = await client.get("/sources")
                return structure, light

    structure, light = asyncio.run(scenario())
    structure_row = _source_row(structure, SourceAgent.TRAE)
    light_row = _source_row(light, SourceAgent.TRAE)

    assert structure.status_code == light.status_code == 200
    assert "Unverifiable" in structure_row
    assert "model_id_unverifiable" in structure_row
    assert "Not checked" in light_row
    assert "Not run" in light_row
    assert "model_id_unverifiable" not in light_row
    assert probes.reserve_calls == 1
    assert probes.light_calls == 2


def test_trae_probe_form_contains_exactly_one_csrf_field(tmp_path: Path) -> None:
    readable = _optional_probe_results(
        {
            SourceAgent.TRAE: _probe_result(
                SourceAgent.TRAE,
                installation_status=InstallationStatus.DETECTED,
                data_status=DataStatus.READABLE,
                warning_codes=(),
            )
        }
    )
    probes = _RecordingProbeService(light_results=readable)

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())
    row = _source_row(response, SourceAgent.TRAE)
    match = re.search(
        r'(<form method="post" action="/sources/trae/probe">.*?</form>)',
        row,
        flags=re.DOTALL,
    )

    assert response.status_code == 200
    assert match is not None
    assert re.findall(r'name="([^"]+)"', match.group(1)) == ["csrf_token"]


def test_trae_probe_post_writes_no_session_cookie_database_or_cache(
    tmp_path: Path,
) -> None:
    private_body = "NEVER_RENDER_TRAE_PRIVATE_BODY"

    async def scenario() -> tuple[httpx.Response, bool, bool]:
        with _container(tmp_path) as container:
            session_memory = tmp_path / "probe-home" / ".trae" / "session_memory"
            session_memory.mkdir(parents=True)
            (session_memory / "metadata.json").write_text(
                private_body,
                encoding="utf-8",
            )
            _app, client, csrf = await _client(container)
            before_runtime = _tree_snapshot(container.paths.root)
            before_probe_home = _tree_snapshot(tmp_path / "probe-home")
            async with client:
                response = await _post_trae_probe(client, csrf)
            return (
                response,
                _tree_snapshot(container.paths.root) == before_runtime,
                _tree_snapshot(tmp_path / "probe-home") == before_probe_home,
            )

    response, runtime_unchanged, probe_home_unchanged = asyncio.run(scenario())

    assert response.status_code == 200
    assert "set-cookie" not in response.headers
    assert private_body not in response.text
    assert runtime_unchanged
    assert probe_home_unchanged


def test_sources_page_never_renders_private_probe_metadata(tmp_path: Path) -> None:
    sentinels = (
        "/private/NEVER_RENDER_PROBE_PATH",
        "NEVER_RENDER_SCHEMA_IDENTIFIER",
        "NEVER_RENDER_EXCEPTION_BODY",
        "NEVER_RENDER_SESSION_CONTENT",
    )
    result = _probe_result(
        SourceAgent.TRAE,
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        warning_codes=(ProbeWarningCode.PROBE_FAILED,),
    )
    private_result = SimpleNamespace(
        **result.model_dump(),
        path=sentinels[0],
        schema_identifier=sentinels[1],
        exception=RuntimeError(sentinels[2]),
        body=sentinels[3],
    )

    class PrivateMetadata:
        def probe_all_light(self) -> tuple[object, ...]:
            return (
                private_result,
                *_optional_probe_results()[1:],
            )

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = PrivateMetadata()  # type: ignore[assignment]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())

    assert response.status_code == 200
    for sentinel in sentinels:
        assert sentinel not in response.text


def test_projects_toggle_and_report_persisted_permission_state(tmp_path: Path) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, int]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            with container.database.transaction() as connection:
                connection.execute(
                    "update projects set permission_status = 'blocked_permission' "
                    "where project_id = ?",
                    (str(project.project_id),),
                )
            _app, client, csrf = await _client(container)
            async with client:
                page = await client.get("/projects")
                disabled = await client.post(
                    f"/projects/{project.project_id}/disable",
                    headers=_unsafe(csrf),
                )
            with container.database.connect(readonly=True) as connection:
                enabled = connection.execute(
                    "select enabled from projects where project_id = ?",
                    (str(project.project_id),),
                ).fetchone()[0]
            return page, disabled, enabled

    page, disabled, enabled = asyncio.run(scenario())
    assert page.status_code == 200
    assert "blocked_permission" in page.text
    assert "Duplicate candidates" in page.text and "not recorded" in page.text
    assert disabled.status_code == 303
    assert enabled == 0


def test_project_relink_maps_a_disappearing_destination_to_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> tuple[httpx.Response, str]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            destination = tmp_path / "destination"
            destination.mkdir()
            original_relink = container.projects.relink

            def disappear_then_relink(project_id: UUID, new_path: Path):
                destination.rmdir()
                return original_relink(project_id, new_path)

            monkeypatch.setattr(container.projects, "relink", disappear_then_relink)
            _app, client, csrf = await _client(container)
            async with client:
                response = await client.post(
                    f"/projects/{project.project_id}/relink",
                    headers=_unsafe(csrf),
                    data={"new_path": str(destination)},
                )
            stored = container.projects.get(project.project_id)
            return response, str(stored.canonical_path)

    response, stored_path = asyncio.run(scenario())

    assert response.status_code == 409
    assert stored_path == str((tmp_path / "project-a").resolve())


def test_project_relink_maps_invalid_form_path_to_conflict(tmp_path: Path) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            _app, client, csrf = await _client(container)
            async with client:
                return await client.post(
                    f"/projects/{project.project_id}/relink",
                    headers=_unsafe(csrf),
                    data={"new_path": str(tmp_path / "missing-destination")},
                )

    assert asyncio.run(scenario()).status_code == 409


def test_project_relink_does_not_hide_unexpected_storage_errors(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            destination = tmp_path / "destination"
            destination.mkdir()

            def fail_relink(_project_id: UUID, _new_path: Path):
                raise OSError(errno.EIO, "synthetic storage failure")

            monkeypatch.setattr(container.projects, "relink", fail_relink)
            _app, client, csrf = await _client(container)
            async with client:
                return await client.post(
                    f"/projects/{project.project_id}/relink",
                    headers=_unsafe(csrf),
                    data={"new_path": str(destination)},
                )

    response = asyncio.run(scenario())

    assert response.status_code == 500


def test_project_relink_maps_a_permission_race_to_conflict(
    tmp_path: Path,
    monkeypatch,
) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            destination = tmp_path / "destination"
            destination.mkdir()

            def fail_relink(_project_id: UUID, _new_path: Path):
                raise PermissionError("synthetic permission race")

            monkeypatch.setattr(container.projects, "relink", fail_relink)
            _app, client, csrf = await _client(container)
            async with client:
                return await client.post(
                    f"/projects/{project.project_id}/relink",
                    headers=_unsafe(csrf),
                    data={"new_path": str(destination)},
                )

    assert asyncio.run(scenario()).status_code == 409


def test_reconcile_persists_real_discovery_issues_and_duplicates_for_web(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scan_root = tmp_path / "scan-root"
    blocked_root = tmp_path / "blocked-root"
    scan_root.mkdir()
    blocked_root.mkdir()
    first = scan_root / "first"
    second = scan_root / "second"
    first.mkdir()
    second.mkdir()
    for project in (first, second):
        (project / "package.json").write_text('{"name":"duplicate-app"}', encoding="utf-8")

    runtime = tmp_path / "runtime-findings"
    runtime.mkdir(mode=0o700)
    config_path = runtime / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(scan_root, blocked_root),
            enabled_sources=(SourceAgent.CHATGPT,),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    original_open = scanner_module._open_allowed_root

    def selective_open(path: Path) -> int:
        if path == blocked_root.resolve():
            raise PermissionError("synthetic blocked root")
        return original_open(path)

    monkeypatch.setattr(scanner_module, "_open_allowed_root", selective_open)
    probe_home = tmp_path / "findings-probe-home"
    probe_home.mkdir()
    with build_container(config_path, probe_home=probe_home) as container:
        report = container.reconcile.run(force=True)

    async def pages() -> tuple[httpx.Response, httpx.Response]:
        with build_container(config_path, probe_home=probe_home) as container:
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/projects"), await client.get("/")

    projects, overview = asyncio.run(pages())
    assert report.stages["discover"] == "error"
    assert projects.status_code == overview.status_code == 200
    assert "Persisted discovery findings" in projects.text
    assert "blocked_permission" in projects.text
    assert str(blocked_root.resolve()) in projects.text
    assert "grant Files and Folders or Full Disk Access" in projects.text
    assert "Duplicate candidate groups" in projects.text
    assert "manifest" in projects.text
    assert str(first.resolve()) in projects.text
    assert str(second.resolve()) in projects.text
    assert "1 permission error" in overview.text


def test_memories_require_exact_filters_and_escape_stored_content(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            _insert_memory(
                container,
                project.project_id,
                model_id="model-a",
                content="MODEL_A_ONLY <script>alert('x')</script>",
            )
            _insert_memory(
                container,
                project.project_id,
                model_id="model-b",
                content="MODEL_B_PRIVATE",
            )
            _app, client, _csrf = await _client(container)
            async with client:
                empty = await client.get("/memories")
                filtered = await client.get(
                    "/memories",
                    params={
                        "project_id": str(project.project_id),
                        "source_agent": "codex",
                        "model_id": "model-a",
                    },
                )
                return empty, filtered

    empty, filtered = asyncio.run(scenario())
    assert empty.status_code == filtered.status_code == 200
    assert "Choose a project, source, and model" in empty.text
    assert "MODEL_A_ONLY" not in empty.text
    assert "MODEL_B_PRIVATE" not in empty.text
    assert "MODEL_A_ONLY" in filtered.text
    assert "MODEL_B_PRIVATE" not in filtered.text
    assert "<script>" not in filtered.text
    assert "&lt;script&gt;" in filtered.text


def test_projects_page_renders_complete_progressive_browser_contract(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, tuple[tuple[UUID, Path], ...]]:
        with _container(tmp_path) as container:
            registered = tuple(
                (
                    project.project_id,
                    project.canonical_path,
                )
                for index in range(25)
                for project in (
                    _register(
                        container,
                        tmp_path / f"project-{index:02d}",
                        name=f"Project {index:02d}",
                    ),
                )
            )
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/projects"), registered

    response, registered = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.text.count('<script src="/static/projects.js" defer></script>') == 1
    assert "data-project-browser" in response.text
    assert "data-project-search" in response.text
    assert "data-project-status-filter" in response.text
    assert "data-project-visible-count" in response.text
    assert 'data-project-total-count="25"' in response.text
    assert "data-project-show-more" in response.text
    assert response.text.count('class="project-card"') == 25

    opening_cards = re.findall(r'<article class="project-card"[^>]*>', response.text)
    assert len(opening_cards) == 25
    assert all(" hidden" not in card for card in opening_cards)
    assert all("data-project-name=" in card for card in opening_cards)
    assert all("data-project-id=" in card for card in opening_cards)
    assert all("data-project-status=" in card for card in opening_cards)

    for project_id, canonical_path in registered:
        assert f'data-project-id="{project_id}"' in response.text
        assert re.search(
            rf"<details[^>]*data-project-path[^>]*>.*?{re.escape(str(canonical_path))}.*?</details>",
            response.text,
            flags=re.DOTALL,
        )
        assert f'data-project-search="{canonical_path}"' not in response.text


def test_every_template_empty_state_comes_from_the_structured_macro() -> None:
    templates = Path(__file__).resolve().parents[2] / "src/project_memory_hub/web/templates"
    macro = (templates / "_empty_state.html").read_text(encoding="utf-8")

    assert 'class="empty empty-state"' in macro
    for template in templates.glob("*.html"):
        if template.name == "_empty_state.html":
            continue
        class_values = re.findall(
            r"class\s*=\s*[\"']([^\"']*)[\"']",
            template.read_text(encoding="utf-8"),
        )
        assert all("empty" not in value.split() for value in class_values), template.name


def test_projects_empty_states_explain_reason_next_step_and_success(
    tmp_path: Path,
) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/projects")

    response = asyncio.run(scenario())
    blocks = re.findall(
        r'<section class="empty empty-state"[^>]*>(.*?)</section>',
        response.text,
        flags=re.DOTALL,
    )

    assert len(blocks) == 3
    assert response.text.count("no action required") >= 2
    for block in blocks:
        assert block.count("data-empty-reason") == 1
        assert block.count("data-empty-next-step") == 1
        assert block.count("data-empty-success") == 1
        assert block.count("<code>") == 1


def test_memories_distinguish_resolved_and_manually_archived_in_exact_namespace(
    tmp_path: Path,
) -> None:
    resolved_content = "SELECTED_RESOLVED_ISSUE"
    shared_archived_content = "SHARED_ARCHIVED_COLLISION"
    active_content = "ACTIVE_WITH_RESOLUTION_AUDIT"

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            selected = Namespace(source_agent=SourceAgent.CODEX, model_id="model-a")
            foreign = Namespace(source_agent=SourceAgent.CODEX, model_id="model-b")
            resolved_id = _insert_display_open_issue(
                container,
                project.project_id,
                namespace=selected,
                content=resolved_content,
                lifecycle_state=LifecycleState.ARCHIVED,
            )
            _insert_display_open_issue(
                container,
                project.project_id,
                namespace=selected,
                content=shared_archived_content,
                lifecycle_state=LifecycleState.ARCHIVED,
            )
            active_id = _insert_display_open_issue(
                container,
                project.project_id,
                namespace=selected,
                content=active_content,
                lifecycle_state=LifecycleState.ACTIVE,
            )
            foreign_id = _insert_display_open_issue(
                container,
                project.project_id,
                namespace=foreign,
                content=shared_archived_content,
                lifecycle_state=LifecycleState.ARCHIVED,
            )
            _insert_resolution_audit(
                container,
                project.project_id,
                namespace=selected,
                content=resolved_content,
                status="resolved",
                target_memory_id=resolved_id,
            )
            _insert_resolution_audit(
                container,
                project.project_id,
                namespace=selected,
                content=shared_archived_content,
                status="not_found",
                target_memory_id=None,
            )
            _insert_resolution_audit(
                container,
                project.project_id,
                namespace=selected,
                content=active_content,
                status="resolved",
                target_memory_id=active_id,
            )
            _insert_resolution_audit(
                container,
                project.project_id,
                namespace=foreign,
                content=shared_archived_content,
                status="resolved",
                target_memory_id=foreign_id,
            )
            _app, client, _csrf = await _client(container)
            async with client:
                exact = await client.get(
                    "/memories",
                    params={
                        "project_id": str(project.project_id),
                        "source_agent": "codex",
                        "model_id": "model-a",
                    },
                )
                incomplete = await client.get(
                    "/memories",
                    params={
                        "project_id": str(project.project_id),
                        "source_agent": "codex",
                    },
                )
                return exact, incomplete

    exact, incomplete = asyncio.run(scenario())

    assert exact.status_code == incomplete.status_code == 200
    assert re.search(
        r'<header><span[^>]*class="status"[^>]*>open_issue</span><span[^>]*>Resolved</span></header>\s*'
        rf"<p>{resolved_content}</p>",
        exact.text,
    )
    assert re.search(
        r'<header><span[^>]*class="status"[^>]*>open_issue</span><span[^>]*>Archived</span></header>\s*'
        rf"<p>{shared_archived_content}</p>",
        exact.text,
    )
    assert re.search(
        r'<header><span[^>]*class="status"[^>]*>open_issue</span><span[^>]*>Active</span></header>\s*'
        rf"<p>{active_content}</p>",
        exact.text,
    )
    assert len(re.findall(r"<span[^>]*>Resolved</span>", exact.text)) == 1
    assert exact.text.count(shared_archived_content) == 1
    for content in (resolved_content, shared_archived_content, active_content):
        assert content not in incomplete.text


def test_memories_use_one_bounded_resolution_lookup_and_skip_incomplete_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> tuple[
        httpx.Response,
        httpx.Response,
        list[tuple[UUID, Namespace, tuple[UUID, ...]]],
        list[tuple[UUID, Namespace, int]],
        set[UUID],
    ]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="model-a")
            expected_ids = {
                _insert_display_open_issue(
                    container,
                    project.project_id,
                    namespace=namespace,
                    content=f"bounded issue {index}",
                    lifecycle_state=LifecycleState.ARCHIVED,
                )
                for index in range(3)
            }
            resolution_calls: list[tuple[UUID, Namespace, tuple[UUID, ...]]] = []
            memory_calls: list[tuple[UUID, Namespace, int]] = []
            assert container.capture._issue_resolutions is container.issue_resolutions
            original_resolution_lookup = container.issue_resolutions.resolved_target_ids_scoped
            original_memory_lookup = container.memories.list_scoped

            def counted_resolution_lookup(
                connection,
                *,
                project_id: UUID,
                namespace: Namespace,
                memory_ids: tuple[UUID, ...],
            ):
                resolution_calls.append((project_id, namespace, tuple(memory_ids)))
                return original_resolution_lookup(
                    connection,
                    project_id=project_id,
                    namespace=namespace,
                    memory_ids=memory_ids,
                )

            def counted_memory_lookup(
                project_id: UUID,
                selected_namespace: Namespace,
                *,
                limit: int = 100,
            ):
                memory_calls.append((project_id, selected_namespace, limit))
                return original_memory_lookup(
                    project_id,
                    selected_namespace,
                    limit=limit,
                )

            monkeypatch.setattr(
                container.issue_resolutions,
                "resolved_target_ids_scoped",
                counted_resolution_lookup,
            )
            monkeypatch.setattr(container.memories, "list_scoped", counted_memory_lookup)
            _app, client, _csrf = await _client(container)
            async with client:
                exact = await client.get(
                    "/memories",
                    params={
                        "project_id": str(project.project_id),
                        "source_agent": "codex",
                        "model_id": "model-a",
                    },
                )
                incomplete = await client.get(
                    "/memories",
                    params={
                        "project_id": str(project.project_id),
                        "source_agent": "codex",
                    },
                )
            return exact, incomplete, resolution_calls, memory_calls, expected_ids

    exact, incomplete, resolution_calls, memory_calls, expected_ids = asyncio.run(scenario())

    assert exact.status_code == incomplete.status_code == 200
    assert len(resolution_calls) == 1
    project_id, namespace, memory_ids = resolution_calls[0]
    assert namespace == Namespace(source_agent=SourceAgent.CODEX, model_id="model-a")
    assert set(memory_ids) == expected_ids
    assert len(memory_ids) <= 100
    assert memory_calls == [(project_id, namespace, 100)]


def test_memories_show_safe_project_facts_without_querying_behavior_namespace(
    tmp_path: Path,
) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            container.facts.observe(
                project.project_id,
                ProjectFactInput(
                    category="tooling",
                    normalized_content="Shared project fact from pyproject.toml",
                    evidence_type="manifest",
                    evidence_reference="pyproject.toml",
                    observed_at=datetime.now(timezone.utc),
                    confidence=0.85,
                ),
            )

            def behavior_query_must_not_run(*_args, **_kwargs):
                raise AssertionError("behavior namespace query ran without exact filters")

            container.memories.list_scoped = behavior_query_must_not_run  # type: ignore[method-assign]
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/memories", params={"project_id": str(project.project_id)})

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert "Shared project facts" in response.text
    assert "Shared project fact from pyproject.toml" in response.text
    assert "tooling" in response.text
    assert "manifest" in response.text
    assert "0.85" in response.text
    assert "Choose an exact source and model" in response.text


def test_memories_redact_corrupted_rows_and_disable_unsafe_namespace_actions(
    tmp_path: Path,
) -> None:
    secret = "TOPSECRETTOKEN123456789"
    bearer = f"Bearer {secret}"

    async def scenario() -> tuple[httpx.Response, httpx.Response, str]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            memory_id = _insert_memory(
                container,
                project.project_id,
                model_id=bearer,
                content="safe behavior",
            )
            fact = container.facts.observe(
                project.project_id,
                ProjectFactInput(
                    category="tooling",
                    normalized_content="safe shared fact",
                    evidence_type="manifest",
                    evidence_reference="pyproject.toml",
                    observed_at=datetime.now(timezone.utc),
                    confidence=0.85,
                ),
            )
            with container.database.transaction() as connection:
                connection.execute(
                    "update behavior_memories set normalized_content = ? where memory_id = ?",
                    (f"corrupted behavior {bearer}", str(memory_id)),
                )
                connection.execute(
                    """
                    update project_facts
                    set category = ?, normalized_content = ?, evidence_type = ?,
                        evidence_reference = ?
                    where fact_id = ?
                    """,
                    (bearer, bearer, bearer, bearer, str(fact.fact_id)),
                )
            _app, client, csrf = await _client(container)
            query = {
                "project_id": str(project.project_id),
                "source_agent": "codex",
                "model_id": bearer,
            }
            async with client:
                page = await client.get("/memories", params=query)
                archive = await client.post(
                    f"/memories/{memory_id}/archive",
                    headers=_unsafe(csrf),
                    data={**query, "confirmation": "ARCHIVE"},
                )
            with container.database.connect(readonly=True) as connection:
                lifecycle = connection.execute(
                    "select lifecycle_state from behavior_memories where memory_id = ?",
                    (str(memory_id),),
                ).fetchone()[0]
            return page, archive, lifecycle

    page, archive, lifecycle = asyncio.run(scenario())
    assert page.status_code == 200
    assert secret not in page.text
    assert page.text.count("[REDACTED:bearer_token]") >= 2
    assert "/archive" not in page.text
    assert "/delete" not in page.text
    assert "/promote" not in page.text
    assert "Unsafe namespace metadata; memory actions unavailable" in page.text
    assert archive.status_code == 409
    assert secret not in archive.text
    assert lifecycle == "active"


def test_memory_lifecycle_and_promotion_enforce_complete_namespace_in_sql(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, int, int, str, int]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            memory_id = _insert_memory(
                container,
                project.project_id,
                model_id="model-a",
                content="share only after approval",
            )
            _app, client, csrf = await _client(container)
            base = {
                "project_id": str(project.project_id),
                "source_agent": "codex",
            }
            async with client:
                denied_archive = await client.post(
                    f"/memories/{memory_id}/archive",
                    headers=_unsafe(csrf),
                    data={**base, "model_id": "model-b", "confirmation": "ARCHIVE"},
                )
                denied_promote = await client.post(
                    f"/memories/{memory_id}/promote",
                    headers=_unsafe(csrf),
                    data={
                        **base,
                        "model_id": "model-b",
                        "proposed_rule": "safe shared rule",
                    },
                )
                requested = await client.post(
                    f"/memories/{memory_id}/promote",
                    headers=_unsafe(csrf),
                    data={
                        **base,
                        "model_id": "model-a",
                        "proposed_rule": "safe shared rule",
                    },
                )
                proposals = await client.get("/proposals")
                action = re.search(
                    r'action="/promotions/([0-9a-f-]{36})/approve"',
                    proposals.text,
                )
                assert action is not None
                promotion_id = action.group(1)
                assert "safe shared rule" in proposals.text
                assert str(project.project_id) in proposals.text
                assert "model-a" in proposals.text
                denied_approve = await client.post(
                    f"/promotions/{promotion_id}/approve",
                    headers=_unsafe(csrf),
                    data={
                        **base,
                        "model_id": "model-b",
                        "confirmation": "APPROVE",
                    },
                )
                approved = await client.post(
                    f"/promotions/{promotion_id}/approve",
                    headers=_unsafe(csrf),
                    data={
                        **base,
                        "model_id": "model-a",
                        "confirmation": "APPROVE",
                    },
                )
                archived = await client.post(
                    f"/memories/{memory_id}/archive",
                    headers=_unsafe(csrf),
                    data={**base, "model_id": "model-a", "confirmation": "ARCHIVE"},
                )
            with container.database.connect(readonly=True) as connection:
                lifecycle = connection.execute(
                    "select lifecycle_state from behavior_memories where memory_id = ?",
                    (str(memory_id),),
                ).fetchone()[0]
                fact_count = connection.execute(
                    "select count(*) from project_facts where category = 'approved_shared_rule'"
                ).fetchone()[0]
            flow_completed = (
                requested.status_code == 303
                and proposals.status_code == 200
                and approved.status_code == archived.status_code == 303
            )
            return (
                denied_archive.status_code,
                denied_promote.status_code,
                denied_approve.status_code,
                lifecycle,
                fact_count if flow_completed else -1,
            )

    assert asyncio.run(scenario()) == (404, 404, 404, "archived", 1)


def test_chatgpt_upload_is_private_dry_run_and_always_deleted(tmp_path: Path) -> None:
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("conversations.json", "[]")

    async def scenario() -> tuple[httpx.Response, httpx.Response, tuple[Path, ...]]:
        with _container(tmp_path) as container:
            _app, client, csrf = await _client(container)
            async with client:
                with archive.open("rb") as source:
                    dry = await client.post(
                        "/imports/chatgpt",
                        headers=_unsafe(csrf),
                        data={"dry_run": "true"},
                        files={"archive": ("NEVER_RENDER_CLIENT_NAME.zip", source)},
                    )

                def fail_import(_path: Path, *, dry_run: bool = False):
                    del dry_run
                    raise RuntimeError("NEVER_RENDER_IMPORT_FAILURE")

                container.chatgpt_adapter.import_zip = fail_import  # type: ignore[method-assign]
                with archive.open("rb") as source:
                    failed = await client.post(
                        "/imports/chatgpt",
                        headers=_unsafe(csrf),
                        files={"archive": ("NEVER_RENDER_CLIENT_NAME.zip", source)},
                    )
            remaining = tuple(path for path in container.paths.imports.rglob("*") if path.is_file())
            return dry, failed, remaining

    dry, failed, remaining = asyncio.run(scenario())
    assert dry.status_code == 303
    assert failed.status_code == 500
    assert remaining == ()
    for response in (dry, failed):
        assert "NEVER_RENDER_CLIENT_NAME" not in response.text
        assert "NEVER_RENDER_IMPORT_FAILURE" not in response.text


def test_chatgpt_upload_maps_reconcile_required_to_conflict_and_deletes_upload(
    tmp_path: Path,
) -> None:
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr("conversations.json", "[]")

    async def scenario() -> tuple[httpx.Response, tuple[Path, ...]]:
        with _container(tmp_path) as container:

            def fail_import(_path: Path, *, dry_run: bool = False):
                del dry_run
                raise ReconcileRequiredError("SENSITIVE_RECONCILE_DETAIL")

            container.chatgpt_adapter.import_zip = fail_import  # type: ignore[method-assign]
            _app, client, csrf = await _client(container)
            async with client:
                with archive.open("rb") as source:
                    response = await client.post(
                        "/imports/chatgpt",
                        headers=_unsafe(csrf),
                        files={"archive": ("NEVER_RENDER_CLIENT_NAME.zip", source)},
                    )
            remaining = tuple(path for path in container.paths.imports.rglob("*") if path.is_file())
            return response, remaining

    response, remaining = asyncio.run(scenario())

    assert response.status_code == 409
    assert remaining == ()
    assert "SENSITIVE_RECONCILE_DETAIL" not in response.text
    assert "NEVER_RENDER_CLIENT_NAME" not in response.text


def test_import_result_page_renders_only_bounded_counts(tmp_path: Path) -> None:
    marker = "NEVER_RENDER_QUERY_MARKER"

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            _app, client, _csrf = await _client(container)
            async with client:
                valid = await client.get("/imports?status=checked&matches=1&confirmations=2")
                invalid = await client.get(
                    f"/imports?status=checked&matches={marker}&confirmations=2"
                )
            return valid, invalid

    valid, invalid = asyncio.run(scenario())
    assert valid.status_code == invalid.status_code == 200
    assert "Dry-run matches: 1" in valid.text
    assert "Confirmation required: 2" in valid.text
    assert "Dry-run matches:" not in invalid.text
    assert marker not in invalid.text


def test_settings_validate_then_atomically_save_with_restart_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routes_module.ControlPanelService,
        "automation_status",
        lambda _self: "drifted",
    )

    async def scenario() -> tuple[
        httpx.Response,
        httpx.Response,
        httpx.Response,
        AppConfig,
        AppConfig,
    ]:
        with _container(tmp_path) as container:
            extra_root = tmp_path / "extra-projects"
            extra_root.mkdir()
            before = container.config_manager.load()
            _app, client, csrf = await _client(container)
            async with client:
                invalid = await client.post(
                    "/settings",
                    headers=_unsafe(csrf),
                    data={
                        "project_roots": str(extra_root),
                        "enabled_sources": ["codex", "chatgpt", "trae"],
                        "inactive_days": "21",
                        "max_recall_tokens": "800",
                        "daily_reconcile_time": "03:30",
                    },
                )
                unchanged = container.config_manager.load()
                valid = await client.post(
                    "/settings",
                    headers=_unsafe(csrf),
                    data={
                        "project_roots": str(extra_root),
                        "enabled_sources": ["codex", "chatgpt"],
                        "inactive_days": "30",
                        "max_recall_tokens": "700",
                        "daily_reconcile_time": "04:15",
                    },
                )
                status_page = await client.get(valid.headers["location"])
            saved = container.config_manager.load()
            assert container.config == before
            return invalid, valid, status_page, unchanged, saved

    invalid, valid, status_page, unchanged, saved = asyncio.run(scenario())
    assert invalid.status_code == 409
    assert unchanged.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)
    assert valid.status_code == 303
    assert "restart-required" in valid.headers["location"]
    assert saved.inactive_days == 30
    assert saved.max_recall_tokens == 700
    assert saved.daily_reconcile_time == "04:15"
    assert saved.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)
    assert status_page.status_code == 200
    assert "Desired automation" in status_page.text
    assert "drifted" in status_page.text
    assert "authorized Codex host interface" in status_page.text


def test_settings_reject_recall_budget_above_product_hard_limit(tmp_path: Path) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, AppConfig, AppConfig]:
        with _container(tmp_path) as container:
            before = container.config_manager.load()
            _app, client, csrf = await _client(container)
            async with client:
                page = await client.get("/settings")
                response = await client.post(
                    "/settings",
                    headers=_unsafe(csrf),
                    data={
                        "project_roots": str(container.config.project_roots[0]),
                        "enabled_sources": ["codex", "chatgpt"],
                        "inactive_days": "21",
                        "max_recall_tokens": "801",
                        "daily_reconcile_time": "03:30",
                    },
                )
            return response, page, before, container.config_manager.load()

    response, page, before, after = asyncio.run(scenario())

    assert response.status_code == 409
    assert after == before
    assert 'name="max_recall_tokens" min="128" max="800"' in page.text


def test_sources_show_persisted_desired_state_and_reject_disabling_last_source(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, AppConfig, AppConfig]:
        with _container(tmp_path) as container:
            _app, client, csrf = await _client(container)
            async with client:
                disabled = await client.post("/sources/codex/disable", headers=_unsafe(csrf))
                desired_page = await client.get(disabled.headers["location"])
                denied_last = await client.post("/sources/chatgpt/disable", headers=_unsafe(csrf))
            return (
                desired_page,
                denied_last,
                container.config_manager.load(),
                container.config,
            )

    desired_page, denied_last, saved, runtime = asyncio.run(scenario())
    assert desired_page.status_code == 200
    assert "Saved desired source state" in desired_page.text
    assert "Restart required" in desired_page.text
    assert 'data-source="codex"' in desired_page.text
    assert "Desired: Disabled" in desired_page.text
    assert "Runtime: Enabled" in desired_page.text
    assert denied_last.status_code == 409
    assert saved.enabled_sources == (SourceAgent.CHATGPT,)
    assert runtime.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)


def test_settings_add_and_remove_project_roots_with_bounded_multiline_input(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, AppConfig, AppConfig]:
        with _container(tmp_path) as container:
            original_root = container.config.project_roots[0]
            added_root = tmp_path / "added-projects"
            added_root.mkdir()
            common = {
                "enabled_sources": ["codex", "chatgpt"],
                "inactive_days": "21",
                "max_recall_tokens": "800",
                "daily_reconcile_time": "03:30",
            }
            _app, client, csrf = await _client(container)
            async with client:
                added = await client.post(
                    "/settings",
                    headers=_unsafe(csrf),
                    data={
                        **common,
                        "project_roots": f"{original_root}\n{added_root}",
                    },
                )
                after_add = container.config_manager.load()
                removed = await client.post(
                    "/settings",
                    headers=_unsafe(csrf),
                    data={**common, "project_roots": str(added_root)},
                )
            return added, removed, after_add, container.config_manager.load()

    added, removed, after_add, after_remove = asyncio.run(scenario())
    assert added.status_code == removed.status_code == 303
    assert len(after_add.project_roots) == 2
    assert after_remove.project_roots == (tmp_path / "added-projects",)


def test_settings_reject_relative_excessive_or_oversized_project_roots(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[int]:
        with _container(tmp_path) as container:
            common = {
                "enabled_sources": ["codex", "chatgpt"],
                "inactive_days": "21",
                "max_recall_tokens": "800",
                "daily_reconcile_time": "03:30",
            }
            valid_root = str(container.config.project_roots[0])
            invalid_roots = (
                "relative/path",
                "\n".join([valid_root] * 33),
                "/" + ("x" * 5000),
            )
            _app, client, csrf = await _client(container)
            async with client:
                return [
                    (
                        await client.post(
                            "/settings",
                            headers=_unsafe(csrf),
                            data={**common, "project_roots": roots},
                        )
                    ).status_code
                    for roots in invalid_roots
                ]

    assert asyncio.run(scenario()) == [409, 409, 409]


@pytest.mark.parametrize("unsafe_root", (Path("/"), Path.home().parent, Path.home()))
def test_settings_reject_roots_that_include_the_entire_home(
    tmp_path: Path,
    unsafe_root: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, AppConfig, AppConfig]:
        with _container(tmp_path) as container:
            before = container.config_manager.load()
            _app, client, csrf = await _client(container)
            async with client:
                response = await client.post(
                    "/settings",
                    headers=_unsafe(csrf),
                    data={
                        "project_roots": str(unsafe_root),
                        "enabled_sources": ["codex", "chatgpt"],
                        "inactive_days": "21",
                        "max_recall_tokens": "800",
                        "daily_reconcile_time": "03:30",
                    },
                )
            return response, before, container.config_manager.load()

    response, before, after = asyncio.run(scenario())

    assert response.status_code == 409
    assert after == before


def test_proposals_redact_all_display_metadata_and_hide_unsafe_model_scope(
    tmp_path: Path,
) -> None:
    secret = "TOPSECRETTOKEN123456789"
    bearer = f"Bearer {secret}"

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            memory_id = _insert_memory(
                container,
                project.project_id,
                model_id="model-a",
                content="safe behavior",
            )
            promotion = container.promotions.request_scoped(
                project.project_id,
                Namespace(source_agent=SourceAgent.CODEX, model_id="model-a"),
                memory_id,
                "safe rule",
            )
            with container.database.transaction() as connection:
                connection.execute(
                    """
                    update memory_promotions
                    set proposed_rule = ?, requested_at = ?
                    where promotion_id = ?
                    """,
                    (bearer, bearer, str(promotion.promotion_id)),
                )
                connection.execute(
                    """
                    update behavior_memories
                    set source_agent = ?, model_id = ?
                    where memory_id = ?
                    """,
                    (bearer, bearer, str(memory_id)),
                )
                connection.execute(
                    """
                    insert into improvement_proposals(
                        proposal_id, signature, title, description, patch, risk,
                        verification_argv_json, approval_status, created_at
                    ) values (?, ?, ?, '', null, 'low', '[]', 'draft', ?)
                    """,
                    (str(uuid4()), "secret-metadata", bearer, bearer),
                )
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/proposals")

    response = asyncio.run(scenario())
    assert response.status_code == 200
    assert secret not in response.text
    assert "[REDACTED:bearer_token]" in response.text
    assert "Unsafe promotion metadata; approval unavailable" in response.text


def test_proposals_disable_and_reject_corrupted_rules_atomically(
    tmp_path: Path,
) -> None:
    secret = "TOPSECRETTOKEN123456789"
    bearer = f"Bearer {secret}"

    async def scenario() -> tuple[httpx.Response, httpx.Response, tuple[object, ...], int]:
        with _container(tmp_path) as container:
            project = _register(container, tmp_path / "project-a")
            memory_id = _insert_memory(
                container,
                project.project_id,
                model_id="model-a",
                content="safe behavior",
            )
            promotion = container.promotions.request_scoped(
                project.project_id,
                Namespace(source_agent=SourceAgent.CODEX, model_id="model-a"),
                memory_id,
                "safe rule",
            )
            with container.database.transaction() as connection:
                connection.execute(
                    "update memory_promotions set proposed_rule = ? where promotion_id = ?",
                    (bearer, str(promotion.promotion_id)),
                )
            _app, client, csrf = await _client(container)
            async with client:
                page = await client.get("/proposals")
                approve = await client.post(
                    f"/promotions/{promotion.promotion_id}/approve",
                    headers=_unsafe(csrf),
                    data={
                        "project_id": str(project.project_id),
                        "source_agent": "codex",
                        "model_id": "model-a",
                        "confirmation": "APPROVE",
                    },
                )
            with container.database.connect(readonly=True) as connection:
                state = tuple(
                    connection.execute(
                        "select status, approval_actor, approved_at "
                        "from memory_promotions where promotion_id = ?",
                        (str(promotion.promotion_id),),
                    ).fetchone()
                )
                fact_count = connection.execute(
                    "select count(*) from project_facts where project_id = ?",
                    (str(project.project_id),),
                ).fetchone()[0]
            return page, approve, state, fact_count

    page, approve, state, fact_count = asyncio.run(scenario())
    assert page.status_code == 200
    assert secret not in page.text
    assert "[REDACTED:bearer_token]" in page.text
    assert "/approve" not in page.text
    assert "Unsafe promotion metadata; approval unavailable" in page.text
    assert approve.status_code == 409
    assert secret not in approve.text
    assert state == ("pending", None, None)
    assert fact_count == 0


def test_all_control_pages_exist_and_proposals_hide_patch_content(
    tmp_path: Path,
) -> None:
    async def scenario() -> dict[str, httpx.Response]:
        with _container(tmp_path) as container:
            container.proposal_service.create(
                ProposalDraft(
                    signature="safe-signature",
                    title="Safe proposal title",
                    description="SAFE_METADATA_DESCRIPTION",
                    patch=(
                        "diff --git a/README.md b/README.md\n"
                        "--- a/README.md\n"
                        "+++ b/README.md\n"
                        "@@ -1 +1 @@\n"
                        "-seed\n"
                        "+NEVER_RENDER_PATCH_CONTENT\n"
                    ),
                    risk="low",
                    verification_argv=("/usr/bin/true",),
                    origin="local_cli",
                )
            )
            _app, client, _csrf = await _client(container)
            async with client:
                return {
                    path: await client.get(path)
                    for path in (
                        "/",
                        "/sources",
                        "/projects",
                        "/memories",
                        "/imports",
                        "/proposals",
                        "/settings",
                    )
                }

    responses = asyncio.run(scenario())
    assert all(response.status_code == 200 for response in responses.values())
    proposals = responses["/proposals"].text
    assert "Safe proposal title" in proposals
    assert "SAFE_METADATA_DESCRIPTION" in proposals
    assert "NEVER_RENDER_PATCH_CONTENT" not in proposals


def test_startup_due_reconcile_is_background_and_bounded(tmp_path: Path) -> None:
    class FakeReconcile:
        def __init__(self) -> None:
            self.called = False
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def should_run(self) -> bool:
            return True

        def run(self, force: bool = False):
            assert force is False
            self.called = True
            self.started.set()
            try:
                self.release.wait(timeout=1)
            finally:
                self.finished.set()

    async def wait_until(predicate) -> None:
        async def poll() -> None:
            while not predicate():
                await asyncio.sleep(0)

        await asyncio.wait_for(poll(), timeout=1)

    async def scenario() -> tuple[bool, bool, str, str]:
        with _container(tmp_path) as container:
            fake = FakeReconcile()
            container.reconcile = fake  # type: ignore[assignment]
            app = create_app(container)
            async with app.router.lifespan_context(app):
                await wait_until(fake.started.is_set)
                running_status = app.state.startup_reconcile_status
                worker_is_blocked = not fake.finished.is_set()
                fake.release.set()
                await wait_until(
                    lambda: app.state.startup_reconcile_status in {"complete", "degraded"}
                )
                terminal_status = app.state.startup_reconcile_status
            return fake.called, worker_is_blocked, running_status, terminal_status

    called, worker_is_blocked, running_status, terminal_status = asyncio.run(scenario())
    assert called is True
    assert worker_is_blocked is True
    assert running_status == "running"
    assert terminal_status in {"complete", "degraded"}


def test_startup_shutdown_waits_for_worker_before_container_can_close(
    tmp_path: Path,
) -> None:
    class BlockingReconcile:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def should_run(self) -> bool:
            return True

        def run(self, force: bool = False):
            assert force is False
            self.started.set()
            try:
                self.release.wait(timeout=1)
            finally:
                self.finished.set()

    async def scenario() -> tuple[float, bool, bool, str]:
        with _container(tmp_path) as container:
            blocking = BlockingReconcile()
            container.reconcile = blocking  # type: ignore[assignment]
            app = create_app(container)
            async with app.router.lifespan_context(app):
                while not blocking.started.is_set():
                    await asyncio.sleep(0.005)

                async def release_after_observing_worker() -> bool:
                    await asyncio.sleep(0.05)
                    was_running = not blocking.finished.is_set()
                    blocking.release.set()
                    return was_running

                release_task = asyncio.create_task(release_after_observing_worker())
                started = time.monotonic()
            elapsed = time.monotonic() - started
            worker_was_running = await release_task
            worker_finished = blocking.finished.is_set()
            status = app.state.startup_reconcile_status
            return elapsed, worker_was_running, worker_finished, status

    elapsed, worker_was_running, worker_finished, status = asyncio.run(scenario())
    assert 0.04 <= elapsed < 0.2
    assert worker_was_running is True
    assert worker_finished is True
    assert status in {"complete", "degraded"}
