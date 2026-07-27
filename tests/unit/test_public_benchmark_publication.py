from __future__ import annotations

import json
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "results" / "public-v1.jsonl"
SUMMARY = (
    PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "results" / "public-v1-summary.json"
)
REPORT = PROJECT_ROOT / "docs" / "benchmark.md"


def test_public_v1_artifacts_and_report_are_complete_and_bounded() -> None:
    assert RESULTS.is_file()
    assert SUMMARY.is_file()
    assert REPORT.is_file()

    rows = [json.loads(line) for line in RESULTS.read_text(encoding="utf-8").splitlines()]
    summary = json.loads(SUMMARY.read_text(encoding="utf-8"))
    report = REPORT.read_text(encoding="utf-8")

    assert len(rows) == 48
    assert len({(row["task_id"], row["replicate"], row["condition"]) for row in rows}) == 48
    assert {row["harness_commit"] for row in rows} == {"f70ea19530d3032840a7bf3ccdb6065db13b3d81"}
    assert summary["schema_version"] == 2
    assert summary["analysis_unit"] == "task"
    assert summary["run_count"] == 48
    assert summary["comparable_token_task_count"] == 8
    assert summary["comparable_duration_task_count"] == 8
    assert summary["median_task_total_token_change"] == pytest.approx(-0.07828694867309417)
    assert summary["median_task_duration_change"] == pytest.approx(-0.1117539331304647)
    assert summary["task_bootstrap_95pct_total_token_change"] == {
        "lower": pytest.approx(-0.2904860930595269),
        "method": "task_median_percentile_bootstrap_v1",
        "resamples": 10_000,
        "upper": pytest.approx(0.23509965735244437),
    }
    assert summary["task_bootstrap_95pct_duration_change"] == {
        "lower": pytest.approx(-0.25957811568276684),
        "method": "task_median_percentile_bootstrap_v1",
        "resamples": 10_000,
        "upper": pytest.approx(0.14227592825378804),
    }
    assert len(summary["per_task"]) == 8
    assert "../benchmarks/project-memory-effect/results/public-v1.jsonl" in report
    assert "../benchmarks/project-memory-effect/results/public-v1-summary.json" in report
    assert (
        "https://github.com/chenshijun900730-bit/project-memory-hub/commit/"
        "f70ea19530d3032840a7bf3ccdb6065db13b3d81"
    ) in report
    assert "observed_model_id=null" in report
    assert "not billing usage" in report
    assert "intervals include zero" in report


def test_readmes_link_the_public_benchmark_without_overclaiming() -> None:
    english = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (PROJECT_ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    changelog = (PROJECT_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    releasing = (PROJECT_ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")

    assert "[Public benchmark: protocol, results, and limits](docs/benchmark.md)" in english
    assert "7.83%" in english
    assert "11.18%" in english
    assert "intervals include zero" in english
    assert "[公开基准：协议、结果与限制](docs/benchmark.md)" in chinese
    assert "7.83%" in chinese
    assert "11.18%" in chinese
    assert "区间包含 0" in chinese
    assert "48-run preregistered synthetic benchmark" in changelog
    assert "[public benchmark report](docs/benchmark.md)" in changelog
    assert "[public benchmark report](benchmark.md)" in releasing
    assert "Never attach the private benchmark root" in releasing
