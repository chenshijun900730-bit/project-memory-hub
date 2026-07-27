import json
import stat
from pathlib import Path

from typer.testing import CliRunner

import project_memory_hub.services.setup as setup_service_module
from project_memory_hub.cli import app
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import SourceAgent


runner = CliRunner()


def _incomplete_config(tmp_path: Path) -> Path:
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
    with build_container(config_path):
        pass
    return config_path


def _runtime_tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries = (root, *sorted(root.rglob("*")))
    return tuple(
        (
            path.relative_to(root).as_posix() if path != root else ".",
            path.lstat().st_mode,
            path.lstat().st_ino,
            path.lstat().st_size,
            path.lstat().st_mtime_ns,
            path.read_bytes() if stat.S_ISREG(path.lstat().st_mode) else None,
        )
        for path in entries
    )


def test_setup_without_options_reports_safe_status_without_rewriting_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _incomplete_config(tmp_path)
    runtime = config_path.parent
    before = _runtime_tree_snapshot(runtime)
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    result = runner.invoke(
        app,
        ["--config", str(config_path), "setup", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "automation_status": "unavailable",
        "behavior_count": 0,
        "daily_reconcile_time": "03:30",
        "enabled_sources": ["codex", "chatgpt"],
        "fact_count": 0,
        "next_step": "configure",
        "project_count": 0,
        "root_count": 1,
        "setup_completed": False,
        "setup_status": "inspected",
        "status": "ok",
        "valid_root_count": 1,
    }
    assert str(tmp_path) not in result.stdout
    assert _runtime_tree_snapshot(runtime) == before


def test_setup_applies_explicit_local_options_and_completes(
    tmp_path: Path,
    monkeypatch,
) -> None:
    config_path = _incomplete_config(tmp_path)
    selected_root = tmp_path / "selected-projects"
    selected_root.mkdir()
    monkeypatch.setattr(
        setup_service_module.InstallationIdentity,
        "discover",
        staticmethod(lambda: None),
    )

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "setup",
            "--project-root",
            str(selected_root),
            "--source",
            "codex",
            "--source",
            "chatgpt",
            "--inactive-days",
            "30",
            "--max-recall-tokens",
            "700",
            "--daily-reconcile-time",
            "04:15",
            "--complete",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["setup_status"] == "completed"
    assert payload["setup_completed"] is True
    assert payload["automation_status"] == "unavailable"
    assert str(tmp_path) not in result.stdout
    persisted = ConfigManager(config_path).load()
    assert persisted.project_roots == (selected_root,)
    assert persisted.enabled_sources == (SourceAgent.CODEX, SourceAgent.CHATGPT)
    assert persisted.inactive_days == 30
    assert persisted.max_recall_tokens == 700
    assert persisted.daily_reconcile_time == "04:15"
    assert persisted.setup_completed is True


def test_setup_rejects_optional_or_duplicate_sources_without_writing(
    tmp_path: Path,
) -> None:
    config_path = _incomplete_config(tmp_path)
    before = config_path.read_bytes()

    for sources in (("trae",), ("codex", "codex")):
        arguments = ["--config", str(config_path), "setup"]
        for source in sources:
            arguments.extend(("--source", source))
        arguments.extend(("--format", "json"))
        result = runner.invoke(app, arguments)

        assert result.exit_code == 4
        assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
        assert config_path.read_bytes() == before
