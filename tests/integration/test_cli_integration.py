from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
from project_memory_hub.cli import app
from project_memory_hub.integration.agents import MANAGED_END, MANAGED_START


runner = CliRunner()


def _safe_launcher(root: Path) -> Path:
    launcher = root / "bin" / "memory-hub"
    launcher.parent.mkdir()
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    return launcher


def _set_installation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    home: Path,
    launcher: Path | None,
) -> None:
    codex_home = home / ".codex"
    codex_home.mkdir(mode=0o700)
    repository_root = home / "project-memory-hub"
    repository_root.mkdir()

    class FakeInstallationIdentity:
        @classmethod
        def discover_launcher(cls):
            return launcher

        @classmethod
        def discover(cls):
            if launcher is None:
                return None
            return SimpleNamespace(
                launcher=launcher,
                repository_root=repository_root,
            )

    monkeypatch.setattr(cli_module.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        cli_module,
        "InstallationIdentity",
        FakeInstallationIdentity,
    )


def _invoke(*arguments: str):
    return runner.invoke(app, ["integrate", "agents", *arguments])


def test_agents_install_remove_and_idempotence_preserve_existing_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    launcher = _safe_launcher(tmp_path)
    _set_installation(monkeypatch, home=home, launcher=launcher)
    target = home / ".codex" / "AGENTS.md"
    original = b"# Existing rules\nkeep-this-private-rule\n"
    target.write_bytes(original)
    target.chmod(0o600)

    installed = _invoke("install", "--format", "json")
    repeated = _invoke("install", "--format", "json")
    removed = _invoke("remove", "--format", "json")
    repeated_remove = _invoke("remove", "--format", "json")

    assert (
        installed.exit_code
        == repeated.exit_code
        == removed.exit_code
        == repeated_remove.exit_code
        == 0
    )
    installed_payload = json.loads(installed.stdout)
    assert installed_payload == {
        "backup": {"created": True},
        "changed": True,
        "diff": {
            "change": "add_or_update",
            "operation": "install",
            "scope": "managed_agents_block",
        },
        "dry_run": False,
        "status": "installed",
    }
    assert json.loads(repeated.stdout) == {
        "backup": {"created": False},
        "changed": False,
        "diff": {
            "change": "none",
            "operation": "install",
            "scope": "managed_agents_block",
        },
        "dry_run": False,
        "status": "unchanged",
    }
    assert json.loads(removed.stdout) == {
        "backup": {"created": False},
        "changed": True,
        "diff": {
            "change": "remove",
            "operation": "remove",
            "scope": "managed_agents_block",
        },
        "dry_run": False,
        "status": "removed",
    }
    assert json.loads(repeated_remove.stdout) == {
        "backup": {"created": False},
        "changed": False,
        "diff": {
            "change": "none",
            "operation": "remove",
            "scope": "managed_agents_block",
        },
        "dry_run": False,
        "status": "unchanged",
    }
    assert target.read_bytes() == original
    backup = home / ".codex" / ".AGENTS.md.project-memory-hub.backup"
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


def test_agents_install_from_an_installed_distribution_needs_only_the_launcher(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    codex_home = home / ".codex"
    codex_home.mkdir(mode=0o700)
    launcher = _safe_launcher(tmp_path)

    class InstalledDistributionIdentity:
        @classmethod
        def discover(cls):
            return None

        @classmethod
        def discover_launcher(cls):
            return launcher

    monkeypatch.setattr(cli_module.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        cli_module,
        "InstallationIdentity",
        InstalledDistributionIdentity,
    )

    result = _invoke("install", "--format", "json")

    assert result.exit_code == 0
    assert json.loads(result.stdout)["status"] == "installed"
    document = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert "capture_pending_v1" in document
    assert f"{launcher} capture" not in document


def test_agents_dry_run_is_write_free_and_never_discloses_sensitive_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "private-home"
    home.mkdir()
    launcher = _safe_launcher(tmp_path)
    _set_installation(monkeypatch, home=home, launcher=launcher)
    target = home / ".codex" / "AGENTS.md"
    private_rule = "private-rule-do-not-print"
    target.write_text(private_rule, encoding="utf-8")
    before = target.read_bytes()

    install = _invoke("install", "--dry-run", "--format", "json")

    assert install.exit_code == 0
    payload = json.loads(install.stdout)
    assert payload == {
        "backup": {"created": False},
        "changed": True,
        "diff": {
            "change": "add_or_update",
            "operation": "install",
            "scope": "managed_agents_block",
        },
        "dry_run": True,
        "status": "would_install",
    }
    assert target.read_bytes() == before
    assert not (home / ".codex" / ".AGENTS.md.project-memory-hub.backup").exists()
    for sensitive in (private_rule, str(home), str(target), str(launcher)):
        assert sensitive not in install.stdout

    installed = _invoke("install")
    assert installed.exit_code == 0
    installed_document = target.read_bytes()
    remove = _invoke("remove", "--dry-run")

    assert remove.exit_code == 0
    assert remove.stdout.strip() == (
        "would_remove; changed=true; dry_run=true; "
        "diff=managed_agents_block:remove; backup_created=false"
    )
    assert target.read_bytes() == installed_document
    for sensitive in (private_rule, str(home), str(target), str(launcher)):
        assert sensitive not in remove.stdout


def test_agents_commands_report_not_available_without_stable_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _set_installation(monkeypatch, home=home, launcher=None)

    result = _invoke("install", "--dry-run", "--format", "json")

    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error": {
            "code": "not_available",
            "message": "Stable installation is not available.",
        },
        "status": "error",
    }
    assert not (home / ".codex" / "AGENTS.md").exists()
    assert "Traceback" not in result.stdout


def test_agents_commands_expose_no_arbitrary_path_or_launcher_options() -> None:
    help_result = _invoke("install", "--help")
    rejected = _invoke("install", "--path", "/tmp/AGENTS.md", "--format", "json")

    assert help_result.exit_code == 0
    assert "--path" not in help_result.stdout
    assert "--launcher" not in help_result.stdout
    assert rejected.exit_code == 4
    assert json.loads(rejected.stdout)["error"]["code"] == "invalid_input"


def test_agents_errors_are_redacted_and_stably_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    launcher = _safe_launcher(tmp_path)
    _set_installation(monkeypatch, home=home, launcher=launcher)
    target = home / ".codex" / "AGENTS.md"
    victim = tmp_path / "SENSITIVE_TARGET"
    victim.write_text("secret", encoding="utf-8")
    target.symlink_to(victim)

    result = _invoke("install", "--format", "json")

    assert result.exit_code == 2
    assert json.loads(result.stdout) == {
        "error": {
            "code": "permission_denied",
            "message": "Operation denied by local policy.",
        },
        "status": "error",
    }
    assert "SENSITIVE_TARGET" not in result.stdout
    assert "secret" not in result.stdout
    assert "Traceback" not in result.stdout


def test_agents_managed_markers_are_not_rendered_in_cli_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    launcher = _safe_launcher(tmp_path)
    _set_installation(monkeypatch, home=home, launcher=launcher)

    result = _invoke("install", "--dry-run", "--format", "json")

    assert result.exit_code == 0
    assert MANAGED_START not in result.stdout
    assert MANAGED_END not in result.stdout
