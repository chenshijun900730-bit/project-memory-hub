from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from project_memory_hub.improvement.git_apply import (
    PatchValidator,
    UnsafeGitPatch,
)


VALID_MODIFY = """\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-seed
+updated
"""

VALID_ADD = """\
diff --git a/new.txt b/new.txt
new file mode 100644
--- /dev/null
+++ b/new.txt
@@ -0,0 +1 @@
+new
"""


def _git(root: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=5,
    )
    return result.stdout.strip()


def _repository(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test User")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial")
    return root, _git(root, "rev-parse", "HEAD")


def _git_topology(root: Path) -> tuple[str, str]:
    return (
        _git(root, "for-each-ref", "--format=%(refname):%(objectname)"),
        _git(root, "worktree", "list", "--porcelain"),
    )


def test_validator_accepts_only_ordinary_text_modify_and_add(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)
    validator = PatchValidator(max_patch_bytes=8_192, max_files=2)

    modified = validator.validate(VALID_MODIFY, root, base)
    added = validator.validate(VALID_ADD, root, base)

    assert modified.paths == ("README.md",)
    assert modified.patch_bytes == VALID_MODIFY.encode("utf-8")
    assert added.paths == ("new.txt",)
    assert added.patch_bytes == VALID_ADD.encode("utf-8")


@pytest.mark.parametrize(
    "patch",
    (
        "\0" + VALID_MODIFY,
        VALID_MODIFY + "GIT binary patch\n",
        VALID_MODIFY.replace("a/README.md", "/etc/passwd"),
        VALID_MODIFY.replace("README.md", "../outside.txt"),
        VALID_MODIFY.replace("README.md", r"folder\outside.txt"),
        VALID_MODIFY.replace("README.md", ".GIT/config"),
        VALID_MODIFY.replace("README.md", "nested/.gIt/config"),
        VALID_MODIFY.replace("README.md", ".gitmodules"),
        VALID_ADD.replace("new.txt", ".proposal.patch"),
        VALID_ADD.replace("new.txt", ".proposal-hooks/post-commit"),
        VALID_MODIFY.replace(
            "diff --git a/README.md b/README.md",
            'diff --git "a/quoted name" "b/quoted name"',
        ),
        VALID_MODIFY.replace("diff --git", "diff --cc", 1),
        VALID_MODIFY.replace(
            "--- a/README.md\n+++ b/README.md",
            "rename from README.md\nrename to renamed.md\n--- a/README.md\n+++ b/renamed.md",
        ),
        VALID_MODIFY.replace(
            "--- a/README.md\n+++ b/README.md",
            "copy from README.md\ncopy to copied.md\n--- a/README.md\n+++ b/copied.md",
        ),
        VALID_MODIFY.replace(
            "--- a/README.md\n+++ b/README.md",
            "deleted file mode 100644\n--- a/README.md\n+++ /dev/null",
        ),
        VALID_MODIFY.replace(
            "--- a/README.md", "old mode 100644\nnew mode 100755\n--- a/README.md"
        ),
        VALID_ADD.replace("new file mode 100644", "new file mode 100755"),
        VALID_ADD.replace("new file mode 100644", "new file mode 120000"),
        VALID_ADD.replace("new file mode 100644", "new file mode 160000"),
    ),
)
def test_validator_rejects_unsafe_patch_shapes_before_git_mutation(
    tmp_path: Path, patch: str
) -> None:
    root, base = _repository(tmp_path)
    before = _git_topology(root)

    with pytest.raises(UnsafeGitPatch):
        PatchValidator().validate(patch, root, base)

    assert _git_topology(root) == before
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_validator_rejects_mismatched_diff_and_file_headers(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)
    patch = VALID_MODIFY.replace("+++ b/README.md", "+++ b/other.txt")

    with pytest.raises(UnsafeGitPatch):
        PatchValidator().validate(patch, root, base)


