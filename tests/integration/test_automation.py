from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError

import project_memory_hub.integration.automation as automation_module
from project_memory_hub.integration.automation import (
    AutomationInspector,
    DesiredAutomation,
    InstallationIdentity,
)


def _launcher(path: Path) -> Path:
    path.parent.mkdir(parents=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _uv_launcher(root: Path) -> Path:
    environment = root / "project-memory-hub"
    launcher = _launcher(environment / "bin" / "memory-hub")
    (environment / "pyvenv.cfg").write_text(
        "implementation = CPython\nuv = 0.11.28\n",
        encoding="utf-8",
    )
    return launcher


def _desired(tmp_path: Path) -> DesiredAutomation:
    project_root = tmp_path / "project-memory-hub"
    project_root.mkdir(exist_ok=True)
    launcher = tmp_path / "uv-tools" / "memory-hub"
    if not launcher.exists():
        _launcher(launcher)
    return DesiredAutomation.daily_reconcile(
        repository_root=project_root,
        launcher=launcher,
        project_id="project-memory-hub-id",
    )


def _editable_source(
    root: Path,
    *,
    with_git: bool = True,
) -> Path:
    root.mkdir(parents=True)
    if with_git:
        (root / ".git").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "project-memory-hub"\n',
        encoding="utf-8",
    )
    module_path = root / "src" / "project_memory_hub" / "__init__.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("", encoding="utf-8")
    return module_path


def _installed_module_with_direct_url(
    root: Path,
    source_root: Path,
) -> tuple[Path, Path]:
    site_packages = root / "lib" / "python3.12" / "site-packages"
    module_path = site_packages / "project_memory_hub" / "integration" / "automation.py"
    module_path.parent.mkdir(parents=True)
    module_path.write_text("# installed distribution fixture\n", encoding="utf-8")
    dist_info = site_packages / "project_memory_hub-0.2.1.dist-info"
    dist_info.mkdir()
    direct_url = dist_info / "direct_url.json"
    direct_url.write_text(
        json.dumps({"url": source_root.as_uri(), "dir_info": {}}),
        encoding="utf-8",
    )
    metadata = dist_info / "METADATA"
    metadata.write_text(
        "Metadata-Version: 2.4\nName: project-memory-hub\nVersion: 0.2.1\n",
        encoding="utf-8",
    )

    def record_line(path: str, payload: bytes) -> str:
        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=")
        return f"{path},sha256={digest.decode('ascii')},{len(payload)}"

    record = dist_info / "RECORD"
    record.write_text(
        "\n".join(
            (
                record_line(
                    "project_memory_hub/integration/automation.py",
                    module_path.read_bytes(),
                ),
                record_line(
                    "project_memory_hub-0.2.1.dist-info/direct_url.json",
                    direct_url.read_bytes(),
                ),
                record_line("../../../bin/memory-hub", b"synthetic launcher"),
                "project_memory_hub-0.2.1.dist-info/RECORD,,",
                "",
            )
        ),
        encoding="utf-8",
    )
    return module_path, direct_url


