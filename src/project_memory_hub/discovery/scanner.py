import configparser
import errno
import os
import re
import stat
from pathlib import Path

from project_memory_hub.discovery.fingerprint import (
    _read_bounded_regular_text,
    fingerprint_git_remote,
    fingerprint_manifests,
)
from project_memory_hub.discovery.policy import DiscoveryPolicy
from project_memory_hub.domain import DiscoveryIssue, DiscoveryResult, ProjectCandidate


_PERMISSION_REMEDIATION = (
    "On macOS, grant Files and Folders or Full Disk Access in System Settings, "
    "then retry discovery."
)


class ProjectScanner:
    def __init__(self, policy: DiscoveryPolicy) -> None:
        self._policy = policy

    def discover(self) -> DiscoveryResult:
        candidates: dict[Path, ProjectCandidate] = {}
        issues: list[DiscoveryIssue] = []
        for allowed_root in self._policy.allowed_roots:
            try:
                allowed_root_fd = _open_allowed_root(allowed_root)
            except OSError as error:
                self._add_os_issue(
                    allowed_root,
                    error,
                    issues,
                    missing_root=True,
                )
                continue
            try:
                self._scan_directory(
                    allowed_root,
                    allowed_root=allowed_root,
                    allowed_root_fd=allowed_root_fd,
                    depth=0,
                    configured_root=True,
                    candidates=candidates,
                    issues=issues,
                )
            finally:
                os.close(allowed_root_fd)

        ordered_candidates = tuple(candidates[path] for path in sorted(candidates, key=str))
        unique_issues = {(issue.path, issue.code, issue.remediation): issue for issue in issues}
        ordered_issues = tuple(
            sorted(
                unique_issues.values(),
                key=lambda issue: (str(issue.path), issue.code, issue.remediation),
            )
        )
        return DiscoveryResult(candidates=ordered_candidates, issues=ordered_issues)

    def _scan_directory(
        self,
        path: Path,
        *,
        allowed_root: Path,
        allowed_root_fd: int,
        depth: int,
        configured_root: bool,
        candidates: dict[Path, ProjectCandidate],
        issues: list[DiscoveryIssue],
    ) -> None:
        if depth > self._policy.max_depth or not _is_within(path, allowed_root):
            return

        try:
            with os.scandir(path) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except FileNotFoundError as error:
            self._add_os_issue(
                path,
                error,
                issues,
                missing_root=configured_root,
            )
            return
        except OSError as error:
            self._add_os_issue(path, error, issues)
            return

        entry_details: dict[str, tuple[Path, bool, bool]] = {}
        child_directories: list[tuple[str, Path]] = []
        contains_obsidian_directory = False
        for entry in entries:
            entry_path = Path(entry.path)
            try:
                if entry.is_symlink():
                    continue
                is_directory = entry.is_dir(follow_symlinks=False)
                is_file = entry.is_file(follow_symlinks=False)
            except OSError as error:
                self._add_os_issue(entry_path, error, issues)
                continue

            entry_details[entry.name] = (entry_path, is_directory, is_file)
            if entry.name == ".obsidian" and is_directory:
                contains_obsidian_directory = True
            if not is_directory:
                continue
            if self._is_skipped_directory(entry.name):
                continue
            child_directories.append((entry.name, entry_path))

        if contains_obsidian_directory:
            return

        marker_names = tuple(
            marker
            for marker in self._policy.project_markers
            if _is_marker(marker, entry_details.get(marker))
        )
        is_project = bool(marker_names)
        if is_project:
            try:
                canonical_path = path.resolve(strict=True)
            except OSError as error:
                self._add_os_issue(path, error, issues)
                return
            if not _is_within(canonical_path, allowed_root):
                return

            git_remote_fingerprint: str | None = None
            manifest_fingerprint: str | None = None
            if ".git" in marker_names:
                try:
                    remote = _read_git_remote(
                        canonical_path,
                        allowed_root,
                        allowed_root_fd,
                    )
                    if remote:
                        git_remote_fingerprint = fingerprint_git_remote(remote)
                except ValueError:
                    pass
                except OSError as error:
                    self._add_os_issue(
                        Path(error.filename) if error.filename else canonical_path,
                        error,
                        issues,
                    )
            try:
                manifest_fingerprint = fingerprint_manifests(
                    canonical_path,
                    marker_names,
                    anchor_root=allowed_root,
                    anchor_fd=allowed_root_fd,
                )
            except OSError as error:
                self._add_os_issue(
                    Path(error.filename) if error.filename else canonical_path,
                    error,
                    issues,
                )

            candidates.setdefault(
                canonical_path,
                ProjectCandidate(
                    canonical_path=canonical_path,
                    display_name=canonical_path.name or str(canonical_path),
                    git_root=canonical_path if ".git" in marker_names else None,
                    git_remote_fingerprint=git_remote_fingerprint,
                    manifest_fingerprint=manifest_fingerprint,
                    markers=marker_names,
                ),
            )

        if depth == self._policy.max_depth:
            return
        workspace_names = set(self._policy.workspace_directory_names)
        for child_name, child_path in child_directories:
            if is_project and child_name not in workspace_names:
                continue
            if not _is_within(child_path.resolve(strict=False), allowed_root):
                continue
            self._scan_directory(
                child_path,
                allowed_root=allowed_root,
                allowed_root_fd=allowed_root_fd,
                depth=depth + 1,
                configured_root=False,
                candidates=candidates,
                issues=issues,
            )

    def _is_skipped_directory(self, name: str) -> bool:
        return (
            name in self._policy.excluded_directory_names
            or name.startswith(".")
            or any(
                re.search(pattern, name, flags=re.IGNORECASE)
                for pattern in self._policy.sensitive_filename_patterns
            )
        )

    @staticmethod
    def _add_os_issue(
        path: Path,
        error: OSError,
        issues: list[DiscoveryIssue],
        *,
        missing_root: bool = False,
    ) -> None:
        canonical_path = Path(path).expanduser().resolve(strict=False)
        if missing_root and isinstance(error, FileNotFoundError):
            code = "missing_root"
            remediation = "Choose an existing project root in discovery settings."
        elif isinstance(error, PermissionError) or error.errno in {
            errno.EACCES,
            errno.EPERM,
        }:
            code = "blocked_permission"
            remediation = _PERMISSION_REMEDIATION
        else:
            code = "scan_error"
            remediation = "Check that the path is readable and retry discovery."
        issues.append(
            DiscoveryIssue(
                path=canonical_path,
                code=code,
                remediation=remediation,
            )
        )


