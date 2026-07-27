from __future__ import annotations

import ast
import ctypes
import hashlib
import json
import os
import re
import resource
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from functools import partial
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Literal, NoReturn

from project_memory_hub.benchmark import (
    BenchmarkEvidenceError,
    CodexEventSummary,
    TokenUsage,
    canonical_json_bytes,
    read_codex_event_summary,
)
from project_memory_hub.benchmark_fixtures import (
    BenchmarkFixtureError,
    FixtureRepository,
    RecallBundle,
    RecallEvidence,
    TaskSpec,
    materialize_fixture_repository,
)


Condition = Literal["baseline", "pmh"]

_BENCHMARK_ID = "project-memory-effect-public-v1"
_PRIVATE_FILE_MODE = 0o600
_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_EXECUTABLE_MODE = 0o700
_MAX_PRIVATE_EXECUTABLE_BYTES = 512 * 1024 * 1024
_MAX_WORKSPACE_FILE_BYTES = 8 * 1024 * 1024
_MAX_WORKSPACE_TOTAL_BYTES = 128 * 1024 * 1024
_MAX_WORKSPACE_PATHS = 10_000
_MAX_MODEL_OPEN_FILES = 256
_MODEL_PROCESS_HEADROOM = 64
_MODEL_CPU_GRACE_SECONDS = 15
_MAX_PRIVATE_EVENT_BYTES = 16 * 1024 * 1024
_MAX_PUBLIC_RESULT_BYTES = 4 * 1024 * 1024
_MAX_PUBLIC_RESULT_LINE_BYTES = 256 * 1024
_MAX_PUBLIC_INTEGER = 2**63 - 1
_VALIDATOR_PROTOCOL_VERSION = 3
_VALIDATOR_PROFILE_VERSION = "macos-seatbelt-validator-v4"
_VALIDATOR_AST_POLICY_VERSION = "validator-pure-python-v2"
_MANAGED_REQUIREMENTS_LOGICAL_PATH = Path("/etc/codex/requirements.toml")
_MANAGED_REQUIREMENTS_CANONICAL_PATH = Path("/private/etc/codex/requirements.toml")
_MODEL_EXECUTABLE_PATHS = (
    "/bin/cat",
    "/bin/chmod",
    "/bin/echo",
    "/bin/ls",
    "/bin/pwd",
    "/bin/rm",
    "/bin/zsh",
    "/Library/Developer/CommandLineTools/usr/bin/git",
    "/usr/bin/awk",
    "/usr/bin/basename",
    "/usr/bin/cmp",
    "/usr/bin/cut",
    "/usr/bin/diff",
    "/usr/bin/dirname",
    "/usr/bin/env",
    "/usr/bin/false",
    "/usr/bin/git",
    "/usr/bin/grep",
    "/usr/bin/head",
    "/usr/bin/printf",
    "/usr/bin/sed",
    "/usr/bin/sort",
    "/usr/bin/tail",
    "/usr/bin/test",
    "/usr/bin/tr",
    "/usr/bin/true",
    "/usr/bin/wc",
    "/usr/bin/xcrun",
)
_VALIDATOR_EXPECTED_OBSERVATIONS: dict[str, tuple[tuple[str, object], ...]] = {
    "T01": (
        ("preserve-trailing-spaces", {"kind": "return", "value": "alpha  "}),
        ("remove-one-crlf", {"kind": "return", "value": "beta"}),
    ),
    "T02": (
        (
            "nested-settings",
            {
                "kind": "return",
                "value": {
                    "ui": {
                        "columns": ["name"],
                        "density": "compact",
                        "theme": "ink",
                    }
                },
            },
        ),
    ),
    "T03": (
        (
            "retry-timeout",
            {"attempts": 3, "kind": "return", "value": "ok"},
        ),
        (
            "do-not-retry-value-error",
            {"attempts": 1, "kind": "exception", "type": "ValueError"},
        ),
    ),
    "T04": (
        (
            "aware-to-utc",
            {"kind": "return", "value": "2026-07-26T00:30:00Z"},
        ),
        ("reject-naive", {"kind": "exception", "type": "ValueError"}),
    ),
    "T05": (
        ("reject-symlink", {"kind": "exception", "type": "ValueError"}),
        ("normal-relative", {"kind": "return", "matches": True}),
    ),
    "T06": (
        (
            "platform-aliases",
            {
                "kind": "return",
                "value": [
                    "/var/folders/example",
                    "/private/various/example",
                    "/home/example/output",
                ],
            },
        ),
    ),
    "T07": (
        (
            "verified-order",
            {"kind": "return", "value": ["tests", "package", "privacy"]},
        ),
        ("reject-unknown", {"kind": "exception", "type": "ValueError"}),
    ),
    "T08": (
        (
            "highest-priority-open",
            {"kind": "return", "value": "privacy audit"},
        ),
        ("all-resolved", {"kind": "return", "value": None}),
    ),
}
_VALIDATOR_TARGETS = {
    "T01": ("ledger_tools/records.py", "normalize_record"),
    "T02": ("ledger_tools/settings.py", "merge_settings"),
    "T03": ("ledger_tools/retry.py", "run_with_retry"),
    "T04": ("ledger_tools/timefmt.py", "utc_stamp"),
    "T05": ("release_kit/paths.py", "safe_join"),
    "T06": ("release_kit/platform_paths.py", "normalize_platform_path"),
    "T07": ("release_kit/checks.py", "ordered_checks"),
    "T08": ("release_kit/issues.py", "next_open_issue"),
}
_VALIDATOR_ALLOWED_IMPORTS = frozenset(
    {
        "__future__",
        "collections",
        "collections.abc",
        "copy",
        "datetime",
        "functools",
        "pathlib",
        "re",
        "typing",
    }
)
_VALIDATOR_FORBIDDEN_NAMES = frozenset(
    {
        "__import__",
        "__builtins__",
        "asyncio",
        "breakpoint",
        "builtins",
        "compile",
        "ctypes",
        "delattr",
        "eval",
        "exec",
        "exit",
        "getattr",
        "globals",
        "help",
        "importlib",
        "input",
        "inspect",
        "locals",
        "marshal",
        "multiprocessing",
        "open",
        "os",
        "pickle",
        "print",
        "quit",
        "resource",
        "runpy",
        "setattr",
        "signal",
        "socket",
        "subprocess",
        "sys",
        "SystemExit",
        "threading",
        "unittest",
        "vars",
    }
)
_VALIDATOR_FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "_exit",
        "chmod",
        "chown",
        "execv",
        "execve",
        "fork",
        "hardlink_to",
        "kill",
        "link",
        "mkdir",
        "os",
        "popen",
        "remove",
        "rename",
        "rmdir",
        "socket",
        "spawn",
        "subprocess",
        "symlink",
        "symlink_to",
        "system",
        "threading",
        "touch",
        "truncate",
        "unlink",
        "write_bytes",
        "write_text",
    }
)
_VALIDATOR_WORKER = """\
import json
import os
import sys

_dumps = json.dumps
_urandom = os.urandom
_write = os.write
task_id = sys.argv[1]
case_id = sys.argv[2]
sys.path.insert(0, os.getcwd())


def load_target():
    if task_id == "T01":
        from ledger_tools.records import normalize_record
        return normalize_record
    if task_id == "T02":
        from ledger_tools.settings import merge_settings
        return merge_settings
    if task_id == "T03":
        from ledger_tools.retry import run_with_retry
        return run_with_retry
    if task_id == "T04":
        from ledger_tools.timefmt import utc_stamp
        return utc_stamp
    if task_id == "T05":
        from release_kit.paths import safe_join
        return safe_join
    if task_id == "T06":
        from release_kit.platform_paths import normalize_platform_path
        return normalize_platform_path
    if task_id == "T07":
        from release_kit.checks import ordered_checks
        return ordered_checks
    if task_id == "T08":
        from release_kit.issues import next_open_issue
        return next_open_issue
    raise RuntimeError("unknown validator task")


target = load_target()
nonce = _urandom(16).hex()


def observe():
    if task_id == "T01":
        value = "alpha  \\n" if case_id == "preserve-trailing-spaces" else "beta\\r\\n"
        return {"kind": "return", "value": target(value)}
    if task_id == "T02":
        return {
            "kind": "return",
            "value": target(
                {"ui": {"theme": "ink", "columns": ["name", "age"]}},
                {"ui": {"density": "compact", "columns": ["name"]}},
            ),
        }
    if task_id == "T03":
        attempts = [0]
        if case_id == "retry-timeout":
            def operation():
                attempts[0] += 1
                if attempts[0] < 3:
                    raise TimeoutError("temporary")
                return "ok"
        else:
            def operation():
                attempts[0] += 1
                raise ValueError("invalid")
        try:
            value = target(operation)
        except BaseException as error:
            return {
                "attempts": attempts[0],
                "kind": "exception",
                "type": type(error).__name__,
            }
        return {"attempts": attempts[0], "kind": "return", "value": value}
    if task_id == "T04":
        from datetime import datetime, timedelta, timezone
        value = (
            datetime(
                2026,
                7,
                26,
                8,
                30,
                tzinfo=timezone(timedelta(hours=8)),
            )
            if case_id == "aware-to-utc"
            else datetime(2026, 7, 26, 0, 30)
        )
        return {"kind": "return", "value": target(value)}
    if task_id == "T05":
        import tempfile
        from pathlib import Path
        if case_id == "reject-symlink":
            with tempfile.TemporaryDirectory() as root_text:
                with tempfile.TemporaryDirectory() as outside_text:
                    root = Path(root_text)
                    (root / "linked").symlink_to(
                        Path(outside_text),
                        target_is_directory=True,
                    )
                    return {
                        "kind": "return",
                        "value": os.fspath(target(root, "linked/output.txt")),
                    }
        with tempfile.TemporaryDirectory() as root_text:
            root = Path(root_text)
            return {
                "kind": "return",
                "matches": target(root, "nested/output.txt")
                == root / "nested" / "output.txt",
            }
    if task_id == "T06":
        return {
            "kind": "return",
            "value": [
                target("/private/var/folders/example"),
                target("/private/various/example"),
                target("/home/example/output"),
            ],
        }
    if task_id == "T07":
        requested = (
            ["privacy", "package", "tests", "tests"]
            if case_id == "verified-order"
            else ["tests", "upload"]
        )
        return {"kind": "return", "value": list(target(requested))}
    if task_id == "T08":
        issues = (
            [
                {"name": "old failure", "priority": 1, "state": "resolved"},
                {"name": "documentation", "priority": 3, "state": "open"},
                {"name": "privacy audit", "priority": 2, "state": "open"},
            ]
            if case_id == "highest-priority-open"
            else [{"name": "done", "priority": 1, "state": "resolved"}]
        )
        return {"kind": "return", "value": target(issues)}
    raise RuntimeError("unknown validator case")


try:
    observation = observe()
except BaseException as error:
    observation = {"kind": "exception", "type": type(error).__name__}
payload = _dumps(
    {"nonce": nonce, "observation": observation},
    ensure_ascii=False,
    sort_keys=True,
    separators=(",", ":"),
    allow_nan=False,
).encode("utf-8") + b"\\n"
if len(payload) > 65536:
    raise SystemExit(96)
offset = 0
while offset < len(payload):
    written = _write(1, payload[offset:])
    if written <= 0:
        raise SystemExit(97)
    offset += written
"""
_PUBLIC_TOP_LEVEL_FIELDS = frozenset(
    {
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
    }
)
_PUBLIC_NESTED_FIELDS = {
    "recall": frozenset(
        {
            "invoked",
            "status",
            "estimated_tokens",
            "selected_count",
            "omitted_count",
            "brief_sha256",
        }
    ),
    "tokens": frozenset(
        {
            "mode",
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "reasoning_output_tokens",
            "total_tokens",
        }
    ),
    "contamination": frozenset(
        {
            "project_canary_hits",
            "source_canary_hits",
            "model_canary_hits",
        }
    ),
    "write_guard": frozenset(
        {
            "memory_snapshot_unchanged",
            "pending_count_before",
            "pending_count_after",
            "trusted_count_before",
            "trusted_count_after",
        }
    ),
}
_PRIVATE_FIELD_NAMES = frozenset(
    {
        "absolute_path",
        "cwd",
        "prompt_text",
        "recall_text",
        "response_text",
        "session_id",
        "source_record_id",
        "stderr",
        "stdout",
        "thread_id",
        "tool_output",
    }
)
_ALLOWED_INVALID_REASONS = frozenset(
    {
        "changed_path_violation",
        "codex_exit",
        "event_stream_invalid",
        "timeout",
        "token_evidence_invalid",
        "model_mismatch",
        "validator_failed",
        "validator_test_count_invalid",
    }
)


