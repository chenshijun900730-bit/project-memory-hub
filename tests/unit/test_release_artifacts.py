from __future__ import annotations

import hashlib
import io
import json
import os
import signal
import stat
import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.smoke_install_artifact as smoke
import scripts.verify_release_artifacts as release


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_FILE = PROJECT_ROOT / "pyproject.toml"
with PROJECT_FILE.open("rb") as _project_file:
    PROJECT = tomllib.load(_project_file)["project"]
PROJECT_NAME = str(PROJECT["name"])
PROJECT_VERSION = str(PROJECT["version"])
DIST_NAME = PROJECT_NAME.replace("-", "_")
WHEEL_NAME = f"{DIST_NAME}-{PROJECT_VERSION}-py3-none-any.whl"
SDIST_NAME = f"{DIST_NAME}-{PROJECT_VERSION}.tar.gz"
DIST_INFO = f"{DIST_NAME}-{PROJECT_VERSION}.dist-info"
SDIST_ROOT = f"{DIST_NAME}-{PROJECT_VERSION}"


def _metadata(*, name: str = PROJECT_NAME, version: str = PROJECT_VERSION) -> str:
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


def _package_files() -> dict[str, bytes]:
    result: dict[str, bytes] = {}
    source_root = PROJECT_ROOT / "src"
    for path in sorted((source_root / "project_memory_hub").rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            result[path.relative_to(source_root).as_posix()] = path.read_bytes()
    return result


def _write_wheel(
    dist: Path,
    *,
    metadata_name: str = PROJECT_NAME,
    metadata_version: str = PROJECT_VERSION,
    entry_point: str = "project_memory_hub.cli:app",
    omit: str | None = None,
    extra_member: str | None = None,
    extra_members: dict[str, bytes] | None = None,
    member_modes: dict[str, int] | None = None,
) -> Path:
    wheel = dist / WHEEL_NAME
    members = _package_files()
    members.update(
        {
            f"{DIST_INFO}/METADATA": _metadata(
                name=metadata_name,
                version=metadata_version,
            ).encode(),
            f"{DIST_INFO}/WHEEL": b"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{DIST_INFO}/entry_points.txt": (
                f"[console_scripts]\nmemory-hub = {entry_point}\n"
            ).encode(),
            f"{DIST_INFO}/licenses/LICENSE": (PROJECT_ROOT / "LICENSE").read_bytes(),
            f"{DIST_INFO}/RECORD": b"",
        }
    )
    if omit is not None:
        members.pop(omit)
    if extra_member is not None:
        members[extra_member] = b"unexpected"
    if extra_members is not None:
        members.update(extra_members)
    with zipfile.ZipFile(wheel, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(members.items()):
            if member_modes is None or name not in member_modes:
                archive.writestr(name, content)
            else:
                info = zipfile.ZipInfo(name)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = member_modes[name] << 16
                archive.writestr(info, content)
    return wheel


def _add_tar_bytes(archive: tarfile.TarFile, name: str, content: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(content)
    info.mode = 0o644
    archive.addfile(info, io.BytesIO(content))


def _write_sdist(
    dist: Path,
    *,
    metadata_name: str = PROJECT_NAME,
    metadata_version: str = PROJECT_VERSION,
    entry_point: str = "project_memory_hub.cli:app",
    omit: str | None = None,
    extra_member: str | None = None,
    extra_member_type: bytes | None = None,
    extra_members: dict[str, bytes] | None = None,
    gitignore: bytes | None = None,
) -> Path:
    sdist = dist / SDIST_NAME
    pyproject = PROJECT_FILE.read_text(encoding="utf-8").replace(
        'memory-hub = "project_memory_hub.cli:app"',
        f'memory-hub = "{entry_point}"',
    )
    members = {
        f"{SDIST_ROOT}/PKG-INFO": _metadata(
            name=metadata_name,
            version=metadata_version,
        ).encode(),
        f"{SDIST_ROOT}/pyproject.toml": pyproject.encode(),
        f"{SDIST_ROOT}/.gitignore": (
            (PROJECT_ROOT / ".gitignore").read_bytes() if gitignore is None else gitignore
        ),
        f"{SDIST_ROOT}/LICENSE": (PROJECT_ROOT / "LICENSE").read_bytes(),
        f"{SDIST_ROOT}/README.md": (PROJECT_ROOT / "README.md").read_bytes(),
        **{f"{SDIST_ROOT}/src/{name}": content for name, content in _package_files().items()},
    }
    if omit is not None:
        members.pop(omit)
    with tarfile.open(sdist, "w:gz", format=tarfile.PAX_FORMAT) as archive:
        for name, content in sorted(members.items()):
            _add_tar_bytes(archive, name, content)
        if extra_member is not None:
            if extra_member_type is None:
                _add_tar_bytes(archive, extra_member, b"unexpected")
            else:
                info = tarfile.TarInfo(extra_member)
                info.type = extra_member_type
                info.linkname = "/tmp/escape"
                archive.addfile(info)
        if extra_members is not None:
            for name, content in sorted(extra_members.items()):
                _add_tar_bytes(archive, name, content)
    return sdist


def _valid_dist(tmp_path: Path, **kwargs: object) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    _write_wheel(
        dist,
        **{
            key.removeprefix("wheel_"): value
            for key, value in kwargs.items()
            if key.startswith("wheel_")
        },
    )
    _write_sdist(
        dist,
        **{
            key.removeprefix("sdist_"): value
            for key, value in kwargs.items()
            if key.startswith("sdist_")
        },
    )
    return dist


def test_artifact_verifier_accepts_exact_wheel_and_sdist(tmp_path: Path) -> None:
    dist = _valid_dist(tmp_path)

    bundle = release.inspect_release_artifacts(dist)

    assert bundle.wheel == (dist / WHEEL_NAME).resolve()
    assert bundle.sdist == (dist / SDIST_NAME).resolve()
    assert bundle.project_name == PROJECT_NAME
    assert bundle.project_version == PROJECT_VERSION


def test_hatch_sdist_configuration_minimizes_public_source_surface() -> None:
    with PROJECT_FILE.open("rb") as file:
        document = tomllib.load(file)

    assert document["tool"]["hatch"]["build"]["targets"]["sdist"] == {
        "include": [
            "/src",
            "/LICENSE",
            "/README.md",
            "/pyproject.toml",
        ],
    }


def test_sdist_source_inventory_is_exactly_runtime_and_release_metadata() -> None:
    expected = {
        f"{SDIST_ROOT}/PKG-INFO",
        f"{SDIST_ROOT}/.gitignore",
        f"{SDIST_ROOT}/LICENSE",
        f"{SDIST_ROOT}/README.md",
        f"{SDIST_ROOT}/pyproject.toml",
        *{f"{SDIST_ROOT}/src/{name}" for name in release._expected_runtime_members()},
    }

    assert release._expected_sdist_source_members(SDIST_ROOT) == expected


def test_artifact_verifier_binds_hatch_forced_gitignore_content(tmp_path: Path) -> None:
    dist = _valid_dist(
        tmp_path,
        sdist_gitignore=(PROJECT_ROOT / ".gitignore").read_bytes() + b"private.env\n",
    )

    with pytest.raises(release.ReleaseArtifactError, match="sdist .gitignore content mismatch"):
        release.inspect_release_artifacts(dist)


def test_artifact_verifier_allows_only_uv_build_housekeeping(tmp_path: Path) -> None:
    dist = _valid_dist(tmp_path)
    (dist / ".gitignore").write_bytes(b"*")

    release.inspect_release_artifacts(dist)

    (dist / ".gitignore").write_bytes(b"*\nprivate.txt\n")
    with pytest.raises(release.ReleaseArtifactError, match="exactly one wheel and one sdist"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize("mutation", ("missing-wheel", "missing-sdist", "second-wheel", "extra"))
def test_artifact_verifier_requires_only_one_pair(tmp_path: Path, mutation: str) -> None:
    dist = _valid_dist(tmp_path)
    if mutation == "missing-wheel":
        (dist / WHEEL_NAME).unlink()
    elif mutation == "missing-sdist":
        (dist / SDIST_NAME).unlink()
    elif mutation == "second-wheel":
        (dist / "duplicate.whl").write_bytes((dist / WHEEL_NAME).read_bytes())
    else:
        (dist / "release-notes.txt").write_text("unexpected", encoding="utf-8")

    with pytest.raises(release.ReleaseArtifactError, match="exactly one wheel and one sdist"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    (
        ({"wheel_metadata_version": f"{PROJECT_VERSION}.post1"}, "wheel metadata version"),
        ({"sdist_metadata_name": "another-project"}, "sdist metadata name"),
        ({"wheel_entry_point": "project_memory_hub.cli:main"}, "console entry point"),
        ({"sdist_entry_point": "project_memory_hub.cli:main"}, "sdist entry point"),
    ),
)
def test_artifact_verifier_rejects_identity_or_entry_point_mismatch(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    dist = _valid_dist(tmp_path, **kwargs)

    with pytest.raises(release.ReleaseArtifactError, match=message):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    (
        ({"wheel_omit": "project_memory_hub/web/templates/sources.html"}, "sources.html"),
        ({"wheel_omit": "project_memory_hub/web/static/app.css"}, "app.css"),
        (
            {"sdist_omit": f"{SDIST_ROOT}/src/project_memory_hub/web/templates/sources.html"},
            "sources.html",
        ),
        (
            {"sdist_omit": f"{SDIST_ROOT}/src/project_memory_hub/web/static/app.css"},
            "app.css",
        ),
    ),
)
def test_artifact_verifier_requires_templates_and_static_files(
    tmp_path: Path,
    kwargs: dict[str, object],
    missing: str,
) -> None:
    dist = _valid_dist(tmp_path, **kwargs)

    with pytest.raises(release.ReleaseArtifactError, match=missing):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"wheel_omit": "project_memory_hub/cli.py"},
        {
            "sdist_omit": (
                f"{SDIST_ROOT}/src/project_memory_hub/storage/migrations/0001_initial.sql"
            )
        },
        {"wheel_omit": "project_memory_hub/adapters/codex.py"},
        {"sdist_omit": f"{SDIST_ROOT}/src/project_memory_hub/services/reconcile.py"},
        {"wheel_extra_member": "project_memory_hub/debug_backdoor.py"},
        {"sdist_extra_member": (f"{SDIST_ROOT}/src/project_memory_hub/debug_backdoor.py")},
    ),
)
def test_artifact_verifier_requires_complete_runtime_member_manifest(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    dist = _valid_dist(tmp_path, **kwargs)

    with pytest.raises(release.ReleaseArtifactError, match="runtime member manifest"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    "kwargs",
    (
        {"wheel_extra_member": "project_memory_hub/private.env"},
        {"sdist_extra_member": f"{SDIST_ROOT}/private.env"},
    ),
)
def test_artifact_verifier_rejects_private_environment_files(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    dist = _valid_dist(tmp_path, **kwargs)

    with pytest.raises(release.ReleaseArtifactError, match="forbidden release file"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    "member",
    (
        f"{SDIST_ROOT}/.git/config",
        f"{SDIST_ROOT}/credentials.json",
        f"{SDIST_ROOT}/id_rsa",
        f"{SDIST_ROOT}/docs/credentials.json",
        f"{SDIST_ROOT}/.github/private.yml",
        f"{SDIST_ROOT}/docs/superpowers/plans/private-plan.md",
    ),
)
def test_artifact_verifier_rejects_sdist_files_outside_public_inventory(
    tmp_path: Path,
    member: str,
) -> None:
    dist = _valid_dist(tmp_path, sdist_extra_member=member)

    with pytest.raises(release.ReleaseArtifactError, match="sdist source member manifest"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    "kwargs",
    (
        {
            "wheel_member_modes": {
                "project_memory_hub/cli.py": stat.S_IFIFO | 0o600,
            }
        },
        {
            "wheel_member_modes": {
                "project_memory_hub/cli.py": stat.S_IFSOCK | 0o600,
            }
        },
        {
            "wheel_member_modes": {
                "project_memory_hub/cli.py": stat.S_IFCHR | 0o600,
            }
        },
        {
            "wheel_member_modes": {
                "project_memory_hub/cli.py": stat.S_IFBLK | 0o600,
            }
        },
        {
            "wheel_member_modes": {
                "project_memory_hub/cli.py": stat.S_IFDIR | 0o755,
            }
        },
        {
            "wheel_extra_member": "project_memory_hub/unexpected/",
            "wheel_member_modes": {
                "project_memory_hub/unexpected/": stat.S_IFREG | 0o644,
            },
        },
        {
            "wheel_extra_member": "project_memory_hub/unexpected/",
            "wheel_member_modes": {
                "project_memory_hub/unexpected/": stat.S_IFDIR | 0o755,
            },
        },
    ),
)
def test_artifact_verifier_rejects_wheel_special_or_mismatched_member_types(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    dist = _valid_dist(tmp_path, **kwargs)

    with pytest.raises(release.ReleaseArtifactError, match="unsafe archive"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize("artifact", ("wheel-casefold", "sdist-unicode"))
def test_artifact_verifier_rejects_casefold_or_unicode_path_collisions(
    tmp_path: Path,
    artifact: str,
) -> None:
    if artifact == "wheel-casefold":
        kwargs: dict[str, object] = {"wheel_extra_member": "project_memory_hub/CLI.py"}
    else:
        kwargs = {
            "sdist_extra_members": {
                f"{SDIST_ROOT}/docs/caf\N{LATIN SMALL LETTER E WITH ACUTE}.md": b"one",
                f"{SDIST_ROOT}/docs/cafe\N{COMBINING ACUTE ACCENT}.md": b"two",
            }
        }
    dist = _valid_dist(tmp_path, **kwargs)

    with pytest.raises(release.ReleaseArtifactError, match="archive path collision"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    ("limit", "value"),
    (
        ("_MAX_ARCHIVE_MEMBERS", 1),
        ("_MAX_ARCHIVE_MEMBER_BYTES", 1),
        ("_MAX_ARCHIVE_TOTAL_BYTES", 1),
    ),
)
def test_artifact_verifier_enforces_expanded_archive_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit: str,
    value: int,
) -> None:
    dist = _valid_dist(tmp_path)
    monkeypatch.setattr(release, limit, value)

    with pytest.raises(release.ReleaseArtifactError, match="archive limit"):
        release.inspect_release_artifacts(dist)


@pytest.mark.parametrize(
    "artifact",
    ("wheel", "wheel-noncanonical", "sdist", "sdist-noncanonical", "sdist-symlink"),
)
def test_artifact_verifier_rejects_unsafe_archive_paths(tmp_path: Path, artifact: str) -> None:
    kwargs: dict[str, object]
    if artifact == "wheel":
        kwargs = {"wheel_extra_member": "../escape"}
    elif artifact == "wheel-noncanonical":
        kwargs = {"wheel_extra_member": "project_memory_hub//escape.py"}
    elif artifact == "sdist":
        kwargs = {"sdist_extra_member": f"{SDIST_ROOT}/../../escape"}
    elif artifact == "sdist-noncanonical":
        kwargs = {"sdist_extra_member": f"{SDIST_ROOT}/src/./escape.py"}
    else:
        kwargs = {
            "sdist_extra_member": f"{SDIST_ROOT}/src/project_memory_hub/escape",
            "sdist_extra_member_type": tarfile.SYMTYPE,
        }
    dist = _valid_dist(tmp_path, **kwargs)

    with pytest.raises(release.ReleaseArtifactError, match="unsafe archive"):
        release.inspect_release_artifacts(dist)


def test_artifact_verifier_rejects_symlinked_release_file(tmp_path: Path) -> None:
    dist = _valid_dist(tmp_path)
    wheel = dist / WHEEL_NAME
    external = tmp_path / "external.whl"
    wheel.replace(external)
    wheel.symlink_to(external)

    with pytest.raises(release.ReleaseArtifactError, match="release artifact file"):
        release.inspect_release_artifacts(dist)


def _python(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path.resolve()


def test_smoke_interpreters_must_match_uv_system_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    python_311 = _python(tmp_path / "system-311" / "python3.11")
    python_312 = _python(tmp_path / "system-312" / "python3.12")
    uv = _python(tmp_path / "tools" / "uv")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        commands.append(command)
        if command[1:5] == ["python", "find", "--system", "3.11"]:
            output = f"{python_311}\n"
        elif command[1:5] == ["python", "find", "--system", "3.12"]:
            output = f"{python_312}\n"
        else:
            minor = 11 if command[0] == str(python_311) else 12
            output = json.dumps({"major": 3, "minor": minor})
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)

    resolved = release.resolve_smoke_pythons(
        (python_311, python_312),
        uv_executable=str(uv),
    )

    assert resolved == (python_311, python_312)
    assert commands == [
        [str(uv), "python", "find", "--system", "3.11"],
        [str(python_311), "-I", "-c", release._PYTHON_IDENTITY_SCRIPT],
        [str(uv), "python", "find", "--system", "3.12"],
        [str(python_312), "-I", "-c", release._PYTHON_IDENTITY_SCRIPT],
    ]


@pytest.mark.parametrize(
    "failure",
    ("relative-output", "project-venv", "request-mismatch", "symlink-alias"),
)
def test_smoke_interpreters_reject_untrusted_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    external_311 = _python(tmp_path / "external" / "python3.11")
    external_312 = _python(tmp_path / "external" / "python3.12")
    uv = _python(tmp_path / "tools" / "uv")
    project_python = _python(repository / ".venv" / "bin" / "python3.11")
    alias = repository / ".venv" / "bin" / "python-alias"
    alias.symlink_to(external_311)

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[1:2] == ["-I"]:
            minor = 11 if command[0].endswith("3.11") else 12
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"major": 3, "minor": minor}),
                stderr="",
            )
        if failure == "relative-output" and command[-1] == "3.11":
            output = "python3.11"
        elif failure == "project-venv" and command[-1] == "3.11":
            output = str(project_python)
        else:
            output = str(external_311 if command[-1] == "3.11" else external_312)
        return SimpleNamespace(returncode=0, stdout=f"{output}\n", stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)
    requested = (
        (external_312, external_311)
        if failure == "request-mismatch"
        else (alias, external_312)
        if failure == "symlink-alias"
        else (project_python if failure == "project-venv" else external_311, external_312)
    )

    with pytest.raises(release.ReleaseArtifactError, match="system interpreter"):
        release.resolve_smoke_pythons(
            requested,
            repository_root=repository,
            uv_executable=str(uv),
        )


@pytest.mark.parametrize("failure", ("version", "same-identity"))
def test_smoke_interpreters_reject_wrong_version_or_same_file_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    uv = _python(tmp_path / "tools" / "uv")
    python_311 = _python(tmp_path / "external" / "python3.11")
    python_312 = tmp_path / "external" / "python3.12"
    if failure == "same-identity":
        python_312.hardlink_to(python_311)
    else:
        _python(python_312)
    python_312 = python_312.resolve()

    def run(command: list[str], **_kwargs: object) -> SimpleNamespace:
        if command[1:2] == ["-I"]:
            minor = 11 if command[0].endswith("3.11") else 12
            if failure == "version" and minor == 12:
                minor = 11
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"major": 3, "minor": minor}),
                stderr="",
            )
        selected = python_311 if command[-1] == "3.11" else python_312
        return SimpleNamespace(returncode=0, stdout=f"{selected}\n", stderr="")

    monkeypatch.setattr(release.subprocess, "run", run)

    with pytest.raises(release.ReleaseArtifactError, match="system interpreter"):
        release.resolve_smoke_pythons(
            (python_311, python_312),
            repository_root=repository,
            uv_executable=str(uv),
        )


