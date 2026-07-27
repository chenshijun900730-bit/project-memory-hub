import json
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
import uvicorn
from typer.main import get_command
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
from project_memory_hub import __version__
from project_memory_hub.adapters.base import ReconcileRequiredError
from project_memory_hub.adapters.codex import DiscoveryLimitExceeded
from project_memory_hub.cli import app
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import (
    CapturePayload,
    DiscoveryIssue,
    DiscoveryResult,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.services.tokens import ConservativeTokenCounter
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.storage.database import Database


runner = CliRunner()


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _private_config(
    tmp_path: Path,
    project_root: Path,
    *,
    max_recall_tokens: int = 800,
) -> Path:
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=max_recall_tokens,
            daily_reconcile_time="03:30",
        )
    )
    return config


def _register(container, project: Path):
    return container.projects.register(
        ProjectCandidate(
            canonical_path=project,
            display_name=project.name,
            git_root=project if (project / ".git").exists() else None,
            markers=("pyproject.toml",),
        )
    )


def _counts(container) -> tuple[int, int, int, int]:
    with container.database.connect(readonly=True) as connection:
        return tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "projects",
                "project_facts",
                "checkpoints",
                "behavior_memories",
            )
        )


def _capture_json(project: Path, model_id: str = "gpt-5") -> dict:
    return {
        "cwd": str(project),
        "namespace": {"source_agent": "codex", "model_id": model_id},
        "source_record_id": f"record-{model_id}",
        "objective": "fix cache",
        "outcome": "cache fixed",
        "verified_commands": ["uv run pytest"],
    }


def _live_codex_env(
    tmp_path: Path,
    project: Path,
    model_id: str,
) -> dict[str, str]:
    home = tmp_path / "codex-home"
    sessions = home / ".codex" / "sessions" / "2026" / "07" / "15"
    sessions.mkdir(parents=True, exist_ok=True)
    thread_id = "70000000-0000-4000-8000-00000000000a"
    session = sessions / f"rollout-2026-07-15-{thread_id}.jsonl"
    records = (
        {
            "timestamp": "2026-07-15T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "session_id": thread_id},
        },
        {
            "timestamp": "2026-07-15T00:00:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-current",
                "cwd": str(project),
                "model": model_id,
                "summary": "",
            },
        },
    )
    session.write_text("".join(json.dumps(record) + "\n" for record in records))
    return {"HOME": str(home), "CODEX_THREAD_ID": thread_id}


def test_init_is_idempotent_and_private(tmp_path):
    config = tmp_path / "runtime" / "config.toml"

    first = runner.invoke(app, ["--config", str(config), "init", "--format", "json"])
    second = runner.invoke(app, ["--config", str(config), "init", "--format", "json"])

    assert first.exit_code == second.exit_code == 0
    assert json.loads(first.stdout)["status"] == "initialized"
    assert json.loads(second.stdout)["status"] == "initialized"
    assert _mode(config.parent) == 0o700
    assert _mode(config) == 0o600
    assert _mode(config.parent / "memory.db") == 0o600
    for directory in ("imports", "retries", "backups", "logs"):
        assert _mode(config.parent / directory) == 0o700
    for suffix in ("-wal", "-shm"):
        candidate = config.parent / f"memory.db{suffix}"
        if candidate.exists():
            assert _mode(candidate) == 0o600


def test_container_uses_exact_config_parent_and_closes_idempotently(tmp_path):
    config = tmp_path / "isolated" / "custom.toml"

    container = build_container(config)

    assert container.paths.root == config.parent
    assert container.config_manager.path == config
    container.close()
    container.close()
    assert container._closed is True


