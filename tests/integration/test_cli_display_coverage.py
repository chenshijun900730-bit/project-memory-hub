from __future__ import annotations

import io
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn
from uuid import UUID

import pytest
from pydantic import SecretStr
from typer.main import get_command
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
from project_memory_hub import __version__
from project_memory_hub.adapters.codex import CodexContextUnavailable
from project_memory_hub.cli import app
from project_memory_hub.domain import DiscoveryResult, Namespace
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.security.archive import UnsafeArchiveError


runner = CliRunner()
PRIVATE_MARKER = "PRIVATE_CLI_FAILURE_MUST_NOT_ECHO"
THREAD_ID = "70000000-0000-4000-8000-00000000000a"
PROJECT_ID = UUID("11111111-2222-4333-8444-555555555555")


class _Container:
    def __init__(self, **members: object) -> None:
        self.close_calls = 0
        self.close_error: BaseException | None = None
        for name, value in members.items():
            setattr(self, name, value)

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _raise(error: BaseException) -> NoReturn:
    raise error


def _codex_environment(tmp_path: Path, project: Path) -> dict[str, str]:
    home = tmp_path / "home"
    sessions = home / ".codex" / "sessions" / "2026" / "07" / "18"
    sessions.mkdir(parents=True)
    records = (
        {
            "timestamp": "2026-07-18T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": THREAD_ID, "session_id": THREAD_ID},
        },
        {
            "timestamp": "2026-07-18T00:00:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-current",
                "cwd": str(project),
                "model": "gpt-5.6-sol",
                "summary": PRIVATE_MARKER,
            },
        },
    )
    (sessions / f"rollout-2026-07-18-{THREAD_ID}.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return {"HOME": str(home), "CODEX_THREAD_ID": THREAD_ID}


def _import_report() -> SimpleNamespace:
    return SimpleNamespace(
        already_resolved_count=0,
        confirmation_count=0,
        dry_run=False,
        duplicate_count=0,
        imported_count=1,
        resolved_count=0,
        unmatched_resolution_count=0,
        warning_count=0,
    )


def test_safe_group_returns_from_real_command_when_standalone_is_disabled(capsys) -> None:
    result = get_command(app).main(
        args=["version"],
        prog_name="memory-hub",
        standalone_mode=False,
    )

    assert result is None
    assert capsys.readouterr().out.strip() == __version__


def test_safe_group_maps_parse_failure_when_standalone_is_disabled(capsys) -> None:
    with pytest.raises(cli_module.click.exceptions.Exit) as raised:
        get_command(app).main(
            args=["version", "--unknown-option", PRIVATE_MARKER],
            prog_name="memory-hub",
            standalone_mode=False,
        )

    captured = capsys.readouterr()
    assert raised.value.exit_code == 4
    assert captured.err == (
        "error: invalid_input\n"
        "message: The command input was not accepted.\n"
        "hint: Review the command syntax and try again.\n"
    )
    assert PRIVATE_MARKER not in captured.out + captured.err


def test_invalid_format_is_a_stable_json_error_before_runtime_creation(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"

    result = runner.invoke(
        app,
        ["--config", str(runtime / "config.toml"), "init", "--format", "yaml"],
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout) == {
        "error": {"code": "invalid_input", "message": "Invalid output format."},
        "status": "error",
    }
    assert not runtime.exists()


def test_plain_parse_failure_uses_stderr_without_echoing_arguments() -> None:
    result = runner.invoke(app, ["version", "--unknown-option", PRIVATE_MARKER])

    assert result.exit_code == 4
    assert result.stdout == ""
    assert result.stderr == (
        "error: invalid_input\n"
        "message: The command input was not accepted.\n"
        "hint: Review the command syntax and try again.\n"
    )
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_codex_context_text_output_contains_only_the_model(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    result = runner.invoke(
        app,
        ["codex-context", "--cwd", str(project)],
        env=_codex_environment(tmp_path, project),
    )

    assert result.exit_code == 0
    assert result.stdout == "gpt-5.6-sol\n"
    assert PRIVATE_MARKER not in result.stdout


def test_codex_context_os_error_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module.CodexAdapter,
        "resolve_namespace",
        lambda *_args, **_kwargs: _raise(OSError(PRIVATE_MARKER)),
    )

    result = runner.invoke(
        app,
        ["codex-context", "--cwd", str(tmp_path), "--format", "json"],
        env={"CODEX_THREAD_ID": THREAD_ID},
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "permission_denied"
    assert PRIVATE_MARKER not in result.stdout + result.stderr


@pytest.mark.parametrize("debug", (False, True))
def test_codex_context_unexpected_error_respects_debug_mode(
    debug: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module.CodexAdapter,
        "resolve_namespace",
        lambda *_args, **_kwargs: _raise(RuntimeError(PRIVATE_MARKER)),
    )
    arguments = ["codex-context", "--cwd", str(tmp_path), "--format", "json"]
    if debug:
        arguments.insert(0, "--debug")

    result = runner.invoke(app, arguments, env={"CODEX_THREAD_ID": THREAD_ID})

    assert result.exit_code == 1
    if debug:
        assert isinstance(result.exception, RuntimeError)
        assert PRIVATE_MARKER in str(result.exception)
    else:
        assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
        assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_recall_reports_project_not_found_from_real_runtime(tmp_path: Path) -> None:
    project = tmp_path / "unregistered-project"
    project.mkdir()
    runtime = tmp_path / "runtime"
    config = runtime / "config.toml"
    initialized = runner.invoke(
        app,
        ["--config", str(config), "init", "--format", "json"],
    )
    assert initialized.exit_code == 0
    request = {
        "cwd": str(project),
        "task": "recall an unregistered project",
        "namespace": {"source_agent": "codex", "model_id": "gpt-5.6-sol"},
        "max_tokens": 128,
    }

    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "recall",
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps(request),
        env=_codex_environment(tmp_path, project),
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "project_not_found"


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_exit"),
    (
        (PermissionError(PRIVATE_MARKER), "permission_denied", 2),
        (RuntimeError(PRIVATE_MARKER), "operation_failed", 1),
    ),
)
def test_doctor_builder_failures_are_stable_and_redacted(
    error: Exception,
    expected_code: str,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "build_doctor_container",
        lambda _path: _raise(error),
    )

    result = runner.invoke(app, ["doctor", "--format", "json"])

    assert result.exit_code == expected_exit
    assert json.loads(result.stdout)["error"]["code"] == expected_code
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_doctor_unexpected_failure_is_visible_only_in_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "build_doctor_container",
        lambda _path: _raise(RuntimeError(PRIVATE_MARKER)),
    )

    result = runner.invoke(app, ["--debug", "doctor", "--format", "json"])

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert PRIVATE_MARKER in str(result.exception)


def test_doctor_text_rejects_malformed_checks_and_skips_unknown_items() -> None:
    assert cli_module._doctor_text({"status": "warn", "checks": "not-a-list"}) == ("status: fail")
    assert cli_module._doctor_text({"status": "warn", "checks": [None, {"name": "database"}]}) == (
        "status: warn\ndatabase: fail [check_failed] Review the local installation."
    )


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_exit"),
    (
        (PermissionError(PRIVATE_MARKER), "permission_denied", 2),
        (RuntimeError(PRIVATE_MARKER), "operation_failed", 1),
    ),
)
def test_chatgpt_configuration_failures_are_redacted(
    error: Exception,
    expected_code: str,
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "configured_source_enabled",
        lambda *_args: _raise(error),
    )

    result = runner.invoke(
        app,
        ["import", "chatgpt", "/tmp/export.zip", "--format", "json"],
    )

    assert result.exit_code == expected_exit
    assert json.loads(result.stdout)["error"]["code"] == expected_code
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_chatgpt_configuration_failure_respects_debug(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "configured_source_enabled",
        lambda *_args: _raise(RuntimeError(PRIVATE_MARKER)),
    )

    result = runner.invoke(
        app,
        ["--debug", "import", "chatgpt", "/tmp/export.zip", "--format", "json"],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert PRIVATE_MARKER in str(result.exception)


def test_chatgpt_container_fallback_enforces_disabled_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container(config=SimpleNamespace(enabled_sources=()))
    monkeypatch.setattr(cli_module, "configured_source_enabled", lambda *_args: True)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)

    result = runner.invoke(
        app,
        ["import", "chatgpt", "/tmp/export.zip", "--format", "json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "source_disabled"
    assert container.close_calls == 1


def test_chatgpt_unsafe_archive_has_stable_invalid_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        import_zip=lambda *_args, **_kwargs: _raise(UnsafeArchiveError(PRIVATE_MARKER))
    )
    container = _Container(source_enabled=True, chatgpt_adapter=adapter)
    monkeypatch.setattr(cli_module, "configured_source_enabled", lambda *_args: True)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)

    result = runner.invoke(
        app,
        ["import", "chatgpt", "/tmp/export.zip", "--format", "json"],
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert PRIVATE_MARKER not in result.stdout + result.stderr
    assert container.close_calls == 1


def test_chatgpt_text_success_uses_only_the_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(import_zip=lambda *_args, **_kwargs: _import_report())
    container = _Container(source_enabled=True, chatgpt_adapter=adapter)
    monkeypatch.setattr(cli_module, "configured_source_enabled", lambda *_args: True)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)

    result = runner.invoke(app, ["import", "chatgpt", "/tmp/export.zip"])

    assert result.exit_code == 0
    assert result.stdout == "ok\n"
    assert container.close_calls == 1


