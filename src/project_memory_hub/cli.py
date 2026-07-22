from __future__ import annotations

import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, TypeVar, cast
from uuid import UUID

import typer
import uvicorn
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from typer import _click as click
from typer.core import TyperGroup

from project_memory_hub import __version__
from project_memory_hub.adapters.base import ReconcileRequiredError
from project_memory_hub.adapters.codex import CodexAdapter, CodexContextUnavailable
from project_memory_hub.container import (
    ProbeContainer,
    ReadonlyRecallContainer,
    ServiceContainer,
    build_container,
    build_doctor_container,
    build_probe_container,
    build_readonly_chatgpt_container,
    build_readonly_compaction_container,
    build_readonly_proposal_container,
    build_readonly_recall_container,
    build_readonly_setup_container,
    configured_source_enabled,
    runtime_paths_for_config,
)
from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    RecallRequest,
    SourceAgent,
)
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.integration.agents import (
    AgentsIntegration,
    AgentsIntegrationError,
    FileChange,
)
from project_memory_hub.integration.automation import InstallationIdentity
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.probes.base import ProbeBusyError
from project_memory_hub.probes.builtin import OPTIONAL_PROBE_SOURCES
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ModelStatus,
    ProbeMode,
    StructureStatus,
)
from project_memory_hub.security.archive import UnsafeArchiveError
from project_memory_hub.security.redaction import Redactor, SensitivePathError
from project_memory_hub.security.web import LocalAccessToken, loopback_bind_host
from project_memory_hub.services.control import ControlInputError, UnavailableSourceError
from project_memory_hub.services.deferred_recovery import DeferredRecoveryService
from project_memory_hub.services.pending_recovery import (
    PendingRecoveryError,
    PendingRecoveryMapping,
    PendingRecoveryService,
)
from project_memory_hub.services.setup import SetupRequest, SetupService, SetupSnapshot
from project_memory_hub.storage.database import SchemaUpgradeRequiredError
from project_memory_hub.storage.deferred_records import DeferredRecoveryError
from project_memory_hub.storage.proposals import ProposalError
from project_memory_hub.utf8 import InvalidUtf8Text
from project_memory_hub.web.app import create_app


_MAX_STDIN_BYTES = 1024 * 1024
_MAX_OUTPUT_COUNT = 2**31 - 1
_FORMATS = frozenset({"json", "text"})
_RECALL_FORMATS = frozenset({"json", "prompt", "text"})
_ModelT = TypeVar("_ModelT", bound=BaseModel)
_ContainerT = TypeVar("_ContainerT")

_TEXT_ERROR_COPY = {
    "codex_context_unavailable": (
        "The active Codex context is unavailable.",
        'Run memory-hub codex-context --cwd "$PWD" --format json in the active task.',
    ),
    "invalid_input": (
        "The command input was not accepted.",
        "Review the command syntax and try again.",
    ),
    "not_available": (
        "The requested integration is not available.",
        "Install Project Memory Hub from a stable launcher, then retry.",
    ),
    "operation_failed": (
        "The operation could not be completed safely.",
        "Run memory-hub doctor --format json before retrying.",
    ),
    "permission_denied": (
        "The operation was denied by local policy.",
        "Review local permissions, then run memory-hub doctor --format json.",
    ),
    "probe_busy": (
        "The source probe is already running.",
        "Wait for the current probe to finish, then retry.",
    ),
    "project_not_found": (
        "The registered project was not found.",
        "Preview discovery with memory-hub discover --dry-run --format json.",
    ),
    "reconcile_required": (
        "Reconciliation is required before this operation.",
        "Run memory-hub reconcile --if-due --format json, then retry.",
    ),
    "source_disabled": (
        "The requested source is disabled.",
        "Review enabled_sources in the private configuration before retrying.",
    ),
}
_TEXT_ERROR_FALLBACK_CODE = "operation_failed"

_PROBE_SOURCE_LABELS = {
    SourceAgent.TRAE.value: "Trae",
    SourceAgent.WORKBUDDY.value: "WorkBuddy",
    SourceAgent.ZCODE.value: "Zcode",
    SourceAgent.QODERWORK.value: "QoderWork",
    SourceAgent.CLAUDE_CODE.value: "Claude Code",
}
_INSTALLATION_LABELS = {
    InstallationStatus.DETECTED.value: "Detected",
    InstallationStatus.NOT_DETECTED.value: "Not detected",
}
_DATA_LABELS = {
    DataStatus.READABLE.value: "Readable",
    DataStatus.BLOCKED.value: "Permission blocked",
    DataStatus.MISSING.value: "Missing",
    DataStatus.REJECTED.value: "Rejected",
}
_MODEL_LABELS = {
    ModelStatus.NOT_CHECKED.value: "Not checked",
    ModelStatus.UNVERIFIABLE.value: "Unverifiable",
}
_STRUCTURE_LABELS = {
    StructureStatus.NOT_RUN.value: "Not run",
    StructureStatus.RECOGNIZED.value: "Recognized",
    StructureStatus.PARTIAL.value: "Partial",
    StructureStatus.UNSUPPORTED.value: "Unsupported",
}


class _SafeTyperGroup(TyperGroup):
    def main(
        self,
        args: Sequence[str] | None = None,
        prog_name: str | None = None,
        complete_var: str | None = None,
        standalone_mode: bool = True,
        windows_expand_args: bool = True,
        **extra: Any,
    ) -> Any:
        supplied = list(args) if args is not None else list(sys.argv[1:])
        try:
            result = super().main(
                args=supplied,
                prog_name=prog_name,
                complete_var=complete_var,
                standalone_mode=False,
                windows_expand_args=windows_expand_args,
                **extra,
            )
        except click.ClickException:
            _emit_parse_error(supplied)
            if standalone_mode:
                raise SystemExit(4) from None
            raise click.exceptions.Exit(4) from None
        if standalone_mode:
            raise SystemExit(result if isinstance(result, int) else 0)
        return result


app = typer.Typer(
    cls=_SafeTyperGroup,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    suggest_commands=False,
)


@dataclass(frozen=True, slots=True)
class _CliState:
    config_path: Path | None
    debug: bool


@dataclass(frozen=True, slots=True)
class ProbeCliRequest:
    source: SourceAgent | None
    all_sources: bool
    mode: ProbeMode


