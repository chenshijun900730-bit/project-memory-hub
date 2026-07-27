import errno
import hashlib
import os
import re
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from project_memory_hub.config import AppConfig
from project_memory_hub.discovery.fingerprint import (
    fingerprint_git_remote,
    fingerprint_manifests,
    normalize_git_remote,
)
from project_memory_hub.discovery.policy import DiscoveryPolicy
from project_memory_hub.discovery.scanner import ProjectScanner
from project_memory_hub.domain import SourceAgent


EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        "Library",
        "Downloads",
        "Applications",
        ".Trash",
        ".obsidian",
        "node_modules",
        "vendor",
        ".venv",
        "venv",
        "env",
        "Pods",
        "dist",
        "build",
        "target",
        ".next",
        ".nuxt",
        ".turbo",
        ".gradle",
        "DerivedData",
        "coverage",
        "graphify-out",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    }
)


def config_for(*roots: Path) -> AppConfig:
    return AppConfig(
        project_roots=tuple(roots),
        enabled_sources=(SourceAgent.CODEX,),
        inactive_days=21,
        max_recall_tokens=800,
        daily_reconcile_time="03:30",
    )


def add_marker(project: Path, marker: str, content: str = "") -> Path:
    project.mkdir(parents=True, exist_ok=True)
    marker_path = project / marker
    if marker == ".git":
        marker_path.mkdir()
    else:
        marker_path.write_text(content, encoding="utf-8")
    return project


def scanner_for(*roots: Path) -> ProjectScanner:
    return ProjectScanner(DiscoveryPolicy.from_config(config_for(*roots)))


def test_policy_defaults_use_only_canonical_configured_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    alias = tmp_path / "first-alias"
    alias.symlink_to(first, target_is_directory=True)
    monkeypatch.chdir(tmp_path)

    policy = DiscoveryPolicy.from_config(
        config_for(Path("first"), alias, Path("second"), Path("first"))
    )

    assert policy.allowed_roots == (first.resolve(), second.resolve())
    assert frozenset(policy.excluded_directory_names) == EXCLUDED_DIRECTORY_NAMES
    assert policy.project_markers == (
        ".git",
        "package.json",
        "pyproject.toml",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
    )
    assert policy.workspace_directory_names == (
        "apps",
        "packages",
        "services",
        "modules",
        "crates",
    )
    assert policy.max_depth == 8
    assert policy.sensitive_filename_patterns
    for sensitive_name in (
        ".env",
        ".ENV.local",
        "id_rsa",
        "server.PEM",
        ".ssh",
        "Credentials.json",
        "client_secrets.toml",
        "access-token.txt",
    ):
        assert any(
            re.search(pattern, sensitive_name, flags=re.IGNORECASE)
            for pattern in policy.sensitive_filename_patterns
        )
    with pytest.raises(FrozenInstanceError):
        policy.max_depth = 9  # type: ignore[misc]


@pytest.mark.parametrize(
    "unsafe_root",
    (Path("/"), Path.home().parent, Path.home()),
)
def test_policy_rejects_roots_that_include_the_entire_home(unsafe_root: Path) -> None:
    with pytest.raises(ValueError, match="project root is too broad"):
        DiscoveryPolicy.from_config(config_for(unsafe_root))


def test_policy_rejects_macos_aliases_of_home_and_its_physical_ancestors() -> None:
    data_root = Path("/System/Volumes/Data")
    data_home = data_root / Path.home().relative_to(Path.home().anchor)
    case_alias = Path(str(Path.home()).upper())
    if not data_home.exists() or not os.path.samefile(data_home, Path.home()):
        pytest.skip("macOS Data-volume home alias is unavailable")

    unsafe_roots = [data_root, data_home.parent, data_home]
    if case_alias.exists() and os.path.samefile(case_alias, Path.home()):
        unsafe_roots.append(case_alias)

    for unsafe_root in unsafe_roots:
        assert unsafe_root.exists()
        with pytest.raises(ValueError, match="project root is too broad"):
            DiscoveryPolicy.from_config(config_for(unsafe_root))