class BenchmarkRunnerError(ValueError):
    """A benchmark run or public result failed a non-disclosing safety rule."""


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    codex_executable: Path
    codex_cli_version: str
    requested_model_id: str
    reasoning_effort: str
    timeout_seconds: int
    validator_sandbox_executable: Path
    validator_python_executable: Path
    codex_auth_file: Path
    manifest_sha256: str
    harness_commit: str
    harness_sha256: str
    artifact_scope: str = "single-pair-pilot"


@dataclass(frozen=True, slots=True)
class RunSpec:
    task: TaskSpec
    condition: Condition
    replicate: int
    order: str
    run_id: str


@dataclass(slots=True)
class _EventCaptureState:
    failed: bool = False
    auth_removed: bool = False
    launcher_removed: bool = False


@dataclass(frozen=True, slots=True)
class _WorkspaceEntry:
    kind: Literal["directory", "file", "unsafe"]
    mode: int
    owner: int
    links: int
    size: int
    sha256: str | None


@dataclass(frozen=True, slots=True)
class _ValidatorOutcome:
    return_code: int
    output: bytes
    protocol_valid: bool
    tests_run: int
    failures: int
    errors: int
    skipped: int
    expected_failures: int
    unexpected_successes: int


def build_run_schedule(
    tasks: Sequence[TaskSpec],
    *,
    replicates: int,
) -> tuple[RunSpec, ...]:
    """Build a deterministic, counterbalanced A/B schedule."""
    if (
        not tasks
        or type(replicates) is not int
        or replicates <= 0
        or len({task.task_id for task in tasks}) != len(tasks)
    ):
        _reject()
    schedule: list[RunSpec] = []
    for replicate in range(1, replicates + 1):
        for task in tasks:
            if task.order not in {"AB", "BA"}:
                _reject()
            initial: tuple[Condition, Condition] = (
                ("baseline", "pmh") if task.order == "AB" else ("pmh", "baseline")
            )
            conditions = initial if replicate % 2 else tuple(reversed(initial))
            actual_order = "AB" if conditions[0] == "baseline" else "BA"
            for condition in conditions:
                run_id = f"run-{task.task_id.casefold()}-r{replicate}-{condition}"
                schedule.append(
                    RunSpec(
                        task=task,
                        condition=condition,
                        replicate=replicate,
                        order=actual_order,
                        run_id=run_id,
                    )
                )
    if len({spec.run_id for spec in schedule}) != len(schedule):
        _reject()
    return tuple(schedule)


def compose_task_prompt(task: TaskSpec, *, recall_text: str | None) -> str:
    """Return a common task prompt with an optional bounded recall appendix."""
    if (
        not task.prompt
        or task.prompt != task.prompt.strip()
        or (
            recall_text is not None
            and (not recall_text or recall_text != recall_text.strip() or "\x00" in recall_text)
        )
    ):
        _reject()
    prompt = (
        f"{task.prompt}\n\n"
        "Experiment rules:\n"
        "- Work only inside the synthetic workspace exported from the preregistered Git commit.\n"
        "- The workspace contains no .git metadata; inspect its exported repository evidence "
        "before editing.\n"
        "- Change only the allowed implementation file named in the task.\n"
        "- Do not edit tests, create commits, or use network services.\n"
        "- Do not run interpreters, compilers, background jobs, or test commands; "
        "the trusted parent runner validates after you exit.\n"
        "- Stop after completing the bounded implementation change."
    )
    if recall_text is None:
        return prompt
    return f"{prompt}\n\n<project-memory-hub-recall>\n{recall_text}\n</project-memory-hub-recall>"


