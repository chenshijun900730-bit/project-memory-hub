from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

import scripts.generate_demo_assets as generator_module
import scripts.verify_public_assets as verifier_module
from scripts.generate_demo_assets import (
    GENERATED_ASSET_NAMES,
    PUBLIC_ASSET_NAMES,
    DemoRoutePolicy,
    default_runtime_snapshot,
    generate_demo_assets,
)
from scripts.verify_public_assets import (
    AssetVerificationError,
    _read_asset_snapshot,
    verify_public_assets,
)
from project_memory_hub.demo.privacy import PrivacyLimits, canonical_dom_receipt
from project_memory_hub.demo.runtime import (
    OUTPUT_MARKER_NAME,
    prepare_demo_workspace,
)
from project_memory_hub.demo.seed import CHATGPT_NAMESPACE, CODEX_NAMESPACE
from project_memory_hub.paths import RuntimePaths


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolate_default_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep demo generation checks independent from the user's live runtime."""
    original_for_root = RuntimePaths.for_root
    isolated_default = original_for_root(tmp_path / "default-runtime")
    isolated_default.ensure()
    sentinel = isolated_default.root / "test-sentinel"
    sentinel.write_text("synthetic default runtime\n", encoding="utf-8")
    sentinel.chmod(0o600)

    def isolated_for_root(root: Path | None = None) -> RuntimePaths:
        if root is None:
            return isolated_default
        return original_for_root(root)

    monkeypatch.setattr(
        generator_module.RuntimePaths,
        "for_root",
        staticmethod(isolated_for_root),
    )


