from __future__ import annotations

import errno
import json
import os
import stat
import sys
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import NoReturn
from uuid import uuid4

from project_memory_hub.paths import RuntimePaths


RUNTIME_MARKER_NAME = ".project-memory-hub-demo-runtime-v1"
OUTPUT_MARKER_NAME = ".project-memory-hub-demo-output-v1"
_MARKER_DOCUMENT = b"project-memory-hub synthetic demo generator v1\n"
DEMO_MARKER_DOCUMENT = _MARKER_DOCUMENT
_RUNTIME_DIRECTORY_NAMES = frozenset({"backups", "imports", "logs", "retries"})
_RUNTIME_FILE_NAMES = frozenset(
    {
        RUNTIME_MARKER_NAME,
        "access-token",
        "config.toml",
        "memory.db",
        "memory.db-shm",
        "memory.db-wal",
        "proposal-apply.lock",
        "reconcile.lock",
    }
)
_RUNTIME_TOP_LEVEL_ALLOWLIST = _RUNTIME_DIRECTORY_NAMES | _RUNTIME_FILE_NAMES
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_READ_FLAGS = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_MAX_PUBLISH_FILE_BYTES = 64 * 1024 * 1024
_MAX_PUBLISH_TOTAL_BYTES = 256 * 1024 * 1024
_PUBLIC_MANIFEST_NAME = "demo-manifest.json"
_PUBLIC_MANIFEST_GENERATOR = "project-memory-hub-demo-assets"


class DemoPathError(ValueError):
    """A demo target failed the fail-closed path policy."""


