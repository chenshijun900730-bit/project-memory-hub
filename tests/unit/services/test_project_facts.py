from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from project_memory_hub.discovery.fingerprint import fingerprint_git_remote
from project_memory_hub.services import project_facts as subject
from project_memory_hub.services.project_facts import ProjectFactService
from project_memory_hub.domain import ProjectCandidate, ProjectRecord
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.projects import ProjectRepository


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    return database


def _git(root: Path, *argv: str) -> str:
    completed = subprocess.run(
        ["git", *argv],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        timeout=5,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "GIT_OPTIONAL_LOCKS": "0"},
    )
    return completed.stdout.strip()


def _create_project(database: Database, root: Path) -> ProjectRecord:
    return ProjectRepository(database).register(
        ProjectCandidate(canonical_path=root, display_name="Synthetic")
    )


def _fact_rows(database: Database, project: ProjectRecord):
    with database.connect(readonly=True) as connection:
        return connection.execute(
            """
            select category, normalized_content, evidence_type, evidence_reference
            from project_facts where project_id = ?
            order by category, evidence_reference, normalized_content
            """,
            (str(project.project_id),),
        ).fetchall()


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


def test_scan_reads_only_approved_bounded_metadata_and_never_modifies_project(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "synthetic-project"
    root.mkdir()
    (root / "README.md").write_text(
        "# Synthetic Project\nbody must not become a fact\n## Usage\n",
        encoding="utf-8",
    )
    (root / "AGENTS.md").write_text(
        "# Local Rules\nOnly headings are metadata.\n", encoding="utf-8"
    )
    (root / "package.json").write_text(
        json.dumps(
            {
                "name": "synthetic-package",
                "scripts": {
                    "test": "private command body --with-token",
                    "build": "private build body",
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text('[project]\nname = "synthetic-python"\n', encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (root / "vite.config.ts").write_text("private build configuration", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "main.py").write_text("private source body", encoding="utf-8")
    (root / "src" / "view.ts").write_text("private source body", encoding="utf-8")
    (root / ".env").write_text("PRIVATE_FIXTURE_VALUE", encoding="utf-8")
    (root / "client.crt").write_text("private certificate", encoding="utf-8")
    (root / "private-key.txt").write_text("private key material", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "package.json").write_text("private dependency body", encoding="utf-8")
    (root / "dist").mkdir()
    (root / "dist" / "bundle.js").write_text("private output body", encoding="utf-8")
    (root / "Pods").mkdir()
    (root / "Pods" / "private.kt").write_text("private dependency", encoding="utf-8")
    (root / "DerivedData").mkdir()
    (root / "DerivedData" / "private.swift").write_text("private build output", encoding="utf-8")
    (root / "nested" / "graphify-out").mkdir(parents=True)
    (root / "nested" / "graphify-out" / "graph.json").write_text(
        '{"nodes":[1,2,3],"edges":[1,2]}', encoding="utf-8"
    )
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").write_text(
        '{"nodes":[1,2],"edges":[1]}', encoding="utf-8"
    )
    (root / "graphify-out" / "private.py").write_text("private graph source", encoding="utf-8")

    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    remote = "ssh://synthetic.invalid/private/repository"
    _git(root, "remote", "add", "origin", remote)
    (root / "README.md").write_text(
        "# Synthetic Project\nbody must not become a fact\n## Usage\nchanged\n",
        encoding="utf-8",
    )

    forbidden = {
        root / "src" / "main.py",
        root / "src" / "view.ts",
        root / ".env",
        root / "client.crt",
        root / "private-key.txt",
        root / "node_modules" / "package.json",
        root / "dist" / "bundle.js",
        root / "Pods" / "private.kt",
        root / "DerivedData" / "private.swift",
        root / "nested" / "graphify-out" / "graph.json",
        root / "graphify-out" / "private.py",
    }
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):
        if path in forbidden:
            raise AssertionError(f"forbidden metadata read: {path.name}")
        return original_open(path, *args, **kwargs)

    before = _tree_bytes(root)
    monkeypatch.setattr(Path, "open", guarded_open)
    project = _create_project(database, root)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)
    monkeypatch.undo()
    after = _tree_bytes(root)

    assert report.observed_count > 0
    assert report.warnings == ()
    assert before == after
    rows = _fact_rows(database, project)
    facts = {
        (row["category"], row["normalized_content"], row["evidence_reference"]) for row in rows
    }
    assert ("git_branch", "main", "git:branch") in facts
    assert ("git_dirty", "true", "git:dirty") in facts
    assert (
        "git_remote_fingerprint",
        fingerprint_git_remote(remote),
        "git:remote:origin",
    ) in facts
    assert ("manifest", "synthetic-package", "package.json") in facts
    assert ("manifest", "synthetic-python", "pyproject.toml") in facts
    assert ("package_script", "npm run test", "package.json#scripts.test") in facts
    assert ("package_script", "npm run build", "package.json#scripts.build") in facts
    assert ("readme_heading", "Synthetic Project", "README.md#1") in facts
    assert ("agents_heading", "Local Rules", "AGENTS.md#1") in facts
    assert ("test_config", "pytest.ini", "pytest.ini") in facts
    assert ("build_config", "vite.config.ts", "vite.config.ts") in facts
    assert ("file_extension_count", ".py=1", "tree:.py") in facts
    assert ("file_extension_count", ".ts=2", "tree:.ts") in facts
    assert ("language_count", "Python=1", "tree:language:Python") in facts
    assert ("language_count", "TypeScript=2", "tree:language:TypeScript") in facts
    assert (
        "graphify_summary",
        "nodes=2 edges=1",
        "graphify-out/graph.json",
    ) in facts
    persisted = "\n".join(row["normalized_content"] for row in rows)
    assert remote not in persisted
    assert "private command body" not in persisted
    assert "private source body" not in persisted
    assert "PRIVATE_FIXTURE_VALUE" not in persisted
    assert not any(
        row["category"] == "file_extension_count"
        and row["normalized_content"].startswith((".crt=", ".kt=", ".swift=", ".txt="))
        for row in rows
    )


def test_worktree_activity_fingerprint_reactivates_same_size_source_edit_only(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "activity-project"
    source = root / "src" / "private-module.py"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"alpha\n")
    project = _create_project(database, root)
    projects = ProjectRepository(database)
    first_at = datetime(2026, 7, 13, 1, tzinfo=timezone.utc)
    identical_at = first_at + timedelta(hours=1)
    changed_at = identical_at + timedelta(hours=1)

    ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
        now=lambda: first_at,
    ).scan(project)

    with database.transaction() as connection:
        first_fingerprint = connection.execute(
            """
            select normalized_content, evidence_type, evidence_reference
            from project_facts
            where project_id = ? and category = 'worktree_activity_fingerprint'
              and lifecycle_state = 'active' and stale_at is null
            """,
            (str(project.project_id),),
        ).fetchone()
        connection.execute(
            "update projects set inactivity_state = 'inactive' where project_id = ?",
            (str(project.project_id),),
        )

    assert first_fingerprint is not None
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", first_fingerprint["normalized_content"])
    assert first_fingerprint["evidence_type"] == "file_tree_metadata"
    assert first_fingerprint["evidence_reference"] == "tree:activity"
    assert "src" not in " ".join(first_fingerprint)
    assert "private-module.py" not in " ".join(first_fingerprint)
    assert "alpha" not in " ".join(first_fingerprint)

    ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
        now=lambda: identical_at,
    ).scan(project)

    with database.connect(readonly=True) as connection:
        unchanged = connection.execute(
            """
            select last_observed_change, inactivity_state
            from projects where project_id = ?
            """,
            (str(project.project_id),),
        ).fetchone()
        identical_fingerprint = connection.execute(
            """
            select normalized_content from project_facts
            where project_id = ? and category = 'worktree_activity_fingerprint'
              and lifecycle_state = 'active' and stale_at is null
            """,
            (str(project.project_id),),
        ).fetchone()[0]

    assert unchanged["inactivity_state"] == "inactive"
    assert unchanged["last_observed_change"] == "2026-07-13T01:00:00.000000Z"
    assert identical_fingerprint == first_fingerprint["normalized_content"]

    before = source.stat()
    source.write_bytes(b"bravo\n")
    assert source.stat().st_size == before.st_size
    os.utime(
        source,
        ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000),
    )

    ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
        now=lambda: changed_at,
    ).scan(project)

    with database.connect(readonly=True) as connection:
        changed = connection.execute(
            """
            select last_observed_change, inactivity_state
            from projects where project_id = ?
            """,
            (str(project.project_id),),
        ).fetchone()
        fingerprints = connection.execute(
            """
            select normalized_content, lifecycle_state
            from project_facts
            where project_id = ? and category = 'worktree_activity_fingerprint'
            order by lifecycle_state, normalized_content
            """,
            (str(project.project_id),),
        ).fetchall()

    assert changed["inactivity_state"] == "active"
    assert changed["last_observed_change"] == "2026-07-13T03:00:00.000000Z"
    assert {row["lifecycle_state"] for row in fingerprints} == {"active", "cold"}
    assert len({row["normalized_content"] for row in fingerprints}) == 2