def _rewrite_bound_direct_url(direct_url: Path, document: dict[str, object]) -> None:
    direct_url.write_text(json.dumps(document), encoding="utf-8")
    digest = base64.urlsafe_b64encode(hashlib.sha256(direct_url.read_bytes()).digest()).rstrip(b"=")
    record = direct_url.parent / "RECORD"
    lines = record.read_text(encoding="utf-8").splitlines()
    lines[1] = (
        "project_memory_hub-0.2.1.dist-info/direct_url.json,"
        f"sha256={digest.decode('ascii')},{direct_url.stat().st_size}"
    )
    record.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_automation(
    automations_root: Path,
    automation_id: str,
    desired: DesiredAutomation,
    **overrides: object,
) -> Path:
    values: dict[str, object] = {
        "version": 1,
        "id": automation_id,
        "kind": "cron",
        "name": desired.name,
        "prompt": desired.prompt,
        "status": "ACTIVE",
        "rrule": desired.rrule,
        "execution_environment": desired.execution_environment,
        "target_type": "project",
        "project_id": "project-memory-hub-id",
        "target_thread_id": "thread-id",
        "cwds": [str(desired.repository_root)],
        "created_at": 1_750_000_000_000,
        "updated_at": 1_750_000_000_000,
    }
    values.update(overrides)
    directory = automations_root / automation_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "automation.toml"
    lines: list[str] = [
        f"version = {values['version']}",
        f"id = {json.dumps(values['id'])}",
        f"kind = {json.dumps(values['kind'])}",
        f"name = {json.dumps(values['name'])}",
        f"prompt = {json.dumps(values['prompt'])}",
        f"status = {json.dumps(values['status'])}",
        f"rrule = {json.dumps(values['rrule'])}",
    ]
    if values["kind"] == "heartbeat":
        lines.append(f"target_thread_id = {json.dumps(values['target_thread_id'])}")
    else:
        lines.append(f"execution_environment = {json.dumps(values['execution_environment'])}")
        if values["target_type"] == "project":
            lines.append(
                "target = { type = "
                f"{json.dumps(values['target_type'])}, project_id = "
                f"{json.dumps(values['project_id'])} }}"
            )
        else:
            lines.append(f"target = {{ type = {json.dumps(values['target_type'])} }}")
        lines.append("cwds = " + json.dumps(values["cwds"]))
    lines.extend(
        [
            f"created_at = {values['created_at']}",
            f"updated_at = {values['updated_at']}",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def test_daily_reconcile_has_exact_local_safe_desired_state(tmp_path: Path) -> None:
    project_root = tmp_path / "project-memory-hub"
    project_root.mkdir()
    launcher = _launcher(tmp_path / "uv-tools" / "memory-hub")

    desired = DesiredAutomation.daily_reconcile(
        repository_root=project_root,
        launcher=launcher,
    )

    assert desired.name == "Project Memory Hub Daily Reconcile"
    assert desired.timezone == "Asia/Shanghai"
    assert desired.local_time == "03:30"
    assert desired.repository_root == project_root
    assert desired.project_root == project_root
    assert desired.launcher == launcher
    assert desired.execution_environment == "local"
    assert desired.enabled is True
    assert desired.rrule == (
        "DTSTART;TZID=Asia/Shanghai:19700101T033000\nRRULE:FREQ=DAILY;BYHOUR=3;BYMINUTE=30"
    )
    assert "reconcile_if_due_v1" in desired.prompt
    assert "with {}" in desired.prompt
    assert str(launcher) not in desired.prompt
    assert "reconcile --if-due" not in desired.prompt
    assert "health" in desired.prompt
    assert "counts" in desired.prompt
    assert "blocked paths" in desired.prompt
    assert "confirmation-queue size" in desired.prompt
    assert "never expose conversation content" in desired.prompt.lower()
    assert "stage" in desired.prompt
    assert "confirmed cause" in desired.prompt
    assert "record state" in desired.prompt
    assert "code commit impact" in desired.prompt
    assert "safe remediation" in desired.prompt
    assert "user action" in desired.prompt
    assert "error code is evidence, not a root cause" in desired.prompt
    assert "For this automation, stage is `reconcile`" in desired.prompt
    assert "aggregate counts do not prove an individual record state" in desired.prompt
    assert "Report only" not in desired.prompt


@pytest.mark.parametrize("field", ["repository_root", "launcher"])
def test_daily_reconcile_rejects_worktree_paths(
    tmp_path: Path,
    field: str,
) -> None:
    main_root = tmp_path / "project-memory-hub"
    main_root.mkdir()
    stable_launcher = _launcher(tmp_path / "uv-tools" / "memory-hub")
    worktree_root = tmp_path / "repo" / ".worktrees" / "feature"
    worktree_root.mkdir(parents=True)
    worktree_launcher = _launcher(worktree_root / "bin" / "memory-hub")
    values = {
        "repository_root": main_root,
        "launcher": stable_launcher,
    }
    values[field] = worktree_root if field == "repository_root" else worktree_launcher

    with pytest.raises(ValidationError, match=r"\.worktrees"):
        DesiredAutomation.daily_reconcile(**values)


@pytest.mark.parametrize("unsafe_parent", ["contains`backtick", "contains\nnewline"])
def test_daily_reconcile_rejects_prompt_delimiter_or_control_paths(
    tmp_path: Path,
    unsafe_parent: str,
) -> None:
    project_root = tmp_path / "project-memory-hub"
    project_root.mkdir()
    launcher = _launcher(tmp_path / unsafe_parent / "memory-hub")

    with pytest.raises(ValidationError):
        DesiredAutomation.daily_reconcile(
            repository_root=project_root,
            launcher=launcher,
        )


def test_daily_reconcile_rejects_a_writable_or_noncanonical_launcher(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project-memory-hub"
    project_root.mkdir()
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    launcher.chmod(0o777)

    with pytest.raises(ValidationError):
        DesiredAutomation.daily_reconcile(
            repository_root=project_root,
            launcher=launcher,
        )

    launcher.chmod(0o700)
    linked_parent = tmp_path / "linked-bin"
    linked_parent.symlink_to(launcher.parent, target_is_directory=True)
    with pytest.raises(ValidationError):
        DesiredAutomation.daily_reconcile(
            repository_root=project_root,
            launcher=linked_parent / "memory-hub",
        )


def test_installation_identity_discovers_main_editable_from_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    module_path = _editable_source(repository_root)
    uv_tools_root = tmp_path / "uv-tools"
    launcher = _uv_launcher(uv_tools_root)
    monkeypatch.setenv("PATH", str(launcher.parent))
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools_root))

    identity = InstallationIdentity.discover(module_path=module_path)

    assert identity is not None
    assert identity.launcher == launcher
    assert identity.repository_root == repository_root


def test_installation_identity_resolves_the_uv_path_symlink(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    module_path = _editable_source(repository_root)
    uv_tools_root = tmp_path / "uv-tools"
    launcher = _uv_launcher(uv_tools_root)
    path_entry = tmp_path / "local-bin"
    path_entry.mkdir()
    (path_entry / "memory-hub").symlink_to(launcher)
    monkeypatch.setenv("PATH", str(path_entry))
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools_root))

    identity = InstallationIdentity.discover(module_path=module_path)

    assert identity is not None
    assert identity.launcher == launcher


