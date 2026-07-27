from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from project_memory_hub.benchmark_fixtures import (
    BenchmarkFixtureError,
    build_fixture_repositories,
    load_task_specs,
    prepare_recall_bundle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"


def test_loads_eight_task_specs_with_relative_bounded_paths() -> None:
    tasks = load_task_specs(MANIFEST)

    assert [task.task_id for task in tasks] == [f"T{index:02d}" for index in range(1, 9)]
    assert {task.fixture_id for task in tasks} == {"ledger-tools", "release-kit"}
    assert [task.order for task in tasks].count("AB") == 4
    assert [task.order for task in tasks].count("BA") == 4
    for task in tasks:
        assert task.version == 2
        assert task.validation_argv[:3] == ("python3", "-m", "unittest")
        assert all(not path.is_absolute() and ".." not in path.parts for path in task.allowed_paths)


def test_fixture_repositories_are_deterministic_and_start_with_failing_tasks(
    tmp_path: Path,
) -> None:
    first = build_fixture_repositories(tmp_path / "first")
    second = build_fixture_repositories(tmp_path / "second")

    assert [(item.fixture_id, item.commit) for item in first] == [
        (item.fixture_id, item.commit) for item in second
    ]
    assert [item.fixture_id for item in first] == ["ledger-tools", "release-kit"]

    tasks = load_task_specs(MANIFEST)
    repositories = {item.fixture_id: item for item in first}
    for task in tasks:
        repository = repositories[task.fixture_id]
        result = subprocess.run(
            task.validation_argv,
            cwd=repository.path,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode != 0, f"{task.task_id} must start red"
        assert repository.commit == _git(repository.path, "rev-parse", "HEAD")
        assert _git(repository.path, "status", "--porcelain") == ""


def test_recall_contract_hashes_are_independent_of_private_root(tmp_path: Path) -> None:
    tasks = load_task_specs(MANIFEST)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    contract = manifest["recall_contract"]
    bundles = []
    for name in ("first", "second"):
        repositories = build_fixture_repositories(tmp_path / name / "fixtures")
        bundles.append(
            prepare_recall_bundle(
                tmp_path / name / "runtime",
                repositories,
                tasks,
                model_id="gpt-5.6-sol",
            )
        )

    assert {bundle.memory_snapshot_sha256 for bundle in bundles} == {
        contract["memory_snapshot_sha256"]
    }
    assert {recall.task_id: recall.brief_sha256 for recall in bundles[0].recalls} == contract[
        "brief_sha256_by_task"
    ]
    assert [(recall.task_id, recall.brief_sha256) for recall in bundles[0].recalls] == [
        (recall.task_id, recall.brief_sha256) for recall in bundles[1].recalls
    ]


def test_fixture_contains_recoverable_history_without_exposing_expected_patch(
    tmp_path: Path,
) -> None:
    repositories = {
        item.fixture_id: item for item in build_fixture_repositories(tmp_path / "fixtures")
    }
    tasks = load_task_specs(MANIFEST)

    for task in tasks:
        repository = repositories[task.fixture_id]
        note = repository.path / "docs" / "history" / f"{task.task_id}.md"
        document = note.read_text(encoding="utf-8")
        assert task.task_id in document
        assert "Current decision" in document
        assert "Failed route" in document
        assert "Verification" in document
        assert "Expected patch" not in document
        assert "```python" not in document


def test_fixture_builder_rejects_existing_symlink_or_nonempty_target(tmp_path: Path) -> None:
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(BenchmarkFixtureError, match="benchmark fixture rejected"):
        build_fixture_repositories(nonempty)

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    with pytest.raises(BenchmarkFixtureError, match="benchmark fixture rejected"):
        build_fixture_repositories(linked)


@pytest.mark.parametrize("private_path", (".git/HEAD", ".codex/config.toml"))
def test_task_specs_reject_private_metadata_as_allowed_change(
    tmp_path: Path,
    private_path: str,
) -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    manifest["tasks"][0]["allowed_changed_paths"] = [private_path]
    selected = tmp_path / "manifest.json"
    selected.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BenchmarkFixtureError, match="benchmark fixture rejected"):
        load_task_specs(selected)


def _git(repository: Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()
