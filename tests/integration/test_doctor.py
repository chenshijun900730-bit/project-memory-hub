from __future__ import annotations

import json
import os
import stat
import textwrap
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

import project_memory_hub.container as container_module
import project_memory_hub.integration.doctor as doctor_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import DoctorContainer, build_doctor_container
from project_memory_hub.domain import SourceAgent
from project_memory_hub.integration.automation import (
    DesiredAutomation,
    InstallationIdentity,
    InstalledSourceResolution,
)
from project_memory_hub.integration.doctor import (
    DoctorService,
    inspect_graphify_hooks,
)
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.storage.database import Database


NOW = datetime(2026, 7, 15, 3, 30, tzinfo=timezone.utc)


def _config(project_root: Path) -> AppConfig:
    return AppConfig(
        project_roots=(project_root,),
        enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
        inactive_days=21,
        max_recall_tokens=800,
        daily_reconcile_time="03:30",
    )


def _complete_runtime(tmp_path: Path):
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()
    config_path = paths.root / "config.toml"
    project_root = tmp_path / "projects"
    project_root.mkdir()
    config = _config(project_root)
    ConfigManager(config_path).save(config)
    database = Database(paths.database)
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "insert into app_state(name, value_json, updated_at) values (?, ?, ?)",
            (
                "last_reconcile_success",
                json.dumps({"timestamp": "2026-07-15T03:00:00Z"}),
                "2026-07-15T03:00:00Z",
            ),
        )
    sessions = tmp_path / "codex-sessions"
    sessions.mkdir()
    repository = tmp_path / "repository"
    repository.mkdir()
    return paths, config_path, config, sessions, repository


def _service(
    paths: RuntimePaths,
    config_path: Path,
    config: AppConfig | None,
    sessions: Path,
    repository: Path,
    *,
    agents_status: str = "current",
    automation_status: str = "current",
    graphify_status: str = "installed",
) -> DoctorService:
    return DoctorService(
        paths=paths,
        config_path=config_path,
        config=config,
        codex_sessions_path=sessions,
        repository_root=repository,
        agents_status=lambda: agents_status,
        automation_status=lambda: automation_status,
        graphify_status=lambda _root: graphify_status,
        now=lambda: NOW,
    )


def _filesystem_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    if not root.exists():
        return ()
    state: list[tuple[object, ...]] = []
    for path in sorted((root, *root.rglob("*")), key=str):
        metadata = path.lstat()
        state.append(
            (
                "." if path == root else str(path.relative_to(root)),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_uid,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                path.read_bytes() if stat.S_ISREG(metadata.st_mode) else None,
            )
        )
    return tuple(state)


def _write_daily_automation(
    automations_root: Path,
    desired: DesiredAutomation,
) -> None:
    automation_id = "project-memory-hub-daily-reconcile"
    directory = automations_root / automation_id
    directory.mkdir(parents=True)
    document = "\n".join(
        (
            "version = 1",
            f"id = {json.dumps(automation_id)}",
            'kind = "cron"',
            f"name = {json.dumps(desired.name)}",
            f"prompt = {json.dumps(desired.prompt)}",
            'status = "ACTIVE"',
            f"rrule = {json.dumps(desired.rrule)}",
            'execution_environment = "local"',
            'target = { type = "project", project_id = "doctor-project-id" }',
            f"cwds = {json.dumps([str(desired.repository_root)])}",
            "created_at = 1750000000000",
            "updated_at = 1750000000000",
            "",
        )
    )
    (directory / "automation.toml").write_text(document, encoding="utf-8")