def test_malformed_capture_is_stable_invalid_input(tmp_path):
    config = tmp_path / "runtime" / "config.toml"

    result = runner.invoke(
        app,
        ["--config", str(config), "capture", "--stdin-json", "--format", "json"],
        input="not-json",
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout) == {
        "error": {"code": "invalid_input", "message": "Invalid JSON input."},
        "status": "error",
    }
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize(
    "hostile_json",
    (
        '{"value":' + ("9" * 4301) + "}",
        ("[" * 1200) + "0" + ("]" * 1200),
    ),
)
def test_hostile_json_parser_values_are_stable_invalid_input(tmp_path, hostile_json):
    config = tmp_path / "runtime" / "config.toml"

    result = runner.invoke(
        app,
        ["--config", str(config), "capture", "--stdin-json", "--format", "json"],
        input=hostile_json,
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert "Traceback" not in result.stdout


def test_non_utf8_capture_text_is_typed_invalid_input_without_echo(tmp_path):
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
    payload = _capture_json(project)
    payload["outcome"] = "PRIVATE-\ud800-MARKER"

    result = runner.invoke(
        app,
        ["--config", str(config), "capture", "--stdin-json", "--format", "json"],
        input=json.dumps(payload),
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout) == {
        "error": {"code": "invalid_input", "message": "Invalid JSON input."},
        "status": "error",
    }
    assert "PRIVATE" not in result.stdout
    assert "Traceback" not in result.stdout
    with build_container(config) as container:
        with container.database.connect(readonly=True) as connection:
            assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0


def test_doctor_reports_missing_runtime_without_creating_it(tmp_path):
    runtime = tmp_path / "runtime"
    result = runner.invoke(
        app,
        ["--config", str(runtime / "config.toml"), "doctor", "--format", "json"],
    )

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "fail"
    assert len(payload["checks"]) == 12
    assert {check["name"] for check in payload["checks"]} >= {
        "runtime_permissions",
        "database_quick_check",
        "managed_agents",
        "codex_automation",
    }
    assert not runtime.exists()


def test_doctor_warn_report_is_successful_and_renders_safe_text(monkeypatch):
    payload = {
        "checks": [
            {
                "code": "codex_automation_missing",
                "name": "codex_automation",
                "remediation": "Create it through the Codex host tool.",
                "status": "warn",
            }
        ],
        "status": "warn",
    }
    report = SimpleNamespace(status="warn", as_dict=lambda: payload)
    closed = []
    container = SimpleNamespace(
        doctor=SimpleNamespace(run=lambda: report),
        close=lambda: closed.append(True),
    )
    monkeypatch.setattr(
        cli_module,
        "build_doctor_container",
        lambda _config: container,
    )

    result = runner.invoke(app, ["doctor", "--format", "text"])

    assert result.exit_code == 0
    assert result.stdout == (
        "status: warn\n"
        "codex_automation: warn [codex_automation_missing] "
        "Create it through the Codex host tool.\n"
    )
    assert closed == [True]


def test_version_is_side_effect_free(tmp_path):
    runtime = tmp_path / "absent"

    result = runner.invoke(app, ["--config", str(runtime / "config.toml"), "version"])

    assert result.exit_code == 0
    assert result.stdout.strip() == __version__
    assert not runtime.exists()


def test_codex_context_resolves_exact_namespace_without_runtime_side_effects(tmp_path):
    home = tmp_path / "private-home"
    sessions = home / ".codex" / "sessions" / "2026" / "07" / "15"
    sessions.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    thread_id = "70000000-0000-4000-8000-00000000000a"
    session_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    session = sessions / f"rollout-2026-07-15-{thread_id}.jsonl"
    records = (
        {
            "timestamp": "2026-07-15T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": thread_id, "session_id": session_id},
        },
        {
            "timestamp": "2026-07-15T00:00:01Z",
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-current",
                "cwd": str(project),
                "model": "gpt-5.6-sol",
                "summary": "RAW_SUMMARY_MUST_NOT_BE_RETURNED",
            },
        },
    )
    session.write_text("".join(json.dumps(record) + "\n" for record in records))

    result = runner.invoke(
        app,
        ["codex-context", "--cwd", str(project), "--format", "json"],
        env={"HOME": str(home), "CODEX_THREAD_ID": thread_id},
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "namespace": {"model_id": "gpt-5.6-sol", "source_agent": "codex"},
        "source_record_id": thread_id,
        "status": "ok",
    }
    assert "RAW_SUMMARY_MUST_NOT_BE_RETURNED" not in result.stdout
    assert not (home / "Library" / "Application Support" / "Project Memory Hub").exists()


def test_codex_context_ignores_large_nonsemantic_record_without_exposing_it(tmp_path):
    home = tmp_path / "private-home"
    sessions = home / ".codex" / "sessions" / "2026" / "07" / "15"
    sessions.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    thread_id = "70000000-0000-4000-8000-00000000000a"
    marker = "RAW_LARGE_TOOL_OUTPUT_MUST_NOT_BE_RETURNED"
    session = sessions / f"rollout-2026-07-15-{thread_id}.jsonl"
    records = (
        {
            "type": "session_meta",
            "payload": {"id": thread_id, "session_id": thread_id},
        },
        {
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-current",
                "cwd": str(project),
                "model": "gpt-5.6-sol",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": marker + ("x" * 4_194_304),
            },
        },
    )
    session.write_text("".join(json.dumps(record) + "\n" for record in records))

    result = runner.invoke(
        app,
        ["codex-context", "--cwd", str(project), "--format", "json"],
        env={"HOME": str(home), "CODEX_THREAD_ID": thread_id},
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "namespace": {"model_id": "gpt-5.6-sol", "source_agent": "codex"},
        "source_record_id": thread_id,
        "status": "ok",
    }
    assert marker not in result.stdout