@dataclass(frozen=True, slots=True)
class DemoWorkspace:
    runtime_dir: Path
    output_dir: Path
    allowed_output_names: frozenset[str]
    _runtime_created: bool
    _runtime_identity: tuple[int, int]
    _runtime_descriptor: int = field(repr=False, compare=False)
    _output_created: bool
    _output_identity: tuple[int, int]
    _output_marker_created: bool
    _runtime_cleaned: bool = field(default=False, init=False, repr=False, compare=False)
    _output_cleaned: bool = field(default=False, init=False, repr=False, compare=False)
    _output_finalized: bool = field(default=False, init=False, repr=False, compare=False)

    @property
    def paths(self) -> RuntimePaths:
        """Return paths derived from the immutable, identity-bound runtime root."""
        return RuntimePaths.for_root(self._bound_runtime_root())

    def _bound_runtime_root(self) -> Path:
        descriptor = self._runtime_descriptor
        if descriptor < 0:
            _reject("runtime")
        try:
            metadata = os.fstat(descriptor)
            _verify_owned_directory_metadata(metadata, kind="runtime")
            if _descriptor_identity(descriptor) != self._runtime_identity:
                _reject("runtime")
            path = _path_for_open_directory(descriptor)
            live = _open_directory(path, kind="runtime")
            try:
                if _descriptor_identity(live) != self._runtime_identity:
                    _reject("runtime")
            finally:
                os.close(live)
            return path
        except DemoPathError:
            raise
        except OSError as error:
            raise DemoPathError("demo runtime rejected") from error

    def validate_runtime(self) -> None:
        """Revalidate the live runtime identity, permissions, marker, and full tree."""
        descriptor = _open_owned_directory(
            self.runtime_dir,
            self._runtime_identity,
            kind="runtime",
        )
        try:
            _verify_runtime_tree(descriptor)
        finally:
            os.close(descriptor)

    def validate_output(self) -> None:
        """Revalidate the live output identity and its complete anchored tree."""
        allowed = _validated_output_names(self.allowed_output_names)
        descriptor = _open_owned_directory(
            self.output_dir,
            self._output_identity,
            kind="output",
        )
        try:
            _verify_approved_output(descriptor, allowed)
        finally:
            os.close(descriptor)

    def write_output_file(self, name: str, document: bytes) -> None:
        """Atomically replace one allowlisted output without following path links."""
        allowed = _validated_output_names(self.allowed_output_names)
        _validated_output_name(name)
        if name not in allowed:
            _reject("output")
        descriptor = _open_owned_directory(
            self.output_dir,
            self._output_identity,
            kind="output",
        )
        try:
            _verify_approved_output(descriptor, allowed)
            _validate_publish_document(document)
            _atomic_replace(descriptor, name, document)
            _verify_approved_output(descriptor, allowed)
            os.fsync(descriptor)
        except DemoPathError:
            raise
        except OSError as error:
            raise DemoPathError("demo output rejected") from error
        finally:
            os.close(descriptor)

    def publish_output_files(self, documents: Mapping[str, bytes]) -> None:
        """Publish a full asset set, restoring the old nested tree on failure."""
        allowed = _validated_output_names(self.allowed_output_names)
        if set(documents) != set(allowed):
            _reject("output")
        total_bytes = 0
        for name, document in documents.items():
            _validated_output_name(name)
            _validate_publish_document(document)
            total_bytes += len(document)
            if total_bytes > _MAX_PUBLISH_TOTAL_BYTES:
                _reject("output")

        descriptor = _open_owned_directory(
            self.output_dir,
            self._output_identity,
            kind="output",
        )
        originals: dict[str, bytes] = {}
        try:
            existing_files, _existing_directories, marker_present = _verify_approved_output(
                descriptor,
                allowed,
            )
            originals = {
                name: _read_regular_entry(descriptor, name, kind="output")
                for name in existing_files
            }
            try:
                for name in _publish_order(allowed):
                    _atomic_replace(descriptor, name, documents[name])
                files, _directories, current_marker = _verify_approved_output(
                    descriptor,
                    allowed,
                )
                if files != allowed or current_marker != marker_present:
                    _reject("output")
                os.fsync(descriptor)
            except BaseException as error:
                try:
                    for name in _publish_order(frozenset(originals)):
                        _atomic_replace(descriptor, name, originals[name])
                    for name in sorted(allowed - frozenset(originals), reverse=True):
                        _unlink_relative_file(descriptor, name, missing_ok=True)
                    _prune_allowed_directories(descriptor, allowed)
                    files, _directories, restored_marker = _verify_approved_output(
                        descriptor,
                        allowed,
                    )
                    if files != frozenset(originals) or restored_marker != marker_present:
                        _reject("output")
                    os.fsync(descriptor)
                except (DemoPathError, OSError) as rollback_error:
                    raise DemoPathError("demo output rollback failed") from rollback_error
                if isinstance(error, DemoPathError):
                    raise error
                if isinstance(error, OSError):
                    raise DemoPathError("demo output rejected") from error
                raise
        finally:
            os.close(descriptor)

    def finalize_output(self) -> None:
        """Validate a complete public set and remove the private in-progress marker."""
        allowed = _validated_output_names(self.allowed_output_names)
        descriptor = _open_owned_directory(
            self.output_dir,
            self._output_identity,
            kind="output",
        )
        try:
            files, _directories, marker_present = _verify_approved_output(
                descriptor,
                allowed,
            )
            if files != allowed:
                _reject("output")
            _verify_published_manifest(descriptor, allowed)
            if marker_present:
                os.unlink(OUTPUT_MARKER_NAME, dir_fd=descriptor)
                os.fsync(descriptor)
            _verify_approved_output(descriptor, allowed, require_public=True)
        except DemoPathError:
            raise
        except OSError as error:
            raise DemoPathError("demo output rejected") from error
        finally:
            os.close(descriptor)
        object.__setattr__(self, "_output_finalized", True)

    def cleanup_incomplete_output(self) -> None:
        """Undo only output state initialized by this workspace."""
        if (
            self._output_cleaned
            or self._output_finalized
            or not (self._output_created or self._output_marker_created)
        ):
            return
        allowed = _validated_output_names(self.allowed_output_names)
        descriptor = _open_owned_directory(
            self.output_dir,
            self._output_identity,
            kind="output",
        )
        try:
            files, _directories, marker_present = _verify_approved_output(
                descriptor,
                allowed,
            )
            if not marker_present:
                _reject("output")
            for name in sorted(files, reverse=True):
                _unlink_relative_file(descriptor, name)
            _prune_allowed_directories(descriptor, allowed)
            os.unlink(OUTPUT_MARKER_NAME, dir_fd=descriptor)
            os.fsync(descriptor)
        except DemoPathError:
            raise
        except OSError as error:
            raise DemoPathError("demo output rejected") from error
        finally:
            os.close(descriptor)

        if self._output_created:
            try:
                metadata = self.output_dir.lstat()
                if (metadata.st_dev, metadata.st_ino) != self._output_identity:
                    _reject("output")
                self.output_dir.rmdir()
            except DemoPathError:
                raise
            except OSError as error:
                raise DemoPathError("demo output rejected") from error
        object.__setattr__(self, "_output_cleaned", True)

    def cleanup_runtime(self) -> None:
        """Remove only the recursively verified runtime created for this demo run."""
        if self._runtime_cleaned:
            return
        bound_root = self._bound_runtime_root()
        descriptor = os.dup(self._runtime_descriptor)
        try:
            snapshot = _verify_runtime_tree(descriptor)
            names = _directory_names(descriptor, kind="runtime")
            expected_top_level = {relative for relative in snapshot if "/" not in relative}
            if names != expected_top_level:
                _reject("runtime")
            for name in sorted(names - {RUNTIME_MARKER_NAME}):
                _remove_entry(descriptor, name, snapshot=snapshot, prefix=name)
            marker_kind, marker_identity = snapshot[RUNTIME_MARKER_NAME]
            if (
                marker_kind != "file"
                or _verify_regular_entry(
                    descriptor,
                    RUNTIME_MARKER_NAME,
                    kind="runtime",
                )
                != marker_identity
            ):
                _reject("runtime")
            os.unlink(RUNTIME_MARKER_NAME, dir_fd=descriptor)
            os.fsync(descriptor)
        except DemoPathError:
            raise
        except OSError as error:
            raise DemoPathError("demo runtime rejected") from error
        finally:
            os.close(descriptor)

        if self._runtime_created:
            try:
                metadata = bound_root.lstat()
                if (metadata.st_dev, metadata.st_ino) != self._runtime_identity:
                    _reject("runtime")
                bound_root.rmdir()
            except DemoPathError:
                raise
            except OSError as error:
                raise DemoPathError("demo runtime rejected") from error
        os.close(self._runtime_descriptor)
        object.__setattr__(self, "_runtime_descriptor", -1)
        object.__setattr__(self, "_runtime_cleaned", True)

    def __enter__(self) -> DemoWorkspace:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.cleanup_runtime()