def execute_benchmark_run(
    spec: RunSpec,
    repository: FixtureRepository,
    recall: RecallEvidence,
    bundle: RecallBundle,
    *,
    private_root: Path | str,
    config: ExecutionConfig,
) -> dict[str, object]:
    """Run one fresh Codex task and return a public, allowlisted result."""
    _validate_inputs(spec, repository, recall, bundle, config)
    root = Path(private_root)
    _prepare_private_root(root)
    run_root = root / spec.run_id
    _create_private_directory(run_root)
    workspace = run_root / "workspace"
    try:
        materialize_fixture_repository(repository, workspace)
    except BenchmarkFixtureError:
        _reject()
    initial_workspace_snapshot = _workspace_snapshot(workspace)
    if any(entry.kind == "unsafe" for entry in initial_workspace_snapshot.values()):
        _reject()
    workspace_sha256 = _workspace_sha256(workspace)
    validator_sha256 = _validator_sha256(spec.task, workspace)

    recall_text = recall.text if spec.condition == "pmh" else None
    prompt = compose_task_prompt(spec.task, recall_text=recall_text)
    prompt_bytes = prompt.encode("utf-8")
    prompt_path = run_root / "prompt.txt"
    events_path = run_root / "events.jsonl"
    stderr_path = run_root / "stderr.txt"
    validator_path = run_root / "validator.txt"
    _write_private_bytes(prompt_path, prompt_bytes)

    started = 0
    finished = 0
    timed_out = False
    codex_exit = -1
    model_sandbox_profile_sha256 = ""
    with tempfile.TemporaryDirectory(
        prefix=".pmh-benchmark-codex-",
        dir=run_root,
    ) as isolated_root_value:
        isolated_root = Path(isolated_root_value).resolve(strict=True)
        isolated_root.chmod(_PRIVATE_DIRECTORY_MODE)
        clean_home = isolated_root / "home"
        process_tmp = isolated_root / "tmp"
        private_codex = isolated_root / "codex"
        clean_home.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        process_tmp.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
        _copy_auth(config.codex_auth_file, clean_home / "auth.json")
        private_codex_identity = _copy_private_executable(
            config.codex_executable,
            private_codex,
        )
        with (
            _exclusive_binary_writer(events_path) as events,
            _exclusive_binary_writer(stderr_path) as errors,
        ):
            command = _sandboxed_codex_command(
                config,
                workspace,
                clean_home,
                process_tmp,
                private_codex,
                spec.task.allowed_paths,
            )
            model_sandbox_profile_sha256 = _sandbox_profile_identity_sha256(
                command[2],
                workspace=workspace,
                clean_home=clean_home,
                process_tmp=process_tmp,
                private_codex=private_codex,
                sandbox_executable=config.validator_sandbox_executable,
                protected_home=_protected_home(config.codex_auth_file),
            )
            environment = _codex_environment(clean_home, process_tmp)
            process_limit = _model_process_soft_limit()
            process: subprocess.Popen[bytes] | None = None
            started = time.monotonic_ns()
            try:
                process = subprocess.Popen(
                    command,
                    cwd=workspace,
                    env=environment,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    preexec_fn=partial(
                        _limit_model_resources,
                        config.timeout_seconds,
                        process_limit,
                    ),
                )
                if process.stdin is None or process.stdout is None or process.stderr is None:
                    _quiesce_process_group(process)
                    _reject()
                capture_state = _EventCaptureState()
                capture = threading.Thread(
                    target=_capture_codex_events,
                    args=(
                        process.stdout,
                        events,
                        clean_home / "auth.json",
                        private_codex,
                        private_codex_identity,
                        process.pid,
                        capture_state,
                    ),
                    daemon=True,
                )
                error_capture = threading.Thread(
                    target=_capture_bounded_stream,
                    args=(
                        process.stderr,
                        errors,
                        process.pid,
                        capture_state,
                        1024 * 1024,
                    ),
                    daemon=True,
                )
                capture.start()
                error_capture.start()
                try:
                    process.stdin.write(prompt_bytes)
                    process.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
                if not _wait_for_process_exit_without_reaping(
                    process,
                    config.timeout_seconds,
                ):
                    timed_out = True
                contained = _quiesce_process_group(process)
                capture.join(timeout=2)
                error_capture.join(timeout=2)
                if capture.is_alive() or error_capture.is_alive():
                    capture_state.failed = True
                    capture.join(timeout=1)
                    error_capture.join(timeout=1)
                if (
                    not contained
                    or capture_state.failed
                    or not capture_state.auth_removed
                    or not capture_state.launcher_removed
                ):
                    codex_exit = -1
                else:
                    codex_exit = process.returncode
            except (OSError, subprocess.SubprocessError):
                if process is not None:
                    _quiesce_process_group(process)
                codex_exit = -1
            finally:
                finished = time.monotonic_ns()

    model_output = run_root / "model-output"
    validation_workspace = run_root / "validation-workspace"
    try:
        workspace.rename(model_output)
    except OSError:
        _reject()
    model_workspace_snapshot = _workspace_snapshot(model_output)
    validation_workspace_ready = not any(
        entry.kind == "unsafe" for entry in model_workspace_snapshot.values()
    )
    copied_workspace_snapshot = model_workspace_snapshot
    copy_containment_violation = not validation_workspace_ready
    if validation_workspace_ready:
        _copy_fixture(model_output, validation_workspace)
        copied_workspace_snapshot = _workspace_snapshot(validation_workspace)
        source_after_copy_snapshot = _workspace_snapshot(model_output)
        copy_containment_violation = (
            source_after_copy_snapshot != model_workspace_snapshot
            or copied_workspace_snapshot != model_workspace_snapshot
        )
    changed_paths = _snapshot_changed_paths(
        initial_workspace_snapshot,
        model_workspace_snapshot,
    )
    allowed_paths = set(spec.task.allowed_paths)
    protected_metadata_changed = any(
        path.parts and path.parts[0] in {".codex", ".git"} for path in changed_paths
    )
    changed_path_violation = (
        copy_containment_violation
        or protected_metadata_changed
        or bool(changed_paths - allowed_paths)
        or not _changed_files_safe(
            initial_workspace_snapshot,
            model_workspace_snapshot,
            changed_paths,
        )
    )
    missing_required_change = not changed_paths

    if started <= 0 or finished < started:
        _reject()
    duration_ms = max(0, (finished - started) // 1_000_000)
    if timed_out or codex_exit != 0 or changed_path_violation or missing_required_change:
        validator_outcome = _empty_validator_outcome()
    else:
        validator_outcome = _run_validator(
            spec.task,
            validation_workspace,
            run_root,
            config,
        )
    _write_private_bytes(validator_path, validator_outcome.output)
    validated_workspace_snapshot = (
        _workspace_snapshot(validation_workspace)
        if validation_workspace_ready
        else model_workspace_snapshot
    )
    source_after_validation_snapshot = _workspace_snapshot(model_output)
    changed_paths_after_validation = _snapshot_changed_paths(
        initial_workspace_snapshot,
        validated_workspace_snapshot,
    )
    if (
        not validation_workspace_ready
        or validated_workspace_snapshot != copied_workspace_snapshot
        or source_after_validation_snapshot != model_workspace_snapshot
        or changed_paths_after_validation != changed_paths
        or not _changed_files_safe(
            initial_workspace_snapshot,
            validated_workspace_snapshot,
            changed_paths_after_validation,
        )
    ):
        changed_path_violation = True
    try:
        shutil.rmtree(model_output)
    except OSError:
        _reject()
    if workspace.exists() or workspace.is_symlink():
        changed_path_violation = True
    if codex_exit == 0 and not timed_out and missing_required_change:
        changed_path_violation = True

    event_summary: CodexEventSummary | None = None
    token_invalid = False
    try:
        event_summary = read_codex_event_summary(events_path)
    except BenchmarkEvidenceError:
        token_invalid = True

    tests_total = validator_outcome.tests_run if validator_outcome.protocol_valid else 0
    invalid_reason: str | None
    if timed_out:
        invalid_reason = "timeout"
    elif changed_path_violation:
        invalid_reason = "changed_path_violation"
    elif codex_exit != 0:
        invalid_reason = "codex_exit"
    elif token_invalid or event_summary is None:
        invalid_reason = "token_evidence_invalid"
    elif event_summary.completed_turn_count != 1 or event_summary.terminal_error_count != 0:
        invalid_reason = "event_stream_invalid"
    elif (
        event_summary.observed_model_id is not None
        and event_summary.observed_model_id != config.requested_model_id
    ):
        invalid_reason = "model_mismatch"
    elif (
        validator_outcome.return_code != 0
        or not validator_outcome.protocol_valid
        or validator_outcome.failures != 0
        or validator_outcome.errors != 0
        or validator_outcome.skipped != 0
        or validator_outcome.expected_failures != 0
        or validator_outcome.unexpected_successes != 0
    ):
        invalid_reason = "validator_failed"
    elif tests_total != spec.task.expected_tests:
        invalid_reason = "validator_test_count_invalid"
    else:
        invalid_reason = None

    tests_passed = tests_total if invalid_reason is None else 0
    usage = event_summary.usage if event_summary is not None else _empty_usage()
    validation_workspace_sha256 = _workspace_snapshot_sha256(validated_workspace_snapshot)
    validator_evidence_sha256 = hashlib.sha256(validator_outcome.output).hexdigest()
    result: dict[str, object] = {
        "schema_version": 3,
        "benchmark_id": _BENCHMARK_ID,
        "artifact_scope": config.artifact_scope,
        "runtime_evidence_class": "self-reported",
        "manifest_sha256": config.manifest_sha256,
        "harness_commit": config.harness_commit,
        "harness_sha256": config.harness_sha256,
        "model_sandbox_profile_sha256": model_sandbox_profile_sha256,
        "run_id": spec.run_id,
        "task_id": spec.task.task_id,
        "task_version": spec.task.version,
        "condition": spec.condition,
        "replicate": spec.replicate,
        "order": spec.order,
        "fixture_commit": repository.commit,
        "workspace_sha256": workspace_sha256,
        "memory_snapshot_sha256": bundle.memory_snapshot_sha256,
        "prompt_sha256": hashlib.sha256(prompt_bytes).hexdigest(),
        "validator_sha256": validator_sha256,
        "validation_workspace_sha256": validation_workspace_sha256,
        "validator_evidence_sha256": validator_evidence_sha256,
        "codex_cli_version": config.codex_cli_version,
        "requested_model_id": config.requested_model_id,
        "observed_model_id": (None if event_summary is None else event_summary.observed_model_id),
        "reasoning_effort": config.reasoning_effort,
        "success": invalid_reason is None,
        "timed_out": timed_out,
        "timeout_seconds": config.timeout_seconds,
        "invalid_reason": invalid_reason,
        "tests_passed": tests_passed,
        "tests_total": tests_total,
        "duration_ms": duration_ms,
        "tool_call_count": (0 if event_summary is None else event_summary.tool_call_count),
        "wrong_route_count": (0 if event_summary is None else event_summary.failed_tool_call_count),
        "recall": _recall_result(spec.condition, recall),
        "tokens": {"mode": "fresh_session_final_total", **asdict(usage)},
        "contamination": {
            "project_canary_hits": recall.project_canary_hits,
            "source_canary_hits": recall.source_canary_hits,
            "model_canary_hits": recall.model_canary_hits,
        },
        "write_guard": {
            "memory_snapshot_unchanged": bundle.memory_snapshot_unchanged,
            "pending_count_before": bundle.pending_count_before,
            "pending_count_after": bundle.pending_count_after,
            "trusted_count_before": bundle.trusted_count_before,
            "trusted_count_after": bundle.trusted_count_after,
        },
        "raw_evidence_sha256": (
            event_summary.evidence_sha256
            if event_summary is not None
            else _file_sha256(events_path)
        ),
    }
    _validate_public_result(result)
    public_payload = canonical_json_bytes(result)
    _write_private_bytes(run_root / "public-result.json", public_payload)
    _write_private_bytes(
        run_root / "public-result.sha256",
        (hashlib.sha256(public_payload).hexdigest() + "\n").encode("ascii"),
    )
    _fsync_directory(run_root)
    return result


def write_public_jsonl(
    path: Path | str,
    results: Sequence[Mapping[str, object]],
) -> None:
    """Atomically write only the documented public benchmark fields."""
    selected = Path(path)
    temporary: Path | None = None
    if selected.exists() or selected.is_symlink():
        _reject()
    if not results:
        _reject()
    try:
        parent = selected.parent
        parent.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
        parent_metadata = parent.lstat()
        if (
            stat.S_ISLNK(parent_metadata.st_mode)
            or not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != os.getuid()
        ):
            _reject()
        payload = b""
        for result in results:
            _validate_public_result(result)
            payload += canonical_json_bytes(dict(result))
        temporary = parent / f".{selected.name}.{os.getpid()}.tmp"
        _write_private_bytes(temporary, payload)
        os.link(temporary, selected)
        os.unlink(temporary)
        temporary = None
    except BenchmarkRunnerError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError):
        _reject()
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def read_public_jsonl(path: Path | str) -> tuple[dict[str, object], ...]:
    """Read and validate one bounded public benchmark JSONL artifact."""
    selected = Path(path)
    descriptor = -1
    try:
        before = selected.lstat()
        _validate_public_file_metadata(before)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(selected, flags)
        opened = os.fstat(descriptor)
        _validate_public_file_metadata(opened)
        if _file_identity(before) != _file_identity(opened):
            _reject()
        document = _read_bounded(descriptor, _MAX_PUBLIC_RESULT_BYTES)
        after = selected.lstat()
        if _file_identity(opened) != _file_identity(after):
            _reject()
        text = document.decode("utf-8", errors="strict")
        results: list[dict[str, object]] = []
        for line in text.splitlines():
            if not line or len(line.encode("utf-8")) > _MAX_PUBLIC_RESULT_LINE_BYTES:
                _reject()
            parsed = json.loads(line, object_pairs_hook=_unique_object)
            if not isinstance(parsed, dict):
                _reject()
            result = dict(parsed)
            _validate_public_result(result)
            results.append(result)
        if not results:
            _reject()
        return tuple(results)
    except BenchmarkRunnerError:
        raise
    except (OSError, UnicodeError, TypeError, ValueError):
        _reject()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise AssertionError("unreachable")


def _validate_inputs(
    spec: RunSpec,
    repository: FixtureRepository,
    recall: RecallEvidence,
    bundle: RecallBundle,
    config: ExecutionConfig,
) -> None:
    executable = config.codex_executable
    if (
        spec.task.fixture_id != repository.fixture_id
        or spec.task.task_id != recall.task_id
        or spec.condition not in {"baseline", "pmh"}
        or type(spec.replicate) is not int
        or spec.replicate <= 0
        or spec.order not in {"AB", "BA"}
        or not re.fullmatch(r"run-[a-z0-9-]+", spec.run_id)
        or bundle.model_id != config.requested_model_id
        or not config.codex_cli_version.strip()
        or not config.requested_model_id.strip()
        or not config.reasoning_effort.strip()
        or not _sha256_value(config.manifest_sha256)
        or not re.fullmatch(r"[0-9a-f]{40}", config.harness_commit)
        or not _sha256_value(config.harness_sha256)
        or config.artifact_scope not in {"single-pair-pilot", "full-pilot", "public-v1"}
        or type(config.timeout_seconds) is not int
        or config.timeout_seconds <= 0
        or not _safe_executable(executable)
        or not _safe_executable(config.validator_sandbox_executable)
        or not _safe_executable(config.validator_python_executable)
        or not _safe_secret_file(config.codex_auth_file)
    ):
        _reject()


def _codex_command(
    config: ExecutionConfig,
    workspace: Path,
    codex_executable: Path,
) -> list[str]:
    shell_values = {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": os.fspath(workspace),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/Library/Developer/CommandLineTools/usr/bin:/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "SHELL": "/bin/zsh",
        "TMPDIR": os.fspath(workspace),
        "TZ": "UTC",
    }
    shell_environment = ",".join(
        f"{key}={json.dumps(value)}" for key, value in sorted(shell_values.items())
    )
    disabled_features = [
        item
        for feature in (
            "apps",
            "browser_use",
            "browser_use_external",
            "browser_use_full_cdp_access",
            "chronicle",
            "computer_use",
            "image_generation",
            "in_app_browser",
            "memories",
            "multi_agent",
            "standalone_web_search",
        )
        for item in ("--disable", feature)
    ]
    return [
        os.fspath(codex_executable),
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--strict-config",
        *disabled_features,
        "--json",
        "--color",
        "never",
        "--dangerously-bypass-approvals-and-sandbox",
        "--model",
        config.requested_model_id,
        "--config",
        f'model_reasoning_effort="{config.reasoning_effort}"',
        "--config",
        "project_doc_max_bytes=0",
        "--config",
        'shell_environment_policy.inherit="none"',
        "--config",
        f"shell_environment_policy.set={{{shell_environment}}}",
        "--cd",
        ".",
        "-",
    ]


def _sandboxed_codex_command(
    config: ExecutionConfig,
    workspace: Path,
    clean_home: Path,
    process_tmp: Path,
    codex_executable: Path,
    allowed_paths: Sequence[PurePosixPath],
) -> list[str]:
    profile = _codex_sandbox_profile(
        workspace,
        clean_home,
        process_tmp,
        codex_executable,
        config.validator_sandbox_executable,
        _protected_home(config.codex_auth_file),
        allowed_paths,
    )
    return [
        os.fspath(config.validator_sandbox_executable),
        "-p",
        profile,
        *_codex_command(config, workspace, codex_executable),
    ]


def _codex_environment(
    clean_home: Path,
    process_tmp: Path,
) -> dict[str, str]:
    return {
        "CODEX_HOME": os.fspath(clean_home),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": os.fspath(clean_home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PATH": "/Library/Developer/CommandLineTools/usr/bin:/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "SHELL": "/bin/zsh",
        "TMPDIR": os.fspath(process_tmp),
        "TZ": "UTC",
    }


