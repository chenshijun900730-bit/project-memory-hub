from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import os
import shutil
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import unquote_to_bytes, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, field_validator, model_validator


AUTOMATION_NAME: Literal["Project Memory Hub Daily Reconcile"] = (
    "Project Memory Hub Daily Reconcile"
)
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LOCAL_TIME = "03:30"
_MAX_AUTOMATION_BYTES = 64 * 1024
_MAX_AUTOMATION_ENTRIES = 1024
_MAX_SITE_PACKAGE_ENTRIES = 4096

AutomationStatus = Literal[
    "missing",
    "duplicate",
    "disabled",
    "drifted",
    "current",
]
InstalledSourceStatus = Literal["not-local-source", "trusted", "invalid"]


class InstallationIdentity(BaseModel, frozen=True):
    launcher: Path
    repository_root: Path
    repository_device: int
    repository_inode: int

    @classmethod
    def discover_launcher(cls, *, launcher: Path | None = None) -> Path | None:
        """Resolve a stable launcher without requiring a source repository checkout."""

        try:
            if launcher is not None:
                return _discover_launcher(Path(launcher))
            located = shutil.which("memory-hub")
            if located is None:
                return None
            return _discover_uv_launcher(Path(located).resolve(strict=True))
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return None

    @classmethod
    def discover(
        cls,
        *,
        launcher: Path | None = None,
        module_path: Path | None = None,
    ) -> "InstallationIdentity | None":
        try:
            safe_launcher = cls.discover_launcher(launcher=launcher)
            if safe_launcher is None:
                return None

            source_path = Path(module_path) if module_path is not None else Path(__file__)
            repository_root = _discover_repository_root(source_path)
            if repository_root is None:
                return None
            repository_identity = _repository_identity(repository_root)
            if repository_identity is None:
                return None
            return cls(
                launcher=safe_launcher,
                repository_root=repository_root,
                repository_device=repository_identity[0],
                repository_inode=repository_identity[1],
            )
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return None

    @classmethod
    def discover_installed_source(
        cls,
        *,
        launcher: Path | None = None,
        module_path: Path | None = None,
    ) -> "InstallationIdentity | None":
        """Bind an installed package to its independently recorded local source."""

        resolution = cls.resolve_installed_source(
            launcher=launcher,
            module_path=module_path,
        )
        return resolution.identity if resolution.status == "trusted" else None

    @classmethod
    def resolve_installed_source(
        cls,
        *,
        launcher: Path | None = None,
        module_path: Path | None = None,
    ) -> "InstalledSourceResolution":
        try:
            safe_launcher = cls.discover_launcher(launcher=launcher)
            if safe_launcher is None:
                return InstalledSourceResolution(status="invalid")
            installed_module = Path(module_path) if module_path is not None else Path(__file__)
            if not cls.is_installed_distribution(module_path=installed_module):
                return InstalledSourceResolution(status="invalid")
            status, repository_root = _resolve_installed_source_root(installed_module)
            if status != "trusted" or repository_root is None:
                return InstalledSourceResolution(status=status)
            repository_identity = _repository_identity(repository_root)
            if repository_identity is None:
                return InstalledSourceResolution(status="invalid")
            identity = cls(
                launcher=safe_launcher,
                repository_root=repository_root,
                repository_device=repository_identity[0],
                repository_inode=repository_identity[1],
            )
            return InstalledSourceResolution(status="trusted", identity=identity)
        except (OSError, RuntimeError, UnicodeError, ValueError):
            return InstalledSourceResolution(status="invalid")

    @classmethod
    def is_installed_distribution(cls, *, module_path: Path | None = None) -> bool:
        """Identify a normal installed package without treating it as a source repository."""

        path = Path(module_path) if module_path is not None else Path(__file__)
        if (
            not _safe_path_text(path)
            or not _is_canonical_absolute(path)
            or not _is_installed_package_path(path)
        ):
            return False
        try:
            metadata = path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == os.getuid()
            and metadata.st_mode & 0o022 == 0
        )


class InstalledSourceResolution(BaseModel, frozen=True):
    status: InstalledSourceStatus
    identity: InstallationIdentity | None = None

    @model_validator(mode="after")
    def validate_status_identity(self) -> "InstalledSourceResolution":
        if (self.status == "trusted") != (self.identity is not None):
            raise ValueError("trusted installed source requires exactly one identity")
        return self


