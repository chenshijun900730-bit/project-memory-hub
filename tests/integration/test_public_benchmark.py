from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from pathlib import PurePosixPath

import pytest

import project_memory_hub.benchmark_runner as benchmark_runner
from project_memory_hub.benchmark_fixtures import (
    build_fixture_repositories,
    load_task_specs,
    prepare_recall_bundle,
)
from project_memory_hub.benchmark_runner import (
    BenchmarkRunnerError,
    ExecutionConfig,
    _MODEL_EXECUTABLE_PATHS,
    _VALIDATOR_EXPECTED_OBSERVATIONS,
    _VALIDATOR_PROTOCOL_VERSION,
    _VALIDATOR_WORKER,
    _codex_sandbox_profile,
    _implementation_ast_is_safe,
    _validator_response_matches,
    _validator_sandbox_profile,
    _validator_sha256,
    _workspace_snapshot,
    build_run_schedule,
    compose_task_prompt,
    execute_benchmark_run,
    read_public_jsonl,
    write_public_jsonl,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
MANIFEST = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"
MODEL_ID = "gpt-5.6-sol"


def test_outer_profile_uses_bounded_process_and_read_rules(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    clean_home = tmp_path / "clean-home"
    process_tmp = tmp_path / "process-tmp"
    private_codex = tmp_path / "private-codex"
    sandbox = Path("/usr/bin/sandbox-exec")
    protected_home = tmp_path / "protected-home"
    allowed_file = workspace / "ledger_tools" / "records.py"
    allowed_file.parent.mkdir(parents=True)
    allowed_file.write_text("# fixture\n", encoding="utf-8")

    profile = _codex_sandbox_profile(
        workspace,
        clean_home,
        process_tmp,
        private_codex,
        sandbox,
        protected_home,
        (PurePosixPath("ledger_tools/records.py"),),
    )

    for forbidden in (
        "(allow process*)",
        "(allow file-read-metadata file-test-existence)",
        '(subpath "/Applications/ChatGPT.app")',
        '(subpath "/Library/Preferences")',
        '(subpath "/dev")',
        '(subpath "/etc")',
        '(subpath "/private/etc")',
        '(subpath "/private/var/db")',
        '(subpath "/usr")',
        "\n(allow mach-bootstrap)\n",
        "\n(allow mach-register ",
    ):
        assert forbidden not in profile
    for managed_requirements_path in (
        "/etc/codex/requirements.toml",
        "/private/etc/codex/requirements.toml",
    ):
        parent_only_metadata = (
            f'(with-filter (process-path "{private_codex}")\n'
            "  (allow file-read-metadata file-test-existence\n"
            f'    (path-ancestors "{managed_requirements_path}")))'
        )
        parent_only_read = (
            f'(with-filter (process-path "{private_codex}")\n'
            "  (allow file-read* file-read-metadata file-test-existence\n"
            f'    (literal "{managed_requirements_path}")))'
        )
        assert parent_only_metadata in profile
        assert parent_only_read in profile
        assert profile.count(f'(path-ancestors "{managed_requirements_path}")') == 1
        assert profile.count(f'(literal "{managed_requirements_path}")') == 1
    parent_only_dns_metadata = (
        f'(with-filter (process-path "{private_codex}")\n'
        "  (allow file-read-metadata file-test-existence\n"
        '    (path-ancestors "/var/run/mDNSResponder")))'
    )
    assert parent_only_dns_metadata in profile
    assert profile.count('(path-ancestors "/var/run/mDNSResponder")') == 1
    for unneeded_dns_service in (
        "com.apple.SystemConfiguration.DNSConfiguration",
        "com.apple.SystemConfiguration.SCNetworkReachability",
        "com.apple.dnssd.service",
        "com.apple.system.DirectoryService.libinfo_v1",
    ):
        assert unneeded_dns_service not in profile
    assert f'(literal "{private_codex}")' in profile
    assert f'(literal "{workspace / ".git"}")' in profile
    assert f'(subpath "{workspace / ".git"}")' in profile
    assert f'(literal "{allowed_file}")' in profile
    write_rule = profile.split("(allow file-write-data", 1)[1].split(")\n", 1)[0]
    assert f'(subpath "{workspace}")' not in write_rule
    assert profile.count("(allow process-fork)") == 1
    fork_rule = profile.split("(allow process-fork)", 1)[0].rsplit(
        "(with-filter",
        1,
    )[1]
    assert f'(process-path "{private_codex}")' in fork_rule
    exec_rule = profile.split("(allow process-exec", 1)[1].split(")\n", 1)[0]
    assert '(subpath "/usr/bin")' not in exec_rule
    assert '(subpath "/bin")' not in exec_rule
    assert f'(subpath "{workspace}")' not in exec_rule
    for forbidden_executable in (
        "/bin/bash",
        "/bin/sh",
        "/usr/bin/clang",
        "/usr/bin/perl",
        "/usr/bin/python3",
        "/usr/bin/ruby",
        "/usr/bin/swift",
    ):
        assert forbidden_executable not in _MODEL_EXECUTABLE_PATHS
        assert f'(literal "{forbidden_executable}")' not in exec_rule


def test_validator_profile_has_no_general_process_mach_or_host_read_access(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    validator_tmp = tmp_path / "validator-tmp"
    python = Path("/usr/bin/python3")
    sandbox = Path("/usr/bin/sandbox-exec")
    protected_home = tmp_path / "protected-home"

    profile = _validator_sandbox_profile(
        workspace,
        validator_tmp,
        python,
        sandbox,
        protected_home,
    )

    for forbidden in (
        "(allow process*)",
        "(allow process-info*)",
        "(allow sysctl-read)",
        "(allow mach*)",
        "(allow ipc-posix*)",
        "(allow file-read*)",
        '(subpath "/System")',
        '(subpath "/usr/share")',
        '(literal "/dev/random")',
        '(literal "/private/etc/localtime")',
    ):
        assert forbidden not in profile
    assert f'(literal "{python}")' in profile
    assert profile.count(f'(subpath "{workspace}")') == 1
    assert f'(subpath "{validator_tmp}")' in profile
    assert "(deny network*)" in profile


def test_workspace_snapshot_uses_streaming_bounded_walk() -> None:
    assert "rglob" not in _workspace_snapshot.__code__.co_names


def test_workspace_snapshot_rejects_directory_change_after_enumeration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "initial.py").write_text("VALUE = 1\n", encoding="utf-8")
    real_scandir = os.scandir

    class LateEntryScandir:
        def __init__(self, path: str | os.PathLike[str]) -> None:
            self.path = Path(path)
            with real_scandir(path) as entries:
                self.entries = list(entries)

        def __enter__(self) -> LateEntryScandir:
            return self

        def __iter__(self):  # type: ignore[no-untyped-def]
            return iter(self.entries)

        def __exit__(self, *args: object) -> None:
            if self.path == workspace:
                (workspace / "late.py").write_text("VALUE = 2\n", encoding="utf-8")

    monkeypatch.setattr(os, "scandir", LateEntryScandir)

    with pytest.raises(BenchmarkRunnerError, match="benchmark runner rejected"):
        _workspace_snapshot(workspace)


def test_validator_uses_parent_owned_task_oracle_without_trusted_child_report() -> None:
    tasks = load_task_specs(MANIFEST)

    assert _VALIDATOR_PROTOCOL_VERSION == 3
    assert "unittest" not in _VALIDATOR_WORKER
    assert "report_fd" not in _VALIDATOR_WORKER
    assert "PMHVAL" not in _VALIDATOR_WORKER
    assert _VALIDATOR_WORKER.index("target = load_target()") < _VALIDATOR_WORKER.index(
        "nonce = _urandom(16).hex()"
    )
    assert '{"nonce": nonce, "observation": observation}' in _VALIDATOR_WORKER
    assert set(_VALIDATOR_EXPECTED_OBSERVATIONS) == {task.task_id for task in tasks}
    assert all(
        len(_VALIDATOR_EXPECTED_OBSERVATIONS[task.task_id]) == task.expected_tests for task in tasks
    )


def test_validator_response_requires_exact_unique_nonce_schema() -> None:
    expected = {"kind": "return", "value": "alpha  "}
    nonce = "0123456789abcdef0123456789abcdef"
    observed = (
        b'{"nonce":"0123456789abcdef0123456789abcdef",'
        b'"observation":{"kind":"return","value":"alpha  "}}\n'
    )
    seen: set[str] = set()

    assert _validator_response_matches(observed, expected, seen) is True
    assert _validator_response_matches(observed, expected, seen) is False
    assert (
        _validator_response_matches(
            (
                b'{"nonce":"11111111111111111111111111111111",'
                b'"nonce":"22222222222222222222222222222222",'
                b'"observation":{"kind":"return","value":"alpha  "}}\n'
            ),
            expected,
            set(),
        )
        is False
    )
    assert (
        _validator_response_matches(
            (
                b'{"extra":0,"nonce":"11111111111111111111111111111111",'
                b'"observation":{"kind":"return","value":"alpha  "}}\n'
            ),
            expected,
            set(),
        )
        is False
    )
    assert (
        _validator_response_matches(
            (
                b'{"nonce":"ABCDEFABCDEFABCDEFABCDEFABCDEFAB",'
                b'"observation":{"kind":"return","value":"alpha  "}}\n'
            ),
            expected,
            set(),
        )
        is False
    )
    assert nonce in seen


def test_frozen_fixture_implementations_pass_static_validator_policy(tmp_path: Path) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    repository_by_id = {repository.fixture_id: repository for repository in repositories}

    assert all(
        _implementation_ast_is_safe(task, repository_by_id[task.fixture_id].path) for task in tasks
    )


@pytest.mark.parametrize(
    "source",
    (
        "import os\n\ndef normalize_record(line: str) -> str:\n    return line\n",
        "module = __import__('os')\n\ndef normalize_record(line: str) -> str:\n    return line\n",
        (
            "from pathlib import Path\n"
            "Path('import-side-effect').write_text('bad')\n\n"
            "def normalize_record(line: str) -> str:\n"
            "    return line\n"
        ),
        (
            "from pathlib import Path\n\n"
            "def normalize_record(line: Path('import-annotation').resolve()) -> str:\n"
            "    return str(line)\n"
        ),
        "value = eval('1')\n\ndef normalize_record(line: str) -> str:\n    return line\n",
        (
            "def normalize_record(line: str) -> str:\n"
            "    builtins_map = __builtins__\n"
            "    return line\n"
        ),
        (
            "import typing\n\n"
            "def normalize_record(line: str) -> str:\n"
            "    return typing.sys.modules['builtins'].eval(\"'forged'\")\n"
        ),
        ("import operator\n\ndef normalize_record(line: str) -> str:\n    return line\n"),
        (
            "from pathlib import Path\n\n"
            "marker: Path('annotation-side-effect').resolve()\n\n"
            "def normalize_record(line: str) -> str:\n"
            "    return line\n"
        ),
        "def unrelated(line: str) -> str:\n    return line\n",
    ),
)
def test_static_validator_policy_rejects_unsafe_implementation(
    tmp_path: Path,
    source: str,
) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    task = load_task_specs(MANIFEST)[0]
    repository = next(item for item in repositories if item.fixture_id == task.fixture_id)
    (repository.path / task.allowed_paths[0]).write_text(source, encoding="utf-8")

    assert _implementation_ast_is_safe(task, repository.path) is False


def test_validator_identity_binds_static_policy_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = build_fixture_repositories(tmp_path / "fixtures")[0]
    task = load_task_specs(MANIFEST)[0]
    original = _validator_sha256(task, repository.path)

    monkeypatch.setattr(
        benchmark_runner,
        "_VALIDATOR_AST_POLICY_VERSION",
        "validator-pure-python-test-change",
    )

    assert _validator_sha256(task, repository.path) != original


def test_schedule_is_balanced_and_alternates_repeated_order() -> None:
    tasks = load_task_specs(MANIFEST)

    pilot = build_run_schedule(tasks, replicates=1)
    public = build_run_schedule(tasks, replicates=3)

    assert len(pilot) == 16
    assert len(public) == 48
    assert sum(spec.condition == "baseline" for spec in public) == 24
    assert sum(spec.condition == "pmh" for spec in public) == 24
    assert [spec.condition for spec in public if spec.task.task_id == "T01"] == [
        "baseline",
        "pmh",
        "pmh",
        "baseline",
        "baseline",
        "pmh",
    ]
    assert len({spec.run_id for spec in public}) == len(public)


def test_prompt_differs_only_by_bounded_recall_appendix() -> None:
    task = load_task_specs(MANIFEST)[0]

    baseline = compose_task_prompt(task, recall_text=None)
    treatment = compose_task_prompt(task, recall_text="Current state\n- synthetic")

    assert baseline in treatment
    assert "<project-memory-hub-recall>" not in baseline
    assert treatment.endswith(
        "<project-memory-hub-recall>\nCurrent state\n- synthetic\n</project-memory-hub-recall>"
    )


def test_executes_one_pair_with_private_raw_evidence_and_public_allowlist(
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
    executable = _fake_codex(tmp_path / "fake-codex", mode="success")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    task = tasks[0]
    recall = bundle.recalls[0]
    specs = build_run_schedule((task,), replicates=1)

    results = [
        execute_benchmark_run(
            spec,
            repository,
            recall,
            bundle,
            private_root=tmp_path / "private-runs",
            config=config,
        )
        for spec in specs
    ]

    assert [result["condition"] for result in results] == ["baseline", "pmh"]
    assert all(result["success"] is True for result in results)
    assert results[0]["tokens"]["total_tokens"] == 120
    assert results[1]["tokens"]["total_tokens"] == 90
    assert results[0]["recall"]["status"] == "not_invoked"
    assert results[1]["recall"]["status"] == "ok"
    assert results[1]["recall"]["brief_sha256"] == recall.brief_sha256
    assert results[0]["tool_call_count"] == results[1]["tool_call_count"] == 1
    assert results[0]["wrong_route_count"] == results[1]["wrong_route_count"] == 0
    assert results[0]["observed_model_id"] is None
    assert results[1]["observed_model_id"] is None
    assert results[0]["schema_version"] == results[1]["schema_version"] == 3
    assert results[0]["artifact_scope"] == results[1]["artifact_scope"] == "single-pair-pilot"
    assert all(result["runtime_evidence_class"] == "self-reported" for result in results)
    assert results[0]["manifest_sha256"] == results[1]["manifest_sha256"] == "a" * 64
    assert results[0]["harness_commit"] == results[1]["harness_commit"] == "b" * 40
    assert results[0]["harness_sha256"] == results[1]["harness_sha256"] == "c" * 64
    assert results[0]["model_sandbox_profile_sha256"] == results[1]["model_sandbox_profile_sha256"]
    assert all(len(str(result["validation_workspace_sha256"])) == 64 for result in results)
    assert all(len(str(result["validator_evidence_sha256"])) == 64 for result in results)
    assert all(result["write_guard"]["memory_snapshot_unchanged"] for result in results)
    assert all(
        result["contamination"]
        == {
            "project_canary_hits": 0,
            "source_canary_hits": 0,
            "model_canary_hits": 0,
        }
        for result in results
    )

    private_runs = tmp_path / "private-runs"
    assert all((private_runs / spec.run_id / "events.jsonl").is_file() for spec in specs)
    assert all((private_runs / spec.run_id / "prompt.txt").is_file() for spec in specs)
    assert all((private_runs / spec.run_id / "public-result.json").is_file() for spec in specs)
    assert all((private_runs / spec.run_id / "public-result.sha256").is_file() for spec in specs)
    assert all((private_runs / spec.run_id / "validation-workspace").is_dir() for spec in specs)
    assert all(not (private_runs / spec.run_id / "workspace").exists() for spec in specs)
    assert all(not (private_runs / spec.run_id / "model-output").exists() for spec in specs)
    for spec in specs:
        transcript_lines = [
            json.loads(line)
            for line in (private_runs / spec.run_id / "validator.txt")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        assert [line["case_id"] for line in transcript_lines] == [
            "preserve-trailing-spaces",
            "remove-one-crlf",
        ]
        assert all(line["completed"] is True for line in transcript_lines)
        assert all(line["matched"] is True for line in transcript_lines)
        assert all(line["protocol_valid"] is True for line in transcript_lines)
        assert all(set(line["response"]) == {"nonce", "observation"} for line in transcript_lines)
    public_path = tmp_path / "public.jsonl"
    write_public_jsonl(public_path, results)
    public_text = public_path.read_text(encoding="utf-8")
    assert "Current state" not in public_text
    assert "synthetic" not in public_text
    assert "thread_id" not in public_text
    assert str(tmp_path) not in public_text
    assert len(public_text.splitlines()) == 2


@pytest.mark.parametrize(
    ("mode", "invalid_reason"),
    (
        ("test-edit", "changed_path_violation"),
        ("nonzero", "codex_exit"),
        ("no-usage", "token_evidence_invalid"),
        ("zero-tests", "validator_failed"),
        ("spoof-validator", "validator_failed"),
        ("forge-validator-channel", "validator_failed"),
        ("forge-validator-observation", "validator_failed"),
        ("validator-import-side-effect", "validator_failed"),
        ("monkeypatch-unittest", "validator_failed"),
        ("wrong-model", "model_mismatch"),
        ("symlink-edit", "changed_path_violation"),
        ("git-metadata-tamper", "changed_path_violation"),
        ("stderr-flood", "codex_exit"),
    ),
)
def test_runner_preserves_invalid_runs_without_leaking_raw_details(
    tmp_path: Path,
    mode: str,
    invalid_reason: str,
) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode=mode)
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    task = tasks[0]
    spec = build_run_schedule((task,), replicates=1)[0]

    result = execute_benchmark_run(
        spec,
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )

    assert result["success"] is False
    assert result["invalid_reason"] == invalid_reason
    assert str(tmp_path) not in json.dumps(result)


def test_runner_times_out_process_group_and_keeps_result(tmp_path: Path) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="timeout")
    config = _execution_config(tmp_path, executable, timeout_seconds=1)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    task = tasks[0]
    spec = build_run_schedule((task,), replicates=1)[0]

    result = execute_benchmark_run(
        spec,
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )

    assert result["success"] is False
    assert result["timed_out"] is True
    assert result["invalid_reason"] == "timeout"


def test_timeout_kills_descendant_that_ignores_sigterm(tmp_path: Path) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="stubborn-timeout")
    config = _execution_config(tmp_path, executable, timeout_seconds=1)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")

    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )
    time.sleep(2.5)

    assert result["invalid_reason"] == "timeout"
    assert not (tmp_path / "descendant-survived").exists()