def _git_status() -> str:
    return subprocess.run(
        ["git", "status", "--short"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _write_canonical_manifest(path: Path, document: dict[str, object]) -> None:
    path.write_text(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _plain_png(size: tuple[int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "white").save(
        buffer,
        format="PNG",
        compress_level=9,
        optimize=False,
    )
    return buffer.getvalue()


def test_committed_public_asset_bundle_verifies() -> None:
    verified = verify_public_assets(
        REPOSITORY_ROOT / "docs/assets",
        repository_root=REPOSITORY_ROOT,
    )

    assert verified["routes"] == ["/", "/sources", "/memories"]
    assert [asset["path"] for asset in verified["assets"]] == [
        "screenshots/overview.png",
        "screenshots/sources.png",
        "screenshots/memories.png",
        "diagrams/local-data-flow.svg",
        "diagrams/strict-model-isolation.svg",
        "diagrams/approval-gated-improvement.svg",
        "social-preview.png",
    ]


def test_default_runtime_snapshot_is_recursive_physical_and_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "default-runtime"
    paths = RuntimePaths.for_root(root)
    paths.ensure()
    paths.database.write_bytes(b"synthetic database bytes")
    (paths.root / "config.toml").write_text("enabled_sources = []\n", encoding="utf-8")
    nested = paths.logs / "nested"
    nested.mkdir(mode=0o700)
    (nested / "event.log").write_text("synthetic event\n", encoding="utf-8")
    before = sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))

    monkeypatch.setattr(
        generator_module.RuntimePaths,
        "for_root",
        staticmethod(lambda: paths),
    )

    def reject_sqlite_connect(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("snapshot must not open SQLite")

    monkeypatch.setattr(sqlite3, "connect", reject_sqlite_connect)

    snapshot = default_runtime_snapshot()

    assert "memory.db" in {entry[0] for entry in snapshot}
    assert "logs/nested/event.log" in {entry[0] for entry in snapshot}
    assert sorted(path.relative_to(root).as_posix() for path in root.rglob("*")) == before


def test_default_runtime_snapshot_rechecks_files_after_the_full_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "default-runtime"
    paths = RuntimePaths.for_root(root)
    paths.ensure()
    paths.database.write_bytes(b"synthetic database bytes")
    monkeypatch.setattr(
        generator_module.RuntimePaths,
        "for_root",
        staticmethod(lambda: paths),
    )
    real_snapshot_file = generator_module._snapshot_runtime_file
    mutated = False

    def mutate_after_read(
        directory_descriptor: int,
        name: str,
        metadata: os.stat_result,
        *,
        budget: dict[str, int],
    ) -> str:
        nonlocal mutated
        digest = real_snapshot_file(
            directory_descriptor,
            name,
            metadata,
            budget=budget,
        )
        if name == "memory.db" and not mutated:
            mutated = True
            paths.database.write_bytes(b"changed after snapshot read")
        return digest

    monkeypatch.setattr(generator_module, "_snapshot_runtime_file", mutate_after_read)

    with pytest.raises(RuntimeError, match="default runtime snapshot rejected"):
        default_runtime_snapshot()


def test_relative_output_is_scoped_to_the_explicit_repository_root(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    assert generator_module._scoped_output_dir(Path("docs/assets"), repository) == (
        repository / "docs" / "assets"
    )
    with pytest.raises(ValueError, match="demo output rejected"):
        generator_module._scoped_output_dir(Path("../private"), repository)


def test_public_asset_snapshot_accepts_only_the_nested_marker_free_tree(
    tmp_path: Path,
) -> None:
    root = tmp_path / "assets"
    root.mkdir(mode=0o700)
    for name in PUBLIC_ASSET_NAMES:
        target = root / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(f"synthetic {name}\n".encode())
        target.chmod(0o600)

    documents = _read_asset_snapshot(root, limits=PrivacyLimits())

    assert frozenset(documents) == PUBLIC_ASSET_NAMES
    marker = root / OUTPUT_MARKER_NAME
    marker.write_text("private marker\n", encoding="utf-8")
    with pytest.raises(AssetVerificationError, match="asset_set_invalid"):
        _read_asset_snapshot(root, limits=PrivacyLimits())
    marker.unlink()

    screenshots = root / "screenshots"
    screenshots.chmod(0o777)
    with pytest.raises(AssetVerificationError, match="asset_snapshot_invalid"):
        _read_asset_snapshot(root, limits=PrivacyLimits())


def test_public_asset_snapshot_rechecks_every_file_after_the_full_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "assets"
    root.mkdir(mode=0o700)
    for name in PUBLIC_ASSET_NAMES:
        target = root / name
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_bytes(f"synthetic {name}\n".encode())
        target.chmod(0o600)
    overview = root / "screenshots" / "overview.png"
    real_read = verifier_module._read_snapshot_entry
    mutated = False

    def mutate_after_read(
        directory_descriptor: int,
        name: str,
        *,
        max_bytes: int,
    ) -> tuple[bytes, tuple[int, int, int, int, int, int, int]]:
        nonlocal mutated
        document, identity = real_read(
            directory_descriptor,
            name,
            max_bytes=max_bytes,
        )
        if name == "overview.png" and not mutated:
            mutated = True
            overview.write_bytes(b"changed after snapshot read\n")
            overview.chmod(0o600)
        return document, identity

    monkeypatch.setattr(verifier_module, "_read_snapshot_entry", mutate_after_read)

    with pytest.raises(AssetVerificationError, match="asset_snapshot_invalid"):
        _read_asset_snapshot(root, limits=PrivacyLimits())


def test_route_policy_fails_closed_for_projects_and_external_origins() -> None:
    policy = DemoRoutePolicy("http://127.0.0.1:43210")

    assert policy.authorize("http://127.0.0.1:43210/", resource_type="document")
    assert policy.authorize(
        "http://127.0.0.1:43210/static/app.css",
        resource_type="stylesheet",
    )
    assert not policy.authorize(
        "http://127.0.0.1:43210/projects",
        resource_type="document",
    )
    assert not policy.authorize("https://example.com/track", resource_type="image")
    assert policy.violations == ("projects_route_blocked", "external_origin_blocked")


def test_demo_server_closes_listener_when_container_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=REPOSITORY_ROOT,
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=GENERATED_ASSET_NAMES,
    )

    class FakeListener:
        closed = False

        def setsockopt(self, *_args: object) -> None:
            pass

        def bind(self, *_args: object) -> None:
            pass

        def listen(self, *_args: object) -> None:
            pass

        def getsockname(self) -> tuple[str, int]:
            return "127.0.0.1", 43210

        def close(self) -> None:
            self.closed = True

    listener = FakeListener()
    monkeypatch.setattr(generator_module.socket, "socket", lambda *_args: listener)

    def fail_container(_workspace: object) -> object:
        raise RuntimeError("synthetic setup failure")

    monkeypatch.setattr(generator_module, "build_demo_container", fail_container)

    with pytest.raises(RuntimeError, match="synthetic setup failure"):
        with generator_module._demo_server(workspace):
            pass

    assert listener.closed
    workspace.cleanup_incomplete_output()
    workspace.cleanup_runtime()


def test_generate_and_verify_isolated_demo_assets(tmp_path: Path) -> None:
    before_default = default_runtime_snapshot()
    before_git = _git_status()
    root = tmp_path / "demo-one"
    root.mkdir()

    manifest_path = generate_demo_assets(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=REPOSITORY_ROOT,
    )
    verified = verify_public_assets(root / "assets", repository_root=REPOSITORY_ROOT)

    assert manifest_path == root / "assets" / "demo-manifest.json"
    assert {
        path.relative_to(root / "assets").as_posix()
        for path in (root / "assets").rglob("*")
        if path.is_file()
    } == PUBLIC_ASSET_NAMES
    assert not (root / "runtime").exists()
    assert default_runtime_snapshot() == before_default
    assert _git_status() == before_git
    assert verified["routes"] == ["/", "/sources", "/memories"]
    assert verified["demo_label"] == "DEMO DATA"
    assert verified["default_runtime_unchanged"] is True
    assert all("/projects" not in route for route in verified["routes"])
    assert {asset["path"] for asset in verified["assets"]} == {
        "screenshots/overview.png",
        "screenshots/sources.png",
        "screenshots/memories.png",
        "diagrams/local-data-flow.svg",
        "diagrams/strict-model-isolation.svg",
        "diagrams/approval-gated-improvement.svg",
        "social-preview.png",
    }

    for screenshot in (
        "screenshots/overview.png",
        "screenshots/sources.png",
        "screenshots/memories.png",
    ):
        with Image.open(root / "assets" / screenshot) as image:
            assert image.size == (1440, 1000)
            assert image.info == {}
    with Image.open(root / "assets" / "social-preview.png") as image:
        assert image.size == (1280, 640)
        assert image.info == {}

    screenshots = {
        asset["path"]: asset["dom_receipt"]
        for asset in verified["assets"]
        if asset["kind"] == "screenshot"
    }
    overview = screenshots["screenshots/overview.png"]
    assert overview["current_navigation"] == {
        "count": 1,
        "items": [{"aria_current": "page", "href": "/", "text": "Overview"}],
    }
    assert overview["ui_contract"]["next_safe_step"]["count"] == 1
    assert overview["ui_contract"]["next_safe_step"]["kind"] == "reconcile"
    assert overview["ui_contract"]["next_safe_step"]["command"] == (
        "memory-hub reconcile --if-due --format json"
    )
    assert overview["ui_contract"]["next_safe_step"]["command_visible"] is True
    assert overview["ui_contract"]["visible_recorded_operations_count"] == 0

    sources = screenshots["screenshots/sources.png"]
    collections = sources["ui_contract"]["collections"]
    assert [collection["role"] for collection in collections] == ["ingestion", "probes"]
    assert [source["source_agent"] for source in collections[0]["sources"]] == [
        "codex",
        "chatgpt",
    ]
    assert [source["source_agent"] for source in collections[1]["sources"]] == [
        "trae",
        "workbuddy",
        "zcode",
        "qoderwork",
        "claude_code",
    ]
    assert {source["behavior_import"] for source in collections[1]["sources"]} == {"Locked"}
    assert all(source["behavior_import_visible"] is True for source in collections[1]["sources"])

    memories = screenshots["screenshots/memories.png"]
    memories_contract = memories["ui_contract"]
    assert memories_contract["guidance_command"] == (
        'memory-hub codex-context --cwd "$PWD" --format json'
    )
    assert memories_contract["guidance_command_visible"] is True
    assert memories_contract["selected_project_id"] == str(generator_module.PROJECT_ID)
    assert memories_contract["selected_source_agent"] == "codex"
    assert memories_contract["selected_model_id"] == CODEX_NAMESPACE.model_id
    assert memories_contract["memory_card_count"] >= 1
    assert all(
        0 <= bounds["left"] < bounds["right"] <= 1440
        and 0 <= bounds["top"] < bounds["bottom"] <= 1000
        for bounds in memories_contract["memory_card_bounds"]
    )
    assert CHATGPT_NAMESPACE.model_id not in json.dumps(
        memories,
        ensure_ascii=False,
        sort_keys=True,
    )


def test_social_preview_has_release_visual_hierarchy() -> None:
    with Image.open(io.BytesIO(generator_module._social_preview_png())) as image:
        rgb = image.convert("RGB")

    title_pixels = rgb.crop((96, 96, 1000, 238)).tobytes()
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
    body_pixels = rgb.crop((96, 275, 920, 475)).tobytes()
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

    assert title_light_ink >= 4_000
    assert body_dark_ink >= 3_000


def test_manifest_and_svg_assets_are_byte_stable_across_runtimes(tmp_path: Path) -> None:
    documents: list[dict[str, bytes]] = []
    for name in ("first", "second"):
        root = tmp_path / name
        root.mkdir()
        generate_demo_assets(
            runtime_dir=root / "runtime",
            output_dir=root / "assets",
            repository_root=REPOSITORY_ROOT,
        )
        documents.append(
            {
                asset: (root / "assets" / asset).read_bytes()
                for asset in (
                    "demo-manifest.json",
                    "diagrams/local-data-flow.svg",
                    "diagrams/strict-model-isolation.svg",
                    "diagrams/approval-gated-improvement.svg",
                )
            }
        )

    assert documents[0] == documents[1]


def test_verifier_rejects_extra_files_and_png_metadata(tmp_path: Path) -> None:
    root = tmp_path / "tamper"
    root.mkdir()
    generate_demo_assets(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=REPOSITORY_ROOT,
    )
    assets = root / "assets"
    extra = assets / "extra.txt"
    extra.write_text("unexpected", encoding="utf-8")

    with pytest.raises(AssetVerificationError, match="asset_set_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    extra.unlink()
    marker = assets / OUTPUT_MARKER_NAME
    assert not marker.exists()
    marker.write_text("private in-progress marker\n", encoding="utf-8")
    with pytest.raises(AssetVerificationError, match="asset_set_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)
    marker.unlink()

    target = assets / "screenshots" / "overview.png"
    original = target.read_bytes()
    external = root / "external.png"
    external.write_bytes(original)
    target.unlink()
    os.link(external, target)
    with pytest.raises(AssetVerificationError, match="asset_snapshot_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)
    assert external.read_bytes() == original
    target.unlink()
    target.write_bytes(original)
    target.chmod(0o600)

    with Image.open(target) as image:
        copied = image.copy()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "not allowed")
    copied.save(target, pnginfo=metadata)

    with pytest.raises(AssetVerificationError, match="asset_privacy_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)


def test_manifest_dom_receipts_bind_visible_demo_label(tmp_path: Path) -> None:
    root = tmp_path / "receipt"
    root.mkdir()
    generate_demo_assets(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=REPOSITORY_ROOT,
    )
    manifest = json.loads((root / "assets" / "demo-manifest.json").read_text(encoding="utf-8"))

    screenshots = [asset for asset in manifest["assets"] if asset["kind"] == "screenshot"]
    assert len(screenshots) == 3
    for asset in screenshots:
        receipt_text = json.dumps(asset["dom_receipt"], ensure_ascii=False)
        assert "DEMO DATA" in receipt_text
        assert len(asset["dom_sha256"]) == 64
        assert "csrf" not in receipt_text.casefold()
        assert (
            re.search(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])", receipt_text) is None
        )


def test_verifier_binds_screenshot_and_social_preview_bytes(tmp_path: Path) -> None:
    root = tmp_path / "raster-binding"
    root.mkdir()
    generate_demo_assets(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=REPOSITORY_ROOT,
    )
    assets = root / "assets"
    overview = assets / "screenshots" / "overview.png"
    overview_original = overview.read_bytes()
    overview.write_bytes(_plain_png((1440, 1000)))

    with pytest.raises(AssetVerificationError, match="screenshot_contract_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    overview.write_bytes(overview_original)
    social = assets / "social-preview.png"
    plain_social = _plain_png((1280, 640))
    social.write_bytes(plain_social)

    with pytest.raises(AssetVerificationError, match="social_preview_contract_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    manifest_path = assets / "demo-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    social_entry = next(
        asset for asset in manifest["assets"] if asset["path"] == "social-preview.png"
    )
    social_entry["sha256"] = hashlib.sha256(plain_social).hexdigest()
    _write_canonical_manifest(manifest_path, manifest)
    with pytest.raises(AssetVerificationError, match="social_preview_contract_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)


def test_verifier_rechecks_the_tree_after_semantic_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "final-snapshot"
    root.mkdir()
    generate_demo_assets(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=REPOSITORY_ROOT,
    )
    assets = root / "assets"
    overview = assets / "screenshots" / "overview.png"
    real_snapshot = verifier_module._read_asset_snapshot
    calls = 0

    def mutate_after_first_snapshot(
        selected_root: Path,
        *,
        limits: PrivacyLimits,
    ) -> dict[str, bytes]:
        nonlocal calls
        documents = real_snapshot(selected_root, limits=limits)
        calls += 1
        if calls == 1:
            overview.write_bytes(_plain_png((1440, 1000)))
            overview.chmod(0o600)
        return documents

    monkeypatch.setattr(
        verifier_module,
        "_read_asset_snapshot",
        mutate_after_first_snapshot,
    )

    with pytest.raises(AssetVerificationError, match="asset_snapshot_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    assert calls == 2


def test_verifier_rejects_noncanonical_or_semantically_tampered_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "manifest-binding"
    root.mkdir()
    generate_demo_assets(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=REPOSITORY_ROOT,
    )
    assets = root / "assets"
    manifest_path = assets / "demo-manifest.json"
    original_bytes = manifest_path.read_bytes()
    original = json.loads(original_bytes)

    manifest_path.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")
    with pytest.raises(AssetVerificationError, match="manifest_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    seed_tampers = []
    wrong_namespace = copy.deepcopy(original)
    wrong_namespace["seed"]["namespaces"][0]["model_id"] = "another-model"
    seed_tampers.append(wrong_namespace)
    missing_namespace = copy.deepcopy(original)
    missing_namespace["seed"]["namespaces"].pop()
    seed_tampers.append(missing_namespace)
    enabled_optional = copy.deepcopy(original)
    trae = next(
        state
        for state in enabled_optional["seed"]["source_states"]
        if state["source_agent"] == "trae"
    )
    trae["ingestion_allowed"] = True
    trae["model_status"] = "verified"
    seed_tampers.append(enabled_optional)
    integer_boolean = copy.deepcopy(original)
    codex_state = next(
        state
        for state in integer_boolean["seed"]["source_states"]
        if state["source_agent"] == "codex"
    )
    codex_state["ingestion_allowed"] = 1
    seed_tampers.append(integer_boolean)
    boolean_seed_version = copy.deepcopy(original)
    boolean_seed_version["seed"]["seed_version"] = True
    seed_tampers.append(boolean_seed_version)
    for tampered in seed_tampers:
        _write_canonical_manifest(manifest_path, tampered)
        with pytest.raises(AssetVerificationError, match="manifest_seed_invalid"):
            verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    dom_tampered = copy.deepcopy(original)
    screenshot = dom_tampered["assets"][0]
    screenshot["dom_receipt"] = {
        "attributes": [],
        "note": "DEMO DATA",
        "route": "/",
        "visible_text": [],
    }
    _canonical, screenshot["dom_sha256"] = canonical_dom_receipt(screenshot["dom_receipt"])
    _write_canonical_manifest(manifest_path, dom_tampered)
    with pytest.raises(AssetVerificationError, match="screenshot_dom_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    status_tampered = copy.deepcopy(original)
    status_tampered["assets"][0]["http_status"] = 404
    _write_canonical_manifest(manifest_path, status_tampered)
    with pytest.raises(AssetVerificationError, match="screenshot_contract_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    for key in ("schema_version", "seed_version"):
        boolean_version = copy.deepcopy(original)
        boolean_version[key] = True
        _write_canonical_manifest(manifest_path, boolean_version)
        with pytest.raises(AssetVerificationError, match="manifest_contract_invalid"):
            verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    boolean_dom_count = copy.deepcopy(original)
    receipt = boolean_dom_count["assets"][0]["dom_receipt"]
    receipt["main_content_count"] = True
    receipt["demo_overlay"]["count"] = True
    _canonical, boolean_dom_count["assets"][0]["dom_sha256"] = canonical_dom_receipt(receipt)
    _write_canonical_manifest(manifest_path, boolean_dom_count)
    with pytest.raises(AssetVerificationError, match="screenshot_dom_invalid"):
        verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    ui_tampers: list[tuple[dict[str, object], int]] = []

    hidden_overview_command = copy.deepcopy(original)
    overview_contract = hidden_overview_command["assets"][0]["dom_receipt"]["ui_contract"]
    overview_contract["next_safe_step"]["command_visible"] = False
    ui_tampers.append((hidden_overview_command, 0))

    partial_overview = copy.deepcopy(original)
    overview_contract = partial_overview["assets"][0]["dom_receipt"]["ui_contract"]
    overview_contract["visible_recorded_operations_count"] = 1
    ui_tampers.append((partial_overview, 0))

    hidden_locked_label = copy.deepcopy(original)
    source_contract = hidden_locked_label["assets"][1]["dom_receipt"]["ui_contract"]
    source_contract["collections"][1]["sources"][0]["behavior_import_visible"] = False
    ui_tampers.append((hidden_locked_label, 1))

    wrong_memory_source = copy.deepcopy(original)
    memory_contract = wrong_memory_source["assets"][2]["dom_receipt"]["ui_contract"]
    memory_contract["selected_source_agent"] = "chatgpt"
    ui_tampers.append((wrong_memory_source, 2))

    wrong_memory_project = copy.deepcopy(original)
    memory_contract = wrong_memory_project["assets"][2]["dom_receipt"]["ui_contract"]
    memory_contract["selected_project_id"] = original["seed"]["proposal_id"]
    ui_tampers.append((wrong_memory_project, 2))

    partial_memory_card = copy.deepcopy(original)
    memory_contract = partial_memory_card["assets"][2]["dom_receipt"]["ui_contract"]
    memory_contract["memory_card_bounds"] = [
        {"bottom": 1_100, "left": 167, "right": 1_273, "top": 980}
    ]
    ui_tampers.append((partial_memory_card, 2))

    for tampered, screenshot_index in ui_tampers:
        receipt = tampered["assets"][screenshot_index]["dom_receipt"]
        _canonical, tampered["assets"][screenshot_index]["dom_sha256"] = canonical_dom_receipt(
            receipt
        )
        _write_canonical_manifest(manifest_path, tampered)
        with pytest.raises(AssetVerificationError, match="screenshot_dom_invalid"):
            verify_public_assets(assets, repository_root=REPOSITORY_ROOT)

    manifest_path.write_bytes(original_bytes)
    verify_public_assets(assets, repository_root=REPOSITORY_ROOT)


def test_generation_failure_removes_a_new_incomplete_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "new-output"
    root.mkdir()

    def fail_preview() -> bytes:
        raise RuntimeError("synthetic generation failure")

    monkeypatch.setattr(generator_module, "_social_preview_png", fail_preview)

    with pytest.raises(RuntimeError, match="synthetic generation failure"):
        generate_demo_assets(
            runtime_dir=root / "runtime",
            output_dir=root / "assets",
            repository_root=REPOSITORY_ROOT,
        )

    assert not (root / "runtime").exists()
    assert not (root / "assets").exists()


def test_generation_failure_preserves_an_existing_approved_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "existing-output"
    root.mkdir()
    first = prepare_demo_workspace(
        runtime_dir=root / "first-runtime",
        output_dir=root / "assets",
        repository_root=REPOSITORY_ROOT,
        default_runtime_root=root / "default-runtime",
        allowed_output_names=GENERATED_ASSET_NAMES,
    )
    first.write_output_file("demo-manifest.json", b"previous-approved-output\n")
    first.cleanup_runtime()
    before = {
        path.name: path.read_bytes()
        for path in sorted((root / "assets").iterdir())
        if path.is_file()
    }

    def fail_preview() -> bytes:
        raise RuntimeError("synthetic generation failure")

    monkeypatch.setattr(generator_module, "_social_preview_png", fail_preview)

    with pytest.raises(RuntimeError, match="synthetic generation failure"):
        generate_demo_assets(
            runtime_dir=root / "second-runtime",
            output_dir=root / "assets",
            repository_root=REPOSITORY_ROOT,
        )

    after = {
        path.name: path.read_bytes()
        for path in sorted((root / "assets").iterdir())
        if path.is_file()
    }
    assert after == before
    assert not (root / "second-runtime").exists()
