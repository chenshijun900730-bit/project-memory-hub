from __future__ import annotations

import tomllib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.verify_probe_zero_write as zero_write
from scripts.verify_wheel import verify_wheel


PROJECT_ROOT = Path(__file__).resolve().parents[2]
with (PROJECT_ROOT / "pyproject.toml").open("rb") as _project_file:
    _PROJECT = tomllib.load(_project_file)["project"]
PROJECT_NAME = _PROJECT["name"]
PROJECT_VERSION = _PROJECT["version"]

REQUIRED_WHEEL_FILES = {
    "project_memory_hub/demo/__init__.py",
    "project_memory_hub/demo/privacy.py",
    "project_memory_hub/demo/runtime.py",
    "project_memory_hub/demo/seed.py",
    "project_memory_hub/probes/__init__.py",
    "project_memory_hub/probes/models.py",
    "project_memory_hub/probes/base.py",
    "project_memory_hub/probes/filesystem.py",
    "project_memory_hub/probes/service.py",
    "project_memory_hub/probes/builtin.py",
    "project_memory_hub/security/json_limits.py",
    "project_memory_hub/services/setup.py",
    "project_memory_hub/web/errors.py",
    "project_memory_hub/web/presentation.py",
    *{
        f"project_memory_hub/storage/migrations/{name}"
        for name in (
            "0001_initial.sql",
            "0002_import_receipt_source_agent.sql",
            "0003_compaction_kind_order.sql",
            "0004_compaction_enumeration_indexes.sql",
            "0005_strict_observation_epoch.sql",
            "0006_discovery_findings.sql",
            "0007_improvement_proposal_execution.sql",
            "0008_project_path_identity.sql",
            "0009_explicit_issue_resolution.sql",
            "0010_codex_deferred_records.sql",
            "0011_pending_capture_history.sql",
            "0012_capture_correlation.sql",
        )
    },
    "project_memory_hub/web/static/app.css",
    "project_memory_hub/web/static/i18n.js",
    "project_memory_hub/web/static/projects.js",
    "project_memory_hub/web/static/sources.js",
    *{
        f"project_memory_hub/web/templates/{name}.html"
        for name in (
            "_empty_state",
            "base",
            "error",
            "imports",
            "memories",
            "overview",
            "projects",
            "proposals",
            "settings",
            "setup",
            "sources",
        )
    },
    f"project_memory_hub-{PROJECT_VERSION}.dist-info/licenses/LICENSE",
}


def _valid_metadata(*, name: str = PROJECT_NAME, version: str = PROJECT_VERSION) -> str:
    return "\n".join(
        (
            "Metadata-Version: 2.4",
            f"Name: {name}",
            f"Version: {version}",
            "Summary: Local-first, model-isolated project memory for AI-assisted development.",
            "License-Expression: Apache-2.0",
            "License-File: LICENSE",
            "Requires-Python: >=3.11,<3.13",
            "Description-Content-Type: text/markdown",
            "Classifier: Development Status :: 4 - Beta",
            "Classifier: Operating System :: MacOS :: MacOS X",
            "Classifier: Programming Language :: Python :: 3 :: Only",
            "Classifier: Programming Language :: Python :: 3.11",
            "Classifier: Programming Language :: Python :: 3.12",
            "",
        )
    )


VALID_ENTRY_POINTS = "[console_scripts]\nmemory-hub = project_memory_hub.cli:app\n"


def test_wheel_verifier_requires_sources_console_assets() -> None:
    wheel_names = {
        "project_memory_hub/probes/__init__.py",
        "project_memory_hub/probes/models.py",
        "project_memory_hub/probes/base.py",
        "project_memory_hub/probes/filesystem.py",
        "project_memory_hub/probes/service.py",
        "project_memory_hub/probes/builtin.py",
        "project_memory_hub/storage/migrations/0010_codex_deferred_records.sql",
        "project_memory_hub/storage/migrations/0011_pending_capture_history.sql",
        "project_memory_hub/storage/migrations/0012_capture_correlation.sql",
    }

    with pytest.raises(SystemExit) as exc_info:
        verify_wheel(
            wheel_names,
            _valid_metadata(),
            VALID_ENTRY_POINTS,
        )

    message = str(exc_info.value)
    assert "project_memory_hub/web/templates/sources.html" in message
    assert "project_memory_hub/web/templates/setup.html" in message
    assert "project_memory_hub/web/templates/_empty_state.html" in message
    assert "project_memory_hub/web/templates/error.html" in message
    assert "project_memory_hub/web/errors.py" in message
    assert "project_memory_hub/web/presentation.py" in message
    assert "project_memory_hub/services/setup.py" in message
    assert "project_memory_hub/web/static/projects.js" in message
    assert "project_memory_hub/web/static/sources.js" in message
    assert "project_memory_hub/web/static/i18n.js" in message
    assert "project_memory_hub/web/static/app.css" in message
    assert "project_memory_hub/demo/privacy.py" in message
    assert "project_memory_hub/demo/runtime.py" in message


