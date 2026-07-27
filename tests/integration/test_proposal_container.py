from __future__ import annotations

import json
import shutil
import stat
import subprocess
from dataclasses import replace
from pathlib import Path
from uuid import UUID

import pytest
from typer.testing import CliRunner

import project_memory_hub.improvement.service as proposal_service_module
from project_memory_hub.cli import app
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import (
    build_container,
    build_readonly_proposal_container,
)
from project_memory_hub.domain import SourceAgent
from project_memory_hub.improvement.analyzer import ImprovementAnalyzer
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.services.control import ControlInputError, ControlPanelService


def _save_config(
    tmp_path: Path,
    *,
    repository_root: Path | None = None,
    commands: tuple[tuple[str, ...], ...] = (),
) -> Path:
    project_root = tmp_path / "projects"
    project_root.mkdir(exist_ok=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    path = runtime / "config.toml"
    ConfigManager(path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
            improvement_repository_root=repository_root,
            improvement_verification_commands=commands,
        )
    )
    return path


def _non_executable_draft(signature: str) -> ProposalDraft:
    return ProposalDraft(
        signature=signature,
        title="Review a local improvement",
        description="This draft deliberately has no executable patch.",
        risk="low",
        patch=None,
        verification_argv=(),
        target_version=None,
        origin="local_cli",
    )


def _execution_unavailable() -> type[Exception]:
    selected = getattr(
        proposal_service_module,
        "ProposalExecutionUnavailable",
        None,
    )
    assert isinstance(selected, type) and issubclass(selected, Exception)
    return selected


