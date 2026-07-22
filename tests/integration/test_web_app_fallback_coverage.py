from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

import project_memory_hub.web.app as app_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import ServiceContainer, build_container
from project_memory_hub.domain import SourceAgent
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.web.app import create_app
from project_memory_hub.web.errors import error_response


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


class _RecordingReconcile:
    def __init__(
        self,
        *,
        due: bool = False,
        should_run_error: Exception | None = None,
        report_status: str = "success",
        run_error: Exception | None = None,
    ) -> None:
        self.due = due
        self.should_run_error = should_run_error
        self.report_status = report_status
        self.run_error = run_error
        self.should_run_calls = 0
        self.run_forces: list[bool] = []

    def should_run(self) -> bool:
        self.should_run_calls += 1
        if self.should_run_error is not None:
            raise self.should_run_error
        return self.due

    def run(self, *, force: bool = False) -> SimpleNamespace:
        self.run_forces.append(force)
        if self.run_error is not None:
            raise self.run_error
        return SimpleNamespace(status=self.report_status)


@pytest.mark.parametrize(
    ("reconcile", "expected_status"),
    [
        (_RecordingReconcile(due=False), "not_due"),
        (
            _RecordingReconcile(should_run_error=RuntimeError("sensitive schedule failure")),
            "degraded",
        ),
    ],
    ids=("not-due", "schedule-check-failed"),
)
def test_lifespan_fails_closed_when_startup_reconcile_is_not_due_or_uncheckable(
    container: ServiceContainer,
    reconcile: _RecordingReconcile,
    expected_status: str,
) -> None:
    container.reconcile = reconcile  # type: ignore[assignment]
    app = create_app(container)

    async def scenario() -> tuple[str, str]:
        async with app.router.lifespan_context(app):
            active_status = app.state.startup_reconcile_status
        return active_status, app.state.startup_reconcile_status

    active_status, final_status = asyncio.run(scenario())

    assert (active_status, final_status) == (expected_status, expected_status)
    assert reconcile.should_run_calls == 1
    assert reconcile.run_forces == []


def test_incomplete_setup_skips_startup_reconcile_with_a_stable_status(
    tmp_path: Path,
) -> None:
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
            setup_completed=False,
        )
    )

    with build_container(config_path) as selected:
        reconcile = _RecordingReconcile(due=True)
        selected.reconcile = reconcile  # type: ignore[assignment]
        app = create_app(selected)

        async def scenario() -> str:
            async with app.router.lifespan_context(app):
                return app.state.startup_reconcile_status

        status = asyncio.run(scenario())

    assert status == "setup_required"
    assert reconcile.should_run_calls == 0
    assert reconcile.run_forces == []


@pytest.mark.parametrize(
    ("report_status", "run_error", "expected_status"),
    [
        ("success", None, "complete"),
        ("failed", None, "degraded"),
        ("degraded", None, "degraded"),
        ("success", RuntimeError("sensitive reconcile failure"), "degraded"),
    ],
    ids=("success", "failed-report", "degraded-report", "worker-exception"),
)
def test_due_startup_reconcile_records_only_bounded_status(
    container: ServiceContainer,
    report_status: str,
    run_error: Exception | None,
    expected_status: str,
) -> None:
    reconcile = _RecordingReconcile(
        due=True,
        report_status=report_status,
        run_error=run_error,
    )
    container.reconcile = reconcile  # type: ignore[assignment]
    app = create_app(container)

    async def scenario() -> str:
        async with app.router.lifespan_context(app):
            pass
        return app.state.startup_reconcile_status

    assert asyncio.run(scenario()) == expected_status
    assert reconcile.should_run_calls == 1
    assert reconcile.run_forces == [False]


def test_cancelled_shutdown_shields_reconcile_until_worker_finishes(
    container: ServiceContainer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BlockingReconcile:
        def __init__(self) -> None:
            self.started = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def should_run(self) -> bool:
            return True

        def run(self, *, force: bool = False) -> SimpleNamespace:
            assert force is False
            self.started.set()
            try:
                if not self.release.wait(timeout=2):
                    raise TimeoutError("test worker was not released")
                return SimpleNamespace(status="success")
            finally:
                self.finished.set()

    reconcile = BlockingReconcile()
    container.reconcile = reconcile  # type: ignore[assignment]
    app = create_app(container)
    original_shield = asyncio.shield

    async def scenario() -> tuple[bool, bool, str]:
        shield_started = asyncio.Event()

        def recording_shield(task: asyncio.Task[None]) -> asyncio.Future[None]:
            shield_started.set()
            return original_shield(task)

        monkeypatch.setattr(app_module.asyncio, "shield", recording_shield)

        async def use_lifespan() -> None:
            async with app.router.lifespan_context(app):
                pass

        lifespan_task = asyncio.create_task(use_lifespan())
        await asyncio.wait_for(shield_started.wait(), timeout=1)
        lifespan_task.cancel()
        await asyncio.sleep(0)
        waited_for_worker = not lifespan_task.done()
        reconcile.release.set()
        with pytest.raises(asyncio.CancelledError):
            await lifespan_task
        return (
            waited_for_worker,
            reconcile.finished.is_set(),
            app.state.startup_reconcile_status,
        )

    waited_for_worker, worker_finished, status = asyncio.run(scenario())

    assert waited_for_worker is True
    assert worker_finished is True
    assert status == "complete"


async def _authenticated_responses(
    app: FastAPI,
    container: ServiceContainer,
    paths: tuple[str, ...],
) -> tuple[httpx.Response, ...]:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    token = LocalAccessToken.load_or_create(container.paths)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as bootstrap_client:
        bootstrap = await bootstrap_client.get(
            f"/?token={token}",
            follow_redirects=False,
        )
    assert bootstrap.status_code == 303
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        cookies=bootstrap.cookies,
    ) as client:
        return tuple([await client.get(path) for path in paths])


def test_validation_error_returns_fixed_html_without_schema_details(
    container: ServiceContainer,
) -> None:
    app = create_app(container)

    @app.get("/__coverage__/validated")
    async def validated(count: int) -> dict[str, int]:
        return {"count": count}

    (response,) = asyncio.run(
        _authenticated_responses(
            app,
            container,
            ("/__coverage__/validated?count=private-invalid-value",),
        )
    )

    assert response.status_code == 422
    assert response.content == error_response(422).body
    assert "private-invalid-value" not in response.text
    assert response.headers["content-type"].startswith("text/html")


def test_http_errors_use_safe_status_specific_html_and_unknown_fallback(
    container: ServiceContainer,
) -> None:
    app = create_app(container)

    @app.get("/__coverage__/http-error/{status_code}")
    async def fail_with_http_error(status_code: int) -> None:
        raise HTTPException(
            status_code=status_code,
            detail=f"private detail for {status_code}",
        )

    expected = (400, 401, 403, 404, 409, 413, 418)
    responses = asyncio.run(
        _authenticated_responses(
            app,
            container,
            tuple(f"/__coverage__/http-error/{status}" for status in expected),
        )
    )

    assert [response.status_code for response in responses] == list(expected)
    assert [response.content for response in responses] == [
        error_response(status).body for status in expected
    ]
    assert all("private detail" not in response.text for response in responses)
