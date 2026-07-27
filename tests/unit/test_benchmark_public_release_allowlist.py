from __future__ import annotations

import hashlib
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
            "sha256": "5d23e98af4952a6fe0e40283e1c5616497b5d3732b729486b9dd83a89e5cd34c",
            "rules": ["token_like"],
        },
            "tests/integration/test_benchmark_macos_sandbox.py": {
                "sha256": "b5e88bebee3eaaa9843b64fcdad1e394e2e6327463efe3af9b3dd3a12b2d5c4b",
                "rules": ["unknown_uuid"],
            },
            "tests/integration/test_public_benchmark.py": {
                "sha256": "c664863d6061c4c34919451efbb8844a6f5b7eb5447a84a1665f24d1e748fcd7",
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
        assert hashlib.sha256((PROJECT_ROOT / path).read_bytes()).hexdigest() == contract["sha256"]
