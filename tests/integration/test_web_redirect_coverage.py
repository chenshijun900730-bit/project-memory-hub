from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException, Request
from starlette.datastructures import FormData, UploadFile

import project_memory_hub.web.routes as routes_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import ServiceContainer, build_container
from project_memory_hub.domain import Namespace, ProjectCandidate, SourceAgent
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.services.control import (
    ControlInputError,
    ControlPanelService,
    UnavailableSourceError,
)
from project_memory_hub.web.app import create_app


def _container(tmp_path: Path) -> ServiceContainer:
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
    return build_container(config_path, probe_home=probe_home)


@asynccontextmanager
async def _client(
    container: ServiceContainer,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    app = create_app(container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    token = LocalAccessToken.load_or_create(container.paths)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as bootstrap_client:
        boot = await bootstrap_client.get(f"/?token={token}", follow_redirects=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        cookies=boot.cookies,
    ) as client:
        yield client, boot.headers["x-project-memory-hub-csrf"]


def _unsafe(csrf: str) -> dict[str, str]:
    return {"origin": "http://127.0.0.1", "x-csrf-token": csrf}


def _request(query: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/imports",
            "query_string": urlencode(query).encode(),
            "headers": [],
        }
    )


def _assert_http_status(call: Callable[[], Any], expected: int) -> None:
    with pytest.raises(HTTPException) as raised:
        call()
    assert raised.value.status_code == expected


def test_memory_redirect_is_see_other_and_round_trips_exact_namespace() -> None:
    project_id = UUID("12345678-1234-5678-1234-567812345678")
    namespace = Namespace(
        source_agent=SourceAgent.CODEX,
        model_id="model /?&=中文",
    )

    response = routes_module._memory_redirect(project_id, namespace)
    location = urlsplit(response.headers["location"])

    assert response.status_code == 303
    assert location.path == "/memories"
    assert parse_qs(location.query) == {
        "project_id": [str(project_id)],
        "source_agent": ["codex"],
        "model_id": [namespace.model_id],
    }


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        (None, None),
        ("", None),
        ("１２", None),
        ("-1", None),
        ("1.0", None),
        ("00000000", None),
        ("1000001", None),
        ("0", 0),
        ("0000000", 0),
        ("1000000", 1_000_000),
    ),
)
def test_import_count_accepts_only_bounded_ascii_decimal(
    value: str | None,
    expected: int | None,
) -> None:
    assert routes_module._bounded_query_count(value) == expected


@pytest.mark.parametrize("status", ("checked", "imported"))
def test_import_notice_accepts_both_results_at_the_product_limit(status: str) -> None:
    notice = routes_module._import_notice(
        _request(
            {
                "status": status,
                "matches": "1000000",
                "confirmations": "0000000",
            }
        )
    )

    assert notice == {
        "status": status,
        "matches": 1_000_000,
        "confirmations": 0,
    }


@pytest.mark.parametrize(
    "query",
    (
        {"status": "unknown", "matches": "1", "confirmations": "2"},
        {"status": "checked", "matches": "1"},
        {"status": "checked", "matches": "1000001", "confirmations": "2"},
        {"status": "imported", "matches": "1", "confirmations": "non-numeric"},
    ),
)
def test_import_notice_discards_incomplete_or_untrusted_query_state(
    query: dict[str, str],
) -> None:
    assert routes_module._import_notice(_request(query)) is None


@pytest.mark.parametrize(
    "values",
    (
        (),
        ("first", "second"),
        (" \t\n",),
    ),
)
def test_single_rejects_missing_duplicate_or_blank_values(values: tuple[str, ...]) -> None:
    form = FormData(("field", value) for value in values)

    _assert_http_status(lambda: routes_module._single(form, "field"), 400)


def test_single_rejects_uploads_and_normalizes_text() -> None:
    upload = UploadFile(file=BytesIO(b"not text"), filename="value.txt")
    upload_form = FormData((("field", upload),))
    normalized_form = FormData((("field", "  exact value  "),))

    _assert_http_status(lambda: routes_module._single(upload_form, "field"), 400)
    assert routes_module._single(normalized_form, "field") == "exact value"


def test_strings_require_text_values_and_normalize_each_entry() -> None:
    missing = FormData()
    blank = FormData((("field", "one"), ("field", "  ")))
    upload = UploadFile(file=BytesIO(b"not text"), filename="value.txt")
    upload_form = FormData((("field", "one"), ("field", upload)))
    valid = FormData((("field", " one "), ("field", "two")))

    _assert_http_status(lambda: routes_module._strings(missing, "field"), 400)
    _assert_http_status(lambda: routes_module._strings(blank, "field"), 400)
    _assert_http_status(lambda: routes_module._strings(upload_form, "field"), 400)
    assert routes_module._strings(valid, "field") == ["one", "two"]


