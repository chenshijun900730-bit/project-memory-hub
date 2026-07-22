import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import project_memory_hub.storage.path_identity as path_identity_module
import project_memory_hub.storage.projects as projects_module
from project_memory_hub.domain import ProjectCandidate, ProjectRecord
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.projects import ProjectRepository


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    return database


@pytest.fixture
def registry(database: Database) -> ProjectRepository:
    return ProjectRepository(database)


def candidate(
    path: Path,
    *,
    display_name: str | None = None,
    git_remote_fingerprint: str | None = None,
    manifest_fingerprint: str | None = None,
    create: bool = True,
) -> ProjectCandidate:
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return ProjectCandidate(
        canonical_path=path,
        display_name=display_name or path.name,
        git_root=path if git_remote_fingerprint else None,
        git_remote_fingerprint=git_remote_fingerprint,
        manifest_fingerprint=manifest_fingerprint,
        markers=(".git",) if git_remote_fingerprint else ("package.json",),
    )


def raw_project(database: Database, project_id: UUID) -> sqlite3.Row:
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select * from projects where project_id = ?", (str(project_id),)
        ).fetchone()
    assert row is not None
    return row


def physical_aliases(path: Path) -> tuple[Path, ...]:
    resolved = path.resolve(strict=True)
    metadata = resolved.stat()
    candidates = (
        Path("/System/Volumes/Data") / resolved.relative_to(resolved.anchor),
        Path(str(resolved).upper()),
        Path(f"/.vol/{metadata.st_dev}/{metadata.st_ino}"),
    )
    aliases: list[Path] = []
    for alias in candidates:
        try:
            if alias != resolved and os.path.samefile(alias, resolved):
                aliases.append(alias)
        except OSError:
            continue
    return tuple(dict.fromkeys(aliases))


def test_register_creates_stable_active_project_record(
    registry: ProjectRepository, database: Database, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_candidate = candidate(
        project_path,
        display_name="Project Display",
        git_remote_fingerprint="a" * 64,
        manifest_fingerprint="b" * 64,
    )

    record = registry.register(project_candidate)
    row = raw_project(database, record.project_id)

    assert isinstance(record, ProjectRecord)
    assert isinstance(record.project_id, UUID)
    assert record.canonical_path == project_path.resolve()
    assert record.display_name == "Project Display"
    assert record.discovery_status == "active"
    assert record.permission_status == "ok"
    assert record.last_observed_change is None
    assert row["project_id"] == str(record.project_id).lower()
    assert row["git_root"] == str(project_path.resolve())
    assert row["git_remote_fingerprint"] == "a" * 64
    assert row["manifest_fingerprint"] == "b" * 64
    assert row["path_device"] == project_path.stat().st_dev
    assert row["path_inode"] == project_path.stat().st_ino
    assert row["created_at"].endswith("Z")
    assert row["updated_at"].endswith("Z")
    assert datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")).tzinfo


def test_darwin_device_number_drift_preserves_same_path_inode_identity(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = tmp_path / "device-drift"
    project = registry.register(candidate(project_path))
    nested = project.canonical_path / "src"
    nested.mkdir()
    metadata = project.canonical_path.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project.project_id).lower()),
        )
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")

    resolved = registry.find_by_cwd(nested)
    current = registry.record_is_current(project)
    observed = registry.register(candidate(project.canonical_path, create=False))
    row = raw_project(database, project.project_id)

    assert resolved == project
    assert current is True
    assert observed.project_id == project.project_id
    assert (row["path_device"], row["path_inode"]) == (
        metadata.st_dev,
        metadata.st_ino,
    )


def test_path_identity_current_tolerates_only_darwin_device_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = tmp_path / "persisted-device-drift"
    project_path.mkdir()
    metadata = project_path.stat()

    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    assert path_identity_module.path_identity_is_current(
        project_path,
        metadata.st_dev + 1,
        metadata.st_ino,
    )
    assert not path_identity_module.path_identity_is_current(
        project_path,
        metadata.st_dev,
        metadata.st_ino + 1,
    )

    monkeypatch.setattr(path_identity_module.sys, "platform", "linux")
    assert not path_identity_module.path_identity_is_current(
        project_path,
        metadata.st_dev + 1,
        metadata.st_ino,
    )


