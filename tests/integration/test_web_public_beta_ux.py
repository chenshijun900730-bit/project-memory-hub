from __future__ import annotations

import asyncio
import hashlib
import html
import re
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import Body, HTTPException

from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import ServiceContainer, build_container
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    MemoryKind,
    Namespace,
    ProjectCandidate,
    ProjectFactInput,
    SourceAgent,
)
from project_memory_hub.security.web import LocalAccessToken, WebRequestLimits
from project_memory_hub.web.app import create_app


@pytest.fixture
def container(tmp_path: Path) -> Iterator[ServiceContainer]:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir(mode=0o700)
    config_path = runtime_root / "config.toml"
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
    with build_container(config_path, probe_home=probe_home) as selected:
        yield selected


def _assert_security_headers(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


async def _authenticated_client(
    container: ServiceContainer,
) -> tuple[object, httpx.AsyncClient]:
    app = create_app(container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    token = LocalAccessToken.load_or_create(container.paths)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as bootstrap:
        boot = await bootstrap.get(f"/?token={token}", follow_redirects=False)
    return app, httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        cookies=boot.cookies,
    )


def _register_project(container: ServiceContainer, path: Path) -> UUID:
    path.mkdir()
    project = container.projects.register(
        ProjectCandidate(canonical_path=path, display_name="Presentation Project")
    )
    return project.project_id


def _insert_behavior_memory(
    container: ServiceContainer,
    project_id: UUID,
    *,
    model_id: str,
) -> None:
    source_reference_id = uuid4()
    observed_at = datetime.now(timezone.utc)
    digest = hashlib.sha256(model_id.encode()).hexdigest()
    content = f"Private decision for {model_id}"
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
                f"presentation-{model_id}",
                digest,
                observed_at.isoformat(),
                observed_at.isoformat(),
            ),
        )
    container.memories.insert(
        BehaviorMemoryInput(
            project_id=project_id,
            namespace=Namespace(source_agent=SourceAgent.CODEX, model_id=model_id),
            task_fingerprint=digest,
            memory_kind=MemoryKind.DECISION,
            normalized_content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            source_reference_id=source_reference_id,
            created_at=observed_at,
            confidence=0.9,
        )
    )


def _runtime_snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(path.relative_to(root)): path.read_bytes() if path.is_file() else None
        for path in root.rglob("*")
    }


def _assert_structured_empty_states(response: httpx.Response, *, expected: int) -> None:
    blocks = re.findall(
        r'<section class="empty empty-state"[^>]*>(.*?)</section>',
        response.text,
        flags=re.DOTALL,
    )
    assert len(blocks) == expected
    assert 'class="empty"' not in response.text
    for block in blocks:
        assert block.count("data-empty-reason") == 1
        assert block.count("data-empty-next-step") == 1
        assert block.count("data-empty-success") == 1
        assert block.count("<code>") == 1


def test_error_statuses_use_fixed_bilingual_html_without_request_data(
    container: ServiceContainer,
) -> None:
    app = create_app(container)

    @app.get("/__public_beta_error__/{status_code}")
    async def fail(status_code: int, private_query: str = "") -> None:
        if status_code == 500:
            raise RuntimeError("NEVER_ECHO_EXCEPTION_SECRET")
        raise HTTPException(
            status_code=status_code,
            detail=f"NEVER_ECHO_DETAIL_{status_code}_{private_query}",
        )

    @app.post("/__public_beta_invalid_body__")
    async def invalid_body(payload: int = Body()) -> dict[str, int]:
        return {"payload": payload}

    expected_titles = {
        400: ("Invalid request", "请求无效"),
        401: ("Authentication required", "需要身份验证"),
        403: ("Request denied", "请求被拒绝"),
        404: ("Page not found", "页面未找到"),
        409: ("Request conflict", "请求冲突"),
        413: ("Request too large", "请求体过大"),
        422: ("Invalid request", "请求无效"),
        500: ("Operation failed", "操作失败"),
    }
    private_markers = {
        "NEVER_ECHO_QUERY_SECRET",
        "NEVER_ECHO_BODY_SECRET",
        "NEVER_ECHO_PATH_SECRET",
        "NEVER_ECHO_EXCEPTION_SECRET",
        "NEVER_ECHO_DETAIL",
    }

    async def scenario() -> dict[int, httpx.Response]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        token = LocalAccessToken.load_or_create(container.paths)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            boot = await client.get(f"/?token={token}", follow_redirects=False)
            csrf = boot.headers["x-project-memory-hub-csrf"]
            responses = {
                status: await client.get(
                    f"/__public_beta_error__/{status}",
                    params={"private_query": "NEVER_ECHO_QUERY_SECRET"},
                )
                for status in (400, 401, 403, 409, 413, 500)
            }
            responses[404] = await client.get("/NEVER_ECHO_PATH_SECRET")
            responses[422] = await client.post(
                "/__public_beta_invalid_body__",
                headers={
                    "content-type": "application/json",
                    "origin": "http://127.0.0.1",
                    "x-csrf-token": csrf,
                },
                content='"NEVER_ECHO_BODY_SECRET"',
            )
            return responses

    responses = asyncio.run(scenario())

    assert set(responses) == set(expected_titles)
    for status_code, response in responses.items():
        english, chinese = expected_titles[status_code]
        assert response.status_code == status_code
        assert response.headers["content-type"].startswith("text/html")
        assert english in response.text
        assert chinese in response.text
        assert 'href="/"' in response.text
        assert "Return to Overview" in response.text
        assert "返回概览" in response.text
        assert "<script" not in response.text.casefold()
        assert '<link rel="stylesheet"' not in response.text.casefold()
        assert "set-cookie" not in response.headers
        assert all(marker not in response.text for marker in private_markers)
        _assert_security_headers(response)


