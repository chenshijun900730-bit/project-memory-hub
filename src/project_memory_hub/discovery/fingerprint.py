import errno
import hashlib
import json
import os
import re
import stat
import tomllib
import xml.etree.ElementTree as element_tree
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_MAX_METADATA_BYTES = 256 * 1024
_MANIFEST_NAMES = frozenset(
    {
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
    }
)
_SCP_REMOTE = re.compile(r"^(?:[^@/\s]+@)(?P<host>[^:/\s]+):(?P<path>.+)$")


def normalize_git_remote(value: str) -> str:
    stripped = value.strip()
    scp_match = _SCP_REMOTE.fullmatch(stripped)
    if scp_match is not None:
        host = scp_match.group("host").lower()
        path = _clean_remote_path(scp_match.group("path"), leading_slash=False)
        return f"ssh://{host}/{path}"

    parsed = urlsplit(stripped)
    if parsed.scheme:
        scheme = parsed.scheme.lower()
        parsed_hostname = parsed.hostname
        if scheme != "file" and not parsed_hostname:
            raise ValueError("remote URL requires a hostname")
        hostname = (parsed_hostname or "").lower()
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        port = parsed.port
        netloc = hostname if port is None else f"{hostname}:{port}"
        path = _clean_remote_path(parsed.path, leading_slash=True)
        return urlunsplit((scheme, netloc, path, "", ""))

    return _clean_remote_path(stripped, leading_slash=stripped.startswith("/"))