def test_device_number_drift_remains_strict_outside_darwin(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = registry.register(candidate(tmp_path / "strict-device"))
    metadata = project.canonical_path.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project.project_id).lower()),
        )
    monkeypatch.setattr(path_identity_module.sys, "platform", "linux")

    assert registry.find_by_cwd(project.canonical_path) is None
    assert registry.record_is_current(project) is False
    with pytest.raises(ValueError, match="identity changed"):
        registry.register(candidate(project.canonical_path, create=False))


def test_relink_same_path_refreshes_a_darwin_device_number_drift(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = registry.register(candidate(tmp_path / "relink-device-drift"))
    metadata = project.canonical_path.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project.project_id).lower()),
        )
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")

    registry.relink(project.project_id, project.canonical_path)

    row = raw_project(database, project.project_id)
    assert (row["path_device"], row["path_inode"]) == (
        metadata.st_dev,
        metadata.st_ino,
    )


def test_register_requires_an_existing_directory(
    registry: ProjectRepository, tmp_path: Path
) -> None:
    with pytest.raises(FileNotFoundError):
        registry.register(candidate(tmp_path / "missing", create=False))

    regular_file = tmp_path / "file"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        registry.register(candidate(regular_file, create=False))


def test_observation_updates_persist_exact_epoch_and_equal_instant_reactivates(
    registry: ProjectRepository, database: Database, tmp_path: Path
) -> None:
    project = registry.register(candidate(tmp_path / "observed"))
    offset = timezone(timedelta(hours=8))
    observed = datetime(2026, 7, 13, 20, 30, 40, 123456, tzinfo=offset)
    selected_now = datetime(2026, 7, 13, 13, tzinfo=timezone.utc)

    assert registry.advance_last_observed_change(
        project.project_id,
        observed,
        as_of=selected_now,
    )
    first = raw_project(database, project.project_id)
    with database.transaction() as connection:
        connection.execute(
            "update projects set inactivity_state = 'inactive' where project_id = ?",
            (str(project.project_id),),
        )

    same_instant = observed.astimezone(timezone.utc)
    assert registry.advance_last_observed_change(
        project.project_id,
        same_instant,
        as_of=selected_now,
    )
    reactivated = raw_project(database, project.project_id)
    assert not registry.advance_last_observed_change(
        project.project_id,
        observed,
        as_of=selected_now,
    )

    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = same_instant - epoch
    expected_epoch_us = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    assert first["last_observed_change"] == "2026-07-13T12:30:40.123456Z"
    assert first["last_observed_change_epoch_us"] == expected_epoch_us
    assert reactivated["last_observed_change_epoch_us"] == expected_epoch_us
    assert reactivated["inactivity_state"] == "active"


def test_register_is_idempotent_by_canonical_real_path_and_updates_observation(
    registry: ProjectRepository, database: Database, tmp_path: Path
) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    first = registry.register(candidate(target, display_name="Old", manifest_fingerprint="a" * 64))
    before = raw_project(database, first.project_id)

    second = registry.register(
        candidate(
            alias,
            display_name="New",
            manifest_fingerprint="b" * 64,
            create=False,
        )
    )
    after = raw_project(database, second.project_id)

    assert second.project_id == first.project_id
    assert second.canonical_path == target.resolve()
    assert second.display_name == "New"
    assert after["manifest_fingerprint"] == "b" * 64
    assert after["created_at"] == before["created_at"]
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from projects").fetchone()[0] == 1


def test_register_never_merges_projects_by_fingerprint(
    registry: ProjectRepository, database: Database, tmp_path: Path
) -> None:
    common_remote = "a" * 64
    common_manifest = "b" * 64

    first = registry.register(
        candidate(
            tmp_path / "first",
            git_remote_fingerprint=common_remote,
            manifest_fingerprint=common_manifest,
        )
    )
    second = registry.register(
        candidate(
            tmp_path / "second",
            git_remote_fingerprint=common_remote,
            manifest_fingerprint=common_manifest,
        )
    )

    assert first.project_id != second.project_id
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from projects").fetchone()[0] == 2


