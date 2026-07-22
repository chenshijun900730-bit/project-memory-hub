from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import scripts.verify_release_artifacts as release


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROJECT_FILE = PROJECT_ROOT / "pyproject.toml"
with PROJECT_FILE.open("rb") as _project_file:
    PROJECT = tomllib.load(_project_file)["project"]
PROJECT_NAME = str(PROJECT["name"])
PROJECT_VERSION = str(PROJECT["version"])
DIST_NAME = PROJECT_NAME.replace("-", "_")
WHEEL_NAME = f"{DIST_NAME}-{PROJECT_VERSION}-py3-none-any.whl"
SDIST_NAME = f"{DIST_NAME}-{PROJECT_VERSION}.tar.gz"


def _load_checksums() -> ModuleType:
    script = PROJECT_ROOT / "scripts/create_checksums.py"
    assert script.is_file(), "checksum generator must exist"
    spec = importlib.util.spec_from_file_location("create_checksums_contract", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_release_pair(dist: Path) -> tuple[Path, Path]:
    dist.mkdir()
    wheel = dist / WHEEL_NAME
    sdist = dist / SDIST_NAME
    wheel.write_bytes(b"verified wheel\n")
    sdist.write_bytes(b"verified sdist\n")
    return wheel, sdist


def _install_inspector(
    checksums: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    wheel: Path,
    sdist: Path,
) -> list[Path]:
    calls: list[Path] = []

    def inspect(dist: Path, *, project_file: Path = PROJECT_FILE) -> SimpleNamespace:
        del project_file
        calls.append(Path(dist))
        return SimpleNamespace(
            wheel=wheel.resolve(),
            sdist=sdist.resolve(),
            project_version=PROJECT_VERSION,
        )

    monkeypatch.setattr(checksums, "_inspect_release_artifacts", inspect)
    return calls


def _expected_manifest(*artifacts: Path) -> bytes:
    lines = [
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(artifacts, key=lambda candidate: candidate.name)
    ]
    return "".join(lines).encode("ascii")


def test_create_checksums_is_stably_sorted_and_byte_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    calls = _install_inspector(checksums, monkeypatch, wheel, sdist)

    first = checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")
    first_bytes = first.read_bytes()
    second = checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert first == second == dist / "SHA256SUMS"
    assert first_bytes == _expected_manifest(wheel, sdist)
    assert second.read_bytes() == first_bytes
    assert calls == [dist.resolve(), dist.resolve()]


def test_tag_must_exactly_match_pyproject_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text('[project]\nversion = "9.8.7"\n', encoding="utf-8")

    with pytest.raises(checksums.ChecksumError, match="tag_version_mismatch"):
        checksums.create_checksums(
            dist,
            tag=f"v{PROJECT_VERSION}",
            project_file=project_file,
        )

    assert not (dist / "SHA256SUMS").exists()


@pytest.mark.parametrize("mutation", ("extra", "missing", "duplicate"))
def test_release_directory_rejects_extra_missing_or_duplicate_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    if mutation == "extra":
        (dist / "release-notes.txt").write_text("unexpected", encoding="utf-8")
    elif mutation == "missing":
        sdist.unlink()
    else:
        (dist / "duplicate.whl").write_bytes(wheel.read_bytes())

    with pytest.raises(checksums.ChecksumError, match="release_artifact_inventory"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert not (dist / "SHA256SUMS").exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ("extra", "checksum_manifest_inventory"),
        ("missing", "checksum_manifest_inventory"),
        ("duplicate", "checksum_manifest_duplicate"),
        ("mismatch", "checksum_mismatch"),
    ),
)
def test_existing_manifest_rejects_extra_missing_duplicate_or_mismatched_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    manifest = checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")
    lines = manifest.read_text(encoding="ascii").splitlines(keepends=True)
    if mutation == "extra":
        lines.append(f"{'0' * 64}  unexpected.tar.gz\n")
    elif mutation == "missing":
        lines.pop()
    elif mutation == "duplicate":
        lines.append(lines[0])
    else:
        digest, separator, filename = lines[0].partition("  ")
        assert separator == "  "
        replacement = "0" if digest[0] != "0" else "1"
        lines[0] = f"{replacement}{digest[1:]}  {filename}"
    tampered = "".join(lines).encode("ascii")
    manifest.write_bytes(tampered)

    with pytest.raises(checksums.ChecksumError, match=message):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert manifest.read_bytes() == tampered


