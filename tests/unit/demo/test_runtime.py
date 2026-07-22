from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

import project_memory_hub.demo.runtime as runtime_module
from project_memory_hub.demo.runtime import (
    OUTPUT_MARKER_NAME,
    DemoPathError,
    prepare_demo_workspace,
)
from project_memory_hub.paths import RuntimePaths


GENERATED_FILES = frozenset({"overview.png", "manifest.json"})


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare(
    tmp_path: Path,
    *,
    runtime_dir: Path | None = None,
    output_dir: Path | None = None,
    repository_root: Path | None = None,
    default_runtime_root: Path | None = None,
    temporary_root: Path | None = None,
):
    return prepare_demo_workspace(
        runtime_dir=runtime_dir or tmp_path / "runtime",
        output_dir=output_dir or tmp_path / "assets",
        repository_root=repository_root or tmp_path / "repository",
        default_runtime_root=default_runtime_root or tmp_path / "default-runtime",
        allowed_output_names=GENERATED_FILES,
        temporary_root=temporary_root,
    )


@pytest.mark.parametrize("alias", ["same", "parent", "child"])
def test_rejects_default_runtime_and_parent_or_child_aliases(
    tmp_path: Path,
    alias: str,
) -> None:
    default_root = tmp_path / "home" / "Project Memory Hub"
    candidates = {
        "same": default_root,
        "parent": default_root.parent,
        "child": default_root / "demo",
    }

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(
            tmp_path,
            runtime_dir=candidates[alias],
            default_runtime_root=default_root,
        )

    assert not (tmp_path / "assets").exists()


@pytest.mark.parametrize("relation", ["inside_repository", "contains_repository"])
def test_rejects_runtime_that_overlaps_repository(tmp_path: Path, relation: str) -> None:
    repository = tmp_path / "checkout"
    repository.mkdir()
    runtime = repository / "runtime" if relation == "inside_repository" else tmp_path

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(
            tmp_path,
            runtime_dir=runtime,
            repository_root=repository,
            default_runtime_root=tmp_path / "private-home",
        )


@pytest.mark.parametrize("runtime_is_parent", [True, False])
def test_rejects_runtime_and_output_overlap(tmp_path: Path, runtime_is_parent: bool) -> None:
    runtime = tmp_path / "runtime"
    output = runtime / "assets" if runtime_is_parent else tmp_path / "assets"
    if not runtime_is_parent:
        runtime = output / "runtime"

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(tmp_path, runtime_dir=runtime, output_dir=output)


@pytest.mark.parametrize("relation", ["same", "child"])
def test_rejects_output_inside_default_runtime_boundary(tmp_path: Path, relation: str) -> None:
    default_root = tmp_path / "private-home" / "Project Memory Hub"
    default_root.parent.mkdir()
    if relation == "child":
        default_root.mkdir()
    output = default_root if relation == "same" else default_root / "demo-assets"

    with pytest.raises(DemoPathError, match="demo output rejected"):
        _prepare(
            tmp_path,
            output_dir=output,
            default_runtime_root=default_root,
        )

    assert not output.exists()


def test_rejects_relative_paths(tmp_path: Path) -> None:
    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        prepare_demo_workspace(
            runtime_dir=Path("relative-runtime"),
            output_dir=tmp_path / "assets",
            repository_root=tmp_path / "repository",
            default_runtime_root=tmp_path / "default-runtime",
            allowed_output_names=GENERATED_FILES,
        )


def test_rejects_runtime_outside_explicit_temporary_boundary(tmp_path: Path) -> None:
    temporary_root = tmp_path / "allowed-temp"
    temporary_root.mkdir()

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(
            tmp_path,
            runtime_dir=tmp_path / "outside-temp" / "runtime",
            output_dir=tmp_path / "outside-temp" / "assets",
            temporary_root=temporary_root,
        )

    assert not (tmp_path / "outside-temp").exists()


def test_rejects_runtime_under_group_or_world_writable_parent(tmp_path: Path) -> None:
    temporary_root = tmp_path / "allowed-temp"
    temporary_root.mkdir(mode=0o700)
    temporary_root.chmod(0o777)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(
            tmp_path,
            runtime_dir=temporary_root / "runtime",
            output_dir=tmp_path / "assets",
            temporary_root=temporary_root,
        )

    assert not (temporary_root / "runtime").exists()