def test_security_boundary_and_fastapi_handlers_share_the_same_error_shell(
    container: ServiceContainer,
) -> None:
    app = create_app(
        container,
        request_limits=WebRequestLimits(
            max_total_bytes=16,
            max_chunk_bytes=16,
            max_chunks=4,
            max_files=1,
            max_fields=4,
        ),
    )

    @app.get("/__handler_error__/{status_code}")
    async def handler_error(status_code: int) -> None:
        raise HTTPException(status_code=status_code, detail="NEVER_ECHO_HANDLER_DETAIL")

    @app.post("/__accepted_post__")
    async def accepted_post() -> None:
        return None

    async def scenario() -> tuple[dict[int, httpx.Response], dict[int, httpx.Response]]:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        token = LocalAccessToken.load_or_create(container.paths)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1",
        ) as client:
            bad_host = await client.get("/", headers={"host": "attacker.example"})
            bad_token = await client.get("/?token=NEVER_ECHO_TOKEN_SECRET")
            boot = await client.get(f"/?token={token}", follow_redirects=False)
            csrf = boot.headers["x-project-memory-hub-csrf"]
            foreign_origin = await client.post(
                "/__accepted_post__",
                headers={
                    "origin": "https://attacker.example",
                    "x-csrf-token": csrf,
                },
            )
            oversized = await client.post(
                "/__accepted_post__",
                headers={
                    "origin": "http://127.0.0.1",
                    "x-csrf-token": csrf,
                },
                content=b"x" * 17,
            )
            boundary = {
                400: bad_host,
                401: bad_token,
                403: foreign_origin,
                413: oversized,
            }
            handler = {
                status: await client.get(f"/__handler_error__/{status}") for status in boundary
            }
            return boundary, handler

    boundary, handler = asyncio.run(scenario())

    for status_code in boundary:
        assert boundary[status_code].status_code == status_code
        assert handler[status_code].status_code == status_code
        assert boundary[status_code].content == handler[status_code].content
        assert boundary[status_code].headers["content-type"].startswith("text/html")
        assert "NEVER_ECHO" not in boundary[status_code].text
        _assert_security_headers(boundary[status_code])


def test_each_console_page_marks_exactly_one_current_navigation_item(
    container: ServiceContainer,
) -> None:
    expected = {
        "/": "/",
        "/sources": "/sources",
        "/projects": "/projects",
        "/memories": "/memories",
        "/imports": "/imports",
        "/proposals": "/proposals",
        "/settings": "/settings",
    }

    async def scenario() -> dict[str, httpx.Response]:
        _app, client = await _authenticated_client(container)
        async with client:
            return {path: await client.get(path) for path in expected}

    responses = asyncio.run(scenario())

    for path, response in responses.items():
        assert response.status_code == 200, path
        assert response.text.count('aria-current="page"') == 1
        match = re.search(r'<a href="([^"]+)"[^>]* aria-current="page"', response.text)
        assert match is not None
        assert match.group(1) == expected[path]


