from __future__ import annotations

import argparse
import hashlib
import os
import re
import secrets
import stat
import tempfile
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, NamedTuple


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_FILE = PROJECT_ROOT / "pyproject.toml"
MANIFEST_NAME = "SHA256SUMS"
HOUSEKEEPING_NAME = ".gitignore"
MAX_MANIFEST_BYTES = 64 * 1024
_MANIFEST_LINE = re.compile(r"(?P<digest>[0-9a-f]{64})  (?P<filename>[^/\\\r\n]+)")


class ChecksumError(RuntimeError):
    """Raised when a release checksum contract is not satisfied."""


class _ArtifactBinding(NamedTuple):
    path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


def _owner_controlled(metadata: os.stat_result) -> bool:
    wrong_owner = hasattr(os, "getuid") and metadata.st_uid != os.getuid()
    unsafe_permissions = bool(metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH))
    return not wrong_owner and not unsafe_permissions


def _safe_housekeeping(dist: Path) -> Path | None:
    housekeeping = dist / HOUSEKEEPING_NAME
    try:
        metadata = housekeeping.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ChecksumError("release_artifact_inventory") from error
    try:
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not _owner_controlled(metadata)
            or housekeeping.read_bytes() != b"*"
        ):
            raise ChecksumError("release_artifact_inventory")
    except OSError as error:
        raise ChecksumError("release_artifact_inventory") from error
    return housekeeping


def _release_directory(dist: Path) -> Path:
    selected = Path(dist)
    try:
        metadata = selected.lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or not _owner_controlled(metadata)
        ):
            raise ChecksumError("release_artifact_inventory")
        resolved = selected.resolve(strict=True)
        resolved_metadata = resolved.stat()
    except ChecksumError:
        raise
    except OSError as error:
        raise ChecksumError("release_artifact_inventory") from error
    if (metadata.st_dev, metadata.st_ino) != (
        resolved_metadata.st_dev,
        resolved_metadata.st_ino,
    ) or not _owner_controlled(resolved_metadata):
        raise ChecksumError("release_artifact_inventory")
    return resolved


def _metadata_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
    )


def _readonly_flags() -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return flags


def _artifact_binding(
    path: Path,
    *,
    expected: _ArtifactBinding | None = None,
) -> _ArtifactBinding:
    file_descriptor: int | None = None
    try:
        path_metadata = path.lstat()
        if (
            stat.S_ISLNK(path_metadata.st_mode)
            or not stat.S_ISREG(path_metadata.st_mode)
            or path_metadata.st_nlink != 1
            or not _owner_controlled(path_metadata)
        ):
            raise OSError("release artifact is not a regular file")
        file_descriptor = os.open(path, _readonly_flags())
        opened_metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or not _owner_controlled(opened_metadata)
            or _metadata_identity(path_metadata) != _metadata_identity(opened_metadata)
        ):
            raise OSError("release artifact changed while opening")
        digest = hashlib.sha256()
        while True:
            chunk = os.read(file_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        final_metadata = os.fstat(file_descriptor)
        if (
            _metadata_identity(opened_metadata) != _metadata_identity(final_metadata)
            or final_metadata.st_nlink != 1
            or not _owner_controlled(final_metadata)
        ):
            raise OSError("release artifact changed while hashing")
        binding = _ArtifactBinding(
            path=path,
            device=final_metadata.st_dev,
            inode=final_metadata.st_ino,
            size=final_metadata.st_size,
            mtime_ns=final_metadata.st_mtime_ns,
            sha256=digest.hexdigest(),
        )
    except OSError as error:
        message = (
            "release_artifact_changed" if expected is not None else "release_artifact_inventory"
        )
        raise ChecksumError(message) from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
    if expected is not None and binding != expected:
        raise ChecksumError("release_artifact_changed")
    return binding


def _manifest_metadata(manifest: Path, *, allow_missing: bool) -> os.stat_result | None:
    try:
        metadata = manifest.lstat()
    except FileNotFoundError:
        if allow_missing:
            return None
        raise ChecksumError("checksum_manifest_inventory") from None
    except OSError as error:
        raise ChecksumError("checksum_manifest_inventory") from error
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or not _owner_controlled(metadata)
        or metadata.st_size > MAX_MANIFEST_BYTES
    ):
        raise ChecksumError("checksum_manifest_inventory")
    return metadata


