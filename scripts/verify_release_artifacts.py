from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tomllib
import unicodedata
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import Parser
from pathlib import Path, PurePosixPath
from typing import Any

try:
    from scripts.smoke_install_artifact import ArtifactSmokeError, smoke_install_artifact
    from scripts.verify_wheel import verify_wheel
except ModuleNotFoundError:  # Direct script execution sets scripts/ as sys.path[0].
    from smoke_install_artifact import (  # type: ignore[no-redef]
        ArtifactSmokeError,
        smoke_install_artifact,
    )
    from verify_wheel import verify_wheel


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = PROJECT_ROOT / "pyproject.toml"
EXPECTED_SMOKE_VERSIONS = ("3.11", "3.12")
EXPECTED_ENTRY_POINT = "project_memory_hub.cli:app"
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024
_EXPECTED_SDIST_GITIGNORE_SHA256 = (
    "2baadf3d481dcf158b914687c580883db9b3f3e0b54c43041f20f56f072ab558"
)
_PYTHON_IDENTITY_SCRIPT = (
    'import json,sys;print(json.dumps({"major":sys.version_info.major,'
    '"minor":sys.version_info.minor},sort_keys=True))'
)
_EXPECTED_DIST_INFO_FILES = frozenset(
    {
        "METADATA",
        "WHEEL",
        "entry_points.txt",
        "licenses/LICENSE",
        "RECORD",
    }
)


class ReleaseArtifactError(RuntimeError):
    """Raised when a release artifact set is incomplete or inconsistent."""


@dataclass(frozen=True, slots=True)
class ArtifactFingerprint:
    device: int
    inode: int
    size: int
    sha256: str

    @property
    def identity(self) -> tuple[int, int]:
        return (self.device, self.inode)


@dataclass(frozen=True, slots=True)
class ReleaseArtifactBundle:
    wheel: Path
    sdist: Path
    project_name: str
    project_version: str
    wheel_fingerprint: ArtifactFingerprint
    sdist_fingerprint: ArtifactFingerprint


def _load_project(path: Path = PROJECT_FILE) -> dict[str, Any]:
    try:
        with Path(path).open("rb") as file:
            document = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ReleaseArtifactError("project metadata unavailable") from error
    project = document.get("project")
    if type(project) is not dict:
        raise ReleaseArtifactError("project metadata unavailable")
    name = project.get("name")
    version = project.get("version")
    if not isinstance(name, str) or not name.strip():
        raise ReleaseArtifactError("project metadata unavailable")
    if not isinstance(version, str) or not version.strip():
        raise ReleaseArtifactError("project metadata unavailable")
    return project


def _distribution_name(project_name: str) -> str:
    return re.sub(r"[-_.]+", "_", project_name)


def _expected_runtime_members() -> frozenset[str]:
    source_root = PROJECT_ROOT / "src"
    package_root = source_root / "project_memory_hub"
    members: set[str] = set()
    for path in package_root.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            members.add(path.relative_to(source_root).as_posix())
    if not members:
        raise ReleaseArtifactError("runtime member manifest unavailable")
    return frozenset(members)


def _canonical_archive_name(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _validate_archive_inventory(
    members: list[tuple[str, int]],
    *,
    archive_label: str,
) -> list[str]:
    if len(members) > _MAX_ARCHIVE_MEMBERS:
        raise ReleaseArtifactError(f"{archive_label} exceeds archive limit")
    names = [name for name, _size in members]
    if len(names) != len(set(names)):
        raise ReleaseArtifactError(f"{archive_label} contains duplicate archive paths")
    canonical_names = [_canonical_archive_name(name) for name in names]
    if len(canonical_names) != len(set(canonical_names)):
        raise ReleaseArtifactError(f"{archive_label} contains archive path collision")
    total_size = 0
    for name, size in members:
        if size < 0 or size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise ReleaseArtifactError(f"{archive_label} exceeds archive limit")
        total_size += size
        if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
            raise ReleaseArtifactError(f"{archive_label} exceeds archive limit")
        folded_parts = {
            unicodedata.normalize("NFC", part).casefold()
            for part in PurePosixPath(name.rstrip("/")).parts
        }
        if any(
            part == ".env" or part.endswith(".env") or part.startswith(".env.")
            for part in folded_parts
        ):
            raise ReleaseArtifactError(f"{archive_label} contains forbidden release file")
    return names


def _artifact_fingerprint(path: Path) -> ArtifactFingerprint:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ReleaseArtifactError("release artifact file is unavailable") from error
    digest = hashlib.sha256()
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReleaseArtifactError("release artifact file is unavailable")
        with os.fdopen(descriptor, "rb", closefd=False) as file:
            for block in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(block)
        after = os.fstat(descriptor)
    except OSError as error:
        raise ReleaseArtifactError("release artifact file is unavailable") from error
    finally:
        os.close(descriptor)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ):
        raise ReleaseArtifactError("release artifact changed during verification")
    try:
        current = path.lstat()
    except OSError as error:
        raise ReleaseArtifactError("release artifact file is unavailable") from error
    if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        raise ReleaseArtifactError("release artifact changed during verification")
    return ArtifactFingerprint(
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        sha256=digest.hexdigest(),
    )


