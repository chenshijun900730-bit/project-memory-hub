import errno
import fcntl
import hashlib
import json
import os
import secrets
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from project_memory_hub.domain import SourceAgent


class ConfigConflictError(RuntimeError):
    """The config changed after the caller inspected it."""


class ConfigCommitUncertainError(RuntimeError):
    """Replacement completed, but its path or durability could not be confirmed."""

    replacement_completed = True
    durability_confirmed = False


class ConfigIOError(RuntimeError):
    """A non-policy filesystem failure prevented a config operation."""


@dataclass(frozen=True, slots=True)
class ConfigRevision:
    digest: str


_MAX_CONFIG_BYTES = 1024 * 1024
_POLICY_ERRNOS = frozenset(
    {
        errno.EACCES,
        errno.ENOENT,
        errno.ENOTDIR,
        errno.EPERM,
        errno.ELOOP,
    }
)


@dataclass(frozen=True, slots=True)
class AppConfig:
    project_roots: tuple[Path, ...]
    enabled_sources: tuple[SourceAgent, ...]
    inactive_days: int
    max_recall_tokens: int
    daily_reconcile_time: str
    setup_completed: bool = True
    codex_project_id: str | None = None
    improvement_repository_root: Path | None = None
    improvement_verification_commands: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_roots", tuple(map(Path, self.project_roots)))
        object.__setattr__(
            self,
            "enabled_sources",
            tuple(map(SourceAgent, self.enabled_sources)),
        )
        if self.improvement_repository_root is not None:
            object.__setattr__(
                self,
                "improvement_repository_root",
                Path(self.improvement_repository_root),
            )
        object.__setattr__(
            self,
            "improvement_verification_commands",
            tuple(tuple(command) for command in self.improvement_verification_commands),
        )
        if self.codex_project_id is not None:
            project_id = self.codex_project_id
            if (
                not project_id.strip()
                or project_id != project_id.strip()
                or len(project_id) > 512
                or any(ord(character) < 32 or ord(character) == 127 for character in project_id)
            ):
                raise ValueError("codex_project_id is invalid")
        if not isinstance(self.setup_completed, bool):
            raise ValueError("setup_completed is invalid")

    @classmethod
    def defaults(cls, home: Path) -> "AppConfig":
        return cls(
            project_roots=(
                home / "Documents",
                home / "Code x",
                home / "Workbuddy",
            ),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
            setup_completed=False,
        )


@dataclass(frozen=True, slots=True)
class ConfigManager:
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))

    def load(self) -> AppConfig:
        document = self._read_document()
        return _config_from_values(tomllib.loads(document.decode("utf-8")))

    def load_with_revision(self) -> tuple[AppConfig, ConfigRevision]:
        document = self._read_document()
        return (
            _config_from_values(tomllib.loads(document.decode("utf-8"))),
            ConfigRevision(hashlib.sha256(document).hexdigest()),
        )

    def _read_document(self) -> bytes:
        with _ConfigParent(self.path.parent) as parent_fd:
            document, _ = _read_config_target(parent_fd, self.path.name)
        if document is None:
            raise FileNotFoundError(self.path)
        return document

    def save(
        self,
        config: AppConfig,
        *,
        expected_revision: ConfigRevision | None = None,
    ) -> None:
        document = _serialize(config)
        encoded_document = document.encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _ConfigParent(self.path.parent) as parent_fd:
            with _ConfigWriteLock(parent_fd):
                _require_parent_unchanged(parent_fd, self.path.parent)
                current, snapshot = _read_config_target(parent_fd, self.path.name)
                if expected_revision is not None:
                    if current is None:
                        raise ConfigConflictError("config changed")
                    if hashlib.sha256(current).hexdigest() != expected_revision.digest:
                        raise ConfigConflictError("config changed")
                if current == encoded_document:
                    if snapshot is not None:
                        _tighten_matching_config(parent_fd, self.path.name, snapshot)
                    return
                temporary = f".{self.path.name}.{secrets.token_hex(16)}.tmp"
                replacement_completed = False
                try:
                    _write_config_file(parent_fd, temporary, encoded_document)
                    latest, latest_snapshot = _read_config_target(parent_fd, self.path.name)
                    if latest != current or latest_snapshot != snapshot:
                        raise ConfigConflictError("config changed")
                    _require_parent_unchanged(parent_fd, self.path.parent)
                    os.replace(
                        temporary,
                        self.path.name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                    )
                    replacement_completed = True
                    temporary = ""
                    _require_parent_unchanged(parent_fd, self.path.parent)
                    os.fsync(parent_fd)
                except ConfigConflictError:
                    raise
                except PermissionError:
                    if replacement_completed:
                        raise ConfigCommitUncertainError("config commit is uncertain") from None
                    raise
                except OSError as error:
                    if replacement_completed:
                        raise ConfigCommitUncertainError("config commit is uncertain") from None
                    _raise_config_os_error(error, policy_message="config write failed")
                finally:
                    if temporary:
                        try:
                            os.unlink(temporary, dir_fd=parent_fd)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            pass


