import io
import hashlib
import logging
import os
import stat
import struct
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import BinaryIO

import pytest

import project_memory_hub.security.archive as archive_module
from project_memory_hub.security import (
    ArchiveLimits,
    SafeZipReader,
    UnsafeArchiveError,
)


def make_zip(
    path: Path,
    entries: list[tuple[str | zipfile.ZipInfo, bytes]],
    *,
    compression: int = zipfile.ZIP_STORED,
) -> Path:
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, payload in entries:
            archive.writestr(name, payload)
    return path


def patch_zip_filename(path: Path, old: bytes, new: bytes) -> None:
    assert len(old) == len(new)
    data = bytearray(path.read_bytes())
    replaced = 0
    for signature, name_length_offset, name_offset in (
        (b"PK\x03\x04", 26, 30),
        (b"PK\x01\x02", 28, 46),
    ):
        position = 0
        while (position := data.find(signature, position)) >= 0:
            name_length = struct.unpack_from("<H", data, position + name_length_offset)[0]
            start = position + name_offset
            end = start + name_length
            if bytes(data[start:end]) == old:
                data[start:end] = new
                replaced += 1
            position = end
    assert replaced == 2
    path.write_bytes(data)


def patch_first_entry_flags(path: Path, flag_bits: int) -> None:
    data = bytearray(path.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        position = data.index(signature)
        current = struct.unpack_from("<H", data, position + flag_offset)[0]
        struct.pack_into("<H", data, position + flag_offset, current | flag_bits)
    path.write_bytes(data)


def patch_first_entry_compressed_size(path: Path, size: int) -> None:
    data = bytearray(path.read_bytes())
    for signature, size_offset in ((b"PK\x03\x04", 18), (b"PK\x01\x02", 20)):
        position = data.index(signature)
        struct.pack_into("<I", data, position + size_offset, size)
    path.write_bytes(data)


def assert_rejected(
    path: Path,
    *,
    reason: str,
    names: set[str] | None = None,
    limits: ArchiveLimits = ArchiveLimits(),
    private_marker: str | None = None,
) -> None:
    with pytest.raises(UnsafeArchiveError) as exc_info:
        list(SafeZipReader(path, limits).read_json_members(names or {"item.json"}))

    assert str(exc_info.value) == reason
    if private_marker is not None:
        assert private_marker not in str(exc_info.value)


def test_archive_limits_have_exact_frozen_defaults() -> None:
    limits = ArchiveLimits()

    assert limits.max_members == 20_000
    assert limits.max_member_bytes == 256 * 1024 * 1024
    assert limits.max_total_bytes == 2 * 1024 * 1024 * 1024
    assert limits.max_compression_ratio == 100
    assert ArchiveLimits.__slots__ == (
        "max_members",
        "max_member_bytes",
        "max_total_bytes",
        "max_compression_ratio",
    )
    with pytest.raises(FrozenInstanceError):
        limits.max_members = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_members", 0),
        ("max_member_bytes", -1),
        ("max_total_bytes", 0),
        ("max_compression_ratio", 0),
        ("max_members", True),
        ("max_member_bytes", 1.5),
        ("max_total_bytes", "10"),
    ],
)
def test_archive_limits_reject_invalid_values(field: str, value: object) -> None:
    values: dict[str, object] = {
        "max_members": 1,
        "max_member_bytes": 1,
        "max_total_bytes": 1,
        "max_compression_ratio": 1,
    }
    values[field] = value

    with pytest.raises(ValueError, match="invalid archive limits"):
        ArchiveLimits(**values)  # type: ignore[arg-type]


def test_reader_selects_json_members_in_normalized_sorted_order(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "valid.zip",
        [
            ("z.json", b'{"order": 2}'),
            ("notes.txt", b"ignored"),
            ("a.json", b'{"order": 1}'),
            ("folder/", b""),
        ],
    )
    before = {item.name for item in tmp_path.iterdir()}

    result = list(SafeZipReader(archive).read_json_members({"missing.json", "z.json", "a.json"}))

    assert result == [("a.json", {"order": 1}), ("z.json", {"order": 2})]
    assert {item.name for item in tmp_path.iterdir()} == before