def test_smoke_interpreters_require_absolute_uv_executable(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(release.ReleaseArtifactError, match="uv executable must be absolute"):
        release.resolve_smoke_pythons((), repository_root=repository, uv_executable="uv")


def test_release_verifier_cli_resolves_uv_to_absolute_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uv = _python(tmp_path / "tools" / "uv")
    dist = tmp_path / "dist"
    arguments = SimpleNamespace(dist=dist, smoke_python=[])
    calls: list[tuple[Path, tuple[Path, ...], str]] = []
    monkeypatch.setattr(
        release,
        "_parser",
        lambda: SimpleNamespace(parse_args=lambda: arguments),
    )
    monkeypatch.setattr(
        release,
        "shutil",
        SimpleNamespace(which=lambda _name: str(uv)),
        raising=False,
    )
    monkeypatch.setattr(
        release,
        "verify_release_artifacts",
        lambda selected_dist, pythons, **kwargs: calls.append(
            (Path(selected_dist), tuple(pythons), str(kwargs["uv_executable"]))
        ),
    )

    release.main()

    assert calls == [(dist, (), str(uv))]


def test_release_verifier_smokes_both_resolved_interpreters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = _valid_dist(tmp_path)
    python_311 = _python(tmp_path / "python3.11")
    python_312 = _python(tmp_path / "python3.12")
    uv = _python(tmp_path / "uv")
    calls: list[tuple[Path, Path, str, str, str, tuple[int, int]]] = []

    monkeypatch.setattr(
        release,
        "resolve_smoke_pythons",
        lambda _requested, **_kwargs: (python_311, python_312),
    )
    monkeypatch.setattr(
        release,
        "smoke_install_artifact",
        lambda wheel, python, **kwargs: calls.append(
            (
                Path(wheel),
                Path(python),
                str(kwargs["expected_version"]),
                str(kwargs["expected_python_version"]),
                str(kwargs["expected_wheel_sha256"]),
                tuple(kwargs["expected_wheel_identity"]),
            )
        ),
    )

    release.verify_release_artifacts(
        dist,
        (python_311, python_312),
        uv_executable=str(uv),
    )

    wheel = (dist / WHEEL_NAME).resolve()
    wheel_metadata = wheel.stat()
    wheel_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    expected = (
        wheel,
        PROJECT_VERSION,
        wheel_digest,
        (wheel_metadata.st_dev, wheel_metadata.st_ino),
    )
    assert calls == [
        (expected[0], python_311, expected[1], "3.11", expected[2], expected[3]),
        (expected[0], python_312, expected[1], "3.12", expected[2], expected[3]),
    ]


def test_release_verifier_reports_smoke_failure_without_traceback_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dist = _valid_dist(tmp_path)
    python_311 = _python(tmp_path / "python3.11")
    python_312 = _python(tmp_path / "python3.12")
    uv = _python(tmp_path / "uv")

    monkeypatch.setattr(
        release,
        "resolve_smoke_pythons",
        lambda _requested, **_kwargs: (python_311, python_312),
    )

    def fail(*_args: object, **_kwargs: object) -> None:
        raise smoke.ArtifactSmokeError("isolated command failed")

    monkeypatch.setattr(release, "smoke_install_artifact", fail)

    with pytest.raises(release.ReleaseArtifactError, match="artifact smoke failed"):
        release.verify_release_artifacts(
            dist,
            (python_311, python_312),
            uv_executable=str(uv),
        )


@pytest.mark.parametrize("changed", ("wheel", "sdist"))
def test_release_verifier_rechecks_artifact_identity_and_digest_around_each_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    dist = _valid_dist(tmp_path)
    python_311 = _python(tmp_path / "python3.11")
    python_312 = _python(tmp_path / "python3.12")
    uv = _python(tmp_path / "uv")
    target = dist / (WHEEL_NAME if changed == "wheel" else SDIST_NAME)
    original_metadata = target.stat()

    monkeypatch.setattr(
        release,
        "resolve_smoke_pythons",
        lambda _requested, **_kwargs: (python_311, python_312),
    )

    def replace(*_args: object, **_kwargs: object) -> None:
        content = bytearray(target.read_bytes())
        content[-1] ^= 1
        target.write_bytes(content)
        os.utime(
            target,
            ns=(original_metadata.st_atime_ns, original_metadata.st_mtime_ns),
        )

    monkeypatch.setattr(release, "smoke_install_artifact", replace)

    with pytest.raises(release.ReleaseArtifactError, match="artifact changed"):
        release.verify_release_artifacts(
            dist,
            (python_311, python_312),
            uv_executable=str(uv),
        )


def test_clean_install_smoke_uses_only_new_venv_and_isolated_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "sentinel.txt").write_text("repository", encoding="utf-8")
    default_runtime = tmp_path / "default-runtime"
    default_runtime.mkdir()
    (default_runtime / "sentinel.txt").write_text("runtime", encoding="utf-8")
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    system_python = _python(tmp_path / "system" / "python3.11")
    uv = _python(tmp_path / "tools" / "uv")
    temporary_parent = tmp_path / "smoke-temp"
    temporary_parent.mkdir()
    calls: list[tuple[list[str], dict[str, str], Path]] = []
    timeouts: list[float] = []
    serve_calls: list[tuple[Path, dict[str, str], Path, Path, Path]] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        environment = dict(kwargs["env"])  # type: ignore[arg-type]
        cwd = Path(kwargs["cwd"])  # type: ignore[arg-type]
        timeouts.append(float(kwargs["timeout"]))
        calls.append((command, environment, cwd))
        if command[1:3] == ["venv", "--python"]:
            venv = Path(command[-1])
            _python(venv / "bin" / "python")
            _python(venv / "bin" / "memory-hub")
            stdout = ""
        elif command[1:2] == ["-I"]:
            stdout = json.dumps({"major": 3, "minor": 11})
        elif command[1:3] == ["pip", "install"]:
            assert Path(command[-1]).read_bytes() == b"wheel"
            stdout = ""
        elif command[-1:] == ["--help"]:
            stdout = "Usage: memory-hub"
        elif command[-1:] == ["version"]:
            stdout = PROJECT_VERSION
        elif command[-3:] == ["init", "--format", "json"]:
            stdout = json.dumps({"status": "initialized"})
        elif command[-3:] == ["setup", "--format", "json"]:
            stdout = json.dumps(
                {
                    "setup_completed": False,
                    "setup_status": "inspected",
                    "status": "ok",
                }
            )
        elif command[-3:] == ["doctor", "--format", "json"]:
            stdout = json.dumps(
                {
                    "checks": [
                        {"name": "database_quick_check", "status": "pass"},
                        {"name": "codex_automation", "status": "warn"},
                    ],
                    "status": "warn",
                }
            )
        else:
            raise AssertionError(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=stdout,
            stderr="",
        )

    monkeypatch.setattr(smoke.subprocess, "run", run)
    monkeypatch.setattr(
        smoke,
        "_smoke_loopback_serve",
        lambda executable, *, env, cwd, config_path, access_token_path, **_kwargs: (
            serve_calls.append(
                (
                    Path(executable),
                    dict(env),
                    Path(cwd),
                    Path(config_path),
                    Path(access_token_path),
                )
            )
        ),
    )

    smoke.smoke_install_artifact(
        wheel,
        system_python,
        expected_version=PROJECT_VERSION,
        expected_python_version="3.11",
        expected_wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        expected_wheel_identity=(wheel.stat().st_dev, wheel.stat().st_ino),
        repository_root=repository,
        default_runtime_root=default_runtime,
        temporary_parent=temporary_parent,
        uv_executable=str(uv),
    )

    venv = Path(calls[0][0][-1])
    venv_python = (venv / "bin" / "python").resolve()
    executable = (venv / "bin" / "memory-hub").resolve()
    assert calls[0][0] == [
        str(uv),
        "venv",
        "--python",
        str(system_python),
        str(venv),
    ]
    assert calls[1][0] == [str(venv_python), "-I", "-c", smoke._PYTHON_IDENTITY_SCRIPT]
    assert calls[2][0][:4] == [
        str(uv),
        "pip",
        "install",
        "--python",
    ]
    assert calls[2][0][4] == str(venv_python)
    staged_wheel = Path(calls[2][0][5])
    assert staged_wheel.name == wheel.name
    assert staged_wheel != wheel
    runtime_config = venv.parent / "runtime" / "config.toml"
    assert [call[0] for call in calls[3:]] == [
        [str(executable), "--help"],
        [str(executable), "version"],
        [str(executable), "--config", str(runtime_config), "init", "--format", "json"],
        [str(executable), "--config", str(runtime_config), "setup", "--format", "json"],
        [str(executable), "--config", str(runtime_config), "doctor", "--format", "json"],
    ]
    assert serve_calls == [
        (
            executable,
            calls[-1][1],
            calls[-1][2],
            runtime_config,
            runtime_config.parent / "access-token",
        )
    ]
    isolated = calls[-1][1]
    for variable in ("HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert Path(isolated[variable]).is_absolute()
        assert not Path(isolated[variable]).exists()
    assert isolated["PYTHONNOUSERSITE"] == "1"
    assert isolated["PYTHONPATH"] == ""
    assert isolated["UV_NO_CONFIG"] == "1"
    assert set(timeouts) == {300.0}
    assert list(temporary_parent.iterdir()) == []
    assert (repository / "sentinel.txt").read_text(encoding="utf-8") == "repository"
    assert (default_runtime / "sentinel.txt").read_text(encoding="utf-8") == "runtime"


def test_isolated_environment_does_not_inherit_ambient_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRIVATE_RELEASE_TOKEN", "must-not-leak")

    environment = smoke._isolated_environment(tmp_path)

    assert set(environment) == {
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "TMPDIR",
        "PIP_CONFIG_FILE",
        "PYTHONNOUSERSITE",
        "PYTHONPATH",
        "PYTHONUTF8",
        "UV_CACHE_DIR",
        "UV_NO_CONFIG",
        "NO_COLOR",
    }


@pytest.mark.parametrize(
    ("returncode", "payload"),
    (
        (
            1,
            {
                "checks": [{"name": "database_quick_check", "status": "fail"}],
                "status": "fail",
            },
        ),
        (0, {"checks": [], "status": "pass"}),
        (
            0,
            {
                "checks": [{"name": "database_quick_check", "status": "fail"}],
                "status": "warn",
            },
        ),
        (
            0,
            {
                "checks": [{"name": "database_quick_check", "status": "unknown"}],
                "status": "pass",
            },
        ),
    ),
)
def test_doctor_smoke_rejects_failure_empty_or_malformed_checks(
    returncode: int,
    payload: dict[str, object],
) -> None:
    result = subprocess.CompletedProcess(
        ["memory-hub", "doctor"],
        returncode,
        stdout=json.dumps(payload),
        stderr="",
    )

    with pytest.raises(smoke.ArtifactSmokeError, match="doctor output invalid"):
        smoke._validate_doctor_result(result)


@pytest.mark.parametrize("status", ("pass", "warn"))
def test_doctor_smoke_accepts_nonempty_pass_or_warn_report(status: str) -> None:
    checks = [{"name": "database_quick_check", "status": "pass"}]
    if status == "warn":
        checks.append({"name": "codex_automation", "status": "warn"})
    result = subprocess.CompletedProcess(
        ["memory-hub", "doctor"],
        0,
        stdout=json.dumps({"checks": checks, "status": status}),
        stderr="",
    )

    smoke._validate_doctor_result(result)


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"setup_completed": False, "setup_status": "configured", "status": "ok"},
        {"setup_completed": "false", "setup_status": "inspected", "status": "ok"},
        {"setup_completed": False, "setup_status": "inspected", "status": "failed"},
    ),
)
def test_setup_smoke_rejects_malformed_status(payload: dict[str, object]) -> None:
    result = subprocess.CompletedProcess(
        ["memory-hub", "setup"],
        0,
        stdout=json.dumps(payload),
        stderr="",
    )

    with pytest.raises(smoke.ArtifactSmokeError, match="setup output invalid"):
        smoke._validate_setup_result(result)


