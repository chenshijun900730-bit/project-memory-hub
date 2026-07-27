from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
from project_memory_hub.cli import app
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import SourceAgent
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.probes.base import ProbeBusyError
from project_memory_hub.probes.builtin import OPTIONAL_PROBE_SOURCES
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ModelStatus,
    ProbeCapability,
    ProbeMetrics,
    ProbeMode,
    ProbeWarningCode,
    SourceProbeResult,
    StructureStatus,
)
from project_memory_hub.storage.database import Database


runner = CliRunner()
PRIVATE_MARKER = "/private/SECRET_SCHEMA_CHAT_BODY"


def _result(
    source_agent: SourceAgent,
    *,
    mode: ProbeMode = ProbeMode.LIGHT,
    installation_status: InstallationStatus = InstallationStatus.DETECTED,
    data_status: DataStatus = DataStatus.READABLE,
    structure_status: StructureStatus = StructureStatus.NOT_RUN,
    model_status: ModelStatus = ModelStatus.NOT_CHECKED,
    warnings: tuple[ProbeWarningCode, ...] = (),
) -> SourceProbeResult:
    return SourceProbeResult(
        source_agent=source_agent,
        mode=mode,
        installation_status=installation_status,
        data_status=data_status,
        capability=(
            ProbeCapability.STRUCTURE_METADATA
            if source_agent is SourceAgent.TRAE
            else ProbeCapability.PRESENCE_AND_ACCESS
        ),
        structure_status=structure_status,
        model_status=model_status,
        ingestion_allowed=False,
        metrics=ProbeMetrics(),
        warning_codes=warnings,
        checked_at=datetime(2026, 7, 18, tzinfo=UTC),
    )


def _light_results() -> tuple[SourceProbeResult, ...]:
    return (
        _result(SourceAgent.TRAE),
        _result(
            SourceAgent.WORKBUDDY,
            installation_status=InstallationStatus.NOT_DETECTED,
            data_status=DataStatus.MISSING,
            warnings=(ProbeWarningCode.SOURCE_MISSING,),
        ),
        _result(
            SourceAgent.ZCODE,
            data_status=DataStatus.BLOCKED,
            warnings=(ProbeWarningCode.PERMISSION_BLOCKED,),
        ),
        _result(
            SourceAgent.QODERWORK,
            data_status=DataStatus.REJECTED,
            warnings=(ProbeWarningCode.SYMLINK_REJECTED,),
        ),
        _result(SourceAgent.CLAUDE_CODE),
    )


class FakeProbeService:
    def __init__(self) -> None:
        self.all_results = _light_results()
        self.one_result = _result(SourceAgent.TRAE)
        self.busy = False
        self.calls: list[tuple[object, ...]] = []

    def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
        self.calls.append(("all",))
        return self.all_results

    def probe_one(
        self,
        source_agent: SourceAgent,
        *,
        mode: ProbeMode = ProbeMode.LIGHT,
    ) -> SourceProbeResult:
        self.calls.append(("one", source_agent, mode))
        if self.busy:
            raise ProbeBusyError(f"probe_busy {PRIVATE_MARKER}")
        return self.one_result


class FakeProbeContainer:
    def __init__(self, service: FakeProbeService | None = None) -> None:
        self.source_probes = service or FakeProbeService()
        self.close_calls = 0
        self.close_error: BaseException | None = None

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def _install_container(
    monkeypatch: pytest.MonkeyPatch,
    container: FakeProbeContainer,
) -> list[tuple[object, ...]]:
    calls: list[tuple[object, ...]] = []

    def build(*args: object, **kwargs: object) -> FakeProbeContainer:
        calls.append((*args, kwargs))
        return container

    monkeypatch.setattr(cli_module, "build_probe_container", build)
    return calls


def _unexpected_build(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("invalid probe input constructed a container")


@pytest.mark.parametrize(
    "args",
    [
        ["source", "probe", "--format", "json"],
        ["source", "probe", "trae", "--all", "--format", "json"],
        ["source", "probe", "--all", "--structure", "--format", "json"],
        ["source", "probe", "workbuddy", "--structure", "--format", "json"],
        ["source", "probe", "codex", "--format", "json"],
        ["source", "probe", "chatgpt", "--format", "json"],
        ["source", "probe", PRIVATE_MARKER, "--format", "json"],
    ],
)
def test_probe_rejects_invalid_combinations_before_build(
    args: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "build_probe_container", _unexpected_build)

    result = runner.invoke(app, args)

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_probe_rejects_invalid_format_before_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module, "build_probe_container", _unexpected_build)

    result = runner.invoke(app, ["source", "probe", "trae", "--format", "xml"])

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"


def test_probe_all_json_has_stable_order_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeProbeContainer()
    build_calls = _install_container(monkeypatch, container)

    result = runner.invoke(app, ["source", "probe", "--all", "--format", "json"])

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["status"] == "ok"
    assert [item["source_agent"] for item in document["results"]] == [
        source.value for source in OPTIONAL_PROBE_SOURCES
    ]
    assert all(item["ingestion_allowed"] is False for item in document["results"])
    assert container.source_probes.calls == [("all",)]
    assert container.close_calls == 1
    assert build_calls == [({},)]


