from __future__ import annotations

import json
import struct
import zlib
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin, UnidentifiedImageError

import project_memory_hub.demo.privacy as privacy_module
from project_memory_hub.demo.privacy import (
    PrivacyLimits,
    PrivacyPolicyError,
    PrivacyViolation,
    build_privacy_policy,
    canonical_dom_receipt,
    scan_asset_directory,
    scan_dom_receipt,
    scan_file,
    scan_text,
)
from project_memory_hub.demo.runtime import (
    DEMO_MARKER_DOCUMENT,
    OUTPUT_MARKER_NAME,
    prepare_demo_workspace,
)
from project_memory_hub.demo.seed import SYNTHETIC_UUIDS, seed_demo_database


def _policy(tmp_path: Path, *, private_term: str = "private phrase"):
    repository = tmp_path / "repository"
    repository.mkdir(exist_ok=True)
    denylist = tmp_path / "outside-private-denylist.txt"
    denylist.write_text(f"{private_term}\n", encoding="utf-8")
    denylist.chmod(0o600)
    return build_privacy_policy(
        repository_root=repository,
        denylist_path=denylist,
        home_prefixes=(Path("/Users/private-owner"),),
    )


def _replace_png_iend_payload(document: bytes, payload: bytes) -> bytes:
    assert document[-12:-8] == b"\x00\x00\x00\x00"
    assert document[-8:-4] == b"IEND"
    chunk_type = b"IEND"
    crc = zlib.crc32(chunk_type)
    crc = zlib.crc32(payload, crc) & 0xFFFFFFFF
    return (
        document[:-12]
        + struct.pack(">I", len(payload))
        + chunk_type
        + payload
        + struct.pack(">I", crc)
    )


def _append_webp_pixel_payload(document: bytes, payload: bytes) -> bytes:
    assert document[:4] == b"RIFF"
    assert document[8:12] == b"WEBP"
    chunk_type = document[12:16]
    assert chunk_type in {b"VP8 ", b"VP8L"}
    original_length = struct.unpack("<I", document[16:20])[0]
    original_end = 20 + original_length
    assert original_end + (original_length % 2) == len(document)
    pixel_payload = document[20:original_end] + payload
    chunk = chunk_type + struct.pack("<I", len(pixel_payload)) + pixel_payload
    if len(pixel_payload) % 2:
        chunk += b"\x00"
    riff_payload = b"WEBP" + chunk
    return b"RIFF" + struct.pack("<I", len(riff_payload)) + riff_payload


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("/Users/private-owner/Documents/secret", "home_prefix"),
        ("Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456", "token_like"),
        ("api_key=sk-proj-abcdefghijklmnopqrstuvwxyz123456", "token_like"),
        ("A" * 43, "token_like"),
        ("this has a private phrase inside", "forbidden_term"),
        ("99999999-9999-4999-8999-999999999999", "unknown_uuid"),
    ],
)
def test_text_scan_rejects_private_material_without_echoing_it(
    tmp_path: Path,
    text: str,
    code: str,
) -> None:
    policy = _policy(tmp_path)

    with pytest.raises(PrivacyViolation) as caught:
        scan_text(text, policy, asset_name="fixture.txt")

    assert caught.value.code == code
    assert text not in str(caught.value)
    assert "private phrase" not in str(caught.value)


