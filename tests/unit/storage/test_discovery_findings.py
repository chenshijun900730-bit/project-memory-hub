from __future__ import annotations

from pathlib import Path

import pytest

from project_memory_hub.domain import DiscoveryIssue, DiscoveryResult, ProjectCandidate
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.discovery import DiscoveryFindingRepository


def _candidate(path: Path, fingerprint: str) -> ProjectCandidate:
    path.mkdir()
    return ProjectCandidate(
        canonical_path=path,
        display_name=path.name,
        manifest_fingerprint=fingerprint,
        markers=("package.json",),
    )


def test_sync_replaces_discovery_issues_and_groups_duplicate_fingerprints(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = DiscoveryFindingRepository(database)
    first = _candidate(tmp_path / "first", "a" * 64)
    second = _candidate(tmp_path / "second", "a" * 64)
    unique = _candidate(tmp_path / "unique", "b" * 64)
    blocked = tmp_path / "blocked"

    repository.sync(
        DiscoveryResult(
            candidates=(first, second, unique),
            issues=(
                DiscoveryIssue(
                    path=blocked,
                    code="blocked_permission",
                    remediation="Grant access and retry discovery.",
                ),
            ),
        )
    )
    snapshot = repository.snapshot()

    assert [(item.path, item.code) for item in snapshot.issues] == [(blocked, "blocked_permission")]
    assert snapshot.issues[0].affected_capability == "project_discovery"
    assert len(snapshot.duplicates) == 1
    assert snapshot.duplicates[0].fingerprint_kind == "manifest"
    assert snapshot.duplicates[0].candidate_paths == (
        first.canonical_path,
        second.canonical_path,
    )

    repository.sync(DiscoveryResult(candidates=(), issues=()))
    assert repository.snapshot().issues == ()
    assert repository.snapshot().duplicates == ()


def test_invalid_discovery_snapshot_rolls_back_without_erasing_last_good_state(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = DiscoveryFindingRepository(database)
    good = DiscoveryIssue(
        path=tmp_path / "blocked",
        code="blocked_permission",
        remediation="Grant access and retry discovery.",
    )
    repository.sync(DiscoveryResult(candidates=(), issues=(good,)))

    with pytest.raises(ValueError, match="discovery issue code"):
        repository.sync(
            DiscoveryResult(
                candidates=(),
                issues=(
                    DiscoveryIssue(
                        path=tmp_path / "unsafe",
                        code="unbounded_code",
                        remediation="unsafe",
                    ),
                ),
            )
        )

    assert repository.snapshot().issues[0].path == good.path