@pytest.mark.skipif(not hasattr(Path, "symlink_to"), reason="symlinks unavailable")
def test_rejects_symlink_in_any_existing_path_component(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    sentinel = real_parent / "sentinel.txt"
    sentinel.write_text("outside", encoding="utf-8")
    before = _digest(sentinel)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(tmp_path, runtime_dir=linked_parent / "runtime")

    assert _digest(sentinel) == before
    assert not (real_parent / "runtime").exists()


def test_rejects_existing_database_without_touching_it(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = RuntimePaths.for_root(runtime).database
    database.write_bytes(b"existing-private-database")
    before = _digest(database)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(tmp_path, runtime_dir=runtime)

    assert _digest(database) == before
    assert not (tmp_path / "assets").exists()


def test_rejects_unknown_nonempty_runtime_without_touching_external_file(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    external = runtime / "keep.txt"
    external.write_text("caller-owned", encoding="utf-8")
    before = _digest(external)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(tmp_path, runtime_dir=runtime)

    assert _digest(external) == before
    assert tuple(runtime.iterdir()) == (external,)


def test_output_accepts_only_empty_or_matching_marked_allowlisted_directory(
    tmp_path: Path,
) -> None:
    output = tmp_path / "assets"
    output.mkdir()
    (output / "unknown.txt").write_text("do not overwrite", encoding="utf-8")

    with pytest.raises(DemoPathError, match="demo output rejected"):
        _prepare(tmp_path, output_dir=output)

    assert (output / "unknown.txt").read_text(encoding="utf-8") == "do not overwrite"


def test_output_rejects_mismatched_marker(tmp_path: Path) -> None:
    output = tmp_path / "assets"
    output.mkdir()
    (output / OUTPUT_MARKER_NAME).write_text("wrong-generator\n", encoding="utf-8")

    with pytest.raises(DemoPathError, match="demo output rejected"):
        _prepare(tmp_path, output_dir=output)


def test_output_rejects_group_or_world_writable_directory(tmp_path: Path) -> None:
    output = tmp_path / "assets"
    output.mkdir(mode=0o700)
    output.chmod(0o777)

    with pytest.raises(DemoPathError, match="demo output rejected"):
        _prepare(tmp_path, output_dir=output)

    assert tuple(output.iterdir()) == ()


def test_output_reuses_matching_marker_with_allowlisted_files(tmp_path: Path) -> None:
    first = _prepare(tmp_path)
    (first.output_dir / "overview.png").write_bytes(b"generated")
    first.cleanup_runtime()

    second = _prepare(tmp_path)

    assert second.output_dir == first.output_dir
    assert (second.output_dir / "overview.png").read_bytes() == b"generated"
    second.cleanup_runtime()


def test_output_supports_only_allowlisted_nested_asset_paths(tmp_path: Path) -> None:
    allowed = frozenset({"screenshots/overview.png", "demo-manifest.json"})
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )

    workspace.publish_output_files(
        {
            "screenshots/overview.png": b"synthetic screenshot",
            "demo-manifest.json": b"synthetic manifest",
        }
    )
    workspace.validate_output()

    assert (workspace.output_dir / "screenshots" / "overview.png").read_bytes() == (
        b"synthetic screenshot"
    )
    workspace.cleanup_runtime()


def test_workspace_is_frozen_and_paths_are_computed(tmp_path: Path) -> None:
    workspace = _prepare(tmp_path)

    with pytest.raises(FrozenInstanceError):
        workspace.runtime_dir = tmp_path / "redirected-runtime"  # type: ignore[misc]
    with pytest.raises((FrozenInstanceError, AttributeError, TypeError)):
        workspace.paths = RuntimePaths.for_root(tmp_path / "redirected-runtime")  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        workspace.allowed_output_names = frozenset({"../victim.txt"})  # type: ignore[misc]

    assert workspace.paths.root == workspace.runtime_dir
    workspace.cleanup_runtime()


def test_output_write_rejects_relative_path_escape_without_touching_victim(
    tmp_path: Path,
) -> None:
    workspace = _prepare(tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_bytes(b"caller-owned")
    before = _digest(victim)

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.write_output_file("../victim.txt", b"overwritten")

    assert _digest(victim) == before
    workspace.cleanup_runtime()


def test_output_rejects_unsafe_nested_allowlist_names(tmp_path: Path) -> None:
    unsafe_names = (
        "screenshots//overview.png",
        "screenshots/./overview.png",
        "screenshots/../overview.png",
        "/screenshots/overview.png",
        "screenshots\\overview.png",
        "screenshots/over\x00view.png",
    )

    for index, name in enumerate(unsafe_names):
        with pytest.raises(DemoPathError, match="demo output rejected"):
            prepare_demo_workspace(
                runtime_dir=tmp_path / f"runtime-{index}",
                output_dir=tmp_path / f"assets-{index}",
                repository_root=tmp_path / "repository",
                default_runtime_root=tmp_path / "default-runtime",
                allowed_output_names={name},
            )


def test_nested_output_rejects_replaced_parent_symlink_without_touching_victim(
    tmp_path: Path,
) -> None:
    allowed = frozenset({"screenshots/overview.png", "demo-manifest.json"})
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )
    workspace.write_output_file("screenshots/overview.png", b"old")
    screenshots = workspace.output_dir / "screenshots"
    screenshots.rename(workspace.output_dir / "screenshots-original")
    victim_directory = tmp_path / "victim"
    victim_directory.mkdir()
    victim = victim_directory / "overview.png"
    victim.write_bytes(b"caller-owned")
    before = _digest(victim)
    screenshots.symlink_to(victim_directory, target_is_directory=True)

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.write_output_file("screenshots/overview.png", b"overwritten")

    assert _digest(victim) == before
    workspace.cleanup_runtime()


def test_finalize_removes_private_marker_and_allows_verified_reopen(
    tmp_path: Path,
) -> None:
    allowed = frozenset({"screenshots/overview.png", "demo-manifest.json"})
    manifest = b'{"generator":"project-memory-hub-demo-assets","schema_version":1}\n'
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )
    workspace.publish_output_files(
        {
            "screenshots/overview.png": b"synthetic screenshot",
            "demo-manifest.json": manifest,
        }
    )

    workspace.finalize_output()
    workspace.validate_output()

    assert not (workspace.output_dir / OUTPUT_MARKER_NAME).exists()
    assert {
        path.relative_to(workspace.output_dir).as_posix()
        for path in workspace.output_dir.rglob("*")
        if path.is_file()
    } == allowed
    workspace.cleanup_runtime()

    reopened = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime-reopened",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )
    reopened.validate_output()
    reopened.cleanup_incomplete_output()
    reopened.cleanup_runtime()
    assert not (reopened.output_dir / OUTPUT_MARKER_NAME).exists()


def test_finalize_rejects_incomplete_output_and_preserves_marker(tmp_path: Path) -> None:
    workspace = _prepare(tmp_path)
    workspace.write_output_file("overview.png", b"synthetic")

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.finalize_output()

    assert (workspace.output_dir / OUTPUT_MARKER_NAME).is_file()
    workspace.cleanup_runtime()


def test_finalize_rejects_nonfinite_manifest_json(tmp_path: Path) -> None:
    allowed = frozenset({"overview.png", "demo-manifest.json"})
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )
    workspace.publish_output_files(
        {
            "overview.png": b"synthetic",
            "demo-manifest.json": (
                b'{"generator":"project-memory-hub-demo-assets","schema_version":1,"value":NaN}\n'
            ),
        }
    )

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.finalize_output()

    assert (workspace.output_dir / OUTPUT_MARKER_NAME).is_file()
    workspace.cleanup_runtime()


