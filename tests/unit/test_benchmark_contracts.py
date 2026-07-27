from __future__ import annotations

import json
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BENCHMARK_ROOT = PROJECT_ROOT / "benchmarks" / "project-memory-effect"
MANIFEST_PATH = BENCHMARK_ROOT / "manifest.json"
RESULT_SCHEMA_PATH = BENCHMARK_ROOT / "result.schema.json"
README_PATH = BENCHMARK_ROOT / "README.md"
GITIGNORE_PATH = PROJECT_ROOT / ".gitignore"


def _load_json(path: Path) -> dict[str, object]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def test_public_benchmark_manifest_preregisters_balanced_eight_task_protocol() -> None:
    manifest = _load_json(MANIFEST_PATH)

    assert manifest["schema_version"] == 1
    assert manifest["benchmark_id"] == "project-memory-effect-public-v1"
    assert manifest["status"] == "preregistered"
    assert manifest["order_policy"] == "task-id-parity-balanced-v1"
    assert manifest["recall_contract"] == {
        "brief_sha256_by_task": {
            "T01": "bf16ac7229f1e8ff5a400294f3aee9a1c24cdcfe7be472e4cf6356f830977f67",
            "T02": "cff26464cbd0310e66ecd44c0b8213c63e648b349218defbf3dae0267115fbe7",
            "T03": "98981c8a6afda01c9b1dc7deaa284698d1fbff8cb17eb01602ab88d4c73041c1",
            "T04": "994667bab2ff651635b21d5949d0b0c07ac88e8ab3666aba905dd1c6edd41d08",
            "T05": "f4fdd4e62a9174b5709c6219a6528cef6da23cebf6ac3d2ca21de2b55f5e0e86",
            "T06": "a8ce3beb39e8c43e096adb4b815d403553e57e743a2ecd278018f8cb5ed58a65",
            "T07": "6b2f164899848c4925c48ba165a0a781721013b54bc5f54f49a3035029f559c8",
            "T08": "1165ea83aa5d886af5ce926476b4ae931c8c2b240f7225558b4d482af40172a2",
        },
        "memory_snapshot_sha256": (
            "1239eefbb51ba4c0d316ab2bf0fdb3909b0acc365fc46a8956b95921a223f862"
        ),
    }
    public_contract = manifest["public_contract"]
    assert isinstance(public_contract, dict)
    assert set(public_contract["fixture_commit_by_fixture"]) == {
        "ledger-tools",
        "release-kit",
    }
    assert set(public_contract["workspace_sha256_by_fixture"]) == {
        "ledger-tools",
        "release-kit",
    }
    task_contract = public_contract["task_sha256_by_id"]
    assert isinstance(task_contract, dict)
    assert set(task_contract) == {f"T{index:02d}" for index in range(1, 9)}
    assert all(
        set(value)
        == {
            "baseline_prompt_sha256",
            "model_sandbox_profile_sha256",
            "pmh_prompt_sha256",
            "validator_sha256",
        }
        for value in task_contract.values()
    )

    execution = manifest["execution"]
    assert isinstance(execution, dict)
    assert execution == {
        "codex_cli_version": "codex-cli 0.146.0-alpha.3.1",
        "ephemeral": True,
        "ignore_user_config": True,
        "pilot_replicates": 1,
        "public_replicates": 3,
        "reasoning_effort": "low",
        "requested_model_id": "gpt-5.6-sol",
        "sandbox": "macos-external-seatbelt-v2",
        "timeout_seconds": 600,
    }

    conditions = manifest["conditions"]
    assert isinstance(conditions, dict)
    assert conditions == {
        "baseline": {
            "project_memory_brief": False,
            "repository_evidence_allowed": True,
        },
        "pmh": {
            "max_estimated_recall_tokens": 800,
            "project_memory_brief": True,
            "repository_evidence_allowed": True,
            "trusted_memory_only": True,
        },
    }

    tasks = manifest["tasks"]
    assert isinstance(tasks, list)
    assert [task["id"] for task in tasks] == [f"T{index:02d}" for index in range(1, 9)]
    assert {task["fixture"] for task in tasks} == {"ledger-tools", "release-kit"}
    assert [task["order"] for task in tasks].count("AB") == 4
    assert [task["order"] for task in tasks].count("BA") == 4
    assert [task["expected_tests"] for task in tasks] == [2, 1, 2, 2, 2, 1, 2, 2]

    for task in tasks:
        assert task["version"] == 2
        assert task["prompt"].startswith("Fix the preregistered ")
        assert task["validation_argv"][:3] == ["python3", "-m", "unittest"]
        assert task["allowed_changed_paths"]
        assert all(not str(value).startswith("/") for value in task["allowed_changed_paths"])


def test_public_result_schema_is_closed_and_requires_privacy_safe_evidence() -> None:
    schema = _load_json(RESULT_SCHEMA_PATH)

    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert schema["title"] == "Project Memory Hub public benchmark run"
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False

    properties = schema["properties"]
    assert isinstance(properties, dict)
    assert set(schema["required"]) == set(properties)
    assert {
        "schema_version",
        "benchmark_id",
        "artifact_scope",
        "runtime_evidence_class",
        "manifest_sha256",
        "harness_commit",
        "harness_sha256",
        "model_sandbox_profile_sha256",
        "run_id",
        "task_id",
        "task_version",
        "condition",
        "replicate",
        "order",
        "fixture_commit",
        "workspace_sha256",
        "memory_snapshot_sha256",
        "prompt_sha256",
        "validator_sha256",
        "validation_workspace_sha256",
        "validator_evidence_sha256",
        "codex_cli_version",
        "requested_model_id",
        "observed_model_id",
        "reasoning_effort",
        "success",
        "timed_out",
        "timeout_seconds",
        "invalid_reason",
        "tests_passed",
        "tests_total",
        "duration_ms",
        "tool_call_count",
        "wrong_route_count",
        "recall",
        "tokens",
        "contamination",
        "write_guard",
        "raw_evidence_sha256",
    } == set(properties)

    serialized = json.dumps(schema, sort_keys=True)
    for forbidden in (
        "absolute_path",
        "cwd",
        "prompt_text",
        "recall_text",
        "response_text",
        "session_id",
        "source_record_id",
        "thread_id",
        "tool_output",
    ):
        assert f'"{forbidden}"' not in serialized


def test_public_benchmark_readme_separates_pilot_and_public_claims() -> None:
    readme = README_PATH.read_text(encoding="utf-8")

    for marker in (
        "# Project Memory Effect Benchmark",
        "eight preregistered tasks",
        "baseline",
        "PMH",
        "one-replicate pilot",
        "three repetitions",
        "48 fresh Codex runs",
        "success is the primary outcome",
        "not a billing or subscription-quota measurement",
        "raw Codex event streams remain private",
        "foreign-project, foreign-source, and foreign-model",
        "macos-external-seatbelt-v2",
        "--dangerously-bypass-approvals-and-sandbox",
        "unique per-run `0700` launcher path",
        "cannot establish new network connections",
        "read the auth-only",
        "preregistered existing implementation file",
        "path the model sandbox never received",
        "parent-owned oracle",
        "never receives a final success-report",
        "streaming, bounded",
        "pins all eight recall-brief SHA-256 values",
        "`benchmark-state.sha256`",
        "official task, version, model, CLI, reasoning, timeout, replicate, and order protocol",
    ):
        assert marker in readme
    assert "saves tokens on every project" not in readme


def test_private_benchmark_runs_are_ignored() -> None:
    ignored = GITIGNORE_PATH.read_text(encoding="utf-8").splitlines()

    assert "benchmark-runs/" in ignored