def test_clean_install_smoke_rejects_symlinked_default_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime_target = tmp_path / "runtime-target"
    runtime_target.mkdir()
    runtime_link = tmp_path / "runtime-link"
    runtime_link.symlink_to(runtime_target, target_is_directory=True)
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    system_python = _python(tmp_path / "system" / "python3.11")
    uv = _python(tmp_path / "tools" / "uv")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(smoke.ArtifactSmokeError, match="runtime root must not be a symlink"):
        smoke.smoke_install_artifact(
            wheel,
            system_python,
            expected_version=PROJECT_VERSION,
            expected_python_version="3.11",
            expected_wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            expected_wheel_identity=(wheel.stat().st_dev, wheel.stat().st_ino),
            repository_root=repository,
            default_runtime_root=runtime_link,
            uv_executable=str(uv),
        )


def test_clean_install_smoke_rejects_symlinked_runtime_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    runtime_target_parent = tmp_path / "runtime-target-parent"
    runtime_target_parent.mkdir()
    runtime_link_parent = tmp_path / "runtime-link-parent"
    runtime_link_parent.symlink_to(runtime_target_parent, target_is_directory=True)
    runtime = runtime_link_parent / "runtime"
    runtime.mkdir()
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    system_python = _python(tmp_path / "system" / "python3.11")
    uv = _python(tmp_path / "tools" / "uv")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(smoke.ArtifactSmokeError, match="runtime root must not use symlinks"):
        smoke.smoke_install_artifact(
            wheel,
            system_python,
            expected_version=PROJECT_VERSION,
            expected_python_version="3.11",
            expected_wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            expected_wheel_identity=(wheel.stat().st_dev, wheel.stat().st_ino),
            repository_root=repository,
            default_runtime_root=runtime,
            uv_executable=str(uv),
        )