def _codex_sandbox_profile(
    workspace: Path,
    clean_home: Path,
    process_tmp: Path,
    codex_executable: Path,
    sandbox_executable: Path,
    protected_home: Path,
    allowed_paths: Sequence[PurePosixPath],
) -> str:
    allowed_write_paths = _validated_allowed_write_paths(workspace, allowed_paths)
    values = {
        "workspace": _sandbox_string(os.fspath(workspace)),
        "workspace_codex": _sandbox_string(os.fspath(workspace / ".codex")),
        "workspace_git": _sandbox_string(os.fspath(workspace / ".git")),
        "clean_home": _sandbox_string(os.fspath(clean_home)),
        "process_tmp": _sandbox_string(os.fspath(process_tmp)),
        "codex_executable": _sandbox_string(os.fspath(codex_executable)),
        "sandbox_executable": _sandbox_string(os.fspath(sandbox_executable)),
        "protected_home": _sandbox_string(os.fspath(protected_home)),
    }
    allowed_writes = "".join(
        f'\n  (literal "{_sandbox_string(os.fspath(path))}")' for path in allowed_write_paths
    )
    allowed_executables = "".join(
        f'\n  (literal "{_sandbox_string(path)}")' for path in _MODEL_EXECUTABLE_PATHS
    )
    return (
        "(version 1)\n"
        "(deny default)\n"
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow process-fork))\n"
        "(allow process-info* (target same-sandbox))\n"
        "(allow signal (target same-sandbox))\n"
        f"(allow process-exec{allowed_executables})\n"
        "(allow syscall-unix (syscall-number SYS_flock))\n"
        "(allow sysctl-read)\n"
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow mach-bootstrap))\n"
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        '  (allow mach-register (local-name-prefix "")))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow mach-lookup\n"
        '    (global-name "com.apple.SecurityServer")\n'
        '    (global-name "com.apple.SystemConfiguration.configd")\n'
        '    (global-name "com.apple.analyticsd")\n'
        '    (global-name "com.apple.analyticsd.messagetracer")\n'
        '    (global-name "com.apple.logd")\n'
        '    (global-name "com.apple.system.logger")\n'
        '    (global-name "com.apple.system.notification_center")\n'
        '    (global-name "com.apple.system.opendirectoryd.libinfo")\n'
        '    (global-name "com.apple.trustd")\n'
        '    (global-name "com.apple.trustd.agent")))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow ipc-posix*))\n"
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow network*))\n"
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow file-read-metadata file-test-existence\n"
        '    (path-ancestors "/var/run/mDNSResponder")))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow file-read-metadata file-test-existence\n"
        f'    (path-ancestors "{_MANAGED_REQUIREMENTS_LOGICAL_PATH}")))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow file-read* file-read-metadata file-test-existence\n"
        f'    (literal "{_MANAGED_REQUIREMENTS_LOGICAL_PATH}")))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow file-read-metadata file-test-existence\n"
        f'    (path-ancestors "{_MANAGED_REQUIREMENTS_CANONICAL_PATH}")))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow file-read* file-read-metadata file-test-existence\n"
        f'    (literal "{_MANAGED_REQUIREMENTS_CANONICAL_PATH}")))\n'
        "(allow system-mac-syscall\n"
        "  (require-all\n"
        '    (mac-policy-name "Sandbox")\n'
        "    (mac-syscall-number 2 67)))\n"
        "(allow file-read-metadata file-test-existence\n"
        '  (path-ancestors "/Library/Apple")\n'
        '  (path-ancestors "/Library/Developer/CommandLineTools")\n'
        '  (path-ancestors "/System")\n'
        '  (path-ancestors "/bin")\n'
        '  (path-ancestors "/dev/null")\n'
        '  (path-ancestors "/dev/random")\n'
        '  (path-ancestors "/dev/urandom")\n'
        '  (path-ancestors "/dev/zero")\n'
        '  (path-ancestors "/private/etc/hosts")\n'
        '  (path-ancestors "/private/etc/localtime")\n'
        '  (path-ancestors "/private/etc/resolv.conf")\n'
        '  (path-ancestors "/private/etc/ssl")\n'
        '  (path-ancestors "/sbin")\n'
        '  (path-ancestors "/usr/bin")\n'
        '  (path-ancestors "/usr/lib")\n'
        '  (path-ancestors "/usr/libexec")\n'
        '  (path-ancestors "/usr/sbin")\n'
        '  (path-ancestors "/usr/share")\n'
        f'  (path-ancestors "{values["workspace"]}"))\n'
        "(allow file-read* file-read-metadata file-test-existence\n"
        '  (literal "/")\n'
        '  (subpath "/Library/Apple")\n'
        '  (literal "/Library/Developer/CommandLineTools")\n'
        '  (subpath "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework")\n'
        '  (subpath "/Library/Developer/CommandLineTools/usr/bin")\n'
        '  (subpath "/Library/Developer/CommandLineTools/usr/lib")\n'
        '  (subpath "/Library/Developer/CommandLineTools/usr/libexec")\n'
        '  (subpath "/Library/Developer/CommandLineTools/usr/share")\n'
        '  (subpath "/System")\n'
        '  (subpath "/bin")\n'
        '  (literal "/dev/null")\n'
        '  (literal "/dev/random")\n'
        '  (literal "/dev/urandom")\n'
        '  (literal "/dev/zero")\n'
        '  (subpath "/dev/fd")\n'
        '  (literal "/private/etc/hosts")\n'
        '  (literal "/private/etc/localtime")\n'
        '  (literal "/private/etc/resolv.conf")\n'
        '  (subpath "/private/etc/ssl")\n'
        '  (subpath "/sbin")\n'
        '  (subpath "/usr/bin")\n'
        '  (subpath "/usr/lib")\n'
        '  (subpath "/usr/libexec")\n'
        '  (subpath "/usr/sbin")\n'
        '  (subpath "/usr/share")\n'
        f'  (subpath "{values["workspace"]}"))\n'
        f'(with-filter (process-path "{values["sandbox_executable"]}")\n'
        "  (allow file-read-metadata file-test-existence\n"
        f'    (path-ancestors "{values["codex_executable"]}")))\n'
        f'(with-filter (process-path "{values["sandbox_executable"]}")\n'
        "  (allow process-exec\n"
        f'    (literal "{values["codex_executable"]}")))\n'
        f'(with-filter (process-path "{values["sandbox_executable"]}")\n'
        "  (allow file-read* file-read-metadata file-test-existence\n"
        f'    (literal "{values["codex_executable"]}")))\n'
        f'(deny process-exec (literal "{values["sandbox_executable"]}"))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow file-read-metadata file-test-existence\n"
        f'    (path-ancestors "{values["clean_home"]}")\n'
        f'    (path-ancestors "{values["process_tmp"]}")))\n'
        f'(with-filter (process-path "{values["codex_executable"]}")\n'
        "  (allow file-read* file-read-metadata file-write* file-test-existence\n"
        f'    (subpath "{values["clean_home"]}")\n'
        f'    (subpath "{values["process_tmp"]}")))\n'
        "(deny file-read*\n"
        f'  (literal "{values["protected_home"]}")\n'
        f'  (subpath "{values["protected_home"]}"))\n'
        "(allow file-write-data"
        '\n  (literal "/dev/null")'
        f"{allowed_writes})\n"
        "(deny file-write*\n"
        f'  (literal "{values["workspace_codex"]}")\n'
        f'  (subpath "{values["workspace_codex"]}"))\n'
        "(deny file-write*\n"
        f'  (literal "{values["workspace_git"]}")\n'
        f'  (subpath "{values["workspace_git"]}"))'
    )


def _copy_auth(source: Path, target: Path) -> None:
    descriptor = -1
    try:
        before = source.lstat()
        if not _safe_secret_metadata(before):
            _reject()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(source, flags)
        opened = os.fstat(descriptor)
        if not _safe_secret_metadata(opened) or _file_identity(before) != _file_identity(opened):
            _reject()
        payload = _read_bounded(descriptor, 1024 * 1024)
        after = source.lstat()
        if _file_identity(opened) != _file_identity(after):
            _reject()
        _write_private_bytes(target, payload)
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _copy_private_executable(
    source: Path,
    target: Path,
) -> tuple[int, int, int]:
    source_descriptor = -1
    target_descriptor = -1
    try:
        before = source.lstat()
        if (
            not source.is_absolute()
            or not _safe_executable_metadata(before)
            or not os.access(source, os.X_OK)
        ):
            _reject()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_descriptor = os.open(source, flags)
        opened = os.fstat(source_descriptor)
        if (
            _file_identity(before) != _file_identity(opened)
            or not _safe_executable_metadata(opened)
            or opened.st_size <= 0
            or opened.st_size > _MAX_PRIVATE_EXECUTABLE_BYTES
        ):
            _reject()
        target_flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        target_descriptor = os.open(
            target,
            target_flags,
            _PRIVATE_EXECUTABLE_MODE,
        )
        os.fchmod(target_descriptor, _PRIVATE_EXECUTABLE_MODE)
        total = 0
        while chunk := os.read(source_descriptor, 1024 * 1024):
            total += len(chunk)
            if total > _MAX_PRIVATE_EXECUTABLE_BYTES:
                _reject()
            offset = 0
            while offset < len(chunk):
                written = os.write(target_descriptor, chunk[offset:])
                if written <= 0:
                    _reject()
                offset += written
        os.fsync(target_descriptor)
        source_after = os.fstat(source_descriptor)
        path_after = source.lstat()
        target_opened = os.fstat(target_descriptor)
        target_after = target.lstat()
        if (
            total != opened.st_size
            or _stable_file_metadata(opened) != _stable_file_metadata(source_after)
            or _stable_file_metadata(opened) != _stable_file_metadata(path_after)
            or not _safe_executable_metadata(path_after)
            or _file_identity(target_opened) != _file_identity(target_after)
            or not _safe_private_executable_metadata(target_after)
            or target_after.st_size != total
        ):
            _reject()
        return _file_identity(target_after)
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()
    finally:
        for descriptor in (target_descriptor, source_descriptor):
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
    raise AssertionError("unreachable")


def _unlink_private_executable(
    path: Path,
    expected_identity: tuple[int, int, int],
) -> None:
    try:
        metadata = path.lstat()
        if _file_identity(metadata) != expected_identity or not _safe_private_executable_metadata(
            metadata
        ):
            _reject()
        path.unlink()
        try:
            path.lstat()
        except FileNotFoundError:
            return
        _reject()
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()


def _safe_private_executable_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and stat.S_IMODE(metadata.st_mode) == _PRIVATE_EXECUTABLE_MODE
        and 0 < metadata.st_size <= _MAX_PRIVATE_EXECUTABLE_BYTES
    )


