import io
import hashlib
import json
import os
import re
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import IO, BinaryIO, Iterator

from typing_extensions import Buffer

from project_memory_hub.security.json_limits import JsonNestingError, load_json_bounded


class UnsafeArchiveError(ValueError):
    """Raised when an archive does not satisfy the safe-reading contract."""


@dataclass(frozen=True, slots=True)
class JsonArchiveSnapshot:
    sha256: str
    members: tuple[tuple[str, object], ...]
    validated_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_members: int = 20_000
    max_member_bytes: int = 256 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_compression_ratio: int = 100

    def __post_init__(self) -> None:
        values = (
            self.max_members,
            self.max_member_bytes,
            self.max_total_bytes,
            self.max_compression_ratio,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("invalid archive limits")


_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
_MAX_STREAM_CHUNK = 64 * 1024


@dataclass(slots=True)
class _StreamTotals:
    bytes_read: int = 0


@dataclass(frozen=True, slots=True)
class _ArchiveIdentity:
    device: int
    inode: int
    size: int
    modified_ns: int
    changed_ns: int


class _LimitedMemberReader(io.RawIOBase):
    def __init__(
        self,
        source: IO[bytes],
        limits: ArchiveLimits,
        totals: _StreamTotals,
    ) -> None:
        super().__init__()
        self._source = source
        self._limits = limits
        self._totals = totals
        self._member_bytes = 0

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: Buffer, /) -> int:
        view = memoryview(buffer)
        requested = min(len(view), _MAX_STREAM_CHUNK)
        chunk = self._source.read(requested)
        if not chunk:
            return 0

        size = len(chunk)
        self._member_bytes += size
        self._totals.bytes_read += size
        if self._member_bytes > self._limits.max_member_bytes:
            raise UnsafeArchiveError("streamed member size limit exceeded")
        if self._totals.bytes_read > self._limits.max_total_bytes:
            raise UnsafeArchiveError("streamed total size limit exceeded")

        view[:size] = chunk
        return size


def _normalize_member_name(name: str) -> tuple[str, bool]:
    if not isinstance(name, str):
        raise UnsafeArchiveError("invalid member path")
    if "\x00" in name:
        raise UnsafeArchiveError("nul member path")

    normalized = name.replace("\\", "/")
    if not normalized:
        raise UnsafeArchiveError("empty member path")
    if normalized.startswith("/"):
        raise UnsafeArchiveError("absolute member path")
    if _WINDOWS_DRIVE_PATTERN.match(normalized):
        raise UnsafeArchiveError("windows drive member path")

    is_directory = normalized.endswith("/")
    path_without_directory_marker = normalized[:-1] if is_directory else normalized
    if not path_without_directory_marker:
        raise UnsafeArchiveError("empty member path")

    components = path_without_directory_marker.split("/")
    if any(component == "" for component in components):
        raise UnsafeArchiveError("empty member path component")
    if any(component == "." for component in components):
        raise UnsafeArchiveError("dot member path")
    if any(component == ".." for component in components):
        raise UnsafeArchiveError("path traversal")

    result = "/".join(components)
    if is_directory:
        result += "/"
    return result, is_directory


def _open_regular_archive(path: Path) -> BinaryIO:
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise UnsafeArchiveError("archive file rejected")
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
    except UnsafeArchiveError:
        raise
    except (OSError, TypeError, ValueError):
        raise UnsafeArchiveError("archive file rejected") from None

    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UnsafeArchiveError("archive file rejected")
        return os.fdopen(descriptor, "rb", closefd=True)
    except UnsafeArchiveError:
        os.close(descriptor)
        raise
    except (OSError, TypeError, ValueError):
        os.close(descriptor)
        raise UnsafeArchiveError("archive file rejected") from None


def _file_type(info: zipfile.ZipInfo) -> int:
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(unix_mode)


def _archive_identity(archive_file: BinaryIO) -> _ArchiveIdentity:
    try:
        metadata = os.fstat(archive_file.fileno())
        return _ArchiveIdentity(
            device=metadata.st_dev,
            inode=metadata.st_ino,
            size=metadata.st_size,
            modified_ns=metadata.st_mtime_ns,
            changed_ns=metadata.st_ctime_ns,
        )
    except (OSError, TypeError, ValueError):
        raise UnsafeArchiveError("archive changed during read") from None


