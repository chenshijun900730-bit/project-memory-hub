from __future__ import annotations

import argparse
import os
import re
import shlex
import stat
import tomllib
from pathlib import Path
from typing import Any, NoReturn, cast

import yaml  # type: ignore[import-untyped]


EXPECTED_WORKFLOWS = frozenset(
    {
        "ci.yml",
        "linux-experimental.yml",
        "codeql.yml",
        "release-draft.yml",
    }
)
PINNED_ACTIONS = {
    "actions/checkout": "de0fac2e4500dabe0009e67214ff5f5447ce83dd",  # v6.0.2
    "actions/setup-python": "a309ff8b426b58ec0e2a45f0f869d46889d02405",  # v6.2.0
    "actions/upload-artifact": "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a",  # v7.0.1
    "actions/download-artifact": "3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c",  # v8.0.1
    "astral-sh/setup-uv": "08807647e7069bb48b6ef5acd8ec9567f424441b",  # v8.1.0
    "github/codeql-action": "7211b7c8077ea37d8641b6271f6a365a22a5fbfa",  # v4.36.0
}
ALLOWED_ACTIONS = frozenset(
    {
        "actions/checkout",
        "actions/setup-python",
        "actions/upload-artifact",
        "actions/download-artifact",
        "astral-sh/setup-uv",
        "github/codeql-action/init",
        "github/codeql-action/analyze",
    }
)
MAX_POLICY_FILE_BYTES = 256 * 1024

_WORKFLOW_KEYS = frozenset({"name", "on", "permissions", "jobs"})
_JOB_KEYS = frozenset(
    {
        "name",
        "runs-on",
        "timeout-minutes",
        "steps",
        "needs",
        "permissions",
        "continue-on-error",
        "strategy",
    }
)
_STEP_KEYS = frozenset({"name", "id", "uses", "with", "run", "env"})
_EXPECTED_JOB_CONTRACTS = {
    "ci.yml": {
        "quality": ("macos-14", 45),
        "artifact-smoke": ("macos-14", 30),
    },
    "linux-experimental.yml": {"linux-experimental": ("ubuntu-24.04", 45)},
    "codeql.yml": {"analyze": ("ubuntu-24.04", 30)},
    "release-draft.yml": {
        "verify": ("macos-14", 60),
        "draft": ("macos-14", 15),
    },
}
_SETUP_PYTHON = "actions/setup-python"
_RELEASE_TITLE = "Project Memory Hub $GITHUB_REF_NAME"
_RELEASE_NOTES = (
    "Verified Public Beta draft. Maintainer review is still required before publication."
)
_RELEASE_WHEEL = "release-dist/project_memory_hub-0.2.1-py3-none-any.whl"
_RELEASE_SDIST = "release-dist/project_memory_hub-0.2.1.tar.gz"

_FORBIDDEN_COMMANDS = (
    (
        "forbidden_command:remote_add",
        re.compile(r"\bgit\b[^\r\n;&|]{0,200}\bremote\s+add\b", re.IGNORECASE),
    ),
    (
        "forbidden_command:push",
        re.compile(r"\bgit\b[^\r\n;&|]{0,200}\bpush\b", re.IGNORECASE),
    ),
    ("forbidden_command:twine_upload", re.compile(r"\btwine\s+upload\b", re.IGNORECASE)),
    ("forbidden_command:uv_publish", re.compile(r"\buv\s+publish\b", re.IGNORECASE)),
    ("forbidden_command:pypi", re.compile(r"\bpypi\b", re.IGNORECASE)),
    (
        "forbidden_command:oidc_publish",
        re.compile(r"(?:trusted[ -]?publish|pypa/gh-action-pypi-publish|\boidc\b)", re.IGNORECASE),
    ),
    ("forbidden_command:repo_mutation", re.compile(r"\bgh\s+repo\b", re.IGNORECASE)),
    ("forbidden_command:github_api", re.compile(r"\bgh\s+api\b", re.IGNORECASE)),
    (
        "forbidden_command:github_api",
        re.compile(r"api\.github\.com/repos(?:/|\b)", re.IGNORECASE),
    ),
    (
        "forbidden_command:visibility",
        re.compile(r"(?:--visibility\b|\bvisibility\s*[=:])", re.IGNORECASE),
    ),
)


