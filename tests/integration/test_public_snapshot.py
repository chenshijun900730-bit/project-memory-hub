from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, cast

import pytest


BRANCH = "codex/public-beta-0.2.1"
SCRIPT = Path(__file__).parents[2] / "scripts" / "prepare_public_snapshot.py"
FIXED_IDENTITY = "Project Memory Hub Maintainers <noreply@project-memory-hub.invalid>"
FIXED_MESSAGE = "chore: create public beta 0.2.1 snapshot"
ZERO_OID = "0" * 40


def _git(
    root: Path,
    *arguments: str,
    check: bool = True,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> str:
    environment = os.environ.copy()
    if env is not None:
        environment.update(env)
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=False,
        env=environment,
        input=input_bytes,
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    )
    if check and result.returncode != 0:
        raise AssertionError(
            f"git {' '.join(arguments)} failed: {result.stderr.decode(errors='replace')}"
        )
    return result.stdout.decode().strip()


def _write(path: Path, content: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if executable:
        path.chmod(0o755)


def _repository(tmp_path: Path) -> Path:
    root = tmp_path / "private-source"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Private Test Author")
    _git(root, "config", "user.email", "private-test@example.invalid")
    _git(root, "config", "commit.gpgsign", "false")

    _write(root / "private-history.txt", "not present in the public tree\n")
    _git(root, "add", "private-history.txt")
    _git(root, "commit", "-m", "private history")

    _git(root, "rm", "private-history.txt")
    _write(root / "README.md", "public bytes\n")
    _write(
        root / ".github" / "workflows" / "linux-experimental.yml",
        "name: Linux experimental compatibility\n",
    )
    _write(root / "bin" / "tool", "#!/bin/sh\nexit 0\n", executable=True)
    _write(root / "docs" / "space name.txt", "space-safe\n")
    _write(root / "config" / "public-release-allowlist.toml", "schema_version = 1\n")
    _git(root, "add", "--all")
    _git(
        root,
        "commit",
        "-m",
        "public candidate",
        env={
            "GIT_AUTHOR_DATE": "1999-12-31T23:59:59-05:00",
            "GIT_COMMITTER_DATE": "2026-07-23T01:15:24+08:00",
        },
    )
    return root


def _forbidden_file(tmp_path: Path) -> Path:
    path = tmp_path / "private-terms.txt"
    path.write_text("private-project-term\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _receipt_data(root: Path, forbidden_file: Path) -> dict[str, object]:
    source = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    names = _git(root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    total_bytes = sum(
        len(
            subprocess.run(
                ["git", "-C", str(root), "show", f"HEAD:{name}"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ).stdout
        )
        for name in names
    )
    manifest = root / "docs" / "assets" / "manifest.json"
    return {
        "schema_version": 1,
        "auditor": "project-memory-hub-public-tree",
        "policy_version": 1,
        "mode": "tree",
        "source_commit": source,
        "tree": tree,
        "allowlist_sha256": _sha256(
            (root / "config" / "public-release-allowlist.toml").read_bytes()
        ),
        "forbidden_terms_sha256": _sha256(forbidden_file.read_bytes()),
        "manifest_sha256": _sha256(manifest.read_bytes() if manifest.is_file() else b""),
        "file_count": len(names),
        "total_bytes": total_bytes,
    }


def _write_receipt(path: Path, receipt: dict[str, object]) -> None:
    path.write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _subject() -> ModuleType:
    try:
        return importlib.import_module("scripts.prepare_public_snapshot")
    except ModuleNotFoundError:
        pytest.fail(f"snapshot builder is missing: {SCRIPT}")


def _source_state(root: Path) -> tuple[str, str, str, bytes, str]:
    git_dir = Path(_git(root, "rev-parse", "--absolute-git-dir"))
    return (
        _git(root, "rev-parse", "HEAD"),
        _git(root, "symbolic-ref", "-q", "HEAD"),
        _git(root, "write-tree"),
        (git_dir / "index").read_bytes(),
        _git(root, "for-each-ref", "--format=%(refname)%00%(objectname)"),
    )


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    receipt_path: Path,
    worktree: Path,
    forbidden_file: Path,
    *,
    audit: Callable[..., dict[str, object]] | None = None,
    source: str | None = None,
    branch: str = BRANCH,
) -> Any:
    subject = _subject()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if audit is None:

        def audit(**_kwargs: object) -> dict[str, object]:
            return dict(receipt)

    monkeypatch.setattr(subject, "audit_public_tree", audit)
    return subject.prepare_public_snapshot(
        repository=root,
        source=source or _git(root, "rev-parse", "HEAD"),
        receipt_path=receipt_path,
        branch=branch,
        worktree=worktree,
        forbidden_file=forbidden_file,
        allowlist_file=root / "config" / "public-release-allowlist.toml",
    )


def _fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, dict[str, object]]:
    root = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    receipt = _receipt_data(root, forbidden)
    receipt_path = tmp_path / "receipt.json"
    _write_receipt(receipt_path, receipt)
    return root, forbidden, receipt_path, receipt


def test_builds_deterministic_single_root_snapshot_and_reaudits_exact_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, receipt = _fixture(tmp_path)
    worktree = tmp_path / "public-worktree"
    before = _source_state(root)
    calls: list[dict[str, object]] = []

    def audit(**kwargs: object) -> dict[str, object]:
        calls.append(dict(kwargs))
        return dict(receipt)

    result = _invoke(
        monkeypatch,
        root,
        receipt_path,
        worktree,
        forbidden,
        audit=audit,
    )

    assert calls == [
        {
            "repository": root.resolve(),
            "ref": receipt["source_commit"],
            "mode": "tree",
            "forbidden_file": forbidden,
            "allowlist_file": root / "config" / "public-release-allowlist.toml",
            "receipt_path": None,
        }
    ]
    assert result.branch == BRANCH
    assert result.commit == _git(root, "rev-parse", BRANCH)
    assert result.tree == receipt["tree"]
    assert result.worktree == worktree
    assert _git(root, "rev-list", "--count", BRANCH) == "1"
    assert _git(root, "show", "-s", "--format=%P", BRANCH) == ""
    assert _git(root, "show", "-s", "--format=%an <%ae>", BRANCH) == FIXED_IDENTITY
    assert _git(root, "show", "-s", "--format=%cn <%ce>", BRANCH) == FIXED_IDENTITY
    assert _git(root, "show", "-s", "--format=%aI", BRANCH) == "2026-07-22T00:00:00Z"
    assert _git(root, "show", "-s", "--format=%cI", BRANCH) == "2026-07-22T00:00:00Z"
    assert _git(root, "show", "-s", "--format=%s", BRANCH) == FIXED_MESSAGE
    assert _git(root, "rev-parse", f"{BRANCH}^{{tree}}") == receipt["tree"]
    assert _git(worktree, "write-tree") == receipt["tree"]
    assert (worktree / "README.md").read_bytes() == b"public bytes\n"
    assert stat.S_IMODE((worktree / "bin" / "tool").stat().st_mode) == 0o755
    assert not (worktree / "private-history.txt").exists()
    after = _source_state(root)
    assert after[:4] == before[:4]
    assert sorted(after[4].splitlines()) == sorted(
        [
            *before[4].splitlines(),
            f"refs/heads/{BRANCH}\x00{result.commit}",
        ]
    )


def test_root_commit_is_stable_for_the_same_source(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    source = _git(root, "rev-parse", "HEAD")
    tree = _git(root, "rev-parse", "HEAD^{tree}")
    subject = _subject()

    first = subject._create_root_commit(root, tree, source)
    second = subject._create_root_commit(root, tree, source)

    assert first == second


def test_public_snapshot_preserves_source_linux_workflow_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, receipt = _fixture(tmp_path)
    workflow_path = Path(".github/workflows/linux-experimental.yml")
    expected = (root / workflow_path).read_bytes()
    worktree = tmp_path / "public-worktree"

    result = _invoke(
        monkeypatch,
        root,
        receipt_path,
        worktree,
        forbidden,
    )
    committed = subprocess.run(
        ["git", "-C", str(root), "show", f"{BRANCH}:{workflow_path.as_posix()}"],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=10,
    ).stdout

    assert result.tree == receipt["tree"]
    assert _git(root, "rev-parse", f"{BRANCH}^{{tree}}") == receipt["tree"]
    assert (worktree / workflow_path).read_bytes() == expected
    assert committed == expected


@pytest.mark.parametrize(
    "raw",
    [
        b"tree " + b"1" * 40 + b"\ncommitter Test <test@example.invalid> invalid\n\nmessage\n",
        b"tree "
        + b"1" * 40
        + b"\ncommitter Test <test@example.invalid> 999999999999 +0000\n\nmessage\n",
        b"tree "
        + b"1" * 40
        + b"\ncommitter Test <test@example.invalid> 1784764800 +2400\n\nmessage\n",
        b"tree "
        + b"1" * 40
        + b"\ncommitter One <one@example.invalid> 1784764800 +0000"
        + b"\ncommitter Two <two@example.invalid> 1784764800 +0000\n\nmessage\n",
    ],
)
def test_rejects_invalid_source_committer_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: bytes,
) -> None:
    subject = _subject()

    def fake_run(_repository: Path, *arguments: str, **_kwargs: object) -> bytes:
        if arguments[:2] == ("cat-file", "-s"):
            return f"{len(raw)}\n".encode("ascii")
        return raw

    monkeypatch.setattr(subject, "_run_git", fake_run)

    with pytest.raises(subject.PublicSnapshotError, match="source commit metadata"):
        subject._public_snapshot_date(tmp_path, "1" * 40)


def test_rejects_oversized_source_commit_before_reading_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subject = _subject()
    calls: list[tuple[str, ...]] = []

    def fake_run(_repository: Path, *arguments: str, **_kwargs: object) -> bytes:
        calls.append(arguments)
        assert arguments[:2] == ("cat-file", "-s")
        return f"{subject.MAX_SOURCE_COMMIT_BYTES + 1}\n".encode("ascii")

    monkeypatch.setattr(subject, "_run_git", fake_run)

    with pytest.raises(subject.PublicSnapshotError, match="source commit metadata"):
        subject._public_snapshot_date(tmp_path, "1" * 40)

    assert calls == [("cat-file", "-s", "1" * 40)]


def test_builds_snapshot_when_repository_path_contains_non_ascii(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    localized_root = tmp_path / "私人源码"
    root.rename(localized_root)
    forbidden = _forbidden_file(tmp_path)
    receipt = _receipt_data(localized_root, forbidden)
    receipt_path = tmp_path / "receipt.json"
    _write_receipt(receipt_path, receipt)
    worktree = tmp_path / "public-worktree"

    result = _invoke(
        monkeypatch,
        localized_root,
        receipt_path,
        worktree,
        forbidden,
    )

    assert result.worktree == worktree
    assert _git(localized_root, "rev-list", "--count", BRANCH) == "1"
    assert _git(localized_root, "rev-parse", f"{BRANCH}^{{tree}}") == receipt["tree"]
    assert _git(worktree, "write-tree") == receipt["tree"]
    assert (worktree / "README.md").read_bytes() == b"public bytes\n"


def test_public_api_does_not_allow_auditor_injection() -> None:
    subject = _subject()

    assert "auditor" not in inspect.signature(subject.prepare_public_snapshot).parameters


def test_cli_integrates_with_real_auditor_and_default_allowlist(tmp_path: Path) -> None:
    root = _repository(tmp_path)
    forbidden = _forbidden_file(tmp_path)
    receipt_path = tmp_path / "real-receipt.json"
    source = _git(root, "rev-parse", "HEAD")
    subject = _subject()
    subject.audit_public_tree(
        repository=root,
        ref=source,
        mode="tree",
        forbidden_file=forbidden,
        allowlist_file=root / "config" / "public-release-allowlist.toml",
        receipt_path=receipt_path,
    )
    worktree = tmp_path / "real-public-worktree"

    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--source",
            source,
            "--receipt",
            str(receipt_path),
            "--branch",
            BRANCH,
            "--worktree",
            str(worktree),
            "--forbidden-file",
            str(forbidden),
        ],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr.decode(errors="replace")
    assert json.loads(completed.stdout) == {
        "branch": BRANCH,
        "commit": _git(root, "rev-parse", BRANCH),
        "tree": _git(root, "rev-parse", "HEAD^{tree}"),
        "worktree": str(worktree),
    }


@pytest.mark.parametrize("dirty_kind", ["staged", "tracked", "untracked"])
def test_rejects_dirty_source_without_creating_ref_or_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dirty_kind: str,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    if dirty_kind == "staged":
        (root / "README.md").write_text("staged\n", encoding="utf-8")
        _git(root, "add", "README.md")
    elif dirty_kind == "tracked":
        (root / "README.md").write_text("tracked\n", encoding="utf-8")
    else:
        (root / "untracked.txt").write_text("untracked\n", encoding="utf-8")
    worktree = tmp_path / "public-worktree"
    before = _source_state(root)

    with pytest.raises(Exception, match="clean"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert _source_state(root) == before
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""
    assert not worktree.exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_rejects_hidden_index_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    _git(root, "update-index", flag, "README.md")

    with pytest.raises(Exception, match="index flag"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
        )

    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("schema_version", 2),
        ("auditor", "other-auditor"),
        ("policy_version", 2),
        ("mode", "snapshot"),
        ("source_commit", "1" * 40),
        ("tree", "2" * 40),
        ("allowlist_sha256", "3" * 64),
        ("forbidden_terms_sha256", "4" * 64),
        ("manifest_sha256", "5" * 64),
        ("file_count", -1),
        ("total_bytes", -1),
    ],
)
def test_rejects_forged_or_noncanonical_receipt_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    replacement: object,
) -> None:
    root, forbidden, receipt_path, receipt = _fixture(tmp_path)
    forged = dict(receipt)
    forged[field] = replacement
    _write_receipt(receipt_path, forged)

    with pytest.raises(Exception, match="receipt"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
            audit=lambda **_kwargs: dict(receipt),
        )

    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