def test_clean_install_smoke_rejects_wrong_venv_python_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    system_python = _python(tmp_path / "system" / "python3.11")
    uv = _python(tmp_path / "tools" / "uv")
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:3] == ["venv", "--python"]:
            venv = Path(command[-1])
            _python(venv / "bin" / "python")
            _python(venv / "bin" / "memory-hub")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
        if command[1:2] == ["-I"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"major": 3, "minor": 12}),
                stderr="",
            )
        pytest.fail(f"unexpected command: {command}")

    monkeypatch.setattr(smoke.subprocess, "run", run)

    with pytest.raises(smoke.ArtifactSmokeError, match="venv Python version mismatch"):
        smoke.smoke_install_artifact(
            wheel,
            system_python,
            expected_version=PROJECT_VERSION,
            expected_python_version="3.11",
            expected_wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            expected_wheel_identity=(wheel.stat().st_dev, wheel.stat().st_ino),
            repository_root=repository,
            uv_executable=str(uv),
        )

    assert len(commands) == 2


def test_clean_install_smoke_rejects_unbound_wheel_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    system_python = _python(tmp_path / "system" / "python3.11")
    uv = _python(tmp_path / "tools" / "uv")
    monkeypatch.setattr(
        smoke.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("subprocess must not run"),
    )

    with pytest.raises(smoke.ArtifactSmokeError, match="wheel changed"):
        smoke.smoke_install_artifact(
            wheel,
            system_python,
            expected_version=PROJECT_VERSION,
            expected_python_version="3.11",
            expected_wheel_sha256="0" * 64,
            expected_wheel_identity=(wheel.stat().st_dev, wheel.stat().st_ino),
            repository_root=repository,
            uv_executable=str(uv),
        )


