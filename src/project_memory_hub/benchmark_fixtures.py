from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import textwrap
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Literal, NoReturn
from uuid import UUID

from project_memory_hub.benchmark import canonical_json_bytes
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import ServiceContainer, build_container
from project_memory_hub.domain import (
    CapturePayload,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    RecallRequest,
    SourceAgent,
)
from project_memory_hub.services.tokens import ConservativeTokenCounter


_FIXED_GIT_DATE = "2026-07-26T00:00:00+00:00"
_FOREIGN_MODEL_SUFFIX = "-foreign"
_DISTRACTOR_COUNT = 36
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_FIXTURE_PATHS = 4096
_MAX_FIXTURE_FILE_BYTES = 4 * 1024 * 1024
_MAX_FIXTURE_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_GIT_TREE_BYTES = 4 * 1024 * 1024
_MAX_GIT_EXECUTABLE_BYTES = 512 * 1024 * 1024
_GIT_EXECUTABLE = Path("/usr/bin/git")


class BenchmarkFixtureError(ValueError):
    """A synthetic benchmark fixture failed a non-disclosing safety rule."""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    task_id: str
    fixture_id: str
    version: int
    expected_tests: int
    order: str
    title: str
    prompt: str
    validation_argv: tuple[str, ...]
    allowed_paths: tuple[PurePosixPath, ...]


@dataclass(frozen=True, slots=True)
class FixtureRepository:
    fixture_id: str
    path: Path
    commit: str


@dataclass(frozen=True, slots=True)
class _CommittedFixtureFile:
    mode: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class RecallEvidence:
    task_id: str
    text: str
    estimated_tokens: int
    selected_count: int
    omitted_count: int
    candidate_estimated_tokens: int
    brief_sha256: str
    project_canary_hits: int
    source_canary_hits: int
    model_canary_hits: int


@dataclass(frozen=True, slots=True)
class RecallBundle:
    model_id: str
    recalls: tuple[RecallEvidence, ...]
    memory_snapshot_sha256: str
    memory_snapshot_unchanged: bool
    pending_count_before: int
    pending_count_after: int
    trusted_count_before: int
    trusted_count_after: int


def load_task_specs(manifest_path: Path | str) -> tuple[TaskSpec, ...]:
    selected = Path(manifest_path)
    try:
        metadata = selected.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size <= 0
            or metadata.st_size > _MAX_MANIFEST_BYTES
        ):
            _reject_fixture()
        document = json.loads(selected.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            _reject_fixture()
        raw_tasks = document.get("tasks")
        if not isinstance(raw_tasks, list):
            _reject_fixture()
        tasks = tuple(_task_spec(raw) for raw in raw_tasks)
    except BenchmarkFixtureError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        _reject_fixture()
    if len(tasks) != 8 or len({task.task_id for task in tasks}) != len(tasks):
        _reject_fixture()
    return tasks


def build_fixture_repositories(root: Path | str) -> tuple[FixtureRepository, ...]:
    selected = Path(root)
    _prepare_empty_directory(selected)
    repositories: list[FixtureRepository] = []
    try:
        for fixture_id, files in (
            ("ledger-tools", _ledger_files()),
            ("release-kit", _release_files()),
        ):
            repository = selected / fixture_id
            repository.mkdir(mode=0o700)
            _write_tree(repository, files)
            _initialize_git(repository)
            commit = _git_output(repository, "rev-parse", "HEAD")
            if len(commit) != 40 or any(
                character not in "0123456789abcdef" for character in commit
            ):
                _reject_fixture()
            repositories.append(
                FixtureRepository(
                    fixture_id=fixture_id,
                    path=repository,
                    commit=commit,
                )
            )
    except BenchmarkFixtureError:
        raise
    except (OSError, subprocess.SubprocessError):
        _reject_fixture()
    return tuple(repositories)


def verify_fixture_repository(repository: FixtureRepository) -> None:
    """Fail closed unless the fixture worktree exactly matches its recorded commit."""
    committed = _committed_fixture_files(repository)
    _verify_fixture_worktree(repository.path, committed)


def materialize_fixture_repository(
    repository: FixtureRepository,
    target: Path | str,
) -> None:
    """Build a model workspace from verified commit blobs without copying `.git`."""
    selected = Path(target)
    committed = _committed_fixture_files(repository)
    _verify_fixture_worktree(repository.path, committed)
    try:
        if selected.exists() or selected.is_symlink():
            _reject_fixture()
        selected.mkdir(mode=0o700)
        for relative, entry in sorted(
            committed.items(),
            key=lambda item: item[0].as_posix(),
        ):
            destination = selected.joinpath(*relative.parts)
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(destination, flags, entry.mode)
            try:
                os.fchmod(descriptor, entry.mode)
                offset = 0
                while offset < len(entry.payload):
                    written = os.write(descriptor, entry.payload[offset:])
                    if written <= 0:
                        _reject_fixture()
                    offset += written
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        _verify_fixture_worktree(selected, committed, require_git_directory=False)
        if (selected / ".git").exists() or (selected / ".git").is_symlink():
            _reject_fixture()
        if _committed_fixture_files(repository) != committed:
            _reject_fixture()
        _verify_fixture_worktree(repository.path, committed)
    except BenchmarkFixtureError:
        raise
    except OSError:
        _reject_fixture()


def benchmark_git_executable() -> Path:
    """Return the fixed, metadata-validated Git used by the benchmark parent."""
    try:
        metadata = _GIT_EXECUTABLE.lstat()
    except OSError:
        _reject_fixture()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid not in {0, os.getuid()}
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or not metadata.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        or metadata.st_size <= 0
        or metadata.st_size > _MAX_GIT_EXECUTABLE_BYTES
    ):
        _reject_fixture()
    return _GIT_EXECUTABLE


