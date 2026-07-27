from __future__ import annotations

import hashlib
import html
import io
import json
import os
import re
import stat
import struct
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ElementTree
import zlib
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import NoReturn
from uuid import UUID

from project_memory_hub.demo.runtime import DEMO_MARKER_DOCUMENT, OUTPUT_MARKER_NAME
from project_memory_hub.demo.seed import SYNTHETIC_UUIDS


_UUID = re.compile(
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
)
_TOKEN_PATTERNS = (
    re.compile(r"(?i)Bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|password)\s*[:=]\s*\S{8,}"),
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])"),
    re.compile(
        r"(?i)"
        r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"
    ),
    re.compile(r"(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"(?i)AWS_SECRET_ACCESS_KEY\s*[:=]\s*[A-Za-z0-9/+=]{32,}"),
    re.compile(r"(?i)xox[bpa rs]-[A-Za-z0-9-]{20,}".replace(" ", "")),
    re.compile(r"(?i)xapp-[A-Za-z0-9-]{20,}"),
)
_PRINTABLE_ASCII_RUN = re.compile(rb"[\x20-\x7e]+")
_TEXT_SUFFIXES = frozenset({".html", ".json", ".svg", ".txt"})
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_CORE_CHUNKS = frozenset({b"IHDR", b"IDAT", b"IEND"})
_WEBP_METADATA_CHUNKS = frozenset({b"EXIF", b"XMP ", b"ICCP"})
_WEBP_PIXEL_CHUNKS = frozenset({b"VP8 ", b"VP8L"})
_DENYLIST_MAX_BYTES = 64 * 1024
_DENYLIST_MAX_LINES = 1_000
_DENYLIST_MAX_TERM_CHARS = 512


class PrivacyPolicyError(ValueError):
    """A private policy input was unsafe or malformed."""


class PrivacyViolation(ValueError):
    """A public asset failed a stable non-disclosing privacy rule."""

    def __init__(self, code: str, asset_name: str) -> None:
        self.code = code
        self.asset_name = _safe_asset_name(asset_name)
        super().__init__(f"privacy scan rejected: {code}")


@dataclass(frozen=True, slots=True)
class PrivacyLimits:
    max_files: int = 64
    max_file_bytes: int = 16 * 1024 * 1024
    max_total_bytes: int = 48 * 1024 * 1024
    max_decoded_chars: int = 2 * 1024 * 1024
    max_metadata_bytes: int = 64 * 1024
    max_pixels: int = 8_000_000

    def __post_init__(self) -> None:
        for value in (
            self.max_files,
            self.max_file_bytes,
            self.max_total_bytes,
            self.max_decoded_chars,
            self.max_metadata_bytes,
            self.max_pixels,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("privacy limits must be positive integers")


@dataclass(frozen=True, slots=True)
class PrivacyPolicy:
    synthetic_uuid_allowlist: frozenset[UUID]
    _home_prefixes: tuple[str, ...] = field(repr=False)
    _forbidden_terms: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class ScannedAsset:
    asset_name: str
    size_bytes: int
    media_type: str
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True, slots=True)
class PrivacyReport:
    files: tuple[ScannedAsset, ...]
    total_bytes: int
    ocr_performed: bool = False


def build_privacy_policy(
    *,
    repository_root: Path,
    denylist_path: Path | None = None,
    home_prefixes: tuple[Path, ...] | None = None,
) -> PrivacyPolicy:
    repository = _absolute_path(repository_root, code="denylist_rejected")
    _reject_symlink_components(repository)
    homes = home_prefixes if home_prefixes is not None else (Path.home(),)
    normalized_homes = tuple(
        dict.fromkeys(
            _normalize_text(str(_absolute_path(path, code="home_prefix_rejected")))
            for path in homes
        )
    )
    terms = () if denylist_path is None else _read_external_denylist(repository, denylist_path)
    return PrivacyPolicy(
        synthetic_uuid_allowlist=SYNTHETIC_UUIDS,
        _home_prefixes=normalized_homes,
        _forbidden_terms=terms,
    )


