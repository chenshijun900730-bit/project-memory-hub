from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_verifier() -> ModuleType:
    path = PROJECT_ROOT / "scripts/verify_release_checkout.py"
    assert path.is_file(), "release checkout verifier must exist"
    spec = importlib.util.spec_from_file_location("verify_release_checkout_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        timeout=10.0,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.name", "Release Test")
    _git(repository, "config", "user.email", "release-test@example.invalid")
    (repository / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    (repository / "tracked.txt").write_text("public\n", encoding="utf-8")
    _git(repository, "add", ".gitignore", "tracked.txt")
    _git(repository, "commit", "-qm", "initial")
    return repository, _git(repository, "rev-parse", "HEAD")


def test_release_checkout_accepts_exact_clean_head_and_ignored_files(tmp_path: Path) -> None:
    verifier = _load_verifier()
    repository, head = _repository(tmp_path)
    ignored = repository / "ignored" / "cache.bin"
    ignored.parent.mkdir()
    ignored.write_bytes(b"not part of the release tree")

    assert verifier.verify_release_checkout(repository, expected_head=head) == head


@pytest.mark.parametrize(
    "mutation",
    (
        "staged",
        "unstaged",
        "deleted",
        "untracked",
        "assume-unchanged",
        "skip-worktree",
    ),
)
def test_release_checkout_rejects_every_publishable_dirty_state(
    tmp_path: Path,
    mutation: str,
) -> None:
    verifier = _load_verifier()
    repository, head = _repository(tmp_path)
    tracked = repository / "tracked.txt"
    if mutation == "staged":
        tracked.write_text("staged private material\n", encoding="utf-8")
        _git(repository, "add", "tracked.txt")
    elif mutation == "unstaged":
        tracked.write_text("unstaged private material\n", encoding="utf-8")
    elif mutation == "deleted":
        tracked.unlink()
    elif mutation == "untracked":
        (repository / "private-canary-name.txt").write_text(
            "PRIVATE-CANARY-CONTENT\n",
            encoding="utf-8",
        )
    else:
        _git(repository, "update-index", f"--{mutation}", "tracked.txt")
        tracked.write_text(f"{mutation} private material\n", encoding="utf-8")

    with pytest.raises(verifier.ReleaseCheckoutError, match="release_checkout_dirty") as caught:
        verifier.verify_release_checkout(repository, expected_head=head)

    message = str(caught.value)
    assert "private-canary-name" not in message
    assert "PRIVATE-CANARY-CONTENT" not in message
    assert str(repository) not in message


def test_release_checkout_rejects_invalid_or_different_expected_head(tmp_path: Path) -> None:
    verifier = _load_verifier()
    repository, head = _repository(tmp_path)

    for expected, code in (
        ("HEAD", "release_checkout_expected_head_invalid"),
        ("A" * 40, "release_checkout_expected_head_invalid"),
        ("0" * 40, "release_checkout_head_mismatch"),
    ):
        with pytest.raises(verifier.ReleaseCheckoutError, match=code):
            verifier.verify_release_checkout(repository, expected_head=expected)


def test_release_checkout_rejects_non_repository_without_echoing_git_errors(
    tmp_path: Path,
) -> None:
    verifier = _load_verifier()
    canary = tmp_path / "PRIVATE-REPOSITORY-CANARY"
    canary.mkdir()

    with pytest.raises(
        verifier.ReleaseCheckoutError, match="release_checkout_git_failed"
    ) as caught:
        verifier.verify_release_checkout(canary, expected_head="0" * 40)

    assert "PRIVATE-REPOSITORY-CANARY" not in str(caught.value)
