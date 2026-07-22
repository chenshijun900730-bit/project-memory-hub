from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from project_memory_hub.config import AppConfig, ConfigRevision
from project_memory_hub.integration.automation import (
    AutomationInspector,
    DesiredAutomation,
    InstallationIdentity,
)
from project_memory_hub.services.control import ControlInputError, ControlPanelService

if TYPE_CHECKING:
    from project_memory_hub.container import ServiceContainer


SetupAutomationStatus = Literal[
    "current",
    "authorization_required",
    "drifted",
    "unavailable",
]
SetupNextStep = Literal[
    "configure",
    "discover",
    "first_memory",
    "authorize_automation",
    "ready",
]
SetupResultStatus = Literal["configured", "completed", "unchanged"]


@dataclass(frozen=True, slots=True)
class SetupRequest:
    project_roots: tuple[str, ...] | None = None
    enabled_sources: tuple[str, ...] | None = None
    inactive_days: str | None = None
    max_recall_tokens: str | None = None
    daily_reconcile_time: str | None = None
    complete: bool = False
    expected_revision: str | None = None


@dataclass(frozen=True, slots=True)
class SetupSnapshot:
    setup_completed: bool
    project_roots: tuple[str, ...]
    valid_root_count: int
    enabled_sources: tuple[str, ...]
    inactive_days: int
    max_recall_tokens: int
    daily_reconcile_time: str
    project_count: int
    fact_count: int
    behavior_count: int
    automation_status: SetupAutomationStatus
    next_step: SetupNextStep
    revision: str


@dataclass(frozen=True, slots=True)
class SetupResult:
    status: SetupResultStatus
    changed: bool
    snapshot: SetupSnapshot


class SetupService:
    def __init__(
        self,
        container: ServiceContainer,
        *,
        automation_status: Callable[[AppConfig], SetupAutomationStatus] | None = None,
    ) -> None:
        self._container = container
        self._automation_status = automation_status or _inspect_automation

    def inspect(self) -> SetupSnapshot:
        config, revision = self._container.config_manager.load_with_revision()
        with self._container.database.connect(readonly=True) as connection:
            project_count = _count(connection, "projects")
            fact_count = _count(connection, "project_facts")
            behavior_count = _count(connection, "behavior_memories")
        automation_status = self._automation_status(config)
        return SetupSnapshot(
            setup_completed=config.setup_completed,
            project_roots=tuple(str(path) for path in config.project_roots),
            valid_root_count=sum(_root_is_available(path) for path in config.project_roots),
            enabled_sources=tuple(source.value for source in config.enabled_sources),
            inactive_days=config.inactive_days,
            max_recall_tokens=config.max_recall_tokens,
            daily_reconcile_time=config.daily_reconcile_time,
            project_count=project_count,
            fact_count=fact_count,
            behavior_count=behavior_count,
            automation_status=automation_status,
            next_step=_next_step(
                setup_completed=config.setup_completed,
                project_count=project_count,
                fact_count=fact_count,
                automation_status=automation_status,
            ),
            revision=revision.digest,
        )

    def apply_local(self, request: SetupRequest) -> SetupResult:
        current, revision = self._container.config_manager.load_with_revision()
        expected_revision = revision
        if request.expected_revision is not None:
            if re.fullmatch(r"[0-9a-f]{64}", request.expected_revision) is None:
                raise ControlInputError("configuration changed")
            expected_revision = ConfigRevision(request.expected_revision)
        updated = ControlPanelService(self._container).save_settings(
            project_roots=(
                [str(path) for path in current.project_roots]
                if request.project_roots is None
                else list(request.project_roots)
            ),
            enabled_sources=(
                [source.value for source in current.enabled_sources]
                if request.enabled_sources is None
                else list(request.enabled_sources)
            ),
            inactive_days=(
                str(current.inactive_days)
                if request.inactive_days is None
                else request.inactive_days
            ),
            max_recall_tokens=(
                str(current.max_recall_tokens)
                if request.max_recall_tokens is None
                else request.max_recall_tokens
            ),
            daily_reconcile_time=(
                current.daily_reconcile_time
                if request.daily_reconcile_time is None
                else request.daily_reconcile_time
            ),
            setup_completed=True if request.complete else None,
            expected_revision=expected_revision,
        )
        changed = current != updated
        if not changed:
            status: SetupResultStatus = "unchanged"
        elif request.complete and not current.setup_completed and updated.setup_completed:
            status = "completed"
        else:
            status = "configured"
        return SetupResult(
            status=status,
            changed=changed,
            snapshot=self.inspect(),
        )

    def complete(self) -> SetupResult:
        return self.apply_local(SetupRequest(complete=True))


def _root_is_available(path: Path) -> bool:
    try:
        return path.is_absolute() and path.is_dir() and not path.is_symlink()
    except OSError:
        return False


def _count(connection: object, table: str) -> int:
    if table not in {"projects", "project_facts", "behavior_memories"}:
        raise ValueError("unsupported count")
    row = connection.execute(f"select count(*) from {table}").fetchone()  # type: ignore[attr-defined]
    return int(row[0])


def _next_step(
    *,
    setup_completed: bool,
    project_count: int,
    fact_count: int,
    automation_status: SetupAutomationStatus,
) -> SetupNextStep:
    if not setup_completed:
        return "configure"
    if project_count == 0:
        return "discover"
    if fact_count == 0:
        return "first_memory"
    if automation_status != "current":
        return "authorize_automation"
    return "ready"


def _inspect_automation(config: AppConfig) -> SetupAutomationStatus:
    try:
        identity = InstallationIdentity.discover()
        if identity is None:
            return "unavailable"
        desired = DesiredAutomation.daily_reconcile(
            local_time=config.daily_reconcile_time,
            repository_root=identity.repository_root,
            launcher=identity.launcher,
            project_id=config.codex_project_id,
        )
        inspection = AutomationInspector(Path.home() / ".codex" / "automations").inspect(desired)
    except Exception:
        return "unavailable"
    if inspection.status == "current":
        return "current"
    if inspection.status in {"missing", "disabled"}:
        return "authorization_required"
    return "drifted"
