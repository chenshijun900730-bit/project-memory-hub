import hashlib
import stat
from pathlib import Path

import pytest

import project_memory_hub.container as container_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import (
    build_container,
    build_mcp_capture_container,
    build_mcp_reconcile_container,
)
from project_memory_hub.domain import (
    CapturePayload,
    Namespace,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.storage.database import Database


def _tree_state(root: Path) -> tuple[tuple[object, ...], ...]:
    if not root.exists():
        return ()
    result: list[tuple[object, ...]] = []
    for path in sorted((root, *root.rglob("*")), key=str):
        metadata = path.lstat()
        digest = ""
        if stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result.append(
            (
                "." if path == root else str(path.relative_to(root)),
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_uid,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
                digest,
            )
        )
    return tuple(result)


def _runtime(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "projects"
    project = project_root / "demo"
    project.mkdir(parents=True)
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX,),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    with build_container(config) as container:
        container.projects.register(ProjectCandidate(canonical_path=project, display_name="demo"))
    return config, project


@pytest.mark.parametrize(
    "builder_name",
    ("build_mcp_capture_container", "build_mcp_reconcile_container"),
)
def test_mcp_builders_require_an_existing_runtime_and_never_prepare_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builder_name: str,
) -> None:
    config, _project = _runtime(tmp_path)
    before = _tree_state(config.parent)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("write-capable runtime preparation was called")

    monkeypatch.setattr(Database, "initialize", unexpected)
    monkeypatch.setattr(RuntimePaths, "ensure", unexpected)
    monkeypatch.setattr(container_module, "_create_default_config_if_absent", unexpected)
    monkeypatch.setattr(container_module, "_tighten_config_permissions", unexpected)
    builder = getattr(container_module, builder_name)

    with builder(config):
        pass

    assert _tree_state(config.parent) == before


def test_mcp_capture_container_only_exposes_pending_capture(
    tmp_path: Path,
) -> None:
    config, project = _runtime(tmp_path)
    with build_mcp_capture_container(config) as container:
        assert not hasattr(container, "reconcile")
        result = container.capture.capture(
            CapturePayload(
                cwd=project,
                namespace=Namespace(
                    source_agent=SourceAgent.CODEX,
                    model_id="gpt-test",
                ),
                source_record_id="mcp-container-test",
                objective="queue a bounded capture",
                outcome="capture remains pending",
            )
        )

    assert result.status == "pending_verification"
    with Database(config.parent / "memory.db").connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from pending_captures where verification_state = 'pending'"
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("select count(*) from source_refs").fetchone()[0] == 0
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0


@pytest.mark.parametrize(
    "builder",
    (build_mcp_capture_container, build_mcp_reconcile_container),
)
def test_mcp_builders_missing_runtime_create_nothing(
    tmp_path: Path,
    builder,
) -> None:
    config = tmp_path / "missing" / "config.toml"

    with pytest.raises(PermissionError, match="private runtime file rejected"):
        builder(config)

    assert not config.parent.exists()