@pytest.mark.parametrize(
    "document",
    (
        "x" * (32 * 4096 + 1),
        "\n".join(f"/tmp/project-{index}" for index in range(33)),
    ),
)
def test_project_roots_reject_oversized_documents_and_excessive_entries(
    document: str,
) -> None:
    form = FormData((("project_roots", document),))

    _assert_http_status(lambda: routes_module._project_roots(form), 409)


def test_project_roots_drop_blank_lines_and_surrounding_space() -> None:
    form = FormData((("project_roots", "  /first  \n\n /second\t"),))

    assert routes_module._project_roots(form) == ["/first", "/second"]


@pytest.mark.parametrize(
    ("project_id", "source_agent"),
    (
        ("not-a-uuid", "codex"),
        ("12345678-1234-5678-1234-567812345678", "unknown-source"),
    ),
)
def test_namespace_form_rejects_invalid_project_or_source(
    project_id: str,
    source_agent: str,
) -> None:
    form = FormData(
        (
            ("project_id", project_id),
            ("source_agent", source_agent),
            ("model_id", "model-a"),
        )
    )

    _assert_http_status(lambda: routes_module._namespace_form(form), 400)


def test_project_enable_and_relink_success_use_303_redirects(tmp_path: Path) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, Path, bool]:
        with _container(tmp_path) as container:
            original = tmp_path / "original"
            original.mkdir()
            project = container.projects.register(
                ProjectCandidate(canonical_path=original, display_name="Original")
            )
            container.projects.set_enabled(project.project_id, False)
            destination = tmp_path / "destination"
            destination.mkdir()
            async with _client(container) as (client, csrf):
                enabled = await client.post(
                    f"/projects/{project.project_id}/enable",
                    headers=_unsafe(csrf),
                )
                relinked = await client.post(
                    f"/projects/{project.project_id}/relink",
                    headers=_unsafe(csrf),
                    data={"new_path": str(destination)},
                )
            control_record = next(
                item
                for item in container.projects.list_control()
                if item.project_id == project.project_id
            )
            return (
                enabled,
                relinked,
                container.projects.get(project.project_id).canonical_path,
                (control_record.enabled),
            )

    enabled, relinked, stored_path, is_enabled = asyncio.run(scenario())

    assert enabled.status_code == relinked.status_code == 303
    assert enabled.headers["location"] == relinked.headers["location"] == "/projects"
    assert stored_path == (tmp_path / "destination").resolve()
    assert is_enabled is True


def test_missing_project_and_memory_mutations_map_to_not_found(tmp_path: Path) -> None:
    async def scenario() -> list[int]:
        with _container(tmp_path) as container:
            missing_project = uuid4()
            missing_memory = uuid4()
            destination = tmp_path / "destination"
            destination.mkdir()
            async with _client(container) as (client, csrf):
                headers = _unsafe(csrf)
                namespace = {
                    "project_id": str(missing_project),
                    "source_agent": "codex",
                    "model_id": "model-a",
                }
                responses = (
                    await client.post(
                        f"/projects/{missing_project}/enable",
                        headers=headers,
                    ),
                    await client.post(
                        f"/projects/{missing_project}/disable",
                        headers=headers,
                    ),
                    await client.post(
                        f"/projects/{missing_project}/relink",
                        headers=headers,
                        data={"new_path": str(destination)},
                    ),
                    await client.get(f"/memories?project_id={missing_project}"),
                    await client.post(
                        f"/memories/{missing_memory}/delete",
                        headers=headers,
                        data={**namespace, "confirmation": "DELETE"},
                    ),
                )
            return [response.status_code for response in responses]

    assert asyncio.run(scenario()) == [404, 404, 404, 404, 404]


def test_complete_memory_filter_maps_scoped_lookup_failure_to_not_found(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ControlPanelService, "shared_facts", lambda _self, _project_id: ())

    def fail_memories(
        _self: ControlPanelService,
        _project_id: UUID,
        _namespace: Namespace,
    ) -> tuple[()]:
        raise KeyError("synthetic scoped lookup failure")

    monkeypatch.setattr(ControlPanelService, "memories", fail_memories)

    async def scenario() -> int:
        with _container(tmp_path) as container:
            async with _client(container) as (client, _csrf):
                response = await client.get(
                    "/memories",
                    params={
                        "project_id": str(uuid4()),
                        "source_agent": "codex",
                        "model_id": "model-a",
                    },
                )
            return response.status_code

    assert asyncio.run(scenario()) == 404


def test_delete_memory_success_redirects_with_encoded_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[UUID, Namespace, UUID, str]] = []

    def record_delete(
        _self: ControlPanelService,
        project_id: UUID,
        namespace: Namespace,
        memory_id: UUID,
        confirmation: str,
    ) -> None:
        calls.append((project_id, namespace, memory_id, confirmation))

    monkeypatch.setattr(ControlPanelService, "delete_memory", record_delete)

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            async with _client(container) as (client, csrf):
                return await client.post(
                    f"/memories/{memory_id}/delete",
                    headers=_unsafe(csrf),
                    data={
                        "project_id": str(project_id),
                        "source_agent": "codex",
                        "model_id": model_id,
                        "confirmation": "DELETE",
                    },
                )

    project_id = uuid4()
    memory_id = uuid4()
    model_id = "model /?&=中文"
    response = asyncio.run(scenario())
    location = urlsplit(response.headers["location"])

    assert response.status_code == 303
    assert parse_qs(location.query) == {
        "project_id": [str(project_id)],
        "source_agent": ["codex"],
        "model_id": [model_id],
    }
    assert calls == [
        (
            project_id,
            Namespace(source_agent=SourceAgent.CODEX, model_id=model_id),
            memory_id,
            "DELETE",
        )
    ]