def test_installation_identity_rejects_a_repo_local_path_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    module_path = _editable_source(repository_root)
    launcher = _launcher(repository_root / ".venv" / "bin" / "memory-hub")
    monkeypatch.setenv("PATH", str(launcher.parent))
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "uv-tools"))

    assert InstallationIdentity.discover(module_path=module_path) is None


@pytest.mark.parametrize("directory", ["environment", "bin"])
def test_installation_identity_rejects_writable_uv_directories(
    tmp_path: Path,
    monkeypatch,
    directory: str,
) -> None:
    repository_root = tmp_path / "source" / "project-memory-hub"
    module_path = _editable_source(repository_root)
    uv_tools_root = tmp_path / "uv-tools"
    launcher = _uv_launcher(uv_tools_root)
    selected = launcher.parents[1] if directory == "environment" else launcher.parent
    selected.chmod(0o777)
    monkeypatch.setenv("PATH", str(launcher.parent))
    monkeypatch.setenv("UV_TOOL_DIR", str(uv_tools_root))

    assert InstallationIdentity.discover(module_path=module_path) is None


@pytest.mark.parametrize("layout", ["worktree", "non_git", "site_packages"])
def test_installation_identity_rejects_unstable_source_layouts(
    tmp_path: Path,
    layout: str,
) -> None:
    if layout == "worktree":
        repository_root = tmp_path / "repo" / ".worktrees" / "feature"
        module_path = _editable_source(repository_root)
    elif layout == "site_packages":
        repository_root = tmp_path / "project-memory-hub"
        repository_root.mkdir()
        (repository_root / ".git").mkdir()
        (repository_root / "pyproject.toml").write_text(
            '[project]\nname = "project-memory-hub"\n',
            encoding="utf-8",
        )
        module_path = (
            repository_root
            / ".venv"
            / "lib"
            / "python3.11"
            / "site-packages"
            / "project_memory_hub"
            / "__init__.py"
        )
        module_path.parent.mkdir(parents=True)
        module_path.write_text("", encoding="utf-8")
    else:
        repository_root = tmp_path / "project-memory-hub"
        module_path = _editable_source(repository_root, with_git=False)
    launcher = _launcher(tmp_path / "bin" / "memory-hub")

    assert (
        InstallationIdentity.discover(
            launcher=launcher,
            module_path=module_path,
        )
        is None
    )


