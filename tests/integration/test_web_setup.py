import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx

import project_memory_hub.services.setup as setup_service_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import ProjectCandidate, SourceAgent
from project_memory_hub.web.app import create_app


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
            setup_completed=False,
        )
    )
    return build_container(config_path), config_path


async def _bootstrap(app) -> tuple[str, str]:
    token = app.state.container.paths.access_token.read_text(encoding="ascii")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as client:
        response = await client.get(f"/?token={token}", follow_redirects=False)
    return (
        response.headers["set-cookie"].split(";", 1)[0],
        response.headers["x-project-memory-hub-csrf"],
    )


def test_setup_page_is_authenticated_read_only_and_has_only_registered_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    container, config_path = _container(tmp_path)
    before = config_path.read_bytes()
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, _csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.get("/setup", headers={"cookie": cookie})

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert 'action="/setup/configure"' in response.text
    assert 'formaction="/setup/complete"' in response.text
    assert 'name="enabled_sources" value="codex"' in response.text
    assert 'name="enabled_sources" value="chatgpt"' in response.text
    assert 'name="enabled_sources" value="trae"' not in response.text
    assert "csrf_token" in response.text
    assert 'name="expected_revision"' in response.text
    assert config_path.read_bytes() == before


def test_setup_configure_saves_explicit_local_settings_without_completing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    container, config_path = _container(tmp_path)
    selected_root = tmp_path / "selected-projects"
    selected_root.mkdir()
    revision = ConfigManager(config_path).load_with_revision()[1].digest
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.post(
                    "/setup/configure",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data={
                        "csrf_token": csrf,
                        "expected_revision": revision,
                        "project_roots": str(selected_root),
                        "enabled_sources": ["codex", "chatgpt"],
                        "inactive_days": "30",
                        "max_recall_tokens": "700",
                        "daily_reconcile_time": "04:15",
                    },
                    follow_redirects=False,
                )

    response = asyncio.run(scenario())

    assert response.status_code == 303
    assert response.headers["location"] == "/setup?saved=1"
    persisted = ConfigManager(config_path).load()
    assert persisted.project_roots == (selected_root,)
    assert persisted.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)
    assert persisted.inactive_days == 30
    assert persisted.max_recall_tokens == 700
    assert persisted.daily_reconcile_time == "04:15"
    assert persisted.setup_completed is False


def test_setup_complete_is_idempotent_and_redirects_to_overview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    container, config_path = _container(tmp_path)
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    async def scenario() -> tuple[httpx.Response, httpx.Response, tuple[int, int]]:
        with container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            manager = ConfigManager(config_path)
            config = manager.load()
            data = {
                "csrf_token": csrf,
                "expected_revision": manager.load_with_revision()[1].digest,
                "project_roots": str(config.project_roots[0]),
                "enabled_sources": [source.value for source in config.enabled_sources],
                "inactive_days": str(config.inactive_days),
                "max_recall_tokens": str(config.max_recall_tokens),
                "daily_reconcile_time": config.daily_reconcile_time,
            }
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                first = await client.post(
                    "/setup/complete",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data=data,
                    follow_redirects=False,
                )
                before_repeat = config_path.stat()
                repeated = await client.post(
                    "/setup/complete",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data=data,
                    follow_redirects=False,
                )
                after_repeat = config_path.stat()
                return (
                    first,
                    repeated,
                    (
                        before_repeat.st_ino == after_repeat.st_ino,
                        before_repeat.st_mtime_ns == after_repeat.st_mtime_ns,
                    ),
                )

    first, repeated, unchanged = asyncio.run(scenario())

    assert first.status_code == repeated.status_code == 303
    assert first.headers["location"] == repeated.headers["location"] == "/?setup-complete=1"
    assert unchanged == (True, True)
    assert ConfigManager(config_path).load().setup_completed is True