def _config_from_values(values: dict[str, Any]) -> AppConfig:
    return AppConfig(
        project_roots=tuple(Path(value) for value in values["project_roots"]),
        enabled_sources=tuple(SourceAgent(value) for value in values["enabled_sources"]),
        inactive_days=values["inactive_days"],
        max_recall_tokens=values["max_recall_tokens"],
        daily_reconcile_time=values["daily_reconcile_time"],
        setup_completed=values.get("setup_completed", True),
        codex_project_id=values.get("codex_project_id"),
        improvement_repository_root=(
            Path(values["improvement_repository_root"])
            if "improvement_repository_root" in values
            else None
        ),
        improvement_verification_commands=_verification_commands(
            values.get("improvement_verification_commands", [])
        ),
    )


def _serialize(config: AppConfig) -> str:
    roots = ", ".join(_toml_string(str(path)) for path in config.project_roots)
    sources = ", ".join(_toml_string(source.value) for source in config.enabled_sources)
    lines = [
        f"project_roots = [{roots}]",
        f"enabled_sources = [{sources}]",
        f"inactive_days = {config.inactive_days}",
        f"max_recall_tokens = {config.max_recall_tokens}",
        f"daily_reconcile_time = {_toml_string(config.daily_reconcile_time)}",
        f"setup_completed = {'true' if config.setup_completed else 'false'}",
    ]
    if config.codex_project_id is not None:
        lines.append(f"codex_project_id = {_toml_string(config.codex_project_id)}")
    if config.improvement_repository_root is not None:
        lines.append(
            "improvement_repository_root = " + _toml_string(str(config.improvement_repository_root))
        )
    if config.improvement_verification_commands:
        commands = ", ".join(
            "[" + ", ".join(_toml_string(argument) for argument in command) + "]"
            for command in config.improvement_verification_commands
        )
        lines.append(f"improvement_verification_commands = [{commands}]")
    lines.append("")
    return "\n".join(lines)


def _verification_commands(value: object) -> tuple[tuple[str, ...], ...]:
    if not isinstance(value, list):
        raise ValueError("improvement verification commands are invalid")
    commands: list[tuple[str, ...]] = []
    for command in value:
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(argument, str) for argument in command)
        ):
            raise ValueError("improvement verification commands are invalid")
        commands.append(tuple(command))
    return tuple(commands)


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


class _ConfigParent:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._descriptor = -1

    def __enter__(self) -> int:
        if not self._path.is_absolute():
            raise PermissionError("config parent rejected")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(self._path.anchor, flags)
            for component in self._path.parts[1:]:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
            metadata = os.fstat(descriptor)
            current = self._path.lstat()
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            _raise_config_os_error(error, policy_message="config parent rejected")
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_mode & 0o022
            or _directory_identity(metadata) != _directory_identity(current)
        ):
            os.close(descriptor)
            raise PermissionError("config parent rejected")
        self._descriptor = descriptor
        return descriptor

    def __exit__(self, *_exc_info: object) -> None:
        if self._descriptor >= 0:
            os.close(self._descriptor)
            self._descriptor = -1