def test_probe_single_structure_returns_one_result_and_fixed_model_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeProbeService()
    service.one_result = _result(
        SourceAgent.TRAE,
        mode=ProbeMode.STRUCTURE,
        structure_status=StructureStatus.UNSUPPORTED,
        model_status=ModelStatus.UNVERIFIABLE,
        warnings=(
            ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
            ProbeWarningCode.UNSUPPORTED_FORMAT,
        ),
    )
    container = FakeProbeContainer(service)
    _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        ["source", "probe", "trae", "--structure", "--format", "json"],
    )

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert len(document["results"]) == 1
    assert document["results"][0]["model_status"] == "unverifiable"
    assert document["results"][0]["ingestion_allowed"] is False
    assert service.calls == [("one", SourceAgent.TRAE, ProbeMode.STRUCTURE)]
    assert container.close_calls == 1


def test_probe_text_uses_only_fixed_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeProbeContainer()
    _install_container(monkeypatch, container)

    result = runner.invoke(app, ["source", "probe", "--all"])

    assert result.exit_code == 0
    lines = result.stdout.strip().splitlines()
    assert lines[0] == (
        "Trae | Detected: Detected | Probe health: Readable | "
        "Model identity: Not checked | Structure: Not run | "
        "Behavior import: Locked | Warnings: none"
    )
    assert lines[1] == (
        "WorkBuddy | Detected: Not detected | Probe health: Missing | "
        "Model identity: Not checked | Structure: Not run | "
        "Behavior import: Locked | Warnings: source_missing"
    )
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_probe_busy_is_top_level_error_and_container_still_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeProbeService()
    service.busy = True
    container = FakeProbeContainer(service)
    _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        ["source", "probe", "trae", "--structure", "--format", "json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "probe_busy"
    assert container.close_calls == 1
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_probe_failed_result_is_normal_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeProbeService()
    service.one_result = _result(
        SourceAgent.WORKBUDDY,
        installation_status=InstallationStatus.NOT_DETECTED,
        data_status=DataStatus.MISSING,
        warnings=(ProbeWarningCode.PROBE_FAILED,),
    )
    container = FakeProbeContainer(service)
    _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        ["source", "probe", "workbuddy", "--format", "json"],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["results"][0]["warning_codes"] == ["probe_failed"]
    assert container.close_calls == 1


def test_global_config_is_not_forwarded_or_touched(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "existing" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_bytes(b"SECRET_CONFIG_BYTES")
    before = tuple(sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")))
    container = FakeProbeContainer()
    build_calls = _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "source",
            "probe",
            "trae",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert build_calls == [({},)]
    assert config_path.read_bytes() == b"SECRET_CONFIG_BYTES"
    assert tuple(sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))) == before


def test_nonexistent_global_config_remains_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / PRIVATE_MARKER.lstrip("/") / "config.toml"
    container = FakeProbeContainer()
    _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "source",
            "probe",
            "trae",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert not config_path.exists()
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_real_probe_builder_leaves_config_and_runtime_tree_unchanged(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    config_path = tmp_path / "existing" / "config.toml"
    config_path.parent.mkdir()
    config_path.write_bytes(b"NOT_A_CONFIG_BUT_MUST_REMAIN_UNTOUCHED")
    runtime = tmp_path / "must-not-exist"
    before = {
        str(path.relative_to(tmp_path)): (path.read_bytes() if path.is_file() else None)
        for path in tmp_path.rglob("*")
    }

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "source",
            "probe",
            "--all",
            "--format",
            "json",
        ],
        env={
            "HOME": str(home),
            "PROJECT_MEMORY_HUB_HOME": str(runtime),
        },
    )

    after = {
        str(path.relative_to(tmp_path)): (path.read_bytes() if path.is_file() else None)
        for path in tmp_path.rglob("*")
    }
    assert result.exit_code == 0
    assert before == after
    assert not runtime.exists()


