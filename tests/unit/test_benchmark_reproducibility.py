from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "run_public_benchmark.py"
MANIFEST = PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"
OFFICIAL_MANIFEST_SHA256 = "a8d2c7696ad3a3035291dfeba0cf2c4cc300cf3a90bb4f04e5f046cd011c8643"


def _load_cli_module() -> ModuleType:
    name = "pmh_public_benchmark_reproducibility_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_official_manifest_is_checked_against_fixed_preregistered_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    forged = tmp_path / "manifest.json"
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    document["tasks"][0]["prompt"] += " silently changed"
    forged.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "DEFAULT_MANIFEST", forged)

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._manifest_execution(forged)


def test_prepare_uses_one_stable_manifest_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    selected = tmp_path / "manifest.json"
    selected.write_bytes(MANIFEST.read_bytes())
    original_load = module.load_task_specs
    loaded_paths: list[Path] = []

    def mutate_after_task_load(path: Path) -> object:
        loaded_paths.append(path)
        tasks = original_load(path)
        path.write_bytes(path.read_bytes() + b" ")
        return tasks

    monkeypatch.setattr(module, "load_task_specs", mutate_after_task_load)
    private_root = tmp_path / "private"
    arguments = argparse.Namespace(
        private_root=private_root,
        manifest=selected,
        model="gpt-5.6-sol",
    )

    assert module._prepare(arguments) == 0
    state = json.loads((private_root / "benchmark-state.json").read_text(encoding="utf-8"))
    assert state["manifest_sha256"] == OFFICIAL_MANIFEST_SHA256
    assert loaded_paths and loaded_paths[0] != selected
    assert selected.read_bytes() == MANIFEST.read_bytes()


def test_resume_rejects_a_different_harness_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    private_root = tmp_path / "private"
    prepared = argparse.Namespace(
        private_root=private_root,
        manifest=MANIFEST,
        model="gpt-5.6-sol",
    )
    assert module._prepare(prepared) == 0
    state = json.loads((private_root / "benchmark-state.json").read_text(encoding="utf-8"))
    monkeypatch.setattr(
        module,
        "_harness_identity",
        lambda _manifest_sha256: (state["harness_commit"], "0" * 64),
    )
    resumed = argparse.Namespace(
        private_root=private_root,
        manifest=MANIFEST,
        scope="single-pair-pilot",
        replicates=1,
        task=["T01"],
        execute=False,
    )

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._run(resumed)


def test_execution_envelope_binds_all_task_profiles_and_actual_boundary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    monkeypatch.setattr(
        module,
        "_MANAGED_REQUIREMENTS_LOGICAL_PATH",
        tmp_path / "absent-logical" / "requirements.toml",
    )
    monkeypatch.setattr(
        module,
        "_MANAGED_REQUIREMENTS_CANONICAL_PATH",
        tmp_path / "absent-canonical" / "requirements.toml",
    )
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    module.build_fixture_repositories(root / "fixtures")
    codex = _executable(tmp_path / "codex", b"codex")
    sandbox = _executable(tmp_path / "sandbox-exec", b"sandbox")
    validator_python = _executable(tmp_path / "validator-python", b"python")
    auth = tmp_path / "auth-one" / "auth.json"
    auth.parent.mkdir()
    auth.write_bytes(b"{}\n")
    auth.chmod(0o600)
    tasks = module.load_task_specs(MANIFEST)
    config = module.ExecutionConfig(
        codex_executable=codex,
        codex_cli_version="codex-cli 0.146.0-alpha.3.1",
        requested_model_id="gpt-5.6-sol",
        reasoning_effort="low",
        timeout_seconds=600,
        validator_sandbox_executable=sandbox,
        validator_python_executable=validator_python,
        codex_auth_file=auth,
        manifest_sha256=OFFICIAL_MANIFEST_SHA256,
        harness_commit="a" * 40,
        harness_sha256="b" * 64,
    )

    module._seal_execution_envelope(root, tasks, config)
    envelope_path = root / "execution-envelope.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))

    assert set(envelope["boundary_files"]) == {
        "codex",
        "codex_auth",
        "git",
        "validator_python",
        "validator_sandbox",
    }
    assert envelope["managed_requirements"] == {"status": "absent"}
    assert set(envelope["model_sandbox_profile_sha256_by_task"]) == {task.task_id for task in tasks}
    validation_workspace = root / "runs" / "run-t01-r1-baseline" / "validation-workspace"
    shutil.copytree(root / "fixtures" / "ledger-tools", validation_workspace)
    assert envelope["model_sandbox_profile_sha256_by_task"]["T01"] == (
        module._model_sandbox_profile_sha256(
            config,
            validation_workspace,
            tasks[0].allowed_paths,
        )
    )
    assert module._seal_execution_envelope(root, tasks, config) is None

    changed_auth = tmp_path / "auth-two" / "auth.json"
    changed_auth.parent.mkdir()
    changed_auth.write_bytes(b"{}\n")
    changed_auth.chmod(0o600)
    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._seal_execution_envelope(
            root,
            tasks,
            replace(config, codex_auth_file=changed_auth),
        )


def test_execution_envelope_privately_binds_safe_managed_requirements(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    canonical_directory = tmp_path / "private" / "etc" / "codex"
    canonical_directory.mkdir(parents=True)
    canonical = canonical_directory / "requirements.toml"
    payload = b"[requirements]\nnetwork = false\n"
    canonical.write_bytes(payload)
    canonical.chmod(0o600)
    logical_directory = tmp_path / "etc"
    logical_directory.symlink_to(canonical_directory, target_is_directory=True)
    logical = logical_directory / "requirements.toml"
    monkeypatch.setattr(module, "_MANAGED_REQUIREMENTS_LOGICAL_PATH", logical)
    monkeypatch.setattr(module, "_MANAGED_REQUIREMENTS_CANONICAL_PATH", canonical)

    boundary = module._managed_requirements_boundary()

    assert boundary == {
        "path": str(canonical),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "status": "present",
    }


def test_managed_requirements_boundary_rejects_an_unexpected_symlink_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    expected = tmp_path / "private" / "etc" / "codex" / "requirements.toml"
    unexpected = tmp_path / "other" / "requirements.toml"
    unexpected.parent.mkdir()
    unexpected.write_text("[requirements]\n", encoding="utf-8")
    unexpected.chmod(0o600)
    logical = tmp_path / "etc" / "codex" / "requirements.toml"
    logical.parent.mkdir(parents=True)
    logical.symlink_to(unexpected)
    monkeypatch.setattr(module, "_MANAGED_REQUIREMENTS_LOGICAL_PATH", logical)
    monkeypatch.setattr(module, "_MANAGED_REQUIREMENTS_CANONICAL_PATH", expected)

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._managed_requirements_boundary()


def _executable(path: Path, payload: bytes) -> Path:
    path.write_bytes(payload)
    path.chmod(0o700)
    assert os.access(path, os.X_OK)
    return path