def prepare_demo_workspace(
    *,
    runtime_dir: Path,
    output_dir: Path,
    repository_root: Path,
    allowed_output_names: Iterable[str],
    default_runtime_root: Path | None = None,
    temporary_root: Path | None = None,
) -> DemoWorkspace:
    """Validate and mark isolated runtime/output directories for synthetic generation."""
    temp_alias, temp_root = _trusted_temporary_root(temporary_root)
    runtime = _canonicalize_temporary_alias(
        _absolute_target(runtime_dir, kind="runtime"),
        temp_alias,
        temp_root,
    )
    output = _canonicalize_temporary_alias(
        _absolute_target(output_dir, kind="output"),
        temp_alias,
        temp_root,
    )
    repository = _absolute_target(repository_root, kind="runtime")
    default_root = _absolute_target(
        RuntimePaths.for_root().root if default_runtime_root is None else default_runtime_root,
        kind="runtime",
    )
    allowed = _validated_output_names(allowed_output_names)

    for candidate in (runtime, output, repository, default_root, temp_root):
        _reject_symlink_components(candidate, kind="output" if candidate == output else "runtime")

    if runtime == temp_root or temp_root not in runtime.parents:
        _reject("runtime")
    if _paths_overlap(runtime, default_root):
        _reject("runtime")
    if _paths_overlap(output, default_root):
        _reject("output")
    if _paths_overlap(runtime, repository):
        _reject("runtime")
    if _paths_overlap(runtime, output):
        _reject("runtime")

    _preflight_runtime(runtime)
    output_state = _preflight_output(output, allowed)

    runtime_descriptor = -1
    output_descriptor = -1
    runtime_created = False
    output_created = False
    output_marker_created = False
    try:
        runtime_descriptor, runtime_created = _open_or_create_leaf(runtime, kind="runtime")
        if not runtime_created:
            _reject("runtime")
        _verify_directory_empty(runtime_descriptor, kind="runtime")
        _write_marker(runtime_descriptor, RUNTIME_MARKER_NAME, kind="runtime")
        runtime_identity = _descriptor_identity(runtime_descriptor)

        output_descriptor, output_created = _open_or_create_leaf(output, kind="output")
        if output_state in {"marked", "published"}:
            _verify_approved_output(output_descriptor, allowed)
        else:
            _verify_directory_empty(output_descriptor, kind="output")
            _write_marker(output_descriptor, OUTPUT_MARKER_NAME, kind="output")
            output_marker_created = True
        output_identity = _descriptor_identity(output_descriptor)

        workspace = DemoWorkspace(
            runtime_dir=runtime,
            output_dir=output,
            allowed_output_names=allowed,
            _runtime_created=runtime_created,
            _runtime_identity=runtime_identity,
            _runtime_descriptor=runtime_descriptor,
            _output_created=output_created,
            _output_identity=output_identity,
            _output_marker_created=output_marker_created,
        )
        runtime_descriptor = -1
        return workspace
    except BaseException:
        if output_descriptor >= 0 and output_marker_created:
            _remove_new_empty_marked_directory(
                output,
                output_descriptor,
                OUTPUT_MARKER_NAME,
                remove_root=output_created,
                kind="output",
            )
            output_descriptor = -1
        if runtime_descriptor >= 0:
            _remove_new_empty_marked_directory(
                runtime,
                runtime_descriptor,
                RUNTIME_MARKER_NAME,
                remove_root=runtime_created,
                kind="runtime",
            )
            runtime_descriptor = -1
        raise
    finally:
        if output_descriptor >= 0:
            os.close(output_descriptor)
        if runtime_descriptor >= 0:
            os.close(runtime_descriptor)