def test_finalize_rejects_boolean_manifest_schema_version(tmp_path: Path) -> None:
    allowed = frozenset({"overview.png", "demo-manifest.json"})
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )
    workspace.publish_output_files(
        {
            "overview.png": b"synthetic",
            "demo-manifest.json": (
                b'{"generator":"project-memory-hub-demo-assets","schema_version":true}\n'
            ),
        }
    )

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.finalize_output()

    assert (workspace.output_dir / OUTPUT_MARKER_NAME).is_file()
    workspace.cleanup_runtime()


def test_publish_writes_manifest_last(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = frozenset({"screenshots/overview.png", "demo-manifest.json"})
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )
    real_replace = runtime_module._atomic_replace
    calls: list[str] = []

    def record_replace(descriptor: int, name: str, document: bytes) -> None:
        calls.append(name)
        real_replace(descriptor, name, document)

    monkeypatch.setattr(runtime_module, "_atomic_replace", record_replace)

    workspace.publish_output_files(
        {
            "demo-manifest.json": b"synthetic manifest",
            "screenshots/overview.png": b"synthetic screenshot",
        }
    )

    assert calls[-1] == "demo-manifest.json"
    workspace.cleanup_runtime()


def test_nested_publish_rolls_back_files_and_directories_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    allowed = frozenset({"screenshots/overview.png", "diagrams/flow.svg", "demo-manifest.json"})
    workspace = prepare_demo_workspace(
        runtime_dir=tmp_path / "runtime",
        output_dir=tmp_path / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names=allowed,
    )
    workspace.write_output_file("demo-manifest.json", b"old-manifest")
    real_replace = runtime_module._atomic_replace
    calls = 0

    def fail_second_write(descriptor: int, name: str, document: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic publish failure")
        real_replace(descriptor, name, document)

    monkeypatch.setattr(runtime_module, "_atomic_replace", fail_second_write)

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.publish_output_files(
            {
                "screenshots/overview.png": b"new-overview",
                "diagrams/flow.svg": b"new-flow",
                "demo-manifest.json": b"new-manifest",
            }
        )

    assert (workspace.output_dir / "demo-manifest.json").read_bytes() == b"old-manifest"
    assert not (workspace.output_dir / "screenshots").exists()
    assert not (workspace.output_dir / "diagrams").exists()
    workspace.cleanup_runtime()


def test_root_owned_sticky_shared_temp_metadata_is_trusted() -> None:
    metadata = SimpleNamespace(st_mode=stat.S_IFDIR | 0o1777, st_uid=0)

    assert runtime_module._is_trusted_temporary_root_metadata(
        metadata,
        current_uid=501,
    )
    assert not runtime_module._is_trusted_temporary_root_metadata(
        SimpleNamespace(st_mode=stat.S_IFDIR | 0o0777, st_uid=0),
        current_uid=501,
    )


def test_failure_restores_a_caller_owned_empty_output_directory(tmp_path: Path) -> None:
    output = tmp_path / "assets"
    output.mkdir(mode=0o700)
    workspace = _prepare(tmp_path, output_dir=output)

    workspace.cleanup_incomplete_output()

    assert output.is_dir()
    assert tuple(output.iterdir()) == ()


@pytest.mark.parametrize("kind", ["runtime", "output"])
def test_workspace_rejects_late_permission_weakening(tmp_path: Path, kind: str) -> None:
    workspace = _prepare(tmp_path)
    path = workspace.runtime_dir if kind == "runtime" else workspace.output_dir
    path.chmod(0o777)

    with pytest.raises(DemoPathError, match=f"demo {kind} rejected"):
        if kind == "runtime":
            workspace.validate_runtime()
        else:
            workspace.validate_output()


def test_runtime_validation_rejects_nested_hardlink_without_touching_source(
    tmp_path: Path,
) -> None:
    workspace = _prepare(tmp_path)
    workspace.paths.imports.mkdir(mode=0o700)
    nested = workspace.paths.imports / "nested"
    nested.mkdir(mode=0o700)
    external = tmp_path / "external.txt"
    external.write_bytes(b"caller-owned")
    before = _digest(external)
    os.link(external, nested / "memory.json")

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        workspace.validate_runtime()

    assert _digest(external) == before


def test_runtime_validation_rejects_nested_symlink_without_touching_target(
    tmp_path: Path,
) -> None:
    workspace = _prepare(tmp_path)
    workspace.paths.logs.mkdir(mode=0o700)
    external = tmp_path / "external.txt"
    external.write_bytes(b"caller-owned")
    before = _digest(external)
    (workspace.paths.logs / "latest.log").symlink_to(external)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        workspace.validate_runtime()

    assert _digest(external) == before


def test_runtime_validation_rejects_unknown_top_level_entry(tmp_path: Path) -> None:
    workspace = _prepare(tmp_path)
    (workspace.runtime_dir / "unexpected.txt").write_bytes(b"synthetic")

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        workspace.validate_runtime()


def test_runtime_validation_rejects_nested_world_writable_directory(
    tmp_path: Path,
) -> None:
    workspace = _prepare(tmp_path)
    workspace.paths.logs.mkdir(mode=0o700)
    nested = workspace.paths.logs / "unsafe"
    nested.mkdir(mode=0o700)
    nested.chmod(0o777)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        workspace.validate_runtime()


def test_output_rejects_allowlisted_hardlink_without_touching_external_file(
    tmp_path: Path,
) -> None:
    first = _prepare(tmp_path)
    first.cleanup_runtime()
    external = tmp_path / "external.txt"
    external.write_text("caller-owned", encoding="utf-8")
    before = _digest(external)
    os.link(external, first.output_dir / "overview.png")

    with pytest.raises(DemoPathError, match="demo output rejected"):
        _prepare(tmp_path)

    assert _digest(external) == before


def test_atomic_output_write_rejects_a_late_hardlink_without_truncating_source(
    tmp_path: Path,
) -> None:
    workspace = _prepare(tmp_path)
    external = tmp_path / "external.txt"
    external.write_text("caller-owned", encoding="utf-8")
    before = _digest(external)
    os.link(external, workspace.output_dir / "overview.png")

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.write_output_file("overview.png", b"synthetic")

    assert _digest(external) == before
    assert workspace.output_dir.joinpath("overview.png").samefile(external)
    workspace.cleanup_runtime()


def test_workspace_rejects_replaced_output_directory(tmp_path: Path) -> None:
    workspace = _prepare(tmp_path)
    original = tmp_path / "assets-original"
    workspace.output_dir.rename(original)
    workspace.output_dir.mkdir(mode=0o700)

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.write_output_file("overview.png", b"synthetic")

    assert tuple(workspace.output_dir.iterdir()) == ()
    assert (original / OUTPUT_MARKER_NAME).is_file()
    workspace.cleanup_runtime()


def test_publish_restores_the_previous_asset_set_after_a_partial_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _prepare(tmp_path)
    workspace.write_output_file("manifest.json", b"old-manifest")
    workspace.write_output_file("overview.png", b"old-overview")
    before = {name: workspace.output_dir.joinpath(name).read_bytes() for name in GENERATED_FILES}
    real_replace = runtime_module._atomic_replace
    calls = 0

    def fail_second_write(descriptor: int, name: str, document: bytes) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic publish failure")
        real_replace(descriptor, name, document)

    monkeypatch.setattr(runtime_module, "_atomic_replace", fail_second_write)

    with pytest.raises(DemoPathError, match="demo output rejected"):
        workspace.publish_output_files(
            {
                "manifest.json": b"new-manifest",
                "overview.png": b"new-overview",
            }
        )

    assert {
        name: workspace.output_dir.joinpath(name).read_bytes() for name in GENERATED_FILES
    } == before
    workspace.cleanup_runtime()


def test_cleanup_removes_only_new_owned_runtime_and_preserves_output(tmp_path: Path) -> None:
    caller_directory = tmp_path / "caller"
    caller_directory.mkdir()
    sentinel = caller_directory / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    workspace = _prepare(
        tmp_path,
        runtime_dir=caller_directory / "runtime",
        output_dir=caller_directory / "assets",
    )
    workspace.paths.imports.mkdir()
    workspace.paths.logs.mkdir()
    workspace.paths.database.write_bytes(b"synthetic")

    workspace.cleanup_runtime()

    assert not workspace.runtime_dir.exists()
    assert workspace.output_dir.is_dir()
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cleanup_rejects_a_directory_replaced_after_tree_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _prepare(tmp_path)
    workspace.paths.logs.mkdir(mode=0o700)
    (workspace.paths.logs / "owned.log").write_text("synthetic", encoding="utf-8")
    caller_directory = tmp_path / "caller-directory"
    caller_directory.mkdir(mode=0o700)
    caller_file = caller_directory / "keep.txt"
    caller_file.write_text("caller-owned", encoding="utf-8")
    before = _digest(caller_file)
    real_verify = runtime_module._verify_runtime_tree
    replaced = False

    def replace_after_validation(
        descriptor: int,
    ) -> dict[str, tuple[str, tuple[int, ...]]]:
        nonlocal replaced
        snapshot = real_verify(descriptor)
        if not replaced:
            replaced = True
            logs = workspace.paths.logs
            logs.rename(logs.with_name("logs-original"))
            caller_directory.rename(logs)
        return snapshot

    monkeypatch.setattr(runtime_module, "_verify_runtime_tree", replace_after_validation)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        workspace.cleanup_runtime()

    moved_caller_file = workspace.paths.logs / "keep.txt"
    assert moved_caller_file.is_file()
    assert _digest(moved_caller_file) == before


def test_rejects_caller_owned_empty_runtime_without_deleting_it(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir()

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(tmp_path, runtime_dir=runtime)

    assert runtime.is_dir()
    assert tuple(runtime.iterdir()) == ()


def test_rejects_runtime_created_between_preflight_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "runtime"
    real_open = runtime_module._open_or_create_leaf

    def inject_caller_directory(path: Path, *, kind: str) -> tuple[int, bool]:
        if kind == "runtime" and not path.exists():
            path.mkdir(mode=0o700)
        return real_open(path, kind=kind)

    monkeypatch.setattr(runtime_module, "_open_or_create_leaf", inject_caller_directory)

    with pytest.raises(DemoPathError, match="demo runtime rejected"):
        _prepare(tmp_path, runtime_dir=runtime)

    assert runtime.is_dir()
    assert tuple(runtime.iterdir()) == ()
    assert not (tmp_path / "assets").exists()