def test_rejects_receipt_with_duplicate_json_key_before_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, receipt = _fixture(tmp_path)
    serialized = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
    receipt_path.write_text(
        serialized[:-1] + ',"tree":"' + str(receipt["tree"]) + '"}\n',
        encoding="utf-8",
    )
    audited = False

    def audit(**_kwargs: object) -> dict[str, object]:
        nonlocal audited
        audited = True
        return dict(receipt)

    with pytest.raises(Exception, match="duplicate"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
            audit=audit,
        )

    assert not audited


def test_rejects_receipt_replaced_after_initial_lstat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    replacement = tmp_path / "replacement-receipt.json"
    replacement.write_bytes(receipt_path.read_bytes())
    displaced = tmp_path / "displaced-receipt.json"
    original_stat = subject.os.stat
    raced = False

    def racing_stat(path: object, *args: object, **kwargs: object) -> object:
        nonlocal raced
        metadata = original_stat(path, *args, **kwargs)
        selected = Path(os.fsdecode(cast(Any, path)))
        if not raced and selected == receipt_path:
            raced = True
            receipt_path.rename(displaced)
            receipt_path.symlink_to(replacement)
        return metadata

    monkeypatch.setattr(subject.os, "stat", racing_stat)

    with pytest.raises(Exception, match="receipt"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
        )

    assert raced
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