class DesiredAutomation(BaseModel, frozen=True):
    name: Literal["Project Memory Hub Daily Reconcile"]
    timezone: str
    local_time: str
    repository_root: Path
    launcher: Path
    project_id: str | None = None
    prompt: str
    rrule: str
    execution_environment: Literal["local"] = "local"
    enabled: bool = True

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("timezone must be a valid IANA timezone") from error
        return value

    @field_validator("local_time")
    @classmethod
    def validate_local_time(cls, value: str) -> str:
        _parse_local_time(value)
        return value

    @field_validator("repository_root")
    @classmethod
    def validate_repository_root(cls, value: Path) -> Path:
        return _stable_path(value, kind="directory")

    @field_validator("launcher")
    @classmethod
    def validate_launcher(cls, value: Path) -> Path:
        path = _stable_path(value, kind="executable")
        if not os.access(path, os.X_OK):
            raise ValueError("launcher must be executable")
        return path

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value.strip()
            or value != value.strip()
            or len(value) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ValueError("project_id must be bounded safe metadata")
        return value

    @model_validator(mode="after")
    def validate_derived_fields(self) -> "DesiredAutomation":
        if self.rrule != _daily_rrule(self.timezone, self.local_time):
            raise ValueError("rrule must match timezone and local_time")
        return self

    @property
    def project_root(self) -> Path:
        return self.repository_root

    @classmethod
    def daily_reconcile(
        cls,
        timezone: str = DEFAULT_TIMEZONE,
        local_time: str = DEFAULT_LOCAL_TIME,
        *,
        repository_root: Path,
        launcher: Path,
        project_id: str | None = None,
    ) -> "DesiredAutomation":
        prompt = (
            "Invoke the Project Memory Hub MCP tool reconcile_if_due_v1 with {}. "
            "Report only health, counts, blocked paths, and confirmation-queue size. "
            "Never expose conversation content."
        )
        return cls(
            name=AUTOMATION_NAME,
            timezone=timezone,
            local_time=local_time,
            repository_root=repository_root,
            launcher=launcher,
            project_id=project_id,
            prompt=prompt,
            rrule=_daily_rrule(timezone, local_time),
        )


class AutomationInspection(BaseModel, frozen=True):
    status: AutomationStatus
    matches: int
    remediation: str = ""


@dataclass(frozen=True, slots=True)
class _AutomationRecord:
    kind: str
    name: str
    prompt: str
    status: str
    rrule: str
    execution_environment: str | None
    target_type: str | None
    project_id: str | None
    cwds: tuple[str, ...]


class _UntrustedAutomationMetadata(Exception):
    pass