def prepare_recall_bundle(
    runtime_root: Path | str,
    repositories: tuple[FixtureRepository, ...],
    tasks: tuple[TaskSpec, ...],
    *,
    model_id: str,
) -> RecallBundle:
    if not model_id or model_id != model_id.strip():
        _reject_fixture()
    repository_by_id = {repository.fixture_id: repository for repository in repositories}
    if set(repository_by_id) != {"ledger-tools", "release-kit"}:
        _reject_fixture()
    if any(task.fixture_id not in repository_by_id for task in tasks):
        _reject_fixture()

    runtime = Path(runtime_root)
    _prepare_empty_directory(runtime)
    probe_home = runtime / "probe-home"
    codex_sessions = runtime / "codex-sessions"
    foreign_project_path = runtime / "foreign-project"
    probe_home.mkdir(mode=0o700)
    codex_sessions.mkdir(mode=0o700)
    foreign_project_path.mkdir(mode=0o700)
    config_path = runtime / "config.toml"
    fixtures_root = next(iter(repository_by_id.values())).path.parent
    if any(repository.path.parent != fixtures_root for repository in repository_by_id.values()):
        _reject_fixture()
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(fixtures_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )

    with build_container(
        config_path,
        probe_home=probe_home,
        codex_sessions_root=codex_sessions,
        discovery_home=probe_home,
    ) as container:
        projects = {
            repository.fixture_id: container.projects.register(
                ProjectCandidate(
                    canonical_path=repository.path,
                    display_name=repository.fixture_id,
                    git_root=repository.path,
                )
            )
            for repository in repositories
        }
        foreign_project = container.projects.register(
            ProjectCandidate(
                canonical_path=foreign_project_path,
                display_name="foreign-control",
            )
        )
        target_namespace = Namespace(source_agent=SourceAgent.CODEX, model_id=model_id)
        foreign_model_namespace = Namespace(
            source_agent=SourceAgent.CODEX,
            model_id=model_id + _FOREIGN_MODEL_SUFFIX,
        )
        foreign_source_namespace = Namespace(
            source_agent=SourceAgent.CHATGPT,
            model_id=model_id,
        )
        for task in tasks:
            repository = repository_by_id[task.fixture_id]
            project = projects[task.fixture_id]
            memory = _memory_for_task(task)
            _capture_verified(
                container,
                project.canonical_path,
                target_namespace,
                source_record_id=f"benchmark-{task.task_id.casefold()}-target",
                objective=f"Complete preregistered benchmark task {task.task_id}",
                outcome=memory["outcome"],
                decisions=[memory["decision"]],
                failed_attempts=[memory["failed"]],
                verified_commands=[memory["verified"]],
                changed_paths=[task.allowed_paths[0].as_posix()],
                open_issues=[memory["open"]],
            )
            _capture_canary(
                container,
                foreign_project.canonical_path,
                target_namespace,
                task.task_id,
                "PROJECT",
                "codex_adapter",
            )
            _capture_canary(
                container,
                repository.path,
                foreign_source_namespace,
                task.task_id,
                "SOURCE",
                "chatgpt_adapter",
            )
            _capture_canary(
                container,
                repository.path,
                foreign_model_namespace,
                task.task_id,
                "MODEL",
                "codex_adapter",
            )
        for fixture_id, project in projects.items():
            _capture_distractors(
                container,
                project.canonical_path,
                target_namespace,
                fixture_id,
            )

        snapshot_before = _logical_snapshot(container)
        pending_before, trusted_before = _row_counts(container)
        recalls = tuple(
            _recall_evidence(
                container,
                repository_by_id[task.fixture_id],
                projects[task.fixture_id].project_id,
                target_namespace,
                task,
            )
            for task in tasks
        )
        snapshot_after = _logical_snapshot(container)
        pending_after, trusted_after = _row_counts(container)
        if (
            snapshot_before != snapshot_after
            or pending_before != 0
            or pending_after != 0
            or trusted_before != trusted_after
        ):
            _reject_fixture()
    return RecallBundle(
        model_id=model_id,
        recalls=recalls,
        memory_snapshot_sha256=hashlib.sha256(snapshot_before).hexdigest(),
        memory_snapshot_unchanged=True,
        pending_count_before=pending_before,
        pending_count_after=pending_after,
        trusted_count_before=trusted_before,
        trusted_count_after=trusted_after,
    )