def fingerprint_git_remote(value: str) -> str:
    normalized = normalize_git_remote(value)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def fingerprint_manifests(
    project_root: Path,
    marker_names: tuple[str, ...],
    *,
    anchor_root: Path | None = None,
    anchor_fd: int | None = None,
) -> str | None:
    root = Path(project_root)
    trusted_root = root
    trusted_root_fd: int | None = None
    project_components: tuple[str, ...] = ()
    if anchor_root is not None or anchor_fd is not None:
        if anchor_root is None or anchor_fd is None:
            raise ValueError("anchor_root and anchor_fd must be provided together")
        trusted_root = Path(anchor_root)
        trusted_root_fd = anchor_fd
        try:
            project_components = root.relative_to(trusted_root).parts
        except ValueError:
            return None

    manifests: list[dict[str, str]] = []
    for marker_name in sorted(set(marker_names) & _MANIFEST_NAMES):
        content = _read_bounded_regular_text(
            trusted_root,
            (*project_components, marker_name),
            trusted_root_fd=trusted_root_fd,
        )
        if content is None:
            continue
        normalized_name = _extract_normalized_name(marker_name, content)
        if normalized_name is None:
            continue
        manifests.append({"marker": marker_name, "name": normalized_name})

    if not manifests:
        return None

    canonical = json.dumps(
        {"manifests": manifests},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clean_remote_path(value: str, *, leading_slash: bool) -> str:
    without_query_or_fragment = re.split(r"[?#]", value, maxsplit=1)[0]
    cleaned = without_query_or_fragment.rstrip("/")
    cleaned = re.sub(r"(?i)\.git$", "", cleaned)
    cleaned = cleaned.rstrip("/")
    if leading_slash:
        return f"/{cleaned.lstrip('/')}" if cleaned else ""
    return cleaned.lstrip("/")


def _read_bounded_regular_text(
    trusted_root: Path,
    relative_components: tuple[str, ...],
    *,
    trusted_root_fd: int | None = None,
) -> str | None:
    content = _read_bounded_regular_bytes(
        trusted_root,
        relative_components,
        trusted_root_fd=trusted_root_fd,
    )
    if content is None:
        return None
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None


def _read_bounded_regular_bytes(
    trusted_root: Path,
    relative_components: tuple[str, ...],
    *,
    trusted_root_fd: int | None = None,
) -> bytes | None:
    if not _valid_relative_components(relative_components):
        return None

    opened_descriptors: list[int] = []
    directory_components: list[tuple[int, str, int, os.stat_result, Path]] = []
    trusted_root = Path(trusted_root)
    try:
        if trusted_root_fd is None:
            root_result = _open_metadata_descriptor(
                trusted_root,
                directory=True,
                dir_fd=None,
                logical_path=trusted_root,
            )
            if root_result is None:
                return None
            root_descriptor, root_before = root_result
            opened_descriptors.append(root_descriptor)
        else:
            root_descriptor = trusted_root_fd
            root_before = os.fstat(root_descriptor)
            if not _is_expected_type(root_before, directory=True):
                return None

        parent_descriptor = root_descriptor
        logical_parent = trusted_root
        for component in relative_components[:-1]:
            logical_component = logical_parent / component
            directory_result = _open_metadata_descriptor(
                component,
                directory=True,
                dir_fd=parent_descriptor,
                logical_path=logical_component,
            )
            if directory_result is None:
                return None
            directory_descriptor, directory_before = directory_result
            opened_descriptors.append(directory_descriptor)
            directory_components.append(
                (
                    parent_descriptor,
                    component,
                    directory_descriptor,
                    directory_before,
                    logical_component,
                )
            )
            parent_descriptor = directory_descriptor
            logical_parent = logical_component

        file_component = relative_components[-1]
        logical_file = logical_parent / file_component
        file_result = _open_metadata_descriptor(
            file_component,
            directory=False,
            dir_fd=parent_descriptor,
            logical_path=logical_file,
        )
        if file_result is None:
            return None
        file_descriptor, before = file_result
        opened_descriptors.append(file_descriptor)

        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_METADATA_BYTES:
            return None

        content = os.read(file_descriptor, before.st_size) if before.st_size else b""
        after = os.fstat(file_descriptor)
        if len(content) != before.st_size or _metadata_changed(before, after):
            return None
        if not _component_matches_descriptor(
            parent_descriptor,
            file_component,
            after,
            directory=False,
            logical_path=logical_file,
        ):
            return None

        for (
            component_parent,
            component_name,
            component_descriptor,
            component_before,
            logical_component,
        ) in reversed(directory_components):
            component_after = os.fstat(component_descriptor)
            if not _same_typed_identity(component_before, component_after, directory=True):
                return None
            if not _component_matches_descriptor(
                component_parent,
                component_name,
                component_after,
                directory=True,
                logical_path=logical_component,
            ):
                return None

        root_after = os.fstat(root_descriptor)
        if not _same_typed_identity(root_before, root_after, directory=True):
            return None
        root_path_metadata = _stat_component(
            trusted_root,
            dir_fd=None,
            logical_path=trusted_root,
        )
        if root_path_metadata is None or not _same_typed_identity(
            root_after, root_path_metadata, directory=True
        ):
            return None
        return content
    finally:
        for descriptor in reversed(opened_descriptors):
            os.close(descriptor)


def _open_metadata_descriptor(
    component: str | Path,
    *,
    directory: bool,
    dir_fd: int | None,
    logical_path: Path,
) -> tuple[int, os.stat_result] | None:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if directory:
        flags |= getattr(os, "O_DIRECTORY", 0)

    fallback_metadata = None
    if not getattr(os, "O_NOFOLLOW", 0):
        fallback_metadata = _stat_component(
            component,
            dir_fd=dir_fd,
            logical_path=logical_path,
        )
        if fallback_metadata is None or not _is_expected_type(
            fallback_metadata, directory=directory
        ):
            return None

    try:
        if dir_fd is None:
            descriptor = os.open(component, flags)
        else:
            descriptor = os.open(component, flags, dir_fd=dir_fd)
    except OSError as error:
        if _is_benign_metadata_path_error(error):
            return None
        raise _logical_metadata_error(error, logical_path) from error

    keep_descriptor = False
    try:
        metadata = os.fstat(descriptor)
        if not _is_expected_type(metadata, directory=directory):
            return None
        if fallback_metadata is not None and not _same_typed_identity(
            fallback_metadata, metadata, directory=directory
        ):
            return None
        keep_descriptor = True
        return descriptor, metadata
    finally:
        if not keep_descriptor:
            os.close(descriptor)


def _component_matches_descriptor(
    parent_descriptor: int,
    component: str,
    descriptor_metadata: os.stat_result,
    *,
    directory: bool,
    logical_path: Path,
) -> bool:
    path_metadata = _stat_component(
        component,
        dir_fd=parent_descriptor,
        logical_path=logical_path,
    )
    if path_metadata is None:
        return False
    if not _same_typed_identity(descriptor_metadata, path_metadata, directory=directory):
        return False
    if directory:
        return True
    return not _metadata_changed(descriptor_metadata, path_metadata)


def _stat_component(
    component: str | Path,
    *,
    dir_fd: int | None,
    logical_path: Path,
) -> os.stat_result | None:
    try:
        if dir_fd is None:
            return os.stat(component, follow_symlinks=False)
        return os.stat(component, dir_fd=dir_fd, follow_symlinks=False)
    except OSError as error:
        if _is_benign_metadata_path_error(error):
            return None
        raise _logical_metadata_error(error, logical_path) from error


def _logical_metadata_error(error: OSError, logical_path: Path) -> OSError:
    return OSError(error.errno, error.strerror, os.fspath(logical_path))


def _is_benign_metadata_path_error(error: OSError) -> bool:
    return error.errno in {
        errno.ENOENT,
        errno.ENOTDIR,
        errno.ELOOP,
        getattr(errno, "ESTALE", -1),
    }


def _valid_relative_components(components: tuple[str, ...]) -> bool:
    if not components:
        return False
    separators = {os.sep}
    if os.altsep:
        separators.add(os.altsep)
    return all(
        component not in {"", ".", ".."}
        and "\0" not in component
        and not any(separator in component for separator in separators)
        for component in components
    )


def _is_expected_type(metadata: os.stat_result, *, directory: bool) -> bool:
    if directory:
        return stat.S_ISDIR(metadata.st_mode)
    return stat.S_ISREG(metadata.st_mode)


def _same_typed_identity(
    left: os.stat_result,
    right: os.stat_result,
    *,
    directory: bool,
) -> bool:
    return (
        _is_expected_type(left, directory=directory)
        and _is_expected_type(right, directory=directory)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return _same_typed_identity(left, right, directory=False)


def _metadata_changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        not _same_file(before, after)
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
    )


def _project_artifact_id(content: str) -> str | None:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", content, flags=re.IGNORECASE):
        return None
    try:
        root = element_tree.fromstring(content)
    except element_tree.ParseError:
        return None
    if _local_xml_name(root.tag) != "project":
        return None
    for child in root:
        if _local_xml_name(child.tag) == "artifactId":
            return child.text
    return None


def _local_xml_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1].rsplit(":", maxsplit=1)[-1]


def _extract_normalized_name(marker_name: str, content: str) -> str | None:
    try:
        if marker_name == "package.json":
            document = json.loads(content)
            value = document.get("name") if isinstance(document, dict) else None
        elif marker_name == "pyproject.toml":
            document = tomllib.loads(content)
            project = document.get("project", {})
            poetry = document.get("tool", {}).get("poetry", {})
            value = project.get("name") or poetry.get("name")
        elif marker_name == "Cargo.toml":
            document = tomllib.loads(content)
            value = document.get("package", {}).get("name")
        elif marker_name == "go.mod":
            match = re.search(r"(?m)^\s*module\s+([^\s]+)\s*$", content)
            value = match.group(1) if match else None
        elif marker_name == "pom.xml":
            value = _project_artifact_id(content)
        elif marker_name == "build.gradle":
            match = re.search(
                r"(?m)^\s*(?:rootProject\.)?name\s*=\s*['\"]([^'\"]+)['\"]",
                content,
            )
            value = match.group(1) if match else None
        else:
            return None
    except (
        AttributeError,
        RecursionError,
        tomllib.TOMLDecodeError,
        TypeError,
        ValueError,
    ):
        return None

    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    return normalized or None
