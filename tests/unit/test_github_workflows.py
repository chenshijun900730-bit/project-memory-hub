from __future__ import annotations

import importlib.util
import re
import shutil
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW_DIRECTORY = PROJECT_ROOT / ".github/workflows"
SECRET_SCANNING = PROJECT_ROOT / ".github/secret_scanning.yml"
DEPENDABOT = PROJECT_ROOT / ".github/dependabot.yml"
WORKFLOW_FILES = {
    "ci.yml",
    "linux-experimental.yml",
    "codeql.yml",
    "release-draft.yml",
}
PINNED_ACTIONS = {
    "actions/checkout": "de0fac2e4500dabe0009e67214ff5f5447ce83dd",
    "actions/setup-python": "a309ff8b426b58ec0e2a45f0f869d46889d02405",
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",
    "astral-sh/setup-uv": "08807647e7069bb48b6ef5acd8ec9567f424441b",
    "github/codeql-action": "7211b7c8077ea37d8641b6271f6a365a22a5fbfa",
}
RELEASE_WHEEL = "release-dist/project_memory_hub-0.2.1-py3-none-any.whl"
RELEASE_SDIST = "release-dist/project_memory_hub-0.2.1.tar.gz"


class _WorkflowLoader(yaml.SafeLoader):
    """Parse YAML 1.2 booleans without treating ``on`` as true."""


_WorkflowLoader.yaml_implicit_resolvers = {
    key: list(resolvers) for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _key, _resolvers in tuple(_WorkflowLoader.yaml_implicit_resolvers.items()):
    _WorkflowLoader.yaml_implicit_resolvers[_key] = [
        resolver for resolver in _resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", flags=re.IGNORECASE),
    list("tTfF"),
)


def _load_yaml(path: Path) -> dict[str, Any]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_WorkflowLoader)
    assert type(document) is dict
    return document


