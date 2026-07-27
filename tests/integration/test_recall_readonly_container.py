import hashlib
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

import project_memory_hub.container as container_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import (
    build_container,
    build_readonly_recall_container,
)
from project_memory_hub.domain import (
    CapturePayload,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    RecallRequest,
    SourceAgent,
)
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.storage.database import Database, ReadonlyDatabaseSnapshot


def _tree_state(root: Path) -> tuple[tuple[object, ...], ...]:
    if not root.exists():
        return ()
    state: list[tuple[object, ...]] = []
    for path in sorted((root, *root.rglob("*")), key=lambda value: str(value)):
        metadata = path.lstat()
        relative = str(path.relative_to(root)) if path != root else "."
        digest = ""
        if stat.S_ISREG(metadata.st_mode):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        state.append(
            (
                relative,
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
    return tuple(state)


def _runtime(tmp_path: Path) -> tuple[Path, Path, Path]:
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
        namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
        payload = CapturePayload(
            cwd=project,
            namespace=namespace,
            source_record_id="session:turn",
            objective="recall exact memory",
            outcome="READONLY_RECALL_MEMORY",
        )
        assert (
            container.capture.capture(
                payload,
                NamespaceVerification(
                    namespace=namespace,
                    source_record_id=payload.source_record_id,
                    verified_by="codex_adapter",
                    verified_at=datetime.now(timezone.utc),
                ),
            ).status
            == "inserted"
        )
    return config, config.parent, project


def test_readonly_recall_container_never_initializes_or_mutates_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, runtime, project = _runtime(tmp_path)
    before = _tree_state(runtime)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("write-capable setup was called")

    monkeypatch.setattr(Database, "initialize", unexpected)
    monkeypatch.setattr(RuntimePaths, "ensure", unexpected)
    monkeypatch.setattr(container_module, "_create_default_config_if_absent", unexpected)
    monkeypatch.setattr(container_module, "_tighten_config_permissions", unexpected)

    with build_readonly_recall_container(config) as container:
        assert isinstance(container.database, ReadonlyDatabaseSnapshot)
        with pytest.raises(PermissionError, match="snapshot database is read-only"):
            with container.database.transaction():
                pass
        result = container.recall.recall(
            RecallRequest(
                cwd=project,
                task="recall exact memory",
                namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test"),
                max_tokens=128,
            )
        )

    assert "READONLY_RECALL_MEMORY" in result.text
    assert _tree_state(runtime) == before


def test_readonly_recall_uses_one_snapshot_when_source_database_changes(
    tmp_path: Path,
) -> None:
    config, _runtime_root, project = _runtime(tmp_path)
    container = build_readonly_recall_container(config)
    try:
        with build_container(config) as writable:
            namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test")
            payload = CapturePayload(
                cwd=project,
                namespace=namespace,
                source_record_id="later-session:later-turn",
                objective="later memory",
                outcome="MUST_NOT_ENTER_EXISTING_SNAPSHOT",
            )
            assert (
                writable.capture.capture(
                    payload,
                    NamespaceVerification(
                        namespace=namespace,
                        source_record_id=payload.source_record_id,
                        verified_by="codex_adapter",
                        verified_at=datetime.now(timezone.utc),
                    ),
                ).status
                == "inserted"
            )

        result = container.recall.recall(
            RecallRequest(
                cwd=project,
                task="later memory recall exact memory",
                namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-test"),
                max_tokens=256,
            )
        )
    finally:
        container.close()

    assert "READONLY_RECALL_MEMORY" in result.text
    assert "MUST_NOT_ENTER_EXISTING_SNAPSHOT" not in result.text


def test_readonly_recall_container_missing_runtime_creates_nothing(
    tmp_path: Path,
) -> None:
    config = tmp_path / "missing-runtime" / "config.toml"

    with pytest.raises(PermissionError, match="private runtime file rejected"):
        build_readonly_recall_container(config)

    assert not config.parent.exists()