def scan_text(
    text: str,
    policy: PrivacyPolicy,
    *,
    asset_name: str,
    limits: PrivacyLimits | None = None,
) -> None:
    selected_limits = limits or PrivacyLimits()
    if not isinstance(text, str):
        raise TypeError("privacy text must be a string")
    try:
        encoded_size = len(text.encode("utf-8", errors="strict"))
    except UnicodeError:
        _violate("invalid_text", asset_name)
    if len(text) > selected_limits.max_decoded_chars:
        _violate("decoded_text_too_large", asset_name)
    if encoded_size > selected_limits.max_file_bytes:
        _violate("file_too_large", asset_name)

    for variant in _text_variants(text):
        folded = variant.casefold()
        for prefix in policy._home_prefixes:
            if prefix and prefix.casefold() in folded:
                _violate("home_prefix", asset_name)
        for term in policy._forbidden_terms:
            if term.casefold() in folded:
                _violate("forbidden_term", asset_name)
        if any(pattern.search(variant) is not None for pattern in _TOKEN_PATTERNS):
            _violate("token_like", asset_name)
        for match in _UUID.finditer(variant):
            if (match.start() > 0 and variant[match.start() - 1] in "0123456789abcdefABCDEF") or (
                match.end() < len(variant) and variant[match.end()] in "0123456789abcdefABCDEF"
            ):
                _violate("unknown_uuid", asset_name)
            try:
                identifier = UUID(match.group(0))
            except ValueError:
                _violate("unknown_uuid", asset_name)
            if identifier not in policy.synthetic_uuid_allowlist:
                _violate("unknown_uuid", asset_name)


def scan_dom_receipt(
    receipt: object,
    policy: PrivacyPolicy,
    *,
    asset_name: str,
    limits: PrivacyLimits | None = None,
) -> None:
    canonical, _digest = canonical_dom_receipt(receipt)
    try:
        text = canonical.decode("utf-8", errors="strict")
    except UnicodeError:
        _violate("invalid_dom_receipt", asset_name)
    scan_text(text, policy, asset_name=asset_name, limits=limits)