def _discover_artifacts(release_dir: Path) -> tuple[_ArtifactBinding, _ArtifactBinding]:
    manifest = release_dir / MANIFEST_NAME
    _manifest_metadata(manifest, allow_missing=True)
    housekeeping = _safe_housekeeping(release_dir)
    try:
        entries = tuple(release_dir.iterdir())
    except OSError as error:
        raise ChecksumError("release_artifact_inventory") from error
    excluded = {manifest}
    if housekeeping is not None:
        excluded.add(housekeeping)
    artifacts = tuple(entry for entry in entries if entry not in excluded)
    wheels = tuple(path for path in artifacts if path.suffix == ".whl")
    sdists = tuple(path for path in artifacts if path.name.endswith(".tar.gz"))
    if len(artifacts) != 2 or len(wheels) != 1 or len(sdists) != 1:
        raise ChecksumError("release_artifact_inventory")
    ordered = tuple(sorted((wheels[0], sdists[0]), key=lambda path: path.name))
    bindings = tuple(_artifact_binding(path) for path in ordered)
    return bindings[0], bindings[1]


def _assert_artifacts_unchanged(bindings: tuple[_ArtifactBinding, ...]) -> None:
    for binding in bindings:
        _artifact_binding(binding.path, expected=binding)


def _write_all(file_descriptor: int, content: bytes) -> None:
    view = memoryview(content)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _copy_bound_artifact(binding: _ArtifactBinding, destination: Path) -> None:
    source_descriptor: int | None = None
    destination_descriptor: int | None = None
    destination_created = False
    completed = False
    try:
        source_descriptor = os.open(binding.path, _readonly_flags())
        source_metadata = os.fstat(source_descriptor)
        if (
            _metadata_identity(source_metadata)
            != (
                binding.device,
                binding.inode,
                binding.size,
                binding.mtime_ns,
            )
            or source_metadata.st_nlink != 1
            or not _owner_controlled(source_metadata)
        ):
            raise ChecksumError("release_artifact_changed")
        destination_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        destination_flags |= getattr(os, "O_CLOEXEC", 0)
        destination_flags |= getattr(os, "O_NOFOLLOW", 0)
        destination_descriptor = os.open(destination, destination_flags, 0o600)
        destination_created = True
        digest = hashlib.sha256()
        while True:
            chunk = os.read(source_descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            _write_all(destination_descriptor, chunk)
        os.fsync(destination_descriptor)
        final_source_metadata = os.fstat(source_descriptor)
        if (
            _metadata_identity(final_source_metadata)
            != (binding.device, binding.inode, binding.size, binding.mtime_ns)
            or digest.hexdigest() != binding.sha256
            or final_source_metadata.st_nlink != 1
            or not _owner_controlled(final_source_metadata)
        ):
            raise ChecksumError("release_artifact_changed")
        completed = True
    except ChecksumError:
        raise
    except OSError as error:
        raise ChecksumError("release_artifact_changed") from error
    finally:
        if destination_descriptor is not None:
            os.close(destination_descriptor)
        if source_descriptor is not None:
            os.close(source_descriptor)
        if destination_created and not completed:
            destination.unlink(missing_ok=True)


def _inspect_release_artifacts(
    dist: Path,
    *,
    project_file: Path = PROJECT_FILE,
) -> Any:
    try:
        from scripts import verify_release_artifacts as release
    except ModuleNotFoundError:  # Direct script execution sets scripts/ as sys.path[0].
        import verify_release_artifacts as release  # type: ignore[no-redef]

    bindings = _discover_artifacts(dist)
    try:
        with tempfile.TemporaryDirectory(
            prefix=".pmh-checksum-verify-",
        ) as temporary:
            staging = Path(temporary)
            for binding in bindings:
                _copy_bound_artifact(binding, staging / binding.path.name)
            inspected = release.inspect_release_artifacts(
                staging,
                project_file=project_file,
            )
            source_by_name = {binding.path.name: binding.path for binding in bindings}
            if (
                inspected.wheel.name not in source_by_name
                or inspected.sdist.name not in source_by_name
            ):
                raise ChecksumError("release_artifact_inventory")
            selected = SimpleNamespace(
                wheel=source_by_name[inspected.wheel.name],
                sdist=source_by_name[inspected.sdist.name],
                project_version=str(inspected.project_version),
            )
        _assert_artifacts_unchanged(bindings)
        return selected
    except ChecksumError:
        raise
    except (OSError, release.ReleaseArtifactError) as error:
        raise ChecksumError("release_artifacts_not_verified") from error


def _project_version(project_file: Path) -> str:
    try:
        with Path(project_file).open("rb") as file:
            document = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ChecksumError("project_metadata_unavailable") from error
    project = document.get("project")
    version = project.get("version") if type(project) is dict else None
    if not isinstance(version, str) or not version:
        raise ChecksumError("project_version_missing")
    return version


def _validated_artifacts(
    release_dir: Path,
    bundle: Any,
    bindings: tuple[_ArtifactBinding, ...],
) -> tuple[Path, Path]:
    wheel = Path(bundle.wheel)
    sdist = Path(bundle.sdist)
    artifacts = (wheel, sdist)
    bound_paths = {binding.path for binding in bindings}
    if (
        wheel.parent != release_dir
        or sdist.parent != release_dir
        or wheel == sdist
        or wheel.suffix != ".whl"
        or not sdist.name.endswith(".tar.gz")
        or set(artifacts) != bound_paths
    ):
        raise ChecksumError("release_artifact_inventory")
    _assert_artifacts_unchanged(bindings)
    return artifacts


def _read_manifest(manifest: Path) -> bytes:
    metadata = _manifest_metadata(manifest, allow_missing=False)
    assert metadata is not None
    file_descriptor: int | None = None
    try:
        file_descriptor = os.open(manifest, _readonly_flags())
        opened_metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(opened_metadata.st_mode)
            or opened_metadata.st_nlink != 1
            or _metadata_identity(metadata) != _metadata_identity(opened_metadata)
        ):
            raise ChecksumError("checksum_manifest_inventory")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(file_descriptor, 64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_MANIFEST_BYTES:
                raise ChecksumError("checksum_manifest_inventory")
            chunks.append(chunk)
        final_opened_metadata = os.fstat(file_descriptor)
        final_path_metadata = _manifest_metadata(manifest, allow_missing=False)
        assert final_path_metadata is not None
        if (
            _metadata_identity(opened_metadata) != _metadata_identity(final_opened_metadata)
            or _metadata_identity(opened_metadata) != _metadata_identity(final_path_metadata)
            or final_opened_metadata.st_nlink != 1
            or not _owner_controlled(final_opened_metadata)
        ):
            raise ChecksumError("checksum_manifest_inventory")
        return b"".join(chunks)
    except ChecksumError:
        raise
    except OSError as error:
        raise ChecksumError("checksum_manifest_inventory") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)