class AutomationInspector:
    def __init__(self, automations_root: Path) -> None:
        self.automations_root = Path(automations_root)

    def inspect(self, desired: DesiredAutomation) -> AutomationInspection:
        records = self._records()
        if records is None:
            return AutomationInspection(
                status="drifted",
                matches=0,
                remediation=(
                    "Automation metadata could not be safely inspected. Repair it "
                    "through the Codex automation interface."
                ),
            )
        if records is _MISSING_AUTOMATIONS_ROOT:
            return AutomationInspection(
                status="missing",
                matches=0,
                remediation=(
                    "Create the exact named automation through the Codex automation interface."
                ),
            )
        assert isinstance(records, tuple)

        matches = tuple(record for record in records if record.name == desired.name)
        if not matches:
            return AutomationInspection(
                status="missing",
                matches=0,
                remediation=(
                    "Create the exact named automation through the Codex automation interface."
                ),
            )
        if len(matches) > 1:
            return AutomationInspection(
                status="duplicate",
                matches=len(matches),
                remediation=(
                    "Remove duplicate exact-name automations through the Codex "
                    "automation interface."
                ),
            )

        record = matches[0]
        if desired.enabled and record.status != "ACTIVE":
            return AutomationInspection(
                status="disabled",
                matches=1,
                remediation=(
                    "Enable the exact named automation through the Codex automation interface."
                ),
            )
        if not self._matches_desired_state(record, desired):
            return AutomationInspection(
                status="drifted",
                matches=1,
                remediation=(
                    "Update the exact named automation through the Codex automation interface."
                ),
            )
        return AutomationInspection(status="current", matches=1)

    def _records(
        self,
    ) -> tuple[_AutomationRecord, ...] | object | None:
        if not self.automations_root.is_absolute():
            return None
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            root_descriptor = os.open(self.automations_root, directory_flags)
        except FileNotFoundError:
            return _MISSING_AUTOMATIONS_ROOT
        except OSError:
            return None
        if not _is_canonical_absolute(self.automations_root) or not _descriptor_matches_path(
            root_descriptor,
            self.automations_root,
            kind="directory",
        ):
            os.close(root_descriptor)
            return None

        records: list[_AutomationRecord] = []
        observed_entries = 0
        try:
            try:
                with os.scandir(root_descriptor) as entries:
                    for entry in entries:
                        observed_entries += 1
                        if observed_entries > _MAX_AUTOMATION_ENTRIES:
                            raise _UntrustedAutomationMetadata
                        if entry.is_symlink():
                            raise _UntrustedAutomationMetadata
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        record = self._record_from_directory(
                            root_descriptor,
                            entry.name,
                            directory_flags,
                        )
                        if record is not None:
                            records.append(record)
                if not _descriptor_matches_path(
                    root_descriptor,
                    self.automations_root,
                    kind="directory",
                ):
                    raise _UntrustedAutomationMetadata
            except (OSError, _UntrustedAutomationMetadata):
                return None
        finally:
            os.close(root_descriptor)
        return tuple(records)

    def _record_from_directory(
        self,
        root_descriptor: int,
        directory_name: str,
        directory_flags: int,
    ) -> _AutomationRecord | None:
        try:
            directory_descriptor = os.open(
                directory_name,
                directory_flags,
                dir_fd=root_descriptor,
            )
        except OSError as error:
            raise _UntrustedAutomationMetadata from error
        if not _descriptor_matches_path(
            directory_descriptor,
            directory_name,
            kind="directory",
            dir_fd=root_descriptor,
        ):
            os.close(directory_descriptor)
            raise _UntrustedAutomationMetadata

        try:
            file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(
                    "automation.toml",
                    file_flags,
                    dir_fd=directory_descriptor,
                )
            except FileNotFoundError:
                return None
            except OSError as error:
                raise _UntrustedAutomationMetadata from error
            try:
                metadata = os.fstat(descriptor)
                if (
                    not _descriptor_matches_path(
                        descriptor,
                        "automation.toml",
                        kind="file",
                        dir_fd=directory_descriptor,
                    )
                    or metadata.st_nlink != 1
                ):
                    raise _UntrustedAutomationMetadata
                document = tomllib.loads(_read_bounded(descriptor).decode("utf-8"))
                if not _descriptor_matches_path(
                    descriptor,
                    "automation.toml",
                    kind="file",
                    dir_fd=directory_descriptor,
                ):
                    raise _UntrustedAutomationMetadata
            except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
                raise _UntrustedAutomationMetadata from error
            finally:
                os.close(descriptor)
        finally:
            directory_matches = _descriptor_matches_path(
                directory_descriptor,
                directory_name,
                kind="directory",
                dir_fd=root_descriptor,
            )
            os.close(directory_descriptor)
            if not directory_matches:
                raise _UntrustedAutomationMetadata

        return _parse_record(document, directory_name)

    @staticmethod
    def _matches_desired_state(
        record: _AutomationRecord,
        desired: DesiredAutomation,
    ) -> bool:
        return (
            record.kind == "cron"
            and record.prompt == desired.prompt
            and record.rrule == desired.rrule
            and record.execution_environment == desired.execution_environment
            and record.target_type == "project"
            and record.project_id == desired.project_id
            and record.cwds == (str(desired.repository_root),)
            and (record.status == "ACTIVE") == desired.enabled
        )


_MISSING_AUTOMATIONS_ROOT = object()


def _read_bounded(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(8192, _MAX_AUTOMATION_BYTES + 1 - total))
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_AUTOMATION_BYTES:
            raise _UntrustedAutomationMetadata