def _is_marker(marker_name: str, details: tuple[Path, bool, bool] | None) -> bool:
    if details is None:
        return False
    _, is_directory, is_file = details
    if marker_name == ".git":
        return is_directory or is_file
    return is_file


def _is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _open_allowed_root(allowed_root: Path) -> int:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
    )
    fallback_metadata = None
    if not getattr(os, "O_NOFOLLOW", 0):
        fallback_metadata = os.stat(allowed_root, follow_symlinks=False)
        if not stat.S_ISDIR(fallback_metadata.st_mode):
            raise NotADirectoryError(
                errno.ENOTDIR,
                "configured discovery root is not a directory",
                allowed_root,
            )

    descriptor = os.open(allowed_root, flags)
    keep_descriptor = False
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(allowed_root, follow_symlinks=False)
        if not _same_directory_identity(descriptor_metadata, path_metadata):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "configured discovery root changed while opening",
                allowed_root,
            )
        if fallback_metadata is not None and not _same_directory_identity(
            fallback_metadata, descriptor_metadata
        ):
            raise OSError(
                getattr(errno, "ESTALE", errno.EIO),
                "configured discovery root changed while opening",
                allowed_root,
            )
        keep_descriptor = True
        return descriptor
    finally:
        if not keep_descriptor:
            os.close(descriptor)


def _same_directory_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISDIR(left.st_mode)
        and stat.S_ISDIR(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _read_git_remote(
    project_root: Path,
    allowed_root: Path,
    allowed_root_fd: int,
) -> str | None:
    config_path = project_root / ".git" / "config"
    if not _is_within(config_path, allowed_root):
        return None
    try:
        project_components = project_root.relative_to(allowed_root).parts
    except ValueError:
        return None
    text = _read_bounded_regular_text(
        allowed_root,
        (*project_components, ".git", "config"),
        trusted_root_fd=allowed_root_fd,
    )
    if text is None:
        return None

    parser = configparser.RawConfigParser(interpolation=None)
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    remote_sections = sorted(
        section for section in parser.sections() if section.startswith('remote "')
    )
    origin = 'remote "origin"'
    if origin in remote_sections:
        remote_sections.remove(origin)
        remote_sections.insert(0, origin)
    for section in remote_sections:
        remote = parser.get(section, "url", fallback="").strip()
        if remote:
            return remote
    return None
