from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException
from starlette.responses import HTMLResponse

from project_memory_hub.container import ServiceContainer
from project_memory_hub.security.web import (
    LocalAccessToken,
    LocalWebBoundary,
    WebRequestLimits,
)
from project_memory_hub.web.errors import error_response
from project_memory_hub.web.routes import build_router


def create_app(
    container: ServiceContainer,
    *,
    request_limits: WebRequestLimits | None = None,
) -> FastAPI:
    limits = request_limits or WebRequestLimits()
    token = LocalAccessToken.load_or_create(container.paths)

    @asynccontextmanager
    async def lifespan(selected_app: FastAPI) -> AsyncIterator[None]:
        task: asyncio.Task[None] | None = None
        try:
            setup_completed = container.config_manager.load().setup_completed
        except Exception:
            selected_app.state.startup_reconcile_status = "degraded"
        else:
            if not setup_completed:
                selected_app.state.startup_reconcile_status = "setup_required"
            else:
                try:
                    due = container.reconcile.should_run()
                except Exception:
                    selected_app.state.startup_reconcile_status = "degraded"
                else:
                    if due:
                        selected_app.state.startup_reconcile_status = "running"
                        task = asyncio.create_task(
                            _run_startup_reconcile(selected_app, container),
                            name="project-memory-hub-startup-reconcile",
                        )
                    else:
                        selected_app.state.startup_reconcile_status = "not_due"
        try:
            yield
        finally:
            if task is not None:
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    # A to_thread worker cannot be force-cancelled. Keep the
                    # container alive until its reconcile call really returns.
                    with suppress(asyncio.CancelledError):
                        await task
                    raise

    app = FastAPI(
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.container = container
    app.state.request_limits = limits
    app.state.startup_reconcile_status = "not_checked"
    app.include_router(build_router(container))
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )
    app.add_middleware(
        LocalWebBoundary,
        access_token=token,
        request_limits=limits,
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _error: RequestValidationError) -> HTMLResponse:
        return error_response(422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, error: HTTPException) -> HTMLResponse:
        return error_response(error.status_code)

    return app


async def _run_startup_reconcile(
    app: FastAPI,
    container: ServiceContainer,
) -> None:
    try:
        report = await asyncio.to_thread(container.reconcile.run, force=False)
    except Exception:
        app.state.startup_reconcile_status = "degraded"
        return
    status = getattr(report, "status", "success")
    app.state.startup_reconcile_status = (
        "degraded" if status in {"failed", "degraded"} else "complete"
    )