def _task_spec(raw: object) -> TaskSpec:
    if not isinstance(raw, dict):
        _reject_fixture()
    try:
        task_id = raw["id"]
        fixture_id = raw["fixture"]
        version = raw["version"]
        expected_tests = raw["expected_tests"]
        order = raw["order"]
        title = raw["title"]
        prompt = raw["prompt"]
        validation = raw["validation_argv"]
        allowed = raw["allowed_changed_paths"]
    except KeyError:
        _reject_fixture()
    if (
        not isinstance(task_id, str)
        or len(task_id) != 3
        or not task_id.startswith("T")
        or not task_id[1:].isdigit()
        or fixture_id not in {"ledger-tools", "release-kit"}
        or type(version) is not int
        or version != 2
        or type(expected_tests) is not int
        or expected_tests <= 0
        or order not in {"AB", "BA"}
        or not isinstance(title, str)
        or not title
        or not isinstance(prompt, str)
        or not prompt
        or not isinstance(validation, list)
        or not validation
        or not all(isinstance(value, str) and value for value in validation)
        or not isinstance(allowed, list)
        or not allowed
        or not all(isinstance(value, str) and value for value in allowed)
    ):
        _reject_fixture()
    paths = tuple(PurePosixPath(value) for value in allowed)
    if any(
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or not path.parts
        or path.parts[0] in {".codex", ".git"}
        for path in paths
    ):
        _reject_fixture()
    return TaskSpec(
        task_id=task_id,
        fixture_id=fixture_id,
        version=version,
        expected_tests=expected_tests,
        order=order,
        title=title,
        prompt=prompt,
        validation_argv=tuple(validation),
        allowed_paths=paths,
    )


def _prepare_empty_directory(path: Path) -> None:
    try:
        if path.exists() or path.is_symlink():
            metadata = path.lstat()
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISDIR(metadata.st_mode)
                or any(path.iterdir())
            ):
                _reject_fixture()
            if metadata.st_uid != os.getuid():
                _reject_fixture()
            return
        path.mkdir(mode=0o700, parents=True)
    except BenchmarkFixtureError:
        raise
    except OSError:
        _reject_fixture()


def _write_tree(repository: Path, files: dict[str, str]) -> None:
    for relative, document in sorted(files.items()):
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or "." in path.parts:
            _reject_fixture()
        target = repository.joinpath(*path.parts)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(document.rstrip() + "\n", encoding="utf-8", newline="\n")


def _committed_fixture_files(
    repository: FixtureRepository,
) -> dict[PurePosixPath, _CommittedFixtureFile]:
    path = repository.path
    if (
        repository.fixture_id not in {"ledger-tools", "release-kit"}
        or len(repository.commit) != 40
        or repository.commit != repository.commit.casefold()
        or any(character not in "0123456789abcdef" for character in repository.commit)
    ):
        _reject_fixture()
    try:
        metadata = path.lstat()
        git_metadata = (path / ".git").lstat()
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_ISLNK(git_metadata.st_mode)
            or not stat.S_ISDIR(git_metadata.st_mode)
            or git_metadata.st_uid != os.getuid()
        ):
            _reject_fixture()
        head = (
            _fixture_git_bytes(
                path,
                "rev-parse",
                "--verify",
                "HEAD^{commit}",
                maximum=128,
            )
            .decode("ascii", errors="strict")
            .strip()
        )
        if head != repository.commit:
            _reject_fixture()
        tree = _fixture_git_bytes(
            path,
            "ls-tree",
            "-rz",
            "--full-tree",
            repository.commit,
            maximum=_MAX_GIT_TREE_BYTES,
        )
    except BenchmarkFixtureError:
        raise
    except (OSError, UnicodeError):
        _reject_fixture()

    files: dict[PurePosixPath, _CommittedFixtureFile] = {}
    total_bytes = 0
    records = tree.split(b"\0")
    if not records or records[-1] != b"":
        _reject_fixture()
    for raw_record in records[:-1]:
        try:
            raw_metadata, raw_path = raw_record.split(b"\t", 1)
            raw_mode, raw_kind, raw_object = raw_metadata.split(b" ", 2)
            relative = PurePosixPath(raw_path.decode("utf-8", errors="strict"))
            object_id = raw_object.decode("ascii", errors="strict")
        except (UnicodeError, ValueError):
            _reject_fixture()
        if (
            raw_kind != b"blob"
            or raw_mode not in {b"100644", b"100755"}
            or len(object_id) != 40
            or object_id != object_id.casefold()
            or any(character not in "0123456789abcdef" for character in object_id)
            or not relative.parts
            or relative.is_absolute()
            or "." in relative.parts
            or ".." in relative.parts
            or relative.parts[0] in {".codex", ".git"}
            or relative in files
            or len(files) >= _MAX_FIXTURE_PATHS
        ):
            _reject_fixture()
        payload = _fixture_git_bytes(
            path,
            "cat-file",
            "blob",
            object_id,
            maximum=_MAX_FIXTURE_FILE_BYTES,
        )
        git_object = b"blob " + str(len(payload)).encode("ascii") + b"\0" + payload
        if hashlib.sha1(git_object, usedforsecurity=False).hexdigest() != object_id:
            _reject_fixture()
        total_bytes += len(payload)
        if total_bytes > _MAX_FIXTURE_TOTAL_BYTES:
            _reject_fixture()
        files[relative] = _CommittedFixtureFile(
            mode=0o755 if raw_mode == b"100755" else 0o644,
            payload=payload,
        )
    if not files:
        _reject_fixture()
    return files