def _assert_archive_identity(archive_file: BinaryIO, expected: _ArchiveIdentity) -> None:
    if _archive_identity(archive_file) != expected:
        raise UnsafeArchiveError("archive changed during read")


class SafeZipReader:
    def __init__(
        self,
        path: Path,
        limits: ArchiveLimits = ArchiveLimits(),
    ) -> None:
        if not isinstance(path, Path):
            raise TypeError("archive path must be a pathlib Path")
        if not isinstance(limits, ArchiveLimits):
            raise TypeError("limits must be ArchiveLimits")
        self._path = path
        self._limits = limits

    def _validated_members(
        self, archive: zipfile.ZipFile
    ) -> dict[str, tuple[zipfile.ZipInfo, bool]]:
        infos = archive.infolist()
        if len(infos) > self._limits.max_members:
            raise UnsafeArchiveError("member count limit exceeded")

        members: dict[str, tuple[zipfile.ZipInfo, bool]] = {}
        path_identities: set[str] = set()
        total_size = 0
        for info in infos:
            original_name = getattr(info, "orig_filename", info.filename)
            normalized, name_is_directory = _normalize_member_name(original_name)
            path_identity = normalized[:-1] if name_is_directory else normalized
            if path_identity in path_identities:
                raise UnsafeArchiveError("duplicate member path")
            path_identities.add(path_identity)
            if info.flag_bits & 0x1:
                raise UnsafeArchiveError("encrypted member")

            file_type = _file_type(info)
            if file_type == stat.S_IFLNK:
                raise UnsafeArchiveError("symbolic link member")
            if file_type not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UnsafeArchiveError("special file member")

            try:
                file_size = info.file_size
                compressed_size = info.compress_size
                if (
                    type(file_size) is not int
                    or type(compressed_size) is not int
                    or file_size < 0
                    or compressed_size < 0
                ):
                    raise UnsafeArchiveError("invalid member metadata")
                if file_size > 0 and compressed_size == 0:
                    raise UnsafeArchiveError("invalid compressed size")
                if file_size > self._limits.max_member_bytes:
                    raise UnsafeArchiveError("member size limit exceeded")
                total_size += file_size
                if total_size > self._limits.max_total_bytes:
                    raise UnsafeArchiveError("total size limit exceeded")
                if file_size > self._limits.max_compression_ratio * compressed_size:
                    raise UnsafeArchiveError("compression ratio limit exceeded")
            except UnsafeArchiveError:
                raise
            except (ArithmeticError, TypeError, ValueError):
                raise UnsafeArchiveError("invalid member metadata") from None

            is_directory = name_is_directory or file_type == stat.S_IFDIR
            members[normalized] = (info, is_directory)
        return members

    def _selected_names(self, requested: set[str]) -> tuple[list[str], _ArchiveIdentity]:
        try:
            with _open_regular_archive(self._path) as archive_file:
                identity = _archive_identity(archive_file)
                with zipfile.ZipFile(archive_file, mode="r", allowZip64=True) as archive:
                    members = self._validated_members(archive)
                    _assert_archive_identity(archive_file, identity)
                    selected = sorted(
                        name for name in requested.intersection(members) if not members[name][1]
                    )
                _assert_archive_identity(archive_file, identity)
                return selected, identity
        except UnsafeArchiveError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
            raise UnsafeArchiveError("invalid archive") from None
        except (OSError, RuntimeError, ValueError):
            raise UnsafeArchiveError("invalid archive") from None

    def _read_json_member(
        self,
        name: str,
        totals: _StreamTotals,
        expected_identity: _ArchiveIdentity,
    ) -> object:
        try:
            with _open_regular_archive(self._path) as archive_file:
                _assert_archive_identity(archive_file, expected_identity)
                with zipfile.ZipFile(archive_file, mode="r", allowZip64=True) as archive:
                    members = self._validated_members(archive)
                    _assert_archive_identity(archive_file, expected_identity)
                    if name not in members or members[name][1]:
                        raise UnsafeArchiveError("archive changed during read")
                    info = members[name][0]
                    try:
                        with archive.open(info, mode="r") as member_stream:
                            limited = _LimitedMemberReader(member_stream, self._limits, totals)
                            with io.BufferedReader(
                                limited, buffer_size=_MAX_STREAM_CHUNK
                            ) as buffered:
                                with io.TextIOWrapper(
                                    buffered, encoding="utf-8", errors="strict"
                                ) as text_stream:
                                    value = load_json_bounded(text_stream)
                        _assert_archive_identity(archive_file, expected_identity)
                        return value
                    except UnsafeArchiveError:
                        raise
                    except UnicodeDecodeError:
                        raise UnsafeArchiveError("invalid utf-8 json member") from None
                    except (json.JSONDecodeError, JsonNestingError, RecursionError):
                        raise UnsafeArchiveError("invalid json member") from None
                    except RuntimeError:
                        raise UnsafeArchiveError("encrypted member") from None
                    except (zipfile.BadZipFile, EOFError, OSError, ValueError):
                        raise UnsafeArchiveError("invalid archive") from None
        except UnsafeArchiveError:
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
            raise UnsafeArchiveError("invalid archive") from None
        except (OSError, RuntimeError, ValueError):
            raise UnsafeArchiveError("invalid archive") from None

    def read_json_members(self, names: set[str]) -> Iterator[tuple[str, object]]:
        requested: set[str] = set()
        try:
            for name in names:
                normalized, is_directory = _normalize_member_name(name)
                if not is_directory:
                    requested.add(normalized)
        except UnsafeArchiveError:
            raise
        except (TypeError, ValueError):
            raise UnsafeArchiveError("invalid requested member names") from None

        totals = _StreamTotals()
        selected_names, identity = self._selected_names(requested)
        for name in selected_names:
            value = self._read_json_member(name, totals, identity)
            yield name, value

    def read_json_snapshot(
        self,
        names: set[str],
        *,
        name_pattern: re.Pattern[str] | None = None,
    ) -> JsonArchiveSnapshot:
        requested: set[str] = set()
        try:
            for name in names:
                normalized, is_directory = _normalize_member_name(name)
                if not is_directory:
                    requested.add(normalized)
        except UnsafeArchiveError:
            raise
        except (TypeError, ValueError):
            raise UnsafeArchiveError("invalid requested member names") from None
        if name_pattern is not None and not isinstance(name_pattern, re.Pattern):
            raise UnsafeArchiveError("invalid requested member pattern")

        totals = _StreamTotals()
        try:
            with _open_regular_archive(self._path) as archive_file:
                identity = _archive_identity(archive_file)
                digest = _sha256_file_descriptor(archive_file.fileno(), identity.size)
                _assert_archive_identity(archive_file, identity)
                values: list[tuple[str, object]] = []
                with zipfile.ZipFile(archive_file, mode="r", allowZip64=True) as archive:
                    members = self._validated_members(archive)
                    validated_names = tuple(
                        sorted(name for name, details in members.items() if not details[1])
                    )
                    selected = sorted(
                        name
                        for name in validated_names
                        if name in requested
                        or (name_pattern is not None and name_pattern.fullmatch(name) is not None)
                    )
                    for name in selected:
                        info = members[name][0]
                        with archive.open(info, mode="r") as member_stream:
                            limited = _LimitedMemberReader(member_stream, self._limits, totals)
                            with io.BufferedReader(
                                limited, buffer_size=_MAX_STREAM_CHUNK
                            ) as buffered:
                                with io.TextIOWrapper(
                                    buffered, encoding="utf-8", errors="strict"
                                ) as text_stream:
                                    values.append((name, load_json_bounded(text_stream)))
                        _assert_archive_identity(archive_file, identity)
                _assert_archive_identity(archive_file, identity)
                return JsonArchiveSnapshot(digest, tuple(values), validated_names)
        except UnsafeArchiveError:
            raise
        except UnicodeDecodeError:
            raise UnsafeArchiveError("invalid utf-8 json member") from None
        except (json.JSONDecodeError, JsonNestingError, RecursionError):
            raise UnsafeArchiveError("invalid json member") from None
        except (zipfile.BadZipFile, zipfile.LargeZipFile, EOFError):
            raise UnsafeArchiveError("invalid archive") from None
        except RuntimeError:
            raise UnsafeArchiveError("encrypted member") from None
        except (OSError, TypeError, ValueError):
            raise UnsafeArchiveError("invalid archive") from None


def _sha256_file_descriptor(descriptor: int, size: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        expected = min(_MAX_STREAM_CHUNK, size - offset)
        chunk = os.pread(descriptor, expected, offset)
        if len(chunk) != expected:
            raise UnsafeArchiveError("archive changed during read")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()