def test_smoke_cli_rejects_symlinked_wheel_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wheel_target = tmp_path / WHEEL_NAME
    wheel_target.write_bytes(b"wheel")
    wheel_link = tmp_path / "linked.whl"
    wheel_link.symlink_to(wheel_target)
    system_python = _python(tmp_path / "system" / "python3.11")
    uv = _python(tmp_path / "tools" / "uv")
    arguments = SimpleNamespace(
        wheel=wheel_link,
        system_python=system_python,
        expected_version=PROJECT_VERSION,
        expected_python_version="3.11",
        repository_root=tmp_path,
    )
    monkeypatch.setattr(
        smoke,
        "_parser",
        lambda: SimpleNamespace(parse_args=lambda: arguments),
    )
    monkeypatch.setattr(smoke.shutil, "which", lambda _name: str(uv))
    monkeypatch.setattr(
        smoke,
        "smoke_install_artifact",
        lambda *_args, **_kwargs: pytest.fail("symlinked wheel must not be dispatched"),
    )

    with pytest.raises(SystemExit, match="wheel is unavailable"):
        smoke.main()


@pytest.mark.parametrize("changed", ("repository", "runtime"))
def test_clean_install_smoke_rejects_host_state_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    changed: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    default_runtime = tmp_path / "default-runtime"
    default_runtime.mkdir()
    wheel = tmp_path / WHEEL_NAME
    wheel.write_bytes(b"wheel")
    system_python = _python(tmp_path / "system" / "python3.11")
    uv = _python(tmp_path / "tools" / "uv")
    target = repository if changed == "repository" else default_runtime

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["venv", "--python"]:
            venv = Path(command[-1])
            _python(venv / "bin" / "python")
            _python(venv / "bin" / "memory-hub")
        if command[1:2] == ["-I"]:
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps({"major": 3, "minor": 11}),
                stderr="",
            )
        if command[-1:] == ["--help"]:
            (target / "changed.txt").write_text("changed", encoding="utf-8")
        stdout = (
            PROJECT_VERSION
            if command[-1:] == ["version"]
            else json.dumps({"status": "initialized"})
            if command[-3:] == ["init", "--format", "json"]
            else json.dumps(
                {
                    "checks": [{"name": "database_quick_check", "status": "pass"}],
                    "status": "pass",
                }
            )
            if command[-3:] == ["doctor", "--format", "json"]
            else "Usage"
        )
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(smoke.subprocess, "run", run)
    monkeypatch.setattr(smoke, "_smoke_loopback_serve", lambda *_args, **_kwargs: None)

    with pytest.raises(smoke.ArtifactSmokeError, match=f"{changed} changed"):
        smoke.smoke_install_artifact(
            wheel,
            system_python,
            expected_version=PROJECT_VERSION,
            expected_python_version="3.11",
            expected_wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
            expected_wheel_identity=(wheel.stat().st_dev, wheel.stat().st_ino),
            repository_root=repository,
            default_runtime_root=default_runtime,
            uv_executable=str(uv),
        )


