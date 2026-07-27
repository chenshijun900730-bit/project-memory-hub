from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import selectors
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from project_memory_hub.domain import FactScanReport, ProjectFactInput, ProjectRecord
from project_memory_hub.discovery.fingerprint import fingerprint_git_remote
from project_memory_hub.security.json_limits import loads_json_bounded
from project_memory_hub.security.redaction import Redactor, SensitivePathError
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.projects import ProjectRepository


_MANIFESTS: Final = (
    "Cargo.toml",
    "composer.json",
    "go.mod",
    "package.json",
    "pyproject.toml",
)
_HEADING_FILES: Final = (
    ("README.md", "readme_heading"),
    ("AGENTS.md", "agents_heading"),
)
_TEST_CONFIG = re.compile(
    r"^(?:pytest[.]ini|tox[.]ini|jest[.]config[.].+|vitest[.]config[.].+|"
    r"playwright[.]config[.].+|phpunit[.]xml(?:[.]dist)?)$",
    re.IGNORECASE,
)
_BUILD_CONFIG = re.compile(
    r"^(?:Makefile|CMakeLists[.]txt|meson[.]build|vite[.]config[.].+|"
    r"webpack[.]config[.].+|rollup[.]config[.].+|tsconfig[.]json)$",
    re.IGNORECASE,
)
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_SENSITIVE_FILENAME = re.compile(
    r"^(?:[.]env(?:[.].*)?|[.]ssh|id_(?:rsa|dsa|ecdsa|ed25519)(?:[.].*)?|"
    r".*[.](?:pem|key|p12|pfx|crt|cer)|.*private[-_. ]?key.*|"
    r".*credentials?.*|.*secrets?.*|.*tokens?.*)$",
    re.IGNORECASE,
)
_EXCLUDED_DIRS: Final = frozenset(
    {
        "__pycache__",
        "applications",
        "build",
        "cache",
        "coverage",
        "deriveddata",
        "dist",
        "downloads",
        "env",
        "graphify-out",
        "library",
        "node_modules",
        "out",
        "pods",
        "target",
        "vendor",
        "venv",
    }
)
_LANGUAGES: Final = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}
_MAX_FACT_CHARS = 1024
_MAX_GIT_OUTPUT = 65_536
_MAX_GIT_CONFIG_BYTES = 262_144
_GIT_TIMEOUT_SECONDS = 3.0
_GIT_SECTION_HEADER = re.compile(r"^\[\s*(.*?)\s*\](?:\s*[#;].*)?$")
_WORKTREE_CONFIG_KEY = re.compile(
    r"^worktreeconfig(?:\s*=\s*([^#;]*))?\s*(?:[#;].*)?$", re.IGNORECASE
)
_GIT_FD_EXEC = (
    "import os,sys;"
    "fd=int(sys.argv[1]);"
    "os.fchdir(fd);"
    "os.close(fd);"
    "os.execvpe('git',['git',*sys.argv[2:]],os.environ)"
)
_DIRECTORY_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)
_FILE_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


@dataclass(frozen=True, slots=True)
class _Candidate:
    category: str
    content: str
    evidence_type: str
    evidence_reference: str
    confidence: float = 1.0


@dataclass(frozen=True, slots=True)
class _RootAnchor:
    parent_path: Path
    parent_fd: int
    root_fd: int
    root_name: str
    metadata: os.stat_result


@dataclass(frozen=True, slots=True)
class _ProcessOutput:
    stdout: bytes
    reason: str | None
    returncode: int | None


@dataclass(frozen=True, slots=True)
class _GitProbe:
    returncode: int | None
    stdout: bytes
    reason: str | None


@dataclass(frozen=True, slots=True)
class _GitConfigAnchor:
    git_dir_fd: int
    git_dir_metadata: os.stat_result
    config_fd: int
    config_metadata: os.stat_result
    dirty_state_ambiguous: bool


class _ProjectIdentityChanged(RuntimeError):
    pass


def _project_replaced_report(project: ProjectRecord) -> FactScanReport:
    return FactScanReport(
        project_id=project.project_id,
        observed_count=0,
        stale_count=0,
        warnings=("project_replaced",),
    )


