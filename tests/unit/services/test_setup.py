from dataclasses import replace
from pathlib import Path

import pytest

from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import SourceAgent
from project_memory_hub.services.control import ControlInputError
from project_memory_hub.services.setup import SetupRequest, SetupService, _next_step


def _save_incomplete_config(tmp_path: Path) -> Path:
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
    return config_path


def test_setup_inspect_returns_a_safe_resumable_snapshot(tmp_path: Path) -> None:
    config_path = _save_incomplete_config(tmp_path)

    with build_container(config_path) as container:
        snapshot = SetupService(
            container,
            automation_status=lambda _config: "authorization_required",
        ).inspect()

    assert snapshot.setup_completed is False
    assert snapshot.project_roots == (str(tmp_path / "projects"),)
    assert snapshot.valid_root_count == 1
    assert snapshot.enabled_sources == ("codex", "chatgpt")
    assert snapshot.daily_reconcile_time == "03:30"
    assert snapshot.project_count == 0
    assert snapshot.fact_count == 0
    assert snapshot.behavior_count == 0
    assert snapshot.automation_status == "authorization_required"
    assert snapshot.next_step == "configure"


def test_setup_apply_saves_configuration_and_completion_atomically(
    tmp_path: Path,
) -> None:
    config_path = _save_incomplete_config(tmp_path)
    project_root = tmp_path / "selected-projects"
    project_root.mkdir()

    with build_container(config_path) as container:
        service = SetupService(
            container,
            automation_status=lambda _config: "authorization_required",
        )
        result = service.apply_local(
            SetupRequest(
                project_roots=(str(project_root),),
                enabled_sources=("codex", "chatgpt"),
                inactive_days="30",
                max_recall_tokens="700",
                daily_reconcile_time="04:15",
                complete=True,
            )
        )

    persisted = ConfigManager(config_path).load()
    assert result.status == "completed"
    assert result.changed is True
    assert result.snapshot.setup_completed is True
    assert persisted.project_roots == (project_root,)
    assert persisted.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)
    assert persisted.inactive_days == 30
    assert persisted.max_recall_tokens == 700
    assert persisted.daily_reconcile_time == "04:15"
    assert persisted.setup_completed is True


def test_setup_complete_resumes_from_the_persisted_configuration(tmp_path: Path) -> None:
    config_path = _save_incomplete_config(tmp_path)

    with build_container(config_path) as container:
        service = SetupService(
            container,
            automation_status=lambda _config: "authorization_required",
        )
        first = service.complete()
        before_repeat = config_path.stat()
        repeated = service.complete()
        after_repeat = config_path.stat()

    assert first.status == "completed"
    assert first.snapshot.setup_completed is True
    assert repeated.status == "unchanged"
    assert (after_repeat.st_ino, after_repeat.st_mtime_ns) == (
        before_repeat.st_ino,
        before_repeat.st_mtime_ns,
    )


def test_setup_complete_rejects_a_concurrent_settings_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _save_incomplete_config(tmp_path)
    real_load = ConfigManager.load
    real_load_with_revision = ConfigManager.load_with_revision
    injected = False

    def inject_concurrent_change(config: AppConfig) -> None:
        nonlocal injected
        if injected:
            return
        injected = True
        ConfigManager(config_path).save(
            replace(
                config,
                inactive_days=45,
                setup_completed=True,
            )
        )

    def racing_load(manager: ConfigManager) -> AppConfig:
        config = real_load(manager)
        if manager.path == config_path:
            inject_concurrent_change(config)
        return config

    def racing_load_with_revision(manager: ConfigManager):
        config, revision = real_load_with_revision(manager)
        if manager.path == config_path:
            inject_concurrent_change(config)
        return config, revision

    with build_container(config_path) as container:
        monkeypatch.setattr(ConfigManager, "load", racing_load)
        monkeypatch.setattr(ConfigManager, "load_with_revision", racing_load_with_revision)
        service = SetupService(
            container,
            automation_status=lambda _config: "authorization_required",
        )
        with pytest.raises(ControlInputError, match="configuration changed"):
            service.complete()

    persisted = real_load(ConfigManager(config_path))
    assert persisted.inactive_days == 45
    assert persisted.setup_completed is True


def test_setup_change_after_completion_reports_configured(tmp_path: Path) -> None:
    config_path = _save_incomplete_config(tmp_path)
    manager = ConfigManager(config_path)
    manager.save(replace(manager.load(), setup_completed=True))

    with build_container(config_path) as container:
        result = SetupService(
            container,
            automation_status=lambda _config: "authorization_required",
        ).apply_local(SetupRequest(inactive_days="30"))

    assert result.status == "configured"
    assert result.snapshot.setup_completed is True
    assert manager.load().inactive_days == 30


@pytest.mark.parametrize(
    ("setup_completed", "project_count", "fact_count", "automation_status", "expected"),
    [
        (False, 0, 0, "unavailable", "configure"),
        (True, 0, 0, "current", "discover"),
        (True, 1, 0, "current", "first_memory"),
        (True, 1, 1, "authorization_required", "authorize_automation"),
        (True, 1, 1, "drifted", "authorize_automation"),
        (True, 1, 1, "unavailable", "authorize_automation"),
        (True, 1, 1, "current", "ready"),
    ],
)
def test_setup_next_step_state_machine(
    setup_completed: bool,
    project_count: int,
    fact_count: int,
    automation_status: str,
    expected: str,
) -> None:
    assert (
        _next_step(
            setup_completed=setup_completed,
            project_count=project_count,
            fact_count=fact_count,
            automation_status=automation_status,  # type: ignore[arg-type]
        )
        == expected
    )
