from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import sys
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "run_public_benchmark.py"


def _load_cli_module() -> ModuleType:
    name = "pmh_public_benchmark_artifact_contract_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("command", "arguments"),
    (
        (
            "run",
            (
                "--private-root",
                "/tmp/private",
                "--replicates",
                "1",
                "--task",
                "T01",
            ),
        ),
        ("verify-public", ("--input", "/tmp/results.jsonl")),
        (
            "summarize",
            ("--input", "/tmp/results.jsonl", "--output", "/tmp/summary.json"),
        ),
    ),
)
def test_artifact_commands_require_an_explicit_scope(
    command: str,
    arguments: tuple[str, ...],
) -> None:
    module = _load_cli_module()
    parser = module._parser()

    with pytest.raises(SystemExit):
        parser.parse_args((command, *arguments))

    parsed = parser.parse_args((command, *arguments, "--scope", "single-pair-pilot"))
    assert parsed.scope == "single-pair-pilot"


def test_public_rows_label_runtime_evidence_as_self_reported() -> None:
    schema = json.loads(
        (PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "result.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert schema["properties"]["runtime_evidence_class"] == {"const": "self-reported"}
    assert "runtime_evidence_class" in schema["required"]


def test_summary_classifies_test_counts_by_row_outcome() -> None:
    module = _load_cli_module()

    classification = module._summary_evidence_classification()

    assert "tests_passed" not in classification["contract_verified"]
    assert "tests_total" not in classification["contract_verified"]
    assert classification["success_row_contract_verified"] == [
        "tests_passed",
        "tests_total",
    ]
    assert classification["failure_row_self_reported"] == [
        "tests_passed",
        "tests_total",
    ]


def test_single_pair_scope_requires_exact_preregistered_protocol_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, results = _contract_fixture(monkeypatch)

    module._validate_official_public_results(results, "single-pair-pilot")

    unexpected = copy.deepcopy(results[0])
    unexpected["task_id"] = "T02"
    unexpected["run_id"] = "run-t02-r1-baseline"
    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._validate_official_public_results(
            [*results, unexpected],
            "single-pair-pilot",
        )


def test_public_v1_scope_requires_all_48_protocol_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, results = _contract_fixture(monkeypatch)

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._validate_official_public_results(results, "public-v1")


def test_scope_protocols_are_fixed_to_2_16_and_48_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, _ = _contract_fixture(monkeypatch)
    tasks, execution, _, _ = module._manifest_snapshot(module.DEFAULT_MANIFEST)

    single = module._scope_protocol_keys(
        "single-pair-pilot",
        tasks,
        execution,
    )
    full = module._scope_protocol_keys("full-pilot", tasks, execution)
    public = module._scope_protocol_keys("public-v1", tasks, execution)

    assert single == {
        ("T01", 1, "baseline"),
        ("T01", 1, "pmh"),
    }
    assert len(full) == 16
    assert {key[0] for key in full} == {f"T{index:02d}" for index in range(1, 9)}
    assert {key[1] for key in full} == {1}
    assert len(public) == 48
    assert {key[0] for key in public} == {f"T{index:02d}" for index in range(1, 9)}
    assert {key[1] for key in public} == {1, 2, 3}


def test_public_artifact_rejects_mixed_harness_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, results = _contract_fixture(monkeypatch)
    results[1]["harness_commit"] = "b" * 40
    results[1]["harness_sha256"] = "c" * 64
    monkeypatch.setattr(
        module,
        "_harness_sha256_at_commit",
        lambda _manifest_sha256, commit: "c" * 64 if commit == "b" * 40 else "e" * 64,
    )

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._validate_official_public_results(results, "single-pair-pilot")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("fixture_commit", "0" * 40),
        ("workspace_sha256", "0" * 64),
        ("prompt_sha256", "0" * 64),
        ("validator_sha256", "0" * 64),
        ("model_sandbox_profile_sha256", "0" * 64),
    ),
)
def test_public_artifact_binds_reconstructable_task_evidence(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    module, results = _contract_fixture(monkeypatch)
    results[0][field] = value

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._validate_official_public_results(results, "single-pair-pilot")


def test_successful_rows_bind_expected_test_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module, results = _contract_fixture(monkeypatch)
    results[0]["tests_total"] = 1
    results[0]["tests_passed"] = 1

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._validate_official_public_results(results, "single-pair-pilot")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("estimated_tokens", 0),
        ("estimated_tokens", 801),
        ("selected_count", 0),
    ),
)
def test_pmh_rows_require_bounded_nonempty_recall(
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: int,
) -> None:
    module, results = _contract_fixture(monkeypatch)
    recall = results[1]["recall"]
    assert isinstance(recall, dict)
    recall[field] = value

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._validate_official_public_results(results, "single-pair-pilot")