def _absolute_target(path: Path, *, kind: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        _reject(kind)
    return Path(os.path.abspath(candidate))


def _trusted_temporary_root(temporary_root: Path | None) -> tuple[Path, Path]:
    if temporary_root is None:
        alias = _absolute_target(Path(tempfile.gettempdir()), kind="runtime")
        try:
            canonical = alias.resolve(strict=True)
        except OSError as error:
            raise DemoPathError("demo runtime rejected") from error
    else:
        alias = _absolute_target(temporary_root, kind="runtime")
        _reject_symlink_components(alias, kind="runtime")
        canonical = alias
    descriptor = _open_directory(canonical, kind="runtime", allow_shared_temporary=True)
    os.close(descriptor)
    return alias, canonical


def _is_trusted_temporary_root_metadata(
    metadata: os.stat_result,
    *,
    current_uid: int,
) -> bool:
    if not stat.S_ISDIR(metadata.st_mode):
        return False
    if metadata.st_uid == current_uid:
        return not bool(metadata.st_mode & 0o022)
    return bool(
        metadata.st_uid == 0 and metadata.st_mode & stat.S_ISVTX and metadata.st_mode & stat.S_IWOTH
    )


def _canonicalize_temporary_alias(path: Path, alias: Path, canonical: Path) -> Path:
    if path == alias:
        return canonical
    if alias in path.parents:
        return canonical / path.relative_to(alias)
    return path


def _validated_output_name(name: str) -> tuple[str, ...]:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or any(ord(character) < 32 or ord(character) == 127 for character in name)
    ):
        _reject("output")
    components = tuple(name.split("/"))
    if (
        name.startswith("/")
        or any(component in {"", ".", "..", OUTPUT_MARKER_NAME} for component in components)
        or PurePosixPath(name).is_absolute()
        or PurePosixPath(name).as_posix() != name
    ):
        _reject("output")
    return components


def _validated_output_names(names: Iterable[str]) -> frozenset[str]:
    try:
        validated = frozenset(names)
    except TypeError as error:
        raise DemoPathError("demo output rejected") from error
    if not validated:
        _reject("output")
    for name in validated:
        _validated_output_name(name)
    return validated


def _allowed_output_directories(allowed_names: frozenset[str]) -> frozenset[str]:
    directories: set[str] = set()
    for name in allowed_names:
        components = _validated_output_name(name)
        for length in range(1, len(components)):
            directories.add("/".join(components[:length]))
    return frozenset(directories)


def _publish_order(names: frozenset[str]) -> tuple[str, ...]:
    manifests = sorted(
        name
        for name in names
        if PurePosixPath(name).name in {_PUBLIC_MANIFEST_NAME, "manifest.json"}
    )
    ordinary = sorted(set(names) - set(manifests))
    return tuple([*ordinary, *manifests])


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _reject_symlink_components(path: Path, *, kind: str) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise DemoPathError(f"demo {kind} rejected") from error
        if stat.S_ISLNK(metadata.st_mode):
            _reject(kind)


def _preflight_runtime(path: Path) -> None:
    try:
        path.lstat()
    except FileNotFoundError:
        _validate_parent(path, kind="runtime")
        return
    except OSError as error:
        raise DemoPathError("demo runtime rejected") from error
    _reject("runtime")


