from __future__ import annotations

import tomllib
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_benchmark_release_exemptions_are_exact_reviewed_blobs() -> None:
    expected = {
        "benchmarks/project-memory-effect/manifest.json": {
            "sha256": "a8d2c7696ad3a3035291dfeba0cf2c4cc300cf3a90bb4f04e5f046cd011c8643",
            "rules": ["token_like"],
        },
        "docs/superpowers/specs/2026-07-26-project-memory-effect-benchmark-design.md": {
            "sha256": "d620353af17ff9e9268239778f0aba6bcce18ad1182f37aed3d1e902e55bb950",
            "rules": ["token_like"],
        },
        "scripts/run_public_benchmark.py": {
            "sha256": "0d25bef4788b2088f9e53573628505367f724b6a9fab0bdf4984f7297075ea41",
            "rules": ["token_like"],
        },
        "tests/integration/test_benchmark_macos_sandbox.py": {
            "sha256": "a311b2c3a08684174f05ae500f43c4959ad1da0c85233ca823ed5de21d949a77",
            "rules": ["unknown_uuid"],
        },
        "tests/integration/test_public_benchmark.py": {
            "sha256": "3a69bf81f93c5ba96d5a90a5f3ba6028491d27263cf3c055e994922ae678c9f8",
            "rules": ["token_like"],
        },
        "tests/unit/test_benchmark_contracts.py": {
            "sha256": "bc275cb042fcd21d30062bea084a10ce055573f6ecc1abb75741ea7eae894e87",
            "rules": ["token_like"],
        },
    }
    document = tomllib.loads(
        (PROJECT_ROOT / "config/public-release-allowlist.toml").read_text(encoding="utf-8")
    )
    exemptions = {
        item["path"]: {"sha256": item["sha256"], "rules": item["rules"]}
        for item in document["exemptions"]
    }

    for path, contract in expected.items():
        assert exemptions.get(path) == contract
