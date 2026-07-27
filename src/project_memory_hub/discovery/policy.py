import os
from dataclasses import dataclass
from pathlib import Path

from project_memory_hub.config import AppConfig


_EXCLUDED_DIRECTORY_NAMES = (
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
)

_SENSITIVE_FILENAME_PATTERNS = (
    r"^\.env(?:\..*)?$",
    r"^\.ssh$",
    r"^(?:id_rsa|id_dsa|id_ecdsa|id_ed25519)(?:\..*)?$",
    r".*\.(?:pem|key|p12|pfx|crt|cer)$",
    r".*private[-_. ]?key.*",
    r".*credentials?.*",
    r".*secrets?.*",
    r".*tokens?.*",
)

_PROJECT_MARKERS = (
    ".git",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pom.xml",
    "build.gradle",
)

_WORKSPACE_DIRECTORY_NAMES = (
    "apps",
    "packages",
    "services",
    "modules",
    "crates",
)


def validate_project_root_scope(path: Path, *, home: Path | None = None) -> Path:
    """Return a canonical root that cannot encompass the user's entire home."""
    canonical_root = Path(path).expanduser().resolve(strict=False)
    canonical_home = (Path.home() if home is None else Path(home)).resolve(strict=False)
    if any(
        _root_encompasses_home_alias(canonical_root, home_alias)
        for home_alias in _home_aliases(canonical_home)
    ):
        raise ValueError("project root is too broad")
    return canonical_root


def _home_aliases(canonical_home: Path) -> tuple[Path, ...]:
    aliases = [canonical_home]
    data_root = Path("/System/Volumes/Data")
    try:
        data_home = data_root / canonical_home.relative_to(canonical_home.anchor)
        if data_home.exists() and os.path.samefile(data_home, canonical_home):
            aliases.append(data_home)
    except (OSError, ValueError):
        pass
    return tuple(aliases)


def _root_encompasses_home_alias(canonical_root: Path, home_alias: Path) -> bool:
    home_scope = (home_alias, *home_alias.parents)
    if canonical_root in home_scope:
        return True
    for unsafe_root in home_scope:
        try:
            if os.path.samefile(canonical_root, unsafe_root):
                return True
        except OSError:
            continue
    return False


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    allowed_roots: tuple[Path, ...]
    excluded_directory_names: tuple[str, ...]
    sensitive_filename_patterns: tuple[str, ...]
    project_markers: tuple[str, ...]
    workspace_directory_names: tuple[str, ...]
    max_depth: int

    @classmethod
    def from_config(
        cls,
        config: AppConfig,
        *,
        home: Path | None = None,
    ) -> "DiscoveryPolicy":
        allowed_roots: list[Path] = []
        seen: set[Path] = set()
        for configured_root in config.project_roots:
            canonical_root = validate_project_root_scope(Path(configured_root), home=home)
            if canonical_root in seen:
                continue
            seen.add(canonical_root)
            allowed_roots.append(canonical_root)

        return cls(
            allowed_roots=tuple(allowed_roots),
            excluded_directory_names=_EXCLUDED_DIRECTORY_NAMES,
            sensitive_filename_patterns=_SENSITIVE_FILENAME_PATTERNS,
            project_markers=_PROJECT_MARKERS,
            workspace_directory_names=_WORKSPACE_DIRECTORY_NAMES,
            max_depth=8,
        )