def test_json_snapshot_hash_and_members_share_one_validated_archive(
    tmp_path: Path,
) -> None:
    archive = make_zip(
        tmp_path / "snapshot-json.zip",
        [("a.json", b'{"order":1}'), ("b.json", b'{"order":2}')],
    )

    snapshot = SafeZipReader(archive).read_json_snapshot({"b.json", "a.json"})

    assert snapshot.sha256 == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert snapshot.members == (
        ("a.json", {"order": 1}),
        ("b.json", {"order": 2}),
    )
    assert snapshot.validated_names == ("a.json", "b.json")


@pytest.mark.parametrize(
    ("member_name", "reason"),
    [
        ("../item.json", "path traversal"),
        ("folder/../../item.json", "path traversal"),
        (r"folder\..\item.json", "path traversal"),
        ("/absolute/item.json", "absolute member path"),
        (r"C:\private\item.json", "windows drive member path"),
        (r"\\server\share\item.json", "absolute member path"),
        ("./item.json", "dot member path"),
        ("folder/./item.json", "dot member path"),
        ("folder//item.json", "empty member path component"),
    ],
)
def test_reader_rejects_unsafe_member_paths(tmp_path: Path, member_name: str, reason: str) -> None:
    private_marker = "synthetic-private-member"
    archive = make_zip(tmp_path / "hostile.zip", [(member_name, b'{"marker": "safe"}')])

    assert_rejected(
        archive,
        reason=reason,
        private_marker=private_marker,
    )


def test_reader_rejects_nul_and_empty_member_names(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "nul.zip", [("x", b"{}")])
    patch_zip_filename(archive, b"x", b"\x00")

    assert_rejected(archive, reason="nul member path")


def test_reader_rejects_duplicate_normalized_member_names(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "duplicate.zip",
        [("folder/item.json", b"{}"), (r"folder\item.json", b"{}")],
    )

    assert_rejected(archive, reason="duplicate member path")


def test_reviewer_reader_rejects_directory_file_alias(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "directory-alias.zip",
        [("folder", b"0"), ("folder/", b"")],
    )

    assert_rejected(archive, reason="duplicate member path")


def test_reader_rejects_hostile_unselected_member(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "unselected.zip",
        [("selected.json", b"{}"), ("../unselected.json", b"{}")],
    )

    assert_rejected(
        archive,
        reason="path traversal",
        names={"selected.json"},
    )


def test_reader_rejects_unsafe_requested_name_even_when_missing(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "valid.zip", [("item.json", b"{}")])

    assert_rejected(
        archive,
        reason="path traversal",
        names={"../missing.json"},
    )


def test_reader_rejects_encrypted_metadata_before_opening(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "encrypted.zip", [("item.json", b"{}")])
    patch_first_entry_flags(archive, 0x1)

    assert_rejected(archive, reason="encrypted member")


@pytest.mark.parametrize(
    ("file_type", "reason"),
    [
        (stat.S_IFLNK, "symbolic link member"),
        (stat.S_IFIFO, "special file member"),
    ],
)
def test_reader_rejects_symlink_and_special_file_metadata(
    tmp_path: Path, file_type: int, reason: str
) -> None:
    info = zipfile.ZipInfo("item.json")
    info.create_system = 3
    info.external_attr = (file_type | 0o600) << 16
    archive = make_zip(tmp_path / "special.zip", [(info, b"{}")])

    assert_rejected(archive, reason=reason)


def test_reader_enforces_declared_member_count(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "members.zip", [("a.json", b"{}"), ("b.json", b"{}")])

    assert_rejected(
        archive,
        reason="member count limit exceeded",
        limits=ArchiveLimits(max_members=1),
    )


def test_reader_enforces_declared_per_member_size(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "member-size.zip", [("item.json", b"123")])

    assert_rejected(
        archive,
        reason="member size limit exceeded",
        limits=ArchiveLimits(max_member_bytes=2),
    )


def test_reader_enforces_declared_total_size(tmp_path: Path) -> None:
    archive = make_zip(tmp_path / "total-size.zip", [("a.json", b"12"), ("b.json", b"34")])

    assert_rejected(
        archive,
        reason="total size limit exceeded",
        limits=ArchiveLimits(max_total_bytes=3),
    )