def test_codex_context_missing_thread_id_fails_without_creating_runtime(tmp_path):
    home = tmp_path / "private-home"
    project = tmp_path / "project"
    project.mkdir()

    result = runner.invoke(
        app,
        ["codex-context", "--cwd", str(project), "--format", "json"],
        env={"HOME": str(home), "CODEX_THREAD_ID": ""},
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert not (home / "Library" / "Application Support" / "Project Memory Hub").exists()


def test_codex_context_discovery_limit_has_stable_error_boundary(
    tmp_path,
    monkeypatch,
):
    home = tmp_path / "private-home"
    project = tmp_path / "project"
    project.mkdir()
    thread_id = "70000000-0000-4000-8000-00000000000a"

    def exceed_limit(_adapter):
        raise DiscoveryLimitExceeded("discovery_limit_exceeded")

    monkeypatch.setattr(cli_module.CodexAdapter, "discover_scopes", exceed_limit)
    result = runner.invoke(
        app,
        ["codex-context", "--cwd", str(project), "--format", "json"],
        env={"HOME": str(home), "CODEX_THREAD_ID": thread_id},
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "codex_context_unavailable"
    assert not (home / "Library" / "Application Support" / "Project Memory Hub").exists()


def test_discover_dry_run_does_not_register_then_persist_does(tmp_path):
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n")
    config = _private_config(tmp_path, root)
    sentinel = DiscoveryIssue(
        path=tmp_path / "previously-blocked",
        code="blocked_permission",
        remediation="Grant access.",
    )
    with build_container(config) as container:
        container.discovery_findings.sync(DiscoveryResult(candidates=(), issues=(sentinel,)))

    dry = runner.invoke(
        app,
        ["--config", str(config), "discover", "--dry-run", "--format", "json"],
    )
    with build_container(config) as container:
        assert _counts(container)[0] == 0
        assert container.discovery_findings.snapshot().issues[0].path == sentinel.path
    persisted = runner.invoke(app, ["--config", str(config), "discover", "--format", "json"])

    assert dry.exit_code == persisted.exit_code == 0
    assert len(json.loads(dry.stdout)["candidates"]) == 1
    assert len(json.loads(persisted.stdout)["projects"]) == 1
    with build_container(config) as container:
        assert _counts(container)[0] == 1
        assert container.discovery_findings.snapshot().issues == ()


def test_discovery_issue_does_not_echo_inaccessible_path(monkeypatch, tmp_path):
    marker = "NEVER_ECHO_INACCESSIBLE_FILENAME"

    class FakeContainer:
        project_scanner = SimpleNamespace(
            discover=lambda: DiscoveryResult(
                candidates=(),
                issues=(
                    DiscoveryIssue(
                        path=Path("/private") / marker,
                        code="blocked_permission",
                        remediation="Grant access.",
                    ),
                ),
            )
        )
        projects = SimpleNamespace()

        def close(self):
            pass

    monkeypatch.setattr(cli_module, "build_container", lambda _path: FakeContainer())
    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "config.toml"),
            "discover",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["issues"][0]["code"] == "blocked_permission"
    assert marker not in result.stdout


def test_scan_dry_run_is_read_only_and_normal_scan_persists(tmp_path):
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname = 'demo'\n[tool.pytest.ini_options]\n")
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
        before = _counts(container)

    dry = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "scan",
            "--cwd",
            str(project),
            "--dry-run",
            "--format",
            "json",
        ],
    )
    with build_container(config) as container:
        assert _counts(container) == before
    persisted = runner.invoke(
        app,
        ["--config", str(config), "scan", "--cwd", str(project), "--format", "json"],
    )

    assert dry.exit_code == persisted.exit_code == 0
    assert json.loads(dry.stdout)["observed_count"] > 0
    with build_container(config) as container:
        after = _counts(container)
    assert after[1] > before[1]
    assert after[2] == before[2]