def test_normal_completion_kills_background_descendant(tmp_path: Path) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="normal-background")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")

    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )
    time.sleep(1.5)

    assert result["success"] is True
    assert not (tmp_path / "normal-descendant-survived").exists()


def test_validator_static_policy_rejects_background_descendant(tmp_path: Path) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="validator-background")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")

    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )
    time.sleep(1.5)

    assert result["success"] is False
    assert result["invalid_reason"] == "validator_failed"
    transcript = (tmp_path / "private-runs" / "run-t01-r1-baseline" / "validator.txt").read_bytes()
    assert transcript
    assert b'"ast_policy_valid":false' in transcript
    assert not (tmp_path / "validator-descendant-survived").exists()


def test_codex_process_receives_preventive_resource_limits(tmp_path: Path) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="assert-resources")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")

    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )

    assert result["success"] is True


def test_duration_excludes_private_launcher_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="success")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    original_copy = benchmark_runner._copy_private_executable

    def delayed_copy(source: Path, target: Path) -> tuple[int, int, int]:
        time.sleep(1)
        return original_copy(source, target)

    monkeypatch.setattr(benchmark_runner, "_copy_private_executable", delayed_copy)
    started = time.monotonic()
    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )
    elapsed_ms = int((time.monotonic() - started) * 1000)

    assert result["success"] is True
    assert elapsed_ms - result["duration_ms"] >= 900