def _stable_file_metadata(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _capture_codex_events(
    source: BinaryIO,
    destination: BinaryIO,
    auth_copy: Path,
    private_codex: Path,
    private_codex_identity: tuple[int, int, int],
    process_group: int,
    state: _EventCaptureState,
) -> None:
    total = 0
    try:
        while True:
            line = source.readline(1024 * 1024 + 1)
            if not line:
                break
            total += len(line)
            if len(line) > 1024 * 1024 or total > _MAX_PRIVATE_EVENT_BYTES:
                state.failed = True
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except OSError:
                    pass
                break
            destination.write(line)
            destination.flush()
            if (state.auth_removed and state.launcher_removed) or not line.endswith(b"\n"):
                continue
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, UnicodeError, ValueError, TypeError):
                continue
            if isinstance(event, dict) and event.get("type") == "thread.started":
                try:
                    if not state.auth_removed:
                        auth_copy.unlink()
                        state.auth_removed = True
                    if not state.launcher_removed:
                        _unlink_private_executable(
                            private_codex,
                            private_codex_identity,
                        )
                        state.launcher_removed = True
                except (BenchmarkRunnerError, OSError):
                    state.failed = True
                    try:
                        os.killpg(process_group, signal.SIGKILL)
                    except OSError:
                        pass
                    break
    except (OSError, ValueError):
        state.failed = True
        try:
            os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass
    finally:
        try:
            source.close()
        except OSError:
            pass


def _capture_bounded_stream(
    source: BinaryIO,
    destination: BinaryIO,
    process_group: int,
    state: _EventCaptureState,
    maximum_bytes: int,
) -> None:
    total = 0
    try:
        while True:
            chunk = source.read(64 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > maximum_bytes:
                state.failed = True
                try:
                    os.killpg(process_group, signal.SIGKILL)
                except OSError:
                    pass
                break
            destination.write(chunk)
            destination.flush()
    except OSError:
        state.failed = True
        try:
            os.killpg(process_group, signal.SIGKILL)
        except OSError:
            pass
    finally:
        try:
            source.close()
        except OSError:
            pass


def _safe_secret_file(path: Path) -> bool:
    try:
        return _safe_secret_metadata(path.lstat())
    except OSError:
        return False


def _safe_secret_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid == os.getuid()
        and not metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
        and 0 < metadata.st_size <= 1024 * 1024
    )


def _protected_home(auth_file: Path) -> Path:
    parent = auth_file.parent
    return parent.parent if parent.name == ".codex" else parent


def _implementation_ast_is_safe(task: TaskSpec, workspace: Path) -> bool:
    target = _VALIDATOR_TARGETS.get(task.task_id)
    if (
        target is None
        or len(task.allowed_paths) != 1
        or task.allowed_paths[0].as_posix() != target[0]
    ):
        return False
    source_path = workspace / task.allowed_paths[0]
    try:
        workspace_root = workspace.resolve(strict=True)
        parent = source_path.parent.resolve(strict=True)
        metadata = source_path.lstat()
        if (
            parent != workspace_root
            and workspace_root not in parent.parents
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_WORKSPACE_FILE_BYTES
        ):
            return False
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=target[0])
    except (OSError, UnicodeError, SyntaxError, ValueError):
        return False

    target_definitions = 0
    for statement in tree.body:
        if isinstance(statement, (ast.Import, ast.ImportFrom)):
            if not _validator_import_is_safe(statement):
                return False
        elif isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if isinstance(statement, ast.AsyncFunctionDef) or not _validator_function_is_safe(
                statement
            ):
                return False
            if statement.name == target[1]:
                target_definitions += 1
        elif isinstance(statement, ast.ClassDef):
            if not _validator_declarative_class_is_safe(statement):
                return False
        elif isinstance(statement, (ast.Assign, ast.AnnAssign)):
            if not _validator_declaration_is_safe(statement):
                return False
        elif not (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            return False

    if target_definitions != 1:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in _VALIDATOR_FORBIDDEN_NAMES:
            return False
        if isinstance(node, ast.Attribute) and (
            node.attr.startswith("_")
            or node.attr in _VALIDATOR_FORBIDDEN_ATTRIBUTES
            or node.attr in _VALIDATOR_FORBIDDEN_NAMES
        ):
            return False
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _VALIDATOR_FORBIDDEN_NAMES:
                return False
            if isinstance(node.func, ast.Attribute) and (
                node.func.attr.startswith("_")
                or node.func.attr in _VALIDATOR_FORBIDDEN_ATTRIBUTES
                or node.func.attr in _VALIDATOR_FORBIDDEN_NAMES
            ):
                return False
    return True


def _validator_import_is_safe(statement: ast.Import | ast.ImportFrom) -> bool:
    if isinstance(statement, ast.Import):
        selected = tuple(alias.name for alias in statement.names)
    else:
        if statement.level != 0 or statement.module is None:
            return False
        selected = (statement.module,)
        if statement.module == "__future__" and any(
            alias.name != "annotations" for alias in statement.names
        ):
            return False
    return all(
        module in _VALIDATOR_ALLOWED_IMPORTS
        and all(
            alias.name != "*"
            and (alias.asname is None or alias.asname not in _VALIDATOR_FORBIDDEN_NAMES)
            for alias in statement.names
        )
        for module in selected
    )


def _validator_function_is_safe(statement: ast.FunctionDef) -> bool:
    if statement.decorator_list:
        return False
    defaults = (*statement.args.defaults, *statement.args.kw_defaults)
    annotations = [
        argument.annotation
        for argument in (
            *statement.args.posonlyargs,
            *statement.args.args,
            *statement.args.kwonlyargs,
        )
        if argument.annotation is not None
    ]
    for optional in (statement.args.vararg, statement.args.kwarg):
        if optional is not None and optional.annotation is not None:
            annotations.append(optional.annotation)
    if statement.returns is not None:
        annotations.append(statement.returns)
    return all(
        default is None or _validator_static_expression_is_safe(default) for default in defaults
    ) and all(_validator_static_expression_is_safe(annotation) for annotation in annotations)


def _validator_declarative_class_is_safe(statement: ast.ClassDef) -> bool:
    if (
        statement.decorator_list
        or statement.keywords
        or not statement.bases
        or any(not isinstance(base, ast.Name) or base.id != "TypedDict" for base in statement.bases)
    ):
        return False
    return all(
        isinstance(child, ast.Pass)
        or (
            isinstance(child, ast.AnnAssign)
            and _validator_static_expression_is_safe(child.annotation)
            and (child.value is None or _validator_static_expression_is_safe(child.value))
        )
        or (
            isinstance(child, ast.Expr)
            and isinstance(child.value, ast.Constant)
            and isinstance(child.value.value, str)
        )
        for child in statement.body
    )


def _validator_declaration_is_safe(statement: ast.Assign | ast.AnnAssign) -> bool:
    if isinstance(statement, ast.Assign):
        if not all(isinstance(target, ast.Name) for target in statement.targets):
            return False
        value = statement.value
    else:
        if not isinstance(statement.target, ast.Name):
            return False
        if statement.value is None:
            return _validator_static_expression_is_safe(statement.annotation)
        value = statement.value
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "TypeVar"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Constant)
        and isinstance(value.args[0].value, str)
        and not value.keywords
    ):
        return True
    return _validator_static_expression_is_safe(value)


def _validator_static_expression_is_safe(expression: ast.expr) -> bool:
    forbidden = (
        ast.Await,
        ast.Call,
        ast.DictComp,
        ast.GeneratorExp,
        ast.Lambda,
        ast.ListComp,
        ast.NamedExpr,
        ast.SetComp,
        ast.Yield,
        ast.YieldFrom,
    )
    return not any(isinstance(node, forbidden) for node in ast.walk(expression))


def _run_validator(
    task: TaskSpec,
    workspace: Path,
    run_root: Path,
    config: ExecutionConfig,
) -> _ValidatorOutcome:
    raw_path = run_root / "validator-raw.txt"
    cases = _VALIDATOR_EXPECTED_OBSERVATIONS.get(task.task_id)
    if cases is None or len(cases) != task.expected_tests:
        _reject()
    if not _implementation_ast_is_safe(task, workspace):
        return _ValidatorOutcome(
            return_code=1,
            output=canonical_json_bytes(
                {
                    "ast_policy_valid": False,
                    "validator_protocol_version": _VALIDATOR_PROTOCOL_VERSION,
                }
            ),
            protocol_valid=False,
            tests_run=0,
            failures=0,
            errors=1,
            skipped=0,
            expected_failures=0,
            unexpected_successes=0,
        )
    failures = 0
    errors = 0
    raw_chunks: list[bytes] = []
    seen_nonces: set[str] = set()
    with tempfile.TemporaryDirectory(
        prefix=".pmh-benchmark-validator-",
        dir=run_root,
    ) as validator_tmp_value:
        validator_tmp = Path(validator_tmp_value).resolve(strict=True)
        validator_tmp.chmod(_PRIVATE_DIRECTORY_MODE)
        environment = {
            "HOME": os.fspath(validator_tmp),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONHASHSEED": "0",
            "PYTHONNOUSERSITE": "1",
            "TMPDIR": os.fspath(validator_tmp),
            "TZ": "UTC",
        }
        process_limit = _model_process_soft_limit()
        deadline = time.monotonic() + min(config.timeout_seconds, 120)
        for index, (case_id, expected) in enumerate(cases):
            stdout_path = validator_tmp / f"case-{index:02d}.stdout"
            stderr_path = validator_tmp / f"case-{index:02d}.stderr"
            remaining = max(0, int(deadline - time.monotonic()))
            if remaining <= 0:
                errors += 1
                raw_chunks.append(
                    _validator_case_transcript(
                        case_id=case_id,
                        completed=False,
                        contained=False,
                        exited=False,
                        matched=False,
                        process_return_code=None,
                        response=None,
                        runner_error=True,
                        stderr=b"",
                        stdout=b"",
                    )
                )
                continue
            matched, completed, raw = _run_validator_case(
                task,
                case_id,
                expected,
                workspace,
                validator_tmp,
                config,
                environment,
                process_limit,
                remaining,
                stdout_path,
                stderr_path,
                seen_nonces,
            )
            raw_chunks.append(raw)
            if not completed:
                errors += 1
            elif not matched:
                failures += 1
    output = b"".join(raw_chunks)
    if len(output) > 1024 * 1024:
        return _empty_validator_outcome()
    _write_private_bytes(raw_path, output)
    return _ValidatorOutcome(
        return_code=0 if failures == 0 and errors == 0 else 1,
        output=output,
        protocol_valid=errors == 0,
        tests_run=len(cases),
        failures=failures,
        errors=errors,
        skipped=0,
        expected_failures=0,
        unexpected_successes=0,
    )


def _run_validator_case(
    task: TaskSpec,
    case_id: str,
    expected: object,
    workspace: Path,
    validator_tmp: Path,
    config: ExecutionConfig,
    environment: Mapping[str, str],
    process_limit: int,
    timeout_seconds: int,
    stdout_path: Path,
    stderr_path: Path,
    seen_nonces: set[str],
) -> tuple[bool, bool, bytes]:
    process: subprocess.Popen[bytes] | None = None
    try:
        command = _validator_command(
            task,
            case_id,
            workspace,
            validator_tmp,
            config,
        )
        with (
            _exclusive_binary_writer(stdout_path) as stdout,
            _exclusive_binary_writer(stderr_path) as stderr,
        ):
            process = subprocess.Popen(
                command,
                cwd=workspace,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                start_new_session=True,
                preexec_fn=partial(
                    _limit_validator_resources,
                    timeout_seconds,
                    process_limit,
                ),
            )
            exited = _wait_for_process_exit_without_reaping(process, timeout_seconds)
            contained = _quiesce_process_group(process)
        observed = _read_small_private_file(
            stdout_path,
            64 * 1024,
            allow_empty=True,
        )
        stderr_bytes = _read_small_private_file(
            stderr_path,
            256 * 1024,
            allow_empty=True,
        )
        completed = exited and contained and process.returncode == 0
        response = _validator_response_document(
            observed,
            seen_nonces,
        )
        matched = completed and response is not None and response["observation"] == expected
        transcript = _validator_case_transcript(
            case_id=case_id,
            completed=completed,
            contained=contained,
            exited=exited,
            matched=bool(matched),
            process_return_code=process.returncode,
            response=response,
            runner_error=False,
            stderr=stderr_bytes,
            stdout=observed,
        )
        return bool(matched), completed, transcript
    except (BenchmarkRunnerError, OSError, subprocess.SubprocessError):
        if process is not None:
            _quiesce_process_group(process)
        return (
            False,
            False,
            _validator_case_transcript(
                case_id=case_id,
                completed=False,
                contained=False,
                exited=False,
                matched=False,
                process_return_code=None if process is None else process.returncode,
                response=None,
                runner_error=True,
                stderr=b"",
                stdout=b"",
            ),
        )


