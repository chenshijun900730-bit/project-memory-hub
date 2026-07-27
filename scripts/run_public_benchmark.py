from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn, Sequence

from project_memory_hub.benchmark import (
    BenchmarkEvidenceError,
    BenchmarkRun,
    canonical_json_bytes,
    summarize_paired_runs,
)
from project_memory_hub.benchmark_fixtures import (
    BenchmarkFixtureError,
    FixtureRepository,
    RecallBundle,
    RecallEvidence,
    TaskSpec,
    benchmark_git_executable,
    build_fixture_repositories,
    load_task_specs,
    prepare_recall_bundle,
    verify_fixture_repository,
)
from project_memory_hub.benchmark_runner import (
    BenchmarkRunnerError,
    ExecutionConfig,
    RunSpec,
    _MANAGED_REQUIREMENTS_CANONICAL_PATH,
    _MANAGED_REQUIREMENTS_LOGICAL_PATH,
    _model_sandbox_profile_sha256,
    _validator_sha256,
    _workspace_snapshot,
    _workspace_snapshot_sha256,
    _workspace_sha256,
    build_run_schedule,
    compose_task_prompt,
    execute_benchmark_run,
    read_public_jsonl,
    write_public_jsonl,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"
_STATE_FILE = "benchmark-state.json"
_STATE_RECEIPT = "benchmark-state.sha256"
_STATE_VERSION = 4
_EXECUTION_ENVELOPE_FILE = "execution-envelope.json"
_EXECUTION_ENVELOPE_RECEIPT = "execution-envelope.sha256"
_EXECUTION_ENVELOPE_VERSION = 3
_CHECKPOINT_RECEIPT = "public-result.sha256"
_MAX_STATE_BYTES = 2 * 1024 * 1024
_MAX_CHECKPOINT_EVIDENCE_BYTES = 16 * 1024 * 1024
_MAX_BOUNDARY_FILE_BYTES = 512 * 1024 * 1024
_MAX_MANIFEST_BYTES = 1024 * 1024
_OFFICIAL_MANIFEST_SHA256 = "a8d2c7696ad3a3035291dfeba0cf2c4cc300cf3a90bb4f04e5f046cd011c8643"
_HARNESS_PATHS = (
    Path("benchmarks/project-memory-effect/manifest.json"),
    Path("benchmarks/project-memory-effect/result.schema.json"),
    Path("pyproject.toml"),
    Path("uv.lock"),
)
_HARNESS_TREE_ROOTS = (Path("scripts"), Path("src"))
_MAX_HARNESS_PATHS = 10_000
_MAX_HARNESS_TREE_BYTES = 8 * 1024 * 1024
_ARTIFACT_SCOPES = (
    "single-pair-pilot",
    "full-pilot",
    "public-v1",
)


class BenchmarkStateError(ValueError):
    """Private benchmark preparation state failed closed validation."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare, run, and verify the public Project Memory Hub A/B benchmark"
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    prepare = subcommands.add_parser(
        "prepare",
        help="create synthetic fixtures and a frozen private recall bundle",
    )
    prepare.add_argument("--private-root", type=Path, required=True)
    prepare.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    prepare.add_argument("--model", required=True)
    prepare.set_defaults(handler=_prepare)

    run = subcommands.add_parser(
        "run",
        help="show the schedule, or execute it only with --execute",
    )
    run.add_argument("--private-root", type=Path, required=True)
    run.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    run.add_argument("--scope", choices=_ARTIFACT_SCOPES, required=True)
    run.add_argument("--replicates", type=int, required=True)
    run.add_argument("--task", action="append", default=[])
    run.add_argument("--execute", action="store_true")
    run.add_argument("--output", type=Path)
    run.add_argument("--codex", type=Path)
    run.add_argument("--codex-auth", type=Path)
    run.add_argument("--validator-sandbox", type=Path)
    run.add_argument("--validator-python", type=Path)
    run.set_defaults(handler=_run)

    summarize = subcommands.add_parser(
        "summarize",
        help="create a success-first paired summary from public JSONL",
    )
    summarize.add_argument("--input", type=Path, required=True)
    summarize.add_argument("--output", type=Path, required=True)
    summarize.add_argument("--scope", choices=_ARTIFACT_SCOPES, required=True)
    summarize.set_defaults(handler=_summarize)

    verify = subcommands.add_parser(
        "verify-public",
        help="fail closed unless a public JSONL artifact passes the allowlist",
    )
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--scope", choices=_ARTIFACT_SCOPES, required=True)
    verify.set_defaults(handler=_verify_public)
    return parser


def _prepare(arguments: argparse.Namespace) -> int:
    tasks, execution, recall_contract, manifest_sha256 = _manifest_snapshot(arguments.manifest)
    if execution["requested_model_id"] != arguments.model:
        _reject_state()
    harness_commit, harness_sha256 = _harness_identity(manifest_sha256)
    root = arguments.private_root.resolve()
    if root.exists() or root.is_symlink():
        _reject_state()
    root.mkdir(mode=0o700, parents=True)
    repositories = build_fixture_repositories(root / "fixtures")
    bundle = prepare_recall_bundle(
        root / "runtime",
        repositories,
        tasks,
        model_id=arguments.model,
    )
    _verify_recall_contract(bundle, recall_contract)
    state = {
        "schema_version": _STATE_VERSION,
        "manifest_sha256": manifest_sha256,
        "recall_contract_sha256": _recall_contract_sha256(recall_contract),
        "harness_commit": harness_commit,
        "harness_sha256": harness_sha256,
        "execution": execution,
        "repositories": [
            {"fixture_id": repository.fixture_id, "commit": repository.commit}
            for repository in repositories
        ],
        "recall_bundle": {
            "model_id": bundle.model_id,
            "memory_snapshot_sha256": bundle.memory_snapshot_sha256,
            "memory_snapshot_unchanged": bundle.memory_snapshot_unchanged,
            "pending_count_before": bundle.pending_count_before,
            "pending_count_after": bundle.pending_count_after,
            "trusted_count_before": bundle.trusted_count_before,
            "trusted_count_after": bundle.trusted_count_after,
            "recalls": [asdict(recall) for recall in bundle.recalls],
        },
    }
    state_payload = canonical_json_bytes(state)
    _write_exclusive(root / _STATE_FILE, state_payload)
    _write_exclusive(
        root / _STATE_RECEIPT,
        (hashlib.sha256(state_payload).hexdigest() + "\n").encode("ascii"),
    )
    print(f"prepared_tasks={len(tasks)}")
    print("memory_snapshot_unchanged=true")
    print("pending_rows=0")
    return 0


def _run(arguments: argparse.Namespace) -> int:
    if arguments.execute and not sys.dont_write_bytecode:
        _reject_state()
    root = arguments.private_root.resolve(strict=True)
    all_tasks, manifest_execution, recall_contract, manifest_sha256 = _manifest_snapshot(
        arguments.manifest
    )
    if arguments.execute:
        harness_commit, harness_sha256 = _harness_identity(
            manifest_sha256,
            require_committed=True,
        )
    else:
        harness_commit, harness_sha256 = _harness_identity(manifest_sha256)
    repositories, bundle, execution = _load_state(
        root,
        manifest_sha256,
        harness_commit,
        harness_sha256,
        recall_contract,
    )
    if execution != manifest_execution:
        _reject_state()
    selected_ids = tuple(arguments.task)
    if selected_ids:
        if len(set(selected_ids)) != len(selected_ids):
            _reject_state()
        selected = tuple(task for task in all_tasks if task.task_id in selected_ids)
        if {task.task_id for task in selected} != set(selected_ids):
            _reject_state()
    else:
        selected = all_tasks
    schedule = build_run_schedule(selected, replicates=arguments.replicates)
    actual_protocol_keys = {
        (spec.task.task_id, spec.replicate, spec.condition) for spec in schedule
    }
    if actual_protocol_keys != _scope_protocol_keys(
        _state_string(vars(arguments), "scope"),
        all_tasks,
        manifest_execution,
    ):
        _reject_state()
    print(f"artifact_scope={arguments.scope}")
    print(f"scheduled_runs={len(schedule)}")
    if not arguments.execute:
        print("execution=disabled")
        return 0
    if arguments.output is None:
        _reject_state()

    codex = _resolve_executable(arguments.codex, "codex")
    validator_sandbox = _resolve_executable(
        arguments.validator_sandbox,
        "/usr/bin/sandbox-exec",
    )
    validator_python = _resolve_executable(
        arguments.validator_python,
        (
            "/Library/Developer/CommandLineTools/Library/Frameworks/"
            "Python3.framework/Versions/3.9/Resources/Python.app/"
            "Contents/MacOS/Python"
        ),
    )
    codex_version = _codex_version(codex)
    if codex_version != execution["codex_cli_version"]:
        _reject_state()
    codex_auth = (
        arguments.codex_auth
        if arguments.codex_auth is not None
        else Path.home() / ".codex" / "auth.json"
    ).resolve(strict=True)
    config = ExecutionConfig(
        codex_executable=codex,
        codex_cli_version=codex_version,
        requested_model_id=_state_string(execution, "requested_model_id"),
        reasoning_effort=_state_string(execution, "reasoning_effort"),
        timeout_seconds=_state_integer(execution, "timeout_seconds"),
        validator_sandbox_executable=validator_sandbox,
        validator_python_executable=validator_python,
        codex_auth_file=codex_auth,
        manifest_sha256=manifest_sha256,
        harness_commit=harness_commit,
        harness_sha256=harness_sha256,
        artifact_scope=_state_string(vars(arguments), "scope"),
    )
    _seal_execution_envelope(root, all_tasks, config)
    repository_by_id = {repository.fixture_id: repository for repository in repositories}
    recall_by_task = {recall.task_id: recall for recall in bundle.recalls}
    results: list[dict[str, object]] = []
    for spec in schedule:
        run_root = root / "runs" / spec.run_id
        checkpoint = run_root / "public-result.json"
        receipt = run_root / _CHECKPOINT_RECEIPT
        checkpoint_present = checkpoint.exists() or checkpoint.is_symlink()
        receipt_present = receipt.exists() or receipt.is_symlink()
        if checkpoint_present and receipt_present and _checkpoint_receipt_is_complete(receipt):
            checkpoint_results = read_public_jsonl(checkpoint)
            if len(checkpoint_results) != 1:
                _reject_state()
            result = checkpoint_results[0]
            _verify_checkpoint(
                result,
                spec,
                repository_by_id[spec.task.fixture_id],
                recall_by_task[spec.task.task_id],
                bundle,
                config,
                run_root,
            )
        else:
            if checkpoint_present or receipt_present or run_root.exists() or run_root.is_symlink():
                _quarantine_incomplete_run(run_root)
            result = execute_benchmark_run(
                spec,
                repository_by_id[spec.task.fixture_id],
                recall_by_task[spec.task.task_id],
                bundle,
                private_root=root / "runs",
                config=config,
            )
        results.append(result)
    write_public_jsonl(arguments.output, results)
    succeeded = sum(result["success"] is True for result in results)
    print(f"completed_runs={len(results)}")
    print(f"successful_runs={succeeded}")
    print("runtime_evidence=self-reported")
    print(f"public_result_sha256={_sha256_file(arguments.output)}")
    return 0


def _summarize(arguments: argparse.Namespace) -> int:
    results = read_public_jsonl(arguments.input)
    scope = _state_string(vars(arguments), "scope")
    _validate_official_public_results(results, scope)
    runs = [
        BenchmarkRun(
            task_id=_string(result, "task_id"),
            condition=_string(result, "condition"),
            replicate=_integer(result, "replicate"),
            fixture_commit=_string(result, "fixture_commit"),
            task_version=_integer(result, "task_version"),
            workspace_sha256=_string(result, "workspace_sha256"),
            memory_snapshot_sha256=_string(result, "memory_snapshot_sha256"),
            validator_sha256=_string(result, "validator_sha256"),
            timeout_seconds=_integer(result, "timeout_seconds"),
            codex_cli_version=_string(result, "codex_cli_version"),
            requested_model_id=_string(result, "requested_model_id"),
            reasoning_effort=_string(result, "reasoning_effort"),
            order=_string(result, "order"),
            success=_boolean(result, "success"),
            invalid_reason=_optional_string(result, "invalid_reason"),
            total_tokens=_nested_optional_integer(result, "tokens", "total_tokens"),
            duration_ms=_integer(result, "duration_ms"),
            contamination_hits=sum(
                _nested_integer(result, "contamination", field)
                for field in (
                    "project_canary_hits",
                    "source_canary_hits",
                    "model_canary_hits",
                )
            ),
            manifest_sha256=_string(result, "manifest_sha256"),
            harness_commit=_string(result, "harness_commit"),
            harness_sha256=_string(result, "harness_sha256"),
            model_sandbox_profile_sha256=_string(
                result,
                "model_sandbox_profile_sha256",
            ),
            validation_workspace_sha256=_string(
                result,
                "validation_workspace_sha256",
            ),
            validator_evidence_sha256=_string(
                result,
                "validator_evidence_sha256",
            ),
        )
        for result in results
    ]
    summary = {
        "schema_version": 2,
        "benchmark_id": "project-memory-effect-public-v1",
        "artifact_scope": scope,
        "evidence_classification": _summary_evidence_classification(),
        "source_sha256": _sha256_file(arguments.input),
        **summarize_paired_runs(runs),
    }
    _write_exclusive(arguments.output, canonical_json_bytes(summary))
    print(f"summarized_runs={len(results)}")
    print(f"artifact_scope={scope}")
    print("runtime_evidence=self-reported")
    print(f"summary_sha256={_sha256_file(arguments.output)}")
    return 0


def _summary_evidence_classification() -> dict[str, list[str]]:
    return {
        "contract_verified": [
            "artifact_scope",
            "condition",
            "fixture_commit",
            "harness_commit",
            "harness_sha256",
            "model_sandbox_profile_sha256",
            "prompt_sha256",
            "replicate",
            "task_id",
            "validator_sha256",
            "workspace_sha256",
        ],
        "success_row_contract_verified": [
            "tests_passed",
            "tests_total",
        ],
        "failure_row_self_reported": [
            "tests_passed",
            "tests_total",
        ],
        "self_reported": [
            "contamination",
            "duration_ms",
            "raw_evidence_sha256",
            "tokens",
            "tool_call_count",
            "validation_workspace_sha256",
            "validator_evidence_sha256",
            "write_guard",
            "wrong_route_count",
        ],
    }


def _verify_checkpoint(
    result: dict[str, object],
    spec: RunSpec,
    repository: FixtureRepository,
    recall: RecallEvidence,
    bundle: RecallBundle,
    config: ExecutionConfig,
    run_root: Path,
) -> None:
    _verify_checkpoint_receipt(
        canonical_json_bytes(result),
        run_root / _CHECKPOINT_RECEIPT,
    )
    recall_text = recall.text if spec.condition == "pmh" else None
    prompt = compose_task_prompt(spec.task, recall_text=recall_text)
    expected = {
        "artifact_scope": config.artifact_scope,
        "manifest_sha256": config.manifest_sha256,
        "harness_commit": config.harness_commit,
        "harness_sha256": config.harness_sha256,
        "run_id": spec.run_id,
        "task_id": spec.task.task_id,
        "task_version": spec.task.version,
        "condition": spec.condition,
        "replicate": spec.replicate,
        "order": spec.order,
        "fixture_commit": repository.commit,
        "workspace_sha256": _workspace_sha256(repository.path),
        "memory_snapshot_sha256": bundle.memory_snapshot_sha256,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "codex_cli_version": config.codex_cli_version,
        "requested_model_id": config.requested_model_id,
        "reasoning_effort": config.reasoning_effort,
        "timeout_seconds": config.timeout_seconds,
        "validator_sha256": _validator_sha256(spec.task, repository.path),
        "model_sandbox_profile_sha256": _model_sandbox_profile_sha256(
            config,
            run_root / "validation-workspace",
            spec.task.allowed_paths,
        ),
        "validation_workspace_sha256": _workspace_snapshot_sha256(
            _workspace_snapshot(run_root / "validation-workspace")
        ),
        "validator_evidence_sha256": _checkpoint_evidence_sha256(run_root / "validator.txt"),
        "raw_evidence_sha256": _checkpoint_evidence_sha256(run_root / "events.jsonl"),
    }
    if any(result.get(field) != value for field, value in expected.items()):
        _reject_state()
    expected_recall: dict[str, object]
    if spec.condition == "pmh":
        expected_recall = {
            "invoked": True,
            "status": "ok",
            "estimated_tokens": recall.estimated_tokens,
            "selected_count": recall.selected_count,
            "omitted_count": recall.omitted_count,
            "brief_sha256": recall.brief_sha256,
        }
    else:
        expected_recall = {
            "invoked": False,
            "status": "not_invoked",
            "estimated_tokens": None,
            "selected_count": None,
            "omitted_count": None,
            "brief_sha256": None,
        }
    expected_contamination = {
        "project_canary_hits": recall.project_canary_hits,
        "source_canary_hits": recall.source_canary_hits,
        "model_canary_hits": recall.model_canary_hits,
    }
    expected_write_guard = {
        "memory_snapshot_unchanged": bundle.memory_snapshot_unchanged,
        "pending_count_before": bundle.pending_count_before,
        "pending_count_after": bundle.pending_count_after,
        "trusted_count_before": bundle.trusted_count_before,
        "trusted_count_after": bundle.trusted_count_after,
    }
    if (
        result.get("recall") != expected_recall
        or result.get("contamination") != expected_contamination
        or result.get("write_guard") != expected_write_guard
    ):
        _reject_state()


def _verify_checkpoint_receipt(result_payload: bytes, receipt_path: Path) -> None:
    descriptor = -1
    try:
        before = receipt_path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size != 65
        ):
            _reject_state()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(receipt_path, flags)
        opened = os.fstat(descriptor)
        if _stable_evidence_metadata(before) != _stable_evidence_metadata(opened):
            _reject_state()
        payload = os.read(descriptor, 66)
        if len(payload) != 65 or os.read(descriptor, 1):
            _reject_state()
        after = receipt_path.lstat()
        if _stable_evidence_metadata(opened) != _stable_evidence_metadata(after):
            _reject_state()
        receipt = payload.decode("ascii", errors="strict")
    except (OSError, UnicodeError):
        _reject_state()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if (
        not receipt.endswith("\n")
        or not _sha256_value(receipt[:-1])
        or receipt[:-1] != hashlib.sha256(result_payload).hexdigest()
    ):
        _reject_state()


def _checkpoint_receipt_is_complete(receipt_path: Path) -> bool:
    try:
        metadata = receipt_path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and metadata.st_size == 65
    )


def _quarantine_incomplete_run(run_root: Path) -> None:
    quarantine: Path | None = None
    try:
        before = run_root.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISDIR(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            _reject_state()
        quarantine = Path(
            tempfile.mkdtemp(
                prefix=f".{run_root.name}.incomplete-",
                dir=run_root.parent,
            )
        )
        quarantine.chmod(0o700)
        quarantine_metadata = quarantine.lstat()
        if (
            stat.S_ISLNK(quarantine_metadata.st_mode)
            or not stat.S_ISDIR(quarantine_metadata.st_mode)
            or quarantine_metadata.st_uid != os.getuid()
            or stat.S_IMODE(quarantine_metadata.st_mode) != 0o700
        ):
            _reject_state()
        evidence = quarantine / "evidence"
        run_root.rename(evidence)
        after = evidence.lstat()
        if (
            (before.st_dev, before.st_ino, before.st_mode)
            != (after.st_dev, after.st_ino, after.st_mode)
            or run_root.exists()
            or run_root.is_symlink()
        ):
            _reject_state()
    except BenchmarkStateError:
        raise
    except OSError:
        _reject_state()


def _checkpoint_evidence_sha256(path: Path) -> str:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size < 0
            or before.st_size > _MAX_CHECKPOINT_EVIDENCE_BYTES
        ):
            _reject_state()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stable_evidence_metadata(before) != _stable_evidence_metadata(opened):
            _reject_state()
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 64 * 1024):
            total += len(chunk)
            if total > _MAX_CHECKPOINT_EVIDENCE_BYTES:
                _reject_state()
            digest.update(chunk)
        after = path.lstat()
        if total != before.st_size or _stable_evidence_metadata(
            opened
        ) != _stable_evidence_metadata(after):
            _reject_state()
        return digest.hexdigest()
    except BenchmarkStateError:
        raise
    except OSError:
        _reject_state()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise AssertionError("unreachable")


def _stable_evidence_metadata(metadata: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _verify_public(arguments: argparse.Namespace) -> int:
    results = read_public_jsonl(arguments.input)
    scope = _state_string(vars(arguments), "scope")
    _validate_official_public_results(results, scope)
    print(f"artifact_scope={scope}")
    print(f"verified_public_runs={len(results)}")
    print("runtime_evidence=self-reported")
    print(f"public_result_sha256={_sha256_file(arguments.input)}")
    return 0


def _scope_protocol_keys(
    scope: str,
    tasks: Sequence[TaskSpec],
    execution: dict[str, object],
) -> frozenset[tuple[str, int, str]]:
    task_ids = tuple(task.task_id for task in tasks)
    if task_ids != tuple(f"T{index:02d}" for index in range(1, 9)):
        _reject_state()
    pilot_replicates = _state_integer(execution, "pilot_replicates")
    public_replicates = _state_integer(execution, "public_replicates")
    if pilot_replicates != 1 or public_replicates != 3:
        _reject_state()
    selected_ids: tuple[str, ...]
    if scope == "single-pair-pilot":
        selected_ids = ("T01",)
        replicates = pilot_replicates
    elif scope == "full-pilot":
        selected_ids = task_ids
        replicates = pilot_replicates
    elif scope == "public-v1":
        selected_ids = task_ids
        replicates = public_replicates
    else:
        _reject_state()
    return frozenset(
        (task_id, replicate, condition)
        for task_id in selected_ids
        for replicate in range(1, replicates + 1)
        for condition in ("baseline", "pmh")
    )


def _validate_official_public_results(
    results: Sequence[dict[str, object]],
    scope: str,
) -> None:
    tasks, execution, recall_contract, manifest_sha256 = _manifest_snapshot(DEFAULT_MANIFEST)
    public_contract = _manifest_public_contract(DEFAULT_MANIFEST)
    task_by_id = {task.task_id: task for task in tasks}
    brief_by_task = recall_contract.get("brief_sha256_by_task")
    memory_snapshot_sha256 = recall_contract.get("memory_snapshot_sha256")
    fixture_commit_by_fixture = public_contract.get("fixture_commit_by_fixture")
    workspace_sha256_by_fixture = public_contract.get("workspace_sha256_by_fixture")
    task_sha256_by_id = public_contract.get("task_sha256_by_id")
    if not isinstance(brief_by_task, dict) or not isinstance(memory_snapshot_sha256, str):
        _reject_state()
    if (
        not isinstance(fixture_commit_by_fixture, dict)
        or not isinstance(workspace_sha256_by_fixture, dict)
        or not isinstance(task_sha256_by_id, dict)
    ):
        _reject_state()
    public_replicates = _state_integer(execution, "public_replicates")
    expected_model = _state_string(execution, "requested_model_id")
    expected_cli = _state_string(execution, "codex_cli_version")
    expected_reasoning = _state_string(execution, "reasoning_effort")
    expected_timeout = _state_integer(execution, "timeout_seconds")
    seen_run_ids: set[str] = set()
    seen_protocol_keys: set[tuple[str, int, str]] = set()
    expected_protocol_keys = _scope_protocol_keys(scope, tasks, execution)
    artifact_harness_identity: tuple[str, str] | None = None
    for result in results:
        task_id = _string(result, "task_id")
        task = task_by_id.get(task_id)
        replicate = _integer(result, "replicate")
        condition = _string(result, "condition")
        if (
            task is None
            or result.get("artifact_scope") != scope
            or result.get("runtime_evidence_class") != "self-reported"
            or result.get("manifest_sha256") != manifest_sha256
            or result.get("task_version") != task.version
            or type(replicate) is not int
            or replicate < 1
            or replicate > public_replicates
            or condition not in {"baseline", "pmh"}
            or result.get("requested_model_id") != expected_model
            or result.get("codex_cli_version") != expected_cli
            or result.get("reasoning_effort") != expected_reasoning
            or result.get("timeout_seconds") != expected_timeout
            or result.get("memory_snapshot_sha256") != memory_snapshot_sha256
        ):
            _reject_state()
        expected_order = task.order
        if replicate % 2 == 0:
            expected_order = "BA" if expected_order == "AB" else "AB"
        expected_run_id = f"run-{task_id.casefold()}-r{replicate}-{condition}"
        run_id = _string(result, "run_id")
        protocol_key = (task_id, replicate, condition)
        if (
            result.get("order") != expected_order
            or run_id != expected_run_id
            or run_id in seen_run_ids
            or protocol_key in seen_protocol_keys
        ):
            _reject_state()
        seen_run_ids.add(run_id)
        seen_protocol_keys.add(protocol_key)
        harness_commit = _string(result, "harness_commit")
        harness_sha256 = _string(result, "harness_sha256")
        if artifact_harness_identity is None:
            expected_harness_sha256 = _harness_sha256_at_commit(
                manifest_sha256,
                harness_commit,
            )
            artifact_harness_identity = (harness_commit, expected_harness_sha256)
        if (harness_commit, harness_sha256) != artifact_harness_identity:
            _reject_state()
        task_contract = task_sha256_by_id.get(task_id)
        if not isinstance(task_contract, dict):
            _reject_state()
        expected_prompt_field = f"{condition}_prompt_sha256"
        if (
            result.get("fixture_commit") != fixture_commit_by_fixture.get(task.fixture_id)
            or result.get("workspace_sha256") != workspace_sha256_by_fixture.get(task.fixture_id)
            or result.get("prompt_sha256") != task_contract.get(expected_prompt_field)
            or result.get("validator_sha256") != task_contract.get("validator_sha256")
            or result.get("model_sandbox_profile_sha256")
            != task_contract.get("model_sandbox_profile_sha256")
        ):
            _reject_state()
        success = result.get("success")
        if success is True and (
            result.get("tests_total") != task.expected_tests
            or result.get("tests_passed") != task.expected_tests
        ):
            _reject_state()
        recall = result.get("recall")
        if not isinstance(recall, dict):
            _reject_state()
        if condition == "pmh":
            estimated_tokens = recall.get("estimated_tokens")
            selected_count = recall.get("selected_count")
            if (
                recall.get("brief_sha256") != brief_by_task.get(task_id)
                or type(estimated_tokens) is not int
                or estimated_tokens < 1
                or estimated_tokens > 800
                or type(selected_count) is not int
                or selected_count <= 0
            ):
                _reject_state()
        observed_model = result.get("observed_model_id")
        if (
            observed_model is not None
            and observed_model != expected_model
            and (
                result.get("success") is not False
                or result.get("invalid_reason") != "model_mismatch"
            )
        ):
            _reject_state()
    if seen_protocol_keys != expected_protocol_keys:
        _reject_state()


def _seal_execution_envelope(
    root: Path,
    tasks: Sequence[TaskSpec],
    config: ExecutionConfig,
) -> None:
    task_ids = {task.task_id for task in tasks}
    if len(tasks) != 8 or task_ids != {f"T{index:02d}" for index in range(1, 9)}:
        _reject_state()
    boundary_files = {
        "codex": config.codex_executable,
        "codex_auth": config.codex_auth_file,
        "git": benchmark_git_executable(),
        "validator_python": config.validator_python_executable,
        "validator_sandbox": config.validator_sandbox_executable,
    }
    envelope: dict[str, object] = {
        "schema_version": _EXECUTION_ENVELOPE_VERSION,
        "artifact_scope": config.artifact_scope,
        "manifest_sha256": config.manifest_sha256,
        "harness_commit": config.harness_commit,
        "harness_sha256": config.harness_sha256,
        "codex_cli_version": config.codex_cli_version,
        "requested_model_id": config.requested_model_id,
        "reasoning_effort": config.reasoning_effort,
        "timeout_seconds": config.timeout_seconds,
        "boundary_files": {
            name: {
                "path": os.fspath(path),
                "sha256": _stable_boundary_file_sha256(path),
            }
            for name, path in sorted(boundary_files.items())
        },
        "managed_requirements": _managed_requirements_boundary(),
        "model_sandbox_profile_sha256_by_task": {
            task.task_id: _model_sandbox_profile_sha256(
                config,
                root / "fixtures" / task.fixture_id,
                task.allowed_paths,
            )
            for task in tasks
        },
    }
    payload = canonical_json_bytes(envelope)
    envelope_path = root / _EXECUTION_ENVELOPE_FILE
    receipt_path = root / _EXECUTION_ENVELOPE_RECEIPT
    envelope_present = envelope_path.exists() or envelope_path.is_symlink()
    receipt_present = receipt_path.exists() or receipt_path.is_symlink()
    if not envelope_present and not receipt_present:
        _write_exclusive(envelope_path, payload)
        _write_exclusive(
            receipt_path,
            (hashlib.sha256(payload).hexdigest() + "\n").encode("ascii"),
        )
        return
    if not envelope_present or not receipt_present:
        _reject_state()
    existing = _read_private_json(
        envelope_path,
        receipt_path=receipt_path,
    )
    if existing != envelope:
        _reject_state()


def _managed_requirements_boundary() -> dict[str, str]:
    logical = _MANAGED_REQUIREMENTS_LOGICAL_PATH
    canonical = _MANAGED_REQUIREMENTS_CANONICAL_PATH
    if not logical.is_absolute() or not canonical.is_absolute():
        _reject_state()
    try:
        logical.lstat()
    except FileNotFoundError:
        try:
            canonical.lstat()
        except FileNotFoundError:
            return {"status": "absent"}
        except OSError:
            _reject_state()
        _reject_state()
    except OSError:
        _reject_state()
    try:
        canonical.lstat()
        resolved_before = logical.resolve(strict=True)
        canonical_before = canonical.resolve(strict=True)
    except OSError:
        _reject_state()
    if resolved_before != canonical or canonical_before != canonical:
        _reject_state()
    digest = _stable_boundary_file_sha256(canonical)
    try:
        resolved_after = logical.resolve(strict=True)
        canonical_after = canonical.resolve(strict=True)
    except OSError:
        _reject_state()
    if resolved_after != resolved_before or canonical_after != canonical_before:
        _reject_state()
    return {
        "status": "present",
        "path": os.fspath(canonical),
        "sha256": digest,
    }


def _stable_boundary_file_sha256(path: Path) -> str:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not path.is_absolute()
            or stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid not in {0, os.getuid()}
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size <= 0
            or before.st_size > _MAX_BOUNDARY_FILE_BYTES
        ):
            _reject_state()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stable_evidence_metadata(before) != _stable_evidence_metadata(opened):
            _reject_state()
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            total += len(chunk)
            if total > _MAX_BOUNDARY_FILE_BYTES:
                _reject_state()
            digest.update(chunk)
        after = path.lstat()
        if total != before.st_size or _stable_evidence_metadata(
            opened
        ) != _stable_evidence_metadata(after):
            _reject_state()
        return digest.hexdigest()
    except BenchmarkStateError:
        raise
    except OSError:
        _reject_state()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise AssertionError("unreachable")


def _load_state(
    root: Path,
    manifest_sha256: str,
    harness_commit: str,
    harness_sha256: str,
    recall_contract: dict[str, object],
) -> tuple[tuple[FixtureRepository, ...], RecallBundle, dict[str, object]]:
    document = _read_private_json(
        root / _STATE_FILE,
        receipt_path=root / _STATE_RECEIPT,
    )
    expected = {
        "schema_version",
        "manifest_sha256",
        "recall_contract_sha256",
        "harness_commit",
        "harness_sha256",
        "execution",
        "repositories",
        "recall_bundle",
    }
    if set(document) != expected or document["schema_version"] != _STATE_VERSION:
        _reject_state()
    if (
        document["manifest_sha256"] != manifest_sha256
        or document["recall_contract_sha256"] != _recall_contract_sha256(recall_contract)
        or document["harness_commit"] != harness_commit
        or document["harness_sha256"] != harness_sha256
    ):
        _reject_state()
    execution = document["execution"]
    raw_repositories = document["repositories"]
    raw_bundle = document["recall_bundle"]
    if (
        not isinstance(execution, dict)
        or not isinstance(raw_repositories, list)
        or not isinstance(raw_bundle, dict)
    ):
        _reject_state()
    repositories: list[FixtureRepository] = []
    for raw in raw_repositories:
        if not isinstance(raw, dict) or set(raw) != {"fixture_id", "commit"}:
            _reject_state()
        fixture_id = raw["fixture_id"]
        commit = raw["commit"]
        if (
            fixture_id not in {"ledger-tools", "release-kit"}
            or not isinstance(commit, str)
            or len(commit) != 40
        ):
            _reject_state()
        path = root / "fixtures" / fixture_id
        _verify_fixture(path, commit)
        repositories.append(
            FixtureRepository(
                fixture_id=fixture_id,
                path=path,
                commit=commit,
            )
        )
    if {repository.fixture_id for repository in repositories} != {
        "ledger-tools",
        "release-kit",
    }:
        _reject_state()
    bundle = _decode_bundle(raw_bundle)
    _verify_recall_contract(bundle, recall_contract)
    if bundle.model_id != execution.get("requested_model_id"):
        _reject_state()
    return (tuple(repositories), bundle, dict(execution))


def _decode_bundle(raw: dict[str, object]) -> RecallBundle:
    expected = {
        "model_id",
        "memory_snapshot_sha256",
        "memory_snapshot_unchanged",
        "pending_count_before",
        "pending_count_after",
        "trusted_count_before",
        "trusted_count_after",
        "recalls",
    }
    if set(raw) != expected or not isinstance(raw["recalls"], list):
        _reject_state()
    recalls: list[RecallEvidence] = []
    recall_fields = set(RecallEvidence.__dataclass_fields__)
    for value in raw["recalls"]:
        if not isinstance(value, dict) or set(value) != recall_fields:
            _reject_state()
        try:
            recall = RecallEvidence(**value)
        except TypeError:
            _reject_state()
        if not _valid_recall_evidence(recall):
            _reject_state()
        recalls.append(recall)
    try:
        bundle = RecallBundle(
            model_id=_state_string(raw, "model_id"),
            recalls=tuple(recalls),
            memory_snapshot_sha256=_state_string(
                raw,
                "memory_snapshot_sha256",
            ),
            memory_snapshot_unchanged=_state_boolean(
                raw,
                "memory_snapshot_unchanged",
            ),
            pending_count_before=_state_integer(raw, "pending_count_before"),
            pending_count_after=_state_integer(raw, "pending_count_after"),
            trusted_count_before=_state_integer(raw, "trusted_count_before"),
            trusted_count_after=_state_integer(raw, "trusted_count_after"),
        )
    except (TypeError, ValueError):
        _reject_state()
    if (
        len(bundle.recalls) != 8
        or {recall.task_id for recall in bundle.recalls}
        != {f"T{index:02d}" for index in range(1, 9)}
        or bundle.pending_count_before != 0
        or bundle.pending_count_after != 0
        or not bundle.memory_snapshot_unchanged
        or bundle.trusted_count_before != bundle.trusted_count_after
        or not _sha256_value(bundle.memory_snapshot_sha256)
    ):
        _reject_state()
    return bundle


def _verify_recall_contract(
    bundle: RecallBundle,
    contract: dict[str, object],
) -> None:
    expected_briefs = contract.get("brief_sha256_by_task")
    if (
        not isinstance(expected_briefs, dict)
        or bundle.memory_snapshot_sha256 != contract.get("memory_snapshot_sha256")
        or {recall.task_id: recall.brief_sha256 for recall in bundle.recalls} != expected_briefs
    ):
        _reject_state()


def _recall_contract_sha256(contract: dict[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(contract)).hexdigest()


def _valid_recall_evidence(recall: RecallEvidence) -> bool:
    try:
        text_bytes = recall.text.encode("utf-8", errors="strict")
    except UnicodeError:
        return False
    counts = (
        recall.estimated_tokens,
        recall.selected_count,
        recall.omitted_count,
        recall.candidate_estimated_tokens,
        recall.project_canary_hits,
        recall.source_canary_hits,
        recall.model_canary_hits,
    )
    return (
        recall.text == recall.text.strip()
        and 0 < len(text_bytes) <= 64 * 1024
        and all(type(value) is int and 0 <= value <= 100_000 for value in counts)
        and 0 < recall.estimated_tokens <= 800
        and recall.selected_count > 0
        and recall.candidate_estimated_tokens >= recall.estimated_tokens
        and recall.project_canary_hits == recall.source_canary_hits == recall.model_canary_hits == 0
        and _sha256_value(recall.brief_sha256)
        and recall.brief_sha256 == hashlib.sha256(text_bytes).hexdigest()
    )


def _sha256_value(value: str) -> bool:
    return (
        len(value) == 64
        and value == value.casefold()
        and all(character in "0123456789abcdef" for character in value)
    )


def _manifest_snapshot(
    path: Path,
) -> tuple[tuple[TaskSpec, ...], dict[str, object], dict[str, object], str]:
    payload = _read_stable_source_bytes(path, maximum=_MAX_MANIFEST_BYTES)
    manifest_sha256 = hashlib.sha256(payload).hexdigest()
    if manifest_sha256 != _OFFICIAL_MANIFEST_SHA256:
        _reject_state()
    document = _decode_source_json(payload)
    execution = _manifest_execution_document(document)
    recall_contract = _manifest_recall_contract_document(document)
    with tempfile.TemporaryDirectory(prefix=".pmh-manifest-snapshot-") as temporary:
        snapshot = Path(temporary) / "manifest.json"
        _write_exclusive(snapshot, payload)
        tasks = load_task_specs(snapshot)
    return (tuple(tasks), execution, recall_contract, manifest_sha256)


def _manifest_execution(path: Path) -> dict[str, object]:
    _, execution, _, _ = _manifest_snapshot(path)
    return execution


def _manifest_execution_document(document: dict[str, object]) -> dict[str, object]:
    execution = document.get("execution")
    if (
        set(document)
        != {
            "benchmark_id",
            "conditions",
            "execution",
            "order_policy",
            "public_contract",
            "recall_contract",
            "schema_version",
            "status",
            "tasks",
        }
        or document.get("benchmark_id") != "project-memory-effect-public-v1"
        or document.get("schema_version") != 1
        or document.get("status") != "preregistered"
        or document.get("order_policy") != "task-id-parity-balanced-v1"
        or document.get("conditions")
        != {
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
        or not isinstance(execution, dict)
    ):
        _reject_state()
    required = {
        "codex_cli_version",
        "ephemeral",
        "ignore_user_config",
        "pilot_replicates",
        "public_replicates",
        "reasoning_effort",
        "requested_model_id",
        "sandbox",
        "timeout_seconds",
    }
    if (
        set(execution) != required
        or execution.get("ephemeral") is not True
        or execution.get("ignore_user_config") is not True
        or execution.get("sandbox") != "macos-external-seatbelt-v2"
        or not isinstance(execution.get("requested_model_id"), str)
        or not isinstance(execution.get("reasoning_effort"), str)
        or type(execution.get("timeout_seconds")) is not int
        or int(execution["timeout_seconds"]) <= 0
    ):
        _reject_state()
    return dict(execution)


def _manifest_recall_contract_document(
    document: dict[str, object],
) -> dict[str, object]:
    raw = document.get("recall_contract")
    if not isinstance(raw, dict) or set(raw) != {
        "brief_sha256_by_task",
        "memory_snapshot_sha256",
    }:
        _reject_state()
    briefs = raw.get("brief_sha256_by_task")
    memory_snapshot = raw.get("memory_snapshot_sha256")
    expected_task_ids = {f"T{index:02d}" for index in range(1, 9)}
    if (
        not isinstance(briefs, dict)
        or set(briefs) != expected_task_ids
        or any(
            not isinstance(task_id, str) or not isinstance(digest, str) or not _sha256_value(digest)
            for task_id, digest in briefs.items()
        )
        or not isinstance(memory_snapshot, str)
        or not _sha256_value(memory_snapshot)
    ):
        _reject_state()
    return {
        "brief_sha256_by_task": dict(briefs),
        "memory_snapshot_sha256": memory_snapshot,
    }


def _manifest_public_contract(path: Path) -> dict[str, object]:
    payload = _read_stable_source_bytes(path, maximum=_MAX_MANIFEST_BYTES)
    if hashlib.sha256(payload).hexdigest() != _OFFICIAL_MANIFEST_SHA256:
        _reject_state()
    return _manifest_public_contract_document(_decode_source_json(payload))


def _manifest_public_contract_document(
    document: dict[str, object],
) -> dict[str, object]:
    raw = document.get("public_contract")
    if not isinstance(raw, dict) or set(raw) != {
        "fixture_commit_by_fixture",
        "task_sha256_by_id",
        "workspace_sha256_by_fixture",
    }:
        _reject_state()
    fixture_commits = raw.get("fixture_commit_by_fixture")
    workspace_hashes = raw.get("workspace_sha256_by_fixture")
    task_hashes = raw.get("task_sha256_by_id")
    fixture_ids = {"ledger-tools", "release-kit"}
    task_ids = {f"T{index:02d}" for index in range(1, 9)}
    if (
        not isinstance(fixture_commits, dict)
        or set(fixture_commits) != fixture_ids
        or any(
            not isinstance(value, str)
            or len(value) != 40
            or value != value.casefold()
            or any(character not in "0123456789abcdef" for character in value)
            for value in fixture_commits.values()
        )
        or not isinstance(workspace_hashes, dict)
        or set(workspace_hashes) != fixture_ids
        or any(
            not isinstance(value, str) or not _sha256_value(value)
            for value in workspace_hashes.values()
        )
        or not isinstance(task_hashes, dict)
        or set(task_hashes) != task_ids
    ):
        _reject_state()
    required_task_hashes = {
        "baseline_prompt_sha256",
        "model_sandbox_profile_sha256",
        "pmh_prompt_sha256",
        "validator_sha256",
    }
    for value in task_hashes.values():
        if (
            not isinstance(value, dict)
            or set(value) != required_task_hashes
            or any(
                not isinstance(digest, str) or not _sha256_value(digest)
                for digest in value.values()
            )
        ):
            _reject_state()
    return {
        "fixture_commit_by_fixture": dict(fixture_commits),
        "task_sha256_by_id": {
            task_id: dict(value)
            for task_id, value in task_hashes.items()
            if isinstance(value, dict)
        },
        "workspace_sha256_by_fixture": dict(workspace_hashes),
    }


def _harness_identity(
    manifest_sha256: str,
    *,
    require_committed: bool = False,
) -> tuple[str, str]:
    if manifest_sha256 != _OFFICIAL_MANIFEST_SHA256:
        _reject_state()
    try:
        root = Path(
            _harness_git_bytes(
                "rev-parse",
                "--show-toplevel",
                maximum=4096,
            )
            .decode("utf-8", errors="strict")
            .strip()
        ).resolve(strict=True)
        if root != PROJECT_ROOT.resolve(strict=True):
            _reject_state()
        harness_commit = (
            _harness_git_bytes(
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
                maximum=128,
            )
            .decode("ascii", errors="strict")
            .strip()
        )
    except (OSError, UnicodeError, subprocess.SubprocessError):
        _reject_state()
    if (
        len(harness_commit) != 40
        or harness_commit != harness_commit.casefold()
        or any(character not in "0123456789abcdef" for character in harness_commit)
    ):
        _reject_state()
    digest = hashlib.sha256()
    digest.update(b"project-memory-effect-harness-v1\0")
    digest.update(manifest_sha256.encode("ascii"))
    digest.update(b"\0")
    harness_paths = _harness_paths_at_commit(harness_commit)
    if require_committed:
        _verify_harness_source_tree(harness_paths)
    for relative in harness_paths:
        payload = _read_stable_source_bytes(
            PROJECT_ROOT / relative,
            maximum=_MAX_STATE_BYTES,
        )
        if require_committed:
            committed_mode, committed_payload = _harness_commit_blob(
                harness_commit,
                relative,
            )
            try:
                working_mode = stat.S_IMODE((PROJECT_ROOT / relative).lstat().st_mode)
            except OSError:
                _reject_state()
            if payload != committed_payload or bool(working_mode & stat.S_IXUSR) != bool(
                committed_mode & stat.S_IXUSR
            ):
                _reject_state()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    return (harness_commit, digest.hexdigest())


def _harness_sha256_at_commit(
    manifest_sha256: str,
    harness_commit: str,
) -> str:
    if (
        manifest_sha256 != _OFFICIAL_MANIFEST_SHA256
        or len(harness_commit) != 40
        or harness_commit != harness_commit.casefold()
        or any(character not in "0123456789abcdef" for character in harness_commit)
    ):
        _reject_state()
    resolved_commit = (
        _harness_git_bytes(
            "rev-parse",
            "--verify",
            f"{harness_commit}^{{commit}}",
            maximum=128,
        )
        .decode("ascii", errors="strict")
        .strip()
    )
    if resolved_commit != harness_commit:
        _reject_state()
    digest = hashlib.sha256()
    digest.update(b"project-memory-effect-harness-v1\0")
    digest.update(manifest_sha256.encode("ascii"))
    digest.update(b"\0")
    manifest_seen = False
    for relative in _harness_paths_at_commit(harness_commit):
        _, payload = _harness_commit_blob(harness_commit, relative)
        if relative == Path("benchmarks/project-memory-effect/manifest.json"):
            manifest_seen = True
            if hashlib.sha256(payload).hexdigest() != manifest_sha256:
                _reject_state()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
        digest.update(b"\0")
    if not manifest_seen:
        _reject_state()
    return digest.hexdigest()


def _harness_paths_at_commit(harness_commit: str) -> tuple[Path, ...]:
    tree = _harness_git_bytes(
        "ls-tree",
        "-rz",
        "--full-tree",
        harness_commit,
        "--",
        *(root.as_posix() for root in _HARNESS_TREE_ROOTS),
        maximum=_MAX_HARNESS_TREE_BYTES,
    )
    selected = set(_HARNESS_PATHS)
    records = tree.split(b"\0")
    if not records or records[-1] != b"":
        _reject_state()
    for raw_record in records[:-1]:
        try:
            raw_metadata, raw_path = raw_record.split(b"\t", 1)
            raw_mode, raw_kind, raw_object = raw_metadata.split(b" ", 2)
            relative = Path(raw_path.decode("utf-8", errors="strict"))
            object_id = raw_object.decode("ascii", errors="strict")
        except (UnicodeError, ValueError):
            _reject_state()
        if (
            raw_kind != b"blob"
            or raw_mode not in {b"100644", b"100755"}
            or len(object_id) != 40
            or object_id != object_id.casefold()
            or any(character not in "0123456789abcdef" for character in object_id)
            or relative.is_absolute()
            or not relative.parts
            or "." in relative.parts
            or ".." in relative.parts
            or len(selected) >= _MAX_HARNESS_PATHS
        ):
            _reject_state()
        selected.add(relative)
    if not selected:
        _reject_state()
    return tuple(sorted(selected, key=lambda path: path.as_posix()))


def _verify_harness_source_tree(expected_paths: tuple[Path, ...]) -> None:
    expected = {
        relative
        for relative in expected_paths
        if any(relative == root or root in relative.parents for root in _HARNESS_TREE_ROOTS)
    }
    actual: set[Path] = set()
    path_count = 0
    try:
        for relative_root in _HARNESS_TREE_ROOTS:
            root = PROJECT_ROOT / relative_root
            root_metadata = root.lstat()
            if (
                stat.S_ISLNK(root_metadata.st_mode)
                or not stat.S_ISDIR(root_metadata.st_mode)
                or root_metadata.st_uid != os.getuid()
            ):
                _reject_state()
            directories = [root]
            while directories:
                directory = directories.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        path_count += 1
                        if path_count > _MAX_HARNESS_PATHS:
                            _reject_state()
                        path = Path(entry.path)
                        relative = path.relative_to(PROJECT_ROOT)
                        metadata = entry.stat(follow_symlinks=False)
                        if (
                            stat.S_ISDIR(metadata.st_mode)
                            and not stat.S_ISLNK(metadata.st_mode)
                            and metadata.st_uid == os.getuid()
                        ):
                            if path.name == "__pycache__":
                                _reject_state()
                            directories.append(path)
                            continue
                        if (
                            not stat.S_ISREG(metadata.st_mode)
                            or stat.S_ISLNK(metadata.st_mode)
                            or metadata.st_uid != os.getuid()
                        ):
                            _reject_state()
                        actual.add(relative)
    except BenchmarkStateError:
        raise
    except (OSError, UnicodeError, ValueError):
        _reject_state()
    if actual != expected:
        _reject_state()


def _harness_commit_blob(
    commit: str,
    relative: Path,
) -> tuple[int, bytes]:
    if (
        relative.is_absolute()
        or not relative.parts
        or "." in relative.parts
        or ".." in relative.parts
    ):
        _reject_state()
    tree = _harness_git_bytes(
        "ls-tree",
        "-z",
        commit,
        "--",
        relative.as_posix(),
        maximum=4096,
    )
    records = tree.split(b"\0")
    if len(records) != 2 or records[-1] != b"":
        _reject_state()
    try:
        raw_metadata, raw_path = records[0].split(b"\t", 1)
        raw_mode, raw_kind, raw_object = raw_metadata.split(b" ", 2)
        recorded_path = raw_path.decode("utf-8", errors="strict")
        object_id = raw_object.decode("ascii", errors="strict")
    except (UnicodeError, ValueError):
        _reject_state()
    if (
        raw_kind != b"blob"
        or raw_mode not in {b"100644", b"100755"}
        or recorded_path != relative.as_posix()
        or len(object_id) != 40
        or object_id != object_id.casefold()
        or any(character not in "0123456789abcdef" for character in object_id)
    ):
        _reject_state()
    payload = _harness_git_bytes(
        "cat-file",
        "blob",
        object_id,
        maximum=_MAX_STATE_BYTES,
    )
    git_object = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
    if hashlib.sha1(git_object, usedforsecurity=False).hexdigest() != object_id:
        _reject_state()
    return (0o755 if raw_mode == b"100755" else 0o644, payload)


def _harness_git_bytes(*arguments: str, maximum: int) -> bytes:
    environment = {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": os.fspath(PROJECT_ROOT),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", os.defpath),
        "TZ": "UTC",
    }
    try:
        completed = subprocess.run(
            (
                os.fspath(benchmark_git_executable()),
                "--no-replace-objects",
                *arguments,
            ),
            cwd=PROJECT_ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _reject_state()
    if completed.returncode != 0 or len(completed.stdout) > maximum:
        _reject_state()
    return completed.stdout


def _verify_fixture(path: Path, commit: str) -> None:
    try:
        verify_fixture_repository(
            FixtureRepository(
                fixture_id=path.name,
                path=path,
                commit=commit,
            )
        )
    except BenchmarkFixtureError:
        _reject_state()


def _resolve_executable(selected: Path | None, fallback: str) -> Path:
    candidate: str | None
    if selected is None:
        candidate = shutil.which(fallback) if "/" not in fallback else fallback
    else:
        candidate = os.fspath(selected)
    if not candidate:
        _reject_state()
    try:
        executable = Path(candidate).resolve(strict=True)
        metadata = executable.lstat()
    except OSError:
        _reject_state()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not os.access(executable, os.X_OK)
    ):
        _reject_state()
    return executable


def _codex_version(executable: Path) -> str:
    try:
        completed = subprocess.run(
            [executable, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _reject_state()
    version = completed.stdout.strip()
    if not version or "\n" in version or len(version) > 200:
        _reject_state()
    return version


def _read_private_json(
    path: Path,
    *,
    receipt_path: Path,
) -> dict[str, object]:
    payload = _read_stable_private_bytes(path)
    _verify_checkpoint_receipt(payload, receipt_path)
    try:
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except BenchmarkStateError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError):
        _reject_state()
    if not isinstance(document, dict):
        _reject_state()
    return document


def _read_stable_private_bytes(path: Path) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or before.st_size <= 0
            or before.st_size > _MAX_STATE_BYTES
        ):
            _reject_state()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stable_evidence_metadata(before) != _stable_evidence_metadata(opened):
            _reject_state()
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(
            descriptor,
            min(64 * 1024, _MAX_STATE_BYTES - total + 1),
        ):
            total += len(chunk)
            if total > _MAX_STATE_BYTES:
                _reject_state()
            chunks.append(chunk)
        after = path.lstat()
        if total != before.st_size or _stable_evidence_metadata(
            opened
        ) != _stable_evidence_metadata(after):
            _reject_state()
        return b"".join(chunks)
    except BenchmarkStateError:
        raise
    except OSError:
        _reject_state()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise AssertionError("unreachable")


def _decode_source_json(payload: bytes) -> dict[str, object]:
    try:
        document = json.loads(
            payload.decode("utf-8", errors="strict"),
            object_pairs_hook=_unique_object,
        )
    except BenchmarkStateError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError):
        _reject_state()
    if not isinstance(document, dict):
        _reject_state()
    return document


def _read_stable_source_bytes(path: Path, *, maximum: int) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            stat.S_ISLNK(before.st_mode)
            or not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_size <= 0
            or before.st_size > maximum
        ):
            _reject_state()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stable_evidence_metadata(before) != _stable_evidence_metadata(opened):
            _reject_state()
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(64 * 1024, maximum - total + 1)):
            total += len(chunk)
            if total > maximum:
                _reject_state()
            chunks.append(chunk)
        after = path.lstat()
        if total != before.st_size or _stable_evidence_metadata(
            opened
        ) != _stable_evidence_metadata(after):
            _reject_state()
        return b"".join(chunks)
    except BenchmarkStateError:
        raise
    except OSError:
        _reject_state()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise AssertionError("unreachable")


def _write_exclusive(path: Path, payload: bytes) -> None:
    if path.exists() or path.is_symlink():
        _reject_state()
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        _reject_state()


def _sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(64 * 1024):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        _reject_state()


def _string(document: dict[str, object], field: str) -> str:
    value = document.get(field)
    if not isinstance(value, str):
        _reject_state()
    return value


def _optional_string(
    document: dict[str, object],
    field: str,
) -> str | None:
    value = document.get(field)
    if value is not None and not isinstance(value, str):
        _reject_state()
    return value


def _integer(document: dict[str, object], field: str) -> int:
    value = document.get(field)
    if type(value) is not int:
        _reject_state()
    return value


def _boolean(document: dict[str, object], field: str) -> bool:
    value = document.get(field)
    if type(value) is not bool:
        _reject_state()
    return value


def _nested_integer(
    document: dict[str, object],
    parent: str,
    field: str,
) -> int:
    nested = document.get(parent)
    if not isinstance(nested, dict):
        _reject_state()
    return _integer(nested, field)


def _nested_optional_integer(
    document: dict[str, object],
    parent: str,
    field: str,
) -> int | None:
    nested = document.get(parent)
    if not isinstance(nested, dict):
        _reject_state()
    value = nested.get(field)
    if value is not None and type(value) is not int:
        _reject_state()
    return value


def _state_string(document: dict[str, object], field: str) -> str:
    return _string(document, field)


def _state_integer(document: dict[str, object], field: str) -> int:
    value = _integer(document, field)
    if value < 0:
        _reject_state()
    return value


def _state_boolean(document: dict[str, object], field: str) -> bool:
    return _boolean(document, field)


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject_state()
        result[key] = value
    return result


def _reject_state() -> NoReturn:
    raise BenchmarkStateError("benchmark state rejected")


def main() -> int:
    arguments = _parser().parse_args()
    try:
        return int(arguments.handler(arguments))
    except (
        BenchmarkEvidenceError,
        BenchmarkFixtureError,
        BenchmarkRunnerError,
        BenchmarkStateError,
    ) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