def _load_verifier() -> ModuleType:
    path = PROJECT_ROOT / "scripts/verify_workflows.py"
    assert path.is_file(), "workflow policy verifier must exist"
    spec = importlib.util.spec_from_file_location("verify_workflows_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_commands(job: dict[str, Any]) -> str:
    steps = job.get("steps")
    assert type(steps) is list
    return "\n".join(str(step.get("run", "")) for step in steps if type(step) is dict)


def _uses(workflow: dict[str, Any]) -> tuple[str, ...]:
    jobs = workflow.get("jobs")
    assert type(jobs) is dict
    return tuple(
        str(step["uses"])
        for job in jobs.values()
        if type(job) is dict
        for step in job.get("steps", [])
        if type(step) is dict and "uses" in step
    )


def _copy_policy(tmp_path: Path) -> tuple[Path, Path]:
    github = tmp_path / ".github"
    workflows = github / "workflows"
    shutil.copytree(WORKFLOW_DIRECTORY, workflows)
    secret_scanning = github / "secret_scanning.yml"
    shutil.copy2(SECRET_SCANNING, secret_scanning)
    shutil.copy2(DEPENDABOT, github / "dependabot.yml")
    return workflows, secret_scanning


def _write_yaml(path: Path, document: dict[str, Any]) -> None:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _assert_mutated_rejected(
    tmp_path: Path,
    workflow_name: str,
    mutate: Any,
    *,
    match: str,
) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    workflow_path = workflows / workflow_name
    document = deepcopy(_load_yaml(workflow_path))
    mutate(document)
    _write_yaml(workflow_path, document)
    with pytest.raises(verifier.WorkflowPolicyError, match=match):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


def _job_with_release_step(document: dict[str, Any]) -> dict[str, Any]:
    return next(
        job
        for job in document["jobs"].values()
        if any("gh release create" in str(step.get("run", "")) for step in job["steps"])
    )


def test_workflow_inventory_and_policy_verifier_pass() -> None:
    assert {path.name for path in WORKFLOW_DIRECTORY.glob("*.yml")} == WORKFLOW_FILES
    assert SECRET_SCANNING.is_file()
    assert DEPENDABOT.is_file()

    verifier = _load_verifier()
    verifier.verify_workflows(WORKFLOW_DIRECTORY, secret_scanning=SECRET_SCANNING)


def test_all_official_actions_are_fixed_to_verified_full_commit_shas() -> None:
    for workflow_path in sorted(WORKFLOW_DIRECTORY.glob("*.yml")):
        for use in _uses(_load_yaml(workflow_path)):
            action, separator, revision = use.partition("@")
            assert separator == "@"
            root = "/".join(action.split("/")[:2])
            assert root in PINNED_ACTIONS
            assert revision == PINNED_ACTIONS[root]
            assert re.fullmatch(r"[0-9a-f]{40}", revision)


def test_macos_ci_is_blocking_locked_and_complete() -> None:
    workflow = _load_yaml(WORKFLOW_DIRECTORY / "ci.yml")
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert type(jobs) is dict and jobs
    assert all(job["runs-on"].startswith("macos-") for job in jobs.values())
    assert all(job.get("continue-on-error") is not True for job in jobs.values())
    assert all("uv sync --locked --extra test" in _run_commands(job) for job in jobs.values())

    all_commands = "\n".join(_run_commands(job) for job in jobs.values())
    for command in (
        "uv lock --check",
        "uv run ruff format --check .",
        "uv run ruff check .",
        "uv run mypy src/project_memory_hub",
        "uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85",
        "node --check src/project_memory_hub/web/static/i18n.js",
        "node --check src/project_memory_hub/web/static/projects.js",
        "node --check src/project_memory_hub/web/static/sources.js",
        "uv run playwright install chromium",
        "uv run pytest tests/e2e -q",
        "uv build --wheel --sdist",
        "uv run twine check",
        "scripts/verify_public_assets.py docs/assets",
        "scripts/verify_release_artifacts.py",
    ):
        assert command in all_commands

    smoke_jobs = [job for job in jobs.values() if "smoke" in str(job.get("name", "")).casefold()]
    assert len(smoke_jobs) == 1
    setup_steps = [
        step
        for step in smoke_jobs[0]["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert [(step["id"], step["with"]) for step in setup_steps] == [
        ("python-311", {"python-version": "3.11"}),
        ("python-312", {"python-version": "3.12"}),
    ]
    smoke_commands = _run_commands(smoke_jobs[0])
    assert "uv python find --system 3.11" in smoke_commands
    assert "uv python find --system 3.12" in smoke_commands
    assert smoke_commands.count("--smoke-python") == 2


def test_linux_compatibility_is_explicitly_experimental_and_non_blocking() -> None:
    workflow = _load_yaml(WORKFLOW_DIRECTORY / "linux-experimental.yml")
    assert "experimental" in workflow["name"].casefold()
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert type(jobs) is dict
    assert set(jobs) == {
        "linux-python",
        "linux-javascript",
        "linux-browser",
        "linux-privacy",
    }
    for job_id, job in jobs.items():
        assert "experimental" in f"{job_id} {job.get('name', '')}".casefold()
        assert job["runs-on"].startswith("ubuntu-")
        assert job["continue-on-error"] is True
        assert "needs" not in job
        commands = _run_commands(job)
        if job_id == "linux-javascript":
            assert "uv sync --locked --extra test" not in commands
        else:
            assert "uv sync --locked --extra test" in commands

    python_commands = _run_commands(jobs["linux-python"])
    assert (
        "uv run pytest --ignore=tests/e2e --basetemp=$HOME/pmh-linux-pytest -ra -q"
        in python_commands
    )
    assert "--cov-fail-under" not in python_commands

    javascript_commands = _run_commands(jobs["linux-javascript"])
    for script in ("i18n.js", "projects.js", "sources.js"):
        assert f"node --check src/project_memory_hub/web/static/{script}" in javascript_commands

    browser_commands = _run_commands(jobs["linux-browser"])
    assert "uv run playwright install --with-deps chromium" in browser_commands
    assert "uv run pytest tests/e2e --basetemp=$HOME/pmh-linux-e2e -ra -q" in browser_commands

    privacy_commands = _run_commands(jobs["linux-privacy"])
    assert "uv run python scripts/verify_public_assets.py docs/assets" in privacy_commands


@pytest.mark.parametrize("mutation", ("dependency", "missing_job", "full_coverage"))
def test_linux_checks_remain_independent_and_platform_scoped(
    tmp_path: Path,
    mutation: str,
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        jobs = document["jobs"]
        if mutation == "dependency":
            jobs["linux-browser"]["needs"] = "linux-python"
        elif mutation == "missing_job":
            del jobs["linux-privacy"]
        else:
            step = next(
                item
                for item in jobs["linux-python"]["steps"]
                if "uv run pytest" in str(item.get("run", ""))
            )
            step["run"] = "uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85"

    _assert_mutated_rejected(
        tmp_path,
        "linux-experimental.yml",
        mutate,
        match="job_schema|jobs_invalid|run_command_invalid",
    )


def test_no_workflow_uses_a_windows_runner() -> None:
    combined = "\n".join(
        path.read_text(encoding="utf-8") for path in sorted(WORKFLOW_DIRECTORY.glob("*.yml"))
    )
    assert "windows-" not in combined.casefold()


def test_codeql_covers_python_and_javascript_with_minimal_permissions() -> None:
    workflow = _load_yaml(WORKFLOW_DIRECTORY / "codeql.yml")
    assert workflow["permissions"] == {"contents": "read", "security-events": "write"}
    jobs = workflow["jobs"]
    assert type(jobs) is dict and len(jobs) == 1
    job = next(iter(jobs.values()))
    assert sorted(job["strategy"]["matrix"]["language"]) == ["javascript-typescript", "python"]
    uses = _uses(workflow)
    assert uses == (
        f"actions/checkout@{PINNED_ACTIONS['actions/checkout']}",
        f"github/codeql-action/init@{PINNED_ACTIONS['github/codeql-action']}",
        f"github/codeql-action/analyze@{PINNED_ACTIONS['github/codeql-action']}",
    )
    steps = job["steps"]
    assert steps[1]["with"] == {"languages": "${{ matrix.language }}"}
    assert steps[2]["with"] == {"category": "/language:${{ matrix.language }}"}


def test_dependency_updates_and_secret_scanning_are_narrow() -> None:
    dependabot = _load_yaml(DEPENDABOT)
    assert dependabot["version"] == 2
    updates = dependabot["updates"]
    assert type(updates) is list
    assert {entry["package-ecosystem"] for entry in updates} == {"pip", "github-actions"}
    assert all(entry["directory"] == "/" for entry in updates)
    assert all(type(entry["open-pull-requests-limit"]) is int for entry in updates)
    assert all(entry["open-pull-requests-limit"] > 0 for entry in updates)

    assert _load_yaml(SECRET_SCANNING) == {"paths-ignore": []}


def test_release_is_tag_only_and_creates_a_draft_after_every_verification() -> None:
    workflow = _load_yaml(WORKFLOW_DIRECTORY / "release-draft.yml")
    assert workflow["on"] == {"push": {"tags": ["v*"]}}
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert set(jobs) == {"verify", "draft"}
    verify = jobs["verify"]
    draft = jobs["draft"]
    assert verify["permissions"] == {"contents": "read"}
    assert draft["permissions"] == {"contents": "write"}
    assert verify["runs-on"] == draft["runs-on"] == "macos-14"
    assert draft["needs"] == "verify"
    assert all(job.get("continue-on-error") is not True for job in jobs.values())
    setup_steps = [
        step
        for step in verify["steps"]
        if str(step.get("uses", "")).startswith("actions/setup-python@")
    ]
    assert [(step["id"], step["with"]) for step in setup_steps] == [
        ("python-311", {"python-version": "3.11"}),
        ("python-312", {"python-version": "3.12"}),
    ]
    commands = _run_commands(verify)
    for marker in (
        "GITHUB_REF_NAME",
        "pyproject.toml",
        "uv sync --locked --extra test",
        "uv run playwright install chromium",
        "--cov-branch --cov-fail-under=85",
        "uv build --wheel --sdist",
        "uv run twine check",
        "scripts/verify_release_artifacts.py",
        "scripts/verify_public_assets.py docs/assets",
        "git diff --quiet --exit-code HEAD --",
        'scripts/verify_release_checkout.py --expected-head "$GITHUB_SHA"',
        "scripts/create_checksums.py",
    ):
        assert marker in commands
    verify_uses = tuple(step["uses"] for step in verify["steps"] if "uses" in step)
    draft_uses = tuple(step["uses"] for step in draft["steps"] if "uses" in step)
    assert f"actions/upload-artifact@{PINNED_ACTIONS['actions/upload-artifact']}" in verify_uses
    assert draft_uses == (
        f"actions/download-artifact@{PINNED_ACTIONS['actions/download-artifact']}",
    )
    upload = next(
        step for step in verify["steps"] if "actions/upload-artifact@" in step.get("uses", "")
    )
    assert upload["with"]["path"].splitlines() == [
        RELEASE_WHEEL,
        RELEASE_SDIST,
        "release-dist/SHA256SUMS",
    ]
    release_step = draft["steps"][-1]
    release_command = str(release_step["run"])
    assert "gh release create --draft" in release_command
    assert RELEASE_WHEEL in release_command
    assert RELEASE_SDIST in release_command
    assert "*.whl" not in release_command
    assert "*.tar.gz" not in release_command
    assert release_step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "GH_REPO": "${{ github.repository }}",
    }


def test_release_build_requires_a_clean_checkout_bound_to_github_sha() -> None:
    workflow = _load_yaml(WORKFLOW_DIRECTORY / "release-draft.yml")
    commands = _run_commands(workflow["jobs"]["verify"])

    tracked_check = "git diff --quiet --exit-code HEAD --"
    checkout_check = (
        'uv run python scripts/verify_release_checkout.py --expected-head "$GITHUB_SHA"'
    )
    build = "uv build --wheel --sdist --out-dir release-dist"
    assert commands.count(tracked_check) == 1
    assert commands.count(checkout_check) == 1
    assert commands.index(tracked_check) < commands.index(checkout_check) < commands.index(build)


@pytest.mark.parametrize("mutation", ("delete", "move-after-build", "wrong-sha"))
def test_workflow_verifier_rejects_release_checkout_guard_bypass(
    tmp_path: Path,
    mutation: str,
) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    release_path = workflows / "release-draft.yml"
    document = _load_yaml(release_path)
    steps = document["jobs"]["verify"]["steps"]
    guard = next(step for step in steps if "verify_release_checkout.py" in str(step.get("run", "")))
    if mutation == "delete":
        steps.remove(guard)
    elif mutation == "move-after-build":
        steps.remove(guard)
        build_index = next(
            index
            for index, step in enumerate(steps)
            if "uv build --wheel --sdist" in str(step.get("run", ""))
        )
        steps.insert(build_index + 1, guard)
    else:
        guard["run"] = str(guard["run"]).replace("$GITHUB_SHA", "$GITHUB_REF_NAME")
    _write_yaml(release_path, document)

    with pytest.raises(verifier.WorkflowPolicyError, match="run_command_invalid"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


def test_release_tag_must_exactly_match_the_project_version(tmp_path: Path) -> None:
    verifier = _load_verifier()
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text('[project]\nname = "demo"\nversion = "0.2.1"\n', encoding="utf-8")

    verifier.verify_release_tag("v0.2.1", project_file=project_file)
    for invalid in ("0.2.1", "v0.2.2", "v0.2.1-rc1", "v0.2.1\nunsafe"):
        with pytest.raises(verifier.WorkflowPolicyError, match="release_tag"):
            verifier.verify_release_tag(invalid, project_file=project_file)


@pytest.mark.parametrize(
    ("workflow_name", "old", "new", "message"),
    (
        (
            "ci.yml",
            "actions/checkout@de0fac2e4500dabe0009e67214ff5f5447ce83dd",
            "actions/checkout@v6",
            "action_not_pinned",
        ),
        (
            "ci.yml",
            "permissions:\n  contents: read",
            "permissions:\n  contents: write",
            "permissions",
        ),
        ("ci.yml", "uv lock --check", "git push", "forbidden_command"),
        (
            "ci.yml",
            "uv lock --check",
            "git -c http.extraHeader=x push",
            "forbidden_command",
        ),
        (
            "ci.yml",
            "uv lock --check",
            "gh api -X PATCH /repos/example/repository -f private=false",
            "forbidden_command",
        ),
        ("ci.yml", "runs-on: macos-14", "runs-on: windows-latest", "windows_runner"),
        ("release-draft.yml", '- "v*"', '- "release-*"', "release_trigger"),
    ),
)
def test_verifier_rejects_unsafe_workflow_mutations(
    tmp_path: Path,
    workflow_name: str,
    old: str,
    new: str,
    message: str,
) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    workflow = workflows / workflow_name
    document = workflow.read_text(encoding="utf-8")
    assert old in document
    workflow.write_text(document.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(verifier.WorkflowPolicyError, match=message):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


def test_verifier_rejects_any_secret_scanning_exclusion(tmp_path: Path) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    secret_scanning.write_text("paths-ignore:\n  - tests/**\n", encoding="utf-8")

    with pytest.raises(verifier.WorkflowPolicyError, match="secret_scanning"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


def test_verifier_does_not_follow_a_workflow_symlink(tmp_path: Path) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    outside = tmp_path / "outside.yml"
    outside.write_bytes(b"\xff\xfeprivate")
    candidate = workflows / "ci.yml"
    candidate.unlink()
    candidate.symlink_to(outside)

    with pytest.raises(verifier.WorkflowPolicyError, match="policy_file_not_regular"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


def test_release_verification_cannot_be_deferred_until_after_draft_creation(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    release_path = workflows / "release-draft.yml"
    release = _load_yaml(release_path)
    steps = release["jobs"]["verify"]["steps"]
    checksum_step = next(
        step for step in steps if "create_checksums.py" in str(step.get("run", ""))
    )
    checksum_step["run"] = str(checksum_step["run"]).replace(
        "uv run python scripts/create_checksums.py release-dist",
        "true",
    )
    release_step = release["jobs"]["draft"]["steps"][-1]
    release_step["run"] = (
        str(release_step["run"]) + "\nuv run python scripts/create_checksums.py release-dist"
    )
    release_path.write_text(yaml.safe_dump(release, sort_keys=False), encoding="utf-8")

    with pytest.raises(
        verifier.WorkflowPolicyError,
        match=r"required_command_missing.*create_checksums\.py|release_order|run_command_invalid",
    ):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


@pytest.mark.parametrize(
    ("scope", "key", "value"),
    (
        ("workflow", "if", "always()"),
        ("workflow", "defaults", {"run": {"shell": "bash"}}),
        ("workflow", "env", {"TOKEN": "unsafe"}),
        ("job", "if", "always()"),
        ("job", "defaults", {"run": {"working-directory": "/tmp"}}),
        ("job", "container", "python:latest"),
        ("job", "env", {"HOME": "/tmp"}),
        ("step", "if", "always()"),
        ("step", "continue-on-error", False),
        ("step", "working-directory", "/tmp"),
        ("step", "shell", "bash --noprofile --norc -e {0}"),
        ("step", "env", {"UV_INDEX_URL": "https://example.invalid"}),
    ),
)
def test_verifier_rejects_control_flow_and_execution_context_overrides(
    tmp_path: Path,
    scope: str,
    key: str,
    value: Any,
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        target: dict[str, Any]
        if scope == "workflow":
            target = document
        else:
            target = next(iter(document["jobs"].values()))
            if scope == "step":
                target = next(step for step in target["steps"] if "run" in step)
        target[key] = value

    _assert_mutated_rejected(
        tmp_path,
        "ci.yml",
        mutate,
        match="workflow_schema|job_schema|step_schema|execution_context",
    )


@pytest.mark.parametrize("continue_value", (False, True))
def test_only_linux_job_may_declare_continue_on_error(
    tmp_path: Path,
    continue_value: bool,
) -> None:
    def mutate(document: dict[str, Any]) -> None:
        next(iter(document["jobs"].values()))["continue-on-error"] = continue_value

    _assert_mutated_rejected(
        tmp_path,
        "ci.yml",
        mutate,
        match="continue_on_error|blocking_job",
    )


@pytest.mark.parametrize(
    "replacement",
    (
        "echo uv lock --check",
        "# uv lock --check",
        "uv lock --check || true",
        "uv lock --check | tee workflow.log",
        "uv lock --check > workflow.log",
        "$(uv lock --check)",
        "xuv lock --check",
        "true uv lock --check",
        "uv lock --check --help",
    ),
)
def test_required_commands_cannot_be_satisfied_by_shell_smuggling(
    tmp_path: Path,
    replacement: str,
) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    ci = workflows / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    assert "uv lock --check" in text
    ci.write_text(text.replace("uv lock --check", replacement), encoding="utf-8")

    with pytest.raises(verifier.WorkflowPolicyError, match="run_command_invalid|required_command"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


@pytest.mark.parametrize(
    "prefix",
    (
        "exit 0",
        "set +e",
        "PATH=/tmp",
        "trap 'exit 0' ERR",
        "uv() {\n  return 0\n}",
        "if false\nthen",
    ),
)
def test_required_commands_reject_unreachable_or_overridden_execution(
    tmp_path: Path,
    prefix: str,
) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    ci = workflows / "ci.yml"
    document = _load_yaml(ci)
    quality = document["jobs"]["quality"]
    synchronization = next(
        step for step in quality["steps"] if "uv lock --check" in str(step.get("run", ""))
    )
    synchronization["run"] = f"{prefix}\n{synchronization['run']}"
    if prefix == "if false\nthen":
        synchronization["run"] += "\nfi"
    ci.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    with pytest.raises(verifier.WorkflowPolicyError, match="run_command_invalid"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


@pytest.mark.parametrize(
    "mutation",
    (
        f'gh release create --draft=false --verify-tag "$GITHUB_REF_NAME" {RELEASE_WHEEL} {RELEASE_SDIST} release-dist/SHA256SUMS --title "Project Memory Hub $GITHUB_REF_NAME" --notes "Verified Public Beta draft. Maintainer review is still required before publication."',
        f'gh release create --draft --draft --verify-tag "$GITHUB_REF_NAME" {RELEASE_WHEEL} {RELEASE_SDIST} release-dist/SHA256SUMS --title "Project Memory Hub $GITHUB_REF_NAME" --notes "Verified Public Beta draft. Maintainer review is still required before publication."',
        f'gh release create --draft --verify-tag --repo owner/repository "$GITHUB_REF_NAME" {RELEASE_WHEEL} {RELEASE_SDIST} release-dist/SHA256SUMS --title "Project Memory Hub $GITHUB_REF_NAME" --notes "Verified Public Beta draft. Maintainer review is still required before publication."',
        f'gh release create --draft --verify-tag "$GITHUB_REF_NAME" {RELEASE_WHEEL} {RELEASE_SDIST} release-dist/SHA256SUMS --title "Project Memory Hub $GITHUB_REF_NAME" --notes "Verified Public Beta draft. Maintainer review is still required before publication." | tee release.log',
        f'gh release create --draft --verify-tag "$GITHUB_REF_NAME" {RELEASE_WHEEL} {RELEASE_SDIST} release-dist/SHA256SUMS --title "Project Memory Hub $GITHUB_REF_NAME" --notes "Verified Public Beta draft. Maintainer review is still required before publication." > release.log',
    ),
)
def test_draft_release_command_requires_exact_safe_argv(tmp_path: Path, mutation: str) -> None:
    def mutate(document: dict[str, Any]) -> None:
        _job_with_release_step(document)["steps"][-1]["run"] = mutation

    _assert_mutated_rejected(
        tmp_path,
        "release-draft.yml",
        mutate,
        match="release_command|run_command_invalid|release_order",
    )


def test_draft_release_step_rejects_any_extra_environment(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        _job_with_release_step(document)["steps"][-1]["env"]["EXTRA"] = "unsafe"

    _assert_mutated_rejected(
        tmp_path,
        "release-draft.yml",
        mutate,
        match="execution_context|release_environment",
    )


def test_draft_release_step_requires_explicit_repository_context(tmp_path: Path) -> None:
    def mutate(document: dict[str, Any]) -> None:
        del _job_with_release_step(document)["steps"][-1]["env"]["GH_REPO"]

    _assert_mutated_rejected(
        tmp_path,
        "release-draft.yml",
        mutate,
        match="execution_context|release_environment",
    )


@pytest.mark.parametrize("mutation", ("languages", "category", "order"))
def test_codeql_inputs_and_order_are_exact(tmp_path: Path, mutation: str) -> None:
    def mutate(document: dict[str, Any]) -> None:
        steps = next(iter(document["jobs"].values()))["steps"]
        if mutation == "languages":
            steps[1]["with"]["languages"] = "python"
        elif mutation == "category":
            steps[2]["with"]["category"] = "/language:python"
        else:
            steps[1], steps[2] = steps[2], steps[1]

    _assert_mutated_rejected(tmp_path, "codeql.yml", mutate, match="codeql")


def test_duplicate_yaml_keys_fail_closed(tmp_path: Path) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    ci = workflows / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    duplicate = "permissions:\n  contents: read\n\npermissions:\n  contents: read"
    ci.write_text(text.replace("permissions:\n  contents: read", duplicate, 1), encoding="utf-8")

    with pytest.raises(
        verifier.WorkflowPolicyError, match="duplicate_yaml_key|policy_yaml_invalid"
    ):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


@pytest.mark.parametrize(
    ("workflow_name", "old", "new", "message"),
    (
        ("ci.yml", "runs-on: macos-14", "runs-on: macos-latest", "runner_invalid"),
        (
            "linux-experimental.yml",
            "runs-on: ubuntu-24.04",
            "runs-on: ubuntu-latest",
            "runner_invalid",
        ),
        ("codeql.yml", "runs-on: ubuntu-24.04", "runs-on: ubuntu-latest", "runner_invalid"),
        (
            "ci.yml",
            "persist-credentials: false",
            "persist-credentials: false\n          fetch-depth: 0",
            "checkout_credentials",
        ),
    ),
)
def test_runner_and_checkout_inputs_are_exact(
    tmp_path: Path,
    workflow_name: str,
    old: str,
    new: str,
    message: str,
) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    workflow = workflows / workflow_name
    text = workflow.read_text(encoding="utf-8")
    assert old in text
    workflow.write_text(text.replace(old, new, 1), encoding="utf-8")

    with pytest.raises(verifier.WorkflowPolicyError, match=message):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


@pytest.mark.parametrize("limit", (0, -1, False, "5"))
def test_dependabot_schema_requires_positive_integer_limits(tmp_path: Path, limit: Any) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    dependabot = workflows.parent / "dependabot.yml"
    document = _load_yaml(dependabot)
    document["updates"][0]["open-pull-requests-limit"] = limit
    _write_yaml(dependabot, document)

    with pytest.raises(verifier.WorkflowPolicyError, match="dependabot"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


@pytest.mark.parametrize("scope", ("root", "entry", "schedule"))
def test_dependabot_rejects_unknown_schema_keys(tmp_path: Path, scope: str) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    dependabot = workflows.parent / "dependabot.yml"
    document = _load_yaml(dependabot)
    target = (
        document
        if scope == "root"
        else document["updates"][0]
        if scope == "entry"
        else document["updates"][0]["schedule"]
    )
    target["unexpected"] = True
    _write_yaml(dependabot, document)

    with pytest.raises(verifier.WorkflowPolicyError, match="dependabot"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)


@pytest.mark.parametrize("policy_name", ("dependabot.yml", "secret_scanning.yml"))
def test_every_policy_yaml_rejects_duplicate_keys(tmp_path: Path, policy_name: str) -> None:
    verifier = _load_verifier()
    workflows, secret_scanning = _copy_policy(tmp_path)
    path = workflows.parent / policy_name
    text = path.read_text(encoding="utf-8")
    if policy_name == "dependabot.yml":
        text = text.replace("version: 2", "version: 2\nversion: 2", 1)
    else:
        text = text.replace("paths-ignore: []", "paths-ignore: []\npaths-ignore: []", 1)
    path.write_text(text, encoding="utf-8")

    with pytest.raises(verifier.WorkflowPolicyError, match="duplicate_yaml_key"):
        verifier.verify_workflows(workflows, secret_scanning=secret_scanning)