def _preflight_output(path: Path, allowed_names: frozenset[str]) -> str:
    try:
        path.lstat()
    except FileNotFoundError:
        _validate_parent(path, kind="output")
        return "absent"
    except OSError as error:
        raise DemoPathError("demo output rejected") from error
    descriptor = _open_directory(path, kind="output")
    try:
        names = _directory_names(descriptor, kind="output")
        if not names:
            return "empty"
        _files, _directories, marker_present = _verify_approved_output(
            descriptor,
            allowed_names,
        )
        return "marked" if marker_present else "published"
    finally:
        os.close(descriptor)


def _validate_parent(path: Path, *, kind: str) -> None:
    parent = path.parent
    _reject_symlink_components(parent, kind=kind)
    descriptor = _open_directory(parent, kind=kind)
    os.close(descriptor)


def _open_or_create_leaf(path: Path, *, kind: str) -> tuple[int, bool]:
    parent_descriptor = _open_directory(path.parent, kind=kind)
    created = False
    descriptor = -1
    try:
        try:
            os.mkdir(path.name, mode=0o700, dir_fd=parent_descriptor)
            created = True
        except FileExistsError:
            pass
        descriptor = os.open(path.name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        _verify_owned_directory_metadata(os.fstat(descriptor), kind=kind)
        return descriptor, created
    except DemoPathError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise DemoPathError(f"demo {kind} rejected") from error
    finally:
        os.close(parent_descriptor)


def _open_directory(
    path: Path,
    *,
    kind: str,
    allow_shared_temporary: bool = False,
) -> int:
    selected = Path(path)
    if not selected.is_absolute():
        _reject(kind)
    descriptor = -1
    try:
        descriptor = os.open(selected.anchor, _DIRECTORY_FLAGS)
        for component in selected.parts[1:]:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise DemoPathError(f"demo {kind} rejected") from error
    if allow_shared_temporary:
        trusted = _is_trusted_temporary_root_metadata(metadata, current_uid=os.getuid())
        if not trusted:
            os.close(descriptor)
            _reject(kind)
    else:
        try:
            _verify_owned_directory_metadata(metadata, kind=kind)
        except DemoPathError:
            os.close(descriptor)
            raise
    return descriptor


def _open_owned_directory(path: Path, identity: tuple[int, int], *, kind: str) -> int:
    _reject_symlink_components(path, kind=kind)
    descriptor = _open_directory(path, kind=kind)
    if _descriptor_identity(descriptor) != identity:
        os.close(descriptor)
        _reject(kind)
    return descriptor


def _verify_owned_directory_metadata(metadata: os.stat_result, *, kind: str) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        _reject(kind)


def _verify_owned_regular_metadata(metadata: os.stat_result, *, kind: str) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o022
    ):
        _reject(kind)


def _descriptor_identity(descriptor: int) -> tuple[int, int]:
    metadata = os.fstat(descriptor)
    return metadata.st_dev, metadata.st_ino


def _runtime_entry_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _path_for_open_directory(descriptor: int) -> Path:
    if sys.platform == "darwin":
        import fcntl

        raw = fcntl.fcntl(
            descriptor,
            getattr(fcntl, "F_GETPATH", 50),
            b"\0" * 1024,
        )
        document = bytes(raw).split(b"\0", 1)[0]
        if not document:
            _reject("runtime")
        path = Path(os.fsdecode(document))
    elif sys.platform.startswith("linux"):
        path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
    else:
        _reject("runtime")
    if not path.is_absolute() or str(path).endswith(" (deleted)"):
        _reject("runtime")
    return Path(os.path.abspath(path))


def _directory_names(descriptor: int, *, kind: str) -> frozenset[str]:
    try:
        with os.scandir(descriptor) as entries:
            names = frozenset(entry.name for entry in entries)
    except OSError as error:
        raise DemoPathError(f"demo {kind} rejected") from error
    if any(name in {".", ".."} for name in names):
        _reject(kind)
    return names


def _verify_directory_empty(descriptor: int, *, kind: str) -> None:
    if _directory_names(descriptor, kind=kind):
        _reject(kind)


def _write_marker(descriptor: int, name: str, *, kind: str) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    marker_descriptor = -1
    try:
        marker_descriptor = os.open(name, flags, 0o600, dir_fd=descriptor)
        _write_all(marker_descriptor, _MARKER_DOCUMENT)
        os.fsync(marker_descriptor)
    except OSError as error:
        raise DemoPathError(f"demo {kind} rejected") from error
    finally:
        if marker_descriptor >= 0:
            os.close(marker_descriptor)


