from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
from project_memory_hub.cli import app


runner = CliRunner()
PRIVATE_PATH = "/Users/private-owner/Secret Project"
PRIVATE_TOKEN = "ghp_abcdefghijklmnopqrstuvwxyz1234567890"
PRIVATE_INPUT = "do-not-echo-this-user-input"


def test_text_error_uses_allowlisted_copy_and_does_not_echo_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "build_doctor_container",
        lambda _path: (_ for _ in ()).throw(
            PermissionError(f"{PRIVATE_PATH} {PRIVATE_TOKEN} {PRIVATE_INPUT}")
        ),
    )

    result = runner.invoke(app, ["doctor", "--format", "text"])

    assert result.exit_code == 2
    assert result.stdout == (
        "error: permission_denied\n"
        "message: The operation was denied by local policy.\n"
        "hint: Review local permissions, then run memory-hub doctor --format json.\n"
    )
    assert PRIVATE_PATH not in result.stdout + result.stderr
    assert PRIVATE_TOKEN not in result.stdout + result.stderr
    assert PRIVATE_INPUT not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("code", "message", "hint"),
    (
        (
            "invalid_input",
            "The command input was not accepted.",
            "Review the command syntax and try again.",
        ),
        (
            "project_not_found",
            "The registered project was not found.",
            "Preview discovery with memory-hub discover --dry-run --format json.",
        ),
        (
            "source_disabled",
            "The requested source is disabled.",
            "Review enabled_sources in the private configuration before retrying.",
        ),
        (
            "codex_context_unavailable",
            "The active Codex context is unavailable.",
            'Run memory-hub codex-context --cwd "$PWD" --format json in the active task.',
        ),
        (
            "reconcile_required",
            "Reconciliation is required before this operation.",
            "Run memory-hub reconcile --if-due --format json, then retry.",
        ),
        (
            "probe_busy",
            "The source probe is already running.",
            "Wait for the current probe to finish, then retry.",
        ),
        (
            "not_available",
            "The requested integration is not available.",
            "Install Project Memory Hub from a stable launcher, then retry.",
        ),
        (
            "operation_failed",
            "The operation could not be completed safely.",
            "Run memory-hub doctor --format json before retrying.",
        ),
    ),
)
def test_text_error_copy_is_selected_only_by_stable_code(
    code: str,
    message: str,
    hint: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_module._emit_error(
        "text",
        code,
        f"{PRIVATE_PATH} {PRIVATE_TOKEN} {PRIVATE_INPUT}",
    )

    captured = capsys.readouterr()
    assert captured.out == f"error: {code}\nmessage: {message}\nhint: {hint}\n"
    assert captured.err == ""
    assert PRIVATE_PATH not in captured.out
    assert PRIVATE_TOKEN not in captured.out
    assert PRIVATE_INPUT not in captured.out


def test_unknown_text_error_uses_fixed_fallback_without_echoing_code_or_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_module._emit_error(
        "text",
        f"unknown-{PRIVATE_INPUT}",
        f"{PRIVATE_PATH} {PRIVATE_TOKEN}",
    )

    captured = capsys.readouterr()
    assert captured.out == (
        "error: operation_failed\n"
        "message: The operation could not be completed safely.\n"
        "hint: Run memory-hub doctor --format json before retrying.\n"
    )
    assert captured.err == ""
    assert PRIVATE_PATH not in captured.out
    assert PRIVATE_TOKEN not in captured.out
    assert PRIVATE_INPUT not in captured.out


def test_json_error_payload_and_exit_code_remain_byte_exact(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "runtime" / "config.toml"),
            "capture",
            "--stdin-json",
            "--format",
            "json",
        ],
        input="not-json",
    )

    assert result.exit_code == 4
    assert result.stdout == (
        '{"error":{"code":"invalid_input","message":"Invalid JSON input."},"status":"error"}\n'
    )


def test_text_init_renders_ordered_first_run_commands(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["--config", str(tmp_path / "runtime" / "config.toml"), "init"],
    )

    assert result.exit_code == 0
    assert result.stdout == (
        "initialized\n"
        "Next steps:\n"
        "1. Review first-run setup: memory-hub setup\n"
        "2. Preview discovery: memory-hub discover --dry-run --format json\n"
        "3. Apply discovery: memory-hub discover --format json\n"
        "4. Install AGENTS integration: "
        "memory-hub integrate agents install --format json\n"
        "5. Check local health: memory-hub doctor --format json\n"
    )


def test_json_init_payload_remains_exact(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "runtime" / "config.toml"),
            "init",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert result.stdout == '{"status":"initialized"}\n'
    assert json.loads(result.stdout) == {"status": "initialized"}
