from __future__ import annotations

import hashlib
import subprocess
import tomllib
from pathlib import Path

from project_memory_hub import __version__
from scripts import verify_wheel as wheel_verifier


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _project_document() -> dict[str, object]:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
        return tomllib.load(file)


def _project_metadata() -> dict[str, object]:
    return _project_document()["project"]  # type: ignore[return-value,index]


def test_public_beta_version_is_consistent() -> None:
    project = _project_metadata()

    assert project["version"] == __version__ == "0.2.1"


def test_public_metadata_is_bounded_to_verified_support() -> None:
    project = _project_metadata()
    classifiers = set(project.get("classifiers", ()))  # type: ignore[arg-type]

    assert project.get("description") == (
        "Local-first, model-isolated project memory for AI-assisted development."
    )
    assert project.get("readme") == "README.md"
    assert project.get("license") == "Apache-2.0"
    assert project.get("license-files") == ["LICENSE"]
    assert project.get("authors") == [{"name": "Project Memory Hub Contributors"}]
    assert project["requires-python"] == ">=3.11,<3.13"
    assert set(project["keywords"]) >= {  # type: ignore[arg-type]
        "ai",
        "codex",
        "local-first",
        "project-memory",
    }
    assert {
        "Development Status :: 4 - Beta",
        "Operating System :: MacOS :: MacOS X",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    } <= classifiers
    assert not any(classifier.startswith("License ::") for classifier in classifiers)
    assert {
        classifier
        for classifier in classifiers
        if classifier.startswith("Programming Language :: Python :: 3.")
    } == {
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    }
    assert not any("Windows" in classifier for classifier in classifiers)
    assert "urls" not in project

    build_system = _project_document()["build-system"]
    assert build_system["requires"] == ["hatchling>=1.27,<2"]  # type: ignore[index]


def test_release_only_tools_stay_in_the_bounded_test_extra() -> None:
    project = _project_metadata()
    runtime_dependencies = tuple(project["dependencies"])  # type: ignore[arg-type]
    test_dependencies = set(
        project["optional-dependencies"]["test"]  # type: ignore[index]
    )

    assert {"twine>=5,<7", "PyYAML>=6,<7", "Pillow>=10,<13"} <= test_dependencies
    assert not any(
        dependency.casefold().startswith(("twine", "pyyaml", "pillow"))
        for dependency in runtime_dependencies
    )


def test_uv_lock_is_tracked_and_matches_the_public_beta() -> None:
    ignored_lines = {
        line.strip()
        for line in (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "uv.lock"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    ignored = subprocess.run(
        ["git", "check-ignore", "--no-index", "-q", "uv.lock"],
        cwd=PROJECT_ROOT,
        check=False,
    )
    with (PROJECT_ROOT / "uv.lock").open("rb") as file:
        lock = tomllib.load(file)
    package = next(item for item in lock["package"] if item["name"] == "project-memory-hub")

    assert "uv.lock" not in ignored_lines
    assert ignored.returncode == 1
    assert tracked.returncode == 0, tracked.stderr
    assert lock["requires-python"] == ">=3.11, <3.13"
    assert package["version"] == "0.2.1"
    assert {dependency["name"] for dependency in package["optional-dependencies"]["test"]} >= {
        "twine",
        "pyyaml",
        "pillow",
    }


def test_license_is_the_complete_official_apache_2_text() -> None:
    license_path = PROJECT_ROOT / "LICENSE"

    assert license_path.is_file(), "LICENSE must contain the complete Apache License 2.0 text"
    license_bytes = license_path.read_bytes()

    assert len(license_bytes) == 11_358
    assert hashlib.sha256(license_bytes).hexdigest() == (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    )


def test_wheel_verifier_reads_expected_identity_from_project_metadata(tmp_path: Path) -> None:
    metadata_file = tmp_path / "pyproject.toml"
    metadata_file.write_text(
        '[project]\nname = "example-package"\nversion = "9.8.7"\n',
        encoding="utf-8",
    )
    loader = getattr(wheel_verifier, "load_project_identity", None)

    assert callable(loader)
    assert loader(metadata_file) == ("example-package", "9.8.7")
    verifier_source = (PROJECT_ROOT / "scripts/verify_wheel.py").read_text(encoding="utf-8")
    assert "0.2.0" not in verifier_source
