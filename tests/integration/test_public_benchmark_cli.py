from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"
MEMORY_SNAPSHOT_SHA256 = "1239eefbb51ba4c0d316ab2bf0fdb3909b0acc365fc46a8956b95921a223f862"
T01_BRIEF_SHA256 = "bf16ac7229f1e8ff5a400294f3aee9a1c24cdcfe7be472e4cf6356f830977f67"
_COMMITTED_HARNESS_DIRECTORY = tempfile.TemporaryDirectory(prefix="pmh-benchmark-cli-tests-")
_COMMITTED_HARNESS_ROOT = Path(_COMMITTED_HARNESS_DIRECTORY.name) / "project"
_COMMITTED_HARNESS_IDENTITY: tuple[str, str] | None = None


def test_cli_help_exposes_safe_benchmark_lifecycle() -> None:
    completed = _run("--help")

    assert completed.returncode == 0
    assert "prepare" in completed.stdout
    assert "run" in completed.stdout
    assert "summarize" in completed.stdout
    assert "verify-public" in completed.stdout


def test_prepare_then_dry_run_does_not_execute_codex(tmp_path: Path) -> None:
    private_root = tmp_path / "private"

    prepared = _run(
        "prepare",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--model",
        "gpt-5.6-sol",
    )
    dry_run = _run(
        "run",
        "--scope",
        "single-pair-pilot",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--replicates",
        "1",
        "--task",
        "T01",
    )

    assert prepared.returncode == 0, prepared.stderr
    assert "prepared_tasks=8" in prepared.stdout
    state = private_root / "benchmark-state.json"
    assert state.is_file()
    assert stat_mode(state) == 0o600
    state_receipt = private_root / "benchmark-state.sha256"
    assert state_receipt.is_file()
    assert stat_mode(state_receipt) == 0o600
    assert state_receipt.read_text(encoding="ascii") == (
        hashlib.sha256(state.read_bytes()).hexdigest() + "\n"
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert "artifact_scope=single-pair-pilot" in dry_run.stdout
    assert "scheduled_runs=2" in dry_run.stdout
    assert "execution=disabled" in dry_run.stdout
    assert not (private_root / "runs").exists()


def test_prepare_rejects_custom_manifest_claiming_official_benchmark(tmp_path: Path) -> None:
    custom_manifest = tmp_path / "custom-manifest.json"
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["benchmark_id"] = "lookalike"
    custom_manifest.write_text(
        json.dumps(document, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    private_root = tmp_path / "private"

    rejected = _run(
        "prepare",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(custom_manifest),
        "--model",
        "gpt-5.6-sol",
    )

    assert rejected.returncode != 0
    assert not private_root.exists()


def test_execute_resumes_from_private_public_checkpoints(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    fake_codex = _fake_codex(tmp_path / "fake-codex", tmp_path / "calls")
    fake_sandbox = _fake_sandbox(tmp_path / "fake-sandbox")
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    prepared = _run(
        "prepare",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--model",
        "gpt-5.6-sol",
    )
    assert prepared.returncode == 0, prepared.stderr
    common = (
        "run",
        "--scope",
        "single-pair-pilot",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--replicates",
        "1",
        "--task",
        "T01",
        "--execute",
        "--codex",
        os.fspath(fake_codex),
        "--codex-auth",
        os.fspath(auth),
        "--validator-sandbox",
        os.fspath(fake_sandbox),
        "--validator-python",
        os.fspath(Path(sys.executable).resolve()),
    )
    first_output = tmp_path / "first.jsonl"
    second_output = tmp_path / "second.jsonl"

    first = _run(*common, "--output", os.fspath(first_output))
    second = _run(*common, "--output", os.fspath(second_output))

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
    assert first_output.read_bytes() == second_output.read_bytes()

    checkpoint = private_root / "runs" / "run-t01-r1-baseline" / "public-result.json"
    original_checkpoint = checkpoint.read_bytes()
    for index, field in enumerate(
        (
            "workspace_sha256",
            "prompt_sha256",
            "validator_sha256",
            "raw_evidence_sha256",
        )
    ):
        tampered = json.loads(original_checkpoint)
        tampered[field] = "0" * 64
        checkpoint.write_text(
            json.dumps(tampered, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        rejected = _run(
            *common,
            "--output",
            os.fspath(tmp_path / f"rejected-{index}.jsonl"),
        )

        assert rejected.returncode != 0
        assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
        checkpoint.write_bytes(original_checkpoint)

    events = private_root / "runs" / "run-t01-r1-baseline" / "events.jsonl"
    original_events = events.read_bytes()
    events.write_bytes(original_events + b"\n")
    rejected = _run(
        *common,
        "--output",
        os.fspath(tmp_path / "rejected-evidence.jsonl"),
    )

    assert rejected.returncode != 0
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
    events.write_bytes(original_events)

    pmh_checkpoint = private_root / "runs" / "run-t01-r1-pmh" / "public-result.json"
    original_pmh_checkpoint = pmh_checkpoint.read_bytes()
    tampered = json.loads(original_pmh_checkpoint)
    tampered["recall"]["estimated_tokens"] += 1
    pmh_checkpoint.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    rejected = _run(
        *common,
        "--output",
        os.fspath(tmp_path / "rejected-recall.jsonl"),
    )

    assert rejected.returncode != 0
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
    pmh_checkpoint.write_bytes(original_pmh_checkpoint)

    for index, (section, field) in enumerate(
        (
            ("contamination", "project_canary_hits"),
            ("write_guard", "pending_count_after"),
        )
    ):
        tampered = json.loads(original_checkpoint)
        tampered["success"] = False
        tampered["invalid_reason"] = "codex_exit"
        tampered["tests_passed"] = 0
        tampered[section][field] = 1
        checkpoint.write_text(
            json.dumps(tampered, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        rejected = _run(
            *common,
            "--output",
            os.fspath(tmp_path / f"rejected-{section}-{index}.jsonl"),
        )

        assert rejected.returncode != 0
        assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
        checkpoint.write_bytes(original_checkpoint)

    tampered = json.loads(original_checkpoint)
    tampered["duration_ms"] += 1
    checkpoint.write_text(
        json.dumps(tampered, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    rejected = _run(
        *common,
        "--output",
        os.fspath(tmp_path / "rejected-receipt.jsonl"),
    )

    assert rejected.returncode != 0
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
    checkpoint.write_bytes(original_checkpoint)

    validation_workspace = (
        private_root
        / "runs"
        / "run-t01-r1-baseline"
        / "validation-workspace"
        / "ledger_tools"
        / "records.py"
    )
    original_validation_workspace = validation_workspace.read_bytes()
    validation_workspace.write_bytes(original_validation_workspace + b"\n")
    rejected = _run(
        *common,
        "--output",
        os.fspath(tmp_path / "rejected-validation-workspace.jsonl"),
    )

    assert rejected.returncode != 0
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
    validation_workspace.write_bytes(original_validation_workspace)

    validator_transcript = private_root / "runs" / "run-t01-r1-baseline" / "validator.txt"
    original_validator_transcript = validator_transcript.read_bytes()
    validator_transcript.write_bytes(original_validator_transcript + b"\n")
    rejected = _run(
        *common,
        "--output",
        os.fspath(tmp_path / "rejected-validator-transcript.jsonl"),
    )

    assert rejected.returncode != 0
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
    validator_transcript.write_bytes(original_validator_transcript)


def test_execute_quarantines_incomplete_run_before_safe_retry(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    fake_codex = _fake_codex(tmp_path / "fake-codex", tmp_path / "calls")
    fake_sandbox = _fake_sandbox(tmp_path / "fake-sandbox")
    auth = tmp_path / "auth.json"
    auth.write_text("{}\n", encoding="utf-8")
    auth.chmod(0o600)
    prepared = _run(
        "prepare",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--model",
        "gpt-5.6-sol",
    )
    assert prepared.returncode == 0, prepared.stderr
    runs = private_root / "runs"
    incomplete = runs / "run-t01-r1-baseline"
    incomplete.mkdir(mode=0o700, parents=True)
    (incomplete / "partial-evidence").write_text("preserve me\n", encoding="utf-8")
    (incomplete / "public-result.json").write_text("partial\n", encoding="utf-8")
    (incomplete / "public-result.sha256").write_text("partial\n", encoding="ascii")

    completed = _run(
        "run",
        "--scope",
        "single-pair-pilot",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--replicates",
        "1",
        "--task",
        "T01",
        "--execute",
        "--codex",
        os.fspath(fake_codex),
        "--codex-auth",
        os.fspath(auth),
        "--validator-sandbox",
        os.fspath(fake_sandbox),
        "--validator-python",
        os.fspath(Path(sys.executable).resolve()),
        "--output",
        os.fspath(tmp_path / "recovered.jsonl"),
    )

    assert completed.returncode == 0, completed.stderr
    assert (tmp_path / "calls").read_text(encoding="utf-8") == "2"
    quarantined = tuple(runs.glob(".run-t01-r1-baseline.incomplete-*"))
    assert len(quarantined) == 1
    assert (quarantined[0] / "evidence" / "partial-evidence").read_text(
        encoding="utf-8"
    ) == "preserve me\n"
    assert (runs / "run-t01-r1-baseline" / "public-result.json").is_file()
    assert (runs / "run-t01-r1-baseline" / "public-result.sha256").is_file()


def test_execute_seals_profiles_and_rejects_boundary_drift_before_new_task(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    calls = tmp_path / "calls"
    fake_codex = _fake_codex(tmp_path / "fake-codex", calls)
    fake_sandbox = _fake_sandbox(tmp_path / "fake-sandbox")
    auth_one = tmp_path / "first-auth" / "auth.json"
    auth_two = tmp_path / "second-auth" / "auth.json"
    for auth in (auth_one, auth_two):
        auth.parent.mkdir()
        auth.write_text("{}\n", encoding="utf-8")
        auth.chmod(0o600)
    prepared = _run(
        "prepare",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--model",
        "gpt-5.6-sol",
    )
    assert prepared.returncode == 0, prepared.stderr
    common = (
        "--scope",
        "single-pair-pilot",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--replicates",
        "1",
        "--execute",
        "--codex",
        os.fspath(fake_codex),
        "--validator-sandbox",
        os.fspath(fake_sandbox),
        "--validator-python",
        os.fspath(Path(sys.executable).resolve()),
    )

    first = _run(
        "run",
        *common,
        "--task",
        "T01",
        "--codex-auth",
        os.fspath(auth_one),
        "--output",
        os.fspath(tmp_path / "t01.jsonl"),
    )

    assert first.returncode == 0, first.stderr
    envelope_path = private_root / "execution-envelope.json"
    receipt_path = private_root / "execution-envelope.sha256"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    assert receipt_path.read_text(encoding="ascii") == (
        hashlib.sha256(envelope_path.read_bytes()).hexdigest() + "\n"
    )
    assert set(envelope["model_sandbox_profile_sha256_by_task"]) == {
        f"T{index:02d}" for index in range(1, 9)
    }
    assert all(
        len(value) == 64 for value in envelope["model_sandbox_profile_sha256_by_task"].values()
    )

    drifted = _run(
        "run",
        *common,
        "--task",
        "T01",
        "--codex-auth",
        os.fspath(auth_two),
        "--output",
        os.fspath(tmp_path / "t01-drift.jsonl"),
    )

    assert drifted.returncode != 0
    assert calls.read_text(encoding="utf-8") == "2"


def test_run_rejects_recall_text_even_when_self_reported_hash_is_recomputed(
    tmp_path: Path,
) -> None:
    private_root = tmp_path / "private"
    prepared = _run(
        "prepare",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--model",
        "gpt-5.6-sol",
    )
    assert prepared.returncode == 0, prepared.stderr
    state_path = private_root / "benchmark-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["recall_bundle"]["recalls"][0]["text"] += " tampered"
    state["recall_bundle"]["recalls"][0]["brief_sha256"] = hashlib.sha256(
        state["recall_bundle"]["recalls"][0]["text"].encode("utf-8")
    ).hexdigest()
    state_path.write_text(
        json.dumps(state, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    (private_root / "benchmark-state.sha256").write_text(
        hashlib.sha256(state_path.read_bytes()).hexdigest() + "\n",
        encoding="ascii",
    )

    rejected = _run(
        "run",
        "--scope",
        "single-pair-pilot",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--replicates",
        "1",
        "--task",
        "T01",
    )

    assert rejected.returncode != 0
    assert "benchmark state rejected" in rejected.stderr


def test_run_rejects_missing_or_mismatched_state_receipt(tmp_path: Path) -> None:
    private_root = tmp_path / "private"
    prepared = _run(
        "prepare",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--model",
        "gpt-5.6-sol",
    )
    assert prepared.returncode == 0, prepared.stderr
    receipt = private_root / "benchmark-state.sha256"
    original = receipt.read_bytes()

    receipt.write_text("0" * 64 + "\n", encoding="ascii")
    mismatched = _run(
        "run",
        "--scope",
        "single-pair-pilot",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--replicates",
        "1",
        "--task",
        "T01",
    )
    receipt.write_bytes(original)
    receipt.unlink()
    missing = _run(
        "run",
        "--scope",
        "single-pair-pilot",
        "--private-root",
        os.fspath(private_root),
        "--manifest",
        os.fspath(MANIFEST),
        "--replicates",
        "1",
        "--task",
        "T01",
    )

    assert mismatched.returncode != 0
    assert missing.returncode != 0


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("manifest_sha256", "0" * 64),
        ("task_id", "T99"),
        ("task_version", 1),
        ("requested_model_id", "lookalike-model"),
        ("observed_model_id", "lookalike-model"),
        ("codex_cli_version", "codex-cli lookalike"),
        ("reasoning_effort", "high"),
        ("timeout_seconds", 601),
        ("order", "BA"),
        ("replicate", 4),
        ("run_id", "run-t01-r1-lookalike"),
        ("memory_snapshot_sha256", "1" * 64),
    ),
)
@pytest.mark.parametrize("command", ("verify-public", "summarize"))
def test_official_commands_reject_structurally_valid_lookalike_results(
    tmp_path: Path,
    command: str,
    field: str,
    value: object,
) -> None:
    results = _official_t01_pair()
    for result in results:
        result[field] = value
        if field == "task_id":
            result["run_id"] = str(result["run_id"]).replace("t01", "t99")
        if field == "replicate":
            result["run_id"] = str(result["run_id"]).replace("-r1-", "-r4-")
    source = tmp_path / f"{command}-{field}.jsonl"
    _write_results(source, results)

    arguments = [
        command,
        "--scope",
        "single-pair-pilot",
        "--input",
        os.fspath(source),
    ]
    output = tmp_path / f"{command}-{field}.json"
    if command == "summarize":
        arguments.extend(("--output", os.fspath(output)))
    rejected = _run(*arguments)

    assert rejected.returncode != 0
    assert not output.exists()


@pytest.mark.parametrize("command", ("verify-public", "summarize"))
def test_official_commands_reject_wrong_preregistered_recall_brief(
    tmp_path: Path,
    command: str,
) -> None:
    results = _official_t01_pair()
    results[1]["recall"]["brief_sha256"] = "2" * 64
    source = tmp_path / f"{command}-recall.jsonl"
    _write_results(source, results)

    arguments = [
        command,
        "--scope",
        "single-pair-pilot",
        "--input",
        os.fspath(source),
    ]
    output = tmp_path / f"{command}-recall.json"
    if command == "summarize":
        arguments.extend(("--output", os.fspath(output)))
    rejected = _run(*arguments)

    assert rejected.returncode != 0
    assert not output.exists()


def test_official_commands_accept_a_preregistered_protocol_pair(tmp_path: Path) -> None:
    source = tmp_path / "official.jsonl"
    summary = tmp_path / "summary.json"
    _write_results(source, _official_t01_pair())

    verified = _run(
        "verify-public",
        "--scope",
        "single-pair-pilot",
        "--input",
        os.fspath(source),
    )
    summarized = _run(
        "summarize",
        "--scope",
        "single-pair-pilot",
        "--input",
        os.fspath(source),
        "--output",
        os.fspath(summary),
    )

    assert verified.returncode == 0, verified.stderr
    assert "verified_public_runs=2" in verified.stdout
    assert summarized.returncode == 0, summarized.stderr
    assert json.loads(summary.read_text(encoding="utf-8"))["task_pair_count"] == 1


def _official_t01_pair() -> list[dict[str, object]]:
    committed_root = _committed_harness_root()
    committed_manifest = committed_root / "benchmarks" / "project-memory-effect" / "manifest.json"
    manifest_payload = committed_manifest.read_bytes()
    manifest_sha256 = hashlib.sha256(manifest_payload).hexdigest()
    manifest = json.loads(manifest_payload)
    public_contract = manifest["public_contract"]
    task_contract = public_contract["task_sha256_by_id"]["T01"]
    harness_commit, harness_sha256 = _committed_harness_identity()
    common: dict[str, object] = {
        "schema_version": 3,
        "benchmark_id": "project-memory-effect-public-v1",
        "artifact_scope": "single-pair-pilot",
        "runtime_evidence_class": "self-reported",
        "manifest_sha256": manifest_sha256,
        "harness_commit": harness_commit,
        "harness_sha256": harness_sha256,
        "model_sandbox_profile_sha256": task_contract["model_sandbox_profile_sha256"],
        "task_id": "T01",
        "task_version": 2,
        "replicate": 1,
        "order": "AB",
        "fixture_commit": public_contract["fixture_commit_by_fixture"]["ledger-tools"],
        "workspace_sha256": public_contract["workspace_sha256_by_fixture"]["ledger-tools"],
        "memory_snapshot_sha256": MEMORY_SNAPSHOT_SHA256,
        "prompt_sha256": task_contract["baseline_prompt_sha256"],
        "validator_sha256": task_contract["validator_sha256"],
        "validation_workspace_sha256": "2" * 64,
        "validator_evidence_sha256": "3" * 64,
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
        "duration_ms": 1000,
        "tool_call_count": 1,
        "wrong_route_count": 0,
        "tokens": {
            "mode": "fresh_session_final_total",
            "input_tokens": 100,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 0,
            "uncached_input_tokens": 90,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": 120,
        },
        "contamination": {
            "project_canary_hits": 0,
            "source_canary_hits": 0,
            "model_canary_hits": 0,
        },
        "write_guard": {
            "memory_snapshot_unchanged": True,
            "pending_count_before": 0,
            "pending_count_after": 0,
            "trusted_count_before": 8,
            "trusted_count_after": 8,
        },
        "raw_evidence_sha256": "4" * 64,
    }
    baseline = {
        **copy.deepcopy(common),
        "run_id": "run-t01-r1-baseline",
        "condition": "baseline",
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
        "prompt_sha256": task_contract["pmh_prompt_sha256"],
        "recall": {
            "invoked": True,
            "status": "ok",
            "estimated_tokens": 100,
            "selected_count": 1,
            "omitted_count": 0,
            "brief_sha256": T01_BRIEF_SHA256,
        },
    }
    return [baseline, pmh]


def _write_results(path: Path, results: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(
            json.dumps(result, sort_keys=True, separators=(",", ":")) + "\n" for result in results
        ),
        encoding="utf-8",
    )


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    committed_root = _committed_harness_root()
    committed_manifest = committed_root / "benchmarks" / "project-memory-effect" / "manifest.json"
    selected_arguments = [
        os.fspath(committed_manifest) if argument == os.fspath(MANIFEST) else argument
        for argument in arguments
    ]
    return subprocess.run(
        [
            sys.executable,
            "-B",
            os.fspath(committed_root / "scripts" / "run_public_benchmark.py"),
            *selected_arguments,
        ],
        cwd=committed_root,
        env={**os.environ, "PYTHONPATH": os.fspath(committed_root / "src")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _committed_harness_root() -> Path:
    if (_COMMITTED_HARNESS_ROOT / ".git").is_dir():
        return _COMMITTED_HARNESS_ROOT
    _COMMITTED_HARNESS_ROOT.mkdir()
    shutil.copytree(
        PROJECT_ROOT / "src",
        _COMMITTED_HARNESS_ROOT / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    shutil.copytree(
        PROJECT_ROOT / "scripts",
        _COMMITTED_HARNESS_ROOT / "scripts",
        ignore=shutil.ignore_patterns("__pycache__", "*.py[cod]"),
    )
    benchmark_root = _COMMITTED_HARNESS_ROOT / "benchmarks" / "project-memory-effect"
    benchmark_root.mkdir(parents=True)
    for name in ("manifest.json", "result.schema.json"):
        shutil.copy2(
            PROJECT_ROOT / "benchmarks" / "project-memory-effect" / name,
            benchmark_root / name,
        )
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy2(PROJECT_ROOT / name, _COMMITTED_HARNESS_ROOT / name)
    subprocess.run(
        ("git", "init", "-q", "--initial-branch=main"),
        cwd=_COMMITTED_HARNESS_ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        ("git", "add", "--all"),
        cwd=_COMMITTED_HARNESS_ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
    subprocess.run(
        (
            "git",
            "-c",
            "user.name=PMH Benchmark Test",
            "-c",
            "user.email=benchmark.invalid",
            "-c",
            "commit.gpgsign=false",
            "-c",
            "core.hooksPath=/dev/null",
            "commit",
            "-q",
            "-m",
            "test: committed benchmark harness",
        ),
        cwd=_COMMITTED_HARNESS_ROOT,
        check=True,
        capture_output=True,
        timeout=30,
    )
    return _COMMITTED_HARNESS_ROOT


def _committed_harness_identity() -> tuple[str, str]:
    global _COMMITTED_HARNESS_IDENTITY
    if _COMMITTED_HARNESS_IDENTITY is not None:
        return _COMMITTED_HARNESS_IDENTITY
    committed_root = _committed_harness_root()
    program = """
import hashlib
import importlib.util
import json
from pathlib import Path

root = Path.cwd()
script = root / "scripts" / "run_public_benchmark.py"
spec = importlib.util.spec_from_file_location("run_public_benchmark", script)
if spec is None or spec.loader is None:
    raise RuntimeError("benchmark module unavailable")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
manifest = root / "benchmarks" / "project-memory-effect" / "manifest.json"
print(json.dumps(module._harness_identity(hashlib.sha256(manifest.read_bytes()).hexdigest(), require_committed=True)))
"""
    completed = subprocess.run(
        (sys.executable, "-B", "-c", program),
        cwd=committed_root,
        env={**os.environ, "PYTHONPATH": os.fspath(committed_root / "src")},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    decoded = json.loads(completed.stdout)
    assert isinstance(decoded, list)
    assert len(decoded) == 2
    assert all(isinstance(value, str) for value in decoded)
    _COMMITTED_HARNESS_IDENTITY = (decoded[0], decoded[1])
    return _COMMITTED_HARNESS_IDENTITY


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _fake_codex(path: Path, counter: Path) -> Path:
    script = f"""#!{sys.executable}
import json
import pathlib
import sys

if "--version" in sys.argv:
    print("codex-cli 0.146.0-alpha.3.1")
    raise SystemExit(0)
counter = pathlib.Path({os.fspath(counter)!r})
count = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(count + 1))
prompt = sys.stdin.read()
root = pathlib.Path.cwd()
(root / "ledger_tools" / "records.py").write_text(
    "def normalize_record(line: str) -> str:\\n"
    "    if line.endswith('\\\\r\\\\n'):\\n"
    "        return line[:-2]\\n"
    "    if line.endswith('\\\\n'):\\n"
    "        return line[:-1]\\n"
    "    return line\\n",
    encoding="utf-8",
)
def emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)
emit({{"type": "thread.started", "thread_id": "private"}})
emit({{
    "type": "item.started",
    "item": {{"id": "private", "type": "command_execution"}},
}})
emit({{
    "type": "item.completed",
    "item": {{"id": "private", "type": "command_execution", "exit_code": 0}},
}})
emit({{
    "type": "turn.completed",
    "usage": {{
        "input_tokens": 100,
        "cached_input_tokens": 10,
        "cache_write_input_tokens": 0,
        "output_tokens": 20,
        "reasoning_output_tokens": 5,
    }},
}})
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    return path


def _fake_sandbox(path: Path) -> Path:
    script = f"""#!{sys.executable}
import os
import sys

if sys.argv[1] != "-p":
    raise SystemExit(90)
os.execv(sys.argv[3], sys.argv[3:])
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    return path