class ProjectFactService:
    def __init__(
        self,
        facts: FactRepository,
        redactor: Redactor,
        *,
        projects: ProjectRepository | None = None,
        now: Callable[[], datetime] | None = None,
        max_manifest_bytes: int = 262_144,
        max_heading_bytes: int = 131_072,
        max_graph_bytes: int = 8_388_608,
        max_tree_entries: int = 50_000,
        max_tree_depth: int = 12,
    ) -> None:
        limits = {
            "max_manifest_bytes": max_manifest_bytes,
            "max_heading_bytes": max_heading_bytes,
            "max_graph_bytes": max_graph_bytes,
            "max_tree_entries": max_tree_entries,
            "max_tree_depth": max_tree_depth,
        }
        for name, value in limits.items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self._facts = facts
        self._redactor = redactor
        self._projects = projects
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._max_manifest_bytes = max_manifest_bytes
        self._max_heading_bytes = max_heading_bytes
        self._max_graph_bytes = max_graph_bytes
        self._max_tree_entries = max_tree_entries
        self._max_tree_depth = max_tree_depth

    def scan(self, project: ProjectRecord, dry_run: bool = False) -> FactScanReport:
        root = Path(project.canonical_path)
        warnings: set[str] = set()
        anchor = _open_root_anchor(root)
        if anchor is None:
            return FactScanReport(
                project_id=project.project_id,
                observed_count=0,
                stale_count=0,
                warnings=("project_unavailable",),
            )

        try:
            anchor_identity = (anchor.metadata.st_dev, anchor.metadata.st_ino)
            projects = self._projects
            if projects is not None and not projects.record_matches_identity(
                project,
                anchor_identity,
            ):
                return _project_replaced_report(project)

            candidates: list[_Candidate] = []
            git_config_anchor = _open_git_config_anchor(anchor.root_fd, warnings)
            if git_config_anchor is not None:
                try:
                    git_candidates = self._git_facts(
                        anchor.root_fd,
                        warnings,
                        dirty_state_ambiguous=git_config_anchor.dirty_state_ambiguous,
                    )
                    if _git_config_anchor_matches(anchor.root_fd, git_config_anchor):
                        candidates.extend(git_candidates)
                    else:
                        warnings.add("git_config_changed")
                finally:
                    os.close(git_config_anchor.config_fd)
                    os.close(git_config_anchor.git_dir_fd)
            candidates.extend(self._manifest_facts(anchor.root_fd, warnings))
            candidates.extend(self._heading_facts(anchor.root_fd, warnings))
            candidates.extend(self._tree_and_config_facts(anchor.root_fd, warnings))
            graph = self._graphify_fact(anchor.root_fd, warnings)
            if graph is not None:
                candidates.append(graph)

            if not _anchor_matches_registered_path(anchor) or (
                projects is not None
                and not projects.record_matches_identity(project, anchor_identity)
            ):
                return _project_replaced_report(project)

            observed_at = self._now()
            if observed_at.tzinfo is None:
                raise ValueError("timestamp must be timezone-aware")
            observed_at = observed_at.astimezone(timezone.utc)
            prepared: list[ProjectFactInput] = []
            seen: set[tuple[str, str, str, str]] = set()
            for candidate in candidates:
                safe_content = self._safe_fact_text(candidate.content)
                safe_reference = self._safe_fact_text(candidate.evidence_reference)
                if not safe_content or not safe_reference:
                    continue
                key = (
                    candidate.category,
                    safe_content,
                    candidate.evidence_type,
                    safe_reference,
                )
                if key in seen:
                    continue
                seen.add(key)
                prepared.append(
                    ProjectFactInput(
                        category=candidate.category,
                        normalized_content=safe_content,
                        evidence_type=candidate.evidence_type,
                        evidence_reference=safe_reference,
                        observed_at=observed_at,
                        confidence=candidate.confidence,
                    )
                )
            prepared.sort(
                key=lambda item: (
                    item.category,
                    item.evidence_reference,
                    item.normalized_content,
                    item.evidence_type,
                )
            )
            stale_count = 0
            if prepared and not dry_run:
                observer = None
                before_observe = None
                if projects is not None:

                    def before_observe(connection: sqlite3.Connection) -> None:
                        if not _anchor_matches_registered_path(anchor) or not (
                            projects._record_matches_identity_on_connection(
                                connection,
                                project.project_id,
                                project.canonical_path,
                                anchor_identity,
                            )
                        ):
                            raise _ProjectIdentityChanged

                    def observer(connection: sqlite3.Connection) -> None:
                        projects._advance_last_observed_change_on_connection(
                            connection,
                            project.project_id,
                            observed_at,
                            as_of=observed_at,
                        )

                try:
                    _, stale_count, _ = self._facts._observe_many_with_changes(
                        project.project_id,
                        tuple(prepared),
                        before_observe=before_observe,
                        after_observe=before_observe,
                        on_effective_change=observer,
                    )
                except _ProjectIdentityChanged:
                    return _project_replaced_report(project)
            return FactScanReport(
                project_id=project.project_id,
                observed_count=len(prepared),
                stale_count=stale_count,
                warnings=tuple(sorted(warnings)),
            )
        finally:
            os.close(anchor.root_fd)
            os.close(anchor.parent_fd)

    def _safe_fact_text(self, value: str) -> str:
        normalized = " ".join(value.split())
        redacted = " ".join(self._redactor.redact(normalized).text.split())
        return redacted[:_MAX_FACT_CHARS].strip()

    def _git_facts(
        self,
        root_fd: int,
        warnings: set[str],
        *,
        dirty_state_ambiguous: bool,
    ) -> list[_Candidate]:
        check = _git_probe(root_fd, "worktree", "rev-parse", "--is-inside-work-tree")
        check_text = _git_text(check, "worktree", warnings)
        if check_text is None or check_text[0] != 0 or check_text[1] != "true":
            return []
        facts: list[_Candidate] = []

        branch = _git_probe(
            root_fd,
            "branch",
            "symbolic-ref",
            "--quiet",
            "--short",
            "HEAD",
        )
        branch_text = _git_text(branch, "branch", warnings)
        if branch_text is not None and branch_text[0] == 0 and branch_text[1]:
            facts.append(_Candidate("git_branch", branch_text[1], "git_metadata", "git:branch"))
        elif branch_text is not None:
            facts.append(_Candidate("git_branch", "detached", "git_metadata", "git:branch"))

        head = _git_probe(root_fd, "head", "rev-parse", "--verify", "--quiet", "HEAD")
        head_text = _git_text(head, "head", warnings)
        head_exists = head_text is not None and head_text[0] == 0 and bool(head_text[1])
        if head_exists:
            assert head_text is not None
            facts.append(_Candidate("git_head", head_text[1], "git_metadata", "git:head"))
        elif head_text is None or head_text[0] != 1 or head_text[1]:
            warnings.add("git_head_unavailable")

        dirty_fact = self._git_dirty_fact(
            root_fd,
            head_exists,
            warnings,
            dirty_state_ambiguous=dirty_state_ambiguous,
        )
        if dirty_fact is not None:
            facts.append(dirty_fact)

        remote = _git_probe(
            root_fd,
            "remote",
            "config",
            "--local",
            "--no-includes",
            "--get",
            "remote.origin.url",
        )
        remote_text = _git_text(remote, "remote", warnings)
        if remote_text is not None and remote_text[0] == 0 and remote_text[1]:
            try:
                fingerprint = fingerprint_git_remote(remote_text[1])
            except ValueError:
                warnings.add("git_remote_invalid")
            else:
                facts.append(
                    _Candidate(
                        "git_remote_fingerprint",
                        fingerprint,
                        "git_metadata",
                        "git:remote:origin",
                    )
                )
        return facts

    @staticmethod
    def _git_dirty_fact(
        root_fd: int,
        head_exists: bool,
        warnings: set[str],
        *,
        dirty_state_ambiguous: bool,
    ) -> _Candidate | None:
        if head_exists:
            staged = _git_probe(
                root_fd,
                "dirty_staged",
                "diff-index",
                "--cached",
                "--quiet",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--ignore-submodules=dirty",
                "HEAD",
                "--",
                max_bytes=1,
            )
            staged_dirty = _git_returncode_signal(staged, "dirty_staged", warnings)
            if staged_dirty:
                return _Candidate("git_dirty", "true", "git_metadata", "git:dirty")
            if not dirty_state_ambiguous:
                worktree = _git_probe(
                    root_fd,
                    "dirty_worktree",
                    "diff-files",
                    "--quiet",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-renames",
                    "--ignore-submodules=dirty",
                    "--",
                    max_bytes=1,
                )
                worktree_dirty = _git_returncode_signal(
                    worktree,
                    "dirty_worktree",
                    warnings,
                )
                if worktree_dirty:
                    return _Candidate("git_dirty", "true", "git_metadata", "git:dirty")
        else:
            cached = _git_probe(
                root_fd,
                "dirty_cached",
                "ls-files",
                "-z",
                "--cached",
                max_bytes=1,
            )
            if _git_output_signal(cached, "dirty_cached", warnings):
                return _Candidate("git_dirty", "true", "git_metadata", "git:dirty")

        conflicts = _git_probe(
            root_fd,
            "dirty_conflicts",
            "ls-files",
            "-z",
            "--unmerged",
            max_bytes=1,
        )
        if _git_output_signal(conflicts, "dirty_conflicts", warnings):
            return _Candidate("git_dirty", "true", "git_metadata", "git:dirty")

        deleted_or_untracked = _git_probe(
            root_fd,
            "dirty_paths",
            "ls-files",
            "-z",
            "--deleted",
            "--others",
            "--exclude-standard",
            max_bytes=1,
        )
        if _git_output_signal(deleted_or_untracked, "dirty_paths", warnings):
            return _Candidate("git_dirty", "true", "git_metadata", "git:dirty")

        if dirty_state_ambiguous:
            warnings.add("git_dirty_unavailable")
        return None

    def _manifest_facts(self, root_fd: int, warnings: set[str]) -> list[_Candidate]:
        facts: list[_Candidate] = []
        for name in _MANIFESTS:
            data = _bounded_regular_file_at(
                root_fd,
                name,
                self._max_manifest_bytes,
                f"manifest_too_large:{name}",
                f"manifest_read_failed:{name}",
                warnings,
            )
            if data is None:
                continue
            try:
                facts.extend(_parse_manifest(name, data))
            except (
                AttributeError,
                RecursionError,
                TypeError,
                ValueError,
                UnicodeDecodeError,
                json.JSONDecodeError,
                tomllib.TOMLDecodeError,
            ):
                warnings.add(f"manifest_parse_failed:{name}")
        return facts

    def _heading_facts(self, root_fd: int, warnings: set[str]) -> list[_Candidate]:
        facts: list[_Candidate] = []
        for name, category in _HEADING_FILES:
            data = _bounded_regular_file_at(
                root_fd,
                name,
                self._max_heading_bytes,
                f"heading_too_large:{name}",
                f"heading_read_failed:{name}",
                warnings,
            )
            if data is None:
                continue
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                warnings.add(f"heading_decode_failed:{name}")
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                match = _HEADING.match(line)
                if match is not None:
                    facts.append(
                        _Candidate(
                            category,
                            match.group(1),
                            "file_heading",
                            f"{name}#{line_number}",
                        )
                    )
        return facts

    def _tree_and_config_facts(self, root_fd: int, warnings: set[str]) -> list[_Candidate]:
        extensions: Counter[str] = Counter()
        languages: Counter[str] = Counter()
        configs: list[_Candidate] = []
        activity = hashlib.sha256(b"project-memory-hub:worktree-activity:v1\0")
        entry_count = 0
        invalid = False

        def walk(directory_fd: int, depth: int, relative_parts: tuple[str, ...]) -> None:
            nonlocal entry_count, invalid
            if invalid:
                return
            remaining = self._max_tree_entries - entry_count
            try:
                iterator = os.scandir(directory_fd)
            except OSError:
                warnings.add("tree_scan_failed")
                return

            entries = []
            try:
                with iterator:
                    entry_iterator = iter(iterator)
                    for _ in range(remaining + 1):
                        try:
                            entries.append(next(entry_iterator))
                        except StopIteration:
                            break
                if len(entries) > remaining:
                    warnings.add("tree_entry_limit")
                    invalid = True
                    return
            except OSError:
                warnings.add("tree_scan_failed")
                return

            entry_count += len(entries)
            entries.sort(key=lambda entry: entry.name)
            for entry in entries:
                if entry.name.startswith("."):
                    continue
                if _SENSITIVE_FILENAME.fullmatch(entry.name):
                    continue
                try:
                    self._redactor.assert_safe_path(Path(entry.name))
                except SensitivePathError:
                    continue
                try:
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    warnings.add("tree_stat_failed")
                    continue
                if stat.S_ISLNK(metadata.st_mode):
                    continue
                if stat.S_ISDIR(metadata.st_mode):
                    if entry.name.casefold() in _EXCLUDED_DIRS:
                        continue
                    if depth >= self._max_tree_depth:
                        warnings.add("tree_depth_limit")
                        continue
                    child = _open_child_directory(directory_fd, entry.name, metadata)
                    if child is None:
                        warnings.add("tree_component_changed")
                        invalid = True
                        return
                    child_fd, child_metadata = child
                    try:
                        walk(child_fd, depth + 1, (*relative_parts, entry.name))
                        if not _component_matches(
                            directory_fd,
                            entry.name,
                            child_metadata,
                            directory=True,
                        ):
                            warnings.add("tree_component_changed")
                            invalid = True
                            return
                    finally:
                        os.close(child_fd)
                elif stat.S_ISREG(metadata.st_mode):
                    current = _stat_at(directory_fd, entry.name)
                    if current is None or _file_metadata_changed(metadata, current):
                        warnings.add("tree_stat_failed")
                        continue
                    path_fingerprint = _relative_path_fingerprint((*relative_parts, entry.name))
                    activity.update(path_fingerprint)
                    activity.update(current.st_size.to_bytes(16, "big"))
                    activity.update(current.st_mtime_ns.to_bytes(16, "big", signed=True))
                    if depth == 0:
                        if _TEST_CONFIG.fullmatch(entry.name):
                            configs.append(
                                _Candidate(
                                    "test_config",
                                    entry.name,
                                    "config_name",
                                    entry.name,
                                )
                            )
                        if _BUILD_CONFIG.fullmatch(entry.name):
                            configs.append(
                                _Candidate(
                                    "build_config",
                                    entry.name,
                                    "config_name",
                                    entry.name,
                                )
                            )
                    suffix = Path(entry.name).suffix.casefold()
                    if suffix:
                        extensions[suffix] += 1
                        language = _LANGUAGES.get(suffix)
                        if language is not None:
                            languages[language] += 1

        walk(root_fd, 0, ())
        if invalid:
            return []
        facts = [
            _Candidate(
                "file_extension_count",
                f"{extension}={count}",
                "file_tree",
                f"tree:{extension}",
            )
            for extension, count in sorted(extensions.items())
        ]
        facts.extend(
            _Candidate(
                "language_count",
                f"{language}={count}",
                "file_tree",
                f"tree:language:{language}",
            )
            for language, count in sorted(languages.items())
        )
        facts.extend(configs)
        facts.append(
            _Candidate(
                "worktree_activity_fingerprint",
                f"sha256:{activity.hexdigest()}",
                "file_tree_metadata",
                "tree:activity",
            )
        )
        return facts

    def _graphify_fact(self, root_fd: int, warnings: set[str]) -> _Candidate | None:
        relative = "graphify-out/graph.json"
        try:
            graph_fd = os.open("graphify-out", _DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError as error:
            if error.errno == errno.ENOENT:
                return None
            if _component_is_symlink(root_fd, "graphify-out"):
                warnings.add(f"graphify_symlink:{relative}")
            else:
                warnings.add(f"graphify_read_failed:{relative}")
            return None
        try:
            graph_metadata = os.fstat(graph_fd)
            if not stat.S_ISDIR(graph_metadata.st_mode):
                warnings.add(f"graphify_not_regular:{relative}")
                return None
            data = _bounded_regular_file_at(
                graph_fd,
                "graph.json",
                self._max_graph_bytes,
                f"graphify_too_large:{relative}",
                f"graphify_read_failed:{relative}",
                warnings,
                symlink_warning=f"graphify_symlink:{relative}",
                not_regular_warning=f"graphify_not_regular:{relative}",
            )
            if not _component_matches(
                root_fd,
                "graphify-out",
                graph_metadata,
                directory=True,
            ):
                warnings.add(f"graphify_read_failed:{relative}")
                return None
        except OSError:
            warnings.add(f"graphify_read_failed:{relative}")
            return None
        finally:
            os.close(graph_fd)
        if data is None:
            return None
        try:
            payload = loads_json_bounded(data.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            nodes = _summary_count(payload.get("nodes"))
            edges = _summary_count(payload.get("edges"))
        except (
            RecursionError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
            TypeError,
        ):
            warnings.add(f"graphify_parse_failed:{relative}")
            return None
        return _Candidate(
            "graphify_summary",
            f"nodes={nodes} edges={edges}",
            "graphify_exact_path",
            relative,
        )


def _relative_path_fingerprint(parts: tuple[str, ...]) -> bytes:
    digest = hashlib.sha256(b"project-memory-hub:relative-path:v1\0")
    for part in parts:
        encoded = os.fsencode(part)
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return digest.digest()


def _parse_manifest(name: str, data: bytes) -> list[_Candidate]:
    if name == "package.json":
        payload = loads_json_bounded(data.decode("utf-8"))
        facts = []
        package_name = payload.get("name") if isinstance(payload, dict) else None
        if isinstance(package_name, str):
            facts.append(_Candidate("manifest", package_name, "manifest_metadata", name))
        scripts = payload.get("scripts", {}) if isinstance(payload, dict) else {}
        if isinstance(scripts, dict):
            facts.extend(
                _Candidate(
                    "package_script",
                    f"npm run {script}",
                    "manifest_metadata",
                    f"{name}#scripts.{script}",
                )
                for script in sorted(scripts)
                if isinstance(script, str) and script.strip()
            )
        return facts
    if name in {"pyproject.toml", "Cargo.toml"}:
        payload = tomllib.loads(data.decode("utf-8"))
        if name == "Cargo.toml":
            package_name = payload.get("package", {}).get("name")
        else:
            package_name = payload.get("project", {}).get("name")
            if package_name is None:
                package_name = payload.get("tool", {}).get("poetry", {}).get("name")
        if isinstance(package_name, str):
            return [_Candidate("manifest", package_name, "manifest_metadata", name)]
        return []
    if name == "composer.json":
        payload = loads_json_bounded(data.decode("utf-8"))
        package_name = payload.get("name") if isinstance(payload, dict) else None
        return (
            [_Candidate("manifest", package_name, "manifest_metadata", name)]
            if isinstance(package_name, str)
            else []
        )
    if name == "go.mod":
        text = data.decode("utf-8")
        for line in text.splitlines():
            if line.startswith("module ") and line[7:].strip():
                return [_Candidate("manifest", line[7:].strip(), "manifest_metadata", name)]
    return []


def _open_root_anchor(root: Path) -> _RootAnchor | None:
    parent_fd: int | None = None
    root_fd: int | None = None
    if not root.name:
        return None
    try:
        parent_fd = os.open(root.parent, _DIRECTORY_FLAGS)
        root_fd = os.open(root.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        metadata = os.fstat(root_fd)
        if not stat.S_ISDIR(metadata.st_mode):
            return None
        anchor = _RootAnchor(root.parent, parent_fd, root_fd, root.name, metadata)
        parent_fd = None
        root_fd = None
        return anchor
    except OSError:
        return None
    finally:
        if root_fd is not None:
            os.close(root_fd)
        if parent_fd is not None:
            os.close(parent_fd)


def _anchor_matches_registered_path(anchor: _RootAnchor) -> bool:
    try:
        parent_fd_metadata = os.fstat(anchor.parent_fd)
        parent_path_metadata = os.stat(anchor.parent_path, follow_symlinks=False)
        current_fd_metadata = os.fstat(anchor.root_fd)
    except OSError:
        return False
    if not _same_identity(parent_fd_metadata, parent_path_metadata, directory=True):
        return False
    if not _same_identity(anchor.metadata, current_fd_metadata, directory=True):
        return False
    return _component_matches(
        anchor.parent_fd,
        anchor.root_name,
        current_fd_metadata,
        directory=True,
    )


def _open_git_config_anchor(root_fd: int, warnings: set[str]) -> _GitConfigAnchor | None:
    git_dir_fd: int | None = None
    config_fd: int | None = None
    try:
        try:
            git_dir_fd = os.open(".git", _DIRECTORY_FLAGS, dir_fd=root_fd)
        except OSError:
            if _stat_at(root_fd, ".git") is not None:
                warnings.add("git_config_unsafe")
            return None
        git_dir_metadata = os.fstat(git_dir_fd)
        if not _component_matches(root_fd, ".git", git_dir_metadata, directory=True):
            warnings.add("git_config_unsafe")
            return None

        try:
            config_fd = os.open("config", _FILE_FLAGS, dir_fd=git_dir_fd)
        except OSError:
            warnings.add("git_config_unsafe")
            return None
        before = os.fstat(config_fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > _MAX_GIT_CONFIG_BYTES:
            warnings.add("git_config_unsafe")
            return None
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(config_fd, min(65_536, remaining))
            if not chunk:
                warnings.add("git_config_unsafe")
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(config_fd)
        if _file_metadata_changed(before, after) or not _component_matches(
            git_dir_fd,
            "config",
            after,
            directory=False,
            require_file_stability=True,
        ):
            warnings.add("git_config_unsafe")
            return None
        try:
            text = b"".join(chunks).decode("utf-8")
        except UnicodeDecodeError:
            warnings.add("git_config_unsafe")
            return None
        if _git_config_is_unsafe(text):
            warnings.add("git_config_unsafe")
            return None

        git_dir_after = os.fstat(git_dir_fd)
        if _metadata_changed(
            git_dir_metadata, git_dir_after, directory=True
        ) or not _component_matches(root_fd, ".git", git_dir_after, directory=True):
            warnings.add("git_config_unsafe")
            return None
        result = _GitConfigAnchor(
            git_dir_fd=git_dir_fd,
            git_dir_metadata=git_dir_after,
            config_fd=config_fd,
            config_metadata=after,
            dirty_state_ambiguous=_git_config_has_clean_filter(text),
        )
        git_dir_fd = None
        config_fd = None
        return result
    except OSError:
        warnings.add("git_config_unsafe")
        return None
    finally:
        if config_fd is not None:
            os.close(config_fd)
        if git_dir_fd is not None:
            os.close(git_dir_fd)


def _git_config_is_unsafe(text: str) -> bool:
    if text.startswith("\ufeff"):
        return True
    current_section = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        header = _GIT_SECTION_HEADER.fullmatch(stripped)
        if header is not None:
            current_section = re.sub(r"[\s\"']", "", header.group(1)).casefold()
            if current_section.startswith("include"):
                return True
            continue
        if stripped.startswith("["):
            return True
        if current_section != "extensions":
            continue
        key = _WORKTREE_CONFIG_KEY.fullmatch(stripped)
        if key is None:
            continue
        value = key.group(1)
        if value is None:
            return True
        normalized = value.strip().strip('"').casefold()
        if normalized not in {"false", "no", "off", "0"}:
            return True
    return False


def _git_config_has_clean_filter(text: str) -> bool:
    current_section = ""
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith(("#", ";")):
            continue
        header = _GIT_SECTION_HEADER.fullmatch(stripped)
        if header is not None:
            current_section = re.sub(r"[\s\"']", "", header.group(1)).casefold()
            continue
        if current_section.startswith("filter") and re.match(
            r"^(?:clean|process)(?:\s*=|\s*$)", stripped, re.IGNORECASE
        ):
            return True
    return False


def _git_config_anchor_matches(root_fd: int, anchor: _GitConfigAnchor) -> bool:
    try:
        git_dir_fd_metadata = os.fstat(anchor.git_dir_fd)
        git_dir_path_metadata = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
        config_fd_metadata = os.fstat(anchor.config_fd)
        config_path_metadata = os.stat("config", dir_fd=anchor.git_dir_fd, follow_symlinks=False)
    except OSError:
        return False
    return (
        not _metadata_changed(anchor.git_dir_metadata, git_dir_fd_metadata, directory=True)
        and not _metadata_changed(git_dir_fd_metadata, git_dir_path_metadata, directory=True)
        and not _metadata_changed(anchor.config_metadata, config_fd_metadata, directory=False)
        and not _metadata_changed(config_fd_metadata, config_path_metadata, directory=False)
    )


def _bounded_regular_file_at(
    parent_fd: int,
    name: str,
    limit: int,
    too_large_warning: str,
    read_warning: str,
    warnings: set[str],
    *,
    symlink_warning: str | None = None,
    not_regular_warning: str | None = None,
) -> bytes | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            if not_regular_warning is not None:
                warnings.add(not_regular_warning)
            return None
        if before.st_size > limit:
            warnings.add(too_large_warning)
            return None
        remaining = before.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(descriptor, min(65_536, remaining))
            if not chunk:
                warnings.add(read_warning)
                return None
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(descriptor)
        if _file_metadata_changed(before, after):
            warnings.add(read_warning)
            return None
        if not _component_matches(
            parent_fd, name, after, directory=False, require_file_stability=True
        ):
            warnings.add(read_warning)
            return None
        return data
    except OSError as error:
        if error.errno == errno.ENOENT:
            return None
        if symlink_warning is not None and _component_is_symlink(parent_fd, name):
            warnings.add(symlink_warning)
            return None
        warnings.add(read_warning)
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _open_child_directory(
    parent_fd: int, name: str, expected: os.stat_result
) -> tuple[int, os.stat_result] | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        metadata = os.fstat(descriptor)
        if not _same_identity(expected, metadata, directory=True):
            return None
        if not _component_matches(parent_fd, name, metadata, directory=True):
            return None
        result = (descriptor, metadata)
        descriptor = None
        return result
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stat_at(parent_fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError:
        return None


def _component_is_symlink(parent_fd: int, name: str) -> bool:
    metadata = _stat_at(parent_fd, name)
    return metadata is not None and stat.S_ISLNK(metadata.st_mode)


def _component_matches(
    parent_fd: int,
    name: str,
    metadata: os.stat_result,
    *,
    directory: bool,
    require_file_stability: bool = False,
) -> bool:
    current = _stat_at(parent_fd, name)
    if current is None or not _same_identity(metadata, current, directory=directory):
        return False
    return not require_file_stability or not _file_metadata_changed(metadata, current)


def _same_identity(left: os.stat_result, right: os.stat_result, *, directory: bool) -> bool:
    expected_type = stat.S_ISDIR if directory else stat.S_ISREG
    return (
        expected_type(left.st_mode)
        and expected_type(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _file_metadata_changed(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        not _same_identity(left, right, directory=False)
        or left.st_size != right.st_size
        or left.st_mtime_ns != right.st_mtime_ns
        or left.st_ctime_ns != right.st_ctime_ns
    )


def _metadata_changed(left: os.stat_result, right: os.stat_result, *, directory: bool) -> bool:
    return (
        not _same_identity(left, right, directory=directory)
        or left.st_size != right.st_size
        or left.st_mtime_ns != right.st_mtime_ns
        or left.st_ctime_ns != right.st_ctime_ns
    )


def _git_probe(
    root_fd: int,
    operation: str,
    *arguments: str,
    max_bytes: int = _MAX_GIT_OUTPUT,
) -> _GitProbe:
    del operation
    environment = {
        "GIT_ASKPASS": "/usr/bin/false",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_EDITOR": "/usr/bin/false",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "SSH_ASKPASS": "/usr/bin/false",
    }
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                _GIT_FD_EXEC,
                str(root_fd),
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-c",
                "core.preloadIndex=false",
                "-c",
                "core.excludesFile=/dev/null",
                *arguments,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            shell=False,
            env=environment,
            pass_fds=(root_fd,),
            close_fds=True,
            start_new_session=os.name == "posix",
        )
    except OSError:
        return _GitProbe(None, b"", "exec")
    result = _collect_bounded_stdout(
        process,
        max_bytes=max_bytes,
        timeout_seconds=_GIT_TIMEOUT_SECONDS,
    )
    return _GitProbe(result.returncode, result.stdout, result.reason)


def _collect_bounded_stdout(
    process: subprocess.Popen[bytes],
    *,
    max_bytes: int,
    timeout_seconds: float,
) -> _ProcessOutput:
    if max_bytes <= 0 or timeout_seconds <= 0:
        raise ValueError("process bounds must be positive")
    stream = process.stdout
    if stream is None:
        _terminate_process_tree(process)
        process.wait()
        return _ProcessOutput(b"", "read", process.returncode)

    selector = selectors.DefaultSelector()
    retained = bytearray()
    reason: str | None = None
    deadline = time.monotonic() + timeout_seconds
    try:
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ)
        pipe_open = True
        while pipe_open:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                reason = "timeout"
                break
            events = selector.select(min(remaining_time, 0.05))
            if not events:
                if process.poll() is not None:
                    continue
                continue
            for key, _ in events:
                read_limit = max_bytes + 1 - len(retained)
                if read_limit <= 0:
                    reason = "overflow"
                    break
                try:
                    chunk = os.read(key.fd, min(65_536, read_limit))
                except BlockingIOError:
                    continue
                except OSError:
                    reason = "read"
                    break
                if not chunk:
                    selector.unregister(stream)
                    pipe_open = False
                    break
                retained.extend(chunk)
                if len(retained) > max_bytes:
                    reason = "overflow"
                    break
            if reason is not None:
                break

        if reason is None:
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                reason = "timeout"
            else:
                try:
                    process.wait(timeout=remaining_time)
                except subprocess.TimeoutExpired:
                    reason = "timeout"
    except (OSError, ValueError):
        reason = "read"
    finally:
        selector.close()
        try:
            _terminate_process_tree(process)
            process.wait()
        finally:
            stream.close()
    return _ProcessOutput(bytes(retained), reason, process.returncode)


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        process.kill()


def _git_returncode_signal(probe: _GitProbe, operation: str, warnings: set[str]) -> bool | None:
    if probe.reason is not None:
        _add_git_warning(probe.reason, operation, warnings)
        return None
    if probe.returncode == 1:
        return True
    if probe.returncode == 0:
        return False
    warnings.add(f"git_probe_failed:{operation}")
    return None


def _git_output_signal(probe: _GitProbe, operation: str, warnings: set[str]) -> bool | None:
    if probe.stdout:
        return True
    if probe.reason is not None:
        _add_git_warning(probe.reason, operation, warnings)
        return None
    if probe.returncode == 0:
        return False
    warnings.add(f"git_probe_failed:{operation}")
    return None


def _git_text(
    probe: _GitProbe, operation: str, warnings: set[str]
) -> tuple[int | None, str] | None:
    if probe.reason is not None:
        _add_git_warning(probe.reason, operation, warnings)
        return None
    try:
        text = probe.stdout.decode("utf-8").strip()
    except UnicodeDecodeError:
        warnings.add(f"git_decode_failed:{operation}")
        return None
    return probe.returncode, text


def _add_git_warning(reason: str | None, operation: str, warnings: set[str]) -> None:
    if reason == "overflow":
        warnings.add(f"git_output_limit:{operation}")
    elif reason == "timeout":
        warnings.add(f"git_timeout:{operation}")
    elif reason == "exec":
        warnings.add(f"git_exec_failed:{operation}")
    elif reason == "read":
        warnings.add(f"git_read_failed:{operation}")


def _summary_count(value: object) -> int:
    if isinstance(value, (list, dict)):
        return len(value)
    if value is None:
        return 0
    raise ValueError