def _verify_existing_manifest(
    manifest: Path,
    bindings: tuple[_ArtifactBinding, ...],
    expected_content: bytes,
) -> None:
    try:
        content = _read_manifest(manifest)
        text = content.decode("ascii")
    except UnicodeDecodeError as error:
        raise ChecksumError("checksum_manifest_malformed") from error
    matches = [_MANIFEST_LINE.fullmatch(line) for line in text.splitlines()]
    if not matches or any(match is None for match in matches):
        raise ChecksumError("checksum_manifest_malformed")
    entries = [(match["filename"], match["digest"]) for match in matches if match is not None]
    filenames = [filename for filename, _digest in entries]
    if len(filenames) != len(set(filenames)):
        raise ChecksumError("checksum_manifest_duplicate")
    expected_names = {binding.path.name for binding in bindings}
    if set(filenames) != expected_names:
        raise ChecksumError("checksum_manifest_inventory")
    digest_by_name = {binding.path.name: binding.sha256 for binding in bindings}
    if any(digest_by_name[name] != digest for name, digest in entries):
        raise ChecksumError("checksum_mismatch")
    if content != expected_content:
        raise ChecksumError("checksum_manifest_not_canonical")


def _manifest_binding(
    manifest: Path,
    *,
    expected: _ArtifactBinding | None = None,
) -> _ArtifactBinding:
    initial_metadata = _manifest_metadata(manifest, allow_missing=False)
    assert initial_metadata is not None
    content = _read_manifest(manifest)
    final_metadata = _manifest_metadata(manifest, allow_missing=False)
    assert final_metadata is not None
    if _metadata_identity(initial_metadata) != _metadata_identity(final_metadata):
        raise ChecksumError("checksum_manifest_inventory")
    binding = _ArtifactBinding(
        path=manifest,
        device=final_metadata.st_dev,
        inode=final_metadata.st_ino,
        size=final_metadata.st_size,
        mtime_ns=final_metadata.st_mtime_ns,
        sha256=hashlib.sha256(content).hexdigest(),
    )
    if expected is not None and binding != expected:
        raise ChecksumError("checksum_manifest_inventory")
    return binding


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_same_inode(path: Path, identity: tuple[int, int]) -> bool:
    try:
        metadata = path.lstat()
        if (metadata.st_dev, metadata.st_ino) != identity:
            return False
        path.unlink()
        _fsync_directory(path.parent)
    except OSError:
        return False
    return True