def test_capture_from_cli_stays_pending_and_search_is_empty(tmp_path):
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        record = _register(container, project)

    result = runner.invoke(
        app,
        ["--config", str(config), "capture", "--stdin-json", "--format", "json"],
        input=json.dumps(_capture_json(project)),
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    assert output["status"] == "pending_verification"
    assert output["resolved_count"] == 0
    assert output["already_resolved_count"] == 0
    assert output["unmatched_resolution_count"] == 0
    with build_container(config) as container:
        assert (
            container.memories.search(
                record.project_id,
                Namespace(source_agent="codex", model_id="gpt-5"),
                "",
                10,
            )
            == []
        )
        with container.database.connect(readonly=True) as connection:
            assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 1


def test_recall_is_token_bounded_and_namespace_isolated(tmp_path):
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
        for model, outcome in (
            ("gpt-5", "OWN_NAMESPACE_METHOD"),
            ("other-model", "FOREIGN_NAMESPACE_SECRET"),
        ):
            payload = CapturePayload.model_validate(
                {**_capture_json(project, model), "outcome": outcome}
            )
            result = container.capture.capture(
                payload,
                NamespaceVerification(
                    namespace=payload.namespace,
                    source_record_id=payload.source_record_id,
                    verified_by="codex_adapter",
                    verified_at=datetime.now(timezone.utc),
                ),
            )
            assert result.status == "inserted"

    result = runner.invoke(
        app,
        ["--config", str(config), "recall", "--stdin-json", "--format", "json"],
        input=json.dumps(
            {
                "cwd": str(project),
                "task": "cache method",
                "namespace": {"source_agent": "codex", "model_id": "gpt-5"},
                "max_tokens": 128,
            }
        ),
        env=_live_codex_env(tmp_path, project, "gpt-5"),
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["estimated_tokens"] <= 128
    assert "OWN_NAMESPACE_METHOD" in payload["text"]
    assert "FOREIGN_NAMESPACE_SECRET" not in payload["text"]


def test_recall_cli_never_builds_the_write_capable_container(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
        payload = CapturePayload.model_validate(
            {**_capture_json(project), "outcome": "READONLY_BUILDER_ONLY"}
        )
        assert (
            container.capture.capture(
                payload,
                NamespaceVerification(
                    namespace=payload.namespace,
                    source_record_id=payload.source_record_id,
                    verified_by="codex_adapter",
                    verified_at=datetime.now(timezone.utc),
                ),
            ).status
            == "inserted"
        )

    def unexpected_write_builder(_config_path):
        raise AssertionError("write-capable container must not be built")

    monkeypatch.setattr(cli_module, "build_container", unexpected_write_builder)
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
        input=json.dumps(
            {
                "cwd": str(project),
                "task": "recall exact memory",
                "namespace": {"source_agent": "codex", "model_id": "gpt-5"},
                "max_tokens": 128,
            }
        ),
        env=_live_codex_env(tmp_path, project, "gpt-5"),
    )

    assert result.exit_code == 0, (result.stdout, result.exception)
    assert "READONLY_BUILDER_ONLY" in json.loads(result.stdout)["text"]


def test_recall_cli_requires_reconcile_for_old_schema_without_migrating(
    tmp_path: Path,
) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
        with container.database.transaction() as connection:
            connection.execute("delete from schema_migrations where version = 13")
    database_path = config.parent / "memory.db"
    before = database_path.read_bytes()

    result = runner.invoke(
        app,
        ["--config", str(config), "recall", "--stdin-json", "--format", "json"],
        input=json.dumps(
            {
                "cwd": str(project),
                "task": "recall exact memory",
                "namespace": {"source_agent": "codex", "model_id": "gpt-5"},
            }
        ),
        env=_live_codex_env(tmp_path, project, "gpt-5"),
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "reconcile_required"
    assert database_path.read_bytes() == before
    with Database(database_path).connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from schema_migrations where version = 13"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize(
    ("requested_source", "requested_model"),
    (("codex", "other-model"), ("chatgpt", "gpt-5")),
)
def test_recall_rejects_namespace_not_proven_by_active_codex_context(
    tmp_path: Path,
    requested_source: str,
    requested_model: str,
) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
        for model, outcome in (
            ("gpt-5", "ACTIVE_MODEL_MEMORY"),
            ("other-model", "FOREIGN_MODEL_MEMORY_MUST_NOT_RETURN"),
        ):
            capture_payload = CapturePayload.model_validate(
                {**_capture_json(project, model), "outcome": outcome}
            )
            assert (
                container.capture.capture(
                    capture_payload,
                    NamespaceVerification(
                        namespace=capture_payload.namespace,
                        source_record_id=capture_payload.source_record_id,
                        verified_by="codex_adapter",
                        verified_at=datetime.now(timezone.utc),
                    ),
                ).status
                == "inserted"
            )

    result = runner.invoke(
        app,
        ["--config", str(config), "recall", "--stdin-json", "--format", "json"],
        input=json.dumps(
            {
                "cwd": str(project),
                "task": "cache method",
                "namespace": {
                    "source_agent": requested_source,
                    "model_id": requested_model,
                },
                "max_tokens": 128,
            }
        ),
        env=_live_codex_env(tmp_path, project, "gpt-5"),
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "codex_context_unavailable"
    assert "FOREIGN_MODEL_MEMORY_MUST_NOT_RETURN" not in result.stdout


def test_manual_recall_requires_stdin_owner_token_wrapper(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
        token = LocalAccessToken.load_or_create(container.paths)
        capture_payload = CapturePayload.model_validate(
            {**_capture_json(project, "other-model"), "outcome": "OWNER_SELECTED_MEMORY"}
        )
        assert (
            container.capture.capture(
                capture_payload,
                NamespaceVerification(
                    namespace=capture_payload.namespace,
                    source_record_id=capture_payload.source_record_id,
                    verified_by="codex_adapter",
                    verified_at=datetime.now(timezone.utc),
                ),
            ).status
            == "inserted"
        )
    request = {
        "cwd": str(project),
        "task": "cache method",
        "namespace": {"source_agent": "codex", "model_id": "other-model"},
        "max_tokens": 128,
    }

    allowed = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "recall",
            "--manual",
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps({"token": token, "request": request}),
        env={"CODEX_THREAD_ID": ""},
    )
    denied = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "recall",
            "--manual",
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps({"token": token + "x", "request": request}),
        env={"CODEX_THREAD_ID": ""},
    )
    smuggled = runner.invoke(
        app,
        ["--config", str(config), "recall", "--stdin-json", "--format", "json"],
        input=json.dumps({**request, "token": token}),
        env={"CODEX_THREAD_ID": ""},
    )

    assert allowed.exit_code == 0
    assert "OWNER_SELECTED_MEMORY" in json.loads(allowed.stdout)["text"]
    assert denied.exit_code == 2
    assert json.loads(denied.stdout)["error"]["code"] == "permission_denied"
    assert token not in denied.stdout
    assert smuggled.exit_code == 4
    assert json.loads(smuggled.stdout)["error"]["code"] == "invalid_input"
    assert token not in smuggled.stdout


@pytest.mark.parametrize(
    ("configured_budget", "effective_budget"),
    ((700, 700), (800, 800), (1200, 800)),
)
def test_recall_cli_enforces_configured_and_product_hard_budget(
    tmp_path: Path,
    configured_budget: int,
    effective_budget: int,
) -> None:
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(
        tmp_path,
        root,
        max_recall_tokens=configured_budget,
    )
    with build_container(config) as container:
        _register(container, project)
        capture_payload = CapturePayload.model_validate(
            {
                **_capture_json(project),
                "open_issues": [
                    f"critical cache issue {index} " + ("long-detail " * 50) for index in range(20)
                ],
            }
        )
        assert (
            container.capture.capture(
                capture_payload,
                NamespaceVerification(
                    namespace=capture_payload.namespace,
                    source_record_id=capture_payload.source_record_id,
                    verified_by="codex_adapter",
                    verified_at=datetime.now(timezone.utc),
                ),
            ).status
            == "inserted"
        )

    result = runner.invoke(
        app,
        ["--config", str(config), "recall", "--stdin-json", "--format", "json"],
        input=json.dumps(
            {
                "cwd": str(project),
                "task": "critical cache issue",
                "namespace": {"source_agent": "codex", "model_id": "gpt-5"},
                "max_tokens": 4096,
            }
        ),
        env=_live_codex_env(tmp_path, project, "gpt-5"),
    )

    response = json.loads(result.stdout)
    assert result.exit_code == 0
    assert response["estimated_tokens"] == ConservativeTokenCounter().count(response["text"])
    assert effective_budget // 2 < response["estimated_tokens"] <= effective_budget
    assert "mandatory_content_shortened" in response["warnings"]


@pytest.mark.parametrize("output_format", ["text", "prompt"])
def test_recall_text_formats_emit_only_the_selected_brief(tmp_path, output_format):
    root = tmp_path / "projects"
    project = root / "demo"
    project.mkdir(parents=True)
    config = _private_config(tmp_path, root)
    with build_container(config) as container:
        _register(container, project)
        payload = CapturePayload.model_validate(
            {**_capture_json(project), "outcome": "TRUSTED_RECALL_BRIEF"}
        )
        assert (
            container.capture.capture(
                payload,
                NamespaceVerification(
                    namespace=payload.namespace,
                    source_record_id=payload.source_record_id,
                    verified_by="codex_adapter",
                    verified_at=datetime.now(timezone.utc),
                ),
            ).status
            == "inserted"
        )

    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "recall",
            "--stdin-json",
            "--format",
            output_format,
        ],
        input=json.dumps(
            {
                "cwd": str(project),
                "task": "TASK_DIAGNOSTIC_NEVER_ECHO",
                "namespace": {"source_agent": "codex", "model_id": "gpt-5"},
                "max_tokens": 128,
            }
        ),
        env=_live_codex_env(tmp_path, project, "gpt-5"),
    )

    assert result.exit_code == 0
    assert "TRUSTED_RECALL_BRIEF" in result.stdout
    assert "ok" not in result.stdout.casefold()
    assert "status" not in result.stdout.casefold()
    assert "TASK_DIAGNOSTIC_NEVER_ECHO" not in result.stdout


