from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DefaultRootRepositories:
    documents_project: Path
    code_x_project: Path
    workbuddy_project: Path

    @property
    def paths(self) -> tuple[Path, Path, Path]:
        return (
            self.documents_project,
            self.code_x_project,
            self.workbuddy_project,
        )


def build_default_root_repositories(home: Path) -> DefaultRootRepositories:
    selected_home = Path(home)
    selected_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    selected_home.chmod(0o700)

    documents = selected_home / "Documents"
    code_x = selected_home / "Code x"
    workbuddy = selected_home / "Workbuddy"
    for root in (documents, code_x, workbuddy):
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        root.chmod(0o700)

    documents_project = _project(documents / "documents-python")
    _write(
        documents_project / "pyproject.toml",
        """[project]
name = "documents-memory-project"
version = "0.1.0"

[tool.pytest.ini_options]
addopts = "-q"
""",
    )
    _write(documents_project / "README.md", "# Documents memory project\n")
    _write(documents_project / "src" / "cache.py", "CACHE_VERSION = 1\n")

    code_x_project = _project(code_x / "code-x-node")
    _write(
        code_x_project / "package.json",
        """{
  "name": "code-x-memory-project",
  "version": "0.1.0",
  "scripts": {"build": "node src/index.js", "test": "node --test"}
}
""",
    )
    _write(code_x_project / "README.md", "# Code X memory project\n")
    _write(code_x_project / "src" / "index.js", "export const ready = true;\n")

    workbuddy_project = _project(workbuddy / "workbuddy-rust")
    _write(
        workbuddy_project / "Cargo.toml",
        """[package]
name = "workbuddy-memory-project"
version = "0.1.0"
edition = "2021"
""",
    )
    _write(workbuddy_project / "README.md", "# Workbuddy memory project\n")
    _write(workbuddy_project / "src" / "lib.rs", "pub const READY: bool = true;\n")

    return DefaultRootRepositories(
        documents_project.resolve(strict=True),
        code_x_project.resolve(strict=True),
        workbuddy_project.resolve(strict=True),
    )


def build_git_repository(path: Path) -> Path:
    repository = Path(path)
    repository.mkdir(mode=0o700, parents=True)
    repository.chmod(0o700)
    repository = repository.resolve(strict=True)
    for arguments in (
        ("init", "-b", "main"),
        ("config", "user.name", "Memory Hub E2E"),
        ("config", "user.email", "memory-hub-e2e@example.invalid"),
    ):
        _git(repository, *arguments)
    _write(repository / "README.md", "seed\n")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "initial fixture")
    return repository


def git_output(repository: Path, *arguments: str) -> str:
    return _git(Path(repository), *arguments).stdout.strip()


def _project(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    path.chmod(0o700)
    return path


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o600)


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
    }
    return subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=10,
        env=environment,
    )