def _parse_record(document: object, directory_name: str) -> _AutomationRecord:
    if not isinstance(document, dict):
        raise _UntrustedAutomationMetadata
    if type(document.get("version")) is not int or document["version"] != 1:
        raise _UntrustedAutomationMetadata
    required_strings = (
        "id",
        "kind",
        "name",
        "prompt",
        "status",
        "rrule",
    )
    if any(not isinstance(document.get(field), str) for field in required_strings):
        raise _UntrustedAutomationMetadata
    if document["id"] != directory_name:
        raise _UntrustedAutomationMetadata
    if document["kind"] not in {"cron", "heartbeat"}:
        raise _UntrustedAutomationMetadata
    if document["status"] not in {"ACTIVE", "PAUSED", "DELETED"}:
        raise _UntrustedAutomationMetadata
    if any(type(document.get(field)) is not int for field in ("created_at", "updated_at")):
        raise _UntrustedAutomationMetadata

    execution_environment: str | None = None
    target_type: str | None = None
    project_id: str | None = None
    cwds: tuple[str, ...] = ()
    if document["kind"] == "heartbeat":
        target_thread_id = document.get("target_thread_id")
        if not isinstance(target_thread_id, str) or not target_thread_id.strip():
            raise _UntrustedAutomationMetadata
    else:
        raw_environment = document.get("execution_environment", "worktree")
        if raw_environment not in {"local", "worktree"}:
            raise _UntrustedAutomationMetadata
        execution_environment = raw_environment
        raw_cwds = document.get("cwds")
        if not isinstance(raw_cwds, list) or any(not isinstance(cwd, str) for cwd in raw_cwds):
            raise _UntrustedAutomationMetadata
        cwds = tuple(raw_cwds)
        target = document.get("target")
        if target is not None:
            if not isinstance(target, dict) or target.get("type") not in {
                "project",
                "projectless",
            }:
                raise _UntrustedAutomationMetadata
            target_type = target["type"]
            if target_type == "project":
                raw_project_id = target.get("project_id")
                if not isinstance(raw_project_id, str) or not raw_project_id.strip():
                    raise _UntrustedAutomationMetadata
                project_id = raw_project_id
    return _AutomationRecord(
        kind=document["kind"],
        name=document["name"],
        prompt=document["prompt"],
        status=document["status"],
        rrule=document["rrule"],
        execution_environment=execution_environment,
        target_type=target_type,
        project_id=project_id,
        cwds=cwds,
    )


def _discover_launcher(path: Path) -> Path | None:
    if (
        not _is_canonical_absolute(path)
        or _has_unstable_part(path)
        or path.name != "memory-hub"
        or not _safe_path_text(path)
    ):
        return None
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not _descriptor_matches_path(descriptor, path, kind="file")
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or not metadata.st_mode & stat.S_IXUSR
            or not os.access(path, os.X_OK)
        ):
            return None
    finally:
        os.close(descriptor)
    return path


def _discover_uv_launcher(path: Path) -> Path | None:
    if (
        not _is_canonical_absolute(path)
        or _has_unstable_part(path)
        or path.name != "memory-hub"
        or not _safe_path_text(path)
    ):
        return None
    raw_root = os.environ.get("UV_TOOL_DIR")
    tools_root = (
        Path(raw_root).expanduser()
        if raw_root is not None
        else Path.home() / ".local" / "share" / "uv" / "tools"
    )
    if not _safe_path_text(tools_root) or not _is_canonical_absolute(tools_root):
        return None
    try:
        relative = path.relative_to(tools_root)
    except ValueError:
        return None
    if relative.parts != ("project-memory-hub", "bin", "memory-hub"):
        return None

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    try:
        root_fd = os.open(tools_root, directory_flags)
        descriptors.append(root_fd)
        environment_fd = os.open(
            "project-memory-hub",
            directory_flags,
            dir_fd=root_fd,
        )
        descriptors.append(environment_fd)
        bin_fd = os.open("bin", directory_flags, dir_fd=environment_fd)
        descriptors.append(bin_fd)
        launcher_fd = os.open("memory-hub", file_flags, dir_fd=bin_fd)
        descriptors.append(launcher_fd)
        pyvenv_fd = os.open("pyvenv.cfg", file_flags, dir_fd=environment_fd)
        descriptors.append(pyvenv_fd)

        launcher_metadata = os.fstat(launcher_fd)
        pyvenv_metadata = os.fstat(pyvenv_fd)
        if (
            not _descriptor_matches_path(root_fd, tools_root, kind="directory")
            or not _descriptor_matches_path(
                environment_fd,
                "project-memory-hub",
                kind="directory",
                dir_fd=root_fd,
            )
            or not _descriptor_matches_path(
                bin_fd,
                "bin",
                kind="directory",
                dir_fd=environment_fd,
            )
            or not _descriptor_matches_path(
                launcher_fd,
                "memory-hub",
                kind="file",
                dir_fd=bin_fd,
            )
            or launcher_metadata.st_nlink != 1
            or not launcher_metadata.st_mode & stat.S_IXUSR
            or not _descriptor_matches_path(
                pyvenv_fd,
                "pyvenv.cfg",
                kind="file",
                dir_fd=environment_fd,
            )
            or pyvenv_metadata.st_nlink != 1
        ):
            return None
        try:
            document = _read_bounded(pyvenv_fd).decode("utf-8")
        except (UnicodeError, _UntrustedAutomationMetadata):
            return None
        if not any(line.strip().startswith("uv = ") for line in document.splitlines()):
            return None
        if (
            not _descriptor_matches_path(root_fd, tools_root, kind="directory")
            or not _descriptor_matches_path(
                environment_fd,
                "project-memory-hub",
                kind="directory",
                dir_fd=root_fd,
            )
            or not _descriptor_matches_path(
                bin_fd,
                "bin",
                kind="directory",
                dir_fd=environment_fd,
            )
            or not _descriptor_matches_path(
                launcher_fd,
                "memory-hub",
                kind="file",
                dir_fd=bin_fd,
            )
        ):
            return None
    except OSError:
        return None
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
    return path