@pytest.mark.parametrize("value", ["", "{", "{} trailing", "[]", "1", '"x"', "{}"])
def test_bad_stdin_shapes_are_redacted_invalid_input(tmp_path, value):
    secret = "TASK_TEXT_MUST_NOT_ECHO"
    config = tmp_path / "runtime" / "config.toml"
    supplied = value if value != "{}" else json.dumps({"task": secret})

    result = runner.invoke(
        app,
        ["--config", str(config), "recall", "--stdin-json", "--format", "json"],
        input=supplied,
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert secret not in result.stdout
    assert "Traceback" not in result.stdout


def test_stdin_accepts_exact_one_mib_and_rejects_one_byte_more(tmp_path):
    project = tmp_path / "missing-project"
    config = tmp_path / "runtime" / "config.toml"
    base = json.dumps(_capture_json(project), separators=(",", ":")).encode()
    exact = base + b" " * (1024 * 1024 - len(base))

    accepted = runner.invoke(
        app,
        ["--config", str(config), "capture", "--stdin-json", "--format", "json"],
        input=exact,
    )
    rejected = runner.invoke(
        app,
        ["--config", str(config), "capture", "--stdin-json", "--format", "json"],
        input=exact + b" ",
    )

    assert len(exact) == 1024 * 1024
    assert accepted.exit_code == 1
    assert json.loads(accepted.stdout)["error"]["code"] == "project_not_found"
    assert rejected.exit_code == 4
    assert json.loads(rejected.stdout)["error"]["code"] == "invalid_input"


def test_unknown_scan_is_typed_and_does_not_echo_path(tmp_path):
    marker = "UNKNOWN_PROJECT_PRIVATE_MARKER"
    config = tmp_path / "runtime" / "config.toml"

    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "scan",
            "--cwd",
            str(tmp_path / marker),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "project_not_found"
    assert marker not in result.stdout


@pytest.mark.parametrize(
    ("raised", "exit_code", "code"),
    [
        (PermissionError("SENSITIVE_PERMISSION_MARKER"), 2, "permission_denied"),
        (RuntimeError("SENSITIVE_FAILURE_MARKER"), 1, "operation_failed"),
    ],
)
def test_handled_failures_are_generic_and_close_container(
    monkeypatch, tmp_path, raised, exit_code, code
):
    fake = SimpleNamespace(
        project_scanner=SimpleNamespace(discover=lambda: (_ for _ in ()).throw(raised)),
        closed=False,
    )

    def close():
        fake.closed = True

    fake.close = close
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)

    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "config.toml"),
            "discover",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == exit_code
    assert json.loads(result.stdout)["error"]["code"] == code
    assert "SENSITIVE" not in result.stdout
    assert fake.closed is True