def _atomic_write_manifest(manifest: Path, content: bytes) -> _ArtifactBinding:
    temporary = manifest.parent / (f".{MANIFEST_NAME}.{os.getpid()}.{secrets.token_hex(12)}.tmp")
    file_descriptor: int | None = None
    temporary_created = False
    link_created = False
    published_identity: tuple[int, int] | None = None
    completed = False
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        file_descriptor = os.open(temporary, flags, 0o600)
        temporary_created = True
        _write_all(file_descriptor, content)
        os.fsync(file_descriptor)
        temporary_metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(temporary_metadata.st_mode)
            or not _owner_controlled(temporary_metadata)
            or temporary_metadata.st_size != len(content)
        ):
            raise ChecksumError("checksum_manifest_write_failed")
        published_identity = (temporary_metadata.st_dev, temporary_metadata.st_ino)
        os.close(file_descriptor)
        file_descriptor = None
        if _manifest_metadata(manifest, allow_missing=True) is not None:
            raise ChecksumError("checksum_manifest_inventory")
        try:
            os.link(temporary, manifest, follow_symlinks=False)
        except FileExistsError:
            raise ChecksumError("checksum_manifest_inventory") from None
        link_created = True
        published_metadata = manifest.lstat()
        if (
            not stat.S_ISREG(published_metadata.st_mode)
            or published_metadata.st_nlink != 2
            or not _owner_controlled(published_metadata)
            or (published_metadata.st_dev, published_metadata.st_ino) != published_identity
            or published_metadata.st_size != len(content)
        ):
            raise ChecksumError("checksum_manifest_inventory")
        temporary.unlink()
        temporary_created = False
        binding = _manifest_binding(manifest)
        if binding.sha256 != hashlib.sha256(content).hexdigest():
            raise ChecksumError("checksum_mismatch")
        _fsync_directory(manifest.parent)
        completed = True
        return binding
    except ChecksumError:
        raise
    except OSError as error:
        raise ChecksumError("checksum_manifest_write_failed") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if temporary_created:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        if not completed and link_created and published_identity is not None:
            _unlink_if_same_inode(manifest, published_identity)


def _verify_final_inventory(
    release_dir: Path,
    artifacts: tuple[_ArtifactBinding, ...],
    manifest: _ArtifactBinding,
) -> None:
    if _release_directory(release_dir) != release_dir:
        raise ChecksumError("release_artifact_inventory")
    housekeeping = _safe_housekeeping(release_dir)
    expected = {binding.path for binding in artifacts}
    expected.add(manifest.path)
    if housekeeping is not None:
        expected.add(housekeeping)
    try:
        if set(release_dir.iterdir()) != expected:
            raise ChecksumError("release_artifact_inventory")
        _assert_artifacts_unchanged(artifacts)
        _manifest_binding(manifest.path, expected=manifest)
        if set(release_dir.iterdir()) != expected:
            raise ChecksumError("release_artifact_inventory")
    except ChecksumError:
        raise
    except OSError as error:
        raise ChecksumError("release_artifact_inventory") from error


def create_checksums(
    dist: Path,
    *,
    tag: str | None = None,
    project_file: Path = PROJECT_FILE,
) -> Path:
    selected_project_file = Path(project_file)
    project_version = _project_version(selected_project_file)
    if tag is not None and tag != f"v{project_version}":
        raise ChecksumError("tag_version_mismatch")
    release_dir = _release_directory(dist)
    manifest = release_dir / MANIFEST_NAME
    existing_manifest = _manifest_metadata(manifest, allow_missing=True) is not None
    initial_manifest_binding = _manifest_binding(manifest) if existing_manifest else None
    bindings = _discover_artifacts(release_dir)
    bundle = _inspect_release_artifacts(
        release_dir,
        project_file=selected_project_file,
    )
    if str(bundle.project_version) != project_version:
        raise ChecksumError("release_artifact_version_mismatch")
    artifacts = sorted(
        _validated_artifacts(release_dir, bundle, bindings),
        key=lambda artifact: artifact.name,
    )
    binding_by_path = {binding.path: binding for binding in bindings}
    ordered_bindings = tuple(binding_by_path[artifact] for artifact in artifacts)
    content = "".join(
        f"{binding.sha256}  {binding.path.name}\n" for binding in ordered_bindings
    ).encode("ascii")
    _assert_artifacts_unchanged(bindings)
    if existing_manifest:
        assert initial_manifest_binding is not None
        _verify_existing_manifest(manifest, ordered_bindings, content)
        _verify_final_inventory(release_dir, bindings, initial_manifest_binding)
        return manifest
    if _manifest_metadata(manifest, allow_missing=True) is not None:
        raise ChecksumError("checksum_manifest_inventory")
    published_manifest = _atomic_write_manifest(manifest, content)
    try:
        _verify_existing_manifest(manifest, ordered_bindings, content)
        _verify_final_inventory(release_dir, bindings, published_manifest)
    except Exception:
        _unlink_if_same_inode(
            manifest,
            (published_manifest.device, published_manifest.inode),
        )
        raise
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create deterministic checksums for verified release artifacts"
    )
    parser.add_argument("dist", type=Path)
    parser.add_argument("--tag")
    parser.add_argument("--project-file", type=Path, default=PROJECT_FILE)
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    tag = arguments.tag
    if tag is None:
        tag = os.environ.get("GITHUB_REF_NAME")
    try:
        create_checksums(
            arguments.dist,
            tag=tag,
            project_file=arguments.project_file,
        )
    except ChecksumError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