def test_setup_complete_submits_and_persists_the_current_form_atomically(
    tmp_path: Path,
    monkeypatch,
) -> None:
    container, config_path = _container(tmp_path)
    selected_root = tmp_path / "selected-projects"
    selected_root.mkdir()
    revision = ConfigManager(config_path).load_with_revision()[1].digest
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.post(
                    "/setup/complete",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data={
                        "csrf_token": csrf,
                        "expected_revision": revision,
                        "project_roots": str(selected_root),
                        "enabled_sources": ["codex", "chatgpt"],
                        "inactive_days": "30",
                        "max_recall_tokens": "700",
                        "daily_reconcile_time": "04:15",
                    },
                    follow_redirects=False,
                )

    response = asyncio.run(scenario())

    assert response.status_code == 303
    assert response.headers["location"] == "/?setup-complete=1"
    persisted = ConfigManager(config_path).load()
    assert persisted.project_roots == (selected_root,)
    assert persisted.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)
    assert persisted.inactive_days == 30
    assert persisted.max_recall_tokens == 700
    assert persisted.daily_reconcile_time == "04:15"
    assert persisted.setup_completed is True


def test_setup_complete_rejects_a_stale_form_without_overwriting_newer_settings(
    tmp_path: Path,
) -> None:
    container, config_path = _container(tmp_path)
    manager = ConfigManager(config_path)
    original, revision = manager.load_with_revision()
    manager.save(replace(original, inactive_days=45))

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.post(
                    "/setup/complete",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data={
                        "csrf_token": csrf,
                        "expected_revision": revision.digest,
                        "project_roots": str(original.project_roots[0]),
                        "enabled_sources": [source.value for source in original.enabled_sources],
                        "inactive_days": str(original.inactive_days),
                        "max_recall_tokens": str(original.max_recall_tokens),
                        "daily_reconcile_time": original.daily_reconcile_time,
                    },
                    follow_redirects=False,
                )

    response = asyncio.run(scenario())

    assert response.status_code == 409
    persisted = manager.load()
    assert persisted.inactive_days == 45
    assert persisted.setup_completed is False


def test_setup_complete_rejects_extra_fields_before_writing(tmp_path: Path) -> None:
    container, config_path = _container(tmp_path)
    before = config_path.read_bytes()
    revision = ConfigManager(config_path).load_with_revision()[1].digest

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.post(
                    "/setup/complete",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data={
                        "csrf_token": csrf,
                        "expected_revision": revision,
                        "project_roots": str(tmp_path / "projects"),
                        "enabled_sources": ["codex", "chatgpt"],
                        "inactive_days": "21",
                        "max_recall_tokens": "800",
                        "daily_reconcile_time": "03:30",
                        "next": "https://attacker.invalid",
                    },
                )

    response = asyncio.run(scenario())

    assert response.status_code == 400
    assert config_path.read_bytes() == before


def test_setup_first_memory_uses_the_existing_safe_scan_preview(
    tmp_path: Path,
    monkeypatch,
) -> None:
    container, config_path = _container(tmp_path)
    manager = ConfigManager(config_path)
    manager.save(replace(manager.load(), setup_completed=True))
    registered = tmp_path / "registered"
    registered.mkdir()
    container.projects.register(
        ProjectCandidate(canonical_path=registered, display_name="Registered")
    )
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, _csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.get("/setup", headers={"cookie": cookie})

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert 'data-setup-next-step="first_memory"' in response.text
    assert 'memory-hub scan --cwd "$PWD" --dry-run --format json' in response.text
    assert "memory-hub reconcile --if-due --format json" not in response.text