def test_chatgpt_import_reports_reconcile_required_without_leaking_details(
    monkeypatch,
    tmp_path: Path,
) -> None:
    marker = "SENSITIVE_RECONCILE_DETAIL"
    fake = SimpleNamespace(
        source_enabled=True,
        chatgpt_adapter=SimpleNamespace(
            import_zip=lambda _path, *, dry_run=False: (_ for _ in ()).throw(
                ReconcileRequiredError(marker)
            )
        ),
        close=lambda: None,
    )
    monkeypatch.setattr(cli_module, "configured_source_enabled", lambda *_args: True)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)

    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "config.toml"),
            "import",
            "chatgpt",
            str(tmp_path / "export.zip"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "reconcile_required"
    assert marker not in result.stdout


def test_chatgpt_import_json_projects_redacted_resolution_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    resolved_marker = "PRIVATE_EXACT_RESOLVED_ISSUE"
    unmatched_marker = "PRIVATE_EXACT_UNMATCHED_ISSUE"
    report = SimpleNamespace(
        confirmation_count=0,
        dry_run=False,
        duplicate_count=1,
        imported_count=2,
        resolved_count=3,
        already_resolved_count=4,
        unmatched_resolution_count=5,
        warning_count=5,
        warnings=(f"resolution_not_found: {unmatched_marker}",),
        results=(SimpleNamespace(evidence=(resolved_marker,)),),
    )
    fake = SimpleNamespace(
        source_enabled=True,
        chatgpt_adapter=SimpleNamespace(import_zip=lambda _path, *, dry_run=False: report),
        close=lambda: None,
    )
    monkeypatch.setattr(cli_module, "configured_source_enabled", lambda *_args: True)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)

    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "config.toml"),
            "import",
            "chatgpt",
            str(tmp_path / "export.zip"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {
        "already_resolved_count": 4,
        "confirmation_count": 0,
        "dry_run": False,
        "duplicate_count": 1,
        "imported_count": 2,
        "resolved_count": 3,
        "status": "ok",
        "unmatched_resolution_count": 5,
        "warning_count": 5,
    }
    assert resolved_marker not in result.stdout
    assert unmatched_marker not in result.stdout


@pytest.mark.parametrize(
    ("raw_count", "expected"),
    (
        (True, 0),
        (1.5, 0),
        ("7", 0),
        (-1, 0),
        (2**40, 2**31 - 1),
    ),
)
def test_chatgpt_import_json_strictly_bounds_report_counts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    raw_count: object,
    expected: int,
) -> None:
    report = SimpleNamespace(
        confirmation_count=0,
        dry_run=False,
        duplicate_count=0,
        imported_count=0,
        resolved_count=raw_count,
        already_resolved_count=raw_count,
        unmatched_resolution_count=raw_count,
        warning_count=raw_count,
        warnings=("one", "two", "three"),
    )
    fake = SimpleNamespace(
        source_enabled=True,
        chatgpt_adapter=SimpleNamespace(import_zip=lambda _path, *, dry_run=False: report),
        close=lambda: None,
    )
    monkeypatch.setattr(cli_module, "configured_source_enabled", lambda *_args: True)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)

    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "config.toml"),
            "import",
            "chatgpt",
            str(tmp_path / "export.zip"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    output = json.loads(result.stdout)
    for key in (
        "resolved_count",
        "already_resolved_count",
        "unmatched_resolution_count",
        "warning_count",
    ):
        assert type(output[key]) is int
        assert output[key] == expected


def test_chatgpt_import_json_falls_back_only_when_warning_count_is_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report = SimpleNamespace(
        confirmation_count=0,
        dry_run=False,
        duplicate_count=0,
        imported_count=0,
        resolved_count=0,
        already_resolved_count=0,
        unmatched_resolution_count=0,
        warnings=("one", "two", "three"),
    )
    fake = SimpleNamespace(
        source_enabled=True,
        chatgpt_adapter=SimpleNamespace(import_zip=lambda _path, *, dry_run=False: report),
        close=lambda: None,
    )
    monkeypatch.setattr(cli_module, "configured_source_enabled", lambda *_args: True)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)

    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "config.toml"),
            "import",
            "chatgpt",
            str(tmp_path / "export.zip"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["warning_count"] == 3


def test_operation_value_error_is_not_misclassified_as_invalid_input(monkeypatch, tmp_path):
    marker = "SENSITIVE_VALUE_ERROR_MARKER"
    fake = SimpleNamespace(
        project_scanner=SimpleNamespace(discover=lambda: (_ for _ in ()).throw(ValueError(marker))),
        close=lambda: None,
    )
    monkeypatch.setattr(cli_module, "build_container", lambda _path: fake)

    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "config.toml"),
            "discover",
            "--dry-run",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert marker not in result.stdout