@pytest.mark.parametrize(
    ("compaction", "expected_code"),
    (
        (
            SimpleNamespace(
                compact_project=lambda *_args, **_kwargs: _raise(KeyError(PRIVATE_MARKER))
            ),
            "project_not_found",
        ),
        (
            SimpleNamespace(
                compact_project=lambda *_args, **_kwargs: SimpleNamespace(failure_count=1)
            ),
            "operation_failed",
        ),
    ),
)
def test_compact_failures_have_stable_codes(
    compaction: object,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container(compaction=compaction)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)

    result = runner.invoke(
        app,
        ["compact", "--project", str(PROJECT_ID), "--format", "json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == expected_code
    assert PRIVATE_MARKER not in result.stdout + result.stderr
    assert container.close_calls == 1


@pytest.mark.parametrize("port", (0, 65_536))
def test_serve_rejects_out_of_range_ports(port: int) -> None:
    result = runner.invoke(app, ["serve", "--port", str(port)])

    assert result.exit_code == 4
    assert result.stdout == (
        "error: invalid_input\n"
        "message: The command input was not accepted.\n"
        "hint: Review the command syntax and try again.\n"
    )


def test_serve_permission_failure_is_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module,
        "build_container",
        lambda _path: _raise(PermissionError(PRIVATE_MARKER)),
    )

    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 2
    assert result.stdout == (
        "error: permission_denied\n"
        "message: The operation was denied by local policy.\n"
        "hint: Review local permissions, then run memory-hub doctor --format json.\n"
    )
    assert PRIVATE_MARKER not in result.stdout + result.stderr


@pytest.mark.parametrize("debug", (False, True))
def test_serve_runtime_failure_respects_debug_and_closes(
    debug: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = _Container()
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)
    monkeypatch.setattr(cli_module, "create_app", lambda _container: object())
    monkeypatch.setattr(
        cli_module.uvicorn,
        "run",
        lambda *_args, **_kwargs: _raise(RuntimeError(PRIVATE_MARKER)),
    )
    arguments = ["serve"]
    if debug:
        arguments.insert(0, "--debug")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1
    assert container.close_calls == 1
    if debug:
        assert isinstance(result.exception, RuntimeError)
        assert PRIVATE_MARKER in str(result.exception)
    else:
        assert result.stdout == (
            "error: operation_failed\n"
            "message: The operation could not be completed safely.\n"
            "hint: Run memory-hub doctor --format json before retrying.\n"
        )
        assert PRIVATE_MARKER not in result.stdout + result.stderr


@pytest.mark.parametrize("debug", (False, True))
def test_agents_unexpected_failure_respects_debug(
    debug: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenInstallationIdentity:
        @classmethod
        def discover_launcher(cls) -> None:
            del cls
            raise RuntimeError(PRIVATE_MARKER)

    monkeypatch.setattr(cli_module, "InstallationIdentity", BrokenInstallationIdentity)
    arguments = ["integrate", "agents", "install", "--format", "json"]
    if debug:
        arguments.insert(0, "--debug")

    result = runner.invoke(app, arguments)

    assert result.exit_code == 1
    if debug:
        assert isinstance(result.exception, RuntimeError)
        assert PRIVATE_MARKER in str(result.exception)
    else:
        assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
        assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_run_debug_mode_reraises_unexpected_operation_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = SimpleNamespace(discover=lambda: _raise(RuntimeError(PRIVATE_MARKER)))
    container = _Container(project_scanner=scanner)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)

    result = runner.invoke(
        app,
        ["--debug", "discover", "--dry-run", "--format", "json"],
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert PRIVATE_MARKER in str(result.exception)
    assert container.close_calls == 1


def test_run_swallows_secondary_echo_failure_after_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scanner = SimpleNamespace(discover=lambda: DiscoveryResult(candidates=(), issues=()))
    container = _Container(project_scanner=scanner)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)
    echo_calls = 0

    def fail_echo(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal echo_calls
        echo_calls += 1
        raise UnicodeError(PRIVATE_MARKER)

    monkeypatch.setattr(cli_module.typer, "echo", fail_echo)

    result = runner.invoke(app, ["discover", "--dry-run", "--format", "json"])

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 1
    assert echo_calls == 2
    assert container.close_calls == 1
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_stdin_model_requires_the_explicit_stdin_flag(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "runtime" / "config.toml"),
            "capture",
            "--format",
            "json",
        ],
        input="{}",
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"] == {
        "code": "invalid_input",
        "message": "JSON stdin is required.",
    }