def _git_repository(path: Path) -> Path:
    path.mkdir()
    commands = (
        ("init", "-b", "main"),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.invalid"),
    )
    for command in commands:
        subprocess.run(
            ["git", "-C", str(path), *command],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    for command in (("add", "README.md"), ("commit", "-m", "initial")):
        subprocess.run(
            ["git", "-C", str(path), *command],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    return path


def _true_executable() -> str:
    selected = shutil.which("true")
    assert selected is not None
    return str(Path(selected).resolve(strict=True))


def _filesystem_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    for path in sorted(root.rglob("*"), key=lambda item: str(item)):
        metadata = path.lstat()
        payload = path.read_bytes() if path.is_file() else None
        entries.append(
            (
                str(path.relative_to(root)),
                stat.S_IMODE(metadata.st_mode),
                metadata.st_mtime_ns,
                payload,
            )
        )
    return tuple(entries)


def test_container_without_execution_config_keeps_proposal_read_write_facade(
    tmp_path: Path,
) -> None:
    config_path = _save_config(tmp_path)

    with build_container(config_path) as container:
        assert isinstance(container.improvement_analyzer, ImprovementAnalyzer)
        assert container.proposal_applier is None

        created = container.proposal_service.create(
            _non_executable_draft("container.facade.create")
        )
        assert container.proposal_service.get(created.record.proposal_id) == created.record
        assert container.proposal_service.list_summaries()[0].proposal_id == (
            created.record.proposal_id
        )

        approved = container.proposal_service.approve(
            created.record.proposal_id,
            actor="local-test",
        )
        rejected = container.proposal_service.reject(approved.proposal_id)

    assert approved.status == "approved"
    assert rejected.status == "rejected"


@pytest.mark.parametrize("repository_kind", ("missing", "not_git", "empty_commands"))
def test_unavailable_execution_config_does_not_block_container_or_read_side(
    tmp_path: Path,
    repository_kind: str,
) -> None:
    repository = tmp_path / "configured-repository"
    commands: tuple[tuple[str, ...], ...] = ((_true_executable(),),)
    if repository_kind == "not_git":
        repository.mkdir()
    elif repository_kind == "empty_commands":
        _git_repository(repository)
        commands = ()
    config_path = _save_config(
        tmp_path,
        repository_root=repository,
        commands=commands,
    )

    with build_container(config_path) as container:
        assert container.proposal_applier is None
        created = container.proposal_service.create(
            _non_executable_draft(f"container.unavailable.{repository_kind}")
        )
        approved = container.proposal_service.approve(
            created.record.proposal_id,
            actor="local-test",
        )
        assert container.proposal_service.get(approved.proposal_id).status == "approved"
        with pytest.raises(_execution_unavailable()):
            container.proposal_service.apply(approved.proposal_id)
        with pytest.raises(_execution_unavailable()):
            container.proposal_service.rollback(approved.proposal_id)


def test_valid_execution_config_builds_one_optional_applier(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "project-memory-hub")
    command = (_true_executable(),)
    config_path = _save_config(
        tmp_path,
        repository_root=repository,
        commands=(command,),
    )

    with build_container(config_path) as container:
        assert container.proposal_applier is not None
        assert container.proposal_service is not None
        assert isinstance(container.improvement_analyzer, ImprovementAnalyzer)


def test_readonly_proposal_container_previews_without_changing_runtime(
    tmp_path: Path,
) -> None:
    config_path = _save_config(tmp_path)
    with build_container(config_path) as container:
        created = container.proposal_service.create(
            _non_executable_draft("container.readonly.preview")
        ).record

    runtime = config_path.parent
    before = _filesystem_snapshot(runtime)
    with build_readonly_proposal_container(config_path) as readonly:
        assert readonly.proposal_service.list_summaries()[0].proposal_id == (created.proposal_id)
        assert (
            readonly.proposal_service.preview_action(
                created.proposal_id,
                action="approve",
            ).record.status
            == "draft"
        )

    assert _filesystem_snapshot(runtime) == before


def test_readonly_proposal_container_never_repairs_runtime_permissions(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "project-memory-hub")
    config_path = _save_config(
        tmp_path,
        repository_root=repository,
        commands=((_true_executable(),),),
    )
    with build_container(config_path):
        pass
    runtime = config_path.parent
    runtime.chmod(0o750)
    before = _filesystem_snapshot(runtime)

    with build_readonly_proposal_container(config_path) as readonly:
        assert readonly.proposal_applier is None

    assert stat.S_IMODE(runtime.stat().st_mode) == 0o750
    assert _filesystem_snapshot(runtime) == before


def test_real_cli_apply_dry_run_validates_without_changing_runtime_or_git(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "project-memory-hub")
    verification_argv = (_true_executable(),)
    config_path = _save_config(
        tmp_path,
        repository_root=repository,
        commands=(verification_argv,),
    )
    with build_container(config_path) as container:
        created = container.proposal_service.create(
            ProposalDraft(
                signature="container.cli.readonly-apply",
                title="Preview an isolated local patch",
                description="Validate the exact approved proposal without writes.",
                risk="low",
                patch=(
                    "diff --git a/README.md b/README.md\n"
                    "--- a/README.md\n"
                    "+++ b/README.md\n"
                    "@@ -1 +1 @@\n"
                    "-seed\n"
                    "+updated\n"
                ),
                verification_argv=verification_argv,
                target_version=None,
                origin="local_cli",
            )
        ).record
        approved = container.proposal_service.approve(
            created.proposal_id,
            actor="local-test",
        )
        token = LocalAccessToken.load_or_create(container.paths)

    before = _filesystem_snapshot(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "proposal",
            "apply",
            str(approved.proposal_id),
            "--stdin-json",
            "--dry-run",
            "--format",
            "json",
        ],
        input=json.dumps({"token": token}),
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "apply_preview"
    assert payload["verification"] == "partial"
    assert "verification_command_execution" in payload["unverified"]
    assert _filesystem_snapshot(tmp_path) == before


def test_real_cli_apply_dry_run_rejects_an_inapplicable_hunk_without_writes(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "project-memory-hub")
    verification_argv = (_true_executable(),)
    config_path = _save_config(
        tmp_path,
        repository_root=repository,
        commands=(verification_argv,),
    )
    with build_container(config_path) as container:
        created = container.proposal_service.create(
            ProposalDraft(
                signature="container.cli.invalid-hunk",
                title="Reject an inapplicable local patch",
                description="The hunk context does not match the approved base.",
                risk="low",
                patch=(
                    "diff --git a/README.md b/README.md\n"
                    "--- a/README.md\n"
                    "+++ b/README.md\n"
                    "@@ -1 +1 @@\n"
                    "-not-the-current-content\n"
                    "+updated\n"
                ),
                verification_argv=verification_argv,
                target_version=None,
                origin="local_cli",
            )
        ).record
        approved = container.proposal_service.approve(
            created.proposal_id,
            actor="local-test",
        )
        token = LocalAccessToken.load_or_create(container.paths)

    before = _filesystem_snapshot(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "--config",
            str(config_path),
            "proposal",
            "apply",
            str(approved.proposal_id),
            "--stdin-json",
            "--dry-run",
            "--format",
            "json",
        ],
        input=json.dumps({"token": token}),
    )

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    assert _filesystem_snapshot(tmp_path) == before


def test_real_cli_create_approve_and_reject_persist_only_after_token_auth(
    tmp_path: Path,
) -> None:
    config_path = _save_config(tmp_path)
    cli = CliRunner()
    initialized = cli.invoke(
        app,
        ["--config", str(config_path), "init", "--format", "json"],
    )
    assert initialized.exit_code == 0
    with build_container(config_path) as container:
        token = LocalAccessToken.load_existing(container.paths)
    document = {
        "signature": "container.cli.real-mutation",
        "title": "Persist an authenticated local proposal",
        "description": "Exercise the real repository and service facade.",
        "risk": "low",
        "patch": (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1 +1 @@\n"
            "-seed\n"
            "+updated\n"
        ),
        "verification_argv": [_true_executable()],
        "target_version": None,
        "token": token,
    }

    created_result = cli.invoke(
        app,
        [
            "--config",
            str(config_path),
            "proposal",
            "create",
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps(document),
    )
    assert created_result.exit_code == 0
    proposal_id = json.loads(created_result.stdout)["proposal_id"]

    approved_result = cli.invoke(
        app,
        [
            "--config",
            str(config_path),
            "proposal",
            "approve",
            proposal_id,
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps({"token": token}),
    )
    rejected_result = cli.invoke(
        app,
        [
            "--config",
            str(config_path),
            "proposal",
            "reject",
            proposal_id,
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps({"token": token}),
    )

    assert json.loads(approved_result.stdout)["status"] == "approved"
    assert json.loads(rejected_result.stdout)["status"] == "rejected"
    assert token not in (created_result.stdout + approved_result.stdout + rejected_result.stdout)
    with build_container(config_path) as container:
        assert container.proposal_service.get(UUID(proposal_id)).status == "rejected"


def test_real_cli_rejects_terminal_control_metadata_before_persistence(
    tmp_path: Path,
) -> None:
    config_path = _save_config(tmp_path)
    cli = CliRunner()
    assert cli.invoke(app, ["--config", str(config_path), "init"]).exit_code == 0
    with build_container(config_path) as container:
        token = LocalAccessToken.load_existing(container.paths)
    document = {
        "signature": "container.cli.control-sequence",
        "title": "safe\x1b]0;terminal-title\x07text",
        "description": "Reject terminal control characters.",
        "risk": "low",
        "patch": (
            "diff --git a/README.md b/README.md\n"
            "--- a/README.md\n"
            "+++ b/README.md\n"
            "@@ -1 +1 @@\n"
            "-seed\n"
            "+updated\n"
        ),
        "verification_argv": [_true_executable()],
        "target_version": None,
        "token": token,
    }

    result = cli.invoke(
        app,
        [
            "--config",
            str(config_path),
            "proposal",
            "create",
            "--stdin-json",
            "--format",
            "json",
        ],
        input=json.dumps(document),
    )

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert "terminal-title" not in result.stdout + result.stderr
    with build_container(config_path) as container:
        assert container.proposal_service.list_summaries() == ()


def test_control_settings_writes_preserve_improvement_execution_config(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "project-memory-hub")
    commands = ((_true_executable(),),)
    config_path = _save_config(
        tmp_path,
        repository_root=repository,
        commands=commands,
    )

    with build_container(config_path) as container:
        control = ControlPanelService(container)
        control.set_source_enabled(SourceAgent.CHATGPT, False)
        after_source_write = ConfigManager(config_path).load()
        assert after_source_write.improvement_repository_root == repository
        assert after_source_write.improvement_verification_commands == commands

        control.save_settings(
            project_roots=[str(container.config.project_roots[0])],
            enabled_sources=[SourceAgent.CODEX.value],
            inactive_days="30",
            max_recall_tokens="700",
            daily_reconcile_time="04:15",
        )
        after_settings_write = ConfigManager(config_path).load()

    assert after_settings_write.improvement_repository_root == repository
    assert after_settings_write.improvement_verification_commands == commands


def test_control_source_write_preserves_setup_and_host_metadata(tmp_path: Path) -> None:
    config_path = _save_config(tmp_path)
    manager = ConfigManager(config_path)
    manager.save(
        replace(
            manager.load(),
            setup_completed=False,
            codex_project_id="opaque-project-id",
        )
    )

    with build_container(config_path) as container:
        control = ControlPanelService(container)
        control.set_source_enabled(SourceAgent.CHATGPT, False)
        control.save_settings(
            project_roots=[str(container.config.project_roots[0])],
            enabled_sources=[SourceAgent.CODEX.value],
            inactive_days="30",
            max_recall_tokens="700",
            daily_reconcile_time="04:15",
        )

    persisted = manager.load()
    assert persisted.setup_completed is False
    assert persisted.codex_project_id == "opaque-project-id"


def test_control_settings_reject_a_concurrent_config_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _save_config(tmp_path)
    original_save = ConfigManager.save
    injected = False

    with build_container(config_path) as container:

        def racing_save(
            manager: ConfigManager,
            config: AppConfig,
            **kwargs: object,
        ) -> None:
            nonlocal injected
            if not injected:
                injected = True
                original_save(
                    manager,
                    replace(manager.load(), inactive_days=45),
                )
            original_save(manager, config, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(ConfigManager, "save", racing_save)
        control = ControlPanelService(container)
        with pytest.raises(ControlInputError):
            control.save_settings(
                project_roots=[str(container.config.project_roots[0])],
                enabled_sources=[SourceAgent.CODEX.value],
                inactive_days="30",
                max_recall_tokens="700",
                daily_reconcile_time="04:15",
            )

    assert ConfigManager(config_path).load().inactive_days == 45


def test_control_settings_preserve_latest_disk_improvement_config(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "initial-repository")
    latest_repository = tmp_path / "latest-repository"
    command = (_true_executable(),)
    latest_command = (_true_executable(), "--latest")
    config_path = _save_config(
        tmp_path,
        repository_root=repository,
        commands=(command,),
    )

    with build_container(config_path) as container:
        latest = AppConfig(
            project_roots=container.config.project_roots,
            enabled_sources=container.config.enabled_sources,
            inactive_days=container.config.inactive_days,
            max_recall_tokens=container.config.max_recall_tokens,
            daily_reconcile_time=container.config.daily_reconcile_time,
            improvement_repository_root=latest_repository,
            improvement_verification_commands=(latest_command,),
        )
        ConfigManager(config_path).save(latest)
        ControlPanelService(container).save_settings(
            project_roots=[str(container.config.project_roots[0])],
            enabled_sources=[SourceAgent.CODEX.value],
            inactive_days="30",
            max_recall_tokens="700",
            daily_reconcile_time="04:15",
        )

    persisted = ConfigManager(config_path).load()
    assert persisted.improvement_repository_root == latest_repository
    assert persisted.improvement_verification_commands == (latest_command,)