def test_wheel_verifier_rejects_metadata_name_mismatch() -> None:
    with pytest.raises(SystemExit, match="name mismatch"):
        verify_wheel(
            REQUIRED_WHEEL_FILES,
            _valid_metadata(name="another-project"),
            VALID_ENTRY_POINTS,
        )


def test_wheel_verifier_accepts_complete_public_wheel_contract() -> None:
    verify_wheel(REQUIRED_WHEEL_FILES, _valid_metadata(), VALID_ENTRY_POINTS)


@pytest.mark.parametrize(
    ("metadata", "message"),
    (
        (_valid_metadata(version=f"{PROJECT_VERSION}.post1"), "version mismatch"),
        (_valid_metadata().replace("License-Expression: Apache-2.0\n", ""), "license mismatch"),
        (_valid_metadata().replace("License-File: LICENSE\n", ""), "license file mismatch"),
        (_valid_metadata().replace("Requires-Python: >=3.11,<3.13\n", ""), "python mismatch"),
        (
            _valid_metadata().replace("Description-Content-Type: text/markdown\n", ""),
            "readme metadata mismatch",
        ),
    ),
)
def test_wheel_verifier_rejects_incomplete_or_inexact_metadata(
    metadata: str,
    message: str,
) -> None:
    with pytest.raises(SystemExit, match=message):
        verify_wheel(REQUIRED_WHEEL_FILES, metadata, VALID_ENTRY_POINTS)


def test_wheel_verifier_requires_exact_console_entry_point() -> None:
    with pytest.raises(SystemExit, match="console entry point mismatch"):
        verify_wheel(
            REQUIRED_WHEEL_FILES,
            _valid_metadata(),
            "[console_scripts]\nnot-memory-hub = project_memory_hub.cli:app\n",
        )


def test_wheel_verifier_rejects_additional_demo_console_entry_point() -> None:
    with pytest.raises(SystemExit, match="console entry point mismatch"):
        verify_wheel(
            REQUIRED_WHEEL_FILES,
            _valid_metadata(),
            (
                "[console_scripts]\n"
                "memory-hub = project_memory_hub.cli:app\n"
                "memory-hub-demo = project_memory_hub.demo.seed:seed_demo_database\n"
            ),
        )


def test_wheel_verifier_rejects_any_additional_executable_group() -> None:
    with pytest.raises(SystemExit, match="console entry point mismatch"):
        verify_wheel(
            REQUIRED_WHEEL_FILES,
            _valid_metadata(),
            (
                VALID_ENTRY_POINTS + "\n[gui_scripts]\n"
                "memory-hub-demo = project_memory_hub.demo.seed:seed_demo_database\n"
            ),
        )


@pytest.mark.parametrize(
    "forbidden_name",
    (
        "tests/unit/demo/test_seed.py",
        "docs/assets/overview.png",
        "demo-output/manifest.json",
        ".project-memory-hub-demo-output-v1",
    ),
)
def test_wheel_verifier_rejects_test_or_generated_demo_output(forbidden_name: str) -> None:
    with pytest.raises(SystemExit, match="wheel contains forbidden release files"):
        verify_wheel(
            REQUIRED_WHEEL_FILES | {forbidden_name},
            _valid_metadata(),
            VALID_ENTRY_POINTS,
        )


@pytest.mark.parametrize(
    "forbidden_name",
    (
        "project_memory_hub/tests/unit/demo/test_seed.py",
        "project_memory_hub/docs/assets/overview.png",
        "project_memory_hub/demo-output/manifest.json",
    ),
)
def test_wheel_verifier_rejects_nested_test_or_generated_output(
    forbidden_name: str,
) -> None:
    with pytest.raises(SystemExit, match="wheel contains forbidden release files"):
        verify_wheel(
            REQUIRED_WHEEL_FILES | {forbidden_name},
            _valid_metadata(),
            VALID_ENTRY_POINTS,
        )


def test_wheel_verifier_requires_embedded_license_file() -> None:
    wheel_names = {name for name in REQUIRED_WHEEL_FILES if not name.endswith("/licenses/LICENSE")}

    with pytest.raises(SystemExit, match="licenses/LICENSE"):
        verify_wheel(wheel_names, _valid_metadata(), VALID_ENTRY_POINTS)


def test_zero_write_probe_can_target_installed_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "memory-hub"
    recorded: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        recorded.append(command)
        return SimpleNamespace(returncode=0, stdout=b"[]")

    monkeypatch.setattr(zero_write.subprocess, "run", run)

    zero_write._probe("--all", executable=executable)

    assert recorded == [[str(executable), "source", "probe", "--all", "--format", "json"]]


def test_public_asset_verifier_supports_direct_script_execution() -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "verify_public_assets.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Verify synthetic public demo assets" in result.stdout


def test_demo_asset_generator_supports_direct_script_execution() -> None:
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "generate_demo_assets.py"), "--help"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Generate isolated synthetic public assets" in result.stdout