def test_model_sandbox_profile_identity_is_stable_across_host_paths(
    tmp_path: Path,
) -> None:
    module = _load_cli_module()
    workspaces: list[Path] = []
    for name in ("first-root", "second-root"):
        workspace = tmp_path / name / "workspace"
        implementation = workspace / "package" / "task.py"
        implementation.parent.mkdir(parents=True)
        implementation.write_text("VALUE = 1\n", encoding="utf-8")
        workspaces.append(workspace)

    common = {
        "codex_cli_version": "codex-cli 0.146.0-alpha.3.1",
        "requested_model_id": "gpt-5.6-sol",
        "reasoning_effort": "low",
        "timeout_seconds": 600,
        "validator_python_executable": Path("/usr/bin/python3"),
        "manifest_sha256": "a" * 64,
        "harness_commit": "b" * 40,
        "harness_sha256": "c" * 64,
    }
    first = module.ExecutionConfig(
        codex_executable=Path("/Applications/First/Codex"),
        validator_sandbox_executable=Path("/usr/bin/sandbox-exec"),
        codex_auth_file=tmp_path / "first-user" / ".codex" / "auth.json",
        **common,
    )
    second = module.ExecutionConfig(
        codex_executable=Path("/Applications/Second/Codex"),
        validator_sandbox_executable=Path("/opt/local/bin/sandbox-exec"),
        codex_auth_file=tmp_path / "second-user" / ".codex" / "auth.json",
        **common,
    )

    first_digest = module._model_sandbox_profile_sha256(
        first,
        workspaces[0],
        (PurePosixPath("package/task.py"),),
    )
    second_digest = module._model_sandbox_profile_sha256(
        second,
        workspaces[1],
        (PurePosixPath("package/task.py"),),
    )

    assert first_digest == second_digest


def test_manifest_public_contract_rebuilds_from_frozen_fixture_and_harness(
    tmp_path: Path,
) -> None:
    module = _load_cli_module()
    contract = module._manifest_public_contract(module.DEFAULT_MANIFEST)
    root = tmp_path.resolve() / "rebuild"
    repositories = module.build_fixture_repositories(root / "fixtures")
    tasks = module.load_task_specs(module.DEFAULT_MANIFEST)
    bundle = module.prepare_recall_bundle(
        root / "runtime",
        repositories,
        tasks,
        model_id="gpt-5.6-sol",
    )
    repository_by_id = {repository.fixture_id: repository for repository in repositories}
    recall_by_task = {recall.task_id: recall for recall in bundle.recalls}
    config = module.ExecutionConfig(
        codex_executable=Path("/Applications/Reviewed/Codex"),
        codex_cli_version="codex-cli 0.146.0-alpha.3.1",
        requested_model_id="gpt-5.6-sol",
        reasoning_effort="low",
        timeout_seconds=600,
        validator_sandbox_executable=Path("/usr/bin/sandbox-exec"),
        validator_python_executable=Path("/usr/bin/python3"),
        codex_auth_file=tmp_path / "user" / ".codex" / "auth.json",
        manifest_sha256="a" * 64,
        harness_commit="b" * 40,
        harness_sha256="c" * 64,
        artifact_scope="public-v1",
    )
    fixture_commits = contract["fixture_commit_by_fixture"]
    workspace_hashes = contract["workspace_sha256_by_fixture"]
    task_hashes = contract["task_sha256_by_id"]
    assert isinstance(fixture_commits, dict)
    assert isinstance(workspace_hashes, dict)
    assert isinstance(task_hashes, dict)

    for repository in repositories:
        assert fixture_commits[repository.fixture_id] == repository.commit
        assert workspace_hashes[repository.fixture_id] == module._workspace_sha256(repository.path)
    for task in tasks:
        repository = repository_by_id[task.fixture_id]
        recall = recall_by_task[task.task_id]
        expected = task_hashes[task.task_id]
        assert isinstance(expected, dict)
        assert expected == {
            "baseline_prompt_sha256": hashlib.sha256(
                module.compose_task_prompt(task, recall_text=None).encode("utf-8")
            ).hexdigest(),
            "model_sandbox_profile_sha256": module._model_sandbox_profile_sha256(
                config,
                repository.path,
                task.allowed_paths,
            ),
            "pmh_prompt_sha256": hashlib.sha256(
                module.compose_task_prompt(task, recall_text=recall.text).encode("utf-8")
            ).hexdigest(),
            "validator_sha256": module._validator_sha256(
                task,
                repository.path,
            ),
        }


