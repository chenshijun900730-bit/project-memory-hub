from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from project_memory_hub.paths import RuntimePaths


_TABLES = (
    "project_facts",
    "source_refs",
    "behavior_memories",
    "pending_captures",
    "checkpoints",
    "import_receipts_v2",
    "codex_deferred_records",
)


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _inventory(root: Path) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    entries: list[str] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        base = Path(current)
        entries.extend(str((base / name).relative_to(root)) for name in directories)
        entries.extend(str((base / name).relative_to(root)) for name in files)
    return tuple(entries)


def _row_counts(database: Path) -> tuple[tuple[str, int], ...]:
    if not database.is_file():
        return ()
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True)
    try:
        present = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        return tuple(
            (table, connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in _TABLES
            if table in present
        )
    finally:
        connection.close()


def _snapshot(paths: RuntimePaths) -> tuple[object, ...]:
    config = paths.root / "config.toml"
    database_files = (
        paths.database,
        Path(f"{paths.database}-wal"),
        Path(f"{paths.database}-shm"),
        Path(f"{paths.database}-journal"),
    )
    return (
        _inventory(paths.root),
        _digest(config),
        tuple(_digest(path) for path in database_files),
        _row_counts(paths.database),
    )


def _probe(*arguments: str, executable: Path | None = None) -> None:
    command = [str(executable)] if executable is not None else ["uv", "run", "memory-hub"]
    completed = subprocess.run(
        [*command, "source", "probe", *arguments, "--format", "json"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit("probe did not complete; ensure no structure probe is active")
    json.loads(completed.stdout)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path)
    arguments = parser.parse_args()
    paths = RuntimePaths.for_root()
    before = _snapshot(paths)
    _probe("--all", executable=arguments.executable)
    _probe("trae", "--structure", executable=arguments.executable)
    after = _snapshot(paths)
    if after != before:
        raise SystemExit("source probe changed Project Memory Hub runtime")
    print("source probe zero-write verification passed")


if __name__ == "__main__":
    main()
