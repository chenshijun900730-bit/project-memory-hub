from __future__ import annotations

import json
from pathlib import Path

from project_memory_hub.benchmark_fixtures import (
    build_fixture_repositories,
    load_task_specs,
    prepare_recall_bundle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"
MODEL_ID = "gpt-5.6-sol"


def test_frozen_recall_bundle_is_bounded_relevant_isolated_and_read_only(
    tmp_path: Path,
) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)

    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )

    assert bundle.model_id == MODEL_ID
    assert bundle.memory_snapshot_unchanged is True
    assert bundle.pending_count_before == bundle.pending_count_after == 0
    assert bundle.trusted_count_before == bundle.trusted_count_after
    assert bundle.trusted_count_before > 40
    assert len(bundle.memory_snapshot_sha256) == 64
    assert [recall.task_id for recall in bundle.recalls] == [
        f"T{index:02d}" for index in range(1, 9)
    ]

    tasks_by_id = {task.task_id: task for task in tasks}
    for recall in bundle.recalls:
        task = tasks_by_id[recall.task_id]
        assert 0 < recall.estimated_tokens <= 800
        assert recall.selected_count > 0
        assert recall.omitted_count > 0
        assert recall.candidate_estimated_tokens > 4_000
        assert task.task_id in recall.text
        assert "Verified command" in recall.text
        assert task.allowed_paths[0].as_posix() in recall.text
        assert len(recall.brief_sha256) == 64
        assert recall.project_canary_hits == 0
        assert recall.source_canary_hits == 0
        assert recall.model_canary_hits == 0


def test_recall_bundle_is_root_independent_and_matches_manifest_contract(
    tmp_path: Path,
) -> None:
    tasks = load_task_specs(MANIFEST)
    first_repositories = build_fixture_repositories(tmp_path / "first-fixtures")
    second_repositories = build_fixture_repositories(tmp_path / "second-fixtures")

    first = prepare_recall_bundle(
        tmp_path / "first-runtime",
        first_repositories,
        tasks,
        model_id=MODEL_ID,
    )
    second = prepare_recall_bundle(
        tmp_path / "second-runtime",
        second_repositories,
        tasks,
        model_id=MODEL_ID,
    )

    assert [
        (
            recall.task_id,
            recall.text,
            recall.estimated_tokens,
            recall.selected_count,
            recall.omitted_count,
            recall.candidate_estimated_tokens,
        )
        for recall in first.recalls
    ] == [
        (
            recall.task_id,
            recall.text,
            recall.estimated_tokens,
            recall.selected_count,
            recall.omitted_count,
            recall.candidate_estimated_tokens,
        )
        for recall in second.recalls
    ]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    expected_snapshot = manifest["recall_contract"]["memory_snapshot_sha256"]
    assert first.memory_snapshot_sha256 == second.memory_snapshot_sha256
    assert first.memory_snapshot_sha256 == expected_snapshot
