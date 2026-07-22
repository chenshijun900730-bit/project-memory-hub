from __future__ import annotations

import tomllib
from pathlib import Path


def test_python_coverage_gate_measures_branches() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    configuration = tomllib.loads((repository_root / "pyproject.toml").read_text(encoding="utf-8"))

    coverage = configuration["tool"]["coverage"]

    assert coverage["run"]["branch"] is True
    assert coverage["report"]["precision"] >= 2