@pytest.mark.parametrize("data", (b"{", b"[]", b"{}"))
def test_model_from_json_bytes_rejects_parse_shape_and_validation_errors(data: bytes) -> None:
    with pytest.raises(cli_module._CliFailure) as raised:
        cli_module._model_from_json_bytes(cli_module._ProposalCreateInput, data)

    assert raised.value.code == "invalid_input"
    assert raised.value.exit_code == 4


def test_stdin_byte_reader_supports_text_only_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", io.StringIO('{"value":"safe"}'))

    assert cli_module._read_stdin_bytes() == b'{"value":"safe"}'


def test_stdin_line_reader_trims_crlf_from_text_only_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", io.StringIO('{"value":"safe"}\r\n'))

    assert cli_module._read_stdin_line_bytes() == b'{"value":"safe"}'


def test_stdin_line_reader_rejects_truncated_text_only_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized = "x" * (cli_module._MAX_STDIN_BYTES + 1) + "\n"
    monkeypatch.setattr(cli_module.sys, "stdin", io.StringIO(oversized))

    with pytest.raises(cli_module._CliFailure) as raised:
        cli_module._read_stdin_line_bytes()

    assert raised.value.code == "invalid_input"


def test_close_container_reraises_base_exception_without_active_error() -> None:
    container = _Container()
    container.close_error = KeyboardInterrupt(PRIVATE_MARKER)

    with pytest.raises(KeyboardInterrupt, match=PRIVATE_MARKER):
        cli_module._close_container(
            container,
            pending_error=False,
            state=cli_module._CliState(config_path=None, debug=False),
            output_format="json",
        )