def _validator_response_matches(
    observed: bytes,
    expected: object,
    seen_nonces: set[str],
) -> bool:
    document = _validator_response_document(observed, seen_nonces)
    return document is not None and bool(document["observation"] == expected)


def _validator_response_document(
    observed: bytes,
    seen_nonces: set[str],
) -> dict[str, object] | None:
    def reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
        document: dict[str, object] = {}
        for key, value in pairs:
            if key in document:
                raise ValueError("duplicate JSON key")
            document[key] = value
        return document

    try:
        document = json.loads(
            observed.decode("utf-8"),
            object_pairs_hook=reject_duplicate_keys,
        )
        if (
            not isinstance(document, dict)
            or set(document) != {"nonce", "observation"}
            or not isinstance(document["nonce"], str)
            or re.fullmatch(r"[0-9a-f]{32}", document["nonce"]) is None
            or canonical_json_bytes(document) != observed
        ):
            return None
        nonce = document["nonce"]
        if nonce in seen_nonces:
            return None
        seen_nonces.add(nonce)
        return {
            "nonce": nonce,
            "observation": document["observation"],
        }
    except (
        BenchmarkEvidenceError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
    ):
        return None


def _validator_case_transcript(
    *,
    case_id: str,
    completed: bool,
    contained: bool,
    exited: bool,
    matched: bool,
    process_return_code: int | None,
    response: dict[str, object] | None,
    runner_error: bool,
    stderr: bytes,
    stdout: bytes,
) -> bytes:
    return canonical_json_bytes(
        {
            "case_id": case_id,
            "completed": completed,
            "contained": contained,
            "exited": exited,
            "matched": matched,
            "process_return_code": process_return_code,
            "protocol_valid": response is not None,
            "response": response,
            "runner_error": runner_error,
            "sandbox_apply_denied": (
                stderr == b"sandbox-exec: sandbox_apply: Operation not permitted\n"
            ),
            "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
        }
    )


def _validator_command(
    task: TaskSpec,
    case_id: str,
    workspace: Path,
    validator_tmp: Path,
    config: ExecutionConfig,
) -> list[str]:
    arguments = task.validation_argv
    known_cases = _VALIDATOR_EXPECTED_OBSERVATIONS.get(task.task_id)
    if (
        len(arguments) != 4
        or arguments[:3] != ("python3", "-m", "unittest")
        or re.fullmatch(r"tests\.test_t[0-9]{2}", arguments[3]) is None
        or known_cases is None
        or case_id not in {selected for selected, _ in known_cases}
    ):
        _reject()
    profile = _validator_sandbox_profile(
        workspace,
        validator_tmp,
        config.validator_python_executable,
        config.validator_sandbox_executable,
        _protected_home(config.codex_auth_file),
    )
    return [
        os.fspath(config.validator_sandbox_executable),
        "-p",
        profile,
        os.fspath(config.validator_python_executable),
        "-B",
        "-I",
        "-S",
        "-c",
        _VALIDATOR_WORKER,
        task.task_id,
        case_id,
    ]


def _validator_sandbox_profile(
    workspace: Path,
    validator_tmp: Path,
    python_executable: Path,
    sandbox_executable: Path,
    protected_home: Path,
) -> str:
    workspace_value = _sandbox_string(os.fspath(workspace))
    validator_tmp_value = _sandbox_string(os.fspath(validator_tmp))
    python_value = _sandbox_string(os.fspath(python_executable))
    python_runtime_value = _sandbox_string(os.fspath(python_executable.parent))
    sandbox_value = _sandbox_string(os.fspath(sandbox_executable))
    protected_value = _sandbox_string(os.fspath(protected_home))
    return (
        "(version 1)\n"
        "(deny default)\n"
        f'(with-filter (process-path "{sandbox_value}")\n'
        "  (allow process-exec\n"
        f'    (literal "{python_value}")))\n'
        f'(with-filter (process-path "{sandbox_value}")\n'
        "  (allow file-read* file-read-metadata file-test-existence\n"
        f'    (literal "{python_value}")))\n'
        "(allow file-read-metadata file-test-existence\n"
        '  (path-ancestors "/System/Library")\n'
        '  (path-ancestors "/Library/Developer/CommandLineTools")\n'
        '  (path-ancestors "/dev/null")\n'
        '  (path-ancestors "/dev/urandom")\n'
        '  (path-ancestors "/usr/lib")\n'
        f'  (path-ancestors "{python_runtime_value}")\n'
        f'  (path-ancestors "{python_value}")\n'
        f'  (path-ancestors "{workspace_value}")\n'
        f'  (path-ancestors "{validator_tmp_value}"))\n'
        "(allow file-read* file-read-metadata file-test-existence\n"
        '  (literal "/")\n'
        '  (subpath "/System/Library")\n'
        '  (subpath "/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework")\n'
        '  (literal "/dev/null")\n'
        '  (literal "/dev/urandom")\n'
        '  (subpath "/usr/lib")\n'
        f'  (subpath "{python_runtime_value}")\n'
        f'  (literal "{python_value}")\n'
        f'  (subpath "{workspace_value}")\n'
        f'  (subpath "{validator_tmp_value}"))\n'
        "(allow file-write*\n"
        '  (literal "/dev/null")\n'
        f'  (subpath "{validator_tmp_value}"))\n'
        "(deny file-read*\n"
        f'  (literal "{protected_value}")\n'
        f'  (subpath "{protected_value}"))\n'
        "(deny network*)"
    )


def _empty_validator_outcome(
    *,
    return_code: int = -1,
    output: bytes = b"",
) -> _ValidatorOutcome:
    return _ValidatorOutcome(
        return_code=return_code,
        output=output,
        protocol_valid=False,
        tests_run=0,
        failures=0,
        errors=0,
        skipped=0,
        expected_failures=0,
        unexpected_successes=0,
    )


def _sandbox_string(value: str) -> str:
    if "\x00" in value or "\n" in value or "\r" in value:
        _reject()
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _validated_allowed_write_paths(
    workspace: Path,
    allowed_paths: Sequence[PurePosixPath],
) -> tuple[Path, ...]:
    try:
        workspace_root = workspace.resolve(strict=True)
        workspace_metadata = workspace_root.lstat()
    except OSError:
        _reject()
    if (
        not allowed_paths
        or not stat.S_ISDIR(workspace_metadata.st_mode)
        or stat.S_ISLNK(workspace_metadata.st_mode)
        or workspace_metadata.st_uid != os.getuid()
    ):
        _reject()
    selected: list[Path] = []
    for relative in allowed_paths:
        if (
            relative.is_absolute()
            or not relative.parts
            or "." in relative.parts
            or ".." in relative.parts
            or relative.parts[0] in {".codex", ".git"}
        ):
            _reject()
        path = workspace_root.joinpath(*relative.parts)
        try:
            resolved = path.resolve(strict=True)
            metadata = path.lstat()
        except OSError:
            _reject()
        if (
            not resolved.is_relative_to(workspace_root)
            or resolved != path
            or not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or metadata.st_size < 0
            or metadata.st_size > _MAX_WORKSPACE_FILE_BYTES
        ):
            _reject()
        selected.append(path)
    if len(set(selected)) != len(selected):
        _reject()
    return tuple(selected)


def _limit_model_resources(
    timeout_seconds: int,
    process_limit: int,
) -> None:
    _set_inherited_limit(resource.RLIMIT_CORE, 0)
    _set_inherited_limit(resource.RLIMIT_FSIZE, _MAX_WORKSPACE_FILE_BYTES)
    _set_inherited_limit(resource.RLIMIT_NOFILE, _MAX_MODEL_OPEN_FILES)
    _set_inherited_limit(resource.RLIMIT_NPROC, process_limit)
    _set_inherited_limit(
        resource.RLIMIT_CPU,
        timeout_seconds + _MODEL_CPU_GRACE_SECONDS,
    )


def _limit_validator_resources(
    timeout_seconds: int,
    process_limit: int,
) -> None:
    _set_inherited_limit(resource.RLIMIT_CORE, 0)
    _set_inherited_limit(resource.RLIMIT_FSIZE, 1024 * 1024)
    _set_inherited_limit(resource.RLIMIT_NOFILE, 128)
    _set_inherited_limit(resource.RLIMIT_NPROC, process_limit)
    _set_inherited_limit(resource.RLIMIT_CPU, timeout_seconds)


def _set_inherited_limit(selected: int, maximum: int) -> None:
    _, hard = resource.getrlimit(selected)
    target = maximum if hard == resource.RLIM_INFINITY else min(maximum, hard)
    resource.setrlimit(selected, (target, target))


def _model_process_soft_limit() -> int:
    _, hard = resource.getrlimit(resource.RLIMIT_NPROC)
    hard_limit = 2**31 - 1 if hard == resource.RLIM_INFINITY else hard
    if sys.platform != "darwin":
        return min(512, hard_limit)
    current = _current_macos_user_process_count()
    target = min(current + _MODEL_PROCESS_HEADROOM, hard_limit)
    if current <= 0 or target <= current:
        _reject()
    return target


def _current_macos_user_process_count() -> int:
    maximum_command_length = 16

    class _ProcBsdInfo(ctypes.Structure):
        _fields_ = [
            ("flags", ctypes.c_uint32),
            ("status", ctypes.c_uint32),
            ("xstatus", ctypes.c_uint32),
            ("pid", ctypes.c_uint32),
            ("ppid", ctypes.c_uint32),
            ("uid", ctypes.c_uint32),
            ("gid", ctypes.c_uint32),
            ("ruid", ctypes.c_uint32),
            ("rgid", ctypes.c_uint32),
            ("svuid", ctypes.c_uint32),
            ("svgid", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32),
            ("command", ctypes.c_char * maximum_command_length),
            ("name", ctypes.c_char * (2 * maximum_command_length)),
            ("files", ctypes.c_uint32),
            ("process_group", ctypes.c_uint32),
            ("job_control", ctypes.c_uint32),
            ("terminal_device", ctypes.c_uint32),
            ("terminal_process_group", ctypes.c_uint32),
            ("nice", ctypes.c_int32),
            ("started_seconds", ctypes.c_uint64),
            ("started_microseconds", ctypes.c_uint64),
        ]

    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        list_all = library.proc_listallpids
        list_all.argtypes = (ctypes.c_void_p, ctypes.c_int)
        list_all.restype = ctypes.c_int
        process_info = library.proc_pidinfo
        process_info.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        process_info.restype = ctypes.c_int
        capacity = list_all(None, 0)
        if capacity <= 0 or capacity > 100_000:
            _reject()
        identifiers = (ctypes.c_int * (capacity + 128))()
        count = list_all(identifiers, ctypes.sizeof(identifiers))
        if count <= 0 or count >= len(identifiers):
            _reject()
        selected_user = os.getuid()
        selected_count = 0
        for process_id in identifiers[:count]:
            information = _ProcBsdInfo()
            copied = process_info(
                process_id,
                3,
                0,
                ctypes.byref(information),
                ctypes.sizeof(information),
            )
            if copied == ctypes.sizeof(information) and information.uid == selected_user:
                selected_count += 1
    except BenchmarkRunnerError:
        raise
    except (AttributeError, OSError, TypeError, ValueError):
        _reject()
    return selected_count