def test_codex_process_uses_clean_auth_only_home_and_isolated_arguments(
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
    executable = _fake_codex(tmp_path / "fake-codex", mode="assert-isolated")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")

    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )

    assert result["success"] is True
    assert executable.is_file()


def test_auth_copy_is_removed_before_model_generated_work(tmp_path: Path) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="assert-auth-unlinked")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")

    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )

    assert result["success"] is True


def test_private_codex_launcher_is_removed_before_model_generated_work(
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
    executable = _fake_codex(
        tmp_path / "fake-codex",
        mode="assert-launcher-unlinked",
    )
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")

    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )

    assert result["success"] is True
    assert executable.is_file()


def test_public_writer_rejects_existing_symlink_or_private_fields(tmp_path: Path) -> None:
    real = tmp_path / "real.jsonl"
    linked = tmp_path / "linked.jsonl"
    real.write_text("{}\n", encoding="utf-8")
    linked.symlink_to(real)

    with pytest.raises(BenchmarkRunnerError, match="benchmark runner rejected"):
        write_public_jsonl(linked, [])
    with pytest.raises(BenchmarkRunnerError, match="benchmark runner rejected"):
        write_public_jsonl(tmp_path / "private.jsonl", [{"prompt_text": "private"}])


def test_public_reader_round_trips_allowlisted_results_and_rejects_duplicate_keys(
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
    executable = _fake_codex(tmp_path / "fake-codex", mode="success")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )
    public_path = tmp_path / "public.jsonl"
    write_public_jsonl(public_path, [result])

    assert read_public_jsonl(public_path) == (result,)

    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text('{"schema_version":1,"schema_version":1}\n', encoding="utf-8")
    with pytest.raises(BenchmarkRunnerError, match="benchmark runner rejected"):
        read_public_jsonl(duplicate)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("duration_ms", True),
        ("fixture_commit", "not-a-commit"),
        ("codex_cli_version", "sk-private-secret-value"),
        ("success", "true"),
    ),
)
def test_public_writer_rejects_invalid_or_secret_values_in_allowed_fields(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="success")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )
    result[field] = value

    with pytest.raises(BenchmarkRunnerError, match="benchmark runner rejected"):
        write_public_jsonl(tmp_path / "rejected.jsonl", [result])