def test_installation_identity_identifies_an_installed_distribution_path(tmp_path: Path) -> None:
    installed_module = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "project_memory_hub"
        / "integration"
        / "automation.py"
    )
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("# installed distribution fixture\n", encoding="utf-8")

    assert InstallationIdentity.is_installed_distribution(module_path=installed_module)
    assert not InstallationIdentity.is_installed_distribution(
        module_path=_editable_source(tmp_path / "source-checkout")
    )


def test_installation_identity_rejects_unsafe_installed_distribution_paths(
    tmp_path: Path,
) -> None:
    installed_module = (
        tmp_path
        / "venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "project_memory_hub"
        / "integration"
        / "automation.py"
    )
    installed_module.parent.mkdir(parents=True)
    installed_module.write_text("# installed distribution fixture\n", encoding="utf-8")
    linked_module = tmp_path / "linked-site-packages" / "automation.py"
    linked_module.parent.mkdir()
    linked_module.symlink_to(installed_module)

    assert not InstallationIdentity.is_installed_distribution(module_path=linked_module)

    installed_module.chmod(0o622)
    assert not InstallationIdentity.is_installed_distribution(module_path=installed_module)


def test_installation_identity_rejects_symlinked_paths(tmp_path: Path) -> None:
    repository_root = tmp_path / "project-memory-hub"
    module_path = _editable_source(repository_root)
    real_launcher = _launcher(tmp_path / "real-bin" / "memory-hub")
    linked_launcher = tmp_path / "bin" / "memory-hub"
    linked_launcher.parent.mkdir()
    linked_launcher.symlink_to(real_launcher)

    assert (
        InstallationIdentity.discover(
            launcher=linked_launcher,
            module_path=module_path,
        )
        is None
    )

    linked_repository = tmp_path / "linked-repository"
    linked_repository.symlink_to(repository_root, target_is_directory=True)
    linked_module = linked_repository / "src" / "project_memory_hub" / "__init__.py"
    assert (
        InstallationIdentity.discover(
            launcher=real_launcher,
            module_path=linked_module,
        )
        is None
    )


def test_installation_identity_rejects_unsafe_launcher(tmp_path: Path) -> None:
    module_path = _editable_source(tmp_path / "project-memory-hub")
    launcher = _launcher(tmp_path / "bin" / "memory-hub")

    launcher.chmod(0o775)
    assert InstallationIdentity.discover(launcher=launcher, module_path=module_path) is None

    launcher.chmod(0o644)
    assert InstallationIdentity.discover(launcher=launcher, module_path=module_path) is None

    launcher.chmod(0o755)
    os.link(launcher, tmp_path / "bin" / "memory-hub-hardlink")
    assert InstallationIdentity.discover(launcher=launcher, module_path=module_path) is None


def test_installation_identity_rejects_non_owner_launcher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    module_path = _editable_source(tmp_path / "project-memory-hub")
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    monkeypatch.setattr(automation_module.os, "getuid", lambda: launcher.stat().st_uid + 1)

    assert InstallationIdentity.discover(launcher=launcher, module_path=module_path) is None