def test_text_scan_accepts_only_fixed_synthetic_uuids(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    document = "\n".join(str(value) for value in sorted(SYNTHETIC_UUIDS, key=str))

    scan_text(document, policy, asset_name="inventory.json")


@pytest.mark.parametrize(
    "token",
    [
        "ghp_" + "A" * 36,
        "github_pat_" + "A" * 22 + "_" + "B" * 59,
        "AKIA" + "A" * 16,
        "AWS_SECRET_ACCESS_KEY=" + "A" * 20 + "/" + "B" * 19,
        "xoxb-" + "1" * 12 + "-" + "2" * 12 + "-" + "A" * 24,
    ],
)
def test_text_scan_rejects_provider_credentials(tmp_path: Path, token: str) -> None:
    with pytest.raises(PrivacyViolation) as caught:
        scan_text(token, _policy(tmp_path), asset_name="fixture.txt")

    assert caught.value.code == "token_like"
    assert token not in str(caught.value)


@pytest.mark.parametrize(
    "text",
    [
        "f99999999-9999-4999-8999-999999999999",
        "99999999-9999-4999-8999-999999999999f",
    ],
)
def test_uuid_cannot_hide_behind_adjacent_hex(tmp_path: Path, text: str) -> None:
    with pytest.raises(PrivacyViolation, match="unknown_uuid"):
        scan_text(text, _policy(tmp_path), asset_name="fixture.txt")


@pytest.mark.parametrize("suffix", ["html", "svg"])
def test_markup_scan_detects_private_visible_text_split_across_nodes(
    tmp_path: Path,
    suffix: str,
) -> None:
    policy = _policy(tmp_path)
    path = tmp_path / f"malicious.{suffix}"
    if suffix == "html":
        path.write_text("<main><span>private </span><span>phrase</span></main>", encoding="utf-8")
    else:
        path.write_text(
            '<svg xmlns="http://www.w3.org/2000/svg"><text>private <tspan>phrase</tspan></text></svg>',
            encoding="utf-8",
        )

    with pytest.raises(PrivacyViolation) as caught:
        scan_file(path, policy, asset_name=path.name)

    assert caught.value.code == "forbidden_term"
    assert "private phrase" not in str(caught.value)


def test_svg_rejects_active_or_external_content(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    path = tmp_path / "active.svg"
    path.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><script>safe</script></svg>',
        encoding="utf-8",
    )

    with pytest.raises(PrivacyViolation, match="svg_active_content"):
        scan_file(path, policy, asset_name=path.name)


@pytest.mark.parametrize(
    "document",
    [
        '<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        '<svg xmlns="http://www.w3.org/2000/svg"><style>'
        "@import url(https://example.invalid/a.css);"
        "</style></svg>",
        '<svg xmlns="http://www.w3.org/2000/svg">'
        '<rect style="fill:url(javascript:alert(1))"/></svg>',
    ],
)
def test_svg_rejects_event_handlers_and_active_css(
    tmp_path: Path,
    document: str,
) -> None:
    path = tmp_path / "active.svg"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(PrivacyViolation, match="svg_active_content"):
        scan_file(path, _policy(tmp_path), asset_name=path.name)


def test_png_text_chunk_is_rejected_even_when_pixels_are_safe(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    path = tmp_path / "metadata.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "private phrase")
    Image.new("RGB", (8, 8), "white").save(path, pnginfo=metadata)

    with pytest.raises(PrivacyViolation) as caught:
        scan_file(path, policy, asset_name=path.name)

    assert caught.value.code == "png_metadata"
    assert "private phrase" not in str(caught.value)


def test_png_metadata_limit_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "metadata.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "x" * 64)
    Image.new("RGB", (8, 8), "white").save(path, pnginfo=metadata)

    with pytest.raises(PrivacyViolation, match="metadata_too_large"):
        scan_file(
            path,
            _policy(tmp_path),
            asset_name=path.name,
            limits=PrivacyLimits(max_metadata_bytes=8),
        )


def test_png_rejects_a_nonempty_iend_chunk_even_with_a_valid_crc(tmp_path: Path) -> None:
    path = tmp_path / "nonempty-iend.png"
    Image.new("RGB", (8, 8), "white").save(path)
    path.write_bytes(_replace_png_iend_payload(path.read_bytes(), b"x"))

    with pytest.raises(PrivacyViolation) as caught:
        scan_file(path, _policy(tmp_path), asset_name=path.name)

    assert caught.value.code == "invalid_png"


@pytest.mark.parametrize(
    ("container", "credential"),
    [
        ("png", "ghp_" + "A" * 36),
        ("png", "AKIA" + "A" * 16),
        ("webp", "xoxb-" + "1" * 12 + "-" + "2" * 12 + "-" + "A" * 24),
        ("webp", "Bearer " + "a" * 32),
    ],
)
def test_raster_scan_rejects_printable_credentials_inside_core_chunks(
    tmp_path: Path,
    container: str,
    credential: str,
) -> None:
    path = tmp_path / f"credential.{container}"
    if container == "png":
        Image.new("RGB", (8, 8), "white").save(path)
        document = _replace_png_iend_payload(path.read_bytes(), credential.encode("ascii"))
    else:
        Image.new("RGB", (8, 8), "white").save(path, format="WEBP", lossless=True)
        document = _append_webp_pixel_payload(path.read_bytes(), credential.encode("ascii"))
    path.write_bytes(document)

    with pytest.raises(PrivacyViolation) as caught:
        scan_file(path, _policy(tmp_path), asset_name=path.name)

    assert caught.value.code == "token_like"
    assert credential not in str(caught.value)


@pytest.mark.parametrize("metadata_key", ["exif", "xmp"])
def test_webp_exif_and_xmp_are_rejected(tmp_path: Path, metadata_key: str) -> None:
    policy = _policy(tmp_path)
    path = tmp_path / f"metadata-{metadata_key}.webp"
    kwargs = {metadata_key: b"private phrase"}
    Image.new("RGB", (8, 8), "white").save(path, format="WEBP", **kwargs)

    with pytest.raises(PrivacyViolation) as caught:
        scan_file(path, policy, asset_name=path.name)

    assert caught.value.code == "webp_metadata"
    assert "private phrase" not in str(caught.value)


def test_webp_metadata_limit_is_enforced(tmp_path: Path) -> None:
    path = tmp_path / "metadata.webp"
    Image.new("RGB", (8, 8), "white").save(
        path,
        format="WEBP",
        xmp=b"x" * 64,
    )

    with pytest.raises(PrivacyViolation, match="metadata_too_large"):
        scan_file(
            path,
            _policy(tmp_path),
            asset_name=path.name,
            limits=PrivacyLimits(max_metadata_bytes=8),
        )


def test_corrupt_png_metadata_fails_closed(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    path = tmp_path / "corrupt.png"
    Image.new("RGB", (8, 8), "white").save(path)
    document = bytearray(path.read_bytes())
    document[-5] ^= 0xFF
    path.write_bytes(document)

    with pytest.raises(PrivacyViolation, match="invalid_png"):
        scan_file(path, policy, asset_name=path.name)


def test_pillow_decompression_bomb_has_a_stable_pixel_limit_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "bomb.png"
    Image.new("RGB", (8, 8), "white").save(path)

    def reject_image(*_args: object, **_kwargs: object) -> object:
        raise Image.DecompressionBombError("private parser detail")

    monkeypatch.setattr(Image, "open", reject_image)

    with pytest.raises(PrivacyViolation) as caught:
        scan_file(path, _policy(tmp_path), asset_name=path.name)

    assert caught.value.code == "pixel_limit_exceeded"
    assert "private parser detail" not in str(caught.value)


def test_other_pillow_parse_errors_have_a_stable_invalid_raster_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "invalid-raster.png"
    Image.new("RGB", (8, 8), "white").save(path)

    def reject_image(*_args: object, **_kwargs: object) -> object:
        raise UnidentifiedImageError("private parser detail")

    monkeypatch.setattr(Image, "open", reject_image)

    with pytest.raises(PrivacyViolation) as caught:
        scan_file(path, _policy(tmp_path), asset_name=path.name)

    assert caught.value.code == "invalid_raster"
    assert "private parser detail" not in str(caught.value)


def test_limits_fail_closed_for_large_file_decode_and_file_count(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    oversized = tmp_path / "oversized.txt"
    oversized.write_text("x" * 65, encoding="utf-8")

    with pytest.raises(PrivacyViolation, match="file_too_large"):
        scan_file(
            oversized,
            policy,
            asset_name=oversized.name,
            limits=PrivacyLimits(max_file_bytes=64),
        )

    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "one.json").write_text("{}", encoding="utf-8")
    (assets / "two.json").write_text("{}", encoding="utf-8")
    with pytest.raises(PrivacyViolation, match="file_count_exceeded"):
        scan_asset_directory(assets, policy, limits=PrivacyLimits(max_files=1))


def test_invalid_utf8_and_decode_limit_fail_closed(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"\xff")
    with pytest.raises(PrivacyViolation, match="invalid_text"):
        scan_file(invalid, policy, asset_name=invalid.name)

    decoded = tmp_path / "decoded.json"
    decoded.write_text(json.dumps({"value": "safe text"}), encoding="utf-8")
    with pytest.raises(PrivacyViolation, match="decoded_text_too_large"):
        scan_file(
            decoded,
            policy,
            asset_name=decoded.name,
            limits=PrivacyLimits(max_decoded_chars=4),
        )


def test_denylist_must_be_external_private_regular_utf8_file(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    inside = repository / "denylist.txt"
    inside.write_text("secret", encoding="utf-8")
    inside.chmod(0o600)

    with pytest.raises(PrivacyPolicyError, match="denylist_rejected"):
        build_privacy_policy(repository_root=repository, denylist_path=inside)

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"\xff")
    outside.chmod(0o600)
    with pytest.raises(PrivacyPolicyError, match="denylist_rejected"):
        build_privacy_policy(repository_root=repository, denylist_path=outside)


def test_output_marker_requires_exact_private_document(tmp_path: Path) -> None:
    path = tmp_path / OUTPUT_MARKER_NAME
    path.write_bytes(DEMO_MARKER_DOCUMENT + b"tampered")

    with pytest.raises(PrivacyViolation, match="invalid_demo_marker"):
        scan_file(path, _policy(tmp_path), asset_name=OUTPUT_MARKER_NAME)

    path.write_bytes(DEMO_MARKER_DOCUMENT)
    path.chmod(0o622)
    with pytest.raises(PrivacyViolation, match="invalid_demo_marker"):
        scan_file(path, _policy(tmp_path), asset_name=OUTPUT_MARKER_NAME)


def test_denylist_handles_short_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    denylist = tmp_path / "denylist.txt"
    denylist.write_text("first\nprivate tail\n", encoding="utf-8")
    denylist.chmod(0o600)
    real_read = privacy_module.os.read
    monkeypatch.setattr(
        privacy_module.os,
        "read",
        lambda descriptor, count: real_read(descriptor, min(count, 1)),
    )

    policy = build_privacy_policy(
        repository_root=repository,
        denylist_path=denylist,
        home_prefixes=(),
    )
    with pytest.raises(PrivacyViolation, match="forbidden_term"):
        scan_text("private tail", policy, asset_name="fixture.txt")


def test_denylist_rejects_empty_normalized_content(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    denylist = tmp_path / "denylist.txt"
    denylist.write_text(" \n\t\n", encoding="utf-8")
    denylist.chmod(0o600)

    with pytest.raises(PrivacyPolicyError, match="denylist_rejected"):
        build_privacy_policy(
            repository_root=repository,
            denylist_path=denylist,
            home_prefixes=(),
        )


def test_dom_receipt_hash_excludes_hidden_values_and_scans_visible_content(
    tmp_path: Path,
) -> None:
    policy = _policy(tmp_path)
    receipt = {
        "route": "/memories",
        "visible_text": ["DEMO DATA", "Northstar Notes"],
        "attributes": ["Exact model namespace"],
    }

    canonical, digest = canonical_dom_receipt(receipt)

    assert b"DEMO DATA" in canonical
    assert len(digest) == 64
    scan_dom_receipt(receipt, policy, asset_name="memories.dom.json")

    receipt["visible_text"].append("private phrase")
    with pytest.raises(PrivacyViolation, match="forbidden_term"):
        scan_dom_receipt(receipt, policy, asset_name="memories.dom.json")


def test_clean_seed_manifest_and_metadata_free_rasters_pass(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    root = tmp_path / "demo"
    root.mkdir()
    workspace = prepare_demo_workspace(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names={"manifest.json", "preview.png", "preview.webp"},
    )
    inventory = seed_demo_database(workspace)
    (workspace.output_dir / "manifest.json").write_bytes(inventory.to_json_bytes())
    Image.new("RGB", (16, 8), "white").save(workspace.output_dir / "preview.png")
    Image.new("RGB", (16, 8), "white").save(
        workspace.output_dir / "preview.webp",
        format="WEBP",
    )

    report = scan_asset_directory(workspace.output_dir, policy)

    assert {item.asset_name for item in report.files} >= {
        "manifest.json",
        "preview.png",
        "preview.webp",
    }
    workspace.cleanup_runtime()