def _verify_marker(descriptor: int, name: str, *, kind: str) -> tuple[int, ...]:
    marker_descriptor = -1
    try:
        marker_descriptor = os.open(name, _READ_FLAGS, dir_fd=descriptor)
        metadata = os.fstat(marker_descriptor)
        document = os.read(marker_descriptor, len(_MARKER_DOCUMENT) + 1)
    except OSError as error:
        raise DemoPathError(f"demo {kind} rejected") from error
    finally:
        if marker_descriptor >= 0:
            os.close(marker_descriptor)
    _verify_owned_regular_metadata(metadata, kind=kind)
    if document != _MARKER_DOCUMENT:
        _reject(kind)
    return _runtime_entry_identity(metadata)


def _verify_runtime_tree(descriptor: int) -> dict[str, tuple[str, tuple[int, ...]]]:
    _verify_owned_directory_metadata(os.fstat(descriptor), kind="runtime")
    names = _directory_names(descriptor, kind="runtime")
    if RUNTIME_MARKER_NAME not in names or not names <= _RUNTIME_TOP_LEVEL_ALLOWLIST:
        _reject("runtime")
    snapshot: dict[str, tuple[str, tuple[int, ...]]] = {
        RUNTIME_MARKER_NAME: (
            "file",
            _verify_marker(descriptor, RUNTIME_MARKER_NAME, kind="runtime"),
        )
    }
    for name in sorted(names - {RUNTIME_MARKER_NAME}):
        if name in _RUNTIME_DIRECTORY_NAMES:
            child = _open_child_directory(descriptor, name, kind="runtime")
            try:
                snapshot[name] = (
                    "directory",
                    _runtime_entry_identity(os.fstat(child)),
                )
                _verify_runtime_directory(child, snapshot=snapshot, prefix=name)
            finally:
                os.close(child)
        else:
            snapshot[name] = (
                "file",
                _verify_regular_entry(descriptor, name, kind="runtime"),
            )
    return snapshot


def _verify_runtime_directory(
    descriptor: int,
    *,
    snapshot: dict[str, tuple[str, tuple[int, ...]]],
    prefix: str,
) -> None:
    _verify_owned_directory_metadata(os.fstat(descriptor), kind="runtime")
    for name in sorted(_directory_names(descriptor, kind="runtime")):
        try:
            metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as error:
            raise DemoPathError("demo runtime rejected") from error
        if stat.S_ISDIR(metadata.st_mode):
            child = _open_child_directory(descriptor, name, kind="runtime")
            try:
                relative = f"{prefix}/{name}"
                snapshot[relative] = (
                    "directory",
                    _runtime_entry_identity(os.fstat(child)),
                )
                _verify_runtime_directory(
                    child,
                    snapshot=snapshot,
                    prefix=relative,
                )
            finally:
                os.close(child)
        elif stat.S_ISREG(metadata.st_mode):
            snapshot[f"{prefix}/{name}"] = (
                "file",
                _verify_regular_entry(descriptor, name, kind="runtime"),
            )
        else:
            _reject("runtime")


def _open_child_directory(parent_descriptor: int, name: str, *, kind: str) -> int:
    descriptor = -1
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        _verify_owned_directory_metadata(os.fstat(descriptor), kind=kind)
        return descriptor
    except DemoPathError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as error:
        if descriptor >= 0:
            os.close(descriptor)
        raise DemoPathError(f"demo {kind} rejected") from error


def _verify_regular_entry(
    parent_descriptor: int,
    name: str,
    *,
    kind: str,
) -> tuple[int, ...]:
    descriptor = -1
    try:
        descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
        metadata = os.fstat(descriptor)
        _verify_owned_regular_metadata(metadata, kind=kind)
        return _runtime_entry_identity(metadata)
    except DemoPathError:
        raise
    except OSError as error:
        raise DemoPathError(f"demo {kind} rejected") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_all(descriptor: int, document: bytes) -> None:
    written = 0
    while written < len(document):
        count = os.write(descriptor, document[written:])
        if count <= 0:
            raise OSError("short demo write")
        written += count