def _discover_repository_root(module_path: Path) -> Path | None:
    if (
        not _is_canonical_absolute(module_path)
        or _has_unstable_part(module_path)
        or _is_installed_package_path(module_path)
    ):
        return None
    try:
        module_metadata = os.lstat(module_path)
    except OSError:
        return None
    if (
        not stat.S_ISREG(module_metadata.st_mode)
        or module_metadata.st_uid != os.getuid()
        or module_metadata.st_nlink != 1
        or module_metadata.st_mode & 0o022
        or not _secure_directory_ancestors(module_path.parent)
    ):
        return None

    for candidate in module_path.parents:
        pyproject_path = candidate / "pyproject.toml"
        git_path = candidate / ".git"
        try:
            root_metadata = os.lstat(candidate)
            git_metadata = os.lstat(git_path)
            pyproject_metadata = os.lstat(pyproject_path)
        except OSError:
            continue
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
            or root_metadata.st_mode & 0o022
            or not stat.S_ISDIR(git_metadata.st_mode)
            or git_metadata.st_uid != os.getuid()
            or git_metadata.st_mode & 0o022
            or not stat.S_ISREG(pyproject_metadata.st_mode)
            or pyproject_metadata.st_uid != os.getuid()
            or pyproject_metadata.st_nlink != 1
            or pyproject_metadata.st_mode & 0o022
            or not _is_canonical_absolute(candidate)
            or _has_unstable_part(candidate)
            or not _secure_directory_ancestors(candidate)
        ):
            continue
        try:
            relative_module = module_path.relative_to(candidate)
        except ValueError:
            continue
        if relative_module.parts[:2] != ("src", "project_memory_hub"):
            continue
        project_document = _read_project_document(pyproject_path)
        if project_document is None:
            return None
        project = project_document.get("project")
        if not isinstance(project, dict) or project.get("name") != "project-memory-hub":
            return None
        return candidate
    return None


def _read_project_document(path: Path) -> dict[str, object] | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not _descriptor_matches_path(descriptor, path, kind="file"):
            return None
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.getuid() or metadata.st_nlink != 1:
            return None
        try:
            document = tomllib.loads(_read_bounded(descriptor).decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError, _UntrustedAutomationMetadata):
            return None
        if not _descriptor_matches_path(descriptor, path, kind="file"):
            return None
    finally:
        os.close(descriptor)
    return document


def _is_canonical_absolute(path: Path) -> bool:
    if not path.is_absolute():
        return False
    try:
        return path == path.resolve(strict=True)
    except (OSError, RuntimeError):
        return False


def _has_unstable_part(path: Path) -> bool:
    return ".worktrees" in path.parts


def _safe_path_text(path: Path) -> bool:
    document = os.fspath(path)
    return (
        0 < len(document.encode("utf-8")) <= 8192
        and "`" not in document
        and not any(ord(character) < 32 or ord(character) == 127 for character in document)
    )


def _is_installed_package_path(path: Path) -> bool:
    lowered_parts = tuple(part.casefold() for part in path.parts)
    return (
        "site-packages" in lowered_parts
        or "dist-packages" in lowered_parts
        or any(part.endswith(".whl") for part in lowered_parts)
    )


