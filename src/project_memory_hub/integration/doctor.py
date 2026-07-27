from __future__ import annotations

import configparser
import json
import os
import sqlite3
import stat
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from importlib import resources
from pathlib import Path
from typing import Literal

from project_memory_hub.config import AppConfig
from project_memory_hub.domain import SourceAgent
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.storage.database import strict_utc_epoch_us


DoctorStatus = Literal["pass", "warn", "fail"]
ExternalStatus = Callable[[], str]
GraphifyStatus = Callable[[Path], str]
_PASS_REMEDIATION = "No action required."
_AVAILABLE_SOURCES = frozenset({SourceAgent.CODEX, SourceAgent.CHATGPT})
_MAX_RETRY_AGE = timedelta(days=7)
_WARN_RETRY_AGE = timedelta(days=1)
_MAX_RECONCILE_AGE = timedelta(days=2)
_MAX_GRAPHIFY_HOOK_BYTES = 128 * 1024
_MAX_RECONCILE_STATE_BYTES = 64 * 1024
_GRAPHIFY_HOOK_MARKERS = {
    "post-commit": "# graphify-hook-start",
    "post-checkout": "# graphify-checkout-hook-start",
}


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: DoctorStatus
    code: str
    remediation: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    status: DoctorStatus
    checks: tuple[DoctorCheck, ...]

    def check(self, name: str) -> DoctorCheck:
        for item in self.checks:
            if item.name == name:
                return item
        raise KeyError(name)

    def as_dict(self) -> dict[str, object]:
        return {
            "checks": [asdict(check) for check in self.checks],
            "status": self.status,
        }

    def as_json(self) -> str:
        return json.dumps(
            self.as_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )


class DoctorService:
    def __init__(
        self,
        *,
        paths: RuntimePaths,
        config_path: Path,
        config: AppConfig | None,
        codex_sessions_path: Path,
        repository_root: Path,
        agents_status: ExternalStatus,
        automation_status: ExternalStatus,
        graphify_status: GraphifyStatus,
        codex_sessions_optional: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._paths = paths
        self._config_path = Path(config_path)
        self._config = config
        self._codex_sessions = Path(codex_sessions_path)
        self._repository_root = Path(repository_root)
        self._agents_status = agents_status
        self._automation_status = automation_status
        self._graphify_status = graphify_status
        self._codex_sessions_optional = codex_sessions_optional
        self._now = now or (lambda: datetime.now(timezone.utc))

    def run(self) -> DoctorReport:
        checks = (
            self._safe_check("runtime_permissions", self._runtime_permissions),
            self._safe_check("database_quick_check", self._database_quick_check),
            self._safe_check("migration_version", self._migration_version),
            self._safe_check("fts5", self._fts5),
            self._safe_check("codex_sessions", self._codex_sessions_health),
            self._safe_check("chatgpt_imports", self._chatgpt_imports_health),
            self._safe_check("enabled_adapters", self._enabled_adapters),
            self._safe_check("retry_queue", self._retry_queue),
            self._safe_check("last_reconcile", self._last_reconcile),
            self._safe_check("managed_agents", self._managed_agents),
            self._safe_check("graphify_hooks", self._graphify_hooks),
            self._safe_check("codex_automation", self._codex_automation),
        )
        status: DoctorStatus = (
            "fail"
            if any(check.status == "fail" for check in checks)
            else "warn"
            if any(check.status == "warn" for check in checks)
            else "pass"
        )
        return DoctorReport(status=status, checks=checks)

    @staticmethod
    def _safe_check(
        name: str,
        operation: Callable[[], DoctorCheck],
    ) -> DoctorCheck:
        try:
            result = operation()
            if result.name != name:
                raise ValueError("doctor check name mismatch")
            return result
        except Exception:
            return _check(
                name,
                "fail",
                f"{name}_check_failed",
                "Review the local installation and run doctor again.",
            )

    def _runtime_permissions(self) -> DoctorCheck:
        directories = (
            self._paths.root,
            self._paths.imports,
            self._paths.retries,
            self._paths.backups,
            self._paths.logs,
        )
        files = (self._config_path, self._paths.database)
        if not all(_private_directory(path) for path in directories) or not all(
            _private_file(path) for path in files
        ):
            return _check(
                "runtime_permissions",
                "fail",
                "runtime_permissions_invalid",
                "Restore owner-only runtime permissions and retry.",
            )
        sidecars = tuple(
            Path(f"{self._paths.database}{suffix}") for suffix in ("-wal", "-shm", "-journal")
        )
        if any(path.exists() and not _private_file(path) for path in sidecars):
            return _check(
                "runtime_permissions",
                "fail",
                "database_sidecar_permissions_invalid",
                "Restore owner-only database permissions and retry.",
            )
        return _passed("runtime_permissions", "runtime_permissions_ok")

    def _database_quick_check(self) -> DoctorCheck:
        with self._database_connection() as connection:
            rows = connection.execute("pragma quick_check(1)").fetchall()
        if not rows or any(tuple(row) != ("ok",) for row in rows):
            return _check(
                "database_quick_check",
                "fail",
                "database_integrity_failed",
                "Restore a verified private backup before continuing.",
            )
        return _passed("database_quick_check", "database_integrity_ok")

    def _migration_version(self) -> DoctorCheck:
        expected = _migration_versions()
        with self._database_connection() as connection:
            observed = tuple(
                int(row[0])
                for row in connection.execute(
                    "select version from schema_migrations order by version limit ?",
                    (len(expected) + 1,),
                ).fetchall()
            )
        if observed != expected:
            return _check(
                "migration_version",
                "fail",
                "schema_version_mismatch",
                "Use the matching application version and rerun migrations safely.",
            )
        return _passed("migration_version", "schema_version_current")

    def _fts5(self) -> DoctorCheck:
        with self._database_connection() as connection:
            connection.execute("pragma schema_version").fetchone()
            available = connection.execute(
                "select sqlite_compileoption_used('ENABLE_FTS5')"
            ).fetchone()[0]
            tables = connection.execute(
                "select count(*) from sqlite_master "
                "where type = 'table' and name = 'project_facts_fts'"
            ).fetchone()[0]
        if int(available) != 1 or int(tables) != 1:
            return _check(
                "fts5",
                "fail",
                "fts5_unavailable",
                "Install a Python build with SQLite FTS5 support.",
            )
        return _passed("fts5", "fts5_available")

    def _codex_sessions_health(self) -> DoctorCheck:
        try:
            self._codex_sessions.lstat()
        except FileNotFoundError:
            if self._codex_sessions_optional:
                return _check(
                    "codex_sessions",
                    "warn",
                    "codex_sessions_missing",
                    "Open Codex once before reconciling Codex task history.",
                )
        except OSError:
            pass
        if not _readable_directory(self._codex_sessions):
            return _check(
                "codex_sessions",
                "fail",
                "codex_sessions_unreadable",
                "Grant Codex session-folder read access and retry.",
            )
        return _passed("codex_sessions", "codex_sessions_readable")

    def _chatgpt_imports_health(self) -> DoctorCheck:
        if not _private_directory(self._paths.imports):
            return _check(
                "chatgpt_imports",
                "fail",
                "chatgpt_imports_unavailable",
                "Restore the private ChatGPT import directory and retry.",
            )
        return _passed("chatgpt_imports", "chatgpt_imports_ready")

    def _enabled_adapters(self) -> DoctorCheck:
        if self._config is None:
            return _check(
                "enabled_adapters",
                "fail",
                "config_unavailable",
                "Create or restore a valid private configuration.",
            )
        enabled = tuple(self._config.enabled_sources)
        if not enabled or any(source not in _AVAILABLE_SOURCES for source in enabled):
            return _check(
                "enabled_adapters",
                "fail",
                "enabled_adapter_unavailable",
                "Disable unavailable sources or install a supported adapter.",
            )
        return _passed("enabled_adapters", "enabled_adapters_ready")

    def _retry_queue(self) -> DoctorCheck:
        with self._database_connection() as connection:
            row = connection.execute("select count(*), min(created_at) from retry_items").fetchone()
        count = int(row[0])
        if count == 0:
            return _passed("retry_queue", "retry_queue_empty")
        oldest = _timestamp(row[1])
        age = self._utc_now() - oldest
        if age < timedelta(0):
            return _check(
                "retry_queue",
                "fail",
                "retry_timestamp_invalid",
                "Review the local clock and retry metadata.",
            )
        if age > _MAX_RETRY_AGE:
            return _check(
                "retry_queue",
                "fail",
                "retry_items_stale",
                "Run reconcile and inspect only redacted retry health codes.",
            )
        if age > _WARN_RETRY_AGE:
            return _check(
                "retry_queue",
                "warn",
                "retry_items_aging",
                "Run reconcile when convenient.",
            )
        return _passed("retry_queue", "retry_queue_recent")

    def _last_reconcile(self) -> DoctorCheck:
        with self._database_connection() as connection:
            row = connection.execute(
                "select length(value_json), substr(value_json, 1, ?) "
                "from app_state where name = 'last_reconcile_success'",
                (_MAX_RECONCILE_STATE_BYTES + 1,),
            ).fetchone()
        if row is None:
            return _check(
                "last_reconcile",
                "warn",
                "reconcile_not_recorded",
                "Run reconcile once after installation.",
            )
        try:
            if (
                type(row[0]) is not int
                or not 0 <= row[0] <= _MAX_RECONCILE_STATE_BYTES
                or not isinstance(row[1], str)
            ):
                raise ValueError("reconcile state too large")
            document = json.loads(row[1])
            timestamp = _timestamp(document["timestamp"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return _check(
                "last_reconcile",
                "fail",
                "reconcile_state_invalid",
                "Run a verified reconcile to replace invalid health state.",
            )
        age = self._utc_now() - timestamp
        if age < timedelta(minutes=-5):
            return _check(
                "last_reconcile",
                "fail",
                "reconcile_timestamp_future",
                "Correct the local clock before running reconcile.",
            )
        if age > _MAX_RECONCILE_AGE:
            return _check(
                "last_reconcile",
                "warn",
                "reconcile_overdue",
                "Run reconcile or restore the daily automation.",
            )
        return _passed("last_reconcile", "reconcile_recent")

    def _managed_agents(self) -> DoctorCheck:
        status = self._agents_status()
        if status == "current":
            return _passed("managed_agents", "managed_agents_current")
        if status == "missing":
            return _check(
                "managed_agents",
                "warn",
                "managed_agents_missing",
                "Install the reviewed managed AGENTS block.",
            )
        return _check(
            "managed_agents",
            "fail",
            "managed_agents_drifted",
            "Review and reinstall only the managed AGENTS block.",
        )

    def _graphify_hooks(self) -> DoctorCheck:
        status = self._graphify_status(self._repository_root)
        if status == "installed":
            return _passed("graphify_hooks", "graphify_hooks_installed")
        if status == "missing":
            return _check(
                "graphify_hooks",
                "warn",
                "graphify_hooks_missing",
                "Install the repository Graphify hooks.",
            )
        return _check(
            "graphify_hooks",
            "fail",
            "graphify_hooks_unavailable",
            "Verify Graphify locally and rerun doctor.",
        )

    def _codex_automation(self) -> DoctorCheck:
        status = self._automation_status()
        if status == "current":
            return _passed("codex_automation", "codex_automation_current")
        if status == "missing":
            return _check(
                "codex_automation",
                "warn",
                "codex_automation_missing",
                "Create the reviewed automation through the Codex host tool.",
            )
        return _check(
            "codex_automation",
            "fail",
            "codex_automation_drifted",
            "Review and update the automation through the Codex host tool.",
        )

    @contextmanager
    def _database_connection(self) -> Iterator[sqlite3.Connection]:
        if not _private_file(self._paths.database):
            raise PermissionError("database unavailable")
        for suffix in ("-wal", "-journal"):
            sidecar = Path(f"{self._paths.database}{suffix}")
            try:
                metadata = sidecar.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != 0:
                raise RuntimeError("database is not quiescent")
        uri = f"{self._paths.database.resolve(strict=True).as_uri()}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, timeout=1)
        try:
            connection.execute("pragma query_only=on")
            connection.execute("pragma busy_timeout=1000")
            yield connection
        finally:
            connection.close()

    def _utc_now(self) -> datetime:
        value = self._now()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("doctor clock invalid")
        return value.astimezone(timezone.utc)


def inspect_graphify_hooks(
    repository_root: Path,
    *,
    executable: Path | None = None,
    timeout_seconds: float = 5.0,
    expected_repository_identity: tuple[int, int] | None = None,
) -> str:
    """Inspect Graphify hook markers without executing repository or tool code."""
    del executable
    if (
        not isinstance(repository_root, Path)
        or not repository_root.is_absolute()
        or isinstance(timeout_seconds, bool)
        or not 0 < timeout_seconds <= 30
    ):
        return "unavailable"
    try:
        repository_metadata = repository_root.lstat()
        if (
            stat.S_ISLNK(repository_metadata.st_mode)
            or not stat.S_ISDIR(repository_metadata.st_mode)
            or repository_root.resolve(strict=True) != repository_root
        ):
            return "unavailable"
    except (OSError, RuntimeError):
        return "unavailable"

    pinned_repository = _pin_secure_repository(
        repository_root,
        expected_identity=expected_repository_identity,
    )
    if pinned_repository is None:
        return "unavailable"

    try:
        return _inspect_graphify_hook_files(pinned_repository)
    finally:
        pinned_repository.close()


def _inspect_graphify_hook_files(repository: _PinnedRepository) -> str:
    hook_path = _graphify_hook_path(repository)
    if isinstance(hook_path, str):
        return hook_path
    directory_status, hooks_fd = _open_graphify_directory(
        repository.descriptor,
        hook_path,
    )
    if hooks_fd is None:
        return directory_status
    pinned_hooks: list[_PinnedGraphifyFile] = []
    try:
        for name, marker in _GRAPHIFY_HOOK_MARKERS.items():
            file_status, pinned_hook = _pin_graphify_file(hooks_fd, name)
            if file_status != "available" or pinned_hook is None:
                return "missing" if file_status == "missing" else "unavailable"
            pinned_hooks.append(pinned_hook)
            try:
                document = pinned_hook.payload.decode("utf-8")
            except UnicodeDecodeError:
                return "unavailable"
            if marker not in document:
                return "missing"
        if (
            _graphify_hook_path(repository) != hook_path
            or not _graphify_directory_matches(
                repository.descriptor,
                hook_path,
                hooks_fd,
            )
            or not all(hook.matches(hooks_fd) for hook in pinned_hooks)
            or not repository.matches()
        ):
            return "unavailable"
        return "installed"
    finally:
        for hook in pinned_hooks:
            hook.close()
        os.close(hooks_fd)


def _graphify_hook_path(repository: _PinnedRepository) -> tuple[str, ...] | str:
    git_status, git_fd = _open_graphify_directory(repository.descriptor, (".git",))
    if git_fd is None:
        return git_status
    try:
        config_status, payload = _read_graphify_file(git_fd, "config")
    finally:
        os.close(git_fd)
    if config_status == "unavailable":
        return "unavailable"
    if config_status == "missing" or payload is None:
        return (".git", "hooks")
    try:
        document = payload.decode("utf-8")
        parser = configparser.RawConfigParser(
            interpolation=None,
            strict=True,
            allow_no_value=True,
        )
        parser.read_string(document)
    except (UnicodeDecodeError, configparser.Error):
        return "unavailable"
    if any(section.casefold().startswith("include") for section in parser.sections()):
        return "unavailable"
    core_sections = tuple(section for section in parser.sections() if section.casefold() == "core")
    if len(core_sections) > 1:
        return "unavailable"
    if not core_sections or not parser.has_option(core_sections[0], "hookspath"):
        return (".git", "hooks")
    raw_path = parser.get(core_sections[0], "hookspath", raw=True)
    if raw_path is None:
        return "unavailable"
    parsed_path = _parse_graphify_hooks_path(raw_path, repository.path)
    return parsed_path if parsed_path is not None else "unavailable"


def _parse_graphify_hooks_path(value: str, repository_root: Path) -> tuple[str, ...] | None:
    value = value.strip()
    if value.startswith('"'):
        try:
            decoded = json.loads(value)
        except (UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(decoded, str):
            return None
        value = decoded
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError:
        return None
    if (
        not 0 < len(encoded) <= 8192
        or value.startswith("~")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        try:
            relative = candidate.relative_to(repository_root)
        except ValueError:
            return None
    else:
        relative = candidate
    components = relative.parts
    if any(component in {"", ".", ".."} for component in components):
        return None
    if components and components[-1] == "_":
        components = components[:-1]
    return components


def _open_graphify_directory(
    root_fd: int,
    components: tuple[str, ...],
) -> tuple[Literal["available", "missing", "unavailable"], int | None]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.dup(root_fd)
    except OSError:
        return "unavailable", None
    try:
        for component in components:
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                return "missing", None
            except OSError:
                return "unavailable", None
            if not _safe_graphify_directory(
                next_descriptor,
                component,
                dir_fd=descriptor,
            ):
                os.close(next_descriptor)
                return "unavailable", None
            os.close(descriptor)
            descriptor = next_descriptor
        result = descriptor
        descriptor = -1
        return "available", result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_graphify_file(
    parent_fd: int,
    name: str,
) -> tuple[Literal["available", "missing", "unavailable"], bytes | None]:
    status, pinned = _pin_graphify_file(parent_fd, name)
    if pinned is None:
        return status, None
    try:
        return "available", pinned.payload
    finally:
        pinned.close()


def _pin_graphify_file(
    parent_fd: int,
    name: str,
) -> tuple[Literal["available", "missing", "unavailable"], _PinnedGraphifyFile | None]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unavailable", None
    try:
        before = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _safe_graphify_file(before, current) or before.st_size > _MAX_GRAPHIFY_HOOK_BYTES:
            return "unavailable", None
        document = bytearray()
        while True:
            chunk = os.read(descriptor, min(8192, _MAX_GRAPHIFY_HOOK_BYTES + 1 - len(document)))
            if not chunk:
                break
            document.extend(chunk)
            if len(document) > _MAX_GRAPHIFY_HOOK_BYTES:
                return "unavailable", None
        after = os.fstat(descriptor)
        current_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if _graphify_file_identity(before) != _graphify_file_identity(
            after
        ) or _graphify_file_identity(before) != _graphify_file_identity(current_after):
            return "unavailable", None
        pinned = _PinnedGraphifyFile(
            descriptor=descriptor,
            name=name,
            identity=_graphify_file_identity(before),
            payload=bytes(document),
        )
        descriptor = -1
        return "available", pinned
    except OSError:
        return "unavailable", None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _graphify_directory_matches(
    root_fd: int,
    components: tuple[str, ...],
    descriptor: int,
) -> bool:
    try:
        expected = os.fstat(descriptor)
    except OSError:
        return False
    status, current_fd = _open_graphify_directory(root_fd, components)
    if status != "available" or current_fd is None:
        return False
    try:
        current = os.fstat(current_fd)
        return (
            expected.st_dev == current.st_dev
            and expected.st_ino == current.st_ino
            and expected.st_mode == current.st_mode
            and expected.st_uid == current.st_uid
        )
    finally:
        os.close(current_fd)


@dataclass(slots=True)
class _PinnedGraphifyFile:
    descriptor: int
    name: str
    identity: tuple[int, int, int, int, int, int, int, int]
    payload: bytes

    def matches(self, parent_fd: int) -> bool:
        try:
            opened = os.fstat(self.descriptor)
            current = os.stat(self.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError:
            return False
        return (
            _graphify_file_identity(opened) == self.identity
            and _graphify_file_identity(current) == self.identity
        )

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


@dataclass(slots=True)
class _PinnedRepository:
    path: Path
    descriptor: int
    identity: tuple[int, int]

    def matches(self) -> bool:
        try:
            opened = os.fstat(self.descriptor)
        except OSError:
            return False
        if not stat.S_ISDIR(opened.st_mode) or (opened.st_dev, opened.st_ino) != self.identity:
            return False
        current = _pin_secure_repository(self.path, expected_identity=self.identity)
        if current is None:
            return False
        current.close()
        return True

    def close(self) -> None:
        if self.descriptor >= 0:
            os.close(self.descriptor)
            self.descriptor = -1


def _pin_secure_repository(
    path: Path,
    *,
    expected_identity: tuple[int, int] | None,
) -> _PinnedRepository | None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(path.anchor, directory_flags)
        if not _safe_graphify_directory(descriptor, path.anchor):
            raise OSError("unsafe repository ancestor")
        for component in path.parts[1:]:
            next_descriptor = os.open(component, directory_flags, dir_fd=descriptor)
            if not _safe_graphify_directory(
                next_descriptor,
                component,
                dir_fd=descriptor,
            ):
                os.close(next_descriptor)
                raise OSError("unsafe repository ancestor")
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if expected_identity is not None and identity != expected_identity:
            raise OSError("repository identity changed")
        return _PinnedRepository(path=path, descriptor=descriptor, identity=identity)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        return None


def _safe_graphify_directory(
    descriptor: int,
    path: Path | str,
    *,
    dir_fd: int | None = None,
) -> bool:
    try:
        opened = os.fstat(descriptor)
        current = os.stat(path, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        stat.S_ISDIR(opened.st_mode)
        and stat.S_ISDIR(current.st_mode)
        and opened.st_uid == current.st_uid
        and opened.st_uid in {0, os.getuid()}
        and opened.st_mode & 0o022 == 0
        and current.st_mode & 0o022 == 0
        and opened.st_dev == current.st_dev
        and opened.st_ino == current.st_ino
    )


def _safe_graphify_file(opened: os.stat_result, current: os.stat_result) -> bool:
    return (
        stat.S_ISREG(opened.st_mode)
        and stat.S_ISREG(current.st_mode)
        and opened.st_uid == current.st_uid == os.getuid()
        and opened.st_nlink == current.st_nlink == 1
        and opened.st_mode & 0o022 == 0
        and current.st_mode & 0o022 == 0
        and opened.st_dev == current.st_dev
        and opened.st_ino == current.st_ino
    )


def _graphify_file_identity(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _migration_versions() -> tuple[int, ...]:
    directory = resources.files("project_memory_hub.storage").joinpath("migrations")
    versions = []
    for item in directory.iterdir():
        prefix, separator, _suffix = item.name.partition("_")
        if separator and prefix.isdigit() and item.name.endswith(".sql"):
            versions.append(int(prefix))
    return tuple(sorted(versions))


def _private_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == 0o700
    )


def _private_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and metadata.st_nlink == 1
        and stat.S_IMODE(metadata.st_mode) == 0o600
    )


def _readable_directory(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and os.access(path, os.R_OK | os.X_OK)
    )


def _timestamp(value: object) -> datetime:
    epoch_us = strict_utc_epoch_us(value)
    if epoch_us is None:
        raise ValueError("timestamp invalid")
    return datetime.fromtimestamp(epoch_us / 1_000_000, tz=timezone.utc)


def _passed(name: str, code: str) -> DoctorCheck:
    return _check(name, "pass", code, _PASS_REMEDIATION)


def _check(
    name: str,
    status: DoctorStatus,
    code: str,
    remediation: str,
) -> DoctorCheck:
    return DoctorCheck(name=name, status=status, code=code, remediation=remediation)
