from __future__ import annotations

import configparser
import re
import subprocess
import tempfile
import tomllib
import zipfile
from email import policy
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = PROJECT_ROOT / "pyproject.toml"

_PACKAGE_FILES = {
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
}
_FORBIDDEN_WHEEL_PREFIXES = ("demo-output/", "docs/assets/", "tests/")
_FORBIDDEN_WHEEL_BASENAMES = frozenset(
    {
        ".project-memory-hub-demo-output-v1",
        ".project-memory-hub-demo-runtime-v1",
    }
)


def _load_project(path: Path = PROJECT_FILE) -> dict[str, Any]:
    with Path(path).open("rb") as file:
        document = tomllib.load(file)
    project = document.get("project")
    if type(project) is not dict:
        raise SystemExit("project metadata missing")
    return project


def load_project_identity(path: Path = PROJECT_FILE) -> tuple[str, str]:
    project = _load_project(path)
    project_name = project.get("name")
    project_version = project.get("version")
    if not isinstance(project_name, str) or not project_name.strip():
        raise SystemExit("project name missing")
    if not isinstance(project_version, str) or not project_version.strip():
        raise SystemExit("project version missing")
    return project_name, project_version


def _distribution_name(project_name: str) -> str:
    return re.sub(r"[-_.]+", "_", project_name)


def _expected_wheel_files(project_name: str, project_version: str) -> set[str]:
    dist_info = f"{_distribution_name(project_name)}-{project_version}.dist-info"
    return {*_PACKAGE_FILES, f"{dist_info}/licenses/LICENSE"}


def _single_header(message: Any, header: str, mismatch: str) -> str:
    values = message.get_all(header, [])
    if len(values) != 1:
        raise SystemExit(mismatch)
    return str(values[0])


def _specifier_parts(value: str) -> frozenset[str]:
    return frozenset(part.strip() for part in value.split(",") if part.strip())


def _verify_metadata(metadata: str, project: dict[str, Any]) -> None:
    message = Parser(policy=policy.compat32).parsestr(metadata)
    project_name = str(project["name"])
    project_version = str(project["version"])

    if _single_header(message, "Name", "wheel metadata name mismatch") != project_name:
        raise SystemExit("wheel metadata name mismatch")
    if _single_header(message, "Version", "wheel metadata version mismatch") != project_version:
        raise SystemExit("wheel metadata version mismatch")
    if _single_header(message, "Summary", "wheel metadata summary mismatch") != project.get(
        "description"
    ):
        raise SystemExit("wheel metadata summary mismatch")
    if _single_header(message, "License-Expression", "wheel metadata license mismatch") != (
        project.get("license")
    ):
        raise SystemExit("wheel metadata license mismatch")
    if message.get_all("License-File", []) != list(project.get("license-files", [])):
        raise SystemExit("wheel metadata license file mismatch")

    requires_python = _single_header(message, "Requires-Python", "wheel metadata python mismatch")
    if _specifier_parts(requires_python) != _specifier_parts(str(project.get("requires-python"))):
        raise SystemExit("wheel metadata python mismatch")
    if (
        _single_header(
            message,
            "Description-Content-Type",
            "wheel readme metadata mismatch",
        )
        != "text/markdown"
    ):
        raise SystemExit("wheel readme metadata mismatch")
    if set(message.get_all("Classifier", [])) != set(project.get("classifiers", [])):
        raise SystemExit("wheel metadata classifiers mismatch")


def _verify_entry_points(entry_points: str) -> None:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    try:
        parser.read_string(entry_points)
    except configparser.Error as error:
        raise SystemExit("console entry point mismatch") from error
    if parser.sections() != ["console_scripts"]:
        raise SystemExit("console entry point mismatch")
    if set(parser["console_scripts"]) != {"memory-hub"}:
        raise SystemExit("console entry point mismatch")
    if parser.get("console_scripts", "memory-hub", fallback=None) != ("project_memory_hub.cli:app"):
        raise SystemExit("console entry point mismatch")


def verify_wheel(wheel_names: set[str], metadata: str, entry_points: str) -> None:
    project = _load_project()
    project_name, project_version = load_project_identity()
    required = _expected_wheel_files(project_name, project_version)
    missing = sorted(required - wheel_names)
    if missing:
        raise SystemExit(f"wheel files missing: {missing}")
    forbidden = sorted(name for name in wheel_names if _is_forbidden_wheel_name(name))
    if forbidden:
        raise SystemExit(f"wheel contains forbidden release files: {forbidden}")
    _verify_metadata(metadata, project)
    _verify_entry_points(entry_points)


def _is_forbidden_wheel_name(name: str) -> bool:
    if name.startswith(_FORBIDDEN_WHEEL_PREFIXES):
        return True
    parts = PurePosixPath(name).parts
    if (
        PurePosixPath(name).name in _FORBIDDEN_WHEEL_BASENAMES
        or "tests" in parts
        or "demo-output" in parts
    ):
        return True
    return any(parts[index : index + 2] == ("docs", "assets") for index in range(len(parts) - 1))


def _single_archive_member(names: set[str], suffix: str, label: str) -> str:
    matches = sorted(name for name in names if name.endswith(suffix))
    if len(matches) != 1:
        raise SystemExit(f"expected exactly one wheel {label}")
    return matches[0]


def main() -> None:
    project_name, project_version = load_project_identity()
    expected_prefix = f"{_distribution_name(project_name)}-{project_version}-"
    with tempfile.TemporaryDirectory(prefix="pmh-wheel-") as directory:
        output = Path(directory)
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(output)],
            check=True,
        )
        wheels = tuple(output.glob("*.whl"))
        if len(wheels) != 1 or not wheels[0].name.startswith(expected_prefix):
            raise SystemExit(f"expected exactly one {project_name} {project_version} wheel")
        with zipfile.ZipFile(wheels[0]) as archive:
            wheel_names = set(archive.namelist())
            metadata_name = _single_archive_member(
                wheel_names,
                ".dist-info/METADATA",
                "METADATA file",
            )
            entry_points_name = _single_archive_member(
                wheel_names,
                ".dist-info/entry_points.txt",
                "entry_points.txt file",
            )
            metadata = archive.read(metadata_name).decode("utf-8", errors="strict")
            entry_points = archive.read(entry_points_name).decode("utf-8", errors="strict")
        verify_wheel(wheel_names, metadata, entry_points)


if __name__ == "__main__":
    main()
