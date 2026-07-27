from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

import project_memory_hub.benchmark_fixtures as benchmark_fixtures
from project_memory_hub.benchmark_runner import compose_task_prompt


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "run_public_benchmark.py"
OFFICIAL_MANIFEST_SHA256 = "a8d2c7696ad3a3035291dfeba0cf2c4cc300cf3a90bb4f04e5f046cd011c8643"


def _load_cli_module() -> ModuleType:
    name = "pmh_public_benchmark_git_inputs_test"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _committed_repository(root: Path, files: dict[str, bytes]) -> Path:
    root.mkdir()
    _git(root, "init", "-q", "--initial-branch=main")
    for relative, payload in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    _git(root, "add", "--all")
    _git(
        root,
        "-c",
        "user.name=PMH Test",
        "-c",
        "user.email=benchmark.invalid",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-q",
        "-m",
        "fixture",
    )
    return root


def test_harness_identity_rejects_a_dirty_path_claiming_the_old_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    repository = _committed_repository(
        tmp_path / "harness",
        {"scripts/harness.py": b"VALUE = 1\n"},
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", repository)
    monkeypatch.setattr(module, "_HARNESS_PATHS", (Path("scripts/harness.py"),))
    monkeypatch.setattr(module, "_HARNESS_TREE_ROOTS", (Path("scripts"),))
    (repository / "scripts" / "harness.py").write_bytes(b"VALUE = 2\n")

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._harness_identity(
            OFFICIAL_MANIFEST_SHA256,
            require_committed=True,
        )


def test_harness_identity_rejects_dirty_transitive_source_dependency(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    repository = _committed_repository(
        tmp_path / "harness",
        {
            "scripts/harness.py": b"VALUE = 1\n",
            "src/project_memory_hub/config.py": b"SETTING = 1\n",
        },
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", repository)
    monkeypatch.setattr(module, "_HARNESS_PATHS", (Path("scripts/harness.py"),))
    monkeypatch.setattr(
        module,
        "_HARNESS_TREE_ROOTS",
        (Path("scripts"), Path("src")),
        raising=False,
    )
    (repository / "src" / "project_memory_hub" / "config.py").write_bytes(b"SETTING = 2\n")

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._harness_identity(
            OFFICIAL_MANIFEST_SHA256,
            require_committed=True,
        )


def test_harness_identity_recursively_binds_clean_source_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    repository = _committed_repository(
        tmp_path / "harness",
        {
            "scripts/harness.py": b"VALUE = 1\n",
            "src/project_memory_hub/config.py": b"SETTING = 1\n",
        },
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", repository)
    monkeypatch.setattr(module, "_HARNESS_PATHS", (Path("scripts/harness.py"),))
    monkeypatch.setattr(module, "_HARNESS_TREE_ROOTS", (Path("scripts"), Path("src")))

    commit, digest = module._harness_identity(
        OFFICIAL_MANIFEST_SHA256,
        require_committed=True,
    )

    assert commit == _git(repository, "rev-parse", "HEAD")
    assert len(digest) == 64


def test_execute_requires_commit_bound_harness_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    monkeypatch.setattr(module.sys, "dont_write_bytecode", True)
    private_root = tmp_path / "private"
    private_root.mkdir()

    class CommitBindingReached(RuntimeError):
        pass

    def harness_identity(
        _manifest_sha256: str,
        *,
        require_committed: bool = False,
    ) -> tuple[str, str]:
        if not require_committed:
            raise AssertionError("execute did not request commit-bound harness inputs")
        raise CommitBindingReached

    monkeypatch.setattr(
        module,
        "_manifest_snapshot",
        lambda _path: ((), {}, {}, OFFICIAL_MANIFEST_SHA256),
    )
    monkeypatch.setattr(module, "_harness_identity", harness_identity)
    arguments = argparse.Namespace(
        private_root=private_root,
        manifest=tmp_path / "manifest.json",
        execute=True,
    )

    with pytest.raises(CommitBindingReached):
        module._run(arguments)


def test_execute_rejects_python_without_bytecode_suppression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    monkeypatch.setattr(module.sys, "dont_write_bytecode", False)
    manifest_loaded = False

    def manifest_snapshot(_path: Path) -> object:
        nonlocal manifest_loaded
        manifest_loaded = True
        raise AssertionError("unsafe interpreter should be rejected before loading the harness")

    monkeypatch.setattr(module, "_manifest_snapshot", manifest_snapshot)
    arguments = argparse.Namespace(
        private_root=tmp_path,
        manifest=tmp_path / "manifest.json",
        execute=True,
    )

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._run(arguments)

    assert not manifest_loaded


def test_official_result_rejects_harness_hash_not_derived_from_its_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_cli_module()
    repository = _committed_repository(
        tmp_path / "public-harness",
        {
            "benchmarks/project-memory-effect/manifest.json": (
                PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"
            ).read_bytes(),
            "scripts/harness.py": b"VALUE = 1\n",
        },
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", repository)
    monkeypatch.setattr(
        module,
        "_HARNESS_PATHS",
        (
            Path("scripts/harness.py"),
            Path("benchmarks/project-memory-effect/manifest.json"),
        ),
    )
    monkeypatch.setattr(module, "_HARNESS_TREE_ROOTS", (Path("scripts"),))
    tasks, execution, recall_contract, manifest_sha256 = module._manifest_snapshot(
        module.DEFAULT_MANIFEST
    )
    task = tasks[0]
    result = {
        "artifact_scope": "single-pair-pilot",
        "runtime_evidence_class": "self-reported",
        "task_id": task.task_id,
        "task_version": task.version,
        "condition": "baseline",
        "replicate": 1,
        "run_id": "run-t01-r1-baseline",
        "order": task.order,
        "manifest_sha256": manifest_sha256,
        "harness_commit": _git(repository, "rev-parse", "HEAD"),
        "harness_sha256": "0" * 64,
        "requested_model_id": execution["requested_model_id"],
        "observed_model_id": None,
        "codex_cli_version": execution["codex_cli_version"],
        "reasoning_effort": execution["reasoning_effort"],
        "timeout_seconds": execution["timeout_seconds"],
        "memory_snapshot_sha256": recall_contract["memory_snapshot_sha256"],
        "recall": {},
        "success": True,
        "invalid_reason": None,
    }

    with pytest.raises(module.BenchmarkStateError, match="benchmark state rejected"):
        module._validate_official_public_results([result], "single-pair-pilot")


def test_fixture_verification_rejects_ignored_untracked_files(tmp_path: Path) -> None:
    repository = benchmark_fixtures.build_fixture_repositories(tmp_path / "fixtures")[0]
    poison = repository.path / "__pycache__" / "poison.pyc"
    poison.parent.mkdir()
    poison.write_bytes(b"not part of the fixture commit")

    with pytest.raises(
        benchmark_fixtures.BenchmarkFixtureError,
        match="benchmark fixture rejected",
    ):
        benchmark_fixtures.verify_fixture_repository(repository)


def test_fixture_builder_ignores_operator_git_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attributes = tmp_path / "global-attributes"
    attributes.write_text("*.py filter=poison\n", encoding="utf-8")
    config = tmp_path / "global-git-config"
    config.write_text(
        "[core]\n"
        f"\tattributesFile = {attributes}\n"
        '[filter "poison"]\n'
        "\tclean = false\n"
        "\trequired = true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))

    repositories = benchmark_fixtures.build_fixture_repositories(tmp_path / "fixtures")

    assert len(repositories) == 2


def test_fixture_builder_does_not_execute_git_from_inherited_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attacker = tmp_path / "attacker-bin"
    attacker.mkdir()
    fake_git = attacker / "git"
    fake_git.write_text("#!/bin/sh\nexit 93\n", encoding="utf-8")
    fake_git.chmod(0o700)
    monkeypatch.setenv("PATH", str(attacker))

    repositories = benchmark_fixtures.build_fixture_repositories(tmp_path / "fixtures")

    assert len(repositories) == 2


def test_fixture_materialization_comes_from_commit_tree_without_git_metadata(
    tmp_path: Path,
) -> None:
    repository = benchmark_fixtures.build_fixture_repositories(tmp_path / "fixtures")[0]
    hook = repository.path / ".git" / "hooks" / "post-checkout"
    hook.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
    hook.chmod(0o700)
    target = tmp_path / "model-workspace"

    benchmark_fixtures.materialize_fixture_repository(repository, target)

    assert not (target / ".git").exists()
    committed_paths = tuple(
        line
        for line in _git(repository.path, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
        if line
    )
    materialized_paths = tuple(
        sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
    )
    assert materialized_paths == tuple(sorted(committed_paths))
    for relative in committed_paths:
        expected = subprocess.run(
            ("git", "show", f"{repository.commit}:{relative}"),
            cwd=repository.path,
            check=True,
            capture_output=True,
            timeout=30,
        ).stdout
        assert (target / relative).read_bytes() == expected


def test_task_prompt_discloses_that_git_metadata_is_not_model_input() -> None:
    task = benchmark_fixtures.load_task_specs(
        PROJECT_ROOT / "benchmarks" / "project-memory-effect" / "manifest.json"
    )[0]

    prompt = compose_task_prompt(task, recall_text=None)

    assert "exported from the preregistered Git commit" in prompt
    assert "contains no .git metadata" in prompt


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return completed.stdout.strip()