class _ConfigWriteLock:
    def __init__(self, parent_fd: int) -> None:
        self._parent_fd = parent_fd
        self._file_locked = False

    def __enter__(self) -> None:
        try:
            fcntl.flock(self._parent_fd, fcntl.LOCK_EX)
        except OSError as error:
            _raise_config_os_error(error, policy_message="config lock failed")
        self._file_locked = True

    def __exit__(self, *_exc_info: object) -> None:
        if self._file_locked:
            try:
                fcntl.flock(self._parent_fd, fcntl.LOCK_UN)
            except OSError:
                # Closing the parent descriptor immediately after this
                # context also releases the advisory lock.
                pass
            self._file_locked = False


def _read_config_target(
    parent_fd: int,
    name: str,
) -> tuple[bytes | None, tuple[int, ...] | None]:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None, None
    except OSError as error:
        _raise_config_os_error(error, policy_message="config file rejected")
    if (
        stat.S_ISLNK(before.st_mode)
        or not stat.S_ISREG(before.st_mode)
        or before.st_uid != os.getuid()
        or before.st_nlink != 1
        or before.st_mode & stat.S_IRUSR == 0
        or before.st_mode & 0o022
        or before.st_size < 0
        or before.st_size > _MAX_CONFIG_BYTES
    ):
        raise PermissionError("config file rejected")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        _raise_config_os_error(error, policy_message="config file rejected")
    try:
        opened = os.fstat(descriptor)
        identity = _file_identity(before)
        if _file_identity(opened) != identity:
            raise ConfigConflictError("config changed")
        document = bytearray()
        while len(document) <= _MAX_CONFIG_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, _MAX_CONFIG_BYTES + 1 - len(document)),
            )
            if not chunk:
                break
            document.extend(chunk)
        after = os.fstat(descriptor)
    except OSError as error:
        _raise_config_os_error(error, policy_message="config file rejected")
    finally:
        os.close(descriptor)
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise ConfigConflictError("config changed") from None
    except OSError as error:
        _raise_config_os_error(error, policy_message="config file rejected")
    if (
        len(document) > _MAX_CONFIG_BYTES
        or len(document) != before.st_size
        or _file_identity(after) != identity
        or _file_identity(current) != identity
    ):
        raise ConfigConflictError("config changed")
    return bytes(document), identity


def _write_config_file(parent_fd: int, name: str, document: bytes) -> None:
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
        os.fchmod(descriptor, 0o600)
        written = 0
        while written < len(document):
            count = os.write(descriptor, document[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
        os.fsync(descriptor)
    except OSError as error:
        _raise_config_os_error(error, policy_message="config write failed")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _tighten_matching_config(parent_fd: int, name: str, identity: tuple[int, ...]) -> None:
    current_mode = stat.S_IMODE(identity[2])
    private_mode = current_mode & 0o600
    if current_mode == private_mode:
        return
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as error:
        _raise_config_os_error(error, policy_message="config file rejected")
    try:
        before = os.fstat(descriptor)
        if _file_identity(before) != identity:
            raise ConfigConflictError("config changed")
        os.fchmod(descriptor, private_mode)
        after = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat.S_IMODE(after.st_mode) != private_mode or _file_identity(current) != _file_identity(
            after
        ):
            raise ConfigConflictError("config changed")
    except ConfigConflictError:
        raise
    except OSError as error:
        _raise_config_os_error(error, policy_message="config permission update failed")
    finally:
        os.close(descriptor)


def _require_parent_unchanged(parent_fd: int, parent: Path) -> None:
    try:
        opened = os.fstat(parent_fd)
        current = parent.lstat()
    except OSError as error:
        _raise_config_os_error(error, policy_message="config parent changed")
    if stat.S_ISLNK(current.st_mode) or _directory_identity(opened) != _directory_identity(current):
        raise PermissionError("config parent changed")


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_uid,
    )


def _raise_config_os_error(error: OSError, *, policy_message: str) -> NoReturn:
    if isinstance(error, PermissionError) or error.errno in _POLICY_ERRNOS:
        raise PermissionError(policy_message) from None
    raise ConfigIOError("config I/O failed") from None
