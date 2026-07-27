from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import stat
from pathlib import Path, PurePosixPath
from typing import Any

from project_memory_hub.demo.privacy import (
    PrivacyPolicyError,
    PrivacyLimits,
    PrivacyViolation,
    build_privacy_policy,
    canonical_dom_receipt,
    scan_document,
    scan_dom_receipt,
)
from project_memory_hub.demo.seed import (
    CHATGPT_NAMESPACE,
    CODEX_NAMESPACE,
    FACT_IDS,
    MEMORY_IDS,
    PROJECT_ID,
    PROPOSAL_ID,
    DEMO_LABEL,
    SYNTHETIC_UUIDS,
)

if __package__:
    from scripts.generate_demo_assets import (
        PUBLIC_ASSET_NAMES,
        MANIFEST_NAME,
        SCREENSHOT_NAMES,
        SOCIAL_PREVIEW_SIZE,
        SVG_NAMES,
        VIEWPORT,
    )
else:
    from generate_demo_assets import (  # type: ignore[import-not-found]
        PUBLIC_ASSET_NAMES,
        MANIFEST_NAME,
        SCREENSHOT_NAMES,
        SOCIAL_PREVIEW_SIZE,
        SVG_NAMES,
        VIEWPORT,
    )


class AssetVerificationError(ValueError):
    """A public demo asset set failed a stable release contract."""