def canonical_dom_receipt(receipt: object) -> tuple[bytes, str]:
    _validate_json_value(receipt, depth=0, nodes=[0])
    try:
        canonical = (
            json.dumps(
                receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeError, ValueError) as error:
        raise PrivacyPolicyError("dom_receipt_rejected") from error
    return canonical, hashlib.sha256(canonical).hexdigest()


def scan_file(
    path: Path,
    policy: PrivacyPolicy,
    *,
    asset_name: str,
    limits: PrivacyLimits | None = None,
) -> ScannedAsset:
    selected_limits = limits or PrivacyLimits()
    document = _read_regular_file(path, selected_limits, asset_name=asset_name)
    return scan_document(
        document,
        policy,
        asset_name=asset_name,
        limits=selected_limits,
    )


def scan_document(
    document: bytes,
    policy: PrivacyPolicy,
    *,
    asset_name: str,
    limits: PrivacyLimits | None = None,
) -> ScannedAsset:
    """Scan one already-safe byte snapshot without reopening its filesystem path."""
    selected_limits = limits or PrivacyLimits()
    if not isinstance(document, bytes):
        raise TypeError("privacy document must be bytes")
    if len(document) > selected_limits.max_file_bytes:
        _violate("file_too_large", asset_name)
    if Path(asset_name).name == OUTPUT_MARKER_NAME:
        if document != DEMO_MARKER_DOCUMENT:
            _violate("invalid_demo_marker", asset_name)
        return ScannedAsset(asset_name, len(document), "text/plain")
    suffix = Path(asset_name).suffix.casefold()
    if suffix == ".png":
        width, height = _scan_png(
            document,
            policy,
            selected_limits,
            asset_name=asset_name,
        )
        return ScannedAsset(asset_name, len(document), "image/png", width, height)
    if suffix == ".webp":
        width, height = _scan_webp(
            document,
            policy,
            selected_limits,
            asset_name=asset_name,
        )
        return ScannedAsset(asset_name, len(document), "image/webp", width, height)
    if suffix not in _TEXT_SUFFIXES and Path(asset_name).name != OUTPUT_MARKER_NAME:
        _violate("unsupported_asset", asset_name)
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeError:
        _violate("invalid_text", asset_name)
    if len(text) > selected_limits.max_decoded_chars:
        _violate("decoded_text_too_large", asset_name)
    if suffix == ".json":
        try:
            json.loads(text)
        except (json.JSONDecodeError, RecursionError):
            _violate("invalid_json", asset_name)
    if suffix == ".html":
        _scan_html(text, policy, asset_name=asset_name, limits=selected_limits)
    elif suffix == ".svg":
        _scan_svg(text, policy, asset_name=asset_name, limits=selected_limits)
    else:
        scan_text(text, policy, asset_name=asset_name, limits=selected_limits)
    media_type = {
        ".html": "text/html",
        ".json": "application/json",
        ".svg": "image/svg+xml",
        ".txt": "text/plain",
    }.get(suffix, "text/plain")
    return ScannedAsset(asset_name, len(document), media_type)


def scan_asset_directory(
    root: Path,
    policy: PrivacyPolicy,
    *,
    limits: PrivacyLimits | None = None,
) -> PrivacyReport:
    selected_limits = limits or PrivacyLimits()
    selected_root = Path(root)
    try:
        root_metadata = selected_root.lstat()
    except OSError:
        _violate("asset_root_rejected", "<root>")
    if stat.S_ISLNK(root_metadata.st_mode) or not stat.S_ISDIR(root_metadata.st_mode):
        _violate("asset_root_rejected", "<root>")

    paths: list[Path] = []
    try:
        for path in selected_root.rglob("*"):
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                _violate("asset_symlink", path.name)
            if stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                _violate("asset_type_rejected", path.name)
            paths.append(path)
            if len(paths) > selected_limits.max_files:
                _violate("file_count_exceeded", "<root>")
    except PrivacyViolation:
        raise
    except OSError:
        _violate("asset_root_rejected", "<root>")

    files: list[ScannedAsset] = []
    total_bytes = 0
    for path in sorted(paths, key=lambda item: item.relative_to(selected_root).as_posix()):
        relative = path.relative_to(selected_root).as_posix()
        scanned = scan_file(
            path,
            policy,
            asset_name=relative,
            limits=selected_limits,
        )
        total_bytes += scanned.size_bytes
        if total_bytes > selected_limits.max_total_bytes:
            _violate("total_bytes_exceeded", "<root>")
        files.append(scanned)
    return PrivacyReport(tuple(files), total_bytes)


class _VisibleHTMLParser(HTMLParser):
    _HIDDEN_TAGS = frozenset({"script", "style", "template", "noscript"})

    def __init__(self, max_chars: int) -> None:
        super().__init__(convert_charrefs=True)
        self._hidden_depth = 0
        self._max_chars = max_chars
        self._chars = 0
        self.text: list[str] = []
        self.attributes: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered in self._HIDDEN_TAGS:
            self._hidden_depth += 1
            return
        if self._hidden_depth:
            return
        attr_map = {name.casefold(): value for name, value in attrs if value is not None}
        input_type = attr_map.get("type", "").casefold()
        for name, value in attr_map.items():
            if name == "value" and input_type in {"hidden", "password"}:
                continue
            if name in {"alt", "title", "value"} or name.startswith("aria-"):
                self._append(self.attributes, value)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in self._HIDDEN_TAGS and self._hidden_depth:
            self._hidden_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden_depth:
            self._append(self.text, data)

    def _append(self, target: list[str], value: str) -> None:
        self._chars += len(value)
        if self._chars > self._max_chars:
            raise OverflowError
        target.append(value)


def _scan_html(
    text: str,
    policy: PrivacyPolicy,
    *,
    asset_name: str,
    limits: PrivacyLimits,
) -> None:
    scan_text(text, policy, asset_name=asset_name, limits=limits)
    parser = _VisibleHTMLParser(limits.max_decoded_chars)
    try:
        parser.feed(text)
        parser.close()
    except (OverflowError, ValueError):
        _violate("invalid_html", asset_name)
    for extracted in (
        "".join(parser.text),
        " ".join(parser.text),
        " ".join(parser.attributes),
    ):
        scan_text(extracted, policy, asset_name=asset_name, limits=limits)


def _scan_svg(
    text: str,
    policy: PrivacyPolicy,
    *,
    asset_name: str,
    limits: PrivacyLimits,
) -> None:
    if re.search(r"(?is)<!DOCTYPE|<!ENTITY", text):
        _violate("svg_active_content", asset_name)
    scan_text(text, policy, asset_name=asset_name, limits=limits)
    try:
        root = ElementTree.fromstring(text)
    except (ElementTree.ParseError, RecursionError):
        _violate("invalid_svg", asset_name)
    visible_parts: list[str] = []
    attribute_parts: list[str] = []
    total_chars = 0
    for element in root.iter():
        local_name = element.tag.rsplit("}", 1)[-1].casefold()
        if local_name in {"script", "foreignobject"}:
            _violate("svg_active_content", asset_name)
        if local_name == "style" and element.text:
            _validate_svg_css(element.text, asset_name=asset_name)
        if element.text:
            visible_parts.append(element.text)
            total_chars += len(element.text)
        if element.tail:
            visible_parts.append(element.tail)
            total_chars += len(element.tail)
        for name, value in element.attrib.items():
            attribute_name = name.rsplit("}", 1)[-1].casefold()
            if attribute_name.startswith("on"):
                _violate("svg_active_content", asset_name)
            if attribute_name == "style":
                _validate_svg_css(value, asset_name=asset_name)
            attribute_parts.append(value)
            total_chars += len(value)
            if attribute_name == "href" and not value.startswith("#"):
                _violate("svg_external_reference", asset_name)
        if total_chars > limits.max_decoded_chars:
            _violate("decoded_text_too_large", asset_name)
    for extracted in (
        "".join(visible_parts),
        " ".join(visible_parts),
        " ".join(attribute_parts),
    ):
        scan_text(extracted, policy, asset_name=asset_name, limits=limits)


def _validate_svg_css(value: str, *, asset_name: str) -> None:
    normalized = _normalize_text(value).casefold()
    if (
        "\\" in normalized
        or "/*" in normalized
        or "*/" in normalized
        or "@" in normalized
        or any(
            token in normalized
            for token in (
                "url(",
                "expression(",
                "javascript:",
                "data:",
                "behavior",
                "-moz-binding",
            )
        )
    ):
        _violate("svg_active_content", asset_name)


def _scan_png(
    document: bytes,
    policy: PrivacyPolicy,
    limits: PrivacyLimits,
    *,
    asset_name: str,
) -> tuple[int, int]:
    if not document.startswith(_PNG_SIGNATURE):
        _violate("invalid_png", asset_name)
    offset = len(_PNG_SIGNATURE)
    chunks: list[bytes] = []
    width = height = 0
    invalid_iend_length = False
    while offset < len(document):
        if offset + 12 > len(document):
            _violate("invalid_png", asset_name)
        length = struct.unpack(">I", document[offset : offset + 4])[0]
        chunk_type = document[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        if chunk_end > len(document):
            _violate("invalid_png", asset_name)
        payload = document[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", document[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type)
        actual_crc = zlib.crc32(payload, actual_crc) & 0xFFFFFFFF
        if expected_crc != actual_crc:
            _violate("invalid_png", asset_name)
        if chunk_type not in _PNG_CORE_CHUNKS and length > limits.max_metadata_bytes:
            _violate("metadata_too_large", asset_name)
        if chunk_type not in _PNG_CORE_CHUNKS:
            _violate("png_metadata", asset_name)
        if chunk_type == b"IEND" and length != 0:
            invalid_iend_length = True
        if chunk_type == b"IHDR":
            if chunks or length != 13:
                _violate("invalid_png", asset_name)
            width, height = struct.unpack(">II", payload[:8])
        chunks.append(chunk_type)
        offset = chunk_end
        if chunk_type == b"IEND":
            break
    if (
        not chunks
        or chunks[0] != b"IHDR"
        or b"IDAT" not in chunks
        or chunks[-1] != b"IEND"
        or offset != len(document)
    ):
        _violate("invalid_png", asset_name)
    _scan_raster_ascii_runs(
        document,
        policy,
        asset_name=asset_name,
        limits=limits,
    )
    if invalid_iend_length:
        _violate("invalid_png", asset_name)
    _validate_dimensions(width, height, limits, asset_name=asset_name)
    _verify_raster(document, "PNG", width, height, limits, asset_name=asset_name)
    return width, height


def _scan_webp(
    document: bytes,
    policy: PrivacyPolicy,
    limits: PrivacyLimits,
    *,
    asset_name: str,
) -> tuple[int, int]:
    if len(document) < 20 or document[:4] != b"RIFF" or document[8:12] != b"WEBP":
        _violate("invalid_webp", asset_name)
    declared_size = struct.unpack("<I", document[4:8])[0]
    if declared_size != len(document) - 8:
        _violate("invalid_webp", asset_name)
    offset = 12
    chunks: list[bytes] = []
    metadata_bytes = 0
    while offset < len(document):
        if offset + 8 > len(document):
            _violate("invalid_webp", asset_name)
        chunk_type = document[offset : offset + 4]
        length = struct.unpack("<I", document[offset + 4 : offset + 8])[0]
        payload_end = offset + 8 + length
        padded_end = payload_end + (length % 2)
        if payload_end > len(document) or padded_end > len(document):
            _violate("invalid_webp", asset_name)
        if chunk_type in _WEBP_METADATA_CHUNKS:
            metadata_bytes += length
            if metadata_bytes > limits.max_metadata_bytes:
                _violate("metadata_too_large", asset_name)
        chunks.append(chunk_type)
        offset = padded_end
    if any(chunk in _WEBP_METADATA_CHUNKS for chunk in chunks):
        _violate("webp_metadata", asset_name)
    if len(chunks) != 1 or chunks[0] not in _WEBP_PIXEL_CHUNKS:
        _violate("invalid_webp", asset_name)
    _scan_raster_ascii_runs(
        document,
        policy,
        asset_name=asset_name,
        limits=limits,
    )
    width, height = _raster_dimensions(document, "WEBP", limits, asset_name=asset_name)
    return width, height


def _verify_raster(
    document: bytes,
    expected_format: str,
    expected_width: int,
    expected_height: int,
    limits: PrivacyLimits,
    *,
    asset_name: str,
) -> None:
    width, height = _raster_dimensions(document, expected_format, limits, asset_name=asset_name)
    if (width, height) != (expected_width, expected_height):
        _violate("raster_dimensions_changed", asset_name)


def _raster_dimensions(
    document: bytes,
    expected_format: str,
    limits: PrivacyLimits,
    *,
    asset_name: str,
) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError:
        _violate("invalid_raster", asset_name)
    try:
        with Image.open(io.BytesIO(document)) as candidate:
            if candidate.format != expected_format:
                _violate("invalid_raster", asset_name)
            width, height = candidate.size
            candidate.verify()
        _validate_dimensions(width, height, limits, asset_name=asset_name)
        with Image.open(io.BytesIO(document)) as decoded:
            decoded.load()
            if decoded.size != (width, height):
                _violate("invalid_raster", asset_name)
    except PrivacyViolation:
        raise
    except Image.DecompressionBombError:
        _violate("pixel_limit_exceeded", asset_name)
    except (OSError, RuntimeError, SyntaxError, ValueError):
        _violate("invalid_raster", asset_name)
    return width, height


def _scan_raster_ascii_runs(
    document: bytes,
    policy: PrivacyPolicy,
    *,
    asset_name: str,
    limits: PrivacyLimits,
) -> None:
    printable = b"\n".join(match.group(0) for match in _PRINTABLE_ASCII_RUN.finditer(document))
    scan_text(printable.decode("ascii"), policy, asset_name=asset_name, limits=limits)


def _validate_dimensions(
    width: int,
    height: int,
    limits: PrivacyLimits,
    *,
    asset_name: str,
) -> None:
    if width <= 0 or height <= 0 or width * height > limits.max_pixels:
        _violate("pixel_limit_exceeded", asset_name)


def _read_regular_file(path: Path, limits: PrivacyLimits, *, asset_name: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_nlink != 1:
            _violate("asset_type_rejected", asset_name)
        if stat.S_IMODE(before.st_mode) & 0o022:
            if Path(asset_name).name == OUTPUT_MARKER_NAME:
                _violate("invalid_demo_marker", asset_name)
            _violate("asset_type_rejected", asset_name)
        if before.st_size > limits.max_file_bytes:
            _violate("file_too_large", asset_name)
        document = bytearray()
        while len(document) <= limits.max_file_bytes:
            chunk = os.read(descriptor, min(64 * 1024, limits.max_file_bytes + 1 - len(document)))
            if not chunk:
                break
            document.extend(chunk)
        if len(document) > limits.max_file_bytes:
            _violate("file_too_large", asset_name)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mode,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mode,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or len(document) != before.st_size:
            _violate("asset_changed", asset_name)
        return bytes(document)
    except PrivacyViolation:
        raise
    except OSError:
        _violate("asset_read_failed", asset_name)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_external_denylist(repository: Path, path: Path) -> tuple[str, ...]:
    selected = _absolute_path(path, code="denylist_rejected")
    _reject_symlink_components(selected)
    if selected == repository or repository in selected.parents:
        raise PrivacyPolicyError("denylist_rejected")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = -1
    try:
        descriptor = os.open(selected, flags)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > _DENYLIST_MAX_BYTES
        ):
            raise PrivacyPolicyError("denylist_rejected")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise PrivacyPolicyError("denylist_rejected")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise PrivacyPolicyError("denylist_rejected")
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mode,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mode,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise PrivacyPolicyError("denylist_rejected")
        document = b"".join(chunks)
    except PrivacyPolicyError:
        raise
    except OSError as error:
        raise PrivacyPolicyError("denylist_rejected") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(document) > _DENYLIST_MAX_BYTES:
        raise PrivacyPolicyError("denylist_rejected")
    try:
        text = document.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise PrivacyPolicyError("denylist_rejected") from error
    lines = text.splitlines()
    if len(lines) > _DENYLIST_MAX_LINES:
        raise PrivacyPolicyError("denylist_rejected")
    terms: list[str] = []
    for line in lines:
        term = _normalize_text(line.strip())
        if not term:
            continue
        if len(term) > _DENYLIST_MAX_TERM_CHARS or any(ord(char) < 32 for char in term):
            raise PrivacyPolicyError("denylist_rejected")
        terms.append(term)
    normalized_terms = tuple(dict.fromkeys(terms))
    if not normalized_terms:
        raise PrivacyPolicyError("denylist_rejected")
    return normalized_terms


def _absolute_path(path: Path, *, code: str) -> Path:
    selected = Path(path).expanduser()
    if not selected.is_absolute():
        raise PrivacyPolicyError(code)
    return Path(os.path.abspath(selected))


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError as error:
            raise PrivacyPolicyError("denylist_rejected") from error
        except OSError as error:
            raise PrivacyPolicyError("denylist_rejected") from error
        if stat.S_ISLNK(metadata.st_mode):
            raise PrivacyPolicyError("denylist_rejected")


def _text_variants(text: str) -> tuple[str, ...]:
    variants: list[str] = []
    current = _normalize_text(text)
    for _index in range(3):
        if current not in variants:
            variants.append(current)
        decoded = _normalize_text(urllib.parse.unquote(html.unescape(current)))
        if decoded == current:
            break
        current = decoded
    return tuple(variants)


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value)


def _validate_json_value(value: object, *, depth: int, nodes: list[int]) -> None:
    nodes[0] += 1
    if depth > 32 or nodes[0] > 10_000:
        raise PrivacyPolicyError("dom_receipt_rejected")
    if value is None or isinstance(value, (str, bool, int)):
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, depth=depth + 1, nodes=nodes)
        return
    if type(value) is dict:
        for key, item in value.items():
            if not isinstance(key, str):
                raise PrivacyPolicyError("dom_receipt_rejected")
            _validate_json_value(item, depth=depth + 1, nodes=nodes)
        return
    raise PrivacyPolicyError("dom_receipt_rejected")


def _safe_asset_name(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        return "<asset>"
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        return "<asset>"
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return "<asset>"
    return candidate.as_posix()


def _violate(code: str, asset_name: str) -> NoReturn:
    raise PrivacyViolation(code, asset_name)