def test_delete_and_promote_input_failures_map_to_conflict(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_promotion(
        _self: ControlPanelService,
        _project_id: UUID,
        _namespace: Namespace,
        _memory_id: UUID,
        _proposed_rule: str,
    ) -> None:
        raise ValueError("synthetic rejected rule")

    monkeypatch.setattr(ControlPanelService, "request_promotion", reject_promotion)

    async def scenario() -> tuple[int, int]:
        with _container(tmp_path) as container:
            async with _client(container) as (client, csrf):
                headers = _unsafe(csrf)
                namespace = {
                    "project_id": str(uuid4()),
                    "source_agent": "codex",
                    "model_id": "model-a",
                }
                denied_delete = await client.post(
                    f"/memories/{uuid4()}/delete",
                    headers=headers,
                    data={**namespace, "confirmation": "NOT DELETE"},
                )
                denied_promotion = await client.post(
                    f"/memories/{uuid4()}/promote",
                    headers=headers,
                    data={**namespace, "proposed_rule": "synthetic rule"},
                )
            return denied_delete.status_code, denied_promotion.status_code

    assert asyncio.run(scenario()) == (409, 409)


def test_missing_proposal_mutations_and_bad_rejection_map_client_errors(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[int]:
        with _container(tmp_path) as container:
            proposal_id = uuid4()
            async with _client(container) as (client, csrf):
                headers = _unsafe(csrf)
                responses = (
                    await client.post(
                        f"/proposals/{proposal_id}/approve",
                        headers=headers,
                        data={"confirmation": "APPROVE"},
                    ),
                    await client.post(
                        f"/proposals/{proposal_id}/reject",
                        headers=headers,
                        data={"confirmation": "NOT REJECT"},
                    ),
                    await client.post(
                        f"/proposals/{proposal_id}/apply",
                        headers=headers,
                        data={"confirmation": "APPLY"},
                    ),
                    await client.post(
                        f"/proposals/{proposal_id}/rollback",
                        headers=headers,
                        data={"confirmation": "ROLLBACK"},
                    ),
                )
            return [response.status_code for response in responses]

    assert asyncio.run(scenario()) == [404, 409, 404, 404]


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (UnavailableSourceError("source disabled"), 409),
        (ControlInputError("upload rejected"), 400),
    ),
)
def test_chatgpt_import_maps_available_input_failures_without_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected: int,
) -> None:
    async def reject_import(
        _self: ControlPanelService,
        _upload: UploadFile,
        *,
        dry_run: bool,
    ) -> Any:
        del dry_run
        raise error

    monkeypatch.setattr(ControlPanelService, "import_chatgpt", reject_import)

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            async with _client(container) as (client, csrf):
                return await client.post(
                    "/imports/chatgpt",
                    headers=_unsafe(csrf),
                    files={"archive": ("archive.zip", b"synthetic")},
                )

    response = asyncio.run(scenario())

    assert response.status_code == expected
    assert str(error) not in response.text


def test_chatgpt_import_requires_exactly_one_upload(tmp_path: Path) -> None:
    async def scenario() -> int:
        with _container(tmp_path) as container:
            async with _client(container) as (client, csrf):
                response = await client.post(
                    "/imports/chatgpt",
                    headers=_unsafe(csrf),
                    data={"dry_run": "true"},
                )
            return response.status_code

    assert asyncio.run(scenario()) == 400


def test_cancelled_probe_cleanup_waits_for_worker_and_swallows_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        worker_started = asyncio.Event()
        worker_release = asyncio.Event()
        shield_started = asyncio.Event()
        original_shield = asyncio.shield

        async def fail() -> None:
            worker_started.set()
            await worker_release.wait()
            raise RuntimeError("synthetic worker failure")

        def recording_shield(awaitable: Any) -> asyncio.Future[Any]:
            shield_started.set()
            return original_shield(awaitable)

        monkeypatch.setattr(routes_module.asyncio, "shield", recording_shield)
        worker = asyncio.create_task(fail())
        cleanup = asyncio.create_task(routes_module._finish_cancelled_probe_worker(worker))
        await asyncio.wait_for(worker_started.wait(), timeout=1)
        await asyncio.wait_for(shield_started.wait(), timeout=1)

        cleanup.cancel()
        await asyncio.sleep(0)
        assert cleanup.done() is False

        worker_release.set()
        await cleanup
        assert worker.done()
        assert isinstance(worker.exception(), RuntimeError)

    asyncio.run(scenario())