def test_rejects_receipt_when_fresh_audit_differs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, receipt = _fixture(tmp_path)
    different = dict(receipt)
    different["manifest_sha256"] = "f" * 64

    with pytest.raises(Exception, match="fresh audit"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
            audit=lambda **_kwargs: different,
        )


@pytest.mark.parametrize("source_kind", ["short", "not-head"])
def test_source_must_be_full_oid_and_current_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    source = (
        _git(root, "rev-parse", "--short", "HEAD")
        if source_kind == "short"
        else _git(root, "rev-parse", "HEAD^")
    )

    with pytest.raises(Exception, match="source"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
            source=source,
        )


def test_rejects_existing_fixed_ref_without_moving_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    original = _git(root, "rev-parse", "HEAD^")
    _git(root, "branch", BRANCH, original)
    before = _source_state(root)

    with pytest.raises(Exception, match="already exists"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
        )

    assert _source_state(root) == before
    assert _git(root, "rev-parse", BRANCH) == original


def test_rejects_wrong_branch_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    with pytest.raises(Exception, match="branch"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
            branch="codex/not-approved",
        )


def test_worktree_path_must_be_absolute_new_and_outside_all_existing_worktrees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    existing = tmp_path / "linked"
    _git(root, "worktree", "add", "-b", "linked", str(existing), "HEAD")
    cases = [Path("relative"), root / "nested", existing / "nested"]
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    cases.append(occupied)

    for candidate in cases:
        with pytest.raises(Exception, match="worktree"):
            _invoke(monkeypatch, root, receipt_path, candidate, forbidden)

    assert not (root / "nested").exists()
    assert not (existing / "nested").exists()
    assert occupied.is_dir()