def test_discovery_is_bounded_and_reports_permission_issue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    add_marker(root / "git-app", ".git")
    add_marker(root / "manifest-app", "package.json", '{"name": "manifest-app"}')
    add_marker(root / "node_modules" / "ignored", "package.json", '{"name": "ignored"}')
    add_marker(root / ".venv" / "ignored", "pyproject.toml", '[project]\nname="ignored"')
    add_marker(root / "git-app" / "random" / "nested", "package.json", '{"name": "nested"}')
    blocked = root / "blocked"
    blocked.mkdir()
    real_scandir = os.scandir

    def guarded_scandir(path: os.PathLike[str] | str):
        if Path(path).resolve(strict=False) == blocked.resolve():
            raise PermissionError(errno.EACCES, "synthetic permission denial", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    result = scanner_for(root).discover()

    assert {item.display_name for item in result.candidates} == {
        "git-app",
        "manifest-app",
    }
    assert all("node_modules" not in str(item.canonical_path) for item in result.candidates)
    assert len(result.issues) == 1
    assert result.issues[0].path == blocked.resolve()
    assert result.issues[0].code == "blocked_permission"
    assert result.issues[0].remediation
    assert any(
        phrase in result.issues[0].remediation.lower()
        for phrase in ("macos", "system settings", "系统设置")
    )


def test_scanner_reports_missing_root_and_generic_os_error_without_aborting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    good = add_marker(root / "good", "package.json", '{"name": "good"}')
    broken = root / "broken"
    broken.mkdir()
    missing = tmp_path / "missing"
    real_scandir = os.scandir

    def guarded_scandir(path: os.PathLike[str] | str):
        if Path(path).resolve(strict=False) == broken.resolve():
            raise OSError(errno.EIO, "synthetic I/O failure", path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)

    result = scanner_for(missing, root).discover()

    assert tuple(item.canonical_path for item in result.candidates) == (good.resolve(),)
    assert {(issue.path, issue.code) for issue in result.issues} == {
        (broken.resolve(), "scan_error"),
        (missing.resolve(strict=False), "missing_root"),
    }
    assert tuple(issue.path for issue in result.issues) == tuple(
        sorted((issue.path for issue in result.issues), key=str)
    )


def test_scanner_skips_hidden_sensitive_and_obsidian_vault_paths(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    expected = add_marker(root / "public", "package.json", '{"name": "public"}')
    for name in (".hidden", ".SSH", ".env.backup", "Credentials", "Api_Token"):
        add_marker(root / name, "package.json", '{"name": "private"}')
    vault = add_marker(root / "vault", "package.json", '{"name": "vault"}')
    (vault / ".obsidian").mkdir()
    add_marker(vault / "packages" / "nested", "package.json", '{"name": "nested"}')

    result = scanner_for(root).discover()

    assert tuple(item.canonical_path for item in result.candidates) == (expected.resolve(),)


def test_depth_eight_is_included_and_depth_nine_is_not(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    at_eight = root
    at_nine = root
    for depth in range(1, 9):
        at_eight /= f"included-{depth}"
        at_eight.mkdir()
    for depth in range(1, 10):
        at_nine /= f"excluded-{depth}"
        at_nine.mkdir()
    (at_eight / "package.json").write_text('{"name": "at-eight"}', encoding="utf-8")
    (at_nine / "package.json").write_text('{"name": "at-nine"}', encoding="utf-8")

    result = scanner_for(root).discover()

    assert tuple(item.canonical_path for item in result.candidates) == (at_eight.resolve(),)


def test_project_root_stops_descent_except_for_workspace_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    app = add_marker(root / "app", "package.json", '{"name": "app"}')
    ignored = add_marker(app / "random" / "nested", "package.json", '{"name": "ignored"}')
    inner = add_marker(app / "packages" / "inner", "package.json", '{"name": "inner"}')

    result = scanner_for(root).discover()

    assert tuple(item.canonical_path for item in result.candidates) == tuple(
        sorted((app.resolve(), inner.resolve()), key=str)
    )
    assert ignored.resolve() not in {item.canonical_path for item in result.candidates}


def test_scanner_ignores_directory_symlinks_and_symlinked_marker_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = add_marker(tmp_path / "outside", "package.json", '{"name": "outside"}')
    (root / "escape").symlink_to(outside, target_is_directory=True)
    marker_target = tmp_path / "marker-target.json"
    marker_target.write_text('{"name": "linked"}', encoding="utf-8")
    linked_marker_project = root / "linked-marker"
    linked_marker_project.mkdir()
    (linked_marker_project / "package.json").symlink_to(marker_target)
    expected = add_marker(root / "real", "package.json", '{"name": "real"}')

    result = scanner_for(root).discover()

    assert tuple(item.canonical_path for item in result.candidates) == (expected.resolve(),)


def test_scanner_deduplicates_canonical_candidates_and_sorts_markers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = add_marker(root / "z-project", "Cargo.toml", '[package]\nname="z"')
    add_marker(project, ".git")
    add_marker(project, "package.json", '{"name": "z"}')
    first = add_marker(root / "a-project", "go.mod", "module example.com/a\n")

    result = scanner_for(project, root).discover()

    assert tuple(item.canonical_path for item in result.candidates) == tuple(
        sorted((first.resolve(), project.resolve()), key=str)
    )
    by_path = {item.canonical_path: item for item in result.candidates}
    assert by_path[project.resolve()].markers == (
        ".git",
        "package.json",
        "Cargo.toml",
    )


@pytest.mark.parametrize(
    ("raw", "normalized"),
    (
        (
            "HTTPS://user:password@Example.COM/Org/Repo.git?token=secret#fragment",
            "https://example.com/Org/Repo",
        ),
        ("git@GitHub.COM:Org/Repo.git/", "ssh://github.com/Org/Repo"),
        (
            "ssh://user:password@Example.COM:2222/Org/Repo.git/",
            "ssh://example.com:2222/Org/Repo",
        ),
    ),
)
def test_git_remote_normalization_strips_credentials_and_transport_noise(
    raw: str, normalized: str
) -> None:
    assert normalize_git_remote(raw) == normalized
    assert fingerprint_git_remote(raw) == hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint_git_remote(raw))


def test_scanner_persists_only_a_git_remote_fingerprint(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = add_marker(root / "git-project", ".git")
    raw_remote = "https://owner:super-secret@example.com/Org/Repo.git?token=hidden"
    (project / ".git" / "config").write_text(
        f'[remote "origin"]\n    url = {raw_remote}\n', encoding="utf-8"
    )

    candidate = scanner_for(root).discover().candidates[0]

    assert candidate.git_root == project.resolve()
    assert candidate.git_remote_fingerprint == fingerprint_git_remote(raw_remote)
    assert "super-secret" not in candidate.model_dump_json()


@pytest.mark.parametrize(
    "raw_remote",
    (
        "ssh://[broken/repo.git",
        "ssh://example.com:not-a-port/Org/Repo.git",
        "https:///Org/Repo.git",
    ),
)
def test_malformed_git_remote_does_not_abort_discovery_or_get_persisted(
    tmp_path: Path, raw_remote: str
) -> None:
    root = tmp_path / "root"
    project = add_marker(root / "git-project", ".git")
    (project / ".git" / "config").write_text(
        f'[remote "origin"]\n    url = {raw_remote}\n', encoding="utf-8"
    )

    result = scanner_for(root).discover()

    assert len(result.candidates) == 1
    assert result.candidates[0].canonical_path == project.resolve()
    assert result.candidates[0].git_remote_fingerprint is None
    assert raw_remote not in result.model_dump_json()


def test_git_config_symlink_is_never_followed(tmp_path: Path) -> None:
    root = tmp_path / "root"
    project = add_marker(root / "git-project", ".git")
    outside_config = tmp_path / "outside-git-config"
    raw_remote = "https://owner:secret@example.com/Org/Repo.git"
    outside_config.write_text(f'[remote "origin"]\n    url = {raw_remote}\n', encoding="utf-8")
    (project / ".git" / "config").symlink_to(outside_config)

    result = scanner_for(root).discover()

    assert len(result.candidates) == 1
    assert result.candidates[0].git_remote_fingerprint is None
    assert raw_remote not in result.model_dump_json()


def test_git_directory_replacement_cannot_escape_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    project = add_marker(root / "git-project", ".git")
    git_directory = project / ".git"
    (git_directory / "config").write_text(
        '[remote "origin"]\n    url = https://example.com/inside.git\n',
        encoding="utf-8",
    )
    outside_git = tmp_path / "outside-git"
    outside_git.mkdir()
    outside_remote = "https://owner:outside-secret@example.com/escaped.git"
    (outside_git / "config").write_text(
        f'[remote "origin"]\n    url = {outside_remote}\n', encoding="utf-8"
    )
    parked_git = project / ".git-parked"
    real_open = os.open
    raced = False
    git_directory_flags: list[int] = []

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if Path(path).name == ".git":
            git_directory_flags.append(flags)
        if Path(path).name == "config" and not raced:
            raced = True
            git_directory.rename(parked_git)
            git_directory.symlink_to(outside_git, target_is_directory=True)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)

    result = scanner_for(root).discover()

    assert raced
    assert git_directory_flags
    if hasattr(os, "O_DIRECTORY"):
        assert all(flags & os.O_DIRECTORY for flags in git_directory_flags)
    if hasattr(os, "O_NOFOLLOW"):
        assert all(flags & os.O_NOFOLLOW for flags in git_directory_flags)
    assert len(result.candidates) == 1
    assert result.candidates[0].git_remote_fingerprint is None
    assert outside_remote not in result.model_dump_json()


def test_scanner_anchors_metadata_reads_to_allowed_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    ancestor = root / "workspace"
    project = add_marker(ancestor / "project", ".git")
    add_marker(project, "package.json", '{"name":"inside"}')
    (project / ".git" / "config").write_text(
        '[remote "origin"]\n    url = https://example.com/inside.git\n',
        encoding="utf-8",
    )

    outside_ancestor = tmp_path / "outside-workspace"
    outside_project = add_marker(outside_ancestor / "project", ".git")
    outside_remote = "https://owner:outside-secret@example.com/escaped.git"
    (outside_project / ".git" / "config").write_text(
        f'[remote "origin"]\n    url = {outside_remote}\n', encoding="utf-8"
    )
    (outside_project / "package.json").write_text('{"name":"outside"}', encoding="utf-8")

    parked_ancestor = root / "workspace-parked"
    real_open = os.open
    real_close = os.close
    raced = False
    anchor_descriptors: list[int] = []
    closed_descriptors: list[int] = []
    full_project_opens: list[Path] = []

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        opened_path = Path(path)
        opens_project_by_full_path = dir_fd is None and opened_path == project
        opens_ancestor_from_anchor = dir_fd is not None and opened_path == Path(ancestor.name)
        if opens_project_by_full_path:
            full_project_opens.append(opened_path)
        if not raced and (opens_project_by_full_path or opens_ancestor_from_anchor):
            raced = True
            ancestor.rename(parked_ancestor)
            ancestor.symlink_to(outside_ancestor, target_is_directory=True)

        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if dir_fd is None and opened_path == root:
            anchor_descriptors.append(descriptor)
        return descriptor

    def recording_close(file_descriptor: int) -> None:
        closed_descriptors.append(file_descriptor)
        real_close(file_descriptor)

    monkeypatch.setattr(os, "open", racing_open)
    monkeypatch.setattr(os, "close", recording_close)

    result = scanner_for(root).discover()

    assert raced
    assert len(result.candidates) == 1
    assert result.candidates[0].canonical_path == project
    assert result.candidates[0].git_remote_fingerprint is None
    assert result.candidates[0].manifest_fingerprint is None
    assert outside_remote not in result.model_dump_json()
    assert len(anchor_descriptors) == 1
    assert not full_project_opens
    assert closed_descriptors.count(anchor_descriptors[0]) == 1


def test_git_config_uses_nofollow_descriptor_and_bounded_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    project = add_marker(root / "git-project", ".git")
    config = project / ".git" / "config"
    config.write_text(
        '[remote "origin"]\n    url = https://example.com/Org/Repo.git\n',
        encoding="utf-8",
    )
    real_open = os.open
    real_read = os.read
    open_flags: list[int] = []
    open_dir_fds: list[int | None] = []
    read_sizes: list[int] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path).name == config.name:
            open_flags.append(flags)
            open_dir_fds.append(dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def recording_read(file_descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return real_read(file_descriptor, size)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "read", recording_read)

    result = scanner_for(root).discover()

    assert result.candidates[0].git_remote_fingerprint is not None
    assert open_flags
    assert all(dir_fd is not None for dir_fd in open_dir_fds)
    if hasattr(os, "O_NOFOLLOW"):
        assert all(flags & os.O_NOFOLLOW for flags in open_flags)
    assert read_sizes
    assert sum(read_sizes) <= 256 * 1024


def test_git_config_permission_issue_uses_full_logical_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    project = add_marker(root / "git-project", ".git")
    config = project / ".git" / "config"
    config.write_text(
        '[remote "origin"]\n    url = https://example.com/Org/Repo.git\n',
        encoding="utf-8",
    )
    real_open = os.open

    def permission_denied_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path).name == config.name and dir_fd is not None:
            raise PermissionError(errno.EACCES, "synthetic metadata permission denial", path)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(os, "open", permission_denied_open)

    result = scanner_for(root).discover()

    assert len(result.candidates) == 1
    assert len(result.issues) == 1
    assert result.issues[0].code == "blocked_permission"
    assert result.issues[0].path == config


def test_manifest_fingerprint_uses_names_not_whole_contents(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    third = tmp_path / "third"
    for project in (first, second, third):
        project.mkdir()
    (first / "package.json").write_text(
        '{"name":" @Scope/Widget ","scripts":{"build":"secret command"}}',
        encoding="utf-8",
    )
    (second / "package.json").write_text(
        '{"description":"different arbitrary content","name":"@scope/widget"}',
        encoding="utf-8",
    )
    (third / "package.json").write_text('{"name":"@scope/another"}', encoding="utf-8")

    first_fingerprint = fingerprint_manifests(first, ("package.json",))

    assert first_fingerprint == fingerprint_manifests(second, ("package.json",))
    assert first_fingerprint == fingerprint_manifests(first, ("package.json", ".git"))
    assert first_fingerprint != fingerprint_manifests(third, ("package.json",))
    assert first_fingerprint is not None
    assert re.fullmatch(r"[0-9a-f]{64}", first_fingerprint)


def test_namespaced_pom_uses_direct_project_artifact_not_parent(
    tmp_path: Path,
) -> None:
    def pom(parent: str, project: str) -> str:
        return f"""
        <project xmlns="http://maven.apache.org/POM/4.0.0">
          <parent>
            <groupId>com.example</groupId>
            <artifactId>{parent}</artifactId>
            <version>1</version>
          </parent>
          <modelVersion>4.0.0</modelVersion>
          <artifactId>{project}</artifactId>
        </project>
        """

    base = add_marker(tmp_path / "base", "pom.xml", pom("parent", "child"))
    other_parent = add_marker(
        tmp_path / "other-parent", "pom.xml", pom("different-parent", "child")
    )
    other_project = add_marker(
        tmp_path / "other-project", "pom.xml", pom("parent", "different-child")
    )

    base_fingerprint = fingerprint_manifests(base, ("pom.xml",))

    assert base_fingerprint == fingerprint_manifests(other_parent, ("pom.xml",))
    assert base_fingerprint != fingerprint_manifests(other_project, ("pom.xml",))


def test_manifest_read_uses_nofollow_descriptor_and_never_exceeds_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    manifest = project / "package.json"
    prefix = b'{"name":"bounded"}'
    manifest.write_bytes(prefix + b" " * (256 * 1024 - len(prefix)))
    real_open = os.open
    real_read = os.read
    open_flags: list[int] = []
    open_dir_fds: list[int | None] = []
    read_sizes: list[int] = []

    def recording_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path).name == manifest.name:
            open_flags.append(flags)
            open_dir_fds.append(dir_fd)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    def recording_read(file_descriptor: int, size: int) -> bytes:
        read_sizes.append(size)
        return real_read(file_descriptor, size)

    monkeypatch.setattr(os, "open", recording_open)
    monkeypatch.setattr(os, "read", recording_read)

    fingerprint = fingerprint_manifests(project, ("package.json",))

    assert fingerprint is not None
    assert open_flags
    assert all(dir_fd is not None for dir_fd in open_dir_fds)
    if hasattr(os, "O_NOFOLLOW"):
        assert all(flags & os.O_NOFOLLOW for flags in open_flags)
    assert read_sizes
    assert sum(read_sizes) <= 256 * 1024


def test_manifest_race_to_symlink_returns_no_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = add_marker(tmp_path / "project", "package.json", '{"name":"inside"}')
    manifest = project / "package.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{"name":"outside"}', encoding="utf-8")
    real_open = os.open
    raced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal raced
        if Path(path).name == manifest.name and not raced:
            raced = True
            manifest.unlink()
            manifest.symlink_to(outside)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", racing_open)

    assert fingerprint_manifests(project, ("package.json",)) is None
    assert raced


def test_manifest_replacement_after_descriptor_open_returns_no_fingerprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = add_marker(tmp_path / "project", "package.json", '{"name":"original"}')
    manifest = project / "package.json"
    outside = tmp_path / "outside.json"
    outside.write_text('{"name":"replacement"}', encoding="utf-8")
    real_fstat = os.fstat
    fstat_calls = 0
    raced = False

    def racing_fstat(file_descriptor: int):
        nonlocal fstat_calls, raced
        metadata = real_fstat(file_descriptor)
        fstat_calls += 1
        if fstat_calls == 3:
            raced = True
            os.replace(outside, manifest)
        return metadata

    monkeypatch.setattr(os, "fstat", racing_fstat)

    assert fingerprint_manifests(project, ("package.json",)) is None
    assert raced


@pytest.mark.parametrize(
    ("marker", "content"),
    (
        ("pyproject.toml", '[project]\nname = "Example"\n'),
        ("Cargo.toml", '[package]\nname = "Example"\n'),
        ("go.mod", "module example.com/Example\n\ngo 1.23\n"),
        (
            "pom.xml",
            "<project><groupId>com.example</groupId><artifactId>Example</artifactId></project>",
        ),
        ("build.gradle", "rootProject.name = 'Example'\n"),
    ),
)
def test_manifest_fingerprint_extracts_only_recognized_metadata(
    tmp_path: Path, marker: str, content: str
) -> None:
    project = tmp_path / marker.replace(".", "-")
    add_marker(project, marker, content)

    fingerprint = fingerprint_manifests(project, (marker,))

    assert fingerprint is not None
    assert re.fullmatch(r"[0-9a-f]{64}", fingerprint)


def test_malformed_and_oversized_metadata_do_not_abort_discovery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    malformed = add_marker(root / "malformed", "package.json", "{not-json")
    oversized = add_marker(root / "oversized", "package.json", "x" * (256 * 1024 + 1))
    git_project = add_marker(root / "git-project", ".git")
    (git_project / ".git" / "config").write_text("x" * (256 * 1024 + 1), encoding="utf-8")

    result = scanner_for(root).discover()
    by_path = {item.canonical_path: item for item in result.candidates}

    assert set(by_path) == {
        malformed.resolve(),
        oversized.resolve(),
        git_project.resolve(),
    }
    assert by_path[malformed.resolve()].manifest_fingerprint is None
    assert by_path[oversized.resolve()].manifest_fingerprint is None
    assert by_path[git_project.resolve()].git_remote_fingerprint is None


def test_no_safe_manifest_metadata_returns_no_fingerprint(tmp_path: Path) -> None:
    project = add_marker(tmp_path / "project", "package.json", '{"private": true}')

    assert fingerprint_manifests(project, ("package.json",)) is None


def test_deeply_nested_json_returns_candidate_without_fingerprint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "deep-json"
    project.mkdir(parents=True)
    depth = 2_000
    content = '{"name":' + "[" * depth + '"nested"' + "]" * depth + "}"
    assert len(content.encode("utf-8")) < 256 * 1024
    (project / "package.json").write_text(content, encoding="utf-8")

    result = scanner_for(root).discover()

    assert len(result.candidates) == 1
    assert result.candidates[0].canonical_path == project.resolve()
    assert result.candidates[0].manifest_fingerprint is None


def test_package_json_huge_integer_returns_candidate_without_fingerprint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    project = root / "huge-integer-json"
    project.mkdir(parents=True)
    content = '{"name":' + "9" * 4_301 + "}"
    assert len(content.encode("utf-8")) < 256 * 1024
    (project / "package.json").write_text(content, encoding="utf-8")

    result = scanner_for(root).discover()

    assert len(result.candidates) == 1
    assert result.candidates[0].canonical_path == project.resolve()
    assert result.candidates[0].manifest_fingerprint is None