def _safe_executable(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return path.is_absolute() and _safe_executable_metadata(metadata) and os.access(path, os.X_OK)


def _safe_executable_metadata(metadata: os.stat_result) -> bool:
    return (
        stat.S_ISREG(metadata.st_mode)
        and not stat.S_ISLNK(metadata.st_mode)
        and metadata.st_uid in {0, os.getuid()}
        and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        and bool(metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    )


def _prepare_private_root(root: Path) -> None:
    try:
        if root.exists() or root.is_symlink():
            metadata = root.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != os.getuid()
                or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            ):
                _reject()
            return
        root.mkdir(mode=_PRIVATE_DIRECTORY_MODE, parents=True)
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()


def _create_private_directory(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            _reject()
        path.mkdir(mode=_PRIVATE_DIRECTORY_MODE)
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()


def _copy_fixture(source: Path, target: Path) -> None:
    try:
        _validate_tree_has_no_symlinks(source)
        shutil.copytree(source, target, symlinks=False)
        target.chmod(_PRIVATE_DIRECTORY_MODE)
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()


def _validate_tree_has_no_symlinks(root: Path) -> None:
    root_metadata = root.lstat()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
    ):
        _reject()
    for path in root.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            _reject()
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            _reject()


def _workspace_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda value: value.as_posix()):
        relative = path.relative_to(root)
        if relative.parts and relative.parts[0] == ".git":
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            _reject()
        if stat.S_ISDIR(metadata.st_mode):
            continue
        if not stat.S_ISREG(metadata.st_mode):
            _reject()
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _workspace_snapshot(root: Path) -> dict[PurePosixPath, _WorkspaceEntry]:
    try:
        root_metadata = root.lstat()
    except OSError:
        _reject()
    if (
        not stat.S_ISDIR(root_metadata.st_mode)
        or stat.S_ISLNK(root_metadata.st_mode)
        or root_metadata.st_uid != os.getuid()
    ):
        _reject()
    snapshot: dict[PurePosixPath, _WorkspaceEntry] = {
        PurePosixPath("."): _WorkspaceEntry(
            kind="directory",
            mode=stat.S_IMODE(root_metadata.st_mode),
            owner=root_metadata.st_uid,
            links=root_metadata.st_nlink,
            size=0,
            sha256=None,
        )
    }
    total_bytes = 0
    path_count = 1
    directories = [root]
    try:
        while directories:
            directory = directories.pop()
            directory_before = directory.lstat()
            if (
                not stat.S_ISDIR(directory_before.st_mode)
                or stat.S_ISLNK(directory_before.st_mode)
                or directory_before.st_uid != os.getuid()
            ):
                _reject()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path_count += 1
                    if path_count > _MAX_WORKSPACE_PATHS:
                        _reject()
                    path = Path(entry.path)
                    relative = PurePosixPath(path.relative_to(root).as_posix())
                    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
                        _reject()
                    before = entry.stat(follow_symlinks=False)
                    mode = stat.S_IMODE(before.st_mode)
                    if (
                        stat.S_ISDIR(before.st_mode)
                        and not stat.S_ISLNK(before.st_mode)
                        and before.st_uid == os.getuid()
                    ):
                        snapshot[relative] = _WorkspaceEntry(
                            kind="directory",
                            mode=mode,
                            owner=before.st_uid,
                            links=before.st_nlink,
                            size=0,
                            sha256=None,
                        )
                        directories.append(path)
                        continue
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or stat.S_ISLNK(before.st_mode)
                        or before.st_uid != os.getuid()
                        or before.st_nlink != 1
                        or before.st_size < 0
                        or before.st_size > _MAX_WORKSPACE_FILE_BYTES
                    ):
                        snapshot[relative] = _WorkspaceEntry(
                            kind="unsafe",
                            mode=mode,
                            owner=before.st_uid,
                            links=before.st_nlink,
                            size=max(0, before.st_size),
                            sha256=None,
                        )
                        continue
                    total_bytes += before.st_size
                    if total_bytes > _MAX_WORKSPACE_TOTAL_BYTES:
                        _reject()
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(path, flags)
                    try:
                        opened = os.fstat(descriptor)
                        if _stable_file_metadata(before) != _stable_file_metadata(opened):
                            _reject()
                        digest = hashlib.sha256()
                        copied = 0
                        while chunk := os.read(descriptor, 64 * 1024):
                            copied += len(chunk)
                            if copied > _MAX_WORKSPACE_FILE_BYTES:
                                _reject()
                            digest.update(chunk)
                        after = path.lstat()
                        if copied != before.st_size or _stable_file_metadata(
                            opened
                        ) != _stable_file_metadata(after):
                            _reject()
                    finally:
                        os.close(descriptor)
                    snapshot[relative] = _WorkspaceEntry(
                        kind="file",
                        mode=mode,
                        owner=before.st_uid,
                        links=before.st_nlink,
                        size=before.st_size,
                        sha256=digest.hexdigest(),
                    )
            directory_after = directory.lstat()
            if _stable_file_metadata(directory_before) != _stable_file_metadata(directory_after):
                _reject()
    except BenchmarkRunnerError:
        raise
    except (OSError, UnicodeError, ValueError):
        _reject()
    return snapshot


def _workspace_snapshot_sha256(
    snapshot: Mapping[PurePosixPath, _WorkspaceEntry],
) -> str:
    rows = [
        {
            "path": path.as_posix(),
            "kind": entry.kind,
            "mode": entry.mode,
            "owner": entry.owner,
            "links": entry.links,
            "size": entry.size,
            "sha256": entry.sha256,
        }
        for path, entry in sorted(snapshot.items(), key=lambda item: item[0].as_posix())
    ]
    return hashlib.sha256(canonical_json_bytes(rows)).hexdigest()


def _snapshot_changed_paths(
    before: Mapping[PurePosixPath, _WorkspaceEntry],
    after: Mapping[PurePosixPath, _WorkspaceEntry],
) -> set[PurePosixPath]:
    return {path for path in set(before) | set(after) if before.get(path) != after.get(path)}


def _changed_files_safe(
    before: Mapping[PurePosixPath, _WorkspaceEntry],
    after: Mapping[PurePosixPath, _WorkspaceEntry],
    changed_paths: set[PurePosixPath],
) -> bool:
    for relative in changed_paths:
        previous = before.get(relative)
        current = after.get(relative)
        if (
            previous is None
            or current is None
            or previous.kind != "file"
            or current.kind != "file"
            or previous.owner != os.getuid()
            or current.owner != previous.owner
            or current.mode != previous.mode
            or current.mode & (stat.S_IWGRP | stat.S_IWOTH)
            or previous.links != 1
            or current.links != 1
            or current.size > 1024 * 1024
            or previous.sha256 == current.sha256
        ):
            return False
    return True