def test_rejects_worktree_path_with_symlink_component(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(Exception, match="symlink"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            linked_parent / "public-worktree",
            forbidden,
        )

    assert not (real_parent / "public-worktree").exists()


def test_rechecks_worktree_parent_without_following_a_symlink_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    snapshot_parent = tmp_path / "snapshot-parent"
    snapshot_parent.mkdir()
    moved_parent = tmp_path / "snapshot-parent-before-race"
    attacker_parent = tmp_path / "attacker-parent"
    attacker_parent.mkdir()
    worktree = snapshot_parent / "public-worktree"
    original = subject._create_root_commit

    def race_parent(repository: Path, tree: str, source: str) -> str:
        commit = cast(str, original(repository, tree, source))
        snapshot_parent.rename(moved_parent)
        snapshot_parent.symlink_to(attacker_parent, target_is_directory=True)
        return commit

    monkeypatch.setattr(subject, "_create_root_commit", race_parent)

    with pytest.raises(Exception, match="symlink|parent"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert not (attacker_parent / "public-worktree").exists()
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


def test_head_race_after_audit_aborts_without_reverting_racer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, receipt = _fixture(tmp_path)
    old_head = _git(root, "rev-parse", "HEAD")

    def racing_audit(**_kwargs: object) -> dict[str, object]:
        (root / "racer.txt").write_text("raced\n", encoding="utf-8")
        _git(root, "add", "racer.txt")
        _git(root, "commit", "-m", "concurrent source update")
        return dict(receipt)

    worktree = tmp_path / "public-worktree"
    with pytest.raises(Exception, match="source.*changed"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            worktree,
            forbidden,
            audit=racing_audit,
        )

    assert _git(root, "rev-parse", "HEAD") != old_head
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""
    assert not worktree.exists()


def test_rejects_tracked_file_replaced_between_lstat_and_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    readme = root / "README.md"
    external = tmp_path / "external-readme"
    external.write_bytes(readme.read_bytes())
    readme_oid = _git(root, "rev-parse", "HEAD:README.md")
    original_run = subject._run_git
    original_validate = subject._validate_source_clean
    races = 0

    def racing_run(repository: Path, *args: str, **kwargs: object) -> bytes:
        nonlocal races
        result = cast(bytes, original_run(repository, *args, **kwargs))
        if repository == root and args[:3] == ("cat-file", "blob", readme_oid):
            readme.unlink()
            readme.symlink_to(external)
            races += 1
        return result

    def racing_validate(repository: Path, source: str, tree: str) -> None:
        if readme.is_symlink():
            readme.unlink()
            readme.write_bytes(b"public bytes\n")
        original_validate(repository, source, tree)

    monkeypatch.setattr(subject, "_run_git", racing_run)
    monkeypatch.setattr(subject, "_validate_source_clean", racing_validate)

    with pytest.raises(Exception, match="tracked file|clean"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
        )

    assert races >= 1
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


def test_hooks_and_clean_smudge_filters_are_never_executed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _repository(tmp_path)
    (root / ".gitattributes").write_text("README.md filter=sentinel\n", encoding="utf-8")
    _git(root, "add", ".gitattributes")
    _git(root, "commit", "-m", "declare filter")
    forbidden = _forbidden_file(tmp_path)
    receipt = _receipt_data(root, forbidden)
    receipt_path = tmp_path / "receipt.json"
    _write_receipt(receipt_path, receipt)
    sentinel = tmp_path / "forbidden-side-effect"
    command = tmp_path / "filter-command"
    command.write_text(f"#!/bin/sh\n: > '{sentinel}'\ncat\n", encoding="utf-8")
    command.chmod(0o700)
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("post-checkout", "post-commit", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\n: > '{sentinel}'\n", encoding="utf-8")
        hook.chmod(0o700)
    _git(root, "config", "core.hooksPath", str(hooks))
    _git(root, "config", "filter.sentinel.clean", str(command))
    _git(root, "config", "filter.sentinel.smudge", str(command))
    _git(root, "config", "filter.sentinel.required", "true")
    subject = _subject()
    original = subject._run_git

    def guarded_run(repository: Path, *args: str, **kwargs: object) -> bytes:
        result = original(repository, *args, **kwargs)
        assert not sentinel.exists(), f"git {args[0]} invoked a hook or filter"
        return cast(bytes, result)

    monkeypatch.setattr(subject, "_run_git", guarded_run)

    _invoke(
        monkeypatch,
        root,
        receipt_path,
        tmp_path / "public-worktree",
        forbidden,
    )

    assert not sentinel.exists()


def test_repo_local_fsmonitor_is_disabled_and_lazy_fetch_is_forbidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    sentinel = tmp_path / "fsmonitor-side-effect"
    monitor = tmp_path / "fsmonitor-command"
    monitor.write_text(
        f"#!/bin/sh\n: > '{sentinel}'\nexit 0\n",
        encoding="utf-8",
    )
    monitor.chmod(0o700)
    _git(root, "config", "core.fsmonitor", str(monitor))
    subject = _subject()
    original_run = subject.subprocess.run
    source = _git(root, "rev-parse", "HEAD")
    observed_commands: list[tuple[str, ...]] = []
    observed_lazy_fetch: list[str | None] = []

    def guarded_subprocess_run(
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        environment = cast(dict[str, str] | None, kwargs.get("env"))
        if environment is None:
            return cast(
                subprocess.CompletedProcess[bytes],
                original_run(command, **kwargs),
            )
        observed_commands.append(tuple(command))
        observed_lazy_fetch.append(environment.get("GIT_NO_LAZY_FETCH"))
        return cast(
            subprocess.CompletedProcess[bytes],
            original_run(command, **kwargs),
        )

    monkeypatch.setattr(subject.subprocess, "run", guarded_subprocess_run)

    _invoke(
        monkeypatch,
        root,
        receipt_path,
        tmp_path / "public-worktree",
        forbidden,
        source=source,
    )

    assert observed_commands
    assert all("core.fsmonitor=false" in command for command in observed_commands)
    assert observed_lazy_fetch and set(observed_lazy_fetch) == {"1"}
    assert not sentinel.exists()


@pytest.mark.parametrize("replacement_kind", ["symlink", "directory"])
@pytest.mark.parametrize("race_timing", ["before-add", "after-add"])
def test_worktree_path_replacement_never_receives_git_writes_or_gets_deleted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement_kind: str,
    race_timing: str,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    original = subject._run_git
    worktree = tmp_path / "public-worktree"
    displaced = tmp_path / "reserved-before-race"
    attacker = tmp_path / "attacker-target"
    raced = False

    def replace_public_path() -> None:
        nonlocal raced
        if os.path.lexists(worktree):
            worktree.rename(displaced)
        if replacement_kind == "symlink":
            attacker.mkdir()
            worktree.symlink_to(attacker, target_is_directory=True)
        else:
            worktree.mkdir()
            (worktree / "winner.txt").write_text("concurrent winner\n", encoding="utf-8")
        raced = True

    def racing_run(repository: Path, *args: str, **kwargs: object) -> bytes:
        if args[:2] == ("worktree", "add") and race_timing == "before-add":
            replace_public_path()
        result = cast(bytes, original(repository, *args, **kwargs))
        if args[:2] == ("worktree", "add") and race_timing == "after-add":
            replace_public_path()
        return result

    monkeypatch.setattr(subject, "_run_git", racing_run)

    with pytest.raises(Exception, match="worktree|publish|changed|replaced"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert raced
    if replacement_kind == "symlink":
        assert worktree.is_symlink()
        assert list(attacker.iterdir()) == []
    else:
        assert worktree.is_dir()
        assert (worktree / "winner.txt").read_text(encoding="utf-8") == "concurrent winner\n"
        assert not (worktree / ".git").exists()
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


def test_rollback_ref_cas_still_runs_when_path_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    worktree = tmp_path / "public-worktree"
    original_run = subject._run_git

    def cleanup_failing_git(
        repository: Path,
        *args: str,
        **kwargs: object,
    ) -> bytes:
        if args[:2] == ("worktree", "remove"):
            raise subject.PublicSnapshotError("injected git cleanup failure")
        return cast(bytes, original_run(repository, *args, **kwargs))

    monkeypatch.setattr(
        subject,
        "_materialize_tree",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subject.PublicSnapshotError("injected materialization failure")
        ),
    )
    monkeypatch.setattr(
        subject,
        "_isolate_and_remove_owned_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            subject.PublicSnapshotError("injected cleanup failure")
        ),
    )
    monkeypatch.setattr(subject, "_run_git", cleanup_failing_git)

    with pytest.raises(Exception, match="materialization"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


def test_cleanup_window_never_deletes_a_replacement_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    worktree = tmp_path / "public-worktree"
    displaced = tmp_path / "owned-worktree-before-cleanup-race"
    original_publish = subject._publish_worktree
    original_rename = subject._rename_directory_noreplace
    published = False
    raced = False

    def publish_then_fail(*args: object, **kwargs: object) -> None:
        nonlocal published
        original_publish(*args, **kwargs)
        published = True
        raise subject.PublicSnapshotError("injected after publication")

    def racing_rename(
        source_parent_fd: int,
        source_name: bytes,
        destination_parent_fd: int,
        destination_name: bytes,
        *,
        operation: str = "worktree publish",
    ) -> None:
        nonlocal raced
        if (
            published
            and not raced
            and operation == "snapshot rollback isolation"
            and source_name == os.fsencode(worktree.name)
        ):
            worktree.rename(displaced)
            worktree.mkdir()
            (worktree / "winner.txt").write_text("concurrent winner\n", encoding="utf-8")
            raced = True
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            operation=operation,
        )

    monkeypatch.setattr(subject, "_publish_worktree", publish_then_fail)
    monkeypatch.setattr(subject, "_rename_directory_noreplace", racing_rename)

    with pytest.raises(Exception, match="publication"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert raced
    assert (worktree / "winner.txt").read_text(encoding="utf-8") == "concurrent winner\n"
    assert (displaced / "README.md").read_bytes() == b"public bytes\n"
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


@pytest.mark.parametrize("entry_kind", ["file", "directory"])
def test_recursive_cleanup_isolates_children_before_deleting_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_kind: str,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    worktree = tmp_path / "public-worktree"
    original_publish = subject._publish_worktree
    original_rename = subject._rename_directory_noreplace
    raced = False

    def publish_then_fail(*args: object, **kwargs: object) -> None:
        original_publish(*args, **kwargs)
        raise subject.PublicSnapshotError("injected after publication")

    def racing_rename(
        source_parent_fd: int,
        source_name: bytes,
        destination_parent_fd: int,
        destination_name: bytes,
        *,
        operation: str = "worktree publish",
    ) -> None:
        nonlocal raced
        selected = b"README.md" if entry_kind == "file" else b"docs"
        expected_operation = (
            "snapshot rollback entry isolation"
            if entry_kind == "file"
            else "snapshot rollback isolation"
        )
        if not raced and operation == expected_operation and source_name == selected:
            owned_name = selected + b".owned"
            os.rename(
                selected,
                owned_name,
                src_dir_fd=source_parent_fd,
                dst_dir_fd=source_parent_fd,
            )
            if entry_kind == "file":
                descriptor = os.open(
                    selected,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=source_parent_fd,
                )
                try:
                    os.write(descriptor, b"concurrent winner\n")
                finally:
                    os.close(descriptor)
            else:
                os.mkdir(selected, mode=0o700, dir_fd=source_parent_fd)
                child_fd = os.open(
                    selected,
                    os.O_RDONLY | os.O_DIRECTORY,
                    dir_fd=source_parent_fd,
                )
                try:
                    winner_fd = os.open(
                        b"winner.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=child_fd,
                    )
                    try:
                        os.write(winner_fd, b"concurrent winner\n")
                    finally:
                        os.close(winner_fd)
                finally:
                    os.close(child_fd)
            raced = True
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            operation=operation,
        )

    monkeypatch.setattr(subject, "_publish_worktree", publish_then_fail)
    monkeypatch.setattr(subject, "_rename_directory_noreplace", racing_rename)

    with pytest.raises(Exception, match="publication"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert raced
    rollback_roots = [
        path
        for path in tmp_path.iterdir()
        if path.is_dir() and path.name.startswith(".pmh-remove-")
    ]
    assert len(rollback_roots) == 1
    rollback_root = rollback_roots[0]
    if entry_kind == "file":
        assert (rollback_root / "README.md").read_bytes() == b"concurrent winner\n"
        assert (rollback_root / "README.md.owned").read_bytes() == b"public bytes\n"
    else:
        assert (rollback_root / "docs" / "winner.txt").read_bytes() == b"concurrent winner\n"
        assert (rollback_root / "docs.owned" / "space name.txt").read_bytes() == b"space-safe\n"
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_publish_rename_interrupt_is_inferred_and_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    worktree = tmp_path / "public-worktree"
    original_rename = subject._rename_directory_noreplace
    interrupted = False

    def interrupt_after_publish_rename(
        source_parent_fd: int,
        source_name: bytes,
        destination_parent_fd: int,
        destination_name: bytes,
        *,
        operation: str = "worktree publish",
    ) -> None:
        nonlocal interrupted
        original_rename(
            source_parent_fd,
            source_name,
            destination_parent_fd,
            destination_name,
            operation=operation,
        )
        if not interrupted and operation == "worktree publish" and source_name == b"worktree":
            interrupted = True
            raise interrupt()

    monkeypatch.setattr(subject, "_rename_directory_noreplace", interrupt_after_publish_rename)

    with pytest.raises(interrupt):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert interrupted
    assert not worktree.exists()
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""
    assert str(worktree) not in _git(root, "worktree", "list", "--porcelain")


def test_post_publish_ref_replacement_is_detected_and_winner_is_preserved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    worktree = tmp_path / "public-worktree"
    competitor = _git(root, "rev-parse", "HEAD")
    original_publish = subject._publish_worktree

    def publish_then_replace_ref(*args: object, **kwargs: object) -> None:
        original_publish(*args, **kwargs)
        _git(root, "update-ref", f"refs/heads/{BRANCH}", competitor)

    monkeypatch.setattr(subject, "_publish_worktree", publish_then_replace_ref)

    with pytest.raises(Exception, match="snapshot ref changed"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert _git(root, "rev-parse", BRANCH) == competitor
    assert not worktree.exists()
    assert str(worktree) not in _git(root, "worktree", "list", "--porcelain")


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_base_exception_after_ref_creation_rolls_back_then_propagates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: type[BaseException],
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    worktree = tmp_path / "public-worktree"

    def interrupt_materialization(*_args: object, **_kwargs: object) -> None:
        raise interrupt()

    monkeypatch.setattr(subject, "_materialize_tree", interrupt_materialization)

    with pytest.raises(interrupt):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""
    assert not worktree.exists()
    assert str(worktree) not in _git(root, "worktree", "list", "--porcelain")


def test_rejects_replaced_worktree_admin_directory_before_index_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    original = subject._run_git
    attacker_metadata = tmp_path / "attacker-metadata"
    displaced_metadata = tmp_path / "owned-metadata-before-race"
    raced = False

    def racing_run(repository: Path, *args: str, **kwargs: object) -> bytes:
        nonlocal raced
        result = cast(bytes, original(repository, *args, **kwargs))
        if not raced and args[:2] == ("worktree", "add"):
            worktree_path = Path(args[-2])
            marker = (worktree_path / ".git").read_text(encoding="utf-8").strip()
            admin = Path(marker.removeprefix("gitdir: "))
            shutil.copytree(admin, attacker_metadata)
            admin.rename(displaced_metadata)
            admin.symlink_to(attacker_metadata, target_is_directory=True)
            raced = True
        return result

    monkeypatch.setattr(subject, "_run_git", racing_run)

    with pytest.raises(Exception, match="metadata"):
        _invoke(
            monkeypatch,
            root,
            receipt_path,
            tmp_path / "public-worktree",
            forbidden,
        )

    assert raced
    assert attacker_metadata.is_dir()
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""


@pytest.mark.parametrize(
    "failure_point",
    ["worktree-add", "metadata-bind", "materialize"],
)
def test_fault_injection_rolls_back_only_created_ref_and_worktree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    before = _source_state(root)
    before_worktrees = _git(root, "worktree", "list", "--porcelain")
    worktree = tmp_path / "public-worktree"
    if failure_point == "worktree-add":
        original = subject._run_git

        def failing_run(repository: Path, *args: str, **kwargs: object) -> bytes:
            if args[:2] == ("worktree", "add"):
                raise subject.PublicSnapshotError("injected worktree failure")
            return cast(bytes, original(repository, *args, **kwargs))

        monkeypatch.setattr(subject, "_run_git", failing_run)
    elif failure_point == "metadata-bind":
        original_bind = subject._bind_worktree_metadata
        bind_attempts = 0

        def fail_first_bind(*args: object, **kwargs: object) -> object:
            nonlocal bind_attempts
            bind_attempts += 1
            if bind_attempts == 1:
                raise subject.PublicSnapshotError("injected metadata bind failure")
            return original_bind(*args, **kwargs)

        monkeypatch.setattr(subject, "_bind_worktree_metadata", fail_first_bind)
    else:
        monkeypatch.setattr(
            subject,
            "_materialize_tree",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                subject.PublicSnapshotError("injected materialization failure")
            ),
        )

    with pytest.raises(Exception, match="injected"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert _source_state(root) == before
    assert _git(root, "show-ref", "--verify", f"refs/heads/{BRANCH}", check=False) == ""
    assert not worktree.exists()
    assert _git(root, "worktree", "list", "--porcelain") == before_worktrees
    assert not list(tmp_path.glob(".pmh-public-snapshot-*"))


def test_concurrent_ref_winner_is_preserved_and_cas_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    original = subject._run_git
    competitor = _git(root, "rev-parse", "HEAD")
    raced = False

    def racing_run(repository: Path, *args: str, **kwargs: object) -> bytes:
        nonlocal raced
        if (
            not raced
            and args[:2] == ("update-ref", f"refs/heads/{BRANCH}")
            and args[-1] == ZERO_OID
        ):
            raced = True
            original(repository, "update-ref", f"refs/heads/{BRANCH}", competitor, ZERO_OID)
        return cast(bytes, original(repository, *args, **kwargs))

    monkeypatch.setattr(subject, "_run_git", racing_run)
    worktree = tmp_path / "public-worktree"

    with pytest.raises(Exception, match="update-ref"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert raced
    assert _git(root, "rev-parse", BRANCH) == competitor
    assert not worktree.exists()


def test_rollback_cas_does_not_delete_ref_stolen_after_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, forbidden, receipt_path, _receipt = _fixture(tmp_path)
    subject = _subject()
    original = subject._run_git
    competitor = _git(root, "rev-parse", "HEAD")

    def steal_then_fail(*_args: object, **_kwargs: object) -> None:
        original(root, "update-ref", f"refs/heads/{BRANCH}", competitor)
        raise subject.PublicSnapshotError("injected after ref theft")

    monkeypatch.setattr(subject, "_materialize_tree", steal_then_fail)
    worktree = tmp_path / "public-worktree"

    with pytest.raises(Exception, match="injected"):
        _invoke(monkeypatch, root, receipt_path, worktree, forbidden)

    assert _git(root, "rev-parse", BRANCH) == competitor
    assert not worktree.exists()
    assert str(worktree) not in _git(root, "worktree", "list", "--porcelain")


def test_git_subprocess_allowlist_has_no_network_or_publication_commands() -> None:
    subject = _subject()
    assert subject.ALLOWED_GIT_SUBCOMMANDS == frozenset(
        {
            "cat-file",
            "commit-tree",
            "for-each-ref",
            "ls-files",
            "ls-tree",
            "read-tree",
            "rev-parse",
            "show-ref",
            "symbolic-ref",
            "update-ref",
            "worktree",
            "write-tree",
        }
    )
    assert subject.ALLOWED_GIT_SUBCOMMANDS.isdisjoint(
        {"remote", "push", "fetch", "tag", "merge", "gh", "release"}
    )