def test_installed_distribution_recovers_source_identity_from_direct_url(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    _editable_source(repository_root)
    module_path, _direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    metadata = repository_root.stat()

    identity = InstallationIdentity.discover_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert identity == InstallationIdentity(
        launcher=launcher,
        repository_root=repository_root,
        repository_device=metadata.st_dev,
        repository_inode=metadata.st_ino,
    )


def test_installed_source_resolution_distinguishes_missing_and_damaged_provenance(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    _editable_source(repository_root)
    module_path, direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    record = direct_url.parent / "RECORD"

    direct_url.unlink()
    record.write_text(
        "\n".join(
            line
            for line in record.read_text(encoding="utf-8").splitlines()
            if "direct_url" not in line
        )
        + "\n",
        encoding="utf-8",
    )
    missing = InstallationIdentity.resolve_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    direct_url.write_text(
        json.dumps({"url": repository_root.as_uri(), "dir_info": {}}),
        encoding="utf-8",
    )
    damaged = InstallationIdentity.resolve_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert missing.status == "not-local-source"
    assert missing.identity is None
    assert damaged.status == "invalid"
    assert damaged.identity is None


def test_installed_source_resolution_accepts_record_bound_wheel_provenance(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    _editable_source(repository_root)
    module_path, direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    _rewrite_bound_direct_url(
        direct_url,
        {
            "url": (tmp_path / "project_memory_hub-0.2.1.whl").as_uri(),
            "archive_info": {},
        },
    )

    resolution = InstallationIdentity.resolve_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert resolution.status == "not-local-source"
    assert resolution.identity is None


@pytest.mark.parametrize(
    "document",
    (
        {"url": "not a url", "archive_info": {}},
        {"url": "https://example.invalid/package.whl\n", "archive_info": {}},
        {"url": "https://example.invalid/package.whl", "archive_info": {"junk": []}},
        {"url": "https://example.invalid/repository", "vcs_info": {"vcs": "git"}},
    ),
    ids=("invalid-url", "control-url", "invalid-archive", "incomplete-vcs"),
)
def test_installed_source_resolution_rejects_malformed_non_directory_provenance(
    tmp_path: Path,
    document: dict[str, object],
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    _editable_source(repository_root)
    module_path, direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    _rewrite_bound_direct_url(direct_url, document)

    resolution = InstallationIdentity.resolve_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert resolution.status == "invalid"
    assert resolution.identity is None


def test_installed_source_resolution_accepts_complete_vcs_provenance(
    tmp_path: Path,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    _editable_source(repository_root)
    module_path, direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    _rewrite_bound_direct_url(
        direct_url,
        {
            "url": "https://example.invalid/repository.git",
            "vcs_info": {
                "vcs": "git",
                "requested_revision": "main",
                "commit_id": "0123456789abcdef0123456789abcdef01234567",
            },
        },
    )

    resolution = InstallationIdentity.resolve_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert resolution.status == "not-local-source"
    assert resolution.identity is None


@pytest.mark.parametrize(
    "tampering",
    ("direct_url", "metadata", "duplicate_dist_info", "record_path"),
)
def test_installed_source_resolution_rejects_distribution_tampering(
    tmp_path: Path,
    tampering: str,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    _editable_source(repository_root)
    module_path, direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    if tampering == "direct_url":
        direct_url.write_text(
            json.dumps({"url": repository_root.as_uri(), "dir_info": {"editable": True}}),
            encoding="utf-8",
        )
    elif tampering == "metadata":
        (direct_url.parent / "METADATA").write_text(
            "Metadata-Version: 2.4\nName: lookalike-project\nVersion: 0.2.1\n",
            encoding="utf-8",
        )
    elif tampering == "duplicate_dist_info":
        (direct_url.parent.parent / "project_memory_hub-9.9.9.dist-info").mkdir()
    else:
        record = direct_url.parent / "RECORD"
        record.write_text(
            record.read_text(encoding="utf-8")
            + "project_memory_hub/../escape.py,sha256=synthetic,1\n",
            encoding="utf-8",
        )

    resolution = InstallationIdentity.resolve_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert resolution.status == "invalid"
    assert resolution.identity is None


def test_installed_source_resolution_rejects_a_worktree_source(tmp_path: Path) -> None:
    repository_root = tmp_path / "repo" / ".worktrees" / "feature"
    _editable_source(repository_root)
    module_path, _direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")

    resolution = InstallationIdentity.resolve_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert resolution.status == "invalid"
    assert resolution.identity is None


def test_installed_source_binding_ignores_an_automation_lookalike_repository(
    tmp_path: Path,
) -> None:
    installed_source = tmp_path / "installed-source"
    _editable_source(installed_source)
    lookalike = tmp_path / "lookalike"
    _editable_source(lookalike)
    module_path, _direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        installed_source,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    desired = DesiredAutomation.daily_reconcile(
        repository_root=lookalike,
        launcher=launcher,
        project_id="project-memory-hub-id",
    )
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "daily", desired)

    identity = InstallationIdentity.discover_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert identity is not None
    assert identity.repository_root == installed_source
    assert (
        AutomationInspector(automations_root)
        .inspect(
            DesiredAutomation.daily_reconcile(
                repository_root=identity.repository_root,
                launcher=launcher,
                project_id=desired.project_id,
            )
        )
        .status
        == "drifted"
    )


@pytest.mark.parametrize(
    "surface",
    ("installed_ancestor", "installed_package", "source_git", "source_module"),
)
def test_installed_source_binding_rejects_writable_identity_surfaces(
    tmp_path: Path,
    surface: str,
) -> None:
    repository_root = tmp_path / "project-memory-hub"
    source_module = _editable_source(repository_root)
    module_path, _direct_url = _installed_module_with_direct_url(
        tmp_path / "uv-tool",
        repository_root,
    )
    launcher = _launcher(tmp_path / "bin" / "memory-hub")
    selected = {
        "installed_ancestor": tmp_path / "uv-tool" / "lib",
        "installed_package": module_path.parents[1],
        "source_git": repository_root / ".git",
        "source_module": source_module,
    }[surface]
    selected.chmod(0o777 if selected.is_dir() else 0o666)

    identity = InstallationIdentity.discover_installed_source(
        launcher=launcher,
        module_path=module_path,
    )

    assert identity is None


def test_inspector_reports_missing_and_matches_exact_name_only(tmp_path: Path) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"

    missing = AutomationInspector(automations_root).inspect(desired)
    assert missing.status == "missing"
    assert missing.matches == 0

    _write_automation(
        automations_root,
        "other",
        desired,
        name="Project Memory Hub Daily Reconcile Copy",
    )
    exact_name_only = AutomationInspector(automations_root).inspect(desired)
    assert exact_name_only.status == "missing"
    assert exact_name_only.matches == 0


def test_inspector_reports_current_without_writing(tmp_path: Path, monkeypatch) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "daily", desired)
    real_open = os.open
    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def read_only_open(path, flags, *args, **kwargs):
        assert flags & write_flags == 0
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(automation_module.os, "open", read_only_open)

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "current"
    assert inspection.matches == 1
    for forbidden_method in ("create", "update", "write", "save", "install"):
        assert not hasattr(AutomationInspector, forbidden_method)


def test_inspector_can_verify_the_exact_host_project_id(tmp_path: Path) -> None:
    base = _desired(tmp_path)
    desired = DesiredAutomation.daily_reconcile(
        repository_root=base.repository_root,
        launcher=base.launcher,
        project_id="expected-project-id",
    )
    automations_root = tmp_path / "automations"
    _write_automation(
        automations_root,
        "daily",
        desired,
        project_id="wrong-project-id",
    )

    wrong = AutomationInspector(automations_root).inspect(desired)
    _write_automation(
        automations_root,
        "daily",
        desired,
        project_id="expected-project-id",
    )
    exact = AutomationInspector(automations_root).inspect(desired)

    assert wrong.status == "drifted"
    assert exact.status == "current"


def test_inspector_does_not_accept_an_unknown_expected_project_id(tmp_path: Path) -> None:
    base = _desired(tmp_path)
    desired = DesiredAutomation.daily_reconcile(
        repository_root=base.repository_root,
        launcher=base.launcher,
        project_id=None,
    )
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "daily", desired)

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "drifted"


def test_inspector_reports_duplicate_exact_names(tmp_path: Path) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "first", desired)
    _write_automation(automations_root, "second", desired)

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "duplicate"
    assert inspection.matches == 2


def test_inspector_reports_disabled_exact_name(tmp_path: Path) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "daily", desired, status="PAUSED")

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "disabled"
    assert inspection.matches == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", "Run something else"),
        (
            "rrule",
            "DTSTART;TZID=Asia/Shanghai:19700101T043000\nRRULE:FREQ=DAILY;BYHOUR=4;BYMINUTE=30",
        ),
        ("execution_environment", "worktree"),
        ("cwds", ["/different/project"]),
        ("kind", "heartbeat"),
        ("target_type", "projectless"),
    ],
)
def test_inspector_reports_drifted_exact_name(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "daily", desired, **{field: value})

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "drifted"
    assert inspection.matches == 1