def verify_public_assets(
    asset_root: Path,
    *,
    repository_root: Path | None = None,
    denylist_path: Path | None = None,
) -> dict[str, Any]:
    root = Path(asset_root)
    limits = PrivacyLimits()
    documents = _read_asset_snapshot(root, limits=limits)

    selected_repository = (
        Path(__file__).resolve().parents[1] if repository_root is None else Path(repository_root)
    )
    try:
        policy = build_privacy_policy(
            repository_root=selected_repository,
            denylist_path=denylist_path,
        )
        scanned_items = {
            name: scan_document(
                document,
                policy,
                asset_name=name,
                limits=limits,
            )
            for name, document in documents.items()
        }
    except (PrivacyPolicyError, PrivacyViolation, OSError, TypeError, ValueError) as error:
        raise AssetVerificationError("asset_privacy_invalid") from error

    try:
        manifest_text = documents[MANIFEST_NAME].decode("utf-8", errors="strict")
        manifest = json.loads(
            manifest_text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_json_constant,
        )
        canonical_manifest = (
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8", errors="strict")
    except (TypeError, UnicodeError, ValueError, json.JSONDecodeError) as error:
        raise AssetVerificationError("manifest_invalid") from error
    if canonical_manifest != documents[MANIFEST_NAME]:
        raise AssetVerificationError("manifest_invalid")
    if not isinstance(manifest, dict) or set(manifest) != {
        "assets",
        "default_runtime_unchanged",
        "demo_label",
        "generator",
        "render",
        "routes",
        "schema_version",
        "seed",
        "seed_version",
    }:
        raise AssetVerificationError("manifest_invalid")
    if (
        not _is_exact_int(manifest["schema_version"], 1)
        or manifest["generator"] != "project-memory-hub-demo-assets"
        or not _is_exact_int(manifest["seed_version"], 1)
        or manifest["demo_label"] != DEMO_LABEL
        or manifest["default_runtime_unchanged"] is not True
        or not _strict_json_equal(manifest["routes"], ["/", "/sources", "/memories"])
        or not _strict_json_equal(
            manifest["render"],
            {
                "locale": "en-US",
                "reduced_motion": "reduce",
                "timezone": "UTC",
                "viewport": VIEWPORT,
            },
        )
    ):
        raise AssetVerificationError("manifest_contract_invalid")

    if not _strict_json_equal(manifest["seed"], _expected_seed_document()):
        raise AssetVerificationError("manifest_seed_invalid")

    assets = manifest["assets"]
    if not isinstance(assets, list) or len(assets) != 7:
        raise AssetVerificationError("manifest_assets_invalid")
    if any(not isinstance(asset, dict) for asset in assets):
        raise AssetVerificationError("manifest_assets_invalid")
    paths = [asset.get("path") for asset in assets]
    expected_asset_order = [*SCREENSHOT_NAMES, *SVG_NAMES, "social-preview.png"]
    if paths != expected_asset_order:
        raise AssetVerificationError("manifest_assets_invalid")

    expected_routes = {
        "screenshots/overview.png": "/",
        "screenshots/sources.png": "/sources",
        "screenshots/memories.png": "/memories",
    }
    expected_headings = {
        "screenshots/overview.png": "Overview",
        "screenshots/sources.png": "Sources",
        "screenshots/memories.png": "Memories",
    }
    for name in SCREENSHOT_NAMES:
        entry = _asset_by_path(assets, name)
        if set(entry) != {
            "dom_receipt",
            "dom_sha256",
            "height",
            "http_status",
            "kind",
            "path",
            "sha256",
            "width",
        }:
            raise AssetVerificationError("screenshot_contract_invalid")
        if (
            entry["kind"] != "screenshot"
            or not _is_exact_int(entry["http_status"], 200)
            or not _is_exact_int(entry["width"], VIEWPORT["width"])
            or not _is_exact_int(entry["height"], VIEWPORT["height"])
            or scanned_items[name].width != VIEWPORT["width"]
            or scanned_items[name].height != VIEWPORT["height"]
            or not _matches_digest(documents[name], entry["sha256"])
        ):
            raise AssetVerificationError("screenshot_contract_invalid")
        try:
            _canonical, digest = canonical_dom_receipt(entry["dom_receipt"])
            scan_dom_receipt(
                entry["dom_receipt"],
                policy,
                asset_name=f"{name}.dom.json",
            )
        except (PrivacyPolicyError, PrivacyViolation) as error:
            raise AssetVerificationError("screenshot_dom_invalid") from error
        receipt = entry["dom_receipt"]
        if digest != entry["dom_sha256"] or not _valid_dom_receipt(
            receipt,
            expected_route=expected_routes[name],
            expected_heading=expected_headings[name],
        ):
            raise AssetVerificationError("screenshot_dom_invalid")

    for name in SVG_NAMES:
        entry = _asset_by_path(assets, name)
        if set(entry) != {"kind", "path", "sha256"} or entry["kind"] != "diagram":
            raise AssetVerificationError("diagram_contract_invalid")
        if not _matches_digest(documents[name], entry["sha256"]):
            raise AssetVerificationError("diagram_contract_invalid")

    social = _asset_by_path(assets, "social-preview.png")
    if (
        set(social) != {"height", "kind", "path", "sha256", "width"}
        or social["kind"] != "social_preview"
        or not _is_exact_int(social["width"], SOCIAL_PREVIEW_SIZE[0])
        or not _is_exact_int(social["height"], SOCIAL_PREVIEW_SIZE[1])
        or scanned_items["social-preview.png"].width != SOCIAL_PREVIEW_SIZE[0]
        or scanned_items["social-preview.png"].height != SOCIAL_PREVIEW_SIZE[1]
        or not _matches_digest(
            documents["social-preview.png"],
            social["sha256"],
        )
        or not _valid_social_preview_visual(documents["social-preview.png"])
    ):
        raise AssetVerificationError("social_preview_contract_invalid")
    final_documents = _read_asset_snapshot(root, limits=limits)
    if final_documents != documents:
        raise AssetVerificationError("asset_snapshot_invalid")
    return manifest


def _read_asset_snapshot(root: Path, *, limits: PrivacyLimits) -> dict[str, bytes]:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(root, flags)
        root_before = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_before.st_mode)
            or root_before.st_uid != os.getuid()
            or stat.S_IMODE(root_before.st_mode) & 0o022
        ):
            raise AssetVerificationError("asset_root_invalid")
        expected_files = _validated_public_asset_paths(PUBLIC_ASSET_NAMES)
        documents: dict[str, bytes] = {}
        identities: dict[str, tuple[int, int, int, int, int, int, int]] = {}
        _read_asset_directory(
            descriptor,
            prefix="",
            expected_files=expected_files,
            documents=documents,
            identities=identities,
            limits=limits,
            total_bytes=[0],
        )
        if frozenset(documents) != expected_files:
            raise AssetVerificationError("asset_set_invalid")
        for name, expected in sorted(identities.items()):
            if _reopen_file_identity(descriptor, name) != expected:
                raise AssetVerificationError("asset_snapshot_invalid")
        root_after = os.fstat(descriptor)
        if _directory_identity(root_after) != _directory_identity(root_before):
            raise AssetVerificationError("asset_snapshot_invalid")
        live_descriptor = os.open(root, flags)
        try:
            if _directory_identity(os.fstat(live_descriptor)) != _directory_identity(root_before):
                raise AssetVerificationError("asset_snapshot_invalid")
        finally:
            os.close(live_descriptor)
        return documents
    except AssetVerificationError:
        raise
    except OSError as error:
        raise AssetVerificationError("asset_snapshot_invalid") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validated_public_asset_paths(names: frozenset[str]) -> frozenset[str]:
    validated: set[str] = set()
    for name in names:
        path = PurePosixPath(name)
        if (
            not name
            or path.is_absolute()
            or path.as_posix() != name
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in name
            or any(ord(character) < 32 for character in name)
        ):
            raise AssetVerificationError("asset_set_invalid")
        validated.add(name)
    return frozenset(validated)