def test_reader_enforces_declared_compression_ratio(tmp_path: Path) -> None:
    archive = make_zip(
        tmp_path / "ratio.zip",
        [("item.json", b'"' + (b"a" * 4096) + b'"')],
        compression=zipfile.ZIP_DEFLATED,
    )

    assert_rejected(
        archive,
        reason="compression ratio limit exceeded",
        limits=ArchiveLimits(max_compression_ratio=2),
    )


def test_reviewer_reader_uses_exact_integer_compression_ratio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "exact-ratio.zip", [])
    compressed_size = 2**60
    ratio = 100
    info = zipfile.ZipInfo("large.bin")
    info.compress_size = compressed_size
    limits = ArchiveLimits(
        max_member_bytes=(ratio * compressed_size) + 1,
        max_total_bytes=(ratio * compressed_size) + 1,
        max_compression_ratio=ratio,
    )

    def controlled_infolist(self: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
        return [info]

    monkeypatch.setattr(archive_module.zipfile.ZipFile, "infolist", controlled_infolist)

    info.file_size = ratio * compressed_size
    assert list(SafeZipReader(archive, limits).read_json_members(set())) == []

    info.file_size += 1
    with pytest.raises(UnsafeArchiveError, match="^compression ratio limit exceeded$"):
        list(SafeZipReader(archive, limits).read_json_members(set()))


def test_reader_rejects_positive_size_with_zero_compressed_bytes(
    tmp_path: Path,
) -> None:
    archive = make_zip(tmp_path / "zero-compressed.zip", [("item.json", b"1")])
    patch_first_entry_compressed_size(archive, 0)

    assert_rejected(archive, reason="invalid compressed size")


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        (b"\xff", "invalid utf-8 json member"),
        (b"synthetic-invalid-json", "invalid json member"),
        (b"[" * 1100 + b"0" + b"]" * 1100, "invalid json member"),
    ],
)
def test_reader_converts_json_parser_failures_to_stable_errors(
    tmp_path: Path, payload: bytes, reason: str
) -> None:
    private_marker = "synthetic-invalid-json"
    archive = make_zip(tmp_path / "invalid-json.zip", [("item.json", payload)])

    assert_rejected(
        archive,
        reason=reason,
        private_marker=private_marker,
    )


def test_reader_converts_bad_zip_to_stable_non_leaking_error(tmp_path: Path) -> None:
    private_marker = "synthetic-invalid-archive"
    archive = tmp_path / f"{private_marker}.zip"
    archive.write_bytes(private_marker.encode())

    assert_rejected(
        archive,
        reason="invalid archive",
        private_marker=private_marker,
    )


def test_reader_rejects_archive_symlink_without_leaking_path(tmp_path: Path) -> None:
    target = make_zip(tmp_path / "target.zip", [("item.json", b"{}")])
    private_marker = "synthetic-private-link"
    link = tmp_path / private_marker
    link.symlink_to(target)

    assert_rejected(
        link,
        reason="archive file rejected",
        private_marker=private_marker,
    )


def test_reader_enforces_actual_streamed_member_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "dishonest-member.zip", [("item.json", b"0")])
    payload = b'{"value":"' + (b"x" * 32) + b'"}'

    def dishonest_open(
        self: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        mode: str = "r",
        pwd: bytes | None = None,
        *,
        force_zip64: bool = False,
    ) -> BinaryIO:
        return io.BytesIO(payload)

    monkeypatch.setattr(archive_module.zipfile.ZipFile, "open", dishonest_open)

    assert_rejected(
        archive,
        reason="streamed member size limit exceeded",
        limits=ArchiveLimits(max_member_bytes=16, max_total_bytes=100),
    )


def test_reader_enforces_actual_streamed_total_size(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "dishonest-total.zip", [("a.json", b"0"), ("b.json", b"0")])
    payloads = {"a.json": b'{"a":1}', "b.json": b'{"b":2}'}

    def dishonest_open(
        self: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        mode: str = "r",
        pwd: bytes | None = None,
        *,
        force_zip64: bool = False,
    ) -> BinaryIO:
        filename = name.filename if isinstance(name, zipfile.ZipInfo) else name
        return io.BytesIO(payloads[filename])

    monkeypatch.setattr(archive_module.zipfile.ZipFile, "open", dishonest_open)

    iterator = SafeZipReader(
        archive, ArchiveLimits(max_member_bytes=20, max_total_bytes=10)
    ).read_json_members({"a.json", "b.json"})
    assert next(iterator) == ("a.json", {"a": 1})
    with pytest.raises(UnsafeArchiveError, match="^streamed total size limit exceeded$"):
        next(iterator)