def test_build_doctor_container_maps_stable_identity_and_is_fully_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, config_path, config_value, _sessions, repository = _complete_runtime(tmp_path)
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=config_value.project_roots,
            enabled_sources=config_value.enabled_sources,
            inactive_days=config_value.inactive_days,
            max_recall_tokens=config_value.max_recall_tokens,
            daily_reconcile_time="04:45",
            codex_project_id="expected-project-id",
        )
    )
    home = tmp_path / "home"
    (home / ".codex" / "sessions").mkdir(parents=True)
    launcher_parent = tmp_path / "bin"
    launcher_parent.mkdir()
    launcher = launcher_parent / "memory-hub"
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    identity = InstallationIdentity(
        launcher=launcher,
        repository_root=repository,
        repository_device=repository.stat().st_dev,
        repository_inode=repository.stat().st_ino,
    )
    calls: dict[str, object] = {}

    class StableIdentity:
        @staticmethod
        def discover() -> InstallationIdentity:
            return identity

    class InspectAgents:
        def __init__(self, selected_launcher: Path) -> None:
            calls["agents_launcher"] = selected_launcher

        def inspect(self, target: Path) -> SimpleNamespace:
            calls["agents_target"] = target
            return SimpleNamespace(status="current")

    class InspectAutomation:
        def __init__(self, automations_root: Path) -> None:
            calls["automations_root"] = automations_root

        def inspect(self, desired) -> SimpleNamespace:
            calls["desired"] = desired
            return SimpleNamespace(status="current")

    def inspect_graphify(
        repository_root: Path,
        *,
        expected_repository_identity: tuple[int, int] | None = None,
    ) -> str:
        calls["graphify_root"] = repository_root
        calls["graphify_identity"] = expected_repository_identity
        return "installed"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(container_module, "InstallationIdentity", StableIdentity)
    monkeypatch.setattr(container_module, "AgentsIntegration", InspectAgents)
    monkeypatch.setattr(container_module, "AutomationInspector", InspectAutomation)
    monkeypatch.setattr(container_module, "inspect_graphify_hooks", inspect_graphify)
    monkeypatch.setattr(
        container_module,
        "DoctorService",
        lambda **kwargs: DoctorService(**kwargs, now=lambda: NOW),
    )
    before = _filesystem_snapshot(tmp_path)

    with build_doctor_container(config_path) as container:
        assert isinstance(container, DoctorContainer)
        report = container.doctor.run()
    container.close()

    assert report.status == "pass"
    assert calls["agents_launcher"] == launcher
    assert calls["agents_target"] == home / ".codex" / "AGENTS.md"
    assert calls["automations_root"] == home / ".codex" / "automations"
    assert calls["graphify_identity"] == (
        identity.repository_device,
        identity.repository_inode,
    )
    desired = calls["desired"]
    assert desired.timezone == "Asia/Shanghai"
    assert desired.local_time == "04:45"
    assert desired.repository_root == repository
    assert desired.launcher == launcher
    assert desired.project_id == "expected-project-id"
    assert calls["graphify_root"] == repository
    assert _filesystem_snapshot(tmp_path) == before
    assert not Path(f"{paths.database}-wal").exists()
    assert not Path(f"{paths.database}-shm").exists()
    assert not Path(f"{paths.database}-journal").exists()


def test_build_doctor_container_missing_runtime_is_zero_write_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_root = tmp_path / "missing-runtime"
    config_path = runtime_root / "config.toml"
    missing_home = tmp_path / "missing-home"

    class MissingIdentity:
        @staticmethod
        def discover() -> None:
            return None

    def unexpected(*_args, **_kwargs):
        raise AssertionError("write or identity-bound inspection was attempted")

    monkeypatch.setenv("HOME", str(missing_home))
    monkeypatch.setattr(container_module, "InstallationIdentity", MissingIdentity)
    monkeypatch.setattr(container_module, "AgentsIntegration", unexpected)
    monkeypatch.setattr(container_module, "AutomationInspector", unexpected)
    monkeypatch.setattr(container_module, "inspect_graphify_hooks", unexpected)
    monkeypatch.setattr(container_module, "_validate_existing_private_file", unexpected)
    monkeypatch.setattr(container_module.RuntimePaths, "ensure", unexpected)
    monkeypatch.setattr(container_module.Database, "initialize", unexpected)
    before = _filesystem_snapshot(tmp_path)

    container = build_doctor_container(config_path)
    report = container.doctor.run()

    assert report.check("managed_agents").status == "fail"
    assert report.check("codex_automation").status == "fail"
    assert report.check("graphify_hooks").status == "fail"
    assert report.check("enabled_adapters").code == "config_unavailable"
    assert container.doctor._repository_root.is_absolute()
    assert not container.doctor._repository_root.exists()
    assert str(tmp_path) not in report.as_json()
    assert _filesystem_snapshot(tmp_path) == before
    assert not runtime_root.exists()
    assert not missing_home.exists()