def test_real_probe_builder_does_not_modify_existing_runtime_database(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    config_path = tmp_path / "runtime" / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    with build_container(config_path):
        pass
    paths = RuntimePaths.for_root(config_path.parent)
    database = Database(paths.database)

    def counts() -> tuple[int, ...]:
        with database.connect(readonly=True) as connection:
            return tuple(
                int(connection.execute(f"select count(*) from {table}").fetchone()[0])
                for table in (
                    "source_refs",
                    "behavior_memories",
                    "pending_captures",
                    "import_receipts",
                    "checkpoints",
                    "app_state",
                )
            )

    before_counts = counts()
    before_hash = hashlib.sha256(paths.database.read_bytes()).hexdigest()
    before_tree = {
        str(path.relative_to(config_path.parent)): (path.read_bytes() if path.is_file() else None)
        for path in config_path.parent.rglob("*")
    }
    home = tmp_path / "probe-home"
    home.mkdir()

    result = runner.invoke(
        app,
        [
            "--config",
            str(config_path),
            "source",
            "probe",
            "--all",
            "--format",
            "json",
        ],
        env={"HOME": str(home)},
    )

    after_tree = {
        str(path.relative_to(config_path.parent)): (path.read_bytes() if path.is_file() else None)
        for path in config_path.parent.rglob("*")
    }
    assert result.exit_code == 0
    assert counts() == before_counts
    assert hashlib.sha256(paths.database.read_bytes()).hexdigest() == before_hash
    assert after_tree == before_tree


def test_close_failure_never_outputs_success_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeProbeContainer()
    container.close_error = RuntimeError(f"close {PRIVATE_MARKER}")
    _install_container(monkeypatch, container)

    result = runner.invoke(app, ["source", "probe", "trae", "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert result.stdout.count("\n") == 1
    assert '"status":"ok"' not in result.stdout
    assert container.close_calls == 1
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_render_failure_closes_and_returns_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeProbeContainer()
    _install_container(monkeypatch, container)

    def fail_render(*_args: object, **_kwargs: object) -> NoReturn:
        raise TypeError(f"render {PRIVATE_MARKER}")

    monkeypatch.setattr(cli_module, "_render_response", fail_render, raising=False)
    result = runner.invoke(app, ["source", "probe", "trae", "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert container.close_calls == 1
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_success_is_rendered_before_close_but_echoed_only_after_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeProbeContainer()
    _install_container(monkeypatch, container)
    real_renderer = cli_module._probe_text

    def checked_renderer(response: dict[str, object]) -> str:
        assert container.close_calls == 0
        return real_renderer(response)

    echo_close_counts: list[int] = []
    real_echo = cli_module.typer.echo

    def checked_echo(message: object, *args: object, **kwargs: object) -> None:
        echo_close_counts.append(container.close_calls)
        real_echo(message, *args, **kwargs)

    monkeypatch.setattr(cli_module, "_probe_text", checked_renderer)
    monkeypatch.setattr(cli_module.typer, "echo", checked_echo)

    result = runner.invoke(app, ["source", "probe", "trae"])

    assert result.exit_code == 0
    assert container.close_calls == 1
    assert echo_close_counts == [1]


def test_echo_encoding_failure_after_close_returns_one_stable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeProbeContainer()
    _install_container(monkeypatch, container)
    real_echo = cli_module.typer.echo
    calls = 0

    def flaky_echo(message: object, *args: object, **kwargs: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise UnicodeEncodeError("ascii", PRIVATE_MARKER, 0, 1, "private")
        real_echo(message, *args, **kwargs)

    monkeypatch.setattr(cli_module.typer, "echo", flaky_echo)

    result = runner.invoke(app, ["source", "probe", "trae", "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert container.close_calls == 1
    assert calls == 2
    assert '"status":"ok"' not in result.stdout
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_debug_builder_failure_is_still_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_build(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError(f"builder {PRIVATE_MARKER}")

    monkeypatch.setattr(cli_module, "build_probe_container", failed_build)

    result = runner.invoke(
        app,
        ["--debug", "source", "probe", "trae", "--format", "json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert PRIVATE_MARKER not in result.stdout + result.stderr
    assert PRIVATE_MARKER not in repr(result.exception)


def test_debug_close_failure_is_still_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container = FakeProbeContainer()
    container.close_error = RuntimeError(f"close {PRIVATE_MARKER}")
    _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        ["--debug", "source", "probe", "trae", "--format", "json"],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert container.close_calls == 1
    assert PRIVATE_MARKER not in result.stdout + result.stderr
    assert PRIVATE_MARKER not in repr(result.exception)


@pytest.mark.parametrize("output_format", ("json", "text"))
def test_probe_service_infrastructure_failure_is_redacted_and_closes(
    output_format: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeProbeService()

    def failed_probe(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError(f"service {PRIVATE_MARKER}")

    service.probe_one = failed_probe  # type: ignore[method-assign]
    container = FakeProbeContainer(service)
    _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        ["source", "probe", "trae", "--format", output_format],
    )

    assert result.exit_code == 1
    if output_format == "json":
        assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    else:
        assert result.stdout == (
            "error: operation_failed\n"
            "message: The operation could not be completed safely.\n"
            "hint: Run memory-hub doctor --format json before retrying.\n"
        )
    assert container.close_calls == 1
    assert PRIVATE_MARKER not in result.stdout + result.stderr


def test_cleanup_base_exception_cannot_mask_an_active_base_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = FakeProbeService()

    def interrupted_probe(*_args: object, **_kwargs: object) -> NoReturn:
        raise KeyboardInterrupt("ORIGINAL_INTERRUPT")

    service.probe_one = interrupted_probe  # type: ignore[method-assign]
    container = FakeProbeContainer(service)
    container.close_error = SystemExit("CLEANUP_MUST_NOT_MASK")
    _install_container(monkeypatch, container)

    result = runner.invoke(
        app,
        ["source", "probe", "trae", "--format", "json"],
    )

    assert container.close_calls == 1
    assert result.exit_code == 130
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 130
    assert "CLEANUP_MUST_NOT_MASK" not in result.stdout + result.stderr
    assert "CLEANUP_MUST_NOT_MASK" not in repr(result.exception)