@pytest.mark.parametrize("kind", ("target_symlink", "parent_symlink", "hardlink"))
def test_validator_rejects_unsafe_filesystem_targets(tmp_path: Path, kind: str) -> None:
    root, _base = _repository(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    if kind == "target_symlink":
        target = root / "linked.txt"
        target.symlink_to(outside / "target.txt")
        _git(root, "add", "linked.txt")
        patch = VALID_MODIFY.replace("README.md", "linked.txt")
    elif kind == "parent_symlink":
        target = root / "linked"
        target.symlink_to(outside, target_is_directory=True)
        _git(root, "add", "linked")
        patch = VALID_ADD.replace("new.txt", "linked/new.txt")
    else:
        os.link(root / "README.md", outside / "README-copy.md")
        patch = VALID_MODIFY

    if kind != "hardlink":
        _git(root, "commit", "-m", f"prepare {kind}")
    base = _git(root, "rev-parse", "HEAD")

    with pytest.raises(UnsafeGitPatch):
        PatchValidator().validate(patch, root, base)

    assert not (outside / "target.txt").exists()
    assert not (outside / "new.txt").exists()


def test_validator_enforces_exact_byte_and_file_limits(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)
    combined = VALID_MODIFY + "\n" + VALID_ADD

    assert PatchValidator(max_patch_bytes=len(VALID_MODIFY.encode("utf-8")), max_files=1).validate(
        VALID_MODIFY, root, base
    ).paths == ("README.md",)

    with pytest.raises(UnsafeGitPatch):
        PatchValidator(
            max_patch_bytes=len(VALID_MODIFY.encode("utf-8")) - 1,
            max_files=1,
        ).validate(VALID_MODIFY, root, base)
    with pytest.raises(UnsafeGitPatch):
        PatchValidator(max_patch_bytes=16_384, max_files=1).validate(combined, root, base)


def test_validator_rejects_invalid_base_without_exposing_it(tmp_path: Path) -> None:
    root, _base = _repository(tmp_path)
    marker = "PRIVATE_BASE_MARKER"

    with pytest.raises(UnsafeGitPatch) as captured:
        PatchValidator().validate(VALID_MODIFY, root, marker)

    assert marker not in str(captured.value)


def test_validator_rejects_existing_executable_target(tmp_path: Path) -> None:
    root, _base = _repository(tmp_path)
    (root / "README.md").chmod(0o755)
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "make executable")
    base = _git(root, "rev-parse", "HEAD")

    with pytest.raises(UnsafeGitPatch):
        PatchValidator().validate(VALID_MODIFY, root, base)


def test_validator_rejects_path_aliases_with_case_or_unicode_collisions(
    tmp_path: Path,
) -> None:
    root, base = _repository(tmp_path)
    case_collision = (
        VALID_ADD.replace("new.txt", "Alias.txt") + "\n" + VALID_ADD.replace("new.txt", "alias.txt")
    )
    unicode_collision = (
        VALID_ADD.replace("new.txt", "caf\u00e9.txt")
        + "\n"
        + VALID_ADD.replace("new.txt", "cafe\u0301.txt")
    )

    for patch in (case_collision, unicode_collision):
        with pytest.raises(UnsafeGitPatch):
            PatchValidator().validate(patch, root, base)


def test_validator_rejects_addition_below_submodule_ancestor(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)
    (root / "vendor").mkdir()
    _git(root, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor")
    _git(root, "commit", "-m", "add synthetic gitlink")
    base = _git(root, "rev-parse", "HEAD")
    patch = VALID_ADD.replace("new.txt", "vendor/new.txt")

    with pytest.raises(UnsafeGitPatch):
        PatchValidator().validate(patch, root, base)


def test_validator_rejects_repository_root_symlink_alias(tmp_path: Path) -> None:
    root, base = _repository(tmp_path)
    alias = tmp_path / "repository-alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(UnsafeGitPatch):
        PatchValidator().validate(VALID_MODIFY, alias, base)


def test_validator_never_uses_unbounded_subprocess_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import project_memory_hub.improvement.git_apply as subject

    root, base = _repository(tmp_path)

    def reject_run(*_args, **_kwargs):
        raise AssertionError("Git validation must use the bounded process runner")

    monkeypatch.setattr(subject.subprocess, "run", reject_run)

    assert PatchValidator().validate(VALID_MODIFY, root, base).paths == ("README.md",)


def test_sandbox_profile_never_grants_broad_usr_or_device_reads(
    tmp_path: Path,
) -> None:
    import project_memory_hub.improvement.git_apply as subject

    worktree = tmp_path / "worktree"
    executable = tmp_path / "verify"
    profile = subject._sandbox_profile(
        (worktree,),
        read_paths=(executable,),
    )

    assert '(subpath "/usr")' not in profile
    assert '(subpath "/dev")' not in profile
    assert '(subpath "/usr/local")' not in profile
    assert '(subpath "/usr/bin")' in profile
    assert '(literal "/dev/null")' in profile