def _read_asset_directory(
    descriptor: int,
    *,
    prefix: str,
    expected_files: frozenset[str],
    documents: dict[str, bytes],
    identities: dict[str, tuple[int, int, int, int, int, int, int]],
    limits: PrivacyLimits,
    total_bytes: list[int],
) -> None:
    before = os.fstat(descriptor)
    if (
        not stat.S_ISDIR(before.st_mode)
        or before.st_uid != os.getuid()
        or stat.S_IMODE(before.st_mode) & 0o022
    ):
        raise AssetVerificationError("asset_snapshot_invalid")
    file_children: dict[str, str] = {}
    directory_children: set[str] = set()
    for relative in expected_files:
        path = PurePosixPath(relative)
        parent = path.parent.as_posix()
        normalized_parent = "" if parent == "." else parent
        if normalized_parent == prefix:
            file_children[path.name] = relative
        elif prefix:
            prefix_text = f"{prefix}/"
            if relative.startswith(prefix_text):
                directory_children.add(relative[len(prefix_text) :].split("/", 1)[0])
        elif "/" in relative:
            directory_children.add(relative.split("/", 1)[0])
    expected_names = frozenset((*file_children, *directory_children))
    names = _directory_names(descriptor)
    if names != expected_names:
        raise AssetVerificationError("asset_set_invalid")

    for name in sorted(directory_children):
        child = os.open(
            name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            _read_asset_directory(
                child,
                prefix=f"{prefix}/{name}" if prefix else name,
                expected_files=expected_files,
                documents=documents,
                identities=identities,
                limits=limits,
                total_bytes=total_bytes,
            )
        finally:
            os.close(child)

    for name, relative in sorted(file_children.items()):
        document, identity = _read_snapshot_entry(
            descriptor,
            name,
            max_bytes=limits.max_file_bytes,
        )
        total_bytes[0] += len(document)
        if total_bytes[0] > limits.max_total_bytes:
            raise AssetVerificationError("asset_snapshot_invalid")
        documents[relative] = document
        identities[relative] = identity

    if _directory_names(descriptor) != names:
        raise AssetVerificationError("asset_snapshot_invalid")
    if _directory_identity(os.fstat(descriptor)) != _directory_identity(before):
        raise AssetVerificationError("asset_snapshot_invalid")


def _directory_names(descriptor: int) -> frozenset[str]:
    with os.scandir(descriptor) as entries:
        return frozenset(entry.name for entry in entries)


def _reopen_file_identity(
    root_descriptor: int,
    name: str,
) -> tuple[int, int, int, int, int, int, int]:
    components = PurePosixPath(name).parts
    directory_descriptor = os.dup(root_descriptor)
    file_descriptor = -1
    try:
        for component in components[:-1]:
            child = os.open(
                component,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            metadata = os.fstat(child)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                os.close(child)
                raise AssetVerificationError("asset_snapshot_invalid")
            os.close(directory_descriptor)
            directory_descriptor = child
        file_descriptor = os.open(
            components[-1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise AssetVerificationError("asset_snapshot_invalid")
        return _file_identity(metadata)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        os.close(directory_descriptor)


def _read_snapshot_entry(
    directory_descriptor: int,
    name: str,
    *,
    max_bytes: int,
) -> tuple[bytes, tuple[int, int, int, int, int, int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(name, flags, dir_fd=directory_descriptor)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size > max_bytes
        ):
            raise AssetVerificationError("asset_snapshot_invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 1024 * 1024))
            if not chunk:
                raise AssetVerificationError("asset_snapshot_invalid")
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise AssetVerificationError("asset_snapshot_invalid")
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(before):
            raise AssetVerificationError("asset_snapshot_invalid")
        return b"".join(chunks), _file_identity(before)
    finally:
        os.close(descriptor)


def _directory_identity(metadata: os.stat_result) -> tuple[int, int, int, int]:
    return metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_ctime_ns


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_json_constant(_value: str) -> Any:
    raise ValueError("non-finite JSON value")


def _is_exact_int(value: object, expected: int) -> bool:
    return type(value) is int and value == expected


def _strict_json_equal(actual: object, expected: object) -> bool:
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, dict):
        if not isinstance(actual, dict) or set(actual) != set(expected):
            return False
        return all(_strict_json_equal(actual[key], value) for key, value in expected.items())
    if isinstance(expected, list):
        if not isinstance(actual, list) or len(actual) != len(expected):
            return False
        return all(
            _strict_json_equal(actual_value, expected_value)
            for actual_value, expected_value in zip(actual, expected, strict=True)
        )
    return actual == expected


def _expected_seed_document() -> dict[str, Any]:
    return {
        "fact_ids": [str(value) for value in FACT_IDS],
        "generated_at": "2026-07-18T12:00:00Z",
        "label": DEMO_LABEL,
        "memory_ids": [str(value) for value in MEMORY_IDS],
        "namespaces": [
            {
                "model_id": CODEX_NAMESPACE.model_id,
                "source_agent": CODEX_NAMESPACE.source_agent.value,
            },
            {
                "model_id": CHATGPT_NAMESPACE.model_id,
                "source_agent": CHATGPT_NAMESPACE.source_agent.value,
            },
        ],
        "project_id": str(PROJECT_ID),
        "project_name": "Northstar Notes — DEMO DATA",
        "proposal_id": str(PROPOSAL_ID),
        "reconcile_receipt": "synthetic-fixed-clock",
        "seed_version": 1,
        "source_states": [
            {"ingestion_allowed": True, "model_status": "verified", "source_agent": "codex"},
            {
                "ingestion_allowed": True,
                "model_status": "verified",
                "source_agent": "chatgpt",
            },
            {
                "ingestion_allowed": False,
                "model_status": "unverifiable",
                "source_agent": "trae",
            },
            {
                "ingestion_allowed": False,
                "model_status": "not_checked",
                "source_agent": "workbuddy",
            },
            {
                "ingestion_allowed": False,
                "model_status": "not_checked",
                "source_agent": "zcode",
            },
            {
                "ingestion_allowed": False,
                "model_status": "not_checked",
                "source_agent": "qoderwork",
            },
            {
                "ingestion_allowed": False,
                "model_status": "not_checked",
                "source_agent": "claude_code",
            },
        ],
        "synthetic_uuid_allowlist": [str(value) for value in sorted(SYNTHETIC_UUIDS, key=str)],
    }


def _matches_digest(document: bytes, expected: object) -> bool:
    return (
        isinstance(expected, str)
        and len(expected) == 64
        and expected == expected.casefold()
        and all(character in "0123456789abcdef" for character in expected)
        and hashlib.sha256(document).hexdigest() == expected
    )


def _valid_dom_receipt(
    receipt: object,
    *,
    expected_route: str,
    expected_heading: str,
) -> bool:
    if not isinstance(receipt, dict) or set(receipt) != {
        "attributes",
        "current_navigation",
        "demo_overlay",
        "heading",
        "main_content_count",
        "route",
        "title",
        "ui_contract",
        "visible_text",
    }:
        return False
    attributes = receipt["attributes"]
    current_navigation = receipt["current_navigation"]
    visible_text = receipt["visible_text"]
    overlay = receipt["demo_overlay"]
    if (
        receipt["route"] != expected_route
        or receipt["heading"] != expected_heading
        or receipt["title"] != f"{expected_heading} · Project Memory Hub"
        or not _is_exact_int(receipt["main_content_count"], 1)
        or not isinstance(attributes, list)
        or not attributes
        or any(not isinstance(value, str) for value in attributes)
        or attributes != sorted(set(attributes))
        or "Synthetic demonstration data" not in attributes
        or not _valid_current_navigation(
            current_navigation,
            expected_route=expected_route,
            expected_heading=expected_heading,
        )
        or not _valid_ui_contract(
            receipt["ui_contract"],
            expected_route=expected_route,
            receipt=receipt,
        )
        or not isinstance(visible_text, list)
        or len(visible_text) != 1
        or not isinstance(visible_text[0], str)
        or DEMO_LABEL not in visible_text[0]
        or not isinstance(overlay, dict)
        or set(overlay) != {"aria_label", "bounds", "count", "id", "position", "role", "text"}
        or overlay["aria_label"] != "Synthetic demonstration data"
        or not _is_exact_int(overlay["count"], 1)
        or overlay["id"] != "project-memory-hub-demo-overlay"
        or overlay["position"] != "fixed"
        or overlay["role"] != "note"
        or overlay["text"] != DEMO_LABEL
    ):
        return False
    bounds = overlay["bounds"]
    if not isinstance(bounds, dict) or set(bounds) != {"bottom", "left", "right", "top"}:
        return False
    values = tuple(bounds[name] for name in ("bottom", "left", "right", "top"))
    if any(type(value) not in {int, float} for value in values):
        return False
    return (
        0 <= bounds["left"] < bounds["right"] <= VIEWPORT["width"]
        and 0 <= bounds["top"] < bounds["bottom"] <= VIEWPORT["height"]
        and abs(bounds["right"] - (VIEWPORT["width"] - 24)) < 0.01
        and abs(bounds["bottom"] - (VIEWPORT["height"] - 20)) < 0.01
    )


def _valid_current_navigation(
    current_navigation: object,
    *,
    expected_route: str,
    expected_heading: str,
) -> bool:
    if not isinstance(current_navigation, dict) or set(current_navigation) != {
        "count",
        "items",
    }:
        return False
    count = current_navigation["count"]
    items = current_navigation["items"]
    if type(count) is not int or count != 1 or not isinstance(items, list) or len(items) != count:
        return False
    item = items[0]
    return (
        isinstance(item, dict)
        and set(item) == {"aria_current", "href", "text"}
        and item["aria_current"] == "page"
        and item["href"] == expected_route
        and item["text"] == expected_heading
    )


def _valid_ui_contract(
    contract: object,
    *,
    expected_route: str,
    receipt: dict[str, Any],
) -> bool:
    if not isinstance(contract, dict):
        return False
    if expected_route == "/":
        if (
            set(contract)
            != {
                "kind",
                "next_safe_step",
                "visible_recorded_operations_count",
            }
            or contract["kind"] != "overview"
        ):
            return False
        step = contract["next_safe_step"]
        return (
            isinstance(step, dict)
            and set(step)
            == {
                "bounds",
                "command",
                "command_bounds",
                "command_visible",
                "count",
                "kind",
            }
            and _is_exact_int(step["count"], 1)
            and step["kind"] == "reconcile"
            and step["command"] == "memory-hub reconcile --if-due --format json"
            and step["command_visible"] is True
            and _bounds_inside_viewport(step["command_bounds"])
            and _bounds_inside_viewport(step["bounds"])
            and _is_exact_int(contract["visible_recorded_operations_count"], 0)
        )
    if expected_route == "/sources":
        return _valid_sources_ui_contract(contract)
    if expected_route == "/memories":
        return _valid_memories_ui_contract(contract, receipt=receipt)
    return False


def _valid_sources_ui_contract(contract: dict[str, Any]) -> bool:
    if set(contract) != {"collections", "kind"} or contract["kind"] != "sources":
        return False
    collections = contract["collections"]
    if not isinstance(collections, list) or len(collections) != 2:
        return False
    expected = (
        ("ingestion", "Ingestion sources", (("codex", "", False), ("chatgpt", "", False))),
        (
            "probes",
            "Read-only probes",
            (
                ("trae", "Locked", True),
                ("workbuddy", "Locked", True),
                ("zcode", "Locked", True),
                ("qoderwork", "Locked", True),
                ("claude_code", "Locked", True),
            ),
        ),
    )
    for collection, (role, heading, source_contract) in zip(
        collections,
        expected,
        strict=True,
    ):
        if (
            not isinstance(collection, dict)
            or set(collection) != {"heading", "heading_bounds", "role", "sources"}
            or collection["role"] != role
            or collection["heading"] != heading
            or not _bounds_inside_viewport(collection["heading_bounds"])
        ):
            return False
        sources = collection["sources"]
        if not isinstance(sources, list) or len(sources) != len(source_contract):
            return False
        for source, (source_agent, behavior_import, behavior_import_visible) in zip(
            sources,
            source_contract,
            strict=True,
        ):
            if (
                not isinstance(source, dict)
                or set(source)
                != {
                    "behavior_import",
                    "behavior_import_bounds",
                    "behavior_import_visible",
                    "bounds",
                    "source_agent",
                }
                or source["source_agent"] != source_agent
                or source["behavior_import"] != behavior_import
                or source["behavior_import_visible"] is not behavior_import_visible
                or not _bounds_inside_viewport(source["bounds"])
            ):
                return False
            if behavior_import_visible:
                if not _bounds_inside_viewport(source["behavior_import_bounds"]):
                    return False
            elif source["behavior_import_bounds"] is not None:
                return False
    return True


def _valid_memories_ui_contract(
    contract: dict[str, Any],
    *,
    receipt: dict[str, Any],
) -> bool:
    if set(contract) != {
        "guidance_bounds",
        "guidance_command",
        "guidance_command_bounds",
        "guidance_command_visible",
        "guidance_count",
        "kind",
        "memory_card_bounds",
        "memory_card_count",
        "rendered_behavior_namespaces",
        "rendered_model_ids",
        "selected_model_bounds",
        "selected_model_id",
        "selected_project_bounds",
        "selected_project_id",
        "selected_source_agent",
        "selected_source_bounds",
    }:
        return False
    card_count = contract["memory_card_count"]
    card_bounds = contract["memory_card_bounds"]
    try:
        receipt_text = json.dumps(
            receipt,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        return False
    return (
        contract["kind"] == "memories"
        and _is_exact_int(contract["guidance_count"], 1)
        and contract["guidance_command"] == 'memory-hub codex-context --cwd "$PWD" --format json'
        and contract["guidance_command_visible"] is True
        and _bounds_inside_viewport(contract["guidance_command_bounds"])
        and _bounds_inside_viewport(contract["guidance_bounds"])
        and contract["selected_project_id"] == str(PROJECT_ID)
        and _bounds_inside_viewport(contract["selected_project_bounds"])
        and contract["selected_source_agent"] == CODEX_NAMESPACE.source_agent.value
        and _bounds_inside_viewport(contract["selected_source_bounds"])
        and contract["selected_model_id"] == CODEX_NAMESPACE.model_id
        and contract["rendered_model_ids"] == [CODEX_NAMESPACE.model_id]
        and isinstance(contract["rendered_behavior_namespaces"], list)
        and bool(contract["rendered_behavior_namespaces"])
        and set(contract["rendered_behavior_namespaces"]) == {f"codex / {CODEX_NAMESPACE.model_id}"}
        and _bounds_inside_viewport(contract["selected_model_bounds"])
        and type(card_count) is int
        and card_count >= 1
        and isinstance(card_bounds, list)
        and len(card_bounds) == card_count
        and all(_bounds_inside_viewport(bounds) for bounds in card_bounds)
        and CHATGPT_NAMESPACE.model_id not in receipt_text
    )


def _valid_social_preview_visual(document: bytes) -> bool:
    from PIL import Image

    try:
        with Image.open(io.BytesIO(document)) as candidate:
            candidate.load()
            image = candidate.convert("RGB")
    except (OSError, ValueError):
        return False
    if image.size != SOCIAL_PREVIEW_SIZE:
        return False
    if image.getpixel((100, 100)) != (21, 93, 59):
        return False

    title_pixels = image.crop((96, 96, 1_000, 238)).tobytes()
    title_light_ink = sum(
        1
        for red, green, blue in zip(
            title_pixels[0::3],
            title_pixels[1::3],
            title_pixels[2::3],
            strict=True,
        )
        if red >= 250 and green >= 250 and blue >= 250
    )
    body_pixels = image.crop((96, 275, 920, 475)).tobytes()
    body_dark_ink = sum(
        1
        for red, green, blue in zip(
            body_pixels[0::3],
            body_pixels[1::3],
            body_pixels[2::3],
            strict=True,
        )
        if red <= 100 and green <= 120 and blue <= 110
    )
    return title_light_ink >= 4_000 and body_dark_ink >= 3_000


def _bounds_inside_viewport(bounds: object) -> bool:
    if not _valid_bounds(bounds):
        return False
    assert isinstance(bounds, dict)
    return (
        0 <= bounds["left"] < bounds["right"] <= VIEWPORT["width"]
        and 0 <= bounds["top"] < bounds["bottom"] <= VIEWPORT["height"]
    )


def _valid_bounds(bounds: object) -> bool:
    return (
        isinstance(bounds, dict)
        and set(bounds) == {"bottom", "left", "right", "top"}
        and all(type(bounds[name]) is int for name in ("bottom", "left", "right", "top"))
        and bounds["left"] < bounds["right"]
        and bounds["top"] < bounds["bottom"]
    )


def _asset_by_path(assets: list[dict[str, Any]], path: str) -> dict[str, Any]:
    matches = [asset for asset in assets if asset.get("path") == path]
    if len(matches) != 1:
        raise AssetVerificationError("manifest_assets_invalid")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify synthetic public demo assets.")
    parser.add_argument("asset_root", type=Path)
    parser.add_argument("--denylist", type=Path)
    arguments = parser.parse_args()
    verify_public_assets(
        arguments.asset_root,
        repository_root=Path(__file__).resolve().parents[1],
        denylist_path=arguments.denylist,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