def _resolve_installed_source_root(
    module_path: Path,
) -> tuple[InstalledSourceStatus, Path | None]:
    site_packages = _installed_site_packages(module_path)
    if site_packages is None:
        return "invalid", None
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    site_fd: int | None = None
    dist_fd: int | None = None
    try:
        site_fd = os.open(site_packages, directory_flags)
        if not _descriptor_matches_path(site_fd, site_packages, kind="directory"):
            return "invalid", None
        matches: list[str] = []
        observed = 0
        with os.scandir(site_fd) as entries:
            for entry in entries:
                observed += 1
                if observed > _MAX_SITE_PACKAGE_ENTRIES:
                    return "invalid", None
                name = entry.name
                if (
                    name.startswith("project_memory_hub-")
                    and name.endswith(".dist-info")
                    and entry.is_dir(follow_symlinks=False)
                    and not entry.is_symlink()
                ):
                    matches.append(name)
        if len(matches) != 1:
            return "invalid", None

        dist_name = matches[0]
        dist_fd = os.open(dist_name, directory_flags, dir_fd=site_fd)
        if not _descriptor_matches_path(
            dist_fd,
            dist_name,
            kind="directory",
            dir_fd=site_fd,
        ):
            return "invalid", None
        metadata_payload = _read_secure_relative_file(dist_fd, "METADATA")
        record_payload = _read_secure_relative_file(dist_fd, "RECORD")
        direct_url_payload = _read_secure_relative_file(dist_fd, "direct_url.json")
        module_payload = _read_secure_file(module_path)
        if metadata_payload is None or record_payload is None or module_payload is None:
            return "invalid", None
        if not _descriptor_matches_path(
            site_fd, site_packages, kind="directory"
        ) or not _descriptor_matches_path(
            dist_fd,
            dist_name,
            kind="directory",
            dir_fd=site_fd,
        ):
            return "invalid", None
    except (OSError, UnicodeError, _UntrustedAutomationMetadata):
        return "invalid", None
    finally:
        if dist_fd is not None:
            os.close(dist_fd)
        if site_fd is not None:
            os.close(site_fd)

    version = dist_name[len("project_memory_hub-") : -len(".dist-info")]
    if (
        not version
        or _metadata_value(metadata_payload, "Name") != "project-memory-hub"
        or _metadata_value(metadata_payload, "Version") != version
    ):
        return "invalid", None
    records = _parse_record_rows(record_payload)
    if records is None:
        return "invalid", None
    try:
        module_relative = module_path.relative_to(site_packages).as_posix()
    except ValueError:
        return "invalid", None
    if not _record_binds(records, module_relative, module_payload):
        return "invalid", None
    direct_url_relative = f"{dist_name}/direct_url.json"
    if direct_url_payload is None:
        if any(row[0] == direct_url_relative for row in records):
            return "invalid", None
        return "not-local-source", None
    if not _record_binds(records, direct_url_relative, direct_url_payload):
        return "invalid", None

    try:
        document = json.loads(direct_url_payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return "invalid", None
    if not isinstance(document, dict):
        return "invalid", None
    raw_url = document.get("url")
    if not isinstance(raw_url, str) or not _valid_direct_url(raw_url):
        return "invalid", None
    provenance_keys = set(document).intersection({"archive_info", "dir_info", "vcs_info"})
    if len(provenance_keys) != 1:
        return "invalid", None
    provenance_key = provenance_keys.pop()
    provenance = document.get(provenance_key)
    if provenance_key != "dir_info":
        if (
            not isinstance(provenance, dict)
            or not {"url", provenance_key}.issubset(document)
            or not set(document).issubset({"url", provenance_key, "subdirectory"})
            or (
                "subdirectory" in document
                and not _valid_direct_url_subdirectory(document["subdirectory"])
            )
            or (provenance_key == "archive_info" and not _valid_archive_info(provenance))
            or (provenance_key == "vcs_info" and not _valid_vcs_info(provenance))
        ):
            return "invalid", None
        return "not-local-source", None
    if set(document) != {"url", "dir_info"}:
        return "invalid", None
    dir_info = document.get("dir_info")
    editable = dir_info.get("editable") if isinstance(dir_info, dict) else None
    if (
        not isinstance(dir_info, dict)
        or not set(dir_info).issubset({"editable"})
        or ("editable" in dir_info and type(editable) is not bool)
    ):
        return "invalid", None
    parsed = urlsplit(raw_url)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        return "invalid", None
    try:
        decoded_path = unquote_to_bytes(parsed.path).decode("utf-8")
    except UnicodeDecodeError:
        return "invalid", None
    source_root = Path(decoded_path)
    if not _safe_path_text(source_root) or not source_root.is_absolute():
        return "invalid", None
    discovered = _discover_repository_root(
        source_root / "src" / "project_memory_hub" / "__init__.py"
    )
    if discovered != source_root:
        return "invalid", None
    return "trusted", source_root


def _valid_direct_url(value: str) -> bool:
    try:
        encoded = value.encode("utf-8")
        parsed = urlsplit(value)
    except (UnicodeError, ValueError):
        return False
    if (
        not 0 < len(encoded) <= 8192
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
        or not parsed.scheme
        or not parsed.scheme[0].isalpha()
        or not all(character.isalnum() or character in "+-." for character in parsed.scheme)
    ):
        return False
    if parsed.scheme == "file":
        return parsed.path.startswith("/")
    return bool(parsed.netloc or parsed.path.startswith("/"))


def _valid_archive_info(value: dict[object, object]) -> bool:
    if not set(value).issubset({"hash", "hashes"}):
        return False
    legacy_hash = value.get("hash")
    if legacy_hash is not None and (
        not isinstance(legacy_hash, str) or not _valid_hash_entry(legacy_hash)
    ):
        return False
    hashes = value.get("hashes")
    if hashes is not None:
        if not isinstance(hashes, dict):
            return False
        for algorithm, digest in hashes.items():
            if (
                not isinstance(algorithm, str)
                or not isinstance(digest, str)
                or not _valid_hash_entry(f"{algorithm}={digest}")
            ):
                return False
    return True


def _valid_hash_entry(value: str) -> bool:
    algorithm, separator, digest = value.partition("=")
    return (
        bool(separator)
        and 0 < len(value.encode("utf-8")) <= 8192
        and algorithm.replace("-", "").replace("_", "").isalnum()
        and all(character in "0123456789abcdefABCDEF" for character in digest)
        and bool(digest)
    )


def _valid_vcs_info(value: dict[object, object]) -> bool:
    vcs = value.get("vcs")
    commit_id = value.get("commit_id")
    if (
        not isinstance(vcs, str)
        or not _valid_direct_url_text(vcs, max_bytes=64)
        or vcs != vcs.casefold()
        or not all(character.isalnum() or character in "._-" for character in vcs)
        or not isinstance(commit_id, str)
        or not _valid_direct_url_text(commit_id)
    ):
        return False
    allowed = {
        "vcs",
        "commit_id",
        "requested_revision",
        "resolved_revision",
        "resolved_revision_type",
    }
    for key, item in value.items():
        if not isinstance(key, str) or (key not in allowed and not key.startswith(f"{vcs}_")):
            return False
        if key not in {"vcs", "commit_id"} and (
            not isinstance(item, str) or not _valid_direct_url_text(item)
        ):
            return False
    return True


def _valid_direct_url_text(value: str, *, max_bytes: int = 8192) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return 0 < len(encoded) <= max_bytes and not any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    )