def test_overview_next_step_uses_existing_state_and_never_mutates_runtime(
    container: ServiceContainer,
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response, httpx.Response]:
        app, client = await _authenticated_client(container)
        before = _runtime_snapshot(container.paths.root)
        async with client:
            discover = await client.get("/")
            assert _runtime_snapshot(container.paths.root) == before

            project_id = _register_project(container, tmp_path / "presentation-project")
            scan = await client.get("/")

            with container.database.transaction() as connection:
                connection.execute(
                    "update projects set permission_status = 'blocked' where project_id = ?",
                    (str(project_id),),
                )
            doctor = await client.get("/")

            with container.database.transaction() as connection:
                connection.execute(
                    "update projects set permission_status = 'ok' where project_id = ?",
                    (str(project_id),),
                )
            container.facts.observe(
                project_id,
                ProjectFactInput(
                    category="language",
                    normalized_content="Python",
                    evidence_type="manifest",
                    evidence_reference="pyproject.toml",
                    observed_at=datetime.now(timezone.utc),
                    confidence=1.0,
                ),
            )
            app.state.startup_reconcile_status = "complete"  # type: ignore[attr-defined]
            reconcile = await client.get("/")
        return discover, scan, doctor, reconcile

    discover, scan, doctor, reconcile = asyncio.run(scenario())

    expected = (
        (discover, "discover", "memory-hub discover --dry-run --format json"),
        (scan, "scan", 'memory-hub scan --cwd "$PWD" --dry-run --format json'),
        (doctor, "doctor", "memory-hub doctor --format json"),
        (reconcile, "reconcile", "memory-hub reconcile --if-due --format json"),
    )
    for response, kind, command in expected:
        assert response.status_code == 200
        assert f'data-next-safe-step="{kind}"' in response.text
        assert command in html.unescape(response.text)
        assert "Success condition:" in response.text


def test_memories_guides_exact_model_without_enumerating_stored_namespaces(
    container: ServiceContainer,
    tmp_path: Path,
) -> None:
    project_id = _register_project(container, tmp_path / "isolated-project")
    _insert_behavior_memory(container, project_id, model_id="stored-model-alpha")
    _insert_behavior_memory(container, project_id, model_id="stored-model-beta")

    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        _app, client = await _authenticated_client(container)
        async with client:
            unfiltered = await client.get("/memories")
            selected = await client.get(
                "/memories",
                params={
                    "project_id": str(project_id),
                    "source_agent": "codex",
                    "model_id": "stored-model-alpha",
                },
            )
        return unfiltered, selected

    unfiltered, selected = asyncio.run(scenario())

    assert 'memory-hub codex-context --cwd "$PWD" --format json' in html.unescape(unfiltered.text)
    assert "never guesses or lists other model namespaces" in unfiltered.text
    assert "stored-model-alpha" not in unfiltered.text
    assert "stored-model-beta" not in unfiltered.text
    assert "stored-model-alpha" in selected.text
    assert "stored-model-beta" not in selected.text


def test_memories_and_proposals_render_only_structured_empty_states(
    container: ServiceContainer,
    tmp_path: Path,
) -> None:
    project_id = _register_project(container, tmp_path / "empty-project")

    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        _app, client = await _authenticated_client(container)
        async with client:
            memories = await client.get("/memories")
            selected_memories = await client.get(
                "/memories", params={"project_id": str(project_id)}
            )
            proposals = await client.get("/proposals")
        return memories, selected_memories, proposals

    memories, selected_memories, proposals = asyncio.run(scenario())

    _assert_structured_empty_states(memories, expected=1)
    _assert_structured_empty_states(selected_memories, expected=2)
    _assert_structured_empty_states(proposals, expected=2)
    assert "memory-hub discover --dry-run --format json" not in html.unescape(memories.text)
    assert 'memory-hub codex-context --cwd "$PWD" --format json' in html.unescape(memories.text)
    assert "registered project directory" in selected_memories.text


def test_memories_uses_discovery_step_only_when_no_project_is_registered(
    container: ServiceContainer,
) -> None:
    async def scenario() -> httpx.Response:
        _app, client = await _authenticated_client(container)
        async with client:
            return await client.get("/memories")

    response = asyncio.run(scenario())
    blocks = re.findall(
        r'<section class="empty empty-state"[^>]*>(.*?)</section>',
        response.text,
        flags=re.DOTALL,
    )

    assert len(blocks) == 1
    assert "memory-hub discover --dry-run --format json" in html.unescape(blocks[0])