def test_loopback_serve_smoke_terminates_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = _python(tmp_path / "memory-hub")
    process = SimpleNamespace(
        pid=4242,
        poll=lambda: None,
        wait_calls=[],
    )

    def wait(timeout: float) -> int:
        process.wait_calls.append(timeout)
        return 0

    process.wait = wait
    commands: list[list[str]] = []
    popen_kwargs: list[dict[str, object]] = []
    signals: list[tuple[int, signal.Signals]] = []
    requests: list[tuple[str, str, dict[str, str]]] = []

    def popen(command: list[str], **kwargs: object) -> SimpleNamespace:
        commands.append(command)
        popen_kwargs.append(kwargs)
        return process

    class _Response:
        def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.status = status
            self.headers = headers
            self.body = body

        def getheader(self, name: str) -> str | None:
            return self.headers.get(name.casefold())

        def read(self, _limit: int) -> bytes:
            return self.body

    class _Connection:
        def __init__(self, host: str, port: int, timeout: float) -> None:
            assert (host, port, timeout) == ("127.0.0.1", 43123, 0.2)
            self.response: _Response | None = None

        def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
            requests.append((method, path, headers))
            if path == "/" and "Cookie" not in headers:
                self.response = _Response(
                    401,
                    {"content-type": "text/html; charset=utf-8"},
                    b"Authentication required",
                )
            elif headers.get("Cookie") == f"pmh_session={'A' + ('a' * 42)}":
                self.response = _Response(
                    401,
                    {"content-type": "text/html; charset=utf-8"},
                    b"Authentication required",
                )
            elif path == f"/?token={'A' + ('a' * 42)}":
                self.response = _Response(
                    401,
                    {"content-type": "text/html; charset=utf-8"},
                    b"Authentication required",
                )
            elif path == f"/?token={'a' * 43}":
                self.response = _Response(
                    303,
                    {
                        "location": "/",
                        "set-cookie": f"pmh_session={'s' * 43}; HttpOnly",
                    },
                    b"",
                )
            elif path == "/" and headers.get("Cookie") == f"pmh_session={'s' * 43}":
                self.response = _Response(
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    b"<title>Overview - Project Memory Hub</title>",
                )
            elif path == "/setup" and headers.get("Cookie") == f"pmh_session={'s' * 43}":
                self.response = _Response(
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    b'<div data-setup><form action="/setup/configure"></form></div>',
                )
            else:
                raise AssertionError((method, path, headers))

        def getresponse(self) -> _Response:
            assert self.response is not None
            return self.response

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(smoke, "_reserve_loopback_port", lambda: 43123)
    monkeypatch.setattr(smoke.subprocess, "Popen", popen)
    monkeypatch.setattr(smoke.http.client, "HTTPConnection", _Connection)

    def killpg(pid: int, selected_signal: signal.Signals | int) -> None:
        if selected_signal == 0:
            raise ProcessLookupError
        signals.append((pid, selected_signal))

    monkeypatch.setattr(smoke.os, "killpg", killpg)
    access_token = tmp_path / "access-token"
    access_token.write_text("a" * 43, encoding="ascii")
    access_token.chmod(0o600)
    config_path = tmp_path / "config.toml"

    smoke._smoke_loopback_serve(
        executable,
        env={},
        cwd=tmp_path,
        config_path=config_path,
        access_token_path=access_token,
        startup_timeout=0.1,
    )

    assert commands == [
        [
            str(executable),
            "--config",
            str(config_path),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "43123",
        ]
    ]
    assert popen_kwargs[0]["start_new_session"] is True
    assert requests == [
        (
            "GET",
            "/",
            {"Accept": "text/html", "Connection": "close"},
        ),
        (
            "GET",
            "/",
            {
                "Accept": "text/html",
                "Connection": "close",
                "Cookie": f"pmh_session={'A' + ('a' * 42)}",
            },
        ),
        (
            "GET",
            f"/?token={'A' + ('a' * 42)}",
            {"Accept": "text/html", "Connection": "close"},
        ),
        (
            "GET",
            f"/?token={'a' * 43}",
            {"Accept": "text/html", "Connection": "close"},
        ),
        (
            "GET",
            "/",
            {
                "Accept": "text/html",
                "Connection": "close",
                "Cookie": f"pmh_session={'s' * 43}",
            },
        ),
        (
            "GET",
            "/setup",
            {
                "Accept": "text/html",
                "Connection": "close",
                "Cookie": f"pmh_session={'s' * 43}",
            },
        ),
    ]
    assert signals == [(4242, signal.SIGTERM)]
    assert process.wait_calls == [5.0]