def _verify_fixture_worktree(
    root: Path,
    committed: dict[PurePosixPath, _CommittedFixtureFile],
    *,
    require_git_directory: bool = True,
) -> None:
    try:
        root_metadata = root.lstat()
        if (
            stat.S_ISLNK(root_metadata.st_mode)
            or not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.getuid()
        ):
            _reject_fixture()
        if require_git_directory:
            git_metadata = (root / ".git").lstat()
            if (
                stat.S_ISLNK(git_metadata.st_mode)
                or not stat.S_ISDIR(git_metadata.st_mode)
                or git_metadata.st_uid != os.getuid()
            ):
                _reject_fixture()
        actual: dict[PurePosixPath, _CommittedFixtureFile] = {}
        total_bytes = 0
        path_count = 0
        directories = [root]
        while directories:
            directory = directories.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    relative = PurePosixPath(path.relative_to(root).as_posix())
                    if relative.parts == (".git",):
                        if require_git_directory:
                            continue
                        _reject_fixture()
                    path_count += 1
                    if (
                        path_count > _MAX_FIXTURE_PATHS
                        or not relative.parts
                        or relative.is_absolute()
                        or "." in relative.parts
                        or ".." in relative.parts
                    ):
                        _reject_fixture()
                    before = entry.stat(follow_symlinks=False)
                    if (
                        stat.S_ISDIR(before.st_mode)
                        and not stat.S_ISLNK(before.st_mode)
                        and before.st_uid == os.getuid()
                    ):
                        directories.append(path)
                        continue
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or stat.S_ISLNK(before.st_mode)
                        or before.st_uid != os.getuid()
                        or before.st_nlink != 1
                        or before.st_size < 0
                        or before.st_size > _MAX_FIXTURE_FILE_BYTES
                    ):
                        _reject_fixture()
                    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(path, flags)
                    try:
                        opened = os.fstat(descriptor)
                        if _stable_fixture_metadata(before) != _stable_fixture_metadata(opened):
                            _reject_fixture()
                        chunks: list[bytes] = []
                        copied = 0
                        while chunk := os.read(descriptor, 64 * 1024):
                            copied += len(chunk)
                            if copied > _MAX_FIXTURE_FILE_BYTES:
                                _reject_fixture()
                            chunks.append(chunk)
                        after = path.lstat()
                        if copied != before.st_size or _stable_fixture_metadata(
                            opened
                        ) != _stable_fixture_metadata(after):
                            _reject_fixture()
                    finally:
                        os.close(descriptor)
                    total_bytes += copied
                    if total_bytes > _MAX_FIXTURE_TOTAL_BYTES:
                        _reject_fixture()
                    actual[relative] = _CommittedFixtureFile(
                        mode=(0o755 if bool(before.st_mode & stat.S_IXUSR) else 0o644),
                        payload=b"".join(chunks),
                    )
    except BenchmarkFixtureError:
        raise
    except (OSError, UnicodeError, ValueError):
        _reject_fixture()
    if actual != committed:
        _reject_fixture()