def test_build_doctor_container_treats_installed_distribution_integrations_as_optional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, config_path, _config_value, _sessions, _repository = _complete_runtime(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    launcher = (tmp_path / "uv-tool" / "bin" / "memory-hub").absolute()
    calls: dict[str, Path] = {}

    class InstalledDistributionWithoutSourceIdentity:
        @staticmethod
        def discover() -> None:
            return None

        @staticmethod
        def discover_launcher() -> Path:
            return launcher

        @staticmethod
        def is_installed_distribution() -> bool:
            return True

        @staticmethod
        def resolve_installed_source(*, launcher: Path) -> InstalledSourceResolution:
            return InstalledSourceResolution(status="not-local-source")

    class InspectInstalledAgents:
        def __init__(self, selected_launcher: Path) -> None:
            calls["launcher"] = selected_launcher

        def inspect(self, target: Path) -> SimpleNamespace:
            calls["target"] = target
            return SimpleNamespace(status="current")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        container_module,
        "InstallationIdentity",
        InstalledDistributionWithoutSourceIdentity,
    )
    monkeypatch.setattr(container_module, "AgentsIntegration", InspectInstalledAgents)
    before = _filesystem_snapshot(tmp_path)

    report = build_doctor_container(config_path).doctor.run()

    assert report.status in {"pass", "warn"}
    assert all(check.status != "fail" for check in report.checks)
    assert report.check("codex_sessions").status == "warn"
    assert report.check("codex_sessions").code == "codex_sessions_missing"
    assert report.check("managed_agents").status == "pass"
    assert report.check("managed_agents").code == "managed_agents_current"
    assert report.check("codex_automation").status == "warn"
    assert report.check("codex_automation").code == "codex_automation_missing"
    assert report.check("graphify_hooks").status == "warn"
    assert report.check("graphify_hooks").code == "graphify_hooks_missing"
    assert calls == {
        "launcher": launcher,
        "target": home / ".codex" / "AGENTS.md",
    }
    assert _filesystem_snapshot(tmp_path) == before
    assert not Path(f"{paths.database}-wal").exists()
    assert not Path(f"{paths.database}-shm").exists()


def test_installed_distribution_uses_independent_source_binding_for_integrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, config_path, config, _sessions, repository = _complete_runtime(tmp_path)
    config = replace(config, codex_project_id="doctor-project-id")
    ConfigManager(config_path).save(config)
    (repository / ".git").mkdir()
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "project-memory-hub"\n',
        encoding="utf-8",
    )
    module_path = repository / "src" / "project_memory_hub" / "__init__.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("", encoding="utf-8")

    home = tmp_path / "home"
    codex_root = home / ".codex"
    codex_root.mkdir(parents=True)
    launcher = tmp_path / "uv-tool" / "bin" / "memory-hub"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    desired = DesiredAutomation.daily_reconcile(
        local_time=config.daily_reconcile_time,
        repository_root=repository,
        launcher=launcher,
        project_id=config.codex_project_id,
    )
    _write_daily_automation(codex_root / "automations", desired)
    repository_metadata = repository.stat()
    identity = InstallationIdentity(
        launcher=launcher,
        repository_root=repository,
        repository_device=repository_metadata.st_dev,
        repository_inode=repository_metadata.st_ino,
    )

    class InstalledDistributionWithoutSourceIdentity:
        @staticmethod
        def discover() -> None:
            return None

        @staticmethod
        def discover_launcher() -> Path:
            return launcher

        @staticmethod
        def is_installed_distribution() -> bool:
            return True

        @staticmethod
        def resolve_installed_source(*, launcher: Path) -> InstalledSourceResolution:
            assert launcher == identity.launcher
            return InstalledSourceResolution(status="trusted", identity=identity)

    graphify_calls: list[tuple[Path, tuple[int, int] | None]] = []

    def inspect_graphify(
        repository_root: Path,
        *,
        expected_repository_identity: tuple[int, int] | None = None,
    ) -> str:
        graphify_calls.append((repository_root, expected_repository_identity))
        return "installed"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        container_module,
        "InstallationIdentity",
        InstalledDistributionWithoutSourceIdentity,
    )
    monkeypatch.setattr(container_module, "inspect_graphify_hooks", inspect_graphify)
    before = _filesystem_snapshot(tmp_path)

    report = build_doctor_container(config_path).doctor.run()

    assert report.check("codex_automation").status == "pass"
    assert report.check("codex_automation").code == "codex_automation_current"
    assert report.check("graphify_hooks").status == "pass"
    assert report.check("graphify_hooks").code == "graphify_hooks_installed"
    assert graphify_calls == [
        (repository, (repository_metadata.st_dev, repository_metadata.st_ino))
    ]
    assert _filesystem_snapshot(tmp_path) == before
    assert not Path(f"{paths.database}-wal").exists()
    assert not Path(f"{paths.database}-shm").exists()