def test_unverified_artifacts_never_receive_a_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    _write_release_pair(dist)

    def reject(_dist: Path, *, project_file: Path = PROJECT_FILE) -> None:
        del project_file
        raise release.ReleaseArtifactError("wheel metadata version mismatch")

    monkeypatch.setattr(release, "inspect_release_artifacts", reject)

    with pytest.raises(checksums.ChecksumError, match="release_artifacts_not_verified"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert not (dist / "SHA256SUMS").exists()


def test_cli_rejects_environment_tag_that_does_not_match_project(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _write_release_pair(dist)
    environment = os.environ.copy()
    environment["GITHUB_REF_NAME"] = "v9.8.7"

    result = subprocess.run(
        [sys.executable, "scripts/create_checksums.py", str(dist)],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "tag_version_mismatch" in result.stderr
    assert not (dist / "SHA256SUMS").exists()


def test_repeat_reverifies_only_the_artifact_pair_outside_the_manifest_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    inspected: list[tuple[Path, frozenset[str]]] = []

    def inspect(candidate: Path, *, project_file: Path = PROJECT_FILE) -> SimpleNamespace:
        del project_file
        selected = Path(candidate).resolve()
        entries = sorted(selected.iterdir(), key=lambda path: path.name)
        inspected.append((selected, frozenset(path.name for path in entries)))
        if any(path.name == "SHA256SUMS" for path in entries):
            raise release.ReleaseArtifactError("expected exactly one wheel and one sdist")
        selected_wheel = next(path for path in entries if path.suffix == ".whl")
        selected_sdist = next(path for path in entries if path.name.endswith(".tar.gz"))
        return SimpleNamespace(
            wheel=selected_wheel.resolve(),
            sdist=selected_sdist.resolve(),
            project_version=PROJECT_VERSION,
        )

    monkeypatch.setattr(release, "inspect_release_artifacts", inspect)

    manifest = checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")
    first_bytes = manifest.read_bytes()
    checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert manifest.read_bytes() == first_bytes == _expected_manifest(wheel, sdist)
    assert len(inspected) == 2
    assert all(candidate != dist.resolve() for candidate, _names in inspected)
    assert all(names == {WHEEL_NAME, SDIST_NAME} for _candidate, names in inspected)


def test_uv_build_housekeeping_is_allowed_but_never_checksummed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    (dist / ".gitignore").write_bytes(b"*")
    _install_inspector(checksums, monkeypatch, wheel, sdist)

    manifest = checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert manifest.read_bytes() == _expected_manifest(wheel, sdist)
    assert b".gitignore" not in manifest.read_bytes()


def test_release_directory_symlink_is_rejected_before_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    real_dist = tmp_path / "real-dist"
    wheel, sdist = _write_release_pair(real_dist)
    dist = tmp_path / "dist"
    dist.symlink_to(real_dist, target_is_directory=True)
    calls = _install_inspector(checksums, monkeypatch, wheel, sdist)

    with pytest.raises(checksums.ChecksumError, match="release_artifact_inventory"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert calls == []
    assert not (real_dist / "SHA256SUMS").exists()


@pytest.mark.parametrize("kind", ("symlink", "directory", "unsafe_permissions"))
def test_existing_manifest_must_be_a_safe_owner_controlled_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    manifest = dist / "SHA256SUMS"
    if kind == "symlink":
        target = tmp_path / "outside-manifest"
        target.write_bytes(_expected_manifest(wheel, sdist))
        manifest.symlink_to(target)
    elif kind == "directory":
        manifest.mkdir()
    else:
        manifest.write_bytes(_expected_manifest(wheel, sdist))
        manifest.chmod(0o666)

    with pytest.raises(checksums.ChecksumError, match="checksum_manifest_inventory"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")


def test_existing_manifest_is_verified_without_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    manifest = dist / "SHA256SUMS"
    manifest.write_bytes(_expected_manifest(wheel, sdist))
    manifest.chmod(0o600)
    before = manifest.stat()

    def reject_rewrite(_source: object, _destination: object) -> None:
        raise AssertionError("an existing manifest must never be replaced")

    monkeypatch.setattr(checksums.os, "replace", reject_rewrite)

    result = checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    after = manifest.stat()
    assert result == manifest
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )


def test_release_inspection_staging_uses_the_system_private_temp_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    (dist / "SHA256SUMS").write_bytes(_expected_manifest(wheel, sdist))
    observed_directories: list[object] = []
    original_temporary_directory = checksums.tempfile.TemporaryDirectory

    def temporary_directory(*args: object, **kwargs: object) -> object:
        observed_directories.append(kwargs.get("dir"))
        return original_temporary_directory(*args, **kwargs)

    def inspect(candidate: Path, *, project_file: Path = PROJECT_FILE) -> SimpleNamespace:
        del project_file
        entries = tuple(Path(candidate).iterdir())
        staged_wheel = next(path for path in entries if path.suffix == ".whl")
        staged_sdist = next(path for path in entries if path.name.endswith(".tar.gz"))
        return SimpleNamespace(
            wheel=staged_wheel,
            sdist=staged_sdist,
            project_version=PROJECT_VERSION,
        )

    monkeypatch.setattr(checksums.tempfile, "TemporaryDirectory", temporary_directory)
    monkeypatch.setattr(release, "inspect_release_artifacts", inspect)

    checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert observed_directories
    assert observed_directories == [None]


@pytest.mark.parametrize("mutation", ("replacement", "same_inode_content_change"))
def test_artifact_identity_is_bound_across_release_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    original = wheel.stat()

    def inspect(_candidate: Path, *, project_file: Path = PROJECT_FILE) -> SimpleNamespace:
        del project_file
        if mutation == "replacement":
            replacement = dist / "replacement.whl"
            replacement.write_bytes(wheel.read_bytes())
            os.replace(replacement, wheel)
        else:
            wheel.write_bytes(b"changed wheel!\n")
            os.utime(wheel, ns=(original.st_atime_ns, original.st_mtime_ns))
        return SimpleNamespace(
            wheel=wheel.resolve(),
            sdist=sdist.resolve(),
            project_version=PROJECT_VERSION,
        )

    monkeypatch.setattr(checksums, "_inspect_release_artifacts", inspect)

    with pytest.raises(checksums.ChecksumError, match="release_artifact_changed"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert not (dist / "SHA256SUMS").exists()


def test_new_manifest_uses_private_same_directory_atomic_no_clobber_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    opened: list[tuple[Path, int, int]] = []
    links: list[tuple[Path, Path, bool]] = []
    original_open = checksums.os.open
    original_link = checksums.os.link

    def tracked_open(
        path: object, flags: int, mode: int = 0o777, *args: object, **kwargs: object
    ) -> int:
        candidate = Path(path) if isinstance(path, (str, os.PathLike)) else None
        if candidate is not None and candidate.name.startswith(".SHA256SUMS."):
            opened.append((candidate, flags, mode))
        return original_open(path, flags, mode, *args, **kwargs)

    def tracked_link(
        source: object,
        destination: object,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        links.append((Path(source), Path(destination), follow_symlinks))
        original_link(source, destination, follow_symlinks=follow_symlinks)

    def reject_replace(_source: object, _destination: object) -> None:
        raise AssertionError("manifest publication must not overwrite a path")

    monkeypatch.setattr(checksums.os, "open", tracked_open)
    monkeypatch.setattr(checksums.os, "link", tracked_link)
    monkeypatch.setattr(checksums.os, "replace", reject_replace)

    manifest = checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert len(opened) == 1
    temporary, flags, mode = opened[0]
    assert temporary.parent == dist.resolve()
    assert flags & os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        assert flags & os.O_NOFOLLOW
    assert mode == 0o600
    assert links == [(temporary, manifest, False)]
    assert not temporary.exists()
    assert manifest.stat().st_mode & 0o777 == 0o600


def test_atomic_manifest_write_failure_removes_private_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)

    def fail_link(
        _source: object,
        _destination: object,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del follow_symlinks
        raise OSError("simulated link failure")

    monkeypatch.setattr(checksums.os, "link", fail_link)

    with pytest.raises(checksums.ChecksumError, match="checksum_manifest_write_failed"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert not (dist / "SHA256SUMS").exists()
    assert not tuple(dist.glob(".SHA256SUMS.*.tmp"))


def test_manifest_created_during_atomic_write_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    manifest = dist / "SHA256SUMS"
    attacker_content = b"do not overwrite an existing manifest\n"
    original_link = checksums.os.link

    def racing_link(
        source: object,
        destination: object,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        manifest.write_bytes(attacker_content)
        original_link(source, destination, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(checksums.os, "link", racing_link)

    with pytest.raises(checksums.ChecksumError, match="checksum_manifest_inventory"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert manifest.read_bytes() == attacker_content
    assert not tuple(dist.glob(".SHA256SUMS.*.tmp"))


def test_manifest_read_revalidates_that_the_path_still_names_the_open_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    manifest = tmp_path / "SHA256SUMS"
    manifest.write_bytes(b"original manifest\n")
    manifest.chmod(0o600)
    original_inode = manifest.stat().st_ino
    replacement = tmp_path / "replacement"
    replacement.write_bytes(b"attacker manifest\n")
    replacement.chmod(0o600)
    original_read = checksums.os.read
    replaced = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not chunk and not replaced and os.fstat(descriptor).st_ino == original_inode:
            os.replace(replacement, manifest)
            replaced = True
        return chunk

    monkeypatch.setattr(checksums.os, "read", racing_read)

    with pytest.raises(checksums.ChecksumError, match="checksum_manifest_inventory"):
        checksums._read_manifest(manifest)

    assert replaced is True
    assert manifest.read_bytes() == b"attacker manifest\n"


def test_final_inventory_rejects_an_artifact_injected_after_manifest_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    original_verify = checksums._verify_existing_manifest

    def verify_then_inject(*args: object, **kwargs: object) -> None:
        original_verify(*args, **kwargs)
        (dist / "late-injected.whl").write_bytes(b"late artifact")

    monkeypatch.setattr(checksums, "_verify_existing_manifest", verify_then_inject)

    with pytest.raises(checksums.ChecksumError, match="release_artifact_inventory"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    assert not (dist / "SHA256SUMS").exists()
    assert (dist / "late-injected.whl").exists()


def test_custom_project_version_must_match_inspected_bundle_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text('[project]\nname = "project-memory-hub"\nversion = "9.8.7"\n')
    observed_project_files: list[Path] = []

    def inspect(candidate: Path, *, project_file: Path) -> SimpleNamespace:
        observed_project_files.append(project_file)
        return SimpleNamespace(
            wheel=wheel.resolve(),
            sdist=sdist.resolve(),
            project_version=PROJECT_VERSION,
        )

    monkeypatch.setattr(checksums, "_inspect_release_artifacts", inspect)

    with pytest.raises(checksums.ChecksumError, match="release_artifact_version_mismatch"):
        checksums.create_checksums(
            dist,
            tag="v9.8.7",
            project_file=project_file,
        )

    assert observed_project_files == [project_file]
    assert not (dist / "SHA256SUMS").exists()


@pytest.mark.parametrize("replacement", (False, True))
def test_failed_post_publish_validation_only_removes_the_manifest_it_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bool,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    manifest = dist / "SHA256SUMS"
    attacker_content = b"attacker replacement\n"

    def fail_validation(*_args: object, **_kwargs: object) -> None:
        if replacement:
            attacker = dist / "attacker-manifest"
            attacker.write_bytes(attacker_content)
            attacker.chmod(0o600)
            os.replace(attacker, manifest)
        raise checksums.ChecksumError("checksum_mismatch")

    monkeypatch.setattr(checksums, "_verify_existing_manifest", fail_validation)

    with pytest.raises(checksums.ChecksumError, match="checksum_mismatch"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")

    if replacement:
        assert manifest.read_bytes() == attacker_content
    else:
        assert not manifest.exists()


@pytest.mark.parametrize(
    ("target", "message"),
    (
        ("dist", "release_artifact_inventory"),
        ("wheel", "release_artifact_inventory"),
        ("manifest", "checksum_manifest_inventory"),
    ),
)
def test_release_paths_reject_group_or_world_writable_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    message: str,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    manifest = dist / "SHA256SUMS"
    if target == "manifest":
        manifest.write_bytes(_expected_manifest(wheel, sdist))
        manifest.chmod(0o666)
    elif target == "wheel":
        wheel.chmod(0o666)
    else:
        dist.chmod(0o777)

    with pytest.raises(checksums.ChecksumError, match=message):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")


def test_manifest_size_is_bounded_before_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    manifest = dist / "SHA256SUMS"
    manifest.write_bytes(b"x" * (64 * 1024 + 1))
    manifest.chmod(0o600)

    with pytest.raises(checksums.ChecksumError, match="checksum_manifest_inventory"):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")


@pytest.mark.parametrize(
    ("target", "message"),
    (
        ("artifact", "release_artifact_inventory"),
        ("manifest", "checksum_manifest_inventory"),
        ("housekeeping", "release_artifact_inventory"),
    ),
)
def test_release_files_reject_external_hardlinks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
    message: str,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    wheel, sdist = _write_release_pair(dist)
    _install_inspector(checksums, monkeypatch, wheel, sdist)
    if target == "artifact":
        selected = wheel
    elif target == "manifest":
        selected = dist / "SHA256SUMS"
        selected.write_bytes(_expected_manifest(wheel, sdist))
        selected.chmod(0o600)
    else:
        selected = dist / ".gitignore"
        selected.write_bytes(b"*")
    os.link(selected, tmp_path / f"outside-{target}")

    with pytest.raises(checksums.ChecksumError, match=message):
        checksums.create_checksums(dist, tag=f"v{PROJECT_VERSION}")


@pytest.mark.parametrize("mutation", ("missing", "malformed"))
def test_project_metadata_io_and_toml_failures_are_stable_checksum_errors(
    tmp_path: Path,
    mutation: str,
) -> None:
    checksums = _load_checksums()
    dist = tmp_path / "dist"
    _write_release_pair(dist)
    project_file = tmp_path / "missing.toml"
    if mutation == "malformed":
        project_file.write_text('[project\nversion = "9.8.7"\n')

    with pytest.raises(checksums.ChecksumError, match="project_metadata_unavailable"):
        checksums.create_checksums(dist, project_file=project_file)

    assert not (dist / "SHA256SUMS").exists()


def test_cli_reports_project_metadata_failure_without_a_traceback(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    _write_release_pair(dist)

    result = subprocess.run(
        [
            sys.executable,
            "scripts/create_checksums.py",
            str(dist),
            "--project-file",
            str(tmp_path / "missing.toml"),
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr.strip() == "project_metadata_unavailable"
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize("operation", ("rehash", "staging_copy"))
def test_artifact_reads_revalidate_that_the_path_still_names_the_open_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    checksums = _load_checksums()
    artifact = tmp_path / WHEEL_NAME
    artifact.write_bytes(b"original artifact")
    binding = checksums._artifact_binding(artifact)
    replacement = tmp_path / "replacement.whl"
    replacement.write_bytes(b"attacker artifact")
    original_read = checksums.os.read
    replaced = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        chunk = original_read(descriptor, size)
        if not chunk and not replaced and os.fstat(descriptor).st_ino == binding.inode:
            os.replace(replacement, artifact)
            replaced = True
        return chunk

    monkeypatch.setattr(checksums.os, "read", racing_read)
    destination = tmp_path / "staged.whl"

    with pytest.raises(checksums.ChecksumError, match="release_artifact_changed"):
        if operation == "rehash":
            checksums._artifact_binding(artifact, expected=binding)
        else:
            checksums._copy_bound_artifact(binding, destination)

    assert replaced is True
    assert artifact.read_bytes() == b"attacker artifact"
    assert not destination.exists()


def test_failed_artifact_staging_copy_removes_partial_regular_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checksums = _load_checksums()
    source = tmp_path / WHEEL_NAME
    source.write_bytes(b"artifact bytes")
    destination = tmp_path / "staging" / WHEEL_NAME
    destination.parent.mkdir()
    binding = checksums._artifact_binding(source)

    def fail_write(_descriptor: int, _content: bytes) -> None:
        raise OSError("simulated staging failure")

    monkeypatch.setattr(checksums, "_write_all", fail_write)

    with pytest.raises(checksums.ChecksumError, match="release_artifact_changed"):
        checksums._copy_bound_artifact(binding, destination)

    assert not destination.exists()