def test_close_container_reraises_exception_in_debug_mode() -> None:
    container = _Container()
    container.close_error = RuntimeError(PRIVATE_MARKER)

    with pytest.raises(RuntimeError, match=PRIVATE_MARKER):
        cli_module._close_container(
            container,
            pending_error=False,
            state=cli_module._CliState(config_path=None, debug=True),
            output_format="json",
        )


def test_display_helpers_fail_closed_for_hostile_shapes(capsys) -> None:
    assert cli_module._proposal_list_text({"proposals": "not-a-list"}) == "No proposals."
    assert cli_module._proposal_list_text({"proposals": [None]}) == "No proposals."
    assert cli_module._proposal_text({"status": "draft", "dry_run": True}) == (
        "draft; verification=partial; unverified=unknown"
    )

    cli_module._emit("text", {})

    assert capsys.readouterr().out == "ok\n"


def test_count_helpers_reject_non_integer_and_unbounded_warning_shapes() -> None:
    error = RuntimeError("not sqlite")
    error.sqlite_errorcode = "busy"  # type: ignore[attr-defined]

    class BadWarnings:
        def __len__(self) -> int:
            raise OverflowError(PRIVATE_MARKER)

    assert cli_module._is_transient_database_error(error) is False  # type: ignore[arg-type]
    assert cli_module._chatgpt_warning_count(SimpleNamespace(warnings="secret")) == 0
    assert cli_module._chatgpt_warning_count(SimpleNamespace(warnings=BadWarnings())) == 0