def test_public_writer_does_not_clobber_target_created_during_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repositories = build_fixture_repositories(tmp_path / "fixtures")
    tasks = load_task_specs(MANIFEST)
    bundle = prepare_recall_bundle(
        tmp_path / "runtime",
        repositories,
        tasks,
        model_id=MODEL_ID,
    )
    executable = _fake_codex(tmp_path / "fake-codex", mode="success")
    config = _execution_config(tmp_path, executable)
    repository = next(item for item in repositories if item.fixture_id == "ledger-tools")
    result = execute_benchmark_run(
        build_run_schedule((tasks[0],), replicates=1)[0],
        repository,
        bundle.recalls[0],
        bundle,
        private_root=tmp_path / "private-runs",
        config=config,
    )
    target = tmp_path / "public.jsonl"
    original_link = os.link

    def create_target_then_link(source: Path, destination: Path) -> None:
        destination.write_text("attacker-owned\n", encoding="utf-8")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", create_target_then_link)

    with pytest.raises(BenchmarkRunnerError, match="benchmark runner rejected"):
        write_public_jsonl(target, [result])
    assert target.read_text(encoding="utf-8") == "attacker-owned\n"


def _fake_codex(path: Path, *, mode: str) -> Path:
    validator_child = (
        "import pathlib,time;"
        "time.sleep(1);"
        f"pathlib.Path({str(path.parent / 'validator-descendant-survived')!r})"
        ".write_text('bad')"
    )
    validator_implementation = (
        "import subprocess,sys\n"
        f"subprocess.Popen([sys.executable, '-c', {validator_child!r}],\n"
        "    stdin=subprocess.DEVNULL,\n"
        "    stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL,\n"
        ")\n"
        "def normalize_record(line: str) -> str:\n"
        "    if line.endswith('\\r\\n'):\n"
        "        return line[:-2]\n"
        "    if line.endswith('\\n'):\n"
        "        return line[:-1]\n"
        "    return line\n"
    )
    validator_forge_implementation = (
        "import hashlib,os,struct,sys\n"
        "for argument in sys.argv[1:]:\n"
        "    try:\n"
        "        report_fd = int(argument)\n"
        "    except ValueError:\n"
        "        continue\n"
        "    frame = struct.pack(\n"
        "        '!8sB32s6I',\n"
        "        b'PMHVAL01',\n"
        "        1,\n"
        "        hashlib.sha256(b'tests.test_t01').digest(),\n"
        "        2, 0, 0, 0, 0, 0,\n"
        "    )\n"
        "    os.write(report_fd, frame)\n"
        "    os._exit(0)\n"
        "def normalize_record(line: str) -> str:\n"
        "    return 'wrong'\n"
    )
    validator_observation_forge = (
        "import json,os,sys\n"
        "observations = {\n"
        "    'preserve-trailing-spaces': "
        "{'kind': 'return', 'value': 'alpha  '},\n"
        "    'remove-one-crlf': {'kind': 'return', 'value': 'beta'},\n"
        "}\n"
        "payload = json.dumps(\n"
        "    observations[sys.argv[2]],\n"
        "    sort_keys=True,\n"
        "    separators=(',', ':'),\n"
        ").encode() + b'\\n'\n"
        "os.write(1, payload)\n"
        "os._exit(0)\n"
    )
    validator_import_side_effect = (
        "from pathlib import Path\n"
        f"Path({str(path.parent / 'validator-import-side-effect')!r}).write_text('bad')\n"
        "def normalize_record(line: str) -> str:\n"
        "    if line.endswith('\\r\\n'):\n"
        "        return line[:-2]\n"
        "    if line.endswith('\\n'):\n"
        "        return line[:-1]\n"
        "    return line\n"
    )
    script = f"""#!{sys.executable}
import json
import os
import pathlib
import resource
import sys
import time

prompt = sys.stdin.read()
mode = {mode!r}
early_thread = False
if mode == "assert-isolated":
    clean_home = pathlib.Path(os.environ["CODEX_HOME"])
    assert not (pathlib.Path.cwd() / ".git").exists()
    assert pathlib.Path(os.environ["HOME"]) == clean_home
    assert clean_home.is_relative_to(pathlib.Path.cwd().parent)
    assert (clean_home / "auth.json").is_file()
    assert not (clean_home / "AGENTS.md").exists()
    assert not (clean_home / "config.toml").exists()
    assert os.environ["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert os.environ["GIT_CONFIG_NOSYSTEM"] == "1"
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "0"
    assert os.environ["PYTHONNOUSERSITE"] == "1"
    assert "CODEX_THREAD_ID" not in os.environ
    assert not any(key.startswith("PROJECT_MEMORY_HUB") for key in os.environ)
    arguments = " ".join(sys.argv[1:])
    for required in (
        "--ignore-rules",
        "--disable memories",
        "--disable chronicle",
        "--disable apps",
        "--strict-config",
        "--dangerously-bypass-approvals-and-sandbox",
        "project_doc_max_bytes=0",
        "shell_environment_policy.inherit",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_OPTIONAL_LOCKS",
        "PYTHONNOUSERSITE",
    ):
        assert required in arguments
    assert "--sandbox workspace-write" not in arguments
    assert "sandbox_workspace_write.network_access=false" not in arguments
if mode == "assert-auth-unlinked":
    print(
        json.dumps({{"type": "thread.started", "thread_id": "private"}}),
        flush=True,
    )
    early_thread = True
    time.sleep(0.4)
    assert not (pathlib.Path(os.environ["CODEX_HOME"]) / "auth.json").exists()
if mode == "assert-launcher-unlinked":
    print(
        json.dumps({{"type": "thread.started", "thread_id": "private"}}),
        flush=True,
    )
    early_thread = True
    time.sleep(0.4)
    assert pathlib.Path(sys.argv[0]) != pathlib.Path({os.fspath(path)!r})
    assert not pathlib.Path(sys.argv[0]).exists()
if mode == "stderr-flood":
    sys.stderr.write("x" * (1024 * 1024 + 4096))
    sys.stderr.flush()
if mode == "timeout":
    time.sleep(5)
    raise SystemExit(0)
if mode == "stubborn-timeout":
    import subprocess
    child = (
        "import pathlib,signal,time;"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN);"
        "time.sleep(2);"
        f"pathlib.Path({str(path.parent / "descendant-survived")!r}).write_text('bad')"
    )
    subprocess.Popen([sys.executable, "-c", child])
    time.sleep(5)
    raise SystemExit(0)
if mode == "normal-background":
    import subprocess
    child = (
        "import pathlib,time;"
        "time.sleep(1);"
        f"pathlib.Path({str(path.parent / "normal-descendant-survived")!r}).write_text('bad')"
    )
    subprocess.Popen(
        [sys.executable, "-c", child],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
if mode == "assert-resources":
    assert resource.getrlimit(resource.RLIMIT_CORE)[0] == 0
    assert resource.getrlimit(resource.RLIMIT_FSIZE)[0] <= 16 * 1024 * 1024
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] <= 256
    assert resource.getrlimit(resource.RLIMIT_NPROC)[0] < 4000

root = pathlib.Path.cwd()
if mode == "git-metadata-tamper":
    readme = root / "README.md"
    readme.write_text(readme.read_text() + "\\nhidden change\\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("forged\\n", encoding="utf-8")
if mode in {{
    "success",
    "nonzero",
    "no-usage",
    "assert-isolated",
    "assert-auth-unlinked",
    "assert-launcher-unlinked",
    "assert-resources",
    "git-metadata-tamper",
    "normal-background",
    "wrong-model",
    "stderr-flood",
}}:
    (root / "ledger_tools" / "records.py").write_text(
        "def normalize_record(line: str) -> str:\\n"
        "    if line.endswith('\\\\r\\\\n'):\\n"
        "        return line[:-2]\\n"
        "    if line.endswith('\\\\n'):\\n"
        "        return line[:-1]\\n"
        "    return line\\n",
        encoding="utf-8",
    )
elif mode == "zero-tests":
    (root / "ledger_tools" / "records.py").write_text(
        "import os\\nos._exit(0)\\n",
        encoding="utf-8",
    )
elif mode == "spoof-validator":
    (root / "ledger_tools" / "records.py").write_text(
        "import os,sys\\n"
        "print('Ran 2 tests', file=sys.stderr, flush=True)\\n"
        "os._exit(0)\\n",
        encoding="utf-8",
    )
elif mode == "forge-validator-channel":
    (root / "ledger_tools" / "records.py").write_text(
        {validator_forge_implementation!r},
        encoding="utf-8",
    )
elif mode == "forge-validator-observation":
    (root / "ledger_tools" / "records.py").write_text(
        {validator_observation_forge!r},
        encoding="utf-8",
    )
elif mode == "validator-import-side-effect":
    (root / "ledger_tools" / "records.py").write_text(
        {validator_import_side_effect!r},
        encoding="utf-8",
    )
elif mode == "monkeypatch-unittest":
    (root / "ledger_tools" / "records.py").write_text(
        "import unittest\\n"
        "unittest.TestResult.addFailure = lambda *args, **kwargs: None\\n"
        "unittest.TestResult.addError = lambda *args, **kwargs: None\\n"
        "def normalize_record(line: str) -> str:\\n"
        "    return 'wrong'\\n",
        encoding="utf-8",
    )
elif mode == "validator-background":
    (root / "ledger_tools" / "records.py").write_text(
        {validator_implementation!r},
        encoding="utf-8",
    )
elif mode == "symlink-edit":
    target = root / "ledger_tools" / "records.py"
    target.unlink()
    target.symlink_to("/etc/passwd")
elif mode == "test-edit":
    (root / "tests" / "test_t01.py").write_text("# changed\\n", encoding="utf-8")

def emit(value):
    print(json.dumps(value, sort_keys=True, separators=(",", ":")), flush=True)

thread = {{"type": "thread.started", "thread_id": "private"}}
if mode == "wrong-model":
    thread["model_id"] = "different-model"
if not early_thread:
    emit(thread)
emit({{
    "type": "item.started",
    "item": {{"id": "private", "type": "command_execution", "command": "private"}},
}})
emit({{
    "type": "item.completed",
    "item": {{"id": "private", "type": "command_execution", "exit_code": 0}},
}})
if mode != "no-usage":
    treatment = "<project-memory-hub-recall>" in prompt
    input_tokens = 70 if treatment else 100
    emit({{
        "type": "turn.completed",
        "usage": {{
            "input_tokens": input_tokens,
            "cached_input_tokens": 10,
            "cache_write_input_tokens": 0,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
        }},
    }})
raise SystemExit(2 if mode == "nonzero" else 0)
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    assert os.access(path, os.X_OK)
    return path


def _execution_config(
    tmp_path: Path,
    codex_executable: Path,
    *,
    timeout_seconds: int = 30,
) -> ExecutionConfig:
    auth = tmp_path / "auth.json"
    if not auth.exists():
        auth.write_text("{}\n", encoding="utf-8")
        auth.chmod(0o600)
    validator_python = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        selected = subprocess.run(
            ["/usr/bin/xcrun", "--find", "python3"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        if selected.returncode == 0:
            validator_python = Path(selected.stdout.strip()).resolve()
    return ExecutionConfig(
        codex_executable=codex_executable,
        codex_cli_version="codex-cli test",
        requested_model_id=MODEL_ID,
        reasoning_effort="low",
        timeout_seconds=timeout_seconds,
        validator_sandbox_executable=_fake_validator_sandbox(tmp_path / "fake-validator-sandbox"),
        validator_python_executable=validator_python,
        codex_auth_file=auth,
        manifest_sha256="a" * 64,
        harness_commit="b" * 40,
        harness_sha256="c" * 64,
    )


def _fake_validator_sandbox(path: Path) -> Path:
    if path.exists():
        return path
    script = f"""#!{sys.executable}
import os
import sys

if sys.argv[1] != "-p" or "(deny default)" not in sys.argv[2]:
    raise SystemExit(91)
os.execv(sys.argv[3], sys.argv[3:])
"""
    path.write_text(script, encoding="utf-8")
    path.chmod(0o700)
    return path