def _contract_fixture(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ModuleType, list[dict[str, object]]]:
    module = _load_cli_module()
    tasks = tuple(
        module.TaskSpec(
            task_id=f"T{index:02d}",
            fixture_id=("ledger-tools" if index <= 4 else "release-kit"),
            version=2,
            expected_tests=(1 if index in {2, 6} else 2),
            order=("AB" if index % 2 else "BA"),
            title=f"Task {index}",
            prompt=f"Prompt {index}",
            validation_argv=("python3", "-m", "unittest", f"tests.test_t{index:02d}"),
            allowed_paths=(PurePosixPath(f"package/task_{index:02d}.py"),),
        )
        for index in range(1, 9)
    )
    execution: dict[str, object] = {
        "codex_cli_version": "codex-cli 0.146.0-alpha.3.1",
        "pilot_replicates": 1,
        "public_replicates": 3,
        "reasoning_effort": "low",
        "requested_model_id": "gpt-5.6-sol",
        "timeout_seconds": 600,
    }
    recall_contract: dict[str, object] = {
        "brief_sha256_by_task": {
            task.task_id: str(index) * 64 for index, task in enumerate(tasks, start=1)
        },
        "memory_snapshot_sha256": "9" * 64,
    }
    public_contract: dict[str, object] = {
        "fixture_commit_by_fixture": {
            "ledger-tools": "a" * 40,
            "release-kit": "f" * 40,
        },
        "workspace_sha256_by_fixture": {
            "ledger-tools": "b" * 64,
            "release-kit": "8" * 64,
        },
        "task_sha256_by_id": {
            task.task_id: {
                "baseline_prompt_sha256": ("c" * 64 if task.task_id == "T01" else "6" * 64),
                "pmh_prompt_sha256": ("d" * 64 if task.task_id == "T01" else "7" * 64),
                "validator_sha256": ("1" * 64 if task.task_id == "T01" else "5" * 64),
                "model_sandbox_profile_sha256": ("2" * 64 if task.task_id == "T01" else "4" * 64),
            }
            for task in tasks
        },
    }
    monkeypatch.setattr(
        module,
        "_manifest_snapshot",
        lambda _path: (
            tasks,
            execution,
            recall_contract,
            "3" * 64,
        ),
    )
    monkeypatch.setattr(
        module,
        "_manifest_public_contract",
        lambda _path: public_contract,
    )
    monkeypatch.setattr(
        module,
        "_harness_sha256_at_commit",
        lambda _manifest_sha256, _commit: "e" * 64,
    )
    common: dict[str, object] = {
        "artifact_scope": "single-pair-pilot",
        "runtime_evidence_class": "self-reported",
        "manifest_sha256": "3" * 64,
        "harness_commit": "4" * 40,
        "harness_sha256": "e" * 64,
        "model_sandbox_profile_sha256": "2" * 64,
        "task_id": "T01",
        "task_version": 2,
        "replicate": 1,
        "order": "AB",
        "fixture_commit": "a" * 40,
        "workspace_sha256": "b" * 64,
        "memory_snapshot_sha256": "9" * 64,
        "validator_sha256": "1" * 64,
        "codex_cli_version": "codex-cli 0.146.0-alpha.3.1",
        "requested_model_id": "gpt-5.6-sol",
        "observed_model_id": None,
        "reasoning_effort": "low",
        "success": True,
        "timed_out": False,
        "timeout_seconds": 600,
        "invalid_reason": None,
        "tests_passed": 2,
        "tests_total": 2,
    }
    baseline = {
        **copy.deepcopy(common),
        "run_id": "run-t01-r1-baseline",
        "condition": "baseline",
        "prompt_sha256": "c" * 64,
        "recall": {
            "invoked": False,
            "status": "not_invoked",
            "estimated_tokens": None,
            "selected_count": None,
            "omitted_count": None,
            "brief_sha256": None,
        },
    }
    pmh = {
        **copy.deepcopy(common),
        "run_id": "run-t01-r1-pmh",
        "condition": "pmh",
        "prompt_sha256": "d" * 64,
        "recall": {
            "invoked": True,
            "status": "ok",
            "estimated_tokens": 100,
            "selected_count": 1,
            "omitted_count": 0,
            "brief_sha256": "1" * 64,
        },
    }
    return module, [baseline, pmh]