def _stable_fixture_metadata(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _fixture_git_bytes(
    repository: Path,
    *arguments: str,
    maximum: int,
) -> bytes:
    environment = _isolated_git_environment(repository)
    try:
        result = subprocess.run(
            (
                os.fspath(benchmark_git_executable()),
                "--no-replace-objects",
                "-c",
                "core.hooksPath=/dev/null",
                "--git-dir",
                os.fspath(repository / ".git"),
                "--work-tree",
                os.fspath(repository),
                *arguments,
            ),
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        _reject_fixture()
    if result.returncode != 0 or len(result.stdout) > maximum:
        _reject_fixture()
    return result.stdout


def _initialize_git(repository: Path) -> None:
    _git_run(repository, "init", "-q", "--initial-branch=main")
    _git_run(repository, "add", "--all")
    _git_run(
        repository,
        "-c",
        "user.name=PMH Benchmark",
        "-c",
        "user.email=benchmark.invalid",
        "-c",
        "commit.gpgsign=false",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "-q",
        "-m",
        "fixture: preregistered benchmark baseline",
    )


def _git_run(repository: Path, *arguments: str) -> None:
    environment = _isolated_git_environment(repository)
    environment.update(
        {
            "GIT_AUTHOR_DATE": _FIXED_GIT_DATE,
            "GIT_COMMITTER_DATE": _FIXED_GIT_DATE,
            "LC_ALL": "C",
            "TZ": "UTC",
        }
    )
    result = subprocess.run(
        (os.fspath(benchmark_git_executable()), *arguments),
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if result.returncode != 0:
        _reject_fixture()


def _git_output(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        (os.fspath(benchmark_git_executable()), *arguments),
        cwd=repository,
        env=_isolated_git_environment(repository),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        _reject_fixture()
    return result.stdout.strip()


def _isolated_git_environment(repository: Path) -> dict[str, str]:
    return {
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "HOME": os.fspath(repository),
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": os.environ.get("PATH", os.defpath),
        "TZ": "UTC",
    }


def _capture_verified(
    container: ServiceContainer,
    cwd: Path,
    namespace: Namespace,
    *,
    source_record_id: str,
    objective: str,
    outcome: str,
    decisions: list[str] | None = None,
    failed_attempts: list[str] | None = None,
    verified_commands: list[str] | None = None,
    changed_paths: list[str] | None = None,
    open_issues: list[str] | None = None,
    verified_by: Literal["codex_adapter", "chatgpt_adapter"] = "codex_adapter",
) -> None:
    payload = CapturePayload(
        cwd=cwd,
        namespace=namespace,
        source_record_id=source_record_id,
        objective=objective,
        outcome=outcome,
        decisions=decisions or [],
        failed_attempts=failed_attempts or [],
        verified_commands=verified_commands or [],
        changed_paths=changed_paths or [],
        open_issues=open_issues or [],
    )
    verification = NamespaceVerification(
        namespace=namespace,
        source_record_id=source_record_id,
        verified_by=verified_by,
        verified_at=datetime.now(timezone.utc),
    )
    result = container.capture.capture(payload, verification)
    if result.status not in {"inserted", "partial", "duplicate"}:
        _reject_fixture()


def _capture_canary(
    container: ServiceContainer,
    cwd: Path,
    namespace: Namespace,
    task_id: str,
    kind: str,
    verified_by: Literal["codex_adapter", "chatgpt_adapter"],
) -> None:
    marker = f"{task_id}_{kind}_FOREIGN_CANARY"
    _capture_verified(
        container,
        cwd,
        namespace,
        source_record_id=f"benchmark-{task_id.casefold()}-{kind.casefold()}-canary",
        objective=f"Foreign namespace isolation probe for {task_id}",
        outcome=f"{marker} must never appear in target recall",
        verified_by=verified_by,
    )


def _capture_distractors(
    container: ServiceContainer,
    cwd: Path,
    namespace: Namespace,
    fixture_id: str,
) -> None:
    shared_outcome = f"{fixture_id} archived unrelated background remains unchanged"
    filler = (
        "historical catalog typography pagination thumbnail telemetry palette keyboard "
        "localization export watermark inventory dashboard annotation navigation "
    )
    for index in range(_DISTRACTOR_COUNT):
        _capture_verified(
            container,
            cwd,
            namespace,
            source_record_id=f"benchmark-{fixture_id}-distractor-{index:02d}",
            objective=f"Retain unrelated archive note {index:02d}",
            outcome=shared_outcome,
            decisions=[f"ARCHIVE_DISTRACTOR_{index:02d} {filler * 3}"],
        )


def _recall_evidence(
    container: ServiceContainer,
    repository: FixtureRepository,
    project_id: UUID,
    namespace: Namespace,
    task: TaskSpec,
) -> RecallEvidence:
    memories = container.memories.search(project_id, namespace, "", 100)
    candidate_text = "\n".join(memory.normalized_content for memory in memories)
    candidate_tokens = ConservativeTokenCounter().count(candidate_text)
    brief = container.recall.recall(
        RecallRequest(
            cwd=repository.path,
            task=task.prompt,
            namespace=namespace,
            max_tokens=800,
        )
    )
    project_marker = f"{task.task_id}_PROJECT_FOREIGN_CANARY"
    source_marker = f"{task.task_id}_SOURCE_FOREIGN_CANARY"
    model_marker = f"{task.task_id}_MODEL_FOREIGN_CANARY"
    project_hits = brief.text.count(project_marker)
    source_hits = brief.text.count(source_marker)
    model_hits = brief.text.count(model_marker)
    expected_warnings = {
        "mandatory_content_shortened",
        "token_budget_truncated",
        "token_counter_fallback",
    }
    unexpected_warnings = set(brief.warnings) - expected_warnings
    if project_hits or source_hits or model_hits or unexpected_warnings:
        _reject_fixture()
    return RecallEvidence(
        task_id=task.task_id,
        text=brief.text,
        estimated_tokens=brief.estimated_tokens,
        selected_count=len(brief.selected_ids),
        omitted_count=brief.omitted_count,
        candidate_estimated_tokens=candidate_tokens,
        brief_sha256=hashlib.sha256(brief.text.encode("utf-8")).hexdigest(),
        project_canary_hits=project_hits,
        source_canary_hits=source_hits,
        model_canary_hits=model_hits,
    )


def _row_counts(container: ServiceContainer) -> tuple[int, int]:
    with container.database.connect(readonly=True) as connection:
        pending = int(connection.execute("select count(*) from pending_captures").fetchone()[0])
        trusted = int(
            connection.execute(
                "select count(*) from behavior_memories where lifecycle_state = 'active'"
            ).fetchone()[0]
        )
    return pending, trusted


def _logical_snapshot(container: ServiceContainer) -> bytes:
    with container.database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select projects.display_name, behavior_memories.source_agent,
                   behavior_memories.model_id, behavior_memories.memory_kind,
                   behavior_memories.normalized_content,
                   behavior_memories.lifecycle_state
            from behavior_memories
            join projects using (project_id)
            order by projects.display_name, behavior_memories.source_agent,
                     behavior_memories.model_id, behavior_memories.memory_kind,
                     normalized_content, lifecycle_state
            """
        ).fetchall()
        pending = connection.execute(
            """
            select projects.display_name, pending_captures.claimed_source_agent,
                   pending_captures.claimed_model_id,
                   pending_captures.verification_state
            from pending_captures
            join projects using (project_id)
            order by projects.display_name,
                     pending_captures.claimed_source_agent,
                     pending_captures.claimed_model_id,
                     pending_captures.verification_state
            """
        ).fetchall()
    return canonical_json_bytes(
        {
            "memories": [list(row) for row in rows],
            "pending": [list(row) for row in pending],
        }
    )


def _memory_for_task(task: TaskSpec) -> dict[str, str]:
    details = _MEMORY_DETAILS.get(task.task_id)
    if details is None:
        _reject_fixture()
    path = task.allowed_paths[0].as_posix()
    command = " ".join(task.validation_argv)
    return {
        "outcome": f"{task.task_id} current state: {path} still contains the bounded defect.",
        "decision": f"{task.task_id} Current decision: {details['decision']}",
        "failed": f"{task.task_id} Failed route: {details['failed']}",
        "verified": f"{task.task_id} Verified command: {command}",
        "open": f"{task.task_id} Open issue: implement only {path} and preserve the tests.",
    }


def _ledger_files_raw() -> dict[str, str]:
    return {
        ".gitignore": "__pycache__/\n*.py[cod]",
        "README.md": _fixture_readme("ledger-tools", ("T01", "T02", "T03", "T04")),
        "ledger_tools/__init__.py": "",
        "ledger_tools/records.py": """
            def normalize_record(line: str) -> str:
                return line.strip()
        """,
        "ledger_tools/settings.py": """
            from __future__ import annotations

            from typing import Any


            def merge_settings(
                base: dict[str, Any],
                override: dict[str, Any],
            ) -> dict[str, Any]:
                result = dict(base)
                for key, value in override.items():
                    current = result.get(key)
                    if isinstance(current, dict) and isinstance(value, dict):
                        result[key] = merge_settings(current, value)
                    elif isinstance(current, list) and isinstance(value, list):
                        result[key] = [*current, *value]
                    else:
                        result[key] = value
                return result
        """,
        "ledger_tools/retry.py": """
            from __future__ import annotations

            from collections.abc import Callable
            from typing import TypeVar


            T = TypeVar("T")


            def run_with_retry(operation: Callable[[], T]) -> T:
                for attempt in range(5):
                    try:
                        return operation()
                    except Exception:
                        if attempt == 4:
                            raise
                raise RuntimeError("unreachable")
        """,
        "ledger_tools/timefmt.py": """
            from datetime import datetime


            def utc_stamp(value: datetime) -> str:
                return value.isoformat()
        """,
        "tests/__init__.py": "",
        "tests/test_t01.py": """
            import unittest

            from ledger_tools.records import normalize_record


            class RecordTests(unittest.TestCase):
                def test_preserves_significant_trailing_spaces(self) -> None:
                    self.assertEqual(normalize_record("alpha  \\n"), "alpha  ")

                def test_removes_one_crlf_sequence(self) -> None:
                    self.assertEqual(normalize_record("beta\\r\\n"), "beta")
        """,
        "tests/test_t02.py": """
            import unittest

            from ledger_tools.settings import merge_settings


            class SettingsTests(unittest.TestCase):
                def test_recurses_into_mappings_but_replaces_lists(self) -> None:
                    base = {"ui": {"theme": "ink", "columns": ["name", "age"]}}
                    override = {"ui": {"density": "compact", "columns": ["name"]}}
                    self.assertEqual(
                        merge_settings(base, override),
                        {
                            "ui": {
                                "theme": "ink",
                                "density": "compact",
                                "columns": ["name"],
                            }
                        },
                    )
        """,
        "tests/test_t03.py": """
            import unittest

            from ledger_tools.retry import run_with_retry


            class RetryTests(unittest.TestCase):
                def test_retries_timeout_at_most_three_attempts(self) -> None:
                    attempts = 0

                    def operation() -> str:
                        nonlocal attempts
                        attempts += 1
                        if attempts < 3:
                            raise TimeoutError("temporary")
                        return "ok"

                    self.assertEqual(run_with_retry(operation), "ok")
                    self.assertEqual(attempts, 3)

                def test_does_not_retry_programming_errors(self) -> None:
                    attempts = 0

                    def operation() -> str:
                        nonlocal attempts
                        attempts += 1
                        raise ValueError("invalid")

                    with self.assertRaises(ValueError):
                        run_with_retry(operation)
                    self.assertEqual(attempts, 1)
        """,
        "tests/test_t04.py": """
            import unittest
            from datetime import datetime, timedelta, timezone

            from ledger_tools.timefmt import utc_stamp


            class TimestampTests(unittest.TestCase):
                def test_converts_aware_values_to_utc_z(self) -> None:
                    value = datetime(
                        2026,
                        7,
                        26,
                        8,
                        30,
                        tzinfo=timezone(timedelta(hours=8)),
                    )
                    self.assertEqual(utc_stamp(value), "2026-07-26T00:30:00Z")

                def test_rejects_naive_values(self) -> None:
                    with self.assertRaises(ValueError):
                        utc_stamp(datetime(2026, 7, 26, 0, 30))
        """,
        **_history_files(("T01", "T02", "T03", "T04")),
    }


def _release_files_raw() -> dict[str, str]:
    return {
        ".gitignore": "__pycache__/\n*.py[cod]",
        "README.md": _fixture_readme("release-kit", ("T05", "T06", "T07", "T08")),
        "release_kit/__init__.py": "",
        "release_kit/paths.py": """
            from pathlib import Path


            def safe_join(root: Path, relative: str) -> Path:
                candidate = (root / relative).resolve(strict=False)
                resolved_root = root.resolve(strict=True)
                if candidate != resolved_root and resolved_root not in candidate.parents:
                    raise ValueError("outside root")
                return candidate
        """,
        "release_kit/platform_paths.py": """
            def normalize_platform_path(value: str) -> str:
                if value.startswith("/private/var"):
                    return value.removeprefix("/private")
                return value
        """,
        "release_kit/checks.py": """
            def ordered_checks(requested: list[str]) -> tuple[str, ...]:
                return tuple(sorted(set(requested)))
        """,
        "release_kit/issues.py": """
            from __future__ import annotations

            from typing import TypedDict


            class Issue(TypedDict):
                name: str
                priority: int
                state: str


            def next_open_issue(issues: list[Issue]) -> str | None:
                if not issues:
                    return None
                return issues[0]["name"]
        """,
        "tests/__init__.py": "",
        "tests/test_t05.py": """
            import tempfile
            import unittest
            from pathlib import Path

            from release_kit.paths import safe_join


            class SafePathTests(unittest.TestCase):
                def test_rejects_existing_symlink_components(self) -> None:
                    with tempfile.TemporaryDirectory() as root_text:
                        with tempfile.TemporaryDirectory() as outside_text:
                            root = Path(root_text)
                            (root / "linked").symlink_to(Path(outside_text), target_is_directory=True)
                            with self.assertRaises(ValueError):
                                safe_join(root, "linked/output.txt")

                def test_accepts_a_normal_relative_path(self) -> None:
                    with tempfile.TemporaryDirectory() as root_text:
                        root = Path(root_text)
                        self.assertEqual(
                            safe_join(root, "nested/output.txt"),
                            root / "nested" / "output.txt",
                        )
        """,
        "tests/test_t06.py": """
            import unittest

            from release_kit.platform_paths import normalize_platform_path


            class PlatformPathTests(unittest.TestCase):
                def test_normalizes_only_the_macos_var_alias(self) -> None:
                    self.assertEqual(
                        normalize_platform_path("/private/var/folders/example"),
                        "/var/folders/example",
                    )
                    self.assertEqual(
                        normalize_platform_path("/private/various/example"),
                        "/private/various/example",
                    )
                    self.assertEqual(
                        normalize_platform_path("/home/example/output"),
                        "/home/example/output",
                    )
        """,
        "tests/test_t07.py": """
            import unittest

            from release_kit.checks import ordered_checks


            class ReleaseCheckTests(unittest.TestCase):
                def test_uses_verified_order_and_deduplicates(self) -> None:
                    self.assertEqual(
                        ordered_checks(["privacy", "package", "tests", "tests"]),
                        ("tests", "package", "privacy"),
                    )

                def test_rejects_unknown_checks(self) -> None:
                    with self.assertRaises(ValueError):
                        ordered_checks(["tests", "upload"])
        """,
        "tests/test_t08.py": """
            import unittest

            from release_kit.issues import next_open_issue


            class IssueTests(unittest.TestCase):
                def test_skips_resolved_and_selects_highest_priority_open_issue(self) -> None:
                    issues = [
                        {"name": "old failure", "priority": 1, "state": "resolved"},
                        {"name": "documentation", "priority": 3, "state": "open"},
                        {"name": "privacy audit", "priority": 2, "state": "open"},
                    ]
                    self.assertEqual(next_open_issue(issues), "privacy audit")

                def test_returns_none_when_every_issue_is_resolved(self) -> None:
                    issues = [{"name": "done", "priority": 1, "state": "resolved"}]
                    self.assertIsNone(next_open_issue(issues))
        """,
        **_history_files(("T05", "T06", "T07", "T08")),
    }


def _fixture_readme(fixture_id: str, task_ids: tuple[str, ...]) -> str:
    commands = "\n".join(
        f"- `{task_id}`: `python3 -m unittest tests.test_{task_id.casefold()}`"
        for task_id in task_ids
    )
    return f"""
        # {fixture_id}

        Synthetic repository for the Project Memory Hub public benchmark.

        Read `docs/history/<task>.md` before changing a task. Keep changes inside the implementation
        path named by the task manifest and never edit tests.

        ## Validation

        {commands}
    """


def _history_files(task_ids: tuple[str, ...]) -> dict[str, str]:
    files: dict[str, str] = {
        "docs/history/archive.md": """
            # Archived alternatives

            These notes are obsolete distractors. Current task-specific decisions live in the
            matching task note and take precedence over this archive.
        """
    }
    for task_id in task_ids:
        detail = _MEMORY_DETAILS[task_id]
        files[f"docs/history/{task_id}.md"] = f"""
            # {task_id} engineering history

            Current decision

            {detail["decision"]}

            Failed route

            {detail["failed"]}

            Verification

            Run the task-specific unittest command documented in the repository README.

            The expected patch is intentionally not recorded. Recover the behavior from the
            decision and tests.
        """
    return files


def _dedent_files(files: dict[str, str]) -> dict[str, str]:
    return {path: textwrap.dedent(document).strip() for path, document in files.items()}


_MEMORY_DETAILS = {
    "T01": {
        "decision": "remove one LF or CRLF terminator while preserving all other whitespace.",
        "failed": "using str.strip changed significant ledger data.",
    },
    "T02": {
        "decision": "merge nested mappings recursively but replace list values atomically.",
        "failed": "concatenating old and new list values retained removed columns.",
    },
    "T03": {
        "decision": "retry TimeoutError only and allow no more than three total attempts.",
        "failed": "catching Exception retried programming errors and hid defects.",
    },
    "T04": {
        "decision": "reject naive datetimes and emit aware values as second-precision UTC with Z.",
        "failed": "serializing the original timezone produced unstable API values.",
    },
    "T05": {
        "decision": "reject absolute, escaping, or existing symlink components before returning.",
        "failed": "resolve-first validation followed a symlink outside the release root.",
    },
    "T06": {
        "decision": "map only /private/var and its descendants to /var; leave other paths unchanged.",
        "failed": "a broad string prefix rewrite corrupted /private/various paths.",
    },
    "T07": {
        "decision": "deduplicate known checks and run tests, package, then privacy; reject unknowns.",
        "failed": "alphabetical ordering ran packaging before the test gate.",
    },
    "T08": {
        "decision": "ignore resolved entries and choose the lowest-priority-number open issue.",
        "failed": "selecting the first row repeated already resolved work.",
    },
}


def _ledger_files() -> dict[str, str]:
    return _dedent_files(_ledger_files_raw())


def _release_files() -> dict[str, str]:
    return _dedent_files(_release_files_raw())


def _reject_fixture() -> NoReturn:
    raise BenchmarkFixtureError("benchmark fixture rejected")