def test_runtime_path_shape_denial_is_permission_error_without_path_leak(tmp_path):
    marker = "PRIVATE_RUNTIME_FILE_MARKER"
    ordinary_file = tmp_path / marker
    ordinary_file.write_text("not a directory")

    result = runner.invoke(
        app,
        [
            "--config",
            str(ordinary_file / "config.toml"),
            "init",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "permission_denied"
    assert marker not in result.stdout


def test_click_parse_error_is_redacted_and_keeps_json_contract(tmp_path):
    marker = "TASK_SECRET_PARSE_MARKER"
    result = runner.invoke(
        app,
        [
            "--config",
            str(tmp_path / "runtime" / "config.toml"),
            "recall",
            "--stdin-json",
            "--format",
            "json",
            marker,
        ],
        input="{}",
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert marker not in result.stdout
    assert marker not in result.stderr


def test_real_console_parse_error_preserves_json_contract(monkeypatch, capsys):
    marker = "TASK_SECRET_REAL_ENTRY"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memory-hub",
            "recall",
            "--stdin-json",
            "--format",
            "json",
            marker,
        ],
    )

    with pytest.raises(SystemExit) as raised:
        get_command(app).main(args=None, prog_name="memory-hub")

    captured = capsys.readouterr()
    assert raised.value.code == 4
    assert json.loads(captured.out)["error"]["code"] == "invalid_input"
    assert marker not in captured.out
    assert marker not in captured.err


def test_explicit_symlink_runtime_parent_fails_closed(tmp_path):
    marker = "PRIVATE_SYMLINK_TARGET"
    target = tmp_path / marker
    target.mkdir(mode=0o755)
    link = tmp_path / "runtime-link"
    link.symlink_to(target, target_is_directory=True)
    before_mode = _mode(target)

    result = runner.invoke(
        app,
        ["--config", str(link / "config.toml"), "init", "--format", "json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "permission_denied"
    assert marker not in result.stdout
    assert _mode(target) == before_mode == 0o755
    assert list(target.iterdir()) == []


def test_runtime_subdirectory_symlink_fails_closed_without_chmod(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    target = tmp_path / "imports-target"
    target.mkdir(mode=0o755)
    (runtime / "imports").symlink_to(target, target_is_directory=True)
    before_mode = _mode(target)

    result = runner.invoke(
        app,
        ["--config", str(runtime / "config.toml"), "init", "--format", "json"],
    )

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "permission_denied"
    assert _mode(target) == before_mode == 0o755
    assert list(target.iterdir()) == []


def test_help_lists_functional_and_unavailable_commands():
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    for command in ("init", "discover", "scan", "capture", "recall", "version"):
        assert command in result.stdout
    for command in ("import", "reconcile", "compact", "serve", "proposal", "doctor"):
        assert command in result.stdout
    assert "Unavailable in this release" not in result.stdout


def test_pending_recovery_help_exposes_stdin_only_preview_and_apply() -> None:
    result = runner.invoke(app, ["pending", "recover", "--help"])

    assert result.exit_code == 0
    help_text = click.unstyle(result.stdout)
    assert "--stdin-json" in help_text
    assert "Preview or apply" in help_text


def test_serve_help_documents_fixed_loopback_defaults() -> None:
    result = runner.invoke(app, ["serve", "--help"])

    assert result.exit_code == 0
    assert "127.0.0.1" in result.stdout
    assert "8765" in result.stdout
    assert "loopback" in result.stdout.casefold()
    assert "Unavailable in this release" not in result.stdout


def test_serve_runs_uvicorn_without_proxy_or_access_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = _private_config(tmp_path, project)
    captured: dict[str, object] = {}

    def fake_run(application, **kwargs) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(
        app,
        [
            "--config",
            str(config),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "9876",
        ],
    )

    assert result.exit_code == 0
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 9876
    assert captured["access_log"] is False
    assert captured["proxy_headers"] is False
    assert captured["application"].docs_url is None


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.8"])
def test_serve_rejects_non_loopback_before_uvicorn(
    host: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = False

    def fake_run(*_args, **_kwargs) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(uvicorn, "run", fake_run)
    result = runner.invoke(app, ["serve", "--host", host])

    assert result.exit_code == 4
    assert "invalid_input" in result.stdout
    assert called is False
