from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import project_memory_hub.services.control as control_module
from project_memory_hub.config import AppConfig
from project_memory_hub.domain import SourceAgent
from project_memory_hub.services.control import ControlPanelService


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        project_roots=(tmp_path,),
        enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
        inactive_days=21,
        max_recall_tokens=800,
        daily_reconcile_time="04:15",
    )


def test_control_panel_reports_the_inspected_automation_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository_root = tmp_path / "repository"
    repository_root.mkdir()
    launcher = tmp_path / "memory-hub"
    launcher.write_bytes(b"#!/bin/sh\n")
    launcher.chmod(0o700)
    config = _config(tmp_path)
    captured: dict[str, object] = {}

    class Identity:
        @classmethod
        def discover(cls):
            return SimpleNamespace(
                launcher=launcher,
                repository_root=repository_root,
            )

    class Inspector:
        def __init__(self, root: Path) -> None:
            captured["root"] = root

        def inspect(self, desired):
            captured["desired"] = desired
            return SimpleNamespace(status="current")

    monkeypatch.setattr(control_module, "InstallationIdentity", Identity)
    monkeypatch.setattr(control_module, "AutomationInspector", Inspector)
    container = SimpleNamespace(
        config_manager=SimpleNamespace(load=lambda: config),
    )

    status = ControlPanelService(container).automation_status()

    assert status == "current"
    assert captured["root"] == Path.home() / ".codex" / "automations"
    desired = captured["desired"]
    assert desired.local_time == "04:15"
    assert desired.repository_root == repository_root
    assert desired.launcher == launcher


def test_control_panel_fails_closed_without_a_stable_installation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Identity:
        @classmethod
        def discover(cls):
            return None

    monkeypatch.setattr(control_module, "InstallationIdentity", Identity)
    container = SimpleNamespace(
        config_manager=SimpleNamespace(load=lambda: _config(tmp_path)),
    )

    assert ControlPanelService(container).automation_status() == "drifted"