def test_registry_uses_component_safe_longest_project_prefix(
    registry: ProjectRepository, tmp_path: Path
) -> None:
    outer = registry.register(candidate(tmp_path / "outer"))
    inner = registry.register(candidate(tmp_path / "outer" / "packages" / "inner"))
    registry.register(candidate(tmp_path / "app"))

    assert registry.find_by_cwd(inner.canonical_path / "src").project_id == inner.project_id  # type: ignore[union-attr]
    assert registry.find_by_cwd(outer.canonical_path / "docs").project_id == outer.project_id  # type: ignore[union-attr]
    assert registry.find_by_cwd(tmp_path / "apple" / "src") is None
    assert registry.find_by_cwd(tmp_path / "unrelated") is None


def test_find_by_cwd_rejects_retargetable_symlink_paths(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    first = registry.register(candidate(tmp_path / "first"))
    second = registry.register(candidate(tmp_path / "second"))
    alias = tmp_path / "working-copy"
    alias.symlink_to(first.canonical_path, target_is_directory=True)

    assert registry.find_by_cwd(alias / "src") is None

    alias.unlink()
    alias.symlink_to(second.canonical_path, target_is_directory=True)
    assert registry.find_by_cwd(alias / "src") is None


def test_find_by_cwd_rechecks_identity_after_in_call_retarget(
    registry: ProjectRepository,
    tmp_path: Path,
    monkeypatch,
) -> None:
    first = registry.register(candidate(tmp_path / "first"))
    second = registry.register(candidate(tmp_path / "second"))
    original_identity = projects_module._path_identity
    calls = 0

    def retarget_after_first_snapshot(path: Path):
        nonlocal calls
        result = original_identity(path)
        calls += 1
        if calls == 1:
            displaced = tmp_path / "first-displaced"
            first.canonical_path.rename(displaced)
            first.canonical_path.symlink_to(second.canonical_path, target_is_directory=True)
        return result

    monkeypatch.setattr(projects_module, "_path_identity", retarget_after_first_snapshot)

    assert registry.find_by_cwd(first.canonical_path / "src") is None


def test_find_by_cwd_rejects_noncanonical_symlink_parent_traversal(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    project = registry.register(candidate(tmp_path / "project"))
    outside = tmp_path / "outside" / "nested"
    outside.mkdir(parents=True)
    (project.canonical_path / "link").symlink_to(outside, target_is_directory=True)

    assert registry.find_by_cwd(project.canonical_path / "link" / "..") is None


def test_find_by_cwd_considers_only_enabled_projects(
    registry: ProjectRepository, database: Database, tmp_path: Path
) -> None:
    record = registry.register(candidate(tmp_path / "project"))
    with database.transaction() as connection:
        connection.execute(
            "update projects set enabled = 0 where project_id = ?",
            (str(record.project_id),),
        )

    assert registry.find_by_cwd(record.canonical_path / "src") is None


def test_relink_preserves_project_id_and_updates_canonical_path(
    registry: ProjectRepository, database: Database, tmp_path: Path
) -> None:
    original = registry.register(candidate(tmp_path / "original"))
    destination = tmp_path / "destination"
    destination.mkdir()
    before = raw_project(database, original.project_id)

    relinked = registry.relink(original.project_id, destination)
    after = raw_project(database, original.project_id)

    assert relinked.project_id == original.project_id
    assert relinked.canonical_path == destination.resolve()
    assert registry.find_by_cwd(destination / "src") == relinked
    assert registry.find_by_cwd(original.canonical_path / "src") is None
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] >= before["updated_at"]


def test_relink_rejects_unknown_collision_and_missing_destination(
    registry: ProjectRepository, tmp_path: Path
) -> None:
    first = registry.register(candidate(tmp_path / "first"))
    second = registry.register(candidate(tmp_path / "second"))
    free_destination = tmp_path / "free"
    free_destination.mkdir()

    with pytest.raises(KeyError):
        registry.relink(uuid4(), free_destination)
    with pytest.raises(ValueError, match="already assigned"):
        registry.relink(first.project_id, second.canonical_path)
    with pytest.raises(FileNotFoundError):
        registry.relink(first.project_id, tmp_path / "missing")
    regular_file = tmp_path / "file"
    regular_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        registry.relink(first.project_id, regular_file)


def test_register_reuses_project_id_for_a_physical_path_alias(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    first = registry.register(candidate(tmp_path / "first"))
    aliases = physical_aliases(first.canonical_path)
    if not aliases:
        pytest.skip("filesystem path aliases are unavailable")

    duplicate_observation = registry.register(candidate(aliases[0], create=False))

    assert duplicate_observation.project_id == first.project_id


def test_register_reuses_a_live_physical_alias_after_darwin_device_drift(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = registry.register(candidate(tmp_path / "device-drift-alias"))
    aliases = physical_aliases(first.canonical_path)
    if not aliases:
        pytest.skip("filesystem path aliases are unavailable")
    metadata = first.canonical_path.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(first.project_id).lower()),
        )
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")

    duplicate_observation = registry.register(candidate(aliases[0], create=False))

    assert duplicate_observation.project_id == first.project_id


def test_register_backfills_an_untrusted_legacy_path_identity(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            ("00000000-0000-0000-0000-000000000001", str(project), "Legacy"),
        )

    observed = registry.register(candidate(project, create=False))
    row = raw_project(database, observed.project_id)

    assert (row["path_device"], row["path_inode"]) == (
        project.stat().st_dev,
        project.stat().st_ino,
    )


def test_register_claims_a_legacy_batch_without_quadratic_identity_reads(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_paths = tuple(tmp_path / f"legacy-{index}" for index in range(24))
    for project_path in project_paths:
        project_path.mkdir()
    with database.transaction() as connection:
        connection.executemany(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            (
                (str(uuid4()), str(project_path), project_path.name)
                for project_path in project_paths
            ),
        )
    real_identity = projects_module.complete_directory_identity
    identity_reads = 0

    def counted_identity(path: Path):
        nonlocal identity_reads
        identity_reads += 1
        return real_identity(path)

    monkeypatch.setattr(projects_module, "complete_directory_identity", counted_identity)

    with registry.discovery_batch():
        for project_path in project_paths:
            registry.register(candidate(project_path, create=False))

    assert identity_reads <= len(project_paths) * 6


def test_register_does_not_claim_unobserved_legacy_project_paths(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    unobserved = tmp_path / "unobserved"
    unobserved.mkdir()
    registered_path = unobserved.resolve(strict=True)
    legacy_project_id = uuid4()
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            (str(legacy_project_id), str(registered_path), "Unobserved"),
        )
    registered_path.rename(tmp_path / "unobserved-moved")
    registered_path.mkdir()

    registry.register(candidate(tmp_path / "observed"))

    row = raw_project(database, legacy_project_id)
    assert (row["path_device"], row["path_inode"]) == (None, None)
    assert registry.find_by_cwd(registered_path) is None


def test_discovery_batch_rebuilds_legacy_identity_cache_between_batches(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    recovered_path = (tmp_path / "recovered").resolve(strict=False)
    legacy_project_id = uuid4()
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            (str(legacy_project_id), str(recovered_path), "Recovered"),
        )

    observed = tmp_path / "observed"
    observed.mkdir()
    with registry.discovery_batch():
        registry.register(candidate(observed, create=False))

    recovered_path.mkdir()
    aliases = physical_aliases(recovered_path)
    if not aliases:
        pytest.skip("filesystem path aliases are unavailable")
    with registry.discovery_batch():
        recovered = registry.register(candidate(aliases[0], create=False))

    assert recovered.project_id == legacy_project_id


def test_discovery_batch_does_not_rescan_missing_legacy_rows_for_each_candidate(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_paths = tuple(tmp_path / f"missing-{index}" for index in range(24))
    candidates = tuple(tmp_path / f"candidate-{index}" for index in range(24))
    for project_path in candidates:
        project_path.mkdir()
    with database.transaction() as connection:
        connection.executemany(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            (
                (str(uuid4()), str(project_path), project_path.name)
                for project_path in missing_paths
            ),
        )
    real_identity = projects_module.complete_directory_identity
    identity_reads = 0

    def counted_identity(path: Path):
        nonlocal identity_reads
        identity_reads += 1
        return real_identity(path)

    monkeypatch.setattr(projects_module, "complete_directory_identity", counted_identity)

    with registry.discovery_batch():
        for project_path in candidates:
            registry.register(candidate(project_path, create=False))

    assert identity_reads <= len(missing_paths) + len(candidates) * 4


def test_relink_recovers_a_missing_legacy_project(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    missing_path = tmp_path / "missing"
    legacy_project_id = uuid4()
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            (str(legacy_project_id), str(missing_path), "Missing"),
        )
    destination = tmp_path / "destination"
    destination.mkdir()

    recovered = registry.relink(legacy_project_id, destination)

    assert recovered.project_id == legacy_project_id
    row = raw_project(database, legacy_project_id)
    assert row["canonical_path"] == str(destination)
    assert (row["path_device"], row["path_inode"]) == (
        destination.stat().st_dev,
        destination.stat().st_ino,
    )


def test_register_does_not_merge_legacy_rows_that_share_a_physical_identity(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    aliases = physical_aliases(project)
    if not aliases:
        pytest.skip("filesystem path aliases are unavailable")
    with database.transaction() as connection:
        connection.executemany(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            (
                (str(uuid4()), str(project), "Legacy"),
                (str(uuid4()), str(aliases[0]), "Legacy alias"),
            ),
        )

    with pytest.raises(ValueError, match="multiple projects"):
        registry.register(candidate(project, create=False))


def test_register_does_not_silently_follow_a_moved_project_identity(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    original = registry.register(candidate(tmp_path / "original"))
    moved = tmp_path / "moved"
    original.canonical_path.rename(moved)

    with pytest.raises(ValueError, match="relink"):
        registry.register(candidate(moved, create=False))


def test_register_rejects_a_replacement_at_the_same_canonical_path(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    original = registry.register(candidate(tmp_path / "project"))
    displaced = tmp_path / "displaced"
    original.canonical_path.rename(displaced)
    original.canonical_path.mkdir()

    with pytest.raises(ValueError, match="identity changed"):
        registry.register(candidate(original.canonical_path, create=False))

    row = raw_project(database, original.project_id)
    assert (row["path_device"], row["path_inode"]) == (
        displaced.stat().st_dev,
        displaced.stat().st_ino,
    )


def test_relink_rejects_a_physical_alias_owned_by_another_project(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    first = registry.register(candidate(tmp_path / "first"))
    second = registry.register(candidate(tmp_path / "second"))
    aliases = physical_aliases(first.canonical_path)
    if not aliases:
        pytest.skip("filesystem path aliases are unavailable")

    with pytest.raises(ValueError, match="already assigned"):
        registry.relink(second.project_id, aliases[0])

    assert raw_project(database, second.project_id)["canonical_path"] == str(second.canonical_path)


def test_find_by_cwd_rejects_a_registered_directory_replaced_after_commit(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    original = registry.register(candidate(tmp_path / "project"))
    displaced = tmp_path / "project-displaced"
    original.canonical_path.rename(displaced)
    original.canonical_path.mkdir()

    assert registry.find_by_cwd(original.canonical_path / "src") is None


def test_find_by_cwd_exact_path_does_not_rescan_unrelated_projects(
    registry: ProjectRepository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = registry.register(candidate(tmp_path / "selected"))
    for index in range(24):
        registry.register(candidate(tmp_path / f"unrelated-{index}"))
    real_identity = projects_module.complete_directory_identity
    identity_reads = 0

    def counted_identity(path: Path):
        nonlocal identity_reads
        identity_reads += 1
        return real_identity(path)

    monkeypatch.setattr(projects_module, "complete_directory_identity", counted_identity)

    resolved = registry.find_by_cwd(selected.canonical_path)

    assert resolved is not None
    assert resolved.project_id == selected.project_id
    assert identity_reads <= 2


def test_find_by_cwd_does_not_fall_back_to_an_outer_project_on_identity_failure(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    outer = registry.register(candidate(tmp_path / "workspace"))
    inner = registry.register(candidate(outer.canonical_path / "packages" / "inner"))
    displaced = tmp_path / "inner-displaced"
    inner.canonical_path.rename(displaced)
    inner.canonical_path.mkdir()

    assert registry.find_by_cwd(inner.canonical_path / "src") is None


def test_find_by_cwd_does_not_fall_back_to_outer_when_inner_is_disabled(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    outer = registry.register(candidate(tmp_path / "workspace"))
    inner = registry.register(candidate(outer.canonical_path / "packages" / "inner"))
    nested = inner.canonical_path / "src"
    nested.mkdir()
    registry.set_enabled(inner.project_id, False)

    assert registry.find_by_cwd(inner.canonical_path) is None
    assert registry.find_by_cwd(nested) is None


def test_find_by_cwd_does_not_fall_back_when_the_inner_path_used_an_alias(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    outer = registry.register(candidate(tmp_path / "workspace"))
    inner_path = outer.canonical_path / "packages" / "inner"
    inner_path.mkdir(parents=True)
    aliases = physical_aliases(inner_path)
    if not aliases:
        pytest.skip("filesystem path aliases are unavailable")
    registry.register(candidate(aliases[0], create=False))
    displaced = tmp_path / "inner-displaced"
    inner_path.rename(displaced)
    inner_path.mkdir()

    assert registry.find_by_cwd(inner_path / "src") is None


def test_find_by_cwd_rejects_an_untrusted_legacy_identity(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    project = tmp_path / "legacy"
    project.mkdir()
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            ("00000000-0000-0000-0000-000000000002", str(project), "Legacy"),
        )

    assert registry.find_by_cwd(project / "src") is None


def test_find_by_cwd_rejects_a_relink_destination_replaced_after_commit(
    registry: ProjectRepository,
    tmp_path: Path,
) -> None:
    original = registry.register(candidate(tmp_path / "original"))
    destination = tmp_path / "destination"
    destination.mkdir()
    relinked = registry.relink(original.project_id, destination)
    displaced = tmp_path / "destination-displaced"
    destination.rename(displaced)
    destination.mkdir()

    assert registry.find_by_cwd(relinked.canonical_path / "src") is None


def test_relink_rejects_destination_replaced_by_symlink_after_validation(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
) -> None:
    original = registry.register(candidate(tmp_path / "original"))
    destination = tmp_path / "destination"
    destination.mkdir()
    validated_destination = destination.resolve(strict=True)
    outside = tmp_path / "outside"
    outside.mkdir()

    destination.rmdir()
    destination.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="relink destination"):
        registry.relink(original.project_id, validated_destination)

    assert raw_project(database, original.project_id)["canonical_path"] == str(
        original.canonical_path
    )


def test_relink_rolls_back_when_destination_changes_during_transaction(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = registry.register(candidate(tmp_path / "original"))
    destination = tmp_path / "destination"
    destination.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    real_select_record = projects_module._select_record

    def swap_after_update(connection: sqlite3.Connection, project_id: str) -> sqlite3.Row:
        row = real_select_record(connection, project_id)
        destination.rmdir()
        destination.symlink_to(outside, target_is_directory=True)
        return row

    monkeypatch.setattr(projects_module, "_select_record", swap_after_update)

    with pytest.raises(ValueError, match="relink destination"):
        registry.relink(original.project_id, destination)

    assert raw_project(database, original.project_id)["canonical_path"] == str(
        original.canonical_path
    )


def test_relink_rolls_back_when_the_transaction_fails(
    registry: ProjectRepository,
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = registry.register(candidate(tmp_path / "original"))
    destination = tmp_path / "destination"
    destination.mkdir()

    def fail_after_update(connection, project_id: str):
        updated = connection.execute(
            "select canonical_path from projects where project_id = ?", (project_id,)
        ).fetchone()
        assert updated["canonical_path"] == str(destination.resolve())
        raise RuntimeError("synthetic post-update failure")

    monkeypatch.setattr(projects_module, "_select_record", fail_after_update)

    with pytest.raises(RuntimeError, match="synthetic post-update failure"):
        registry.relink(original.project_id, destination)

    assert raw_project(database, original.project_id)["canonical_path"] == str(
        original.canonical_path
    )
    assert registry.find_by_cwd(original.canonical_path / "src") == original
    assert registry.find_by_cwd(destination / "src") is None