def test_loopback_http_probe_rejects_unrelated_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.status = status
            self.headers = headers
            self.body = body

        def getheader(self, name: str) -> str | None:
            return self.headers.get(name.casefold())

        def read(self, _limit: int) -> bytes:
            return self.body

    responses = [
        _Response(
            401,
            {"content-type": "text/html"},
            b"Authentication required",
        ),
        _Response(
            401,
            {"content-type": "text/html"},
            b"Authentication required",
        ),
        _Response(
            401,
            {"content-type": "text/html"},
            b"Authentication required",
        ),
        _Response(
            303,
            {
                "location": "/",
                "set-cookie": f"pmh_session={'s' * 43}; HttpOnly",
            },
            b"",
        ),
        _Response(
            200,
            {"content-type": "text/html"},
            b"<title>Another local service</title>",
        ),
    ]

    class _Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def request(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def getresponse() -> _Response:
            return responses.pop(0)

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(smoke.http.client, "HTTPConnection", _Connection)
    access_token = tmp_path / "access-token"
    access_token.write_text("a" * 43, encoding="ascii")
    access_token.chmod(0o600)

    with pytest.raises(smoke.ArtifactSmokeError, match="identity mismatch"):
        smoke._request_loopback_identity(43123, access_token, timeout=0.2)


def test_loopback_http_probe_rejects_an_unrelated_setup_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.status = status
            self.headers = headers
            self.body = body

        def getheader(self, name: str) -> str | None:
            return self.headers.get(name.casefold())

        def read(self, _limit: int) -> bytes:
            return self.body

    responses = [
        _Response(401, {"content-type": "text/html"}, b"Authentication required"),
        _Response(401, {"content-type": "text/html"}, b"Authentication required"),
        _Response(401, {"content-type": "text/html"}, b"Authentication required"),
        _Response(
            303,
            {
                "location": "/",
                "set-cookie": f"pmh_session={'s' * 43}; HttpOnly",
            },
            b"",
        ),
        _Response(
            200,
            {"content-type": "text/html"},
            b"<title>Overview - Project Memory Hub</title>",
        ),
        _Response(
            200,
            {"content-type": "text/html"},
            b"<title>Another local page</title>",
        ),
    ]

    class _Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        @staticmethod
        def request(*_args: object, **_kwargs: object) -> None:
            return None

        @staticmethod
        def getresponse() -> _Response:
            return responses.pop(0)

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(smoke.http.client, "HTTPConnection", _Connection)
    access_token = tmp_path / "access-token"
    access_token.write_text("a" * 43, encoding="ascii")
    access_token.chmod(0o600)

    with pytest.raises(smoke.ArtifactSmokeError, match="setup route mismatch"):
        smoke._request_loopback_identity(43123, access_token, timeout=0.2)


def test_loopback_http_probe_rejects_service_accepting_any_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
            self.status = status
            self.headers = headers
            self.body = body

        def getheader(self, name: str) -> str | None:
            return self.headers.get(name.casefold())

        def read(self, _limit: int) -> bytes:
            return self.body

    class _Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.response: _Response | None = None

        def request(self, _method: str, path: str, *, headers: dict[str, str]) -> None:
            if path.startswith("/?token="):
                self.response = _Response(
                    303,
                    {
                        "location": "/",
                        "set-cookie": f"pmh_session={'s' * 43}; HttpOnly",
                    },
                    b"",
                )
            elif headers.get("Cookie") == f"pmh_session={'s' * 43}":
                self.response = _Response(
                    200,
                    {"content-type": "text/html"},
                    b"<title>Overview - Project Memory Hub</title>",
                )
            else:
                self.response = _Response(
                    401,
                    {"content-type": "text/html"},
                    b"Authentication required",
                )

        def getresponse(self) -> _Response:
            assert self.response is not None
            return self.response

        @staticmethod
        def close() -> None:
            return None

    monkeypatch.setattr(smoke.http.client, "HTTPConnection", _Connection)
    access_token = tmp_path / "access-token"
    access_token.write_text("a" * 43, encoding="ascii")
    access_token.chmod(0o600)

    with pytest.raises(smoke.ArtifactSmokeError, match="identity mismatch"):
        smoke._request_loopback_identity(43123, access_token, timeout=0.2)


def test_process_group_cleanup_escalates_to_kill_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=4343, wait_calls=[])
    signals: list[tuple[int, signal.Signals]] = []

    def wait(timeout: float) -> int:
        process.wait_calls.append(timeout)
        if len(process.wait_calls) == 1:
            raise subprocess.TimeoutExpired(["memory-hub"], timeout)
        return 0

    process.wait = wait

    def killpg(pid: int, selected_signal: signal.Signals | int) -> None:
        if selected_signal == 0:
            raise ProcessLookupError
        signals.append((pid, selected_signal))

    monkeypatch.setattr(smoke.os, "killpg", killpg)

    smoke._terminate_process_group(process)

    assert signals == [(4343, signal.SIGTERM), (4343, signal.SIGKILL)]
    assert process.wait_calls == [5.0, 5.0]


def test_process_group_cleanup_kills_descendants_after_leader_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(pid=4444, wait_calls=[])
    signals: list[tuple[int, signal.Signals]] = []
    group_probes = 0

    def wait(timeout: float) -> int:
        process.wait_calls.append(timeout)
        return 0

    def killpg(pid: int, selected_signal: signal.Signals | int) -> None:
        nonlocal group_probes
        if selected_signal == 0:
            group_probes += 1
            if group_probes >= 2:
                raise ProcessLookupError
            return
        signals.append((pid, selected_signal))

    process.wait = wait
    monkeypatch.setattr(smoke.os, "killpg", killpg)

    smoke._terminate_process_group(process)

    assert signals == [(4444, signal.SIGTERM), (4444, signal.SIGKILL)]
    assert process.wait_calls == [5.0]
    assert group_probes == 2