@pytest.mark.parametrize("failure", ["malformed", "unknown_schema", "symlink"])
def test_inspector_fails_closed_for_untrusted_metadata(
    tmp_path: Path,
    failure: str,
) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    directory = automations_root / "daily"
    directory.mkdir(parents=True)
    path = directory / "automation.toml"
    secret = "private-conversation-text"
    if failure == "malformed":
        path.write_text(f'name = "{desired.name}"\nprompt = "{secret}', encoding="utf-8")
    elif failure == "unknown_schema":
        _write_automation(
            automations_root,
            "daily",
            desired,
            version=2,
            prompt=secret,
        )
    else:
        target = tmp_path / "outside.toml"
        target.write_text(f'prompt = "{secret}"\n', encoding="utf-8")
        path.symlink_to(target)

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "drifted"
    assert secret not in inspection.remediation


def test_inspector_fails_closed_when_automations_root_is_symlink(
    tmp_path: Path,
) -> None:
    desired = _desired(tmp_path)
    real_root = tmp_path / "real-automations"
    real_root.mkdir()
    linked_root = tmp_path / "automations"
    linked_root.symlink_to(real_root, target_is_directory=True)

    inspection = AutomationInspector(linked_root).inspect(desired)

    assert inspection.status == "drifted"


def test_inspector_fails_closed_for_symlink_ancestor(tmp_path: Path) -> None:
    desired = _desired(tmp_path)
    real_parent = tmp_path / "real-parent"
    automations_root = real_parent / "automations"
    _write_automation(automations_root, "daily", desired)
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    inspection = AutomationInspector(linked_parent / "automations").inspect(desired)

    assert inspection.status == "drifted"


def test_inspector_fails_closed_for_non_owner_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "daily", desired)
    monkeypatch.setattr(
        automation_module.os,
        "getuid",
        lambda: automations_root.stat().st_uid + 1,
    )

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "drifted"


@pytest.mark.parametrize("surface", ["root", "directory", "file"])
def test_inspector_rejects_group_or_world_writable_metadata(
    tmp_path: Path,
    surface: str,
) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    automation_file = _write_automation(automations_root, "daily", desired)
    selected = {
        "root": automations_root,
        "directory": automation_file.parent,
        "file": automation_file,
    }[surface]
    selected.chmod(0o777 if selected.is_dir() else 0o666)

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "drifted"


def test_inspector_fails_closed_when_the_entry_budget_is_exceeded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desired = _desired(tmp_path)
    automations_root = tmp_path / "automations"
    _write_automation(automations_root, "daily", desired)
    (automations_root / "extra-one").mkdir()
    (automations_root / "extra-two").mkdir()
    monkeypatch.setattr(automation_module, "_MAX_AUTOMATION_ENTRIES", 2)

    inspection = AutomationInspector(automations_root).inspect(desired)

    assert inspection.status == "drifted"
