import ast
import importlib.util
from pathlib import Path

import pytest


FORBIDDEN = (
    "project_memory_hub.storage",
    "project_memory_hub.adapters",
    "project_memory_hub.services.capture",
    "project_memory_hub.services.reconcile",
)


def _resolved_imports(tree: ast.AST) -> tuple[str, ...]:
    resolved: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            resolved.extend(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        dotted = "." * node.level + (node.module or "")
        base = importlib.util.resolve_name(dotted, "project_memory_hub.probes")
        resolved.append(base)
        resolved.extend(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return tuple(resolved)


def test_probe_package_has_no_forbidden_imports() -> None:
    probe_root = Path("src/project_memory_hub/probes")
    violations: list[str] = []
    for path in sorted(probe_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        violations.extend(
            f"{path}:{name}" for name in _resolved_imports(tree) if name.startswith(FORBIDDEN)
        )
    assert violations == []


@pytest.mark.parametrize(
    "document",
    [
        "from ..storage import database",
        "from project_memory_hub import storage",
    ],
)
def test_forbidden_import_resolver_catches_relative_and_alias_forms(document: str) -> None:
    imported = _resolved_imports(ast.parse(document))
    assert any(name.startswith("project_memory_hub.storage") for name in imported)