def test_setup_rejects_unauthenticated_csrf_origin_and_extra_fields_before_writing(
    tmp_path: Path,
) -> None:
    container, config_path = _container(tmp_path)
    before = config_path.read_bytes()
    revision = ConfigManager(config_path).load_with_revision()[1].digest
    base_data = {
        "expected_revision": revision,
        "project_roots": str(tmp_path / "projects"),
        "enabled_sources": ["codex", "chatgpt"],
        "inactive_days": "21",
        "max_recall_tokens": "800",
        "daily_reconcile_time": "03:30",
    }

    async def scenario() -> tuple[int, int, int, int]:
        with container:
            app = create_app(container)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                unauthenticated = await client.post(
                    "/setup/configure",
                    headers={"origin": "http://127.0.0.1"},
                    data=base_data,
                )
                cookie, csrf = await _bootstrap(app)
                missing_csrf = await client.post(
                    "/setup/configure",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data=base_data,
                )
                hostile_origin = await client.post(
                    "/setup/configure",
                    headers={"cookie": cookie, "origin": "http://attacker.invalid"},
                    data={**base_data, "csrf_token": csrf},
                )
                extra_field = await client.post(
                    "/setup/configure",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data={**base_data, "csrf_token": csrf, "next": "https://attacker.invalid"},
                )
                return (
                    unauthenticated.status_code,
                    missing_csrf.status_code,
                    hostile_origin.status_code,
                    extra_field.status_code,
                )

    statuses = asyncio.run(scenario())

    assert statuses == (401, 403, 403, 400)
    assert config_path.read_bytes() == before


def test_setup_rejects_a_route_level_oversized_body_before_writing(
    tmp_path: Path,
) -> None:
    container, config_path = _container(tmp_path)
    before = config_path.read_bytes()
    revision = ConfigManager(config_path).load_with_revision()[1].digest

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.post(
                    "/setup/configure",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    data={
                        "csrf_token": csrf,
                        "expected_revision": revision,
                        "project_roots": "x" * (256 * 1024),
                        "enabled_sources": ["codex", "chatgpt"],
                        "inactive_days": "21",
                        "max_recall_tokens": "800",
                        "daily_reconcile_time": "03:30",
                    },
                )

    response = asyncio.run(scenario())

    assert response.status_code == 413
    assert config_path.read_bytes() == before


def test_setup_rejects_multipart_even_when_all_fields_are_valid(
    tmp_path: Path,
) -> None:
    container, config_path = _container(tmp_path)
    before = config_path.read_bytes()
    revision = ConfigManager(config_path).load_with_revision()[1].digest

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            fields = [
                ("csrf_token", (None, csrf)),
                ("expected_revision", (None, revision)),
                ("project_roots", (None, str(tmp_path / "projects"))),
                ("enabled_sources", (None, "codex")),
                ("enabled_sources", (None, "chatgpt")),
                ("inactive_days", (None, "21")),
                ("max_recall_tokens", (None, "800")),
                ("daily_reconcile_time", (None, "03:30")),
            ]
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.post(
                    "/setup/configure",
                    headers={"cookie": cookie, "origin": "http://127.0.0.1"},
                    files=fields,
                )

    response = asyncio.run(scenario())

    assert response.status_code == 400
    assert config_path.read_bytes() == before


def test_incomplete_overview_links_to_setup_without_forcing_a_redirect(
    tmp_path: Path,
    monkeypatch,
) -> None:
    container, _config_path = _container(tmp_path)
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, _csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.get("/", headers={"cookie": cookie})

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert response.url.path == "/"
    assert 'data-setup-incomplete="true"' in response.text
    assert 'href="/setup"' in response.text


def test_setup_automation_check_is_read_only_and_requests_host_authorization(
    tmp_path: Path,
    monkeypatch,
) -> None:
    container, _config_path = _container(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    launcher = tmp_path / "memory-hub"
    launcher.write_text("#!/bin/sh\n", encoding="utf-8")
    launcher.chmod(0o700)
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(
            lambda: SimpleNamespace(
                repository_root=repository,
                launcher=launcher,
            )
        ),
    )
    monkeypatch.setattr(setup_service_module.Path, "home", staticmethod(lambda: home))

    async def scenario() -> httpx.Response:
        with container:
            app = create_app(container)
            cookie, _csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                return await client.get("/setup", headers={"cookie": cookie})

    response = asyncio.run(scenario())

    assert response.status_code == 200
    assert "authorization_required" in response.text
    assert not (home / ".codex" / "automations").exists()