def test_reviewer_reader_rejects_archive_replacement_between_yields(
    tmp_path: Path,
) -> None:
    archive = make_zip(
        tmp_path / "snapshot.zip",
        [
            ("a.json", b'{"generation":1}'),
            ("b.json", b'{"generation":1}'),
        ],
    )
    replacement = make_zip(
        tmp_path / "replacement.zip",
        [
            ("a.json", b'{"generation":2}'),
            ("b.json", b'{"generation":2}'),
        ],
    )
    iterator = SafeZipReader(archive).read_json_members({"a.json", "b.json"})

    assert next(iterator) == ("a.json", {"generation": 1})
    os.replace(replacement, archive)

    with pytest.raises(UnsafeArchiveError, match="^archive changed during read$"):
        next(iterator)


def test_reader_never_requests_unbounded_or_oversized_stream_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "streaming.zip", [("item.json", b"0")])
    payload = b'"' + (b"x" * 70_000) + b'"'
    requested_sizes: list[int] = []

    class GuardedStream(io.BytesIO):
        def read(self, size: int = -1) -> bytes:
            requested_sizes.append(size)
            if size < 0 or size > 64 * 1024:
                raise AssertionError("unbounded stream read")
            return super().read(size)

        def read1(self, size: int = -1) -> bytes:
            requested_sizes.append(size)
            if size < 0 or size > 64 * 1024:
                raise AssertionError("unbounded stream read")
            return super().read(size)

    def guarded_open(
        self: zipfile.ZipFile,
        name: str | zipfile.ZipInfo,
        mode: str = "r",
        pwd: bytes | None = None,
        *,
        force_zip64: bool = False,
    ) -> BinaryIO:
        return GuardedStream(payload)

    monkeypatch.setattr(archive_module.zipfile.ZipFile, "open", guarded_open)

    result = list(
        SafeZipReader(
            archive,
            ArchiveLimits(max_member_bytes=100_000, max_total_bytes=100_000),
        ).read_json_members({"item.json"})
    )

    assert result == [("item.json", "x" * 70_000)]
    assert requested_sizes
    assert all(0 <= size <= 64 * 1024 for size in requested_sizes)


def test_reader_closes_all_resources_when_iteration_stops_early(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = make_zip(tmp_path / "resources.zip", [("a.json", b"{}"), ("b.json", b"{}")])
    real_zip_file = zipfile.ZipFile
    instances = []

    class TrackingZipFile(real_zip_file):
        def __init__(self, file: object, *args: object, **kwargs: object) -> None:
            self.source_file = file
            self.member_streams: list[BinaryIO] = []
            self.close_called = False
            super().__init__(file, *args, **kwargs)  # type: ignore[arg-type]
            instances.append(self)

        def open(self, *args: object, **kwargs: object) -> BinaryIO:
            member = super().open(*args, **kwargs)
            self.member_streams.append(member)
            return member

        def close(self) -> None:
            self.close_called = True
            super().close()

    monkeypatch.setattr(archive_module.zipfile, "ZipFile", TrackingZipFile)

    iterator = SafeZipReader(archive).read_json_members({"a.json", "b.json"})
    assert next(iterator) == ("a.json", {})

    assert instances
    assert any(instance.member_streams for instance in instances)
    assert all(instance.close_called for instance in instances)
    assert all(stream.closed for instance in instances for stream in instance.member_streams)
    assert all(getattr(instance.source_file, "closed", False) for instance in instances)
    iterator.close()


def test_reader_never_logs_or_prints_member_content(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    private_marker = "synthetic-private-content"
    archive = make_zip(tmp_path / "quiet.zip", [("item.json", private_marker.encode())])

    with pytest.raises(UnsafeArchiveError) as exc_info:
        list(SafeZipReader(archive).read_json_members({"item.json"}))
    captured = capsys.readouterr()

    assert private_marker not in str(exc_info.value)
    assert private_marker not in captured.out
    assert private_marker not in captured.err
    assert not caplog.records