def _wait_for_process_exit_without_reaping(
    process: subprocess.Popen[bytes],
    timeout_seconds: int,
) -> bool:
    if hasattr(select, "kqueue") and hasattr(select, "kevent"):
        try:
            queue = select.kqueue()
            event = select.kevent(
                process.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                return bool(queue.control((event,), 1, timeout_seconds))
            finally:
                queue.close()
        except OSError:
            return False
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        return False
    return True


def _quiesce_process_group(process: subprocess.Popen[bytes]) -> bool:
    process_group = process.pid
    for selected_signal, pause in (
        (signal.SIGTERM, 0.1),
        (signal.SIGKILL, 0.05),
    ):
        try:
            os.kill(process.pid, selected_signal)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        try:
            os.killpg(process_group, selected_signal)
        except ProcessLookupError:
            pass
        except OSError:
            pass
        time.sleep(pause)
    try:
        process.wait(timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + 1
    while True:
        try:
            os.killpg(process_group, 0)
        except ProcessLookupError:
            return True
        except OSError:
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _validator_sha256(task: TaskSpec, workspace: Path) -> str:
    validator_path = workspace / "tests" / f"test_{task.task_id.casefold()}.py"
    try:
        metadata = validator_path.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_size <= 0
            or metadata.st_size > 1024 * 1024
        ):
            _reject()
        validator_file_sha256 = hashlib.sha256(validator_path.read_bytes()).hexdigest()
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()
    cases = _VALIDATOR_EXPECTED_OBSERVATIONS.get(task.task_id)
    if cases is None or len(cases) != task.expected_tests:
        _reject()
    document = {
        "task_id": task.task_id,
        "task_version": task.version,
        "expected_tests": task.expected_tests,
        "validation_argv": list(task.validation_argv),
        "validator_cases": [
            {"case_id": case_id, "expected": expected} for case_id, expected in cases
        ],
        "validator_file_sha256": validator_file_sha256,
        "validator_ast_policy": {
            "allowed_imports": sorted(_VALIDATOR_ALLOWED_IMPORTS),
            "forbidden_attributes": sorted(_VALIDATOR_FORBIDDEN_ATTRIBUTES),
            "forbidden_names": sorted(_VALIDATOR_FORBIDDEN_NAMES),
            "target": list(_VALIDATOR_TARGETS[task.task_id]),
            "version": _VALIDATOR_AST_POLICY_VERSION,
        },
        "validator_worker_sha256": hashlib.sha256(_VALIDATOR_WORKER.encode("utf-8")).hexdigest(),
        "validator_profile_version": _VALIDATOR_PROFILE_VERSION,
        "validator_protocol_version": _VALIDATOR_PROTOCOL_VERSION,
    }
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


def _recall_result(
    condition: Condition,
    recall: RecallEvidence,
) -> dict[str, object]:
    if condition == "baseline":
        return {
            "invoked": False,
            "status": "not_invoked",
            "estimated_tokens": None,
            "selected_count": None,
            "omitted_count": None,
            "brief_sha256": None,
        }
    return {
        "invoked": True,
        "status": "ok",
        "estimated_tokens": recall.estimated_tokens,
        "selected_count": recall.selected_count,
        "omitted_count": recall.omitted_count,
        "brief_sha256": recall.brief_sha256,
    }


def _empty_usage() -> TokenUsage:
    return TokenUsage(
        input_tokens=None,
        cached_input_tokens=None,
        cache_write_input_tokens=None,
        uncached_input_tokens=None,
        output_tokens=None,
        reasoning_output_tokens=None,
        total_tokens=None,
    )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_public_file_metadata(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_PUBLIC_RESULT_BYTES
    ):
        _reject()


def _file_identity(metadata: os.stat_result) -> tuple[int, int, int]:
    return (metadata.st_dev, metadata.st_ino, metadata.st_mode)


def _read_bounded(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(descriptor, min(64 * 1024, maximum - total + 1))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > maximum:
            _reject()
    if total <= 0:
        _reject()
    return b"".join(chunks)


def _read_small_private_file(
    path: Path,
    maximum: int,
    *,
    allow_empty: bool = False,
) -> bytes:
    descriptor = -1
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > maximum
        ):
            _reject()
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stable_file_metadata(before) != _stable_file_metadata(opened):
            _reject()
        if before.st_size == 0:
            payload = b""
        else:
            payload = _read_bounded(descriptor, maximum)
        after = path.lstat()
        if _stable_file_metadata(opened) != _stable_file_metadata(after):
            _reject()
        if not payload and not allow_empty:
            _reject()
        return payload
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
    raise AssertionError("unreachable")


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _reject()
        result[key] = value
    return result


def _validate_public_result(result: Mapping[str, object]) -> None:
    if set(result) != _PUBLIC_TOP_LEVEL_FIELDS:
        _reject()
    if any(field in result for field in _PRIVATE_FIELD_NAMES):
        _reject()
    for field, expected in _PUBLIC_NESTED_FIELDS.items():
        value = result.get(field)
        if not isinstance(value, Mapping) or set(value) != expected:
            _reject()
        if any(private in value for private in _PRIVATE_FIELD_NAMES):
            _reject()
    if type(result.get("schema_version")) is not int or result.get("schema_version") != 3:
        _reject()
    if result.get("benchmark_id") != _BENCHMARK_ID:
        _reject()
    _require_choice(
        result.get("artifact_scope"),
        {"single-pair-pilot", "full-pilot", "public-v1"},
    )
    if result.get("runtime_evidence_class") != "self-reported":
        _reject()
    _require_string(result.get("run_id"), r"run-[a-z0-9-]+", 100)
    _require_string(result.get("task_id"), r"T[0-9]{2}", 3)
    _require_integer(result.get("task_version"), minimum=1)
    condition = _require_choice(result.get("condition"), {"baseline", "pmh"})
    _require_integer(result.get("replicate"), minimum=1)
    _require_choice(result.get("order"), {"AB", "BA"})
    _require_string(result.get("fixture_commit"), r"[0-9a-f]{40}", 40)
    _require_string(result.get("harness_commit"), r"[0-9a-f]{40}", 40)
    for field in (
        "manifest_sha256",
        "harness_sha256",
        "model_sandbox_profile_sha256",
        "workspace_sha256",
        "memory_snapshot_sha256",
        "prompt_sha256",
        "validator_sha256",
        "validation_workspace_sha256",
        "validator_evidence_sha256",
        "raw_evidence_sha256",
    ):
        _require_string(result.get(field), r"[0-9a-f]{64}", 64)
    codex_version = _require_string(
        result.get("codex_cli_version"),
        r"[A-Za-z0-9][A-Za-z0-9 ._+-]*",
        120,
    )
    _reject_secret_like(codex_version)
    requested_model = _require_string(
        result.get("requested_model_id"),
        r"[A-Za-z0-9][A-Za-z0-9._+-]*",
        128,
    )
    _reject_secret_like(requested_model)
    observed_model = result.get("observed_model_id")
    if observed_model is not None:
        observed = _require_string(
            observed_model,
            r"[A-Za-z0-9][A-Za-z0-9._+-]*",
            128,
        )
        _reject_secret_like(observed)
    _require_string(
        result.get("reasoning_effort"),
        r"[A-Za-z0-9][A-Za-z0-9_-]*",
        40,
    )
    success = _require_boolean(result.get("success"))
    timed_out = _require_boolean(result.get("timed_out"))
    _require_integer(result.get("timeout_seconds"), minimum=1)
    invalid_reason = result.get("invalid_reason")
    if invalid_reason is not None and invalid_reason not in _ALLOWED_INVALID_REASONS:
        _reject()
    if success != (invalid_reason is None) or timed_out != (invalid_reason == "timeout"):
        _reject()
    tests_passed = _require_integer(result.get("tests_passed"), minimum=0)
    tests_total = _require_integer(result.get("tests_total"), minimum=0)
    if tests_passed > tests_total or (
        success and (tests_total <= 0 or tests_passed != tests_total)
    ):
        _reject()
    for field in (
        "duration_ms",
        "tool_call_count",
        "wrong_route_count",
    ):
        _require_integer(result.get(field), minimum=0)
    _validate_recall_result(condition, result["recall"])
    _validate_token_result(success, result["tokens"])
    _validate_contamination(result["contamination"])
    _validate_write_guard(result["write_guard"])
    contamination = result["contamination"]
    write_guard = result["write_guard"]
    assert isinstance(contamination, Mapping)
    assert isinstance(write_guard, Mapping)
    if success and (
        any(
            contamination[field] != 0
            for field in (
                "project_canary_hits",
                "source_canary_hits",
                "model_canary_hits",
            )
        )
        or write_guard["memory_snapshot_unchanged"] is not True
        or write_guard["pending_count_before"] != 0
        or write_guard["pending_count_after"] != 0
        or write_guard["trusted_count_before"] != write_guard["trusted_count_after"]
    ):
        _reject()
    _reject_private_strings(result)


def _validate_recall_result(condition: str, value: object) -> None:
    if not isinstance(value, Mapping):
        _reject()
    invoked = _require_boolean(value.get("invoked"))
    status = _require_choice(value.get("status"), {"not_invoked", "ok", "invalid"})
    optional_counts = (
        value.get("estimated_tokens"),
        value.get("selected_count"),
        value.get("omitted_count"),
    )
    brief_hash = value.get("brief_sha256")
    if condition == "baseline":
        if invoked or status != "not_invoked":
            _reject()
        if any(item is not None for item in (*optional_counts, brief_hash)):
            _reject()
        return
    if not invoked or status != "ok":
        _reject()
    estimated_tokens = _require_integer(value.get("estimated_tokens"), minimum=1)
    if estimated_tokens > 800:
        _reject()
    _require_integer(value.get("selected_count"), minimum=1)
    _require_integer(value.get("omitted_count"), minimum=0)
    _require_string(brief_hash, r"[0-9a-f]{64}", 64)


def _validate_token_result(success: bool, value: object) -> None:
    if not isinstance(value, Mapping):
        _reject()
    if value.get("mode") != "fresh_session_final_total":
        _reject()
    names = (
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    counts: dict[str, int | None] = {}
    for name in names:
        item = value.get(name)
        counts[name] = None if item is None else _require_integer(item, minimum=0)
    if success and counts["total_tokens"] is None:
        _reject()
    input_tokens = counts["input_tokens"]
    output_tokens = counts["output_tokens"]
    total_tokens = counts["total_tokens"]
    if input_tokens is not None and output_tokens is not None:
        if total_tokens != input_tokens + output_tokens:
            _reject()
    cached = counts["cached_input_tokens"]
    cache_write = counts["cache_write_input_tokens"]
    uncached = counts["uncached_input_tokens"]
    if input_tokens is not None and cached is not None and cache_write is not None:
        if uncached != input_tokens - cached - cache_write or input_tokens < (cached + cache_write):
            _reject()
    reasoning = counts["reasoning_output_tokens"]
    if reasoning is not None and output_tokens is not None and reasoning > output_tokens:
        _reject()


def _validate_contamination(value: object) -> None:
    if not isinstance(value, Mapping):
        _reject()
    for field in (
        "project_canary_hits",
        "source_canary_hits",
        "model_canary_hits",
    ):
        _require_integer(value.get(field), minimum=0)


def _validate_write_guard(value: object) -> None:
    if not isinstance(value, Mapping):
        _reject()
    _require_boolean(value.get("memory_snapshot_unchanged"))
    for field in (
        "pending_count_before",
        "pending_count_after",
        "trusted_count_before",
        "trusted_count_after",
    ):
        _require_integer(value.get(field), minimum=0)


def _require_integer(
    value: object,
    *,
    minimum: int,
) -> int:
    if type(value) is not int or value < minimum or value > _MAX_PUBLIC_INTEGER:
        _reject()
    return value


def _require_boolean(value: object) -> bool:
    if type(value) is not bool:
        _reject()
    return value


def _require_choice(value: object, choices: set[str]) -> str:
    if not isinstance(value, str) or value not in choices:
        _reject()
    return value


def _require_string(value: object, pattern: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or re.fullmatch(pattern, value) is None
    ):
        _reject()
    return value


def _reject_secret_like(value: str) -> None:
    lowered = value.casefold()
    if any(
        marker in lowered
        for marker in (
            "api-key",
            "api_key",
            "bearer ",
            "password",
            "secret",
            "sk-",
            "token=",
        )
    ):
        _reject()


def _reject_private_strings(value: object) -> None:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if key in _PRIVATE_FIELD_NAMES:
                _reject()
            _reject_private_strings(nested)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for nested in value:
            _reject_private_strings(nested)
        return
    if isinstance(value, str) and (
        "\x00" in value
        or "\n" in value
        or value.startswith(("/", "~", "file:"))
        or "/Users/" in value
        or "/private/" in value
        or "\\Users\\" in value
    ):
        _reject()


class _ExclusiveBinaryWriter:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._handle: object | None = None

    def __enter__(self):  # type: ignore[no-untyped-def]
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = os.open(self._path, flags, _PRIVATE_FILE_MODE)
        self._handle = os.fdopen(descriptor, "wb")
        return self._handle

    def __exit__(self, exc_type, exc, traceback):  # type: ignore[no-untyped-def]
        if self._handle is not None:
            self._handle.close()  # type: ignore[attr-defined]
        return False


def _exclusive_binary_writer(path: Path) -> _ExclusiveBinaryWriter:
    return _ExclusiveBinaryWriter(path)


def _write_private_bytes(path: Path, payload: bytes) -> None:
    with _exclusive_binary_writer(path) as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            _reject()
        os.fsync(descriptor)
    except BenchmarkRunnerError:
        raise
    except OSError:
        _reject()
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _sandbox_profile_identity_sha256(
    profile: str,
    *,
    workspace: Path,
    clean_home: Path,
    process_tmp: Path,
    private_codex: Path,
    sandbox_executable: Path,
    protected_home: Path,
) -> str:
    normalized = profile
    replacements = (
        (private_codex, "<PRIVATE_CODEX>"),
        (process_tmp, "<PROCESS_TMP>"),
        (clean_home, "<CLEAN_HOME>"),
        (workspace, "<WORKSPACE>"),
        (sandbox_executable, "<SANDBOX_EXECUTABLE>"),
        (protected_home, "<PROTECTED_HOME>"),
    )
    for path, marker in replacements:
        normalized = normalized.replace(_sandbox_string(os.fspath(path)), marker)
    if any(_sandbox_string(os.fspath(path)) in normalized for path, _ in replacements):
        _reject()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _model_sandbox_profile_sha256(
    config: ExecutionConfig,
    workspace: Path,
    allowed_paths: Sequence[PurePosixPath],
) -> str:
    identity_root = workspace.parent / ".sandbox-profile-identity"
    clean_home = identity_root / "home"
    process_tmp = identity_root / "tmp"
    private_codex = identity_root / "codex"
    profile = _codex_sandbox_profile(
        workspace,
        clean_home,
        process_tmp,
        private_codex,
        config.validator_sandbox_executable,
        _protected_home(config.codex_auth_file),
        allowed_paths,
    )
    return _sandbox_profile_identity_sha256(
        profile,
        workspace=workspace,
        clean_home=clean_home,
        process_tmp=process_tmp,
        private_codex=private_codex,
        sandbox_executable=config.validator_sandbox_executable,
        protected_home=_protected_home(config.codex_auth_file),
    )


def _sha256_value(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _reject() -> NoReturn:
    raise BenchmarkRunnerError("benchmark runner rejected")