def _validate_publish_document(document: bytes) -> None:
    if not isinstance(document, bytes) or len(document) > _MAX_PUBLISH_FILE_BYTES:
        _reject("output")


def _open_relative_parent(
    root_descriptor: int,
    name: str,
    *,
    create: bool,
) -> tuple[int, str]:
    components = _validated_output_name(name)
    descriptor = os.dup(root_descriptor)
    try:
        for component in components[:-1]:
            if create:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=descriptor)
                except FileExistsError:
                    pass
            child = _open_child_directory(descriptor, component, kind="output")
            os.close(descriptor)
            descriptor = child
        return descriptor, components[-1]
    except DemoPathError:
        os.close(descriptor)
        raise
    except OSError as error:
        os.close(descriptor)
        raise DemoPathError("demo output rejected") from error


def _atomic_replace(descriptor: int, name: str, document: bytes) -> None:
    _validate_publish_document(document)
    parent_descriptor, leaf_name = _open_relative_parent(descriptor, name, create=True)
    temporary_name = f".pmh-demo-{uuid4().hex}.candidate"
    candidate = -1
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        candidate = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
        _write_all(candidate, document)
        os.fchmod(candidate, 0o600)
        os.fsync(candidate)
        os.close(candidate)
        candidate = -1
        os.replace(
            temporary_name,
            leaf_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
    finally:
        if candidate >= 0:
            os.close(candidate)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def _read_regular_entry(descriptor: int, name: str, *, kind: str) -> bytes:
    parent_descriptor, leaf_name = _open_relative_parent(descriptor, name, create=False)
    file_descriptor = -1
    try:
        file_descriptor = os.open(leaf_name, _READ_FLAGS, dir_fd=parent_descriptor)
        before = os.fstat(file_descriptor)
        _verify_owned_regular_metadata(before, kind=kind)
        if before.st_size > _MAX_PUBLISH_FILE_BYTES:
            _reject(kind)
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(file_descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                _reject(kind)
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(file_descriptor, 1):
            _reject(kind)
        after = os.fstat(file_descriptor)
        if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ):
            _reject(kind)
        return b"".join(chunks)
    except DemoPathError:
        raise
    except OSError as error:
        raise DemoPathError(f"demo {kind} rejected") from error
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(parent_descriptor)


def _verify_approved_output(
    descriptor: int,
    allowed_names: frozenset[str],
    *,
    require_public: bool = False,
) -> tuple[frozenset[str], frozenset[str], bool]:
    _verify_owned_directory_metadata(os.fstat(descriptor), kind="output")
    allowed = _validated_output_names(allowed_names)
    allowed_directories = _allowed_output_directories(allowed)
    files: set[str] = set()
    directories: set[str] = set()
    marker_present = False

    def walk(current: int, prefix: tuple[str, ...]) -> None:
        nonlocal marker_present
        for entry_name in sorted(_directory_names(current, kind="output")):
            relative = "/".join((*prefix, entry_name))
            if not prefix and entry_name == OUTPUT_MARKER_NAME:
                _verify_marker(current, entry_name, kind="output")
                marker_present = True
                continue
            try:
                metadata = os.stat(entry_name, dir_fd=current, follow_symlinks=False)
            except OSError as error:
                raise DemoPathError("demo output rejected") from error
            if stat.S_ISDIR(metadata.st_mode):
                if relative not in allowed_directories:
                    _reject("output")
                child = _open_child_directory(current, entry_name, kind="output")
                directories.add(relative)
                try:
                    walk(child, (*prefix, entry_name))
                finally:
                    os.close(child)
            elif stat.S_ISREG(metadata.st_mode):
                if relative not in allowed:
                    _reject("output")
                _verify_regular_entry(current, entry_name, kind="output")
                files.add(relative)
            else:
                _reject("output")

    walk(descriptor, ())
    if marker_present:
        if require_public:
            _reject("output")
    else:
        if frozenset(files) != allowed or frozenset(directories) != allowed_directories:
            _reject("output")
        _verify_published_manifest(descriptor, allowed)
    return frozenset(files), frozenset(directories), marker_present


def _verify_published_manifest(
    descriptor: int,
    allowed_names: frozenset[str],
) -> None:
    if _PUBLIC_MANIFEST_NAME not in allowed_names:
        return
    document = _read_regular_entry(descriptor, _PUBLIC_MANIFEST_NAME, kind="output")

    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        parsed: dict[str, object] = {}
        for key, value in pairs:
            if key in parsed:
                _reject("output")
            parsed[key] = value
        return parsed

    def reject_constant(_value: str) -> NoReturn:
        _reject("output")

    try:
        parsed = json.loads(
            document.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
            parse_constant=reject_constant,
        )
        canonical = (
            json.dumps(
                parsed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise DemoPathError("demo output rejected") from error
    if (
        not isinstance(parsed, dict)
        or document != canonical
        or parsed.get("generator") != _PUBLIC_MANIFEST_GENERATOR
        or type(parsed.get("schema_version")) is not int
        or parsed.get("schema_version") != 1
    ):
        _reject("output")


def _unlink_relative_file(
    descriptor: int,
    name: str,
    *,
    missing_ok: bool = False,
) -> None:
    try:
        parent_descriptor, leaf_name = _open_relative_parent(descriptor, name, create=False)
    except DemoPathError:
        if missing_ok:
            return
        raise
    try:
        try:
            metadata = os.stat(leaf_name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        if not stat.S_ISREG(metadata.st_mode):
            _reject("output")
        os.unlink(leaf_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except DemoPathError:
        raise
    except OSError as error:
        raise DemoPathError("demo output rejected") from error
    finally:
        os.close(parent_descriptor)


def _prune_allowed_directories(
    descriptor: int,
    allowed_names: frozenset[str],
) -> None:
    directories = sorted(
        _allowed_output_directories(allowed_names),
        key=lambda name: (name.count("/"), name),
        reverse=True,
    )
    for directory in directories:
        components = _validated_output_name(directory)
        if len(components) == 1:
            parent_descriptor = os.dup(descriptor)
            leaf_name = components[0]
        else:
            try:
                parent_descriptor, leaf_name = _open_relative_parent(
                    descriptor,
                    directory,
                    create=False,
                )
            except DemoPathError:
                continue
        try:
            try:
                os.rmdir(leaf_name, dir_fd=parent_descriptor)
            except FileNotFoundError:
                pass
            except OSError as error:
                if error.errno not in {errno.ENOTEMPTY, errno.EEXIST}:
                    raise
        finally:
            os.close(parent_descriptor)


def _remove_entry(
    parent_descriptor: int,
    name: str,
    *,
    snapshot: Mapping[str, tuple[str, tuple[int, ...]]],
    prefix: str,
) -> None:
    try:
        expected_kind, expected_identity = snapshot[prefix]
    except KeyError:
        _reject("runtime")
    metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    if stat.S_ISDIR(metadata.st_mode):
        if expected_kind != "directory" or _runtime_entry_identity(metadata) != expected_identity:
            _reject("runtime")
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_descriptor)
        try:
            opened = os.fstat(descriptor)
            _verify_owned_directory_metadata(opened, kind="runtime")
            if _runtime_entry_identity(opened) != expected_identity:
                _reject("runtime")
            expected_children = {
                relative.rsplit("/", 1)[-1]
                for relative in snapshot
                if relative.startswith(f"{prefix}/") and "/" not in relative[len(prefix) + 1 :]
            }
            actual_children = _directory_names(descriptor, kind="runtime")
            if actual_children != expected_children:
                _reject("runtime")
            for child in sorted(actual_children):
                _remove_entry(
                    descriptor,
                    child,
                    snapshot=snapshot,
                    prefix=f"{prefix}/{child}",
                )
        finally:
            os.close(descriptor)
        os.rmdir(name, dir_fd=parent_descriptor)
        return
    if not stat.S_ISREG(metadata.st_mode) or expected_kind != "file":
        _reject("runtime")
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent_descriptor)
    try:
        opened = os.fstat(descriptor)
        _verify_owned_regular_metadata(opened, kind="runtime")
        if _runtime_entry_identity(opened) != expected_identity:
            _reject("runtime")
    finally:
        os.close(descriptor)
    os.unlink(name, dir_fd=parent_descriptor)


def _remove_new_empty_marked_directory(
    path: Path,
    descriptor: int,
    marker_name: str,
    *,
    remove_root: bool,
    kind: str,
) -> None:
    try:
        names = _directory_names(descriptor, kind=kind)
        if names == {marker_name}:
            os.unlink(marker_name, dir_fd=descriptor)
        os.close(descriptor)
        if remove_root:
            path.rmdir()
    except OSError:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _reject(kind: str) -> NoReturn:
    raise DemoPathError(f"demo {kind} rejected")