def _safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    trimmed = name.rstrip("/")
    path = PurePosixPath(trimmed)
    return (
        bool(path.parts)
        and path.as_posix() == trimmed
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _validate_zip(archive: zipfile.ZipFile, *, allowed_roots: frozenset[str]) -> list[str]:
    members = archive.infolist()
    names = _validate_archive_inventory(
        [(member.filename, member.file_size) for member in members],
        archive_label="wheel",
    )
    for member in members:
        name = member.filename
        if not _safe_archive_name(name):
            raise ReleaseArtifactError("wheel contains unsafe archive path")
        parts = PurePosixPath(name.rstrip("/")).parts
        if parts[0] not in allowed_roots:
            raise ReleaseArtifactError("wheel contains unsafe archive path")
        mode = member.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        directory_name = name.endswith("/")
        if directory_name or file_type not in {0, stat.S_IFREG}:
            raise ReleaseArtifactError("wheel contains unsafe archive path")
    return names


def _single_header(document: str, header: str, message: str) -> str:
    parsed = Parser(policy=policy.compat32).parsestr(document)
    values = parsed.get_all(header, [])
    if len(values) != 1:
        raise ReleaseArtifactError(message)
    return str(values[0])


def _verify_wheel_artifact(wheel: Path, project: dict[str, Any]) -> None:
    project_name = str(project["name"])
    project_version = str(project["version"])
    distribution = _distribution_name(project_name)
    expected_prefix = f"{distribution}-{project_version}-"
    if not wheel.name.startswith(expected_prefix) or not wheel.name.endswith(".whl"):
        raise ReleaseArtifactError("wheel filename identity mismatch")
    dist_info = f"{distribution}-{project_version}.dist-info"
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = _validate_zip(
                archive,
                allowed_roots=frozenset({"project_memory_hub", dist_info}),
            )
            metadata_name = f"{dist_info}/METADATA"
            entry_points_name = f"{dist_info}/entry_points.txt"
            if names.count(metadata_name) != 1:
                raise ReleaseArtifactError("wheel METADATA missing")
            if names.count(entry_points_name) != 1:
                raise ReleaseArtifactError("wheel console entry point missing")
            expected_runtime = _expected_runtime_members()
            actual_runtime = frozenset(
                name
                for name in names
                if name.startswith("project_memory_hub/") and not name.endswith("/")
            )
            if actual_runtime != expected_runtime:
                missing = sorted(expected_runtime - actual_runtime)
                extra = sorted(actual_runtime - expected_runtime)
                raise ReleaseArtifactError(
                    f"wheel runtime member manifest mismatch: missing={missing}, extra={extra}"
                )
            actual_dist_info = frozenset(
                name.removeprefix(f"{dist_info}/")
                for name in names
                if name.startswith(f"{dist_info}/") and not name.endswith("/")
            )
            if actual_dist_info != _EXPECTED_DIST_INFO_FILES:
                raise ReleaseArtifactError("wheel metadata member manifest mismatch")
            metadata = archive.read(metadata_name).decode("utf-8", errors="strict")
            entry_points = archive.read(entry_points_name).decode("utf-8", errors="strict")
    except (OSError, UnicodeError, zipfile.BadZipFile, KeyError) as error:
        raise ReleaseArtifactError("wheel archive invalid") from error
    try:
        verify_wheel(set(names), metadata, entry_points)
    except SystemExit as error:
        raise ReleaseArtifactError(str(error)) from error


def _read_tar_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    extracted = archive.extractfile(member)
    if extracted is None:
        raise ReleaseArtifactError("sdist archive invalid")
    try:
        return extracted.read()
    except OSError as error:
        raise ReleaseArtifactError("sdist archive invalid") from error


def _expected_sdist_source_members(root: str) -> frozenset[str]:
    return frozenset(
        {
            f"{root}/PKG-INFO",
            f"{root}/.gitignore",
            f"{root}/LICENSE",
            f"{root}/README.md",
            f"{root}/pyproject.toml",
            *{f"{root}/src/{name}" for name in _expected_runtime_members()},
        }
    )


def _verify_sdist_artifact(sdist: Path, project: dict[str, Any]) -> None:
    project_name = str(project["name"])
    project_version = str(project["version"])
    distribution = _distribution_name(project_name)
    root = f"{distribution}-{project_version}"
    if sdist.name != f"{root}.tar.gz":
        raise ReleaseArtifactError("sdist filename identity mismatch")
    expected_members = _expected_sdist_source_members(root)
    try:
        with tarfile.open(sdist, mode="r:gz") as archive:
            members = archive.getmembers()
            names = _validate_archive_inventory(
                [(member.name, member.size) for member in members],
                archive_label="sdist",
            )
            for member in members:
                if not _safe_archive_name(member.name):
                    raise ReleaseArtifactError("sdist contains unsafe archive path")
                parts = PurePosixPath(member.name.rstrip("/")).parts
                if parts[0] != root or not member.isreg():
                    raise ReleaseArtifactError("sdist contains unsafe archive path")
            runtime_prefix = f"{root}/src/"
            actual_runtime = frozenset(
                member.name.removeprefix(runtime_prefix)
                for member in members
                if member.isreg() and member.name.startswith(f"{runtime_prefix}project_memory_hub/")
            )
            if actual_runtime != _expected_runtime_members():
                expected_runtime = _expected_runtime_members()
                missing = sorted(expected_runtime - actual_runtime)
                extra = sorted(actual_runtime - expected_runtime)
                raise ReleaseArtifactError(
                    f"sdist runtime member manifest mismatch: missing={missing}, extra={extra}"
                )
            actual_files = frozenset(names)
            if actual_files != expected_members:
                missing = sorted(expected_members - actual_files)
                extra = sorted(actual_files - expected_members)
                raise ReleaseArtifactError(
                    f"sdist source member manifest mismatch: missing={missing}, extra={extra}"
                )
            by_name = {member.name: member for member in members}
            gitignore = _read_tar_member(archive, by_name[f"{root}/.gitignore"])
            if hashlib.sha256(gitignore).hexdigest() != _EXPECTED_SDIST_GITIGNORE_SHA256:
                raise ReleaseArtifactError("sdist .gitignore content mismatch")
            pkg_info = _read_tar_member(archive, by_name[f"{root}/PKG-INFO"]).decode(
                "utf-8", errors="strict"
            )
            pyproject_bytes = _read_tar_member(archive, by_name[f"{root}/pyproject.toml"])
    except ReleaseArtifactError:
        raise
    except (OSError, UnicodeError, tarfile.TarError, KeyError) as error:
        raise ReleaseArtifactError("sdist archive invalid") from error

    if _single_header(pkg_info, "Name", "sdist metadata name mismatch") != project_name:
        raise ReleaseArtifactError("sdist metadata name mismatch")
    if _single_header(pkg_info, "Version", "sdist metadata version mismatch") != project_version:
        raise ReleaseArtifactError("sdist metadata version mismatch")
    try:
        archived_document = tomllib.loads(pyproject_bytes.decode("utf-8", errors="strict"))
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ReleaseArtifactError("sdist pyproject metadata invalid") from error
    archived_project = archived_document.get("project")
    if type(archived_project) is not dict:
        raise ReleaseArtifactError("sdist pyproject metadata invalid")
    if archived_project.get("name") != project_name:
        raise ReleaseArtifactError("sdist pyproject name mismatch")
    if archived_project.get("version") != project_version:
        raise ReleaseArtifactError("sdist pyproject version mismatch")
    if archived_project.get("scripts") != {"memory-hub": EXPECTED_ENTRY_POINT}:
        raise ReleaseArtifactError("sdist entry point mismatch")


def inspect_release_artifacts(
    dist: Path,
    *,
    project_file: Path = PROJECT_FILE,
) -> ReleaseArtifactBundle:
    project = _load_project(project_file)
    selected = Path(dist)
    try:
        metadata = selected.lstat()
        directory = selected.resolve(strict=True)
    except OSError as error:
        raise ReleaseArtifactError("release artifact directory unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ReleaseArtifactError("release artifact directory unavailable")
    entries = sorted(directory.iterdir(), key=lambda path: path.name)
    try:
        if any(stat.S_ISLNK(entry.lstat().st_mode) for entry in entries):
            raise ReleaseArtifactError("release artifact file must not be a symlink")
    except OSError as error:
        raise ReleaseArtifactError("release artifact file is unavailable") from error
    housekeeping = directory / ".gitignore"
    if housekeeping in entries:
        try:
            housekeeping_metadata = housekeeping.lstat()
            valid_housekeeping = (
                stat.S_ISREG(housekeeping_metadata.st_mode)
                and not stat.S_ISLNK(housekeeping_metadata.st_mode)
                and housekeeping.read_bytes() == b"*"
            )
        except OSError:
            valid_housekeeping = False
        if valid_housekeeping:
            entries.remove(housekeeping)
    wheels = [path for path in entries if path.is_file() and path.suffix == ".whl"]
    sdists = [path for path in entries if path.is_file() and path.name.endswith(".tar.gz")]
    if len(wheels) != 1 or len(sdists) != 1 or len(entries) != 2:
        raise ReleaseArtifactError("expected exactly one wheel and one sdist")
    wheel = wheels[0].resolve(strict=True)
    sdist = sdists[0].resolve(strict=True)
    wheel_before = _artifact_fingerprint(wheel)
    sdist_before = _artifact_fingerprint(sdist)
    _verify_wheel_artifact(wheel, project)
    _verify_sdist_artifact(sdist, project)
    wheel_after = _artifact_fingerprint(wheel)
    sdist_after = _artifact_fingerprint(sdist)
    if wheel_after != wheel_before or sdist_after != sdist_before:
        raise ReleaseArtifactError("release artifact changed during verification")
    return ReleaseArtifactBundle(
        wheel=wheel,
        sdist=sdist,
        project_name=str(project["name"]),
        project_version=str(project["version"]),
        wheel_fingerprint=wheel_after,
        sdist_fingerprint=sdist_after,
    )


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _resolve_uv_executable(value: str) -> Path:
    selected = Path(value)
    if not selected.is_absolute():
        raise ReleaseArtifactError("uv executable must be absolute")
    try:
        resolved = selected.resolve(strict=True)
    except OSError as error:
        raise ReleaseArtifactError("uv executable is unavailable") from error
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise ReleaseArtifactError("uv executable is unavailable")
    return resolved


def _interpreter_version(path: Path, *, repository: Path) -> tuple[int, int]:
    try:
        result = subprocess.run(
            [str(path), "-I", "-c", _PYTHON_IDENTITY_SCRIPT],
            cwd=repository,
            capture_output=True,
            text=True,
            check=False,
            timeout=30.0,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReleaseArtifactError("system interpreter identity failed") from error
    try:
        identity = json.loads(result.stdout)
    except (json.JSONDecodeError, UnicodeError) as error:
        raise ReleaseArtifactError("system interpreter identity failed") from error
    if (
        result.returncode != 0
        or type(identity) is not dict
        or type(identity.get("major")) is not int
        or type(identity.get("minor")) is not int
    ):
        raise ReleaseArtifactError("system interpreter identity failed")
    return (identity["major"], identity["minor"])


def resolve_smoke_pythons(
    requested: tuple[Path, ...],
    *,
    repository_root: Path = PROJECT_ROOT,
    uv_executable: str = "uv",
) -> tuple[Path, ...]:
    repository = Path(repository_root).resolve(strict=True)
    uv = _resolve_uv_executable(uv_executable)
    discovered_inputs: list[Path] = []
    discovered: list[Path] = []
    identities: set[tuple[int, int]] = set()
    for version in EXPECTED_SMOKE_VERSIONS:
        command = [str(uv), "python", "find", "--system", version]
        try:
            result = subprocess.run(
                command,
                cwd=repository,
                capture_output=True,
                text=True,
                check=False,
                timeout=30.0,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReleaseArtifactError("uv system interpreter discovery failed") from error
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if result.returncode != 0 or len(lines) != 1:
            raise ReleaseArtifactError("uv system interpreter discovery failed")
        raw = Path(lines[0])
        if not raw.is_absolute():
            raise ReleaseArtifactError("uv system interpreter must be absolute")
        discovered_inputs.append(raw)
        try:
            resolved = raw.resolve(strict=True)
        except OSError as error:
            raise ReleaseArtifactError("uv system interpreter is unavailable") from error
        if (
            not resolved.is_file()
            or not os.access(resolved, os.X_OK)
            or _is_within(resolved, repository)
        ):
            raise ReleaseArtifactError("uv system interpreter is untrusted")
        expected_major, expected_minor = (int(part) for part in version.split(".", 1))
        if _interpreter_version(resolved, repository=repository) != (
            expected_major,
            expected_minor,
        ):
            raise ReleaseArtifactError("uv system interpreter version mismatch")
        file_identity = (resolved.stat().st_dev, resolved.stat().st_ino)
        if file_identity in identities:
            raise ReleaseArtifactError("uv system interpreter identity mismatch")
        identities.add(file_identity)
        discovered.append(resolved)

    requested_inputs: list[Path] = []
    requested_paths: list[Path] = []
    if len(requested) != len(EXPECTED_SMOKE_VERSIONS):
        raise ReleaseArtifactError("requested system interpreter set mismatch")
    for value in requested:
        path = Path(value)
        if not path.is_absolute():
            raise ReleaseArtifactError("requested system interpreter must be absolute")
        requested_inputs.append(path)
        try:
            requested_paths.append(path.resolve(strict=True))
        except OSError as error:
            raise ReleaseArtifactError("requested system interpreter is unavailable") from error
    if tuple(requested_inputs) != tuple(discovered_inputs) or tuple(requested_paths) != tuple(
        discovered
    ):
        raise ReleaseArtifactError("requested system interpreter set mismatch")
    return tuple(discovered)


def _assert_bundle_unchanged(bundle: ReleaseArtifactBundle) -> None:
    if (
        _artifact_fingerprint(bundle.wheel) != bundle.wheel_fingerprint
        or _artifact_fingerprint(bundle.sdist) != bundle.sdist_fingerprint
    ):
        raise ReleaseArtifactError("release artifact changed during smoke verification")


def verify_release_artifacts(
    dist: Path,
    smoke_pythons: tuple[Path, ...],
    *,
    project_file: Path = PROJECT_FILE,
    repository_root: Path = PROJECT_ROOT,
    uv_executable: str = "uv",
) -> ReleaseArtifactBundle:
    bundle = inspect_release_artifacts(dist, project_file=project_file)
    interpreters = resolve_smoke_pythons(
        smoke_pythons,
        repository_root=repository_root,
        uv_executable=uv_executable,
    )
    for python_version, interpreter in zip(
        EXPECTED_SMOKE_VERSIONS,
        interpreters,
        strict=True,
    ):
        _assert_bundle_unchanged(bundle)
        try:
            smoke_install_artifact(
                bundle.wheel,
                interpreter,
                expected_version=bundle.project_version,
                expected_python_version=python_version,
                expected_wheel_sha256=bundle.wheel_fingerprint.sha256,
                expected_wheel_identity=bundle.wheel_fingerprint.identity,
                repository_root=repository_root,
                uv_executable=uv_executable,
            )
        except ArtifactSmokeError as error:
            _assert_bundle_unchanged(bundle)
            raise ReleaseArtifactError("artifact smoke failed") from error
        _assert_bundle_unchanged(bundle)
    return bundle


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify release artifacts and clean installs")
    parser.add_argument("--dist", type=Path, required=True)
    parser.add_argument("--smoke-python", type=Path, action="append", default=[])
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    uv = shutil.which("uv")
    if uv is None:
        raise SystemExit("uv executable is unavailable")
    try:
        uv_executable = str(Path(uv).resolve(strict=True))
    except OSError as error:
        raise SystemExit("uv executable is unavailable") from error
    try:
        verify_release_artifacts(
            arguments.dist,
            tuple(arguments.smoke_python),
            uv_executable=uv_executable,
        )
    except ReleaseArtifactError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