class WorkflowPolicyError(RuntimeError):
    """Raised when a repository automation file violates the release policy."""


class _WorkflowLoader(yaml.SafeLoader):  # type: ignore[misc]
    """Use YAML 1.2-style booleans so the workflow key ``on`` stays a string."""


_WorkflowLoader.yaml_implicit_resolvers = {
    key: list(resolvers) for key, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
for _resolver_key, _resolvers in tuple(_WorkflowLoader.yaml_implicit_resolvers.items()):
    _WorkflowLoader.yaml_implicit_resolvers[_resolver_key] = [
        resolver for resolver in _resolvers if resolver[0] != "tag:yaml.org,2002:bool"
    ]
_WorkflowLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|false)$", flags=re.IGNORECASE),
    list("tTfF"),
)


class _DuplicateYamlKeyError(yaml.YAMLError):  # type: ignore[misc]
    """Raised before a duplicate mapping key can overwrite audited policy."""


def _construct_unique_mapping(
    loader: _WorkflowLoader,
    node: yaml.nodes.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in mapping
        except TypeError as error:
            raise _DuplicateYamlKeyError("unhashable YAML mapping key") from error
        if duplicate:
            raise _DuplicateYamlKeyError(f"duplicate YAML mapping key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_WorkflowLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _fail(code: str, path: Path, detail: str = "") -> NoReturn:
    suffix = f": {detail}" if detail else ""
    raise WorkflowPolicyError(f"{code}: {path.as_posix()}{suffix}")


def _read_policy_text(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise WorkflowPolicyError(f"policy_file_missing: {path.as_posix()}") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        _fail("policy_file_not_regular", path)
    if metadata.st_size > MAX_POLICY_FILE_BYTES:
        _fail("policy_file_too_large", path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise WorkflowPolicyError(f"policy_file_not_regular: {path.as_posix()}") from error
    try:
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            _fail("policy_file_not_regular", path)
        with os.fdopen(descriptor, encoding="utf-8", errors="strict") as file:
            descriptor = -1
            text = file.read(MAX_POLICY_FILE_BYTES + 1)
    except (OSError, UnicodeError) as error:
        raise WorkflowPolicyError(f"policy_yaml_invalid: {path.as_posix()}") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(text.encode("utf-8")) > MAX_POLICY_FILE_BYTES:
        _fail("policy_file_too_large", path)
    return text


def _parse_yaml(text: str, path: Path) -> dict[str, Any]:
    try:
        document = yaml.load(text, Loader=_WorkflowLoader)
    except _DuplicateYamlKeyError as error:
        raise WorkflowPolicyError(f"duplicate_yaml_key: {path.as_posix()}") from error
    except yaml.YAMLError as error:
        raise WorkflowPolicyError(f"policy_yaml_invalid: {path.as_posix()}") from error
    if type(document) is not dict:
        _fail("policy_yaml_invalid", path)
    return cast(dict[str, Any], document)


def _load_yaml(path: Path) -> dict[str, Any]:
    return _parse_yaml(_read_policy_text(path), path)


def _mapping(value: Any, *, code: str, path: Path) -> dict[str, Any]:
    if type(value) is not dict:
        _fail(code, path)
    return cast(dict[str, Any], value)


def _require_exact_keys(
    mapping: dict[str, Any],
    allowed: frozenset[str],
    *,
    code: str,
    path: Path,
) -> None:
    if any(not isinstance(key, str) for key in mapping) or not set(mapping) <= allowed:
        _fail(code, path)


def _verify_schema(document: dict[str, Any], path: Path) -> None:
    _require_exact_keys(document, _WORKFLOW_KEYS, code="workflow_schema", path=path)
    if set(document) != _WORKFLOW_KEYS or not isinstance(document.get("name"), str):
        _fail("workflow_schema", path)
    for job_id, raw_job in _jobs(document, path).items():
        if not isinstance(job_id, str) or not job_id:
            _fail("job_schema", path)
        job = _mapping(raw_job, code="job_schema", path=path)
        _require_exact_keys(job, _JOB_KEYS, code="job_schema", path=path)
        required_job_keys = {"name", "runs-on", "timeout-minutes", "steps"}
        if not required_job_keys <= set(job) or not isinstance(job.get("name"), str):
            _fail("job_schema", path, job_id)
        timeout = job.get("timeout-minutes")
        if type(timeout) is not int or timeout <= 0:
            _fail("job_schema", path, job_id)
        for step in _steps(job, path):
            _require_exact_keys(step, _STEP_KEYS, code="step_schema", path=path)
            if not isinstance(step.get("name"), str) or not step["name"]:
                _fail("step_schema", path, job_id)
            has_run = "run" in step
            has_uses = "uses" in step
            if has_run == has_uses:
                _fail("step_schema", path, job_id)
            if has_run and (not isinstance(step["run"], str) or "with" in step or "id" in step):
                _fail("step_schema", path, job_id)
            if has_uses and ("env" in step or not isinstance(step["uses"], str)):
                _fail("step_schema", path, job_id)
            if "with" in step and type(step["with"]) is not dict:
                _fail("step_schema", path, job_id)
            if "id" in step and (
                not isinstance(step["id"], str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", step["id"])
            ):
                _fail("step_schema", path, job_id)


def _logical_run_commands(run: str, path: Path) -> list[tuple[str, ...]]:
    commands: list[tuple[str, ...]] = []
    continued = ""
    for raw_line in run.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.endswith("\\"):
            continued += stripped[:-1].rstrip() + " "
            continue
        line = (continued + stripped).strip()
        continued = ""
        if line.startswith("#") or re.match(r"^echo(?:\s|$)", line):
            _fail("run_command_invalid", path)
        if any(marker in line for marker in ("||", "&&", "|", ";", ">", "<", "`")):
            _fail("run_command_invalid", path)
        assignment = re.fullmatch(
            r'python_(311|312)="\$\(uv python find --system (3\.(?:11|12))\)"',
            line,
        )
        if assignment is not None:
            suffix, version = assignment.groups()
            if suffix != version.replace(".", ""):
                _fail("run_command_invalid", path)
            commands.append(("uv", "python", "find", "--system", version))
            continue
        if "$(" in line:
            _fail("run_command_invalid", path)
        try:
            argv = tuple(shlex.split(line, posix=True))
        except ValueError as error:
            raise WorkflowPolicyError(f"run_command_invalid: {path.as_posix()}") from error
        if not argv or argv[0] in {"echo", "#"}:
            _fail("run_command_invalid", path)
        commands.append(argv)
    if continued:
        _fail("run_command_invalid", path)
    return commands


def _job_commands(job: dict[str, Any], path: Path) -> list[tuple[str, ...]]:
    return [
        command
        for step in _steps(job, path)
        if "run" in step
        for command in _logical_run_commands(cast(str, step["run"]), path)
    ]


def _jobs(document: dict[str, Any], path: Path) -> dict[str, Any]:
    jobs = _mapping(document.get("jobs"), code="jobs_invalid", path=path)
    if not jobs:
        _fail("jobs_invalid", path)
    return jobs


def _steps(job: dict[str, Any], path: Path) -> list[dict[str, Any]]:
    raw_steps = job.get("steps")
    if type(raw_steps) is not list or not raw_steps:
        _fail("steps_invalid", path)
    steps: list[dict[str, Any]] = []
    for step in raw_steps:
        if type(step) is not dict:
            _fail("steps_invalid", path)
        steps.append(step)
    return steps


def _require_exact_commands(
    commands: list[tuple[str, ...]],
    expected: tuple[str, ...],
    path: Path,
) -> None:
    try:
        expected_commands = [tuple(shlex.split(marker, posix=True)) for marker in expected]
    except ValueError as error:
        raise WorkflowPolicyError(f"required_command_invalid: {path.as_posix()}") from error
    if commands != expected_commands:
        _fail("run_command_invalid", path)


def _verify_forbidden_content(text: str, path: Path) -> None:
    if re.search(r"\$\{\{\s*secrets\.", text, flags=re.IGNORECASE):
        _fail("forbidden_command:repository_secret", path)
    if re.search(r"\bid-token\s*:", text, flags=re.IGNORECASE):
        _fail("permissions:id_token", path)
    for code, pattern in _FORBIDDEN_COMMANDS:
        if pattern.search(text):
            _fail(code, path)
    if path.name != "release-draft.yml" and re.search(
        r"\bgh\s+release\b", text, flags=re.IGNORECASE
    ):
        _fail("forbidden_command:release", path)


def _verify_actions(document: dict[str, Any], path: Path) -> None:
    for job in _jobs(document, path).values():
        job_mapping = _mapping(job, code="job_invalid", path=path)
        for step in _steps(job_mapping, path):
            use = step.get("uses")
            if use is None:
                continue
            if not isinstance(use, str) or use.count("@") != 1:
                _fail("action_not_pinned", path)
            action, revision = use.split("@", maxsplit=1)
            root = "/".join(action.split("/")[:2])
            expected = PINNED_ACTIONS.get(root)
            if action not in ALLOWED_ACTIONS or expected is None:
                _fail("action_not_allowed", path, action)
            if not re.fullmatch(r"[0-9a-f]{40}", revision) or revision != expected:
                _fail("action_not_pinned", path, action)
            inputs = step.get("with")
            if action == "actions/checkout":
                if inputs != {"persist-credentials": False}:
                    _fail("checkout_credentials", path)
            elif root == _SETUP_PYTHON:
                if type(inputs) is not dict or set(inputs) != {"python-version"}:
                    _fail("setup_python", path)
                if inputs["python-version"] not in {"3.11", "3.12"}:
                    _fail("setup_python", path)
            elif root == "astral-sh/setup-uv":
                if inputs != {"version": "0.11.28", "enable-cache": False}:
                    _fail("setup_uv", path)
            if root in {"actions/upload-artifact", "actions/download-artifact"} and path.name != (
                "release-draft.yml"
            ):
                _fail("action_not_allowed", path, action)
            if root == "github/codeql-action" and path.name != "codeql.yml":
                _fail("action_not_allowed", path, action)
            if "id" in step and root != _SETUP_PYTHON:
                _fail("step_schema", path)


def _verify_permissions(document: dict[str, Any], path: Path) -> None:
    expected = (
        {"contents": "read", "security-events": "write"}
        if path.name == "codeql.yml"
        else {"contents": "read"}
    )
    if document.get("permissions") != expected:
        _fail("permissions", path)
    jobs = _jobs(document, path)
    if path.name == "release-draft.yml":
        if _mapping(jobs.get("verify"), code="job_invalid", path=path).get("permissions") != {
            "contents": "read"
        }:
            _fail("permissions", path, "verify")
        if _mapping(jobs.get("draft"), code="job_invalid", path=path).get("permissions") != {
            "contents": "write"
        }:
            _fail("permissions", path, "draft")
    elif any(
        "permissions" in _mapping(job, code="job_invalid", path=path) for job in jobs.values()
    ):
        _fail("permissions:job_override", path)


def _verify_trigger(document: dict[str, Any], path: Path) -> None:
    expected = (
        {"push": {"tags": ["v*"]}}
        if path.name == "release-draft.yml"
        else {"push": None, "pull_request": None}
    )
    if document.get("on") != expected:
        code = "release_trigger" if path.name == "release-draft.yml" else "workflow_trigger"
        _fail(code, path)


def _verify_runners(document: dict[str, Any], path: Path) -> None:
    expected_contracts = _EXPECTED_JOB_CONTRACTS[path.name]
    jobs = _jobs(document, path)
    if set(jobs) != set(expected_contracts):
        _fail("jobs_invalid", path)
    for job_id, raw_job in _jobs(document, path).items():
        job = _mapping(raw_job, code="job_invalid", path=path)
        runner = job.get("runs-on")
        if not isinstance(runner, str):
            _fail("runner_invalid", path, str(job_id))
        if runner.casefold().startswith("windows-"):
            _fail("windows_runner", path, str(job_id))
        expected_runner, expected_timeout = expected_contracts[str(job_id)]
        if runner != expected_runner:
            _fail("runner_invalid", path, str(job_id))
        if job.get("timeout-minutes") != expected_timeout:
            _fail("job_schema", path, str(job_id))


def _verify_execution_context(document: dict[str, Any], path: Path) -> None:
    for job_id, raw_job in _jobs(document, path).items():
        job = _mapping(raw_job, code="job_invalid", path=path)
        if path.name == "linux-experimental.yml" and job_id == "linux-experimental":
            if job.get("continue-on-error") is not True:
                _fail("linux_experimental_blocking", path)
        elif "continue-on-error" in job:
            _fail("continue_on_error", path, str(job_id))
        if "strategy" in job and not (path.name == "codeql.yml" and job_id == "analyze"):
            _fail("job_schema", path, str(job_id))
        expected_needs = (
            "quality"
            if path.name == "ci.yml" and job_id == "artifact-smoke"
            else "verify"
            if path.name == "release-draft.yml" and job_id == "draft"
            else None
        )
        if job.get("needs") != expected_needs or (expected_needs is None and "needs" in job):
            _fail("job_schema", path, str(job_id))
        for index, step in enumerate(_steps(job, path)):
            if "env" not in step:
                continue
            is_release_step = (
                path.name == "release-draft.yml"
                and job_id == "draft"
                and index == len(_steps(job, path)) - 1
            )
            expected_release_environment = {
                "GH_TOKEN": "${{ github.token }}",
                "GH_REPO": "${{ github.repository }}",
            }
            if not is_release_step or step["env"] != expected_release_environment:
                _fail("execution_context", path, str(job_id))


def _verify_setup_python(document: dict[str, Any], path: Path) -> None:
    expected_by_job: dict[str, list[tuple[str | None, str]]] = {
        "quality": [(None, "3.11")],
        "artifact-smoke": [("python-311", "3.11"), ("python-312", "3.12")],
        "linux-experimental": [(None, "3.11")],
        "analyze": [],
        "verify": [("python-311", "3.11"), ("python-312", "3.12")],
        "draft": [],
    }
    expected_setup_uv = {
        "quality": 1,
        "artifact-smoke": 1,
        "linux-experimental": 1,
        "analyze": 0,
        "verify": 1,
        "draft": 0,
    }
    expected_checkout = {
        "quality": 1,
        "artifact-smoke": 1,
        "linux-experimental": 1,
        "analyze": 1,
        "verify": 1,
        "draft": 0,
    }
    for job_id, raw_job in _jobs(document, path).items():
        job = _mapping(raw_job, code="job_invalid", path=path)
        setup_steps: list[tuple[str | None, str]] = []
        setup_uv_count = 0
        checkout_count = 0
        for step in _steps(job, path):
            use = str(step.get("uses", ""))
            action = use.partition("@")[0]
            if action == _SETUP_PYTHON:
                inputs = _mapping(step.get("with"), code="setup_python", path=path)
                setup_steps.append(
                    (cast(str | None, step.get("id")), cast(str, inputs["python-version"]))
                )
            elif action == "astral-sh/setup-uv":
                setup_uv_count += 1
            elif action == "actions/checkout":
                checkout_count += 1
        if setup_steps != expected_by_job[str(job_id)]:
            _fail("setup_python", path, str(job_id))
        if setup_uv_count != expected_setup_uv[str(job_id)]:
            _fail("setup_uv", path, str(job_id))
        if checkout_count != expected_checkout[str(job_id)]:
            _fail("checkout_credentials", path, str(job_id))


def _verify_ci(document: dict[str, Any], path: Path) -> None:
    jobs = _jobs(document, path)
    quality_commands = _job_commands(
        _mapping(jobs["quality"], code="job_invalid", path=path),
        path,
    )
    _require_exact_commands(
        quality_commands,
        (
            "uv lock --check",
            "uv sync --locked --extra test",
            "uv run playwright install chromium",
            "uv run ruff format --check .",
            "uv run ruff check .",
            "uv run mypy src/project_memory_hub",
            "uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85",
            "node --check src/project_memory_hub/web/static/i18n.js",
            "node --check src/project_memory_hub/web/static/projects.js",
            "node --check src/project_memory_hub/web/static/sources.js",
            "uv run pytest tests/e2e -q",
            "test ! -e release-dist",
            "uv build --wheel --sdist --out-dir release-dist",
            "uv run twine check release-dist/*",
            "uv run python scripts/verify_wheel.py",
            "uv run python scripts/verify_public_assets.py docs/assets",
            "uv run python scripts/verify_document_links.py",
            "uv run python scripts/verify_workflows.py .github/workflows "
            "--secret-scanning .github/secret_scanning.yml",
        ),
        path,
    )
    smoke_commands = _job_commands(
        _mapping(jobs["artifact-smoke"], code="job_invalid", path=path),
        path,
    )
    _require_exact_commands(
        smoke_commands,
        (
            "uv lock --check",
            "uv sync --locked --extra test",
            "test ! -e release-dist",
            "uv build --wheel --sdist --out-dir release-dist",
            "uv run twine check release-dist/*",
            "uv python find --system 3.11",
            "uv python find --system 3.12",
            "uv run python scripts/verify_release_artifacts.py --dist release-dist "
            "--smoke-python $python_311 --smoke-python $python_312",
        ),
        path,
    )


def _verify_linux_experimental(document: dict[str, Any], path: Path) -> None:
    if "experimental" not in str(document.get("name", "")).casefold():
        _fail("linux_experimental_name", path)
    for job_id, raw_job in _jobs(document, path).items():
        job = _mapping(raw_job, code="job_invalid", path=path)
        if "experimental" not in f"{job_id} {job.get('name', '')}".casefold():
            _fail("linux_experimental_name", path)
    commands = _job_commands(
        _mapping(_jobs(document, path)["linux-experimental"], code="job_invalid", path=path),
        path,
    )
    _require_exact_commands(
        commands,
        (
            "uv lock --check",
            "uv sync --locked --extra test",
            "uv run playwright install --with-deps chromium",
            "uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85",
            "node --check src/project_memory_hub/web/static/i18n.js",
            "node --check src/project_memory_hub/web/static/projects.js",
            "node --check src/project_memory_hub/web/static/sources.js",
            "uv run pytest tests/e2e -q",
            "uv run python scripts/verify_public_assets.py docs/assets",
        ),
        path,
    )


def _verify_codeql(document: dict[str, Any], path: Path) -> None:
    jobs = _jobs(document, path)
    if set(jobs) != {"analyze"}:
        _fail("codeql_jobs", path)
    job = _mapping(jobs["analyze"], code="job_invalid", path=path)
    if job.get("strategy") != {
        "fail-fast": False,
        "matrix": {"language": ["python", "javascript-typescript"]},
    }:
        _fail("codeql_languages", path)
    steps = _steps(job, path)
    expected_uses = (
        f"actions/checkout@{PINNED_ACTIONS['actions/checkout']}",
        f"github/codeql-action/init@{PINNED_ACTIONS['github/codeql-action']}",
        f"github/codeql-action/analyze@{PINNED_ACTIONS['github/codeql-action']}",
    )
    if tuple(step.get("uses") for step in steps) != expected_uses:
        _fail("codeql_action_order", path)
    if steps[1].get("with") != {"languages": "${{ matrix.language }}"}:
        _fail("codeql_languages", path)
    if steps[2].get("with") != {"category": "/language:${{ matrix.language }}"}:
        _fail("codeql_category", path)


def _verify_release(document: dict[str, Any], path: Path) -> None:
    jobs = _jobs(document, path)
    if set(jobs) != {"verify", "draft"}:
        _fail("release_jobs", path)
    verify_job = _mapping(jobs["verify"], code="job_invalid", path=path)
    draft_job = _mapping(jobs["draft"], code="job_invalid", path=path)
    verify_steps = _steps(verify_job, path)
    verification_commands = _job_commands(verify_job, path)
    _require_exact_commands(
        verification_commands,
        (
            "uv lock --check",
            "uv sync --locked --extra test",
            "uv run python scripts/verify_workflows.py .github/workflows "
            "--secret-scanning .github/secret_scanning.yml --tag $GITHUB_REF_NAME "
            "--project-file pyproject.toml",
            "uv run playwright install chromium",
            "uv run ruff format --check .",
            "uv run ruff check .",
            "uv run mypy src/project_memory_hub",
            "uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85",
            "node --check src/project_memory_hub/web/static/i18n.js",
            "node --check src/project_memory_hub/web/static/projects.js",
            "node --check src/project_memory_hub/web/static/sources.js",
            "uv run pytest tests/e2e -q",
            "uv run python scripts/verify_public_assets.py docs/assets",
            "uv run python scripts/verify_document_links.py",
            "git diff --quiet --exit-code HEAD --",
            "uv run python scripts/verify_release_checkout.py --expected-head $GITHUB_SHA",
            "test ! -e release-dist",
            "uv build --wheel --sdist --out-dir release-dist",
            "uv run twine check release-dist/*",
            "uv python find --system 3.11",
            "uv python find --system 3.12",
            "uv run python scripts/verify_release_artifacts.py --dist release-dist "
            "--smoke-python $python_311 --smoke-python $python_312",
            "uv run python scripts/create_checksums.py release-dist",
            "cd release-dist",
            "shasum -a 256 -c SHA256SUMS",
        ),
        path,
    )
    if any(command[:2] == ("gh", "release") for command in verification_commands):
        _fail("release_order", path)

    upload_steps = [
        step
        for step in verify_steps
        if str(step.get("uses", "")).startswith("actions/upload-artifact@")
    ]
    if len(upload_steps) != 1 or verify_steps[-1] is not upload_steps[0]:
        _fail("release_artifact_handoff", path)
    upload_inputs = _mapping(
        upload_steps[0].get("with"), code="release_artifact_handoff", path=path
    )
    if set(upload_inputs) != {"name", "path", "if-no-files-found", "retention-days"} or (
        upload_inputs.get("name") != "release-dist"
        or str(upload_inputs.get("path", "")).splitlines()
        != [_RELEASE_WHEEL, _RELEASE_SDIST, "release-dist/SHA256SUMS"]
        or upload_inputs.get("if-no-files-found") != "error"
        or upload_inputs.get("retention-days") != 1
    ):
        _fail("release_artifact_handoff", path)

    draft_steps = _steps(draft_job, path)
    if len(draft_steps) != 2:
        _fail("release_jobs", path)
    download_step, release_step = draft_steps
    expected_download = f"actions/download-artifact@{PINNED_ACTIONS['actions/download-artifact']}"
    if download_step.get("uses") != expected_download or download_step.get("with") != {
        "name": "release-dist",
        "path": "release-dist",
    }:
        _fail("release_artifact_handoff", path)
    if release_step.get("env") != {
        "GH_TOKEN": "${{ github.token }}",
        "GH_REPO": "${{ github.repository }}",
    }:
        _fail("release_environment", path)
    release_commands = _logical_run_commands(cast(str, release_step.get("run")), path)
    expected_release_command = (
        "gh",
        "release",
        "create",
        "--draft",
        "--verify-tag",
        "$GITHUB_REF_NAME",
        _RELEASE_WHEEL,
        _RELEASE_SDIST,
        "release-dist/SHA256SUMS",
        "--title",
        _RELEASE_TITLE,
        "--notes",
        _RELEASE_NOTES,
    )
    if release_commands != [expected_release_command]:
        _fail("release_command", path)


def _verify_dependabot(path: Path) -> None:
    document = _load_yaml(path)
    if set(document) != {"version", "updates"}:
        _fail("dependabot_schema", path)
    if type(document.get("version")) is not int or document["version"] != 2:
        _fail("dependabot_version", path)
    updates = document.get("updates")
    if type(updates) is not list or len(updates) != 2:
        _fail("dependabot_ecosystems", path)
    ecosystems: set[str] = set()
    for raw_entry in updates:
        entry = _mapping(raw_entry, code="dependabot_entry", path=path)
        if set(entry) != {
            "package-ecosystem",
            "directory",
            "schedule",
            "open-pull-requests-limit",
        }:
            _fail("dependabot_schema", path)
        ecosystem = entry.get("package-ecosystem")
        if not isinstance(ecosystem, str):
            _fail("dependabot_ecosystems", path)
        ecosystems.add(ecosystem)
        if entry.get("directory") != "/":
            _fail("dependabot_directory", path)
        schedule = _mapping(entry.get("schedule"), code="dependabot_schedule", path=path)
        if schedule != {"interval": "weekly"}:
            _fail("dependabot_schedule", path)
        limit = entry.get("open-pull-requests-limit")
        if type(limit) is not int or limit <= 0:
            _fail("dependabot_limit", path)
    if ecosystems != {"pip", "github-actions"}:
        _fail("dependabot_ecosystems", path)


def _verify_secret_scanning(path: Path) -> None:
    if _load_yaml(path) != {"paths-ignore": []}:
        _fail("secret_scanning", path)


def verify_release_tag(tag: str, *, project_file: Path) -> None:
    try:
        with Path(project_file).open("rb") as file:
            document = tomllib.load(file)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise WorkflowPolicyError("release_tag: project metadata unavailable") from error
    project = document.get("project")
    version = project.get("version") if type(project) is dict else None
    if (
        not isinstance(version, str)
        or tag != f"v{version}"
        or not re.fullmatch(r"v[0-9A-Za-z][0-9A-Za-z.+-]*", tag)
    ):
        raise WorkflowPolicyError("release_tag: tag must exactly match pyproject.toml version")


def verify_workflows(workflows: Path, *, secret_scanning: Path) -> None:
    workflow_directory = Path(workflows)
    try:
        directory_metadata = workflow_directory.lstat()
    except OSError as error:
        raise WorkflowPolicyError("workflow_directory_missing") from error
    if stat.S_ISLNK(directory_metadata.st_mode) or not stat.S_ISDIR(directory_metadata.st_mode):
        raise WorkflowPolicyError("workflow_directory_invalid")

    candidates = {
        path.name: path
        for path in workflow_directory.iterdir()
        if path.suffix.casefold() in {".yml", ".yaml"}
    }
    if set(candidates) != EXPECTED_WORKFLOWS:
        raise WorkflowPolicyError("workflow_inventory: expected the four audited workflows")

    documents: dict[str, dict[str, Any]] = {}
    for name, path in sorted(candidates.items()):
        text = _read_policy_text(path)
        _verify_forbidden_content(text, path)
        document = _parse_yaml(text, path)
        documents[name] = document
        _verify_schema(document, path)
        _verify_permissions(document, path)
        _verify_trigger(document, path)
        _verify_runners(document, path)
        _verify_execution_context(document, path)
        _verify_actions(document, path)
        _verify_setup_python(document, path)

    _verify_ci(documents["ci.yml"], candidates["ci.yml"])
    _verify_linux_experimental(
        documents["linux-experimental.yml"], candidates["linux-experimental.yml"]
    )
    _verify_codeql(documents["codeql.yml"], candidates["codeql.yml"])
    _verify_release(documents["release-draft.yml"], candidates["release-draft.yml"])

    secret_scanning_path = Path(secret_scanning)
    _verify_secret_scanning(secret_scanning_path)
    _verify_dependabot(workflow_directory.parent / "dependabot.yml")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify the audited GitHub workflow policy.")
    parser.add_argument("workflows", type=Path)
    parser.add_argument("--secret-scanning", type=Path, required=True)
    parser.add_argument("--tag")
    parser.add_argument("--project-file", type=Path, default=Path("pyproject.toml"))
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    verify_workflows(arguments.workflows, secret_scanning=arguments.secret_scanning)
    if arguments.tag is not None:
        verify_release_tag(arguments.tag, project_file=arguments.project_file)
    print("workflow policy verification passed")


if __name__ == "__main__":
    main()