def test_scan_dry_run_is_write_free_and_graphify_requires_exact_regular_path(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-project"
    root.mkdir()
    (root / "README.md").write_text("# Dry Run\n", encoding="utf-8")
    (root / "nested" / "graphify-out").mkdir(parents=True)
    (root / "nested" / "graphify-out" / "graph.json").write_text(
        '{"nodes":[1,2,3,4],"edges":[1,2,3]}', encoding="utf-8"
    )
    outside = tmp_path / "outside-graph.json"
    outside.write_text('{"nodes":[1],"edges":[]}', encoding="utf-8")
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").symlink_to(outside)
    project = _create_project(database, root)

    report = ProjectFactService(FactRepository(database), Redactor()).scan(project, dry_run=True)

    assert report.observed_count > 0
    assert _fact_rows(database, project) == []
    assert all("graphify" not in warning or "symlink" in warning for warning in report.warnings)


def test_scan_rejects_replaced_root_and_symlinked_graphify_directory(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "synthetic-project"
    root.mkdir()
    project = _create_project(database, root)
    parked = tmp_path / "parked-project"
    root.rename(parked)
    outside = tmp_path / "outside-project"
    outside.mkdir()
    (outside / "README.md").write_text("# Must Not Be Read\n", encoding="utf-8")
    root.symlink_to(outside, target_is_directory=True)

    replaced_report = ProjectFactService(FactRepository(database), Redactor()).scan(
        project, dry_run=True
    )

    assert replaced_report.observed_count == 0
    assert replaced_report.warnings == ("project_unavailable",)

    root.unlink()
    parked.rename(root)
    external_graphify = tmp_path / "external-graphify"
    external_graphify.mkdir()
    (external_graphify / "graph.json").write_text('{"nodes":[1,2,3],"edges":[1]}', encoding="utf-8")
    (root / "graphify-out").symlink_to(external_graphify, target_is_directory=True)

    graph_report = ProjectFactService(FactRepository(database), Redactor()).scan(
        project, dry_run=True
    )

    assert "graphify_symlink:graphify-out/graph.json" in graph_report.warnings
    assert all(
        fact.normalized_content != "nodes=3 edges=1"
        for fact in FactRepository(database).search(project.project_id, "", 20)
    )


def test_scan_converts_structurally_invalid_manifests_to_stable_warnings(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "invalid-manifests"
    root.mkdir()
    (root / "Cargo.toml").write_text('package = "not-a-table"\n', encoding="utf-8")
    (root / "pyproject.toml").write_text('tool = "not-a-table"\n', encoding="utf-8")
    project = _create_project(database, root)

    report = ProjectFactService(FactRepository(database), Redactor()).scan(project, dry_run=True)

    assert "manifest_parse_failed:Cargo.toml" in report.warnings
    assert "manifest_parse_failed:pyproject.toml" in report.warnings


def test_scan_enforces_manifest_graph_tree_bounds_with_stable_warnings(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "bounded-project"
    root.mkdir()
    (root / "package.json").write_text("{" + ("x" * 100), encoding="utf-8")
    (root / "README.md").write_text("# " + ("H" * 100), encoding="utf-8")
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").write_text("[" + ("0," * 100), encoding="utf-8")
    for index in range(10):
        (root / f"file-{index}.py").write_text("pass", encoding="utf-8")
    project = _create_project(database, root)
    service = ProjectFactService(
        FactRepository(database),
        Redactor(),
        max_manifest_bytes=16,
        max_heading_bytes=16,
        max_graph_bytes=16,
        max_tree_entries=3,
        max_tree_depth=1,
    )

    first = service.scan(project, dry_run=True)
    second = service.scan(project, dry_run=True)

    assert first == second
    assert first.warnings == tuple(sorted(first.warnings))
    assert any("manifest_too_large:package.json" == item for item in first.warnings)
    assert any("heading_too_large:README.md" == item for item in first.warnings)
    assert any("graphify_too_large:graphify-out/graph.json" == item for item in first.warnings)
    assert "tree_entry_limit" in first.warnings


@pytest.mark.parametrize(
    "limit_name, value",
    [
        ("max_manifest_bytes", 0),
        ("max_heading_bytes", 0),
        ("max_graph_bytes", 0),
        ("max_tree_entries", 0),
        ("max_tree_depth", 0),
    ],
)
def test_scan_limits_must_be_positive(
    database: Database,
    limit_name: str,
    value: int,
) -> None:
    with pytest.raises(ValueError, match=limit_name):
        ProjectFactService(
            FactRepository(database),
            Redactor(),
            **{limit_name: value},
        )


def test_git_dirty_includes_untracked_files_without_subprocess_run(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "untracked-project"
    root.mkdir()
    (root / "README.md").write_text("# Clean\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    (root / "untracked.py").write_text("pass", encoding="utf-8")
    project = _create_project(database, root)

    def reject_run(*args, **kwargs):
        raise AssertionError("project fact Git probes must use bounded Popen")

    monkeypatch.setattr(subject.subprocess, "run", reject_run)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert not any(warning.startswith("git_") for warning in report.warnings)
    facts = _fact_rows(database, project)
    assert any(
        row["category"] == "git_dirty" and row["normalized_content"] == "true" for row in facts
    )


def test_clean_git_repository_does_not_warn_that_dirty_state_is_unavailable(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "clean-project"
    root.mkdir()
    (root / "README.md").write_text("# Clean\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    project = _create_project(database, root)

    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert "git_dirty_unavailable" not in report.warnings
    assert all(row["category"] != "git_dirty" for row in _fact_rows(database, project))


def test_git_dirty_includes_modified_tracked_files_without_clean_filter(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "modified-project"
    root.mkdir()
    tracked = root / "README.md"
    tracked.write_text("# Before\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    tracked.write_text("# After\n", encoding="utf-8")
    project = _create_project(database, root)

    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert report.warnings == ()
    assert any(
        row["category"] == "git_dirty" and row["normalized_content"] == "true"
        for row in _fact_rows(database, project)
    )


def test_unborn_git_repository_does_not_warn_that_head_is_unavailable(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "unborn-project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    project = _create_project(database, root)

    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert "git_head_unavailable" not in report.warnings
    assert all(row["category"] != "git_head" for row in _fact_rows(database, project))


def test_git_head_probe_failure_still_warns(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "head-probe-failure"
    root.mkdir()
    _git(root, "init", "-b", "main")
    project = _create_project(database, root)

    def controlled_probe(root_fd, operation, *arguments, max_bytes=None):
        del root_fd, arguments, max_bytes
        if operation == "worktree":
            return SimpleNamespace(returncode=0, stdout=b"true\n", reason=None)
        if operation == "branch":
            return SimpleNamespace(returncode=0, stdout=b"main\n", reason=None)
        if operation == "head":
            return SimpleNamespace(returncode=2, stdout=b"", reason=None)
        return SimpleNamespace(returncode=0, stdout=b"", reason=None)

    monkeypatch.setattr(subject, "_git_probe", controlled_probe, raising=False)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert "git_head_unavailable" in report.warnings
    assert all(row["category"] != "git_head" for row in _fact_rows(database, project))


def test_remote_fingerprint_strips_credentials_and_rejects_malformed_remote(
    database: Database,
    tmp_path: Path,
) -> None:
    credentialed = "https://user:password@Example.INVALID/org/repo.git?token=value"
    equivalent = "https://example.invalid/org/repo"
    assert fingerprint_git_remote(credentialed) == fingerprint_git_remote(equivalent)

    projects = []
    for index, remote in enumerate((credentialed, equivalent)):
        root = tmp_path / f"remote-{index}"
        root.mkdir()
        (root / "README.md").write_text("# Remote\n", encoding="utf-8")
        _git(root, "init", "-b", "main")
        _git(root, "config", "user.email", "synthetic@example.invalid")
        _git(root, "config", "user.name", "Synthetic")
        _git(root, "add", "README.md")
        _git(root, "commit", "-m", "initial")
        _git(root, "remote", "add", "origin", remote)
        project = _create_project(database, root)
        ProjectFactService(FactRepository(database), Redactor()).scan(project)
        projects.append(project)

    fingerprints = [
        next(
            row["normalized_content"]
            for row in _fact_rows(database, project)
            if row["category"] == "git_remote_fingerprint"
        )
        for project in projects
    ]
    assert fingerprints == [fingerprint_git_remote(equivalent)] * 2

    malformed_root = tmp_path / "malformed-remote"
    malformed_root.mkdir()
    (malformed_root / "README.md").write_text("# Remote\n", encoding="utf-8")
    _git(malformed_root, "init", "-b", "main")
    _git(malformed_root, "config", "user.email", "synthetic@example.invalid")
    _git(malformed_root, "config", "user.name", "Synthetic")
    _git(malformed_root, "add", "README.md")
    _git(malformed_root, "commit", "-m", "initial")
    _git(malformed_root, "remote", "add", "origin", "https:///missing-host")
    malformed_project = _create_project(database, malformed_root)

    malformed_report = ProjectFactService(FactRepository(database), Redactor()).scan(
        malformed_project
    )

    assert "git_remote_invalid" in malformed_report.warnings
    assert all(
        row["category"] != "git_remote_fingerprint"
        for row in _fact_rows(database, malformed_project)
    )


@pytest.mark.parametrize("mode", ["overflow", "timeout"])
def test_bounded_process_output_kills_waits_and_closes_resources(mode: str) -> None:
    script = (
        "import os; os.write(1, b'x' * 4096)"
        if mode == "overflow"
        else "import time; time.sleep(10)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    result = subject._collect_bounded_stdout(
        process,
        max_bytes=32,
        timeout_seconds=0.05 if mode == "timeout" else 2.0,
    )

    assert result.reason == mode
    assert len(result.stdout) <= 33
    assert process.poll() is not None
    assert process.stdout is not None
    assert process.stdout.closed


@pytest.mark.parametrize(
    ("reason", "expected_warning"),
    [("overflow", "git_output_limit:branch"), ("timeout", "git_timeout:branch")],
)
def test_bounded_git_failure_warns_without_recording_detached_branch(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected_warning: str,
) -> None:
    root = tmp_path / f"git-{reason}"
    root.mkdir()
    _git(root, "init", "-b", "main")
    project = _create_project(database, root)

    def controlled_probe(root_fd, operation, *arguments, max_bytes=None):
        del root_fd, arguments, max_bytes
        if operation == "worktree":
            return SimpleNamespace(returncode=0, stdout=b"true\n", reason=None)
        if operation == "branch":
            return SimpleNamespace(
                returncode=None,
                stdout=b"x" * 65_537 if reason == "overflow" else b"",
                reason=reason,
            )
        return SimpleNamespace(returncode=1, stdout=b"", reason=None)

    monkeypatch.setattr(subject, "_git_probe", controlled_probe, raising=False)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert expected_warning in report.warnings
    assert all(
        not (row["category"] == "git_branch" and row["normalized_content"] == "detached")
        for row in _fact_rows(database, project)
    )


def test_fact_redacts_full_candidate_before_applying_fact_cap(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "fact-cap"
    root.mkdir()
    secret = "sk-proj-" + ("S" * 24)
    (root / "README.md").write_text("# " + ("A" * 1010) + " " + secret + "\n", encoding="utf-8")
    project = _create_project(database, root)

    ProjectFactService(FactRepository(database), Redactor()).scan(project)

    heading = next(
        row["normalized_content"]
        for row in _fact_rows(database, project)
        if row["category"] == "readme_heading"
    )
    assert len(heading) <= 1024
    if "sk-proj-" in heading or secret in heading:
        pytest.fail("partial credential persisted in capped fact", pytrace=False)


@pytest.mark.parametrize("target", ["manifest", "graphify"])
def test_deep_json_returns_stable_parse_warning(
    database: Database,
    tmp_path: Path,
    target: str,
) -> None:
    root = tmp_path / f"deep-{target}"
    root.mkdir()
    deep_json = ("[" * 2000) + "0" + ("]" * 2000)
    if target == "manifest":
        (root / "package.json").write_text(deep_json, encoding="utf-8")
        expected = "manifest_parse_failed:package.json"
    else:
        (root / "graphify-out").mkdir()
        (root / "graphify-out" / "graph.json").write_text(deep_json, encoding="utf-8")
        expected = "graphify_parse_failed:graphify-out/graph.json"
    project = _create_project(database, root)

    report = ProjectFactService(FactRepository(database), Redactor()).scan(project, dry_run=True)

    assert expected in report.warnings


def test_root_swap_after_anchor_open_discards_all_candidates(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "anchor-project"
    root.mkdir()
    (root / "README.md").write_text("# Original\n", encoding="utf-8")
    project = _create_project(database, root)
    parked = tmp_path / "parked-project"
    outside = tmp_path / "outside-project"
    outside.mkdir()
    (outside / "package.json").write_text('{"name":"outside-package"}', encoding="utf-8")
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = (
            real_open(path, flags, mode)
            if dir_fd is None
            else real_open(path, flags, mode, dir_fd=dir_fd)
        )
        if not swapped and path == root.name and flags & getattr(os, "O_DIRECTORY", 0):
            root.rename(parked)
            root.symlink_to(outside, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(subject.os, "open", racing_open)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert swapped is True
    assert report.observed_count == 0
    assert report.warnings == ("project_replaced",)
    assert _fact_rows(database, project) == []


def test_scan_rejects_normal_directory_replacement_for_registered_project(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "registered-project"
    root.mkdir()
    (root / "README.md").write_text("# Original\n", encoding="utf-8")
    projects = ProjectRepository(database)
    project = projects.register(ProjectCandidate(canonical_path=root, display_name="Registered"))

    root.rename(tmp_path / "parked-project")
    root.mkdir()
    (root / "README.md").write_text("# Replacement\n", encoding="utf-8")

    report = ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
    ).scan(project)

    assert report.observed_count == 0
    assert report.stale_count == 0
    assert report.warnings == ("project_replaced",)
    assert _fact_rows(database, project) == []


def test_scan_rejects_stale_project_record_after_relink(
    database: Database,
    tmp_path: Path,
) -> None:
    original = tmp_path / "original-project"
    original.mkdir()
    (original / "README.md").write_text("# Original\n", encoding="utf-8")
    destination = tmp_path / "relinked-project"
    destination.mkdir()
    projects = ProjectRepository(database)
    stale_project = projects.register(
        ProjectCandidate(canonical_path=original, display_name="Relinked")
    )
    projects.relink(stale_project.project_id, destination)

    report = ProjectFactService(
        FactRepository(database),
        Redactor(),
        projects=projects,
    ).scan(stale_project)

    assert report.observed_count == 0
    assert report.stale_count == 0
    assert report.warnings == ("project_replaced",)
    assert _fact_rows(database, stale_project) == []


def test_scan_revalidates_project_identity_inside_fact_write_transaction(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "write-race-project"
    root.mkdir()
    (root / "README.md").write_text("# Original\n", encoding="utf-8")
    projects = ProjectRepository(database)
    project = projects.register(ProjectCandidate(canonical_path=root, display_name="Write race"))
    facts = FactRepository(database)
    real_observe = facts._observe_many_with_changes
    raced = False

    def racing_observe(project_id, observed_facts, **kwargs):
        nonlocal raced
        root.rename(tmp_path / "write-race-parked")
        root.mkdir()
        (root / "README.md").write_text("# Replacement\n", encoding="utf-8")
        raced = True
        return real_observe(project_id, observed_facts, **kwargs)

    monkeypatch.setattr(facts, "_observe_many_with_changes", racing_observe)

    report = ProjectFactService(
        facts,
        Redactor(),
        projects=projects,
    ).scan(project)

    assert raced is True
    assert report.observed_count == 0
    assert report.stale_count == 0
    assert report.warnings == ("project_replaced",)
    assert _fact_rows(database, project) == []


def test_scan_rolls_back_if_project_is_disabled_before_fact_write(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "disabled-write-race-project"
    root.mkdir()
    (root / "README.md").write_text("# Original\n", encoding="utf-8")
    projects = ProjectRepository(database)
    project = projects.register(ProjectCandidate(canonical_path=root, display_name="Disable race"))
    facts = FactRepository(database)
    real_observe = facts._observe_many_with_changes

    def disabling_observe(project_id, observed_facts, **kwargs):
        projects.set_enabled(project.project_id, False)
        return real_observe(project_id, observed_facts, **kwargs)

    monkeypatch.setattr(facts, "_observe_many_with_changes", disabling_observe)

    report = ProjectFactService(
        facts,
        Redactor(),
        projects=projects,
    ).scan(project)

    assert report.observed_count == 0
    assert report.stale_count == 0
    assert report.warnings == ("project_replaced",)
    assert _fact_rows(database, project) == []


def test_scan_rolls_back_if_project_is_replaced_after_fact_writes(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "post-write-race-project"
    root.mkdir()
    (root / "README.md").write_text("# Original\n", encoding="utf-8")
    projects = ProjectRepository(database)
    project = projects.register(
        ProjectCandidate(canonical_path=root, display_name="Post-write race")
    )
    facts = FactRepository(database)
    real_observe = facts._observe_many_with_changes
    raced = False

    def racing_observe(project_id, observed_facts, **kwargs):
        original_callback = kwargs.get("on_effective_change")

        def replace_after_writes(connection):
            nonlocal raced
            if original_callback is not None:
                original_callback(connection)
            root.rename(tmp_path / "post-write-race-parked")
            root.mkdir()
            (root / "README.md").write_text("# Replacement\n", encoding="utf-8")
            raced = True

        kwargs["on_effective_change"] = replace_after_writes
        return real_observe(project_id, observed_facts, **kwargs)

    monkeypatch.setattr(facts, "_observe_many_with_changes", racing_observe)

    report = ProjectFactService(
        facts,
        Redactor(),
        projects=projects,
    ).scan(project)

    assert raced is True
    assert report.observed_count == 0
    assert report.stale_count == 0
    assert report.warnings == ("project_replaced",)
    assert _fact_rows(database, project) == []


@pytest.mark.parametrize("swap_target", ["manifest", "graph_dir", "graph_file"])
def test_anchored_exact_reads_reject_component_swaps(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    swap_target: str,
) -> None:
    root = tmp_path / f"swap-{swap_target}"
    root.mkdir()
    (root / "package.json").write_text('{"name":"inside-package"}', encoding="utf-8")
    (root / "graphify-out").mkdir()
    (root / "graphify-out" / "graph.json").write_text('{"nodes":[1],"edges":[]}', encoding="utf-8")
    outside_manifest = tmp_path / f"outside-{swap_target}.json"
    outside_manifest.write_text('{"name":"outside-package"}', encoding="utf-8")
    outside_graph = tmp_path / f"outside-graph-{swap_target}"
    outside_graph.mkdir()
    (outside_graph / "graph.json").write_text('{"nodes":[1,2,3,4],"edges":[1]}', encoding="utf-8")
    project = _create_project(database, root)
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None:
            if swap_target == "manifest" and path == "package.json":
                (root / "package.json").rename(root / "package.original")
                (root / "package.json").symlink_to(outside_manifest)
                swapped = True
            elif swap_target == "graph_dir" and path == "graphify-out":
                (root / "graphify-out").rename(root / "graphify-original")
                (root / "graphify-out").symlink_to(outside_graph, target_is_directory=True)
                swapped = True
            elif swap_target == "graph_file" and path == "graph.json":
                graph = root / "graphify-out" / "graph.json"
                graph.rename(root / "graphify-out" / "graph.original")
                graph.symlink_to(outside_graph / "graph.json")
                swapped = True
        return (
            real_open(path, flags, mode)
            if dir_fd is None
            else real_open(path, flags, mode, dir_fd=dir_fd)
        )

    monkeypatch.setattr(subject.os, "open", racing_open)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert swapped is True
    persisted = "\n".join(row["normalized_content"] for row in _fact_rows(database, project))
    assert "outside-package" not in persisted
    assert "nodes=4 edges=1" not in persisted
    assert report.warnings


def test_tree_recursion_rejects_swapped_symlink_directory(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "tree-swap"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "inside.ts").write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside-tree"
    outside.mkdir()
    (outside / "outside.py").write_text("outside", encoding="utf-8")
    parked = root / "src-original"
    project = _create_project(database, root)
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if not swapped and dir_fd is not None and path == "src":
            (root / "src").rename(parked)
            (root / "src").symlink_to(outside, target_is_directory=True)
            swapped = True
        return (
            real_open(path, flags, mode)
            if dir_fd is None
            else real_open(path, flags, mode, dir_fd=dir_fd)
        )

    monkeypatch.setattr(subject.os, "open", racing_open)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert swapped is True
    assert report.warnings
    assert all(
        row["category"] != "language_count" or row["normalized_content"] != "Python=1"
        for row in _fact_rows(database, project)
    )


def test_tree_enumeration_consumes_at_most_budget_plus_one_and_discards_partial(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "bounded-enumeration"
    root.mkdir()
    for index in range(20):
        (root / f"file-{index:02d}.py").write_text("pass", encoding="utf-8")
    project = _create_project(database, root)
    real_scandir = os.scandir
    consumed = 0

    class GuardedScandir:
        def __init__(self, iterator) -> None:
            self._iterator = iterator

        def __iter__(self):
            return self

        def __next__(self):
            nonlocal consumed
            consumed += 1
            if consumed > 4:
                raise AssertionError("tree enumeration exceeded budget plus sentinel")
            return next(self._iterator)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.close()

        def close(self):
            self._iterator.close()

    def guarded_scandir(path):
        return GuardedScandir(real_scandir(path))

    monkeypatch.setattr(subject.os, "scandir", guarded_scandir)
    report = ProjectFactService(FactRepository(database), Redactor(), max_tree_entries=3).scan(
        project, dry_run=True
    )

    assert consumed == 4
    assert report.warnings == ("tree_entry_limit",)
    assert report.observed_count == 0


def test_warning_codes_never_echo_synthetic_entry_names(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "warning-project"
    root.mkdir()
    synthetic_name = "sk-proj-" + ("W" * 24) + ".py"
    (root / synthetic_name).write_text("pass", encoding="utf-8")
    project = _create_project(database, root)
    real_scandir = os.scandir

    class FailingEntry:
        def __init__(self, entry) -> None:
            self._entry = entry
            self.name = entry.name

        def stat(self, *, follow_symlinks=False):
            if self.name == synthetic_name:
                raise OSError("synthetic stat failure")
            return self._entry.stat(follow_symlinks=follow_symlinks)

        def is_symlink(self):
            return self._entry.is_symlink()

        def is_file(self, *, follow_symlinks=True):
            if self.name == synthetic_name:
                raise OSError("synthetic stat failure")
            return self._entry.is_file(follow_symlinks=follow_symlinks)

        def is_dir(self, *, follow_symlinks=True):
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

    class WrappedScandir:
        def __init__(self, iterator) -> None:
            self._iterator = iterator

        def __iter__(self):
            return (FailingEntry(entry) for entry in self._iterator)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            self._iterator.close()

        def close(self):
            self._iterator.close()

    monkeypatch.setattr(subject.os, "scandir", lambda path: WrappedScandir(real_scandir(path)))
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project, dry_run=True)

    if synthetic_name in "\n".join(report.warnings):
        pytest.fail("warning leaked a synthetic entry name", pytrace=False)
    assert "tree_stat_failed" in report.warnings
    assert len(report.warnings) <= 16


def test_scan_disables_configured_git_fsmonitor_helper(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "fsmonitor-project"
    root.mkdir()
    (root / "README.md").write_text("# Fsmonitor\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    marker = tmp_path / "fsmonitor-ran"
    helper = root / "fsmonitor-helper"
    helper.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\n"
        f"Path({str(marker)!r}).touch()\nprint('0')\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    _git(root, "config", "core.fsmonitor", str(helper))
    _git(root, "config", "core.untrackedCache", "true")
    project = _create_project(database, root)
    _git(root, "status", "--porcelain=v1")
    assert marker.exists(), "configured fsmonitor fixture was not executable"
    marker.unlink()

    ProjectFactService(FactRepository(database), Redactor()).scan(project, dry_run=True)

    assert not marker.exists()


def test_every_git_probe_disables_helpers_and_starts_new_session(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "isolated-git-probes"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "README.md").write_text("# Probes\n", encoding="utf-8")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    project = _create_project(database, root)
    real_popen = subprocess.Popen
    calls: list[tuple[list[str], dict[str, object]]] = []

    def recording_popen(argv, **kwargs):
        calls.append((argv, kwargs.copy()))
        return real_popen(argv, **kwargs)

    monkeypatch.setattr(subject.subprocess, "Popen", recording_popen)

    ProjectFactService(FactRepository(database), Redactor()).scan(project, dry_run=True)

    assert calls
    required = [
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.preloadIndex=false",
        "-c",
        "core.excludesFile=/dev/null",
    ]
    git_commands = []
    for argv, kwargs in calls:
        assert kwargs.get("start_new_session") is True
        assert argv[6:14] == required
        environment = kwargs["env"]
        assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
        assert environment["GIT_CONFIG_SYSTEM"] == "/dev/null"
        assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert environment["GIT_ATTR_NOSYSTEM"] == "1"
        assert environment["GIT_TERMINAL_PROMPT"] == "0"
        assert environment["GIT_PAGER"] == "cat"
        assert environment["GIT_EDITOR"] == "/usr/bin/false"
        assert environment["GIT_ASKPASS"] == "/usr/bin/false"
        assert environment["SSH_ASKPASS"] == "/usr/bin/false"
        git_commands.append(argv[14:])
    assert [
        "diff-index",
        "--cached",
        "--quiet",
        "--no-ext-diff",
        "--no-textconv",
        "--no-renames",
        "--ignore-submodules=dirty",
        "HEAD",
        "--",
    ] in git_commands
    assert ["ls-files", "-z", "--unmerged"] in git_commands
    assert [
        "ls-files",
        "-z",
        "--deleted",
        "--others",
        "--exclude-standard",
    ] in git_commands
    assert all(command[:1] != ["status"] for command in git_commands)
    assert all("--modified" not in command for command in git_commands)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX process groups")
def test_bounded_stdout_kills_descendant_that_holds_pipe_after_parent_exit(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "orphan-marker"
    child_code = (
        f"import time; from pathlib import Path; time.sleep(0.2); Path({str(marker)!r}).touch()"
    )
    parent_code = (
        "import subprocess,sys; "
        "subprocess.Popen([sys.executable,'-c',"
        f"{child_code!r}],stdin=subprocess.DEVNULL,stdout=sys.stdout,"
        "stderr=subprocess.DEVNULL)"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        start_new_session=True,
    )
    try:
        assert process.wait(timeout=1) == 0
        result = subject._collect_bounded_stdout(process, max_bytes=32, timeout_seconds=0.05)
        time.sleep(0.3)

        assert result.reason == "timeout"
        assert not marker.exists()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_parent_directory_replacement_discards_all_candidates(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "registered-parent"
    root = parent / "project"
    root.mkdir(parents=True)
    (root / "README.md").write_text("# Original\n", encoding="utf-8")
    project = _create_project(database, root)
    parked_parent = tmp_path / "parked-parent"
    outside_parent = tmp_path / "outside-parent"
    outside_root = outside_parent / root.name
    outside_root.mkdir(parents=True)
    (outside_root / "README.md").write_text("# Outside\n", encoding="utf-8")
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = (
            real_open(path, flags, mode)
            if dir_fd is None
            else real_open(path, flags, mode, dir_fd=dir_fd)
        )
        if not swapped and dir_fd is not None and path == root.name:
            parent.rename(parked_parent)
            parent.symlink_to(outside_parent, target_is_directory=True)
            swapped = True
        return descriptor

    monkeypatch.setattr(subject.os, "open", racing_open)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert swapped is True
    assert report.observed_count == 0
    assert report.warnings == ("project_replaced",)
    assert _fact_rows(database, project) == []


def test_dirty_probe_never_executes_repository_clean_filter(
    database: Database,
    tmp_path: Path,
) -> None:
    root = tmp_path / "clean-filter-project"
    root.mkdir()
    marker = tmp_path / "clean-filter-ran"
    helper = tmp_path / "evil-clean-filter"
    helper.write_text(
        f"#!{sys.executable}\nfrom pathlib import Path\nimport sys\n"
        f"Path({str(marker)!r}).touch()\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    helper.chmod(0o700)
    (root / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    tracked = root / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "config", "filter.evil.clean", str(helper))
    _git(root, "add", ".gitattributes", "tracked.txt")
    _git(root, "commit", "-m", "initial")
    marker.unlink(missing_ok=True)
    tracked.write_text("AFTER!\n", encoding="utf-8")
    project = _create_project(database, root)

    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert not marker.exists()
    assert "git_dirty_unavailable" in report.warnings
    assert all(row["category"] != "git_dirty" for row in _fact_rows(database, project))
    _git(root, "status", "--porcelain=v1")
    assert marker.exists(), "configured clean-filter fixture was not executable"


def test_git_config_process_filter_makes_worktree_probe_ambiguous() -> None:
    assert subject._git_config_has_clean_filter(
        '[filter "evil"]\n\tprocess = /synthetic/filter-process\n'
    )


@pytest.mark.parametrize(
    "unsafe_section",
    [
        "[include]\npath = {outside}\n",
        '[includeIf "gitdir:/synthetic/**"]\npath = {outside}\n',
        "[extensions]\nworktreeConfig = true\n",
        "[extensions]\nworktreeConfig\n",
        "[InClUdEIf.gitdir:{git_dir}]\npath = {outside}\n",
        "[InClUdE.legacy]\npath = {outside}\n",
    ],
    ids=[
        "include",
        "include-if",
        "worktree-config",
        "implicit-worktree-config",
        "dotted-include-if",
        "dotted-include",
    ],
)
def test_unsafe_local_git_config_fails_closed_before_subprocess(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_section: str,
) -> None:
    root = tmp_path / "unsafe-config-project"
    root.mkdir()
    (root / "README.md").write_text("# Unsafe config\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    outside = tmp_path / "outside-config"
    outside.write_text(
        '[remote "origin"]\nurl = https://outside.invalid/private.git\n',
        encoding="utf-8",
    )
    config = root / ".git" / "config"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n"
        + unsafe_section.format(outside=outside, git_dir=root / ".git"),
        encoding="utf-8",
    )
    if unsafe_section == "[extensions]\nworktreeConfig\n":
        assert _git(root, "config", "--bool", "extensions.worktreeConfig") == "true"
    project = _create_project(database, root)

    def reject_popen(*args, **kwargs):
        raise AssertionError("unsafe local Git config launched a subprocess")

    monkeypatch.setattr(subject.subprocess, "Popen", reject_popen)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert "git_config_unsafe" in report.warnings
    assert all(not row["category"].startswith("git_") for row in _fact_rows(database, project))


@pytest.mark.parametrize("unsafe_shape", ["git-symlink", "git-file", "config-symlink"])
def test_non_direct_git_config_shape_fails_closed(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_shape: str,
) -> None:
    root = tmp_path / f"unsafe-{unsafe_shape}"
    root.mkdir()
    if unsafe_shape == "git-symlink":
        outside_repo = tmp_path / "outside-repo"
        outside_repo.mkdir()
        _git(outside_repo, "init", "-b", "main")
        (root / ".git").symlink_to(outside_repo / ".git", target_is_directory=True)
    elif unsafe_shape == "git-file":
        (root / ".git").write_text(f"gitdir: {tmp_path / 'outside-git-dir'}\n", encoding="utf-8")
    else:
        _git(root, "init", "-b", "main")
        config = root / ".git" / "config"
        outside_config = tmp_path / "outside-config"
        config.rename(outside_config)
        config.symlink_to(outside_config)
    project = _create_project(database, root)

    def reject_popen(*args, **kwargs):
        raise AssertionError("non-direct Git config launched a subprocess")

    monkeypatch.setattr(subject.subprocess, "Popen", reject_popen)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert "git_config_unsafe" in report.warnings
    assert all(not row["category"].startswith("git_") for row in _fact_rows(database, project))


def test_leading_utf8_bom_cannot_hide_include_header(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "bom-config-project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    outside = tmp_path / "bom-outside-config"
    outside.write_text(
        '[remote "origin"]\nurl = https://bom-outside.invalid/repo.git\n',
        encoding="utf-8",
    )
    (root / ".git" / "config").write_text(f"\ufeff[include]\npath = {outside}\n", encoding="utf-8")
    assert (
        _git(root, "config", "--includes", "--get", "remote.origin.url")
        == "https://bom-outside.invalid/repo.git"
    )
    project = _create_project(database, root)

    def reject_popen(*args, **kwargs):
        raise AssertionError("BOM-hidden include launched a subprocess")

    monkeypatch.setattr(subject.subprocess, "Popen", reject_popen)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert "git_config_unsafe" in report.warnings
    assert all(not row["category"].startswith("git_") for row in _fact_rows(database, project))


def test_git_config_replacement_during_probes_discards_git_candidates(
    database: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config-race-project"
    root.mkdir()
    (root / "README.md").write_text("# Config race\n", encoding="utf-8")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "synthetic@example.invalid")
    _git(root, "config", "user.name", "Synthetic")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    _git(root, "remote", "add", "origin", "https://inside.invalid/repo.git")
    project = _create_project(database, root)
    config = root / ".git" / "config"
    parked = root / ".git" / "config.original"
    original = config.read_text(encoding="utf-8")
    real_popen = subprocess.Popen
    swapped = False

    def racing_popen(*args, **kwargs):
        nonlocal swapped
        process = real_popen(*args, **kwargs)
        if not swapped:
            config.rename(parked)
            config.write_text(original, encoding="utf-8")
            swapped = True
        return process

    monkeypatch.setattr(subject.subprocess, "Popen", racing_popen)
    report = ProjectFactService(FactRepository(database), Redactor()).scan(project)

    assert swapped is True
    assert "git_config_changed" in report.warnings
    assert all(not row["category"].startswith("git_") for row in _fact_rows(database, project))