def test_installed_distribution_checks_graphify_independently_of_drifted_automation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, config_path, _config, _sessions, repository = _complete_runtime(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    launcher = tmp_path / "uv-tool" / "bin" / "memory-hub"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    metadata = repository.stat()
    identity = InstallationIdentity(
        launcher=launcher,
        repository_root=repository,
        repository_device=metadata.st_dev,
        repository_inode=metadata.st_ino,
    )

    class InstalledSource:
        @staticmethod
        def discover() -> None:
            return None

        @staticmethod
        def discover_launcher() -> Path:
            return launcher

        @staticmethod
        def is_installed_distribution() -> bool:
            return True

        @staticmethod
        def resolve_installed_source(*, launcher: Path) -> InstalledSourceResolution:
            return InstalledSourceResolution(status="trusted", identity=identity)

    class DriftedAutomation:
        def __init__(self, _root: Path) -> None:
            pass

        def inspect(self, _desired) -> SimpleNamespace:
            return SimpleNamespace(status="drifted")

    graphify_calls: list[Path] = []

    def inspect_graphify(repository_root: Path, **_kwargs) -> str:
        graphify_calls.append(repository_root)
        return "installed"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(container_module, "InstallationIdentity", InstalledSource)
    monkeypatch.setattr(container_module, "AutomationInspector", DriftedAutomation)
    monkeypatch.setattr(container_module, "inspect_graphify_hooks", inspect_graphify)

    report = build_doctor_container(config_path).doctor.run()

    assert report.check("codex_automation").status == "fail"
    assert report.check("graphify_hooks").status == "pass"
    assert graphify_calls == [repository]


def test_installed_distribution_fails_closed_when_desired_automation_is_invalid(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, config_path, _config, _sessions, repository = _complete_runtime(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    launcher = tmp_path / "uv-tool" / "bin" / "memory-hub"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"#!/bin/sh\nexit 0\n")
    launcher.chmod(0o700)
    metadata = repository.stat()
    identity = InstallationIdentity(
        launcher=launcher,
        repository_root=repository,
        repository_device=metadata.st_dev,
        repository_inode=metadata.st_ino,
    )

    class InstalledSource:
        @staticmethod
        def discover() -> None:
            return None

        @staticmethod
        def discover_launcher() -> Path:
            return launcher

        @staticmethod
        def is_installed_distribution() -> bool:
            return True

        @staticmethod
        def resolve_installed_source(*, launcher: Path) -> InstalledSourceResolution:
            return InstalledSourceResolution(status="trusted", identity=identity)

    class InvalidDesiredAutomation:
        @staticmethod
        def daily_reconcile(**_kwargs):
            raise ValueError("invalid desired automation")

    graphify_calls: list[Path] = []

    def inspect_graphify(repository_root: Path, **_kwargs) -> str:
        graphify_calls.append(repository_root)
        return "installed"

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(container_module, "InstallationIdentity", InstalledSource)
    monkeypatch.setattr(container_module, "DesiredAutomation", InvalidDesiredAutomation)
    monkeypatch.setattr(container_module, "inspect_graphify_hooks", inspect_graphify)

    report = build_doctor_container(config_path).doctor.run()

    assert report.check("codex_automation").status == "fail"
    assert report.check("codex_automation").code == "codex_automation_drifted"
    assert report.check("graphify_hooks").status == "pass"
    assert report.check("graphify_hooks").code == "graphify_hooks_installed"
    assert graphify_calls == [repository]


def test_installed_distribution_fails_closed_for_invalid_source_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, config_path, _config, _sessions, _repository = _complete_runtime(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    launcher = tmp_path / "uv-tool" / "bin" / "memory-hub"

    class InvalidInstalledSource:
        @staticmethod
        def discover() -> None:
            return None

        @staticmethod
        def discover_launcher() -> Path:
            return launcher

        @staticmethod
        def is_installed_distribution() -> bool:
            return True

        @staticmethod
        def resolve_installed_source(*, launcher: Path) -> InstalledSourceResolution:
            return InstalledSourceResolution(status="invalid")

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(container_module, "InstallationIdentity", InvalidInstalledSource)

    report = build_doctor_container(config_path).doctor.run()

    assert report.check("codex_automation").status == "fail"
    assert report.check("graphify_hooks").status == "fail"


def test_installed_distribution_keeps_unsafe_codex_sessions_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, config_path, _config_value, _sessions, _repository = _complete_runtime(tmp_path)
    home = tmp_path / "home"
    codex_root = home / ".codex"
    codex_root.mkdir(parents=True)
    outside_sessions = tmp_path / "outside-sessions"
    outside_sessions.mkdir()
    (codex_root / "sessions").symlink_to(outside_sessions, target_is_directory=True)

    class InstalledDistributionWithoutSourceIdentity:
        @staticmethod
        def discover() -> None:
            return None

        @staticmethod
        def is_installed_distribution() -> bool:
            return True

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        container_module,
        "InstallationIdentity",
        InstalledDistributionWithoutSourceIdentity,
    )

    report = build_doctor_container(config_path).doctor.run()

    assert report.status == "fail"
    assert report.check("codex_sessions").status == "fail"
    assert report.check("codex_sessions").code == "codex_sessions_unreadable"


@pytest.mark.parametrize("bad_config", ("malformed", "public"))
def test_build_doctor_container_treats_bad_config_as_unavailable_without_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_config: str,
) -> None:
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    config_path = root / "config.toml"
    if bad_config == "malformed":
        config_path.write_text("not valid toml = [", encoding="utf-8")
        config_path.chmod(0o600)
    else:
        ConfigManager(config_path).save(AppConfig.defaults(tmp_path))
        config_path.chmod(0o644)

    class MissingIdentity:
        @staticmethod
        def discover() -> None:
            return None

    monkeypatch.setattr(container_module, "InstallationIdentity", MissingIdentity)
    before = _filesystem_snapshot(tmp_path)

    report = build_doctor_container(config_path).doctor.run()

    assert report.check("enabled_adapters").code == "config_unavailable"
    assert _filesystem_snapshot(tmp_path) == before


def test_doctor_missing_runtime_is_strictly_read_only_and_reports_all_checks(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "missing-runtime")
    config_path = paths.root / "config.toml"
    before = _filesystem_snapshot(tmp_path)

    report = _service(
        paths,
        config_path,
        None,
        tmp_path / "missing-sessions",
        tmp_path / "missing-repository",
        agents_status="missing",
        automation_status="missing",
        graphify_status="missing",
    ).run()

    assert _filesystem_snapshot(tmp_path) == before
    assert report.status == "fail"
    assert {check.name for check in report.checks} == {
        "runtime_permissions",
        "database_quick_check",
        "migration_version",
        "fts5",
        "codex_sessions",
        "chatgpt_imports",
        "enabled_adapters",
        "retry_queue",
        "last_reconcile",
        "managed_agents",
        "graphify_hooks",
        "codex_automation",
    }
    assert report.check("managed_agents").status == "warn"
    assert report.check("codex_automation").status == "warn"
    assert not paths.root.exists()


def test_doctor_complete_runtime_passes_without_mutating_files(tmp_path: Path) -> None:
    paths, config_path, config, sessions, repository = _complete_runtime(tmp_path)
    before = _filesystem_snapshot(tmp_path)

    report = _service(paths, config_path, config, sessions, repository).run()

    assert report.status == "pass"
    assert all(check.status == "pass" for check in report.checks)
    assert _filesystem_snapshot(tmp_path) == before


def test_doctor_detects_database_version_drift_and_corruption(tmp_path: Path) -> None:
    paths, config_path, config, sessions, repository = _complete_runtime(tmp_path)
    database = Database(paths.database)
    with database.transaction() as connection:
        connection.execute(
            "insert into schema_migrations(version, applied_at) values (999, ?)",
            (NOW.isoformat(),),
        )

    future = _service(paths, config_path, config, sessions, repository).run()
    assert future.check("migration_version").status == "fail"
    assert future.check("migration_version").code == "schema_version_mismatch"

    paths.database.write_bytes(b"not a sqlite database")
    os.chmod(paths.database, 0o600)
    corrupt = _service(paths, config_path, config, sessions, repository).run()
    assert corrupt.check("database_quick_check").status == "fail"
    assert corrupt.check("fts5").status == "fail"
    assert corrupt.check("retry_queue").status == "fail"
    assert corrupt.check("last_reconcile").status == "fail"


def test_doctor_reports_permission_symlink_and_unavailable_adapter_failures(
    tmp_path: Path,
) -> None:
    paths, config_path, config, sessions, repository = _complete_runtime(tmp_path)
    outside = tmp_path / "outside-imports"
    outside.mkdir()
    paths.imports.rmdir()
    paths.imports.symlink_to(outside, target_is_directory=True)
    config_path.chmod(0o644)
    unsafe_config = AppConfig(
        project_roots=config.project_roots,
        enabled_sources=(SourceAgent.CODEX, SourceAgent.TRAE),
        inactive_days=config.inactive_days,
        max_recall_tokens=config.max_recall_tokens,
        daily_reconcile_time=config.daily_reconcile_time,
    )

    report = _service(
        paths,
        config_path,
        unsafe_config,
        sessions,
        repository,
    ).run()

    assert report.check("runtime_permissions").status == "fail"
    assert report.check("chatgpt_imports").status == "fail"
    assert report.check("enabled_adapters").status == "fail"
    assert str(tmp_path) not in report.as_json()


def test_doctor_reports_old_retry_and_bad_reconcile_state_without_payload_leaks(
    tmp_path: Path,
) -> None:
    paths, config_path, config, sessions, repository = _complete_runtime(tmp_path)
    secret = "TOPSECRETTOKEN123456789"
    database = Database(paths.database)
    with database.transaction() as connection:
        connection.execute(
            "insert into retry_items(retry_id, payload_json, reason_code, created_at) "
            "values (?, ?, ?, ?)",
            (
                str(uuid4()),
                json.dumps({"secret": secret}),
                "transient_failure",
                (NOW - timedelta(days=8)).isoformat(),
            ),
        )
        connection.execute(
            "update app_state set value_json = ? where name = ?",
            (f'{{"timestamp":"{secret}"}}', "last_reconcile_success"),
        )

    report = _service(paths, config_path, config, sessions, repository).run()
    document = report.as_json()

    assert report.check("retry_queue").status == "fail"
    assert report.check("retry_queue").code == "retry_items_stale"
    assert report.check("last_reconcile").status == "fail"
    assert secret not in document
    assert str(paths.database) not in document


def test_doctor_bounds_oversized_reconcile_state_and_migration_rows(
    tmp_path: Path,
) -> None:
    paths, config_path, config, sessions, repository = _complete_runtime(tmp_path)
    database = Database(paths.database)
    with database.transaction() as connection:
        connection.execute(
            "update app_state set value_json = ? where name = ?",
            ("X" * 70_000, "last_reconcile_success"),
        )
        connection.executemany(
            "insert into schema_migrations(version, applied_at) values (?, ?)",
            ((10_000 + index, NOW.isoformat()) for index in range(1_000)),
        )

    report = _service(paths, config_path, config, sessions, repository).run()

    assert report.check("last_reconcile").code == "reconcile_state_invalid"
    assert report.check("migration_version").code == "schema_version_mismatch"
    assert "XXXXX" not in report.as_json()


def test_doctor_external_statuses_are_mapped_fail_closed(tmp_path: Path) -> None:
    paths, config_path, config, sessions, repository = _complete_runtime(tmp_path)

    report = _service(
        paths,
        config_path,
        config,
        sessions,
        repository,
        agents_status="malformed",
        automation_status="duplicate",
        graphify_status="timeout",
    ).run()

    assert report.check("managed_agents").status == "fail"
    assert report.check("codex_automation").status == "fail"
    assert report.check("graphify_hooks").status == "fail"
    assert all(
        stat.S_IMODE(path.lstat().st_mode) in {0o600, 0o700, 0o755}
        for path in (paths.root, config_path, paths.database, sessions, repository)
    )


def _executable(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "graphify"
    executable.write_text(
        "#!/bin/sh\n" + textwrap.dedent(body),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable


def _write_graphify_hooks(
    repository: Path,
    hooks_relative: Path = Path(".git/hooks"),
) -> None:
    (repository / ".git").mkdir(parents=True, exist_ok=True)
    hooks = repository / hooks_relative
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "post-commit").write_text(
        "#!/bin/sh\n# graphify-hook-start\n",
        encoding="utf-8",
    )
    (hooks / "post-checkout").write_text(
        "#!/bin/sh\n# graphify-checkout-hook-start\n",
        encoding="utf-8",
    )


def test_graphify_inspection_never_executes_the_graphify_launcher(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    marker = tmp_path / "graphify-ran"
    executable = _executable(
        tmp_path,
        f"touch {marker}\nprintf '%s\\n' 'post-commit: installed' 'post-checkout: installed'\n",
    )

    status = inspect_graphify_hooks(repository, executable=executable)

    assert status == "installed"
    assert not marker.exists()


def test_graphify_inspection_reads_exact_default_hook_markers(tmp_path: Path) -> None:
    repository = tmp_path / "repository; touch escaped"
    _write_graphify_hooks(repository)

    status = inspect_graphify_hooks(repository)

    assert status == "installed"
    assert not (tmp_path / "escaped").exists()


def test_graphify_inspection_rejects_a_replaced_repository_identity(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    original = repository.stat()
    moved = tmp_path / "moved-repository"
    repository.rename(moved)
    repository.mkdir()

    status = inspect_graphify_hooks(
        repository,
        expected_repository_identity=(original.st_dev, original.st_ino),
    )

    assert status == "unavailable"


def test_graphify_inspection_reports_missing_and_untrusted_hook_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    checkout = repository / ".git" / "hooks" / "post-checkout"
    checkout.unlink()
    assert inspect_graphify_hooks(repository) == "missing"

    checkout.write_bytes(b"\xff\xfe")
    assert inspect_graphify_hooks(repository) == "unavailable"

    checkout.write_bytes(b"x" * (128 * 1024 + 1))
    assert inspect_graphify_hooks(repository) == "unavailable"


def test_graphify_inspection_rejects_hardlinks_and_writable_parents(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    hook = repository / ".git" / "hooks" / "post-commit"
    hardlink = tmp_path / "post-commit-hardlink"
    os.link(hook, hardlink)

    assert inspect_graphify_hooks(repository) == "unavailable"

    hardlink.unlink()
    hooks = repository / ".git" / "hooks"
    hooks.chmod(0o777)
    try:
        assert inspect_graphify_hooks(repository) == "unavailable"
    finally:
        hooks.chmod(0o700)


def test_graphify_inspection_supports_repository_local_husky_hooks_path(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository, Path(".husky"))
    (repository / ".git" / "config").write_text(
        "[core]\n\thooksPath = .husky/_\n",
        encoding="utf-8",
    )

    assert inspect_graphify_hooks(repository) == "installed"


@pytest.mark.parametrize("configured", (".", "_"))
def test_graphify_inspection_supports_repository_root_hooks_path(
    tmp_path: Path,
    configured: str,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository, Path("."))
    (repository / ".git" / "config").write_text(
        f"[Core]\n\thooksPath = {configured}\n",
        encoding="utf-8",
    )

    assert inspect_graphify_hooks(repository) == "installed"


@pytest.mark.parametrize("configured", ("../outside", "/tmp/outside", ".husky/../hooks"))
def test_graphify_inspection_rejects_hooks_paths_outside_the_repository(
    tmp_path: Path,
    configured: str,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    (repository / ".git" / "config").write_text(
        f"[core]\n\thooksPath = {configured}\n",
        encoding="utf-8",
    )

    assert inspect_graphify_hooks(repository) == "unavailable"


def test_graphify_inspection_rejects_a_symlinked_hooks_path(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    outside = tmp_path / "outside-hooks"
    outside.mkdir()
    (repository / ".husky").symlink_to(outside, target_is_directory=True)
    (repository / ".git" / "config").write_text(
        "[core]\n\thooksPath = .husky\n",
        encoding="utf-8",
    )

    assert inspect_graphify_hooks(repository) == "unavailable"


def test_graphify_inspection_rejects_a_hook_replaced_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    hook = repository / ".git" / "hooks" / "post-commit"
    parked = repository / ".git" / "hooks" / "post-commit-parked"
    real_read = doctor_module.os.read
    replaced = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(descriptor, size)
        if chunk and not replaced:
            hook.rename(parked)
            hook.write_text("#!/bin/sh\n# graphify-hook-start\n", encoding="utf-8")
            replaced = True
        return chunk

    monkeypatch.setattr(doctor_module.os, "read", racing_read)

    assert inspect_graphify_hooks(repository) == "unavailable"
    assert replaced is True


def test_graphify_inspection_rejects_a_hooks_directory_replaced_while_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    hooks = repository / ".git" / "hooks"
    parked = repository / ".git" / "hooks-parked"
    real_read = doctor_module.os.read
    replaced = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = real_read(descriptor, size)
        if chunk and not replaced:
            hooks.rename(parked)
            _write_graphify_hooks(repository)
            replaced = True
        return chunk

    monkeypatch.setattr(doctor_module.os, "read", racing_read)

    assert inspect_graphify_hooks(repository) == "unavailable"
    assert replaced is True


def test_graphify_inspection_revalidates_both_hooks_after_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    hook = repository / ".git" / "hooks" / "post-commit"
    parked = repository / ".git" / "hooks" / "post-commit-parked"
    real_read = doctor_module.os.read
    nonempty_reads = 0

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal nonempty_reads
        chunk = real_read(descriptor, size)
        if chunk:
            nonempty_reads += 1
            if nonempty_reads == 2:
                hook.rename(parked)
                hook.write_text("#!/bin/sh\n# graphify-hook-start\n", encoding="utf-8")
        return chunk

    monkeypatch.setattr(doctor_module.os, "read", racing_read)

    assert inspect_graphify_hooks(repository) == "unavailable"
    assert nonempty_reads == 2


def test_graphify_inspection_rejects_a_foreign_owned_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    _write_graphify_hooks(repository)
    real_fstat = doctor_module.os.fstat

    def foreign_directory_owner(descriptor: int):
        metadata = real_fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            return metadata
        return SimpleNamespace(
            st_mode=metadata.st_mode,
            st_uid=os.getuid() + 10_000,
            st_dev=metadata.st_dev,
            st_ino=metadata.st_ino,
        )

    monkeypatch.setattr(doctor_module.os, "fstat", foreign_directory_owner)

    assert inspect_graphify_hooks(repository) == "unavailable"