def test_live_namespace_errors_map_to_stable_recall_failures(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    request = SimpleNamespace(
        cwd=tmp_path,
        namespace=Namespace(source_agent="codex", model_id="gpt-5.6-sol"),
    )
    monkeypatch.setenv("CODEX_THREAD_ID", THREAD_ID)

    monkeypatch.setattr(
        cli_module.CodexAdapter,
        "resolve_namespace",
        lambda *_args, **_kwargs: _raise(CodexContextUnavailable(PRIVATE_MARKER)),
    )
    with pytest.raises(cli_module._CliFailure) as unavailable:
        cli_module._require_live_codex_namespace(request)  # type: ignore[arg-type]
    assert unavailable.value.code == "codex_context_unavailable"

    monkeypatch.setattr(
        cli_module.CodexAdapter,
        "resolve_namespace",
        lambda *_args, **_kwargs: _raise(OSError(PRIVATE_MARKER)),
    )
    with pytest.raises(cli_module._CliFailure) as denied:
        cli_module._require_live_codex_namespace(request)  # type: ignore[arg-type]
    assert denied.value.code == "permission_denied"


def test_non_tty_proposal_authorization_fails_before_token_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", io.StringIO(""))

    with pytest.raises(cli_module._CliFailure) as denied:
        cli_module._authorize_proposal_mutation(
            RuntimePaths.for_root(tmp_path / "runtime"),
            token=None,
            stdin_json=False,
            yes=False,
            action="approve",
            proposal_id=PROJECT_ID,
        )

    assert denied.value.code == "permission_denied"


def test_tty_yes_authorization_maps_token_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TtyInput(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(cli_module.sys, "stdin", TtyInput())
    monkeypatch.setattr(cli_module.typer, "prompt", lambda *_args, **_kwargs: "token")
    monkeypatch.setattr(
        cli_module.LocalAccessToken,
        "load_existing",
        lambda _paths: _raise(OSError(PRIVATE_MARKER)),
    )

    with pytest.raises(cli_module._CliFailure) as denied:
        cli_module._authorize_proposal_mutation(
            RuntimePaths.for_root(tmp_path / "runtime"),
            token=None,
            stdin_json=False,
            yes=True,
            action="approve",
            proposal_id=PROJECT_ID,
        )

    assert denied.value.code == "permission_denied"


def test_manual_recall_maps_token_read_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli_module.LocalAccessToken,
        "load_existing",
        lambda _paths: _raise(OSError(PRIVATE_MARKER)),
    )

    with pytest.raises(cli_module._CliFailure) as denied:
        cli_module._authorize_manual_recall(
            RuntimePaths.for_root(tmp_path / "runtime"),
            SecretStr("token"),
        )

    assert denied.value.code == "permission_denied"


def test_non_tty_create_without_json_mode_uses_the_bounded_stdin_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", io.StringIO("{}"))

    with pytest.raises(cli_module._CliFailure) as invalid:
        cli_module._proposal_create_request(False)

    assert invalid.value.code == "invalid_input"
    assert invalid.value.message == "JSON stdin is required."


def test_non_transient_capture_failure_is_not_enqueued() -> None:
    original = sqlite3.OperationalError(PRIVATE_MARKER)
    original.sqlite_errorcode = sqlite3.SQLITE_IOERR  # type: ignore[attr-defined]
    enqueue_calls: list[object] = []
    container = SimpleNamespace(
        capture=SimpleNamespace(capture=lambda _payload: _raise(original)),
        retry_queue=SimpleNamespace(enqueue=lambda payload, _reason: enqueue_calls.append(payload)),
    )

    with pytest.raises(sqlite3.OperationalError) as raised:
        cli_module._capture_with_transient_retry(container, object())  # type: ignore[arg-type]

    assert raised.value is original
    assert enqueue_calls == []