class _ProposalCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: str
    title: str
    description: str
    risk: Literal["low", "medium", "high"]
    patch: str
    verification_argv: tuple[str, ...]
    target_version: str | None = None
    token: str | None = None


class _ManualRecallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr
    request: RecallRequest


class _DeferredRecoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_record_id: str
    target_project: Path
    apply: bool = False


class _PendingRecoveryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mappings: tuple[PendingRecoveryMapping, ...]
    apply: bool = False


class _ProposalMutationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: str | None = None


class _CliFailure(Exception):
    def __init__(self, code: str, message: str, exit_code: int) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.exit_code = exit_code


@app.callback()
def main(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config"),
    debug: bool = typer.Option(False, "--debug"),
) -> None:
    """Run Project Memory Hub commands."""
    ctx.obj = _CliState(config_path=config, debug=debug)


source_app = typer.Typer(no_args_is_help=True)
app.add_typer(source_app, name="source", help="Inspect optional local sources.")


@source_app.command("probe")
def source_probe_command(
    ctx: typer.Context,
    source: str | None = typer.Argument(None),
    all_sources: bool = typer.Option(False, "--all"),
    structure: bool = typer.Option(False, "--structure"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Inspect fixed optional sources without writing Project Memory Hub state."""
    _validate_format(output_format)
    try:
        request = _probe_request(source, all_sources, structure)
    except _CliFailure as error:
        _emit_error(output_format, error.code, error.message)
        raise typer.Exit(error.exit_code) from None

    def operation(container: ProbeContainer) -> dict[str, Any]:
        try:
            results = (
                container.source_probes.probe_all_light()
                if request.all_sources
                else (
                    container.source_probes.probe_one(
                        cast(SourceAgent, request.source),
                        mode=request.mode,
                    ),
                )
            )
        except ProbeBusyError:
            raise _CliFailure("probe_busy", "Source probe is busy.", 2) from None
        return {
            "results": [result.model_dump(mode="json") for result in results],
            "status": "ok",
        }

    _run(
        ctx,
        output_format,
        operation,
        builder=_build_cli_probe_container,
        redact_exceptions=True,
        text_renderer=_probe_text,
    )


def _probe_request(
    source: str | None,
    all_sources: bool,
    structure: bool,
) -> ProbeCliRequest:
    if (source is None) == (not all_sources) or (all_sources and structure):
        raise _CliFailure("invalid_input", "Invalid source probe request.", 4)
    if source is None:
        return ProbeCliRequest(
            source=None,
            all_sources=True,
            mode=ProbeMode.LIGHT,
        )
    try:
        selected = SourceAgent(source)
    except ValueError:
        raise _CliFailure("invalid_input", "Invalid source probe request.", 4) from None
    if selected not in OPTIONAL_PROBE_SOURCES or (structure and selected is not SourceAgent.TRAE):
        raise _CliFailure("invalid_input", "Invalid source probe request.", 4)
    return ProbeCliRequest(
        source=selected,
        all_sources=False,
        mode=ProbeMode.STRUCTURE if structure else ProbeMode.LIGHT,
    )


def _build_cli_probe_container(_config_path: Path | None) -> ProbeContainer:
    return build_probe_container()


def _probe_text(response: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in response["results"]:
        warnings = ",".join(item["warning_codes"]) or "none"
        lines.append(
            " | ".join(
                (
                    _PROBE_SOURCE_LABELS[item["source_agent"]],
                    f"Detected: {_INSTALLATION_LABELS[item['installation_status']]}",
                    f"Probe health: {_DATA_LABELS[item['data_status']]}",
                    f"Model identity: {_MODEL_LABELS[item['model_status']]}",
                    f"Structure: {_STRUCTURE_LABELS[item['structure_status']]}",
                    "Behavior import: Locked",
                    f"Warnings: {warnings}",
                )
            )
        )
    return "\n".join(lines)


@app.command()
def init(
    ctx: typer.Context,
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Initialize private local storage."""

    def operation(container: ServiceContainer) -> dict[str, Any]:
        LocalAccessToken.load_or_create(container.paths)
        return {"status": "initialized"}

    _run(ctx, output_format, operation, text_renderer=_init_text)


def _init_text(_response: dict[str, Any]) -> str:
    return "\n".join(
        (
            "initialized",
            "Next steps:",
            "1. Review first-run setup: memory-hub setup",
            "2. Preview discovery: memory-hub discover --dry-run --format json",
            "3. Apply discovery: memory-hub discover --format json",
            "4. Install AGENTS integration: memory-hub integrate agents install --format json",
            "5. Check local health: memory-hub doctor --format json",
        )
    )


@app.command()
def setup(
    ctx: typer.Context,
    project_root: list[Path] | None = typer.Option(None, "--project-root"),
    source: list[str] | None = typer.Option(None, "--source"),
    inactive_days: int | None = typer.Option(None, "--inactive-days"),
    max_recall_tokens: int | None = typer.Option(None, "--max-recall-tokens"),
    daily_reconcile_time: str | None = typer.Option(None, "--daily-reconcile-time"),
    complete: bool = typer.Option(False, "--complete"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Inspect or explicitly update first-run local configuration."""

    inspect_only = (
        all(
            value is None
            for value in (
                project_root,
                source,
                inactive_days,
                max_recall_tokens,
                daily_reconcile_time,
            )
        )
        and not complete
    )

    def operation(container: ServiceContainer) -> dict[str, Any]:
        service = SetupService(container)
        if inspect_only:
            return _setup_payload(service.inspect(), setup_status="inspected")
        try:
            result = service.apply_local(
                SetupRequest(
                    project_roots=(
                        None if project_root is None else tuple(str(path) for path in project_root)
                    ),
                    enabled_sources=None if source is None else tuple(source),
                    inactive_days=None if inactive_days is None else str(inactive_days),
                    max_recall_tokens=(
                        None if max_recall_tokens is None else str(max_recall_tokens)
                    ),
                    daily_reconcile_time=daily_reconcile_time,
                    complete=complete,
                )
            )
        except (ControlInputError, UnavailableSourceError, ValueError):
            raise _CliFailure("invalid_input", "Invalid setup input.", 4) from None
        return _setup_payload(result.snapshot, setup_status=result.status)

    _run(
        ctx,
        output_format,
        operation,
        text_renderer=_setup_text,
        builder=cast(
            Callable[[Path | None], ServiceContainer],
            build_readonly_setup_container if inspect_only else build_container,
        ),
    )


def _setup_payload(
    snapshot: SetupSnapshot,
    *,
    setup_status: str,
) -> dict[str, Any]:
    return {
        "automation_status": snapshot.automation_status,
        "behavior_count": snapshot.behavior_count,
        "daily_reconcile_time": snapshot.daily_reconcile_time,
        "enabled_sources": list(snapshot.enabled_sources),
        "fact_count": snapshot.fact_count,
        "next_step": snapshot.next_step,
        "project_count": snapshot.project_count,
        "root_count": len(snapshot.project_roots),
        "setup_completed": snapshot.setup_completed,
        "setup_status": setup_status,
        "status": "ok",
        "valid_root_count": snapshot.valid_root_count,
    }


def _setup_text(response: dict[str, Any]) -> str:
    sources = ", ".join(str(item) for item in response["enabled_sources"])
    return "\n".join(
        (
            f"setup: {response['setup_status']}",
            f"completed: {str(response['setup_completed']).lower()}",
            f"sources: {sources}",
            f"project roots: {response['valid_root_count']}/{response['root_count']} ready",
            f"daily reconcile: {response['automation_status']} at "
            f"{response['daily_reconcile_time']}",
            f"next: {response['next_step']}",
        )
    )


@app.command()
def discover(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Discover projects under configured roots."""

    def operation(container: ServiceContainer) -> dict[str, Any]:
        result = container.project_scanner.discover()
        if dry_run:
            projects: list[Any] = [
                candidate.model_dump(mode="json") for candidate in result.candidates
            ]
        else:
            container.discovery_findings.sync(result)
            with container.projects.discovery_batch():
                projects = [
                    container.projects.register(candidate).model_dump(mode="json")
                    for candidate in result.candidates
                ]
        issues = [{"code": issue.code, "remediation": issue.remediation} for issue in result.issues]
        return {
            "candidates" if dry_run else "projects": projects,
            "dry_run": dry_run,
            "issues": issues,
            "status": "ok",
        }

    _run(ctx, output_format, operation)


@app.command()
def scan(
    ctx: typer.Context,
    cwd: Path = typer.Option(..., "--cwd"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Scan deterministic facts for a registered project."""

    def operation(container: ServiceContainer) -> dict[str, Any]:
        project = container.projects.find_by_cwd(cwd)
        if project is None:
            raise _CliFailure("project_not_found", "Registered project was not found.", 1)
        report = container.project_facts.scan(project, dry_run=dry_run)
        return {
            **report.model_dump(mode="json"),
            "dry_run": dry_run,
            "status": "ok",
        }

    _run(ctx, output_format, operation)


@app.command()
def capture(
    ctx: typer.Context,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Capture an untrusted structured task result from stdin."""

    def operation(container: ServiceContainer) -> dict[str, Any]:
        payload = _stdin_model(CapturePayload, stdin_json)
        result = _capture_with_transient_retry(container, payload)
        if result.status == "project_not_found":
            raise _CliFailure("project_not_found", "Registered project was not found.", 1)
        return {**result.model_dump(mode="json"), "status": result.status}

    _run(ctx, output_format, operation)


@app.command()
def recall(
    ctx: typer.Context,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    manual: bool = typer.Option(False, "--manual"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Recall a bounded namespace-scoped brief from a JSON stdin request."""

    prepared: RecallRequest | _ManualRecallInput | None = None

    def prepare(_state: _CliState) -> None:
        nonlocal prepared
        if manual:
            prepared = _stdin_model(_ManualRecallInput, stdin_json)
            return
        request = _stdin_model(RecallRequest, stdin_json)
        _require_live_codex_namespace(request)
        prepared = request

    def operation(container: ServiceContainer | ReadonlyRecallContainer) -> dict[str, Any]:
        if manual:
            assert isinstance(prepared, _ManualRecallInput)
            owner_request = prepared
            _authorize_manual_recall(container.paths, owner_request.token)
            request = owner_request.request
        else:
            assert isinstance(prepared, RecallRequest)
            request = prepared
        result = container.recall.recall(request)
        if "project_not_found" in result.warnings:
            raise _CliFailure("project_not_found", "Registered project was not found.", 1)
        return {**result.model_dump(mode="json"), "status": "ok"}

    _run(
        ctx,
        output_format,
        operation,
        allowed_formats=_RECALL_FORMATS,
        text_renderer=lambda response: str(response["text"]),
        builder=build_readonly_recall_container,
        before_build=prepare,
    )


deferred_app = typer.Typer(no_args_is_help=True)
app.add_typer(deferred_app, name="deferred", help="Audit or recover exact deferred captures.")


@deferred_app.command("recover")
def recover_deferred_command(
    ctx: typer.Context,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Preview or apply one explicit source-to-project recovery mapping."""

    def operation(container: ServiceContainer) -> dict[str, Any]:
        request = _stdin_model(_DeferredRecoveryInput, stdin_json)
        recovery = DeferredRecoveryService(
            container.database,
            container.projects,
            container.capture,
            container.codex_adapter,
        )
        try:
            report = recovery.recover(
                source_record_id=request.source_record_id,
                target_project=request.target_project,
                apply=request.apply,
            )
        except DeferredRecoveryError as error:
            if error.code == "project_not_found":
                raise _CliFailure(
                    "project_not_found",
                    "Registered project was not found.",
                    1,
                ) from None
            raise _CliFailure("operation_failed", "Deferred recovery failed.", 1) from None
        return {
            "apply": request.apply,
            "capture_status": report.capture_status,
            "locator_count": report.locator_count,
            "recovered_locator_count": report.recovered_locator_count,
            "status": report.status,
        }

    _run(ctx, output_format, operation, redact_exceptions=True)


pending_app = typer.Typer(no_args_is_help=True)
app.add_typer(pending_app, name="pending", help="Audit or recover pending captures.")


@pending_app.command("recover")
def recover_pending_command(
    ctx: typer.Context,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Preview or apply trusted source mappings for pending captures."""

    def operation(container: ServiceContainer) -> dict[str, Any]:
        request = _stdin_model(_PendingRecoveryInput, stdin_json)
        recovery = PendingRecoveryService(
            container.database,
            container.projects,
            container.capture,
            container.codex_adapter,
        )
        try:
            report = recovery.recover(request.mappings, apply=request.apply)
        except PendingRecoveryError:
            raise _CliFailure("operation_failed", "Pending recovery failed.", 1) from None
        return {
            "apply": request.apply,
            "requested_count": report.requested_count,
            "source_count": report.source_count,
            "status": report.status,
            "verified_count": report.verified_count,
        }

    _run(ctx, output_format, operation, redact_exceptions=True)


@app.command()
def version() -> None:
    """Print the installed version without creating runtime state."""
    typer.echo(__version__)


@app.command("codex-context")
def codex_context_command(
    ctx: typer.Context,
    cwd: Path = typer.Option(..., "--cwd"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Resolve the exact active Codex namespace from local session metadata."""
    _validate_format(output_format)
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if not thread_id:
        _emit_error(output_format, "invalid_input", "Active Codex context is unavailable.")
        raise typer.Exit(4)
    try:
        namespace = CodexAdapter(
            Path.home() / ".codex" / "sessions",
            Redactor(),
        ).resolve_namespace(thread_id, cwd)
    except CodexContextUnavailable:
        _emit_error(
            output_format,
            "codex_context_unavailable",
            "Active Codex context is unavailable.",
        )
        raise typer.Exit(1) from None
    except OSError:
        _emit_error(output_format, "permission_denied", "Operation denied by local policy.")
        raise typer.Exit(2) from None
    except Exception:
        if _state(ctx).debug:
            raise
        _emit_error(output_format, "operation_failed", "Operation failed.")
        raise typer.Exit(1) from None
    response = {
        "namespace": namespace.model_dump(mode="json"),
        "source_record_id": thread_id,
        "status": "ok",
    }
    if output_format == "json":
        _emit("json", response)
    else:
        typer.echo(namespace.model_id)


@app.command("doctor")
def doctor_command(
    ctx: typer.Context,
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Inspect local health without creating or repairing runtime state."""
    _validate_format(output_format)
    state = _state(ctx)
    container: Any | None = None
    report = None
    try:
        container = build_doctor_container(state.config_path)
        report = container.doctor.run()
    except (
        PermissionError,
        SensitivePathError,
        NotADirectoryError,
        IsADirectoryError,
    ):
        _emit_error(
            output_format,
            "permission_denied",
            "Operation denied by local policy.",
        )
        raise typer.Exit(2) from None
    except Exception:
        if state.debug:
            raise
        _emit_error(output_format, "operation_failed", "Operation failed.")
        raise typer.Exit(1) from None
    finally:
        if container is not None:
            _close_container(
                container,
                pending_error=sys.exc_info()[0] is not None,
                state=state,
                output_format=output_format,
            )

    assert report is not None
    response = report.as_dict()
    if output_format == "json":
        _emit("json", response)
    else:
        typer.echo(_doctor_text(response))
    if report.status == "fail":
        raise typer.Exit(1)


@app.command("reconcile")
def reconcile_command(
    ctx: typer.Context,
    force: bool = typer.Option(False, "--force"),
    if_due: bool = typer.Option(False, "--if-due"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Run ordered local recovery and ingestion."""
    _validate_format(output_format)
    if force and if_due:
        _emit_error(output_format, "invalid_input", "Invalid command input.")
        raise typer.Exit(4)

    def operation(container: ServiceContainer) -> dict[str, Any]:
        report = container.reconcile.run(force=force or not if_due)
        if report.status == "failed":
            raise _CliFailure("operation_failed", "Reconcile failed.", 1)
        return report.model_dump(mode="json")

    _run(ctx, output_format, operation)


import_app = typer.Typer(no_args_is_help=True)
app.add_typer(import_app, name="import", help="Import a trusted local export.")


@import_app.command("chatgpt")
def import_chatgpt_command(
    ctx: typer.Context,
    path: Path,
    dry_run: bool = typer.Option(False, "--dry-run"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Import an official ChatGPT export ZIP."""

    _validate_format(output_format)
    state = _state(ctx)
    try:
        configured = configured_source_enabled(
            state.config_path,
            SourceAgent.CHATGPT,
        )
    except PermissionError:
        _emit_error(
            output_format,
            "permission_denied",
            "Operation denied by local policy.",
        )
        raise typer.Exit(2) from None
    except Exception:
        if state.debug:
            raise
        _emit_error(output_format, "operation_failed", "Operation failed.")
        raise typer.Exit(1) from None
    if configured is False:
        _emit_error(output_format, "source_disabled", "Source is disabled.")
        raise typer.Exit(2)

    def operation(container: ServiceContainer) -> dict[str, Any]:
        enabled = getattr(container, "source_enabled", None)
        if enabled is None:
            enabled = SourceAgent.CHATGPT in container.config.enabled_sources
        if not enabled:
            raise _CliFailure("source_disabled", "Source is disabled.", 2)
        try:
            report = container.chatgpt_adapter.import_zip(path, dry_run=dry_run)
        except ReconcileRequiredError:
            raise _CliFailure(
                "reconcile_required",
                "Run reconcile, then relink or disable unavailable projects.",
                2,
            ) from None
        except UnsafeArchiveError:
            raise _CliFailure("invalid_input", "Invalid archive input.", 4) from None
        return {
            "already_resolved_count": _safe_output_count(
                getattr(report, "already_resolved_count", 0)
            ),
            "confirmation_count": report.confirmation_count,
            "dry_run": report.dry_run,
            "duplicate_count": report.duplicate_count,
            "imported_count": report.imported_count,
            "resolved_count": _safe_output_count(getattr(report, "resolved_count", 0)),
            "status": "ok",
            "unmatched_resolution_count": _safe_output_count(
                getattr(report, "unmatched_resolution_count", 0)
            ),
            "warning_count": _chatgpt_warning_count(report),
        }

    _run(
        ctx,
        output_format,
        operation,
        builder=cast(
            Callable[[Path | None], ServiceContainer],
            build_readonly_chatgpt_container if dry_run else build_container,
        ),
    )


@app.command("compact")
def compact_command(
    ctx: typer.Context,
    project: UUID | None = typer.Option(None, "--project"),
    all_inactive: bool = typer.Option(False, "--all-inactive"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Compact inactive behavior into namespace-scoped retrospectives."""
    _validate_format(output_format)
    if (project is None) == (not all_inactive):
        _emit_error(output_format, "invalid_input", "Invalid command input.")
        raise typer.Exit(4)

    def operation(container: ServiceContainer) -> dict[str, Any]:
        try:
            if project is not None:
                summary = container.compaction.compact_project(
                    project,
                    dry_run=dry_run,
                )
            else:
                summary = container.compaction.compact_all_inactive(
                    dry_run=dry_run,
                )
        except KeyError:
            raise _CliFailure("project_not_found", "Registered project was not found.", 1) from None
        if getattr(summary, "failure_count", 0):
            raise _CliFailure("operation_failed", "Compaction failed.", 1)
        return {
            "cold_count": int(getattr(summary, "cold_count", 0)),
            "dry_run": dry_run,
            "namespace_count": int(getattr(summary, "namespace_count", 0)),
            "project_count": int(getattr(summary, "project_count", 0)),
            "remaining_count": int(getattr(summary, "remaining_count", 0)),
            "retrospective_count": int(getattr(summary, "retrospective_count", 0)),
            "source_count": int(getattr(summary, "source_count", 0)),
            "status": "ok",
        }

    _run(
        ctx,
        output_format,
        operation,
        builder=cast(
            Callable[[Path | None], ServiceContainer],
            build_readonly_compaction_container if dry_run else build_container,
        ),
    )


proposal_app = typer.Typer(no_args_is_help=True)
app.add_typer(
    proposal_app,
    name="proposal",
    help="Review and apply approval-gated local proposals.",
)


@proposal_app.command("list")
def proposal_list_command(
    ctx: typer.Context,
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """List safe proposal summaries without loading local approval state."""

    def operation(container: ServiceContainer) -> dict[str, Any]:
        proposals = tuple(
            {
                "created_at": summary.created_at.isoformat(),
                "origin": summary.origin,
                "proposal_id": str(summary.proposal_id),
                "risk": summary.risk,
                "status": summary.status,
                "title": summary.title,
            }
            for summary in container.proposal_service.list_summaries()
        )
        return {"proposals": proposals, "status": "ok"}

    _run(
        ctx,
        output_format,
        operation,
        builder=cast(
            Callable[[Path | None], ServiceContainer],
            build_readonly_proposal_container,
        ),
        text_renderer=_proposal_list_text,
    )


@proposal_app.command("create")
def proposal_create_command(
    ctx: typer.Context,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Create an explicit reviewed proposal from bounded JSON stdin."""
    request: _ProposalCreateInput | None = None

    def authorize_before_build(state: _CliState) -> None:
        nonlocal request
        if output_format == "json" and not stdin_json:
            raise _CliFailure("invalid_input", "JSON stdin is required.", 4)
        request = _proposal_create_request(stdin_json)
        if not stdin_json and request.token is not None:
            raise _CliFailure("invalid_input", "Invalid JSON input.", 4)
        _authorize_proposal_mutation(
            runtime_paths_for_config(state.config_path),
            token=request.token if stdin_json else None,
            stdin_json=stdin_json,
            yes=yes,
            action="create",
            proposal_id=None,
        )

    def operation(container: ServiceContainer) -> dict[str, Any]:
        if request is None:
            raise RuntimeError("proposal authorization missing")
        draft = ProposalDraft(
            signature=request.signature,
            title=request.title,
            description=request.description,
            risk=request.risk,
            patch=request.patch,
            verification_argv=request.verification_argv,
            target_version=request.target_version,
            origin="local_cli",
        )
        if dry_run:
            preview = container.proposal_service.preview_create(draft)
            duplicate = preview.duplicate
            return {
                "dry_run": True,
                "proposal_id": (str(duplicate.proposal_id) if duplicate is not None else None),
                "status": "duplicate_preview" if duplicate is not None else "draft",
                "unverified": preview.unverified,
                "verification": "complete" if preview.complete else "partial",
            }
        created = container.proposal_service.create(draft)
        return {
            "dry_run": False,
            "proposal_id": str(created.record.proposal_id),
            "status": created.record.status,
        }

    _run(
        ctx,
        output_format,
        operation,
        text_renderer=_proposal_text,
        builder=cast(
            Callable[[Path | None], ServiceContainer],
            build_readonly_proposal_container if dry_run else build_container,
        ),
        before_build=authorize_before_build,
    )


def _proposal_transition_command(
    ctx: typer.Context,
    proposal_id: UUID,
    *,
    action: Literal["approve", "reject", "apply", "rollback"],
    stdin_json: bool,
    dry_run: bool,
    yes: bool,
    output_format: str,
) -> None:
    def authorize_before_build(state: _CliState) -> None:
        if output_format == "json" and not stdin_json:
            raise _CliFailure("invalid_input", "JSON stdin is required.", 4)
        token: str | None = None
        if stdin_json:
            request = _stdin_model(_ProposalMutationInput, True)
            token = request.token
        _authorize_proposal_mutation(
            runtime_paths_for_config(state.config_path),
            token=token,
            stdin_json=stdin_json,
            yes=yes,
            action=action,
            proposal_id=proposal_id,
        )

    def operation(container: ServiceContainer) -> dict[str, Any]:
        if dry_run:
            preview = container.proposal_service.preview_action(
                proposal_id,
                action=action,
            )
            return {
                "dry_run": True,
                "proposal_id": str(proposal_id),
                "status": f"{preview.mode}_preview",
                "unverified": preview.unverified,
                "verification": "complete" if preview.complete else "partial",
            }
        if action == "approve":
            status = container.proposal_service.approve(
                proposal_id,
                actor="local-cli",
            ).status
        elif action == "reject":
            status = container.proposal_service.reject(proposal_id).status
        elif action == "apply":
            container.proposal_service.apply(proposal_id)
            status = "applied"
        else:
            status = container.proposal_service.rollback(proposal_id).status
        return {
            "dry_run": False,
            "proposal_id": str(proposal_id),
            "status": status,
        }

    _run(
        ctx,
        output_format,
        operation,
        text_renderer=_proposal_text,
        builder=cast(
            Callable[[Path | None], ServiceContainer],
            build_readonly_proposal_container if dry_run else build_container,
        ),
        before_build=authorize_before_build,
    )


@proposal_app.command("approve")
def proposal_approve_command(
    ctx: typer.Context,
    proposal_id: UUID,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Approve one proposal after a local confirmation boundary."""
    _proposal_transition_command(
        ctx,
        proposal_id,
        action="approve",
        stdin_json=stdin_json,
        dry_run=dry_run,
        yes=yes,
        output_format=output_format,
    )


@proposal_app.command("reject")
def proposal_reject_command(
    ctx: typer.Context,
    proposal_id: UUID,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Reject one proposal after a local confirmation boundary."""
    _proposal_transition_command(
        ctx,
        proposal_id,
        action="reject",
        stdin_json=stdin_json,
        dry_run=dry_run,
        yes=yes,
        output_format=output_format,
    )


@proposal_app.command("apply")
def proposal_apply_command(
    ctx: typer.Context,
    proposal_id: UUID,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Apply one approved proposal in an isolated Git worktree."""
    _proposal_transition_command(
        ctx,
        proposal_id,
        action="apply",
        stdin_json=stdin_json,
        dry_run=dry_run,
        yes=yes,
        output_format=output_format,
    )


@proposal_app.command("rollback")
def proposal_rollback_command(
    ctx: typer.Context,
    proposal_id: UUID,
    stdin_json: bool = typer.Option(False, "--stdin-json"),
    dry_run: bool = typer.Option(False, "--dry-run"),
    yes: bool = typer.Option(False, "--yes"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Mark an applied proposal rolled back after exact-ref verification."""
    _proposal_transition_command(
        ctx,
        proposal_id,
        action="rollback",
        stdin_json=stdin_json,
        dry_run=dry_run,
        yes=yes,
        output_format=output_format,
    )


integrate_app = typer.Typer(no_args_is_help=True)
agents_app = typer.Typer(no_args_is_help=True)
app.add_typer(
    integrate_app,
    name="integrate",
    help="Manage explicit local integrations.",
)
integrate_app.add_typer(
    agents_app,
    name="agents",
    help="Manage the bounded Codex AGENTS guidance block.",
)


@agents_app.command("install")
def agents_install_command(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Install or refresh only the managed Project Memory Hub block."""
    _run_agents_integration(
        ctx,
        action="install",
        dry_run=dry_run,
        output_format=output_format,
    )


@agents_app.command("remove")
def agents_remove_command(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Remove only the managed Project Memory Hub block."""
    _run_agents_integration(
        ctx,
        action="remove",
        dry_run=dry_run,
        output_format=output_format,
    )


@app.command("serve")
def serve_command(
    ctx: typer.Context,
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Loopback bind host only; public and wildcard binds are rejected.",
    ),
    port: int = typer.Option(8765, "--port", help="Local control-panel port."),
) -> None:
    """Serve the loopback-only local control panel."""
    try:
        selected_host = loopback_bind_host(host)
        if not 1 <= port <= 65_535:
            raise ValueError("serve port must be valid")
    except (TypeError, ValueError):
        _emit_error("text", "invalid_input", "Invalid command input.")
        raise typer.Exit(4) from None

    state = _state(ctx)
    container: ServiceContainer | None = None
    try:
        container = build_container(state.config_path)
        uvicorn.run(
            create_app(container),
            host=selected_host,
            port=port,
            access_log=False,
            proxy_headers=False,
        )
    except (
        PermissionError,
        SensitivePathError,
        NotADirectoryError,
        IsADirectoryError,
    ):
        _emit_error("text", "permission_denied", "Operation denied by local policy.")
        raise typer.Exit(2) from None
    except Exception:
        if state.debug:
            raise
        _emit_error("text", "operation_failed", "Operation failed.")
        raise typer.Exit(1) from None
    finally:
        if container is not None:
            _close_container(
                container,
                pending_error=sys.exc_info()[0] is not None,
                state=state,
                output_format="text",
            )


def _run_agents_integration(
    ctx: typer.Context,
    *,
    action: Literal["install", "remove"],
    dry_run: bool,
    output_format: str,
) -> None:
    _validate_format(output_format)
    state = _state(ctx)
    try:
        launcher = InstallationIdentity.discover_launcher()
        if launcher is None:
            raise _CliFailure(
                "not_available",
                "Stable installation is not available.",
                1,
            )
        integration = AgentsIntegration(launcher)
        target = Path.home() / ".codex" / "AGENTS.md"
        change = (
            integration.install(target, dry_run=dry_run)
            if action == "install"
            else integration.remove(target, dry_run=dry_run)
        )
        response = _agents_change_response(
            action=action,
            dry_run=dry_run,
            change=change,
        )
    except _CliFailure as error:
        _emit_error(output_format, error.code, error.message)
        raise typer.Exit(error.exit_code) from None
    except (
        AgentsIntegrationError,
        PermissionError,
        SensitivePathError,
        NotADirectoryError,
        IsADirectoryError,
        OSError,
    ):
        _emit_error(
            output_format,
            "permission_denied",
            "Operation denied by local policy.",
        )
        raise typer.Exit(2) from None
    except Exception:
        if state.debug:
            raise
        _emit_error(output_format, "operation_failed", "Operation failed.")
        raise typer.Exit(1) from None

    if output_format == "json":
        _emit("json", response)
    else:
        typer.echo(_agents_change_text(response))


def _agents_change_response(
    *,
    action: Literal["install", "remove"],
    dry_run: bool,
    change: FileChange,
) -> dict[str, Any]:
    if not change.changed:
        status = "unchanged"
        diff_change = "none"
    elif dry_run:
        status = f"would_{action}"
        diff_change = "add_or_update" if action == "install" else "remove"
    else:
        status = "installed" if action == "install" else "removed"
        diff_change = "add_or_update" if action == "install" else "remove"
    return {
        "backup": {"created": change.backup_path is not None},
        "changed": change.changed,
        "diff": {
            "change": diff_change,
            "operation": action,
            "scope": "managed_agents_block",
        },
        "dry_run": dry_run,
        "status": status,
    }


def _agents_change_text(response: dict[str, Any]) -> str:
    raw_diff = response["diff"]
    raw_backup = response["backup"]
    assert isinstance(raw_diff, dict)
    assert isinstance(raw_backup, dict)
    return (
        f"{response['status']}; changed={str(response['changed']).lower()}; "
        f"dry_run={str(response['dry_run']).lower()}; "
        f"diff={raw_diff['scope']}:{raw_diff['change']}; "
        f"backup_created={str(raw_backup['created']).lower()}"
    )


def _doctor_text(response: dict[str, Any]) -> str:
    lines = [f"status: {response.get('status', 'fail')}"]
    checks = response.get("checks")
    if not isinstance(checks, list):
        return "status: fail"
    for check in checks:
        if not isinstance(check, dict):
            continue
        lines.append(
            f"{check.get('name', 'unknown')}: {check.get('status', 'fail')} "
            f"[{check.get('code', 'check_failed')}] "
            f"{check.get('remediation', 'Review the local installation.')}"
        )
    return "\n".join(lines)


def _authorize_proposal_mutation(
    paths: RuntimePaths,
    *,
    token: str | None,
    stdin_json: bool,
    yes: bool,
    action: str,
    proposal_id: UUID | None,
) -> None:
    if stdin_json:
        try:
            expected = LocalAccessToken.load_existing(paths)
        except OSError:
            raise _CliFailure(
                "permission_denied",
                "Operation denied by local policy.",
                2,
            ) from None
        if token is None or not LocalAccessToken.matches(expected, token):
            raise _CliFailure(
                "permission_denied",
                "Operation denied by local policy.",
                2,
            )
        return
    if not getattr(sys.stdin, "isatty", lambda: False)():
        raise _CliFailure(
            "permission_denied",
            "Operation denied by local policy.",
            2,
        )
    if yes:
        supplied = token
        if supplied is None:
            supplied = typer.prompt("Local access token", hide_input=True)
        try:
            expected = LocalAccessToken.load_existing(paths)
        except OSError:
            raise _CliFailure(
                "permission_denied",
                "Operation denied by local policy.",
                2,
            ) from None
        if not LocalAccessToken.matches(expected, supplied):
            raise _CliFailure(
                "permission_denied",
                "Operation denied by local policy.",
                2,
            )
        return
    target = f" proposal {proposal_id}" if proposal_id is not None else " proposal"
    if not typer.confirm(f"Confirm {action}{target}?"):
        raise _CliFailure(
            "permission_denied",
            "Operation denied by local policy.",
            2,
        )


def _proposal_text(response: dict[str, Any]) -> str:
    proposal_id = response.get("proposal_id")
    status = str(response.get("status", "ok"))
    rendered = f"proposal {proposal_id}: {status}" if proposal_id else status
    if response.get("dry_run") is not True:
        return rendered
    verification = str(response.get("verification", "partial"))
    raw_unverified = response.get("unverified")
    unverified = (
        ",".join(str(item) for item in raw_unverified)
        if isinstance(raw_unverified, (list, tuple))
        else "unknown"
    )
    return f"{rendered}; verification={verification}; unverified={unverified}"


def _proposal_list_text(response: dict[str, Any]) -> str:
    proposals = response.get("proposals")
    if not isinstance(proposals, (list, tuple)) or not proposals:
        return "No proposals."
    lines: list[str] = []
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        lines.append(
            " ".join(
                (
                    str(proposal.get("proposal_id", "")),
                    f"[{proposal.get('status', '')}/{proposal.get('risk', '')}]",
                    str(proposal.get("title", "")),
                )
            ).strip()
        )
    return "\n".join(lines) if lines else "No proposals."


def _run(
    ctx: typer.Context,
    output_format: str,
    operation: Callable[[_ContainerT], dict[str, Any]],
    *,
    allowed_formats: frozenset[str] = _FORMATS,
    text_renderer: Callable[[dict[str, Any]], str] | None = None,
    builder: Callable[[Path | None], _ContainerT] | None = None,
    before_build: Callable[[_CliState], None] | None = None,
    redact_exceptions: bool = False,
) -> None:
    _validate_format(output_format, allowed_formats)
    state = _state(ctx)
    container: _ContainerT | None = None
    rendered: str | None = None
    try:
        if before_build is not None:
            before_build(state)
        container = (
            cast(_ContainerT, build_container(state.config_path))
            if builder is None
            else builder(state.config_path)
        )
        response = operation(container)
        try:
            rendered = _render_response(output_format, response, text_renderer)
        except (TypeError, ValueError, UnicodeError):
            raise _CliFailure("operation_failed", "Operation failed.", 1) from None
    except _CliFailure as error:
        _emit_error(output_format, error.code, error.message)
        raise typer.Exit(error.exit_code) from None
    except SchemaUpgradeRequiredError:
        _emit_error(
            output_format,
            "reconcile_required",
            "Reconciliation is required before this operation.",
        )
        raise typer.Exit(2) from None
    except (
        PermissionError,
        SensitivePathError,
        NotADirectoryError,
        IsADirectoryError,
    ):
        _emit_error(
            output_format,
            "permission_denied",
            "Operation denied by local policy.",
        )
        raise typer.Exit(2) from None
    except (InvalidUtf8Text, ValidationError, json.JSONDecodeError, UnicodeDecodeError):
        _emit_error(output_format, "invalid_input", "Invalid JSON input.")
        raise typer.Exit(4) from None
    except ProposalError:
        _emit_error(output_format, "invalid_input", "Invalid proposal input.")
        raise typer.Exit(4) from None
    except Exception:
        if state.debug and not redact_exceptions:
            raise
        _emit_error(output_format, "operation_failed", "Operation failed.")
        raise typer.Exit(1) from None
    finally:
        if container is not None:
            _close_container(
                container,
                pending_error=sys.exc_info()[0] is not None,
                state=state,
                output_format=output_format,
                redact_exceptions=redact_exceptions,
            )
    assert rendered is not None
    try:
        typer.echo(rendered)
    except (TypeError, ValueError, UnicodeError):
        try:
            _emit_error(output_format, "operation_failed", "Operation failed.")
        except (TypeError, ValueError, UnicodeError):
            pass
        raise typer.Exit(1) from None


def _render_response(
    output_format: str,
    response: dict[str, Any],
    text_renderer: Callable[[dict[str, Any]], str] | None,
) -> str:
    if output_format == "json":
        return json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if text_renderer is not None:
        return text_renderer(response)
    return str(response.get("status", "ok"))


def _is_transient_database_error(error: sqlite3.OperationalError) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    if type(error_code) is not int:
        return False
    base_code = error_code & 0xFF
    return base_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _safe_output_count(value: object) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, _MAX_OUTPUT_COUNT)


def _chatgpt_warning_count(report: object) -> int:
    missing = object()
    explicit = getattr(report, "warning_count", missing)
    if explicit is not missing:
        return _safe_output_count(explicit)
    warnings = getattr(report, "warnings", ())
    if isinstance(warnings, (str, bytes, bytearray)):
        return 0
    try:
        return _safe_output_count(len(warnings))
    except (TypeError, ValueError, OverflowError):
        return 0


def _capture_with_transient_retry(
    container: ServiceContainer,
    payload: CapturePayload,
) -> CaptureResult:
    try:
        return container.capture.capture(payload)
    except sqlite3.OperationalError as error:
        if _is_transient_database_error(error):
            try:
                container.retry_queue.enqueue(payload, "operational_failure")
            except Exception:
                pass
        raise


def _require_live_codex_namespace(request: RecallRequest) -> None:
    thread_id = os.environ.get("CODEX_THREAD_ID", "")
    if request.namespace.source_agent is not SourceAgent.CODEX or not thread_id:
        raise _CliFailure(
            "codex_context_unavailable",
            "Active Codex context is unavailable.",
            1,
        )
    try:
        resolved = CodexAdapter(
            Path.home() / ".codex" / "sessions",
            Redactor(),
        ).resolve_namespace(thread_id, request.cwd)
    except CodexContextUnavailable:
        raise _CliFailure(
            "codex_context_unavailable",
            "Active Codex context is unavailable.",
            1,
        ) from None
    except OSError:
        raise _CliFailure(
            "permission_denied",
            "Operation denied by local policy.",
            2,
        ) from None
    if resolved != request.namespace:
        raise _CliFailure(
            "codex_context_unavailable",
            "Active Codex context is unavailable.",
            1,
        )


def _authorize_manual_recall(paths: RuntimePaths, token: SecretStr) -> None:
    try:
        expected = LocalAccessToken.load_existing(paths)
    except OSError:
        raise _CliFailure(
            "permission_denied",
            "Operation denied by local policy.",
            2,
        ) from None
    if not LocalAccessToken.matches(expected, token.get_secret_value()):
        raise _CliFailure(
            "permission_denied",
            "Operation denied by local policy.",
            2,
        )


def _stdin_model(model: type[_ModelT], enabled: bool) -> _ModelT:
    if not enabled:
        raise _CliFailure("invalid_input", "JSON stdin is required.", 4)
    data = _read_stdin_bytes()
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4) from error
    if not isinstance(value, dict):
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4)
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4) from error


def _proposal_create_request(stdin_json: bool) -> _ProposalCreateInput:
    if stdin_json:
        return _stdin_model(_ProposalCreateInput, True)
    if not getattr(sys.stdin, "isatty", lambda: False)():
        return _stdin_model(_ProposalCreateInput, False)
    return _model_from_json_bytes(_ProposalCreateInput, _read_stdin_line_bytes())


def _model_from_json_bytes(model: type[_ModelT], data: bytes) -> _ModelT:
    try:
        value = json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4) from error
    if not isinstance(value, dict):
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4)
    try:
        return model.model_validate(value)
    except ValidationError as error:
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4) from error


def _read_stdin_bytes() -> bytes:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        text = sys.stdin.read(_MAX_STDIN_BYTES + 1)
        data = text.encode("utf-8")
    else:
        data = buffer.read(_MAX_STDIN_BYTES + 1)
    if not data or len(data) > _MAX_STDIN_BYTES:
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4)
    return data


def _read_stdin_line_bytes() -> bytes:
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        text = sys.stdin.readline(_MAX_STDIN_BYTES + 2)
        data = text.encode("utf-8")
    else:
        data = buffer.readline(_MAX_STDIN_BYTES + 2)
    if data.endswith(b"\n"):
        data = data[:-1]
    if data.endswith(b"\r"):
        data = data[:-1]
    if not data or len(data) > _MAX_STDIN_BYTES:
        raise _CliFailure("invalid_input", "Invalid JSON input.", 4)
    return data


def _close_container(
    container: Any,
    *,
    pending_error: bool,
    state: _CliState,
    output_format: str,
    redact_exceptions: bool = False,
) -> None:
    try:
        container.close()
    except BaseException as error:
        if pending_error:
            return
        if not isinstance(error, Exception):
            raise
        if state.debug and not redact_exceptions:
            raise
        _emit_error(output_format, "operation_failed", "Operation failed.")
        raise typer.Exit(1) from None


def _state(ctx: typer.Context) -> _CliState:
    value = ctx.find_root().obj
    assert isinstance(value, _CliState)
    return value


def _validate_format(output_format: str, allowed_formats: frozenset[str] = _FORMATS) -> None:
    if output_format not in allowed_formats:
        _emit_error("json", "invalid_input", "Invalid output format.")
        raise typer.Exit(4)


def _emit(output_format: str, value: dict[str, Any]) -> None:
    if output_format == "json":
        typer.echo(
            json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return
    typer.echo(value.get("status", "ok"))


def _emit_error(output_format: str, code: str, message: str) -> None:
    payload = {"error": {"code": code, "message": message}, "status": "error"}
    if output_format == "json":
        _emit("json", payload)
    else:
        typer.echo(_render_text_error(code))


def _render_text_error(code: str) -> str:
    rendered_code = code if code in _TEXT_ERROR_COPY else _TEXT_ERROR_FALLBACK_CODE
    message, hint = _TEXT_ERROR_COPY[rendered_code]
    return f"error: {rendered_code}\nmessage: {message}\nhint: {hint}"


def _emit_parse_error(args: Sequence[str] | None) -> None:
    values = tuple(args or ())
    json_requested = "--format=json" in values or any(
        value == "--format" and index + 1 < len(values) and values[index + 1] == "json"
        for index, value in enumerate(values)
    )
    if json_requested:
        _emit_error("json", "invalid_input", "Invalid command input.")
    else:
        typer.echo(_render_text_error("invalid_input"), err=True)
