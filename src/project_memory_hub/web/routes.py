from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.datastructures import FormData, UploadFile

from project_memory_hub.adapters.base import ReconcileRequiredError
from project_memory_hub.container import ServiceContainer
from project_memory_hub.domain import Namespace, SourceAgent
from project_memory_hub.probes.base import ProbeBusyError
from project_memory_hub.security.archive import UnsafeArchiveError
from project_memory_hub.security.web import limited_form, limited_setup_form, require_csrf
from project_memory_hub.services.control import (
    BehaviorMemoryMetadata,
    ControlInputError,
    ControlPanelService,
    SharedFactMetadata,
    UnavailableSourceError,
)
from project_memory_hub.services.setup import SetupRequest, SetupService
from project_memory_hub.web.presentation import group_source_records, select_next_safe_step


_TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


def build_router(container: ServiceContainer) -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_csrf)])
    control = ControlPanelService(container)
    setup_service = SetupService(container)

    @router.get("/")
    async def overview(request: Request) -> Response:
        snapshot = control.overview()
        return _render(
            request,
            "overview.html",
            title="Overview",
            overview=snapshot,
            setup=setup_service.inspect(),
            setup_completed_notice=(request.query_params.get("setup-complete") == "1"),
            next_safe_step=select_next_safe_step(
                project_count=snapshot.project_count,
                fact_count=snapshot.fact_count,
                permission_error_count=snapshot.permission_error_count,
                startup_status=request.app.state.startup_reconcile_status,
            ),
        )

    @router.get("/setup")
    async def setup_page(request: Request) -> Response:
        return _render(
            request,
            "setup.html",
            title="Setup",
            setup=setup_service.inspect(),
            saved=request.query_params.get("saved") == "1",
        )

    @router.post("/setup/configure")
    async def configure_setup(request: Request) -> Response:
        form = await limited_setup_form(request)
        try:
            setup_service.apply_local(_setup_request(form, complete=False))
        except (ControlInputError, UnavailableSourceError, ValueError):
            raise HTTPException(status_code=409) from None
        return _redirect("/setup?saved=1")

    @router.post("/setup/complete")
    async def complete_setup(request: Request) -> Response:
        form = await limited_setup_form(request)
        try:
            setup_service.apply_local(_setup_request(form, complete=True))
        except (ControlInputError, UnavailableSourceError, ValueError):
            raise HTTPException(status_code=409) from None
        return _redirect("/?setup-complete=1")

    @router.get("/sources")
    async def sources(request: Request) -> Response:
        probe_results = await asyncio.to_thread(container.source_probes.probe_all_light)
        return _render(
            request,
            "sources.html",
            title="Sources",
            source_groups=group_source_records(control.sources(probe_results)),
            restart_required=request.query_params.get("restart-required") == "1",
        )

    @router.post("/sources/trae/probe")
    async def probe_trae_source(request: Request) -> Response:
        form = await limited_form(request)
        _require_empty_probe_form(form)
        try:
            lease = container.source_probes.reserve_structure(SourceAgent.TRAE)
        except ProbeBusyError:
            return _render(
                request,
                "sources.html",
                title="Sources",
                source_groups=group_source_records(control.sources((), probe_error="probe_busy")),
                restart_required=False,
                probe_request_complete=True,
                response_status=409,
            )

        worker_coroutine = asyncio.to_thread(lease.run)
        try:
            worker = asyncio.create_task(worker_coroutine)
        except BaseException:
            worker_coroutine.close()
            lease.close()
            raise
        try:
            structure_result = await asyncio.shield(worker)
        except asyncio.CancelledError:
            await _finish_cancelled_probe_worker(worker)
            raise
        except BaseException:
            lease.close()
            raise

        light_results = await asyncio.to_thread(container.source_probes.probe_all_light)
        results = tuple(
            structure_result if item.source_agent is SourceAgent.TRAE else item
            for item in light_results
        )
        return _render(
            request,
            "sources.html",
            title="Sources",
            source_groups=group_source_records(control.sources(results)),
            restart_required=False,
            probe_request_complete=True,
        )

    @router.post("/sources/{source}/enable")
    async def enable_source(request: Request, source: SourceAgent) -> Response:
        await limited_form(request)
        try:
            control.set_source_enabled(source, True)
        except (ControlInputError, UnavailableSourceError):
            raise HTTPException(status_code=409) from None
        return _redirect("/sources?restart-required=1")

    @router.post("/sources/{source}/disable")
    async def disable_source(request: Request, source: SourceAgent) -> Response:
        await limited_form(request)
        try:
            control.set_source_enabled(source, False)
        except (ControlInputError, UnavailableSourceError):
            raise HTTPException(status_code=409) from None
        return _redirect("/sources?restart-required=1")

    @router.get("/projects")
    async def projects(request: Request) -> Response:
        return _render(
            request,
            "projects.html",
            title="Projects",
            projects=control.projects(),
            discovery=control.discovery_health(),
        )

    @router.post("/projects/{project_id}/enable")
    async def enable_project(request: Request, project_id: UUID) -> Response:
        await limited_form(request)
        try:
            control.set_project_enabled(project_id, True)
        except KeyError:
            raise HTTPException(status_code=404) from None
        return _redirect("/projects")

    @router.post("/projects/{project_id}/disable")
    async def disable_project(request: Request, project_id: UUID) -> Response:
        await limited_form(request)
        try:
            control.set_project_enabled(project_id, False)
        except KeyError:
            raise HTTPException(status_code=404) from None
        return _redirect("/projects")

    @router.post("/projects/{project_id}/relink")
    async def relink_project(request: Request, project_id: UUID) -> Response:
        form = await limited_form(request)
        new_path = _single(form, "new_path")
        try:
            control.relink_project(project_id, new_path)
        except KeyError:
            raise HTTPException(status_code=404) from None
        except (ValueError, FileNotFoundError, NotADirectoryError, PermissionError):
            raise HTTPException(status_code=409) from None
        return _redirect("/projects")

    @router.get("/memories")
    async def memories(
        request: Request,
        project_id: UUID | None = None,
        source_agent: SourceAgent | None = None,
        model_id: str | None = None,
    ) -> Response:
        records: tuple[BehaviorMemoryMetadata, ...] = ()
        facts: tuple[SharedFactMetadata, ...] = ()
        if project_id is not None:
            try:
                facts = control.shared_facts(project_id)
            except KeyError:
                raise HTTPException(status_code=404) from None
        filter_complete = (
            project_id is not None
            and source_agent is not None
            and model_id is not None
            and bool(model_id.strip())
        )
        if filter_complete:
            assert project_id is not None and source_agent is not None and model_id is not None
            namespace = Namespace(source_agent=source_agent, model_id=model_id)
            try:
                records = control.memories(project_id, namespace)
            except KeyError:
                raise HTTPException(status_code=404) from None
        return _render(
            request,
            "memories.html",
            title="Memories",
            projects=control.projects(),
            facts=facts,
            records=records,
            filter_complete=filter_complete,
            selected_project_id=project_id,
            selected_source=source_agent,
            selected_model=control.display_model_id(model_id),
        )

    @router.post("/memories/{memory_id}/archive")
    async def archive_memory(request: Request, memory_id: UUID) -> Response:
        form = await limited_form(request)
        project_id, namespace = _namespace_form(form)
        try:
            control.archive_memory(
                project_id,
                namespace,
                memory_id,
                _single(form, "confirmation"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except ControlInputError:
            raise HTTPException(status_code=409) from None
        return _memory_redirect(project_id, namespace)

    @router.post("/memories/{memory_id}/delete")
    async def delete_memory(request: Request, memory_id: UUID) -> Response:
        form = await limited_form(request)
        project_id, namespace = _namespace_form(form)
        try:
            control.delete_memory(
                project_id,
                namespace,
                memory_id,
                _single(form, "confirmation"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except ControlInputError:
            raise HTTPException(status_code=409) from None
        return _memory_redirect(project_id, namespace)

    @router.post("/memories/{memory_id}/promote")
    async def promote_memory(request: Request, memory_id: UUID) -> Response:
        form = await limited_form(request)
        project_id, namespace = _namespace_form(form)
        try:
            control.request_promotion(
                project_id,
                namespace,
                memory_id,
                _single(form, "proposed_rule"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except (ControlInputError, ValueError):
            raise HTTPException(status_code=409) from None
        return _redirect("/proposals")

    @router.post("/promotions/{promotion_id}/approve")
    async def approve_promotion(request: Request, promotion_id: UUID) -> Response:
        form = await limited_form(request)
        project_id, namespace = _namespace_form(form)
        try:
            control.approve_promotion(
                project_id,
                namespace,
                promotion_id,
                _single(form, "confirmation"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except (ControlInputError, ValueError):
            raise HTTPException(status_code=409) from None
        return _redirect("/proposals")

    @router.get("/imports")
    async def imports(request: Request) -> Response:
        return _render(
            request,
            "imports.html",
            title="Imports",
            import_notice=_import_notice(request),
        )

    @router.post("/imports/chatgpt")
    async def import_chatgpt(request: Request) -> Response:
        form = await limited_form(request)
        uploads = form.getlist("archive")
        if len(uploads) != 1 or not isinstance(uploads[0], UploadFile):
            raise HTTPException(status_code=400)
        dry_values = form.getlist("dry_run")
        if len(dry_values) > 1 or any(not isinstance(item, str) for item in dry_values):
            raise HTTPException(status_code=400)
        dry_run = bool(dry_values and dry_values[0] == "true")
        try:
            report = await control.import_chatgpt(uploads[0], dry_run=dry_run)
        except ReconcileRequiredError:
            raise HTTPException(status_code=409) from None
        except UnsafeArchiveError:
            raise HTTPException(status_code=400) from None
        except UnavailableSourceError:
            raise HTTPException(status_code=409) from None
        except ControlInputError:
            raise HTTPException(status_code=400) from None
        query = urlencode(
            {
                "status": "checked" if report.dry_run else "imported",
                "matches": report.imported_count,
                "confirmations": report.confirmation_count,
            }
        )
        return _redirect(f"/imports?{query}")

    @router.get("/proposals")
    async def proposals(request: Request) -> Response:
        return _render(
            request,
            "proposals.html",
            title="Proposals",
            proposals=control.proposals(),
            promotions=control.pending_promotions(),
        )

    @router.post("/proposals/{proposal_id}/approve")
    async def approve_proposal(request: Request, proposal_id: UUID) -> Response:
        form = await limited_form(request)
        try:
            control.approve_proposal(
                proposal_id,
                _single(form, "confirmation"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except ControlInputError:
            raise HTTPException(status_code=409) from None
        return _redirect("/proposals")

    @router.post("/proposals/{proposal_id}/reject")
    async def reject_proposal(request: Request, proposal_id: UUID) -> Response:
        form = await limited_form(request)
        try:
            control.reject_proposal(
                proposal_id,
                _single(form, "confirmation"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except ControlInputError:
            raise HTTPException(status_code=409) from None
        return _redirect("/proposals")

    @router.post("/proposals/{proposal_id}/apply")
    async def apply_proposal(request: Request, proposal_id: UUID) -> Response:
        form = await limited_form(request)
        try:
            await asyncio.to_thread(
                control.apply_proposal,
                proposal_id,
                _single(form, "confirmation"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except ControlInputError:
            raise HTTPException(status_code=409) from None
        return _redirect("/proposals")

    @router.post("/proposals/{proposal_id}/rollback")
    async def rollback_proposal(request: Request, proposal_id: UUID) -> Response:
        form = await limited_form(request)
        try:
            await asyncio.to_thread(
                control.rollback_proposal,
                proposal_id,
                _single(form, "confirmation"),
            )
        except KeyError:
            raise HTTPException(status_code=404) from None
        except ControlInputError:
            raise HTTPException(status_code=409) from None
        return _redirect("/proposals")

    @router.get("/settings")
    async def settings(request: Request) -> Response:
        return _render(
            request,
            "settings.html",
            title="Settings",
            config=control.settings(),
            automation_status=control.automation_status(),
            restart_required=request.query_params.get("restart-required") == "1",
        )

    @router.post("/settings")
    async def save_settings(request: Request) -> Response:
        form = await limited_form(request)
        try:
            roots = _project_roots(form)
            enabled_sources = _strings(form, "enabled_sources")
            control.save_settings(
                project_roots=roots,
                enabled_sources=enabled_sources,
                inactive_days=_single(form, "inactive_days"),
                max_recall_tokens=_single(form, "max_recall_tokens"),
                daily_reconcile_time=_single(form, "daily_reconcile_time"),
            )
        except (ControlInputError, ValueError):
            raise HTTPException(status_code=409) from None
        return _redirect("/settings?restart-required=1")

    return router


def _render(
    request: Request,
    name: str,
    *,
    response_status: int = 200,
    **context: Any,
) -> Response:
    return _TEMPLATES.TemplateResponse(
        request=request,
        name=name,
        context={
            **context,
            "csrf_token": request.state.pmh_csrf,
        },
        status_code=response_status,
    )


def _require_empty_probe_form(form: FormData) -> None:
    items = form.multi_items()
    if len(items) != 1 or items[0][0] != "csrf_token" or not isinstance(items[0][1], str):
        raise HTTPException(status_code=400)


def _require_exact_setup_config_form(form: FormData) -> None:
    allowed = {
        "csrf_token",
        "expected_revision",
        "project_roots",
        "enabled_sources",
        "inactive_days",
        "max_recall_tokens",
        "daily_reconcile_time",
    }
    if {name for name, _value in form.multi_items()} != allowed:
        raise HTTPException(status_code=400)


def _setup_request(form: FormData, *, complete: bool) -> SetupRequest:
    _require_exact_setup_config_form(form)
    return SetupRequest(
        project_roots=tuple(_project_roots(form)),
        enabled_sources=tuple(_strings(form, "enabled_sources")),
        inactive_days=_single(form, "inactive_days"),
        max_recall_tokens=_single(form, "max_recall_tokens"),
        daily_reconcile_time=_single(form, "daily_reconcile_time"),
        complete=complete,
        expected_revision=_single(form, "expected_revision"),
    )


async def _finish_cancelled_probe_worker(worker: asyncio.Task[Any]) -> None:
    while not worker.done():
        try:
            await asyncio.shield(worker)
        except asyncio.CancelledError:
            continue
        except BaseException:
            break
    if worker.done():
        try:
            worker.result()
        except BaseException:
            pass


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(path, status_code=303)


def _single(form: FormData, name: str) -> str:
    values = form.getlist(name)
    if len(values) != 1 or not isinstance(values[0], str):
        raise HTTPException(status_code=400)
    value = values[0].strip()
    if not value:
        raise HTTPException(status_code=400)
    return value


def _strings(form: FormData, name: str) -> list[str]:
    values = form.getlist(name)
    if not values:
        raise HTTPException(status_code=400)
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str):
            raise HTTPException(status_code=400)
        stripped = value.strip()
        if not stripped:
            raise HTTPException(status_code=400)
        normalized.append(stripped)
    return normalized


def _project_roots(form: FormData) -> list[str]:
    document = _single(form, "project_roots")
    if len(document) > 32 * 4096:
        raise HTTPException(status_code=409)
    roots = [line.strip() for line in document.splitlines() if line.strip()]
    if not roots or len(roots) > 32:
        raise HTTPException(status_code=409)
    return roots


def _namespace_form(form: FormData) -> tuple[UUID, Namespace]:
    try:
        project_id = UUID(_single(form, "project_id"))
        namespace = Namespace(
            source_agent=SourceAgent(_single(form, "source_agent")),
            model_id=_single(form, "model_id"),
        )
    except ValueError:
        raise HTTPException(status_code=400) from None
    return project_id, namespace


def _memory_redirect(project_id: UUID, namespace: Namespace) -> RedirectResponse:
    query = urlencode(
        {
            "project_id": str(project_id),
            "source_agent": namespace.source_agent.value,
            "model_id": namespace.model_id,
        }
    )
    return _redirect(f"/memories?{query}")


def _import_notice(request: Request) -> dict[str, int | str] | None:
    status = request.query_params.get("status")
    if status not in {"checked", "imported"}:
        return None
    matches = _bounded_query_count(request.query_params.get("matches"))
    confirmations = _bounded_query_count(request.query_params.get("confirmations"))
    if matches is None or confirmations is None:
        return None
    return {
        "status": status,
        "matches": matches,
        "confirmations": confirmations,
    }


def _bounded_query_count(value: str | None) -> int | None:
    if value is None or not value.isascii() or not value.isdigit() or len(value) > 7:
        return None
    count = int(value)
    return count if count <= 1_000_000 else None