def _valid_direct_url_subdirectory(value: object) -> bool:
    if not isinstance(value, str) or not 0 < len(value.encode("utf-8")) <= 8192:
        return False
    path = Path(value)
    return (
        not path.is_absolute()
        and not any(part in {"", ".", ".."} for part in path.parts)
        and _safe_path_text(path)
    )


def _read_secure_relative_file(parent_fd: int, name: str) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise _UntrustedAutomationMetadata from error
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_nlink != 1 or not _descriptor_matches_path(
            descriptor,
            name,
            kind="file",
            dir_fd=parent_fd,
        ):
            raise _UntrustedAutomationMetadata
        payload = _read_bounded(descriptor)
        if not _descriptor_matches_path(
            descriptor,
            name,
            kind="file",
            dir_fd=parent_fd,
        ):
            raise _UntrustedAutomationMetadata
        return payload
    finally:
        os.close(descriptor)


def _read_secure_file(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if metadata.st_nlink != 1 or not _descriptor_matches_path(descriptor, path, kind="file"):
            return None
        payload = _read_bounded(descriptor)
        if not _descriptor_matches_path(descriptor, path, kind="file"):
            return None
        return payload
    except _UntrustedAutomationMetadata:
        return None
    finally:
        os.close(descriptor)


def _metadata_value(payload: bytes, field: str) -> str | None:
    try:
        lines = payload.decode("utf-8").splitlines()
    except UnicodeDecodeError:
        return None
    prefix = f"{field}:"
    matches = [line[len(prefix) :].strip() for line in lines if line.startswith(prefix)]
    return matches[0] if len(matches) == 1 and matches[0] else None


def _parse_record_rows(payload: bytes) -> tuple[tuple[str, str, str], ...] | None:
    try:
        document = payload.decode("utf-8")
        rows = tuple(tuple(row) for row in csv.reader(io.StringIO(document), strict=True))
    except (UnicodeDecodeError, csv.Error):
        return None
    parsed: list[tuple[str, str, str]] = []
    for row in rows:
        if len(row) != 3:
            return None
        path, digest, size = row
        if not _valid_record_path(path):
            return None
        parsed.append((path, digest, size))
    return tuple(parsed)


def _valid_record_path(value: str) -> bool:
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    parts = value.split("/")
    if (
        not 0 < len(encoded) <= 8192
        or value.startswith("/")
        or "\\" in value
        or any(not part or part == "." for part in parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return False
    seen_payload = False
    for part in parts:
        if part == "..":
            if seen_payload:
                return False
        else:
            seen_payload = True
    return seen_payload


def _record_binds(
    rows: tuple[tuple[str, str, str], ...],
    relative_path: str,
    payload: bytes,
) -> bool:
    matches = tuple(row for row in rows if row[0] == relative_path)
    if len(matches) != 1:
        return False
    _path, digest, raw_size = matches[0]
    try:
        size = int(raw_size)
    except ValueError:
        return False
    expected = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
    return (
        size == len(payload)
        and digest.startswith("sha256=")
        and hmac.compare_digest(digest[len("sha256=") :], expected)
    )


def _installed_site_packages(module_path: Path) -> Path | None:
    if (
        not _safe_path_text(module_path)
        or not _is_canonical_absolute(module_path)
        or not _secure_directory_ancestors(module_path.parent)
    ):
        return None
    for candidate in module_path.parents:
        if candidate.name.casefold() not in {"site-packages", "dist-packages"}:
            continue
        try:
            relative = module_path.relative_to(candidate)
        except ValueError:
            return None
        if relative.parts[:2] != ("project_memory_hub", "integration"):
            return None
        return candidate
    return None


def _repository_identity(repository_root: Path) -> tuple[int, int] | None:
    if not _secure_directory_ancestors(repository_root):
        return None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(repository_root, flags)
    except OSError:
        return None
    try:
        if not _descriptor_matches_path(descriptor, repository_root, kind="directory"):
            return None
        metadata = os.fstat(descriptor)
        return metadata.st_dev, metadata.st_ino
    finally:
        os.close(descriptor)


def _secure_directory_ancestors(path: Path) -> bool:
    if not _is_canonical_absolute(path):
        return False
    allowed_owners = {0, os.getuid()}
    for candidate in (path, *path.parents):
        try:
            metadata = candidate.lstat()
        except OSError:
            return False
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid not in allowed_owners
            or metadata.st_mode & 0o022
        ):
            return False
    return True


def _descriptor_matches_path(
    descriptor: int,
    path: Path | str,
    *,
    kind: Literal["directory", "file"],
    dir_fd: int | None = None,
) -> bool:
    try:
        descriptor_metadata = os.fstat(descriptor)
        path_metadata = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    expected_type = stat.S_ISDIR if kind == "directory" else stat.S_ISREG
    return (
        expected_type(descriptor_metadata.st_mode)
        and expected_type(path_metadata.st_mode)
        and descriptor_metadata.st_uid == os.getuid()
        and path_metadata.st_uid == os.getuid()
        and descriptor_metadata.st_mode & 0o022 == 0
        and path_metadata.st_mode & 0o022 == 0
        and descriptor_metadata.st_dev == path_metadata.st_dev
        and descriptor_metadata.st_ino == path_metadata.st_ino
    )


def _parse_local_time(value: str) -> tuple[int, int]:
    parts = value.split(":")
    if len(parts) != 2 or any(len(part) != 2 or not part.isdigit() for part in parts):
        raise ValueError("local_time must use HH:MM")
    hour, minute = map(int, parts)
    if hour > 23 or minute > 59:
        raise ValueError("local_time must use HH:MM")
    return hour, minute


def _daily_rrule(timezone: str, local_time: str) -> str:
    hour, minute = _parse_local_time(local_time)
    compact_time = f"{hour:02d}{minute:02d}00"
    return (
        f"DTSTART;TZID={timezone}:19700101T{compact_time}\n"
        f"RRULE:FREQ=DAILY;BYHOUR={hour};BYMINUTE={minute}"
    )


def _stable_path(value: Path, *, kind: Literal["directory", "executable"]) -> Path:
    path = Path(value)
    if not _safe_path_text(path) or not path.is_absolute():
        raise ValueError(f"{kind} path must be absolute")
    if ".worktrees" in path.parts:
        raise ValueError(f"{kind} path must not be inside .worktrees")
    if not _is_canonical_absolute(path):
        raise ValueError(f"{kind} path must be canonical")
    if kind == "directory":
        metadata = path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
        ):
            raise ValueError("project_root must be a safe existing directory")
    elif _discover_launcher(path) is None:
        raise ValueError("launcher must be a safe existing executable")
    return path
