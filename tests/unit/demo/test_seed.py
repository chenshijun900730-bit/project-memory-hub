from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from uuid import UUID

import pytest

from project_memory_hub.config import ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.demo.runtime import DemoWorkspace, prepare_demo_workspace
from project_memory_hub.demo.seed import (
    CODEX_NAMESPACE,
    CHATGPT_NAMESPACE,
    DEMO_LABEL,
    FACT_IDS,
    FIXED_DEMO_TIME,
    MEMORY_IDS,
    OPTIONAL_SOURCE_AGENTS,
    SYNTHETIC_UUIDS,
    build_demo_container,
    seed_demo_database,
)
from project_memory_hub.domain import Namespace, SourceAgent
from project_memory_hub.services.reconcile import ReconcileService


def _workspace(tmp_path: Path, name: str = "one") -> DemoWorkspace:
    root = tmp_path / name
    root.mkdir()
    return prepare_demo_workspace(
        runtime_dir=root / "runtime",
        output_dir=root / "assets",
        repository_root=tmp_path / "repository",
        default_runtime_root=tmp_path / "default-runtime",
        allowed_output_names={"manifest.json"},
    )


def _database_uuids(database_path: Path) -> frozenset[UUID]:
    with sqlite3.connect(database_path) as connection:
        values: list[str] = []
        for table, column in (
            ("projects", "project_id"),
            ("project_facts", "fact_id"),
            ("source_refs", "source_reference_id"),
            ("behavior_memories", "memory_id"),
            ("improvement_proposals", "proposal_id"),
        ):
            values.extend(row[0] for row in connection.execute(f"select {column} from {table}"))
    return frozenset(UUID(value) for value in values)


def _corrupt_demo_seed(database_path: Path, violation: str) -> None:
    with sqlite3.connect(database_path) as connection:
        if violation == "integrity":
            connection.execute("pragma ignore_check_constraints = on")
            connection.execute("update projects set enabled = 2")
        elif violation == "foreign_key":
            connection.execute("pragma foreign_keys = off")
            connection.execute(
                "update behavior_memories set source_reference_id = ? where memory_id = ?",
                ("missing-demo-source-reference", str(MEMORY_IDS[0])),
            )
        elif violation == "schema_versions":
            connection.execute("delete from schema_migrations where version = 13")
        elif violation == "row_counts":
            connection.execute("delete from project_facts where fact_id = ?", (str(FACT_IDS[0]),))
        elif violation == "namespaces":
            connection.execute(
                "update behavior_memories set model_id = ? where source_agent = ?",
                ("altered-demo-model", SourceAgent.CODEX.value),
            )
            connection.execute(
                "update source_refs set capture_model_id = ? where source_agent = ?",
                ("altered-demo-model", SourceAgent.CODEX.value),
            )
        elif violation == "source_provenance":
            connection.execute(
                "update source_refs set capture_model_id = ? where source_agent = ?",
                ("mismatched-demo-model", SourceAgent.CODEX.value),
            )
        else:  # pragma: no cover - test parameter contract
            raise AssertionError(f"unknown violation: {violation}")


def test_seed_uses_existing_container_and_fixed_synthetic_inventory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    inventory = seed_demo_database(workspace)

    assert inventory.label == DEMO_LABEL == "DEMO DATA"
    assert inventory.generated_at == FIXED_DEMO_TIME
    assert inventory.synthetic_uuid_allowlist == SYNTHETIC_UUIDS
    assert _database_uuids(workspace.paths.database) == SYNTHETIC_UUIDS
    assert workspace.paths.database.is_file()
    assert ConfigManager(workspace.runtime_dir / "config.toml").load().enabled_sources == (
        SourceAgent.CODEX,
        SourceAgent.CHATGPT,
    )
    workspace.cleanup_runtime()


def test_seed_has_exact_codex_and_chatgpt_namespaces_without_cross_query(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    inventory = seed_demo_database(workspace)

    with build_container(
        workspace.runtime_dir / "config.toml",
        probe_home=workspace.runtime_dir,
    ) as container:
        project = container.projects.get(inventory.project_id)
        codex = container.memories.list_scoped(project.project_id, CODEX_NAMESPACE)
        chatgpt = container.memories.list_scoped(project.project_id, CHATGPT_NAMESPACE)

        assert codex
        assert chatgpt
        assert {memory.namespace for memory in codex} == {CODEX_NAMESPACE}
        assert {memory.namespace for memory in chatgpt} == {CHATGPT_NAMESPACE}
        assert (
            container.memories.search(
                project.project_id,
                Namespace(
                    source_agent=SourceAgent.CODEX,
                    model_id=CHATGPT_NAMESPACE.model_id,
                ),
                "synthetic",
                20,
            )
            == []
        )
        with pytest.raises(KeyError):
            container.memories.get_scoped(
                project.project_id,
                CHATGPT_NAMESPACE,
                codex[0].memory_id,
            )
    workspace.cleanup_runtime()


def test_seed_keeps_optional_sources_probe_only_and_non_ingesting(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)

    inventory = seed_demo_database(workspace)

    states = {state.source_agent: state for state in inventory.source_states}
    assert set(OPTIONAL_SOURCE_AGENTS) == {
        SourceAgent.TRAE,
        SourceAgent.WORKBUDDY,
        SourceAgent.ZCODE,
        SourceAgent.QODERWORK,
        SourceAgent.CLAUDE_CODE,
    }
    assert all(not states[source].ingestion_allowed for source in OPTIONAL_SOURCE_AGENTS)
    assert states[SourceAgent.TRAE].model_status == "unverifiable"
    assert states[SourceAgent.CODEX].ingestion_allowed
    assert states[SourceAgent.CHATGPT].ingestion_allowed
    workspace.cleanup_runtime()


def test_seed_creates_one_draft_proposal_and_disables_live_reconcile(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    inventory = seed_demo_database(workspace)

    with build_container(
        workspace.runtime_dir / "config.toml",
        probe_home=workspace.runtime_dir,
    ) as container:
        summaries = container.proposals.list_summaries()
        assert len(summaries) == 1
        assert summaries[0].proposal_id == inventory.proposal_id
        assert summaries[0].status == "draft"

    assert inventory.reconcile_receipt == "synthetic-fixed-clock"
    workspace.cleanup_runtime()


def test_seed_manifest_is_byte_stable_across_isolated_runtimes(tmp_path: Path) -> None:
    first = _workspace(tmp_path, "first")
    second = _workspace(tmp_path, "second")

    first_document = seed_demo_database(first).to_json_bytes()
    second_document = seed_demo_database(second).to_json_bytes()

    assert first_document == second_document
    parsed = json.loads(first_document)
    assert parsed["label"] == "DEMO DATA"
    assert parsed["seed_version"] == 1
    first.cleanup_runtime()
    second.cleanup_runtime()


@pytest.mark.parametrize(
    "violation",
    (
        "integrity",
        "foreign_key",
        "schema_versions",
        "row_counts",
        "namespaces",
        "source_provenance",
    ),
)
def test_seed_fails_closed_when_the_final_database_invariants_are_broken(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    violation: str,
) -> None:
    workspace = _workspace(tmp_path)
    original_should_run = ReconcileService.should_run

    def corrupt_before_final_validation(
        service: ReconcileService,
        *,
        now: datetime | None = None,
    ) -> bool:
        should_run = original_should_run(service, now=now)
        _corrupt_demo_seed(service._database.path, violation)
        return should_run

    monkeypatch.setattr(ReconcileService, "should_run", corrupt_before_final_validation)

    with pytest.raises(RuntimeError, match="demo seed database validation failed"):
        seed_demo_database(workspace)

    workspace.cleanup_runtime()


def test_demo_container_requires_a_validated_workspace_not_a_bare_path(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    seed_demo_database(workspace)

    with pytest.raises(TypeError, match="validated DemoWorkspace"):
        build_demo_container(workspace.runtime_dir)  # type: ignore[arg-type]

    workspace.cleanup_runtime()


def test_seed_never_resolves_the_real_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)

    def reject_home() -> Path:
        raise AssertionError("demo seed touched the real HOME")

    monkeypatch.setattr(Path, "home", reject_home)

    inventory = seed_demo_database(workspace)

    assert inventory.namespaces == (CODEX_NAMESPACE, CHATGPT_NAMESPACE)
    workspace.cleanup_runtime()


def test_demo_container_rejects_a_replaced_runtime_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    original = workspace.runtime_dir.with_name("runtime-original")
    workspace.runtime_dir.rename(original)
    workspace.runtime_dir.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="demo runtime rejected"):
        build_demo_container(workspace)

    assert tuple(workspace.runtime_dir.iterdir()) == ()


def test_seed_rejects_a_replaced_runtime_before_any_write(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    original = workspace.runtime_dir.with_name("runtime-original")
    workspace.runtime_dir.rename(original)
    workspace.runtime_dir.mkdir(mode=0o700)

    with pytest.raises(ValueError, match="demo runtime rejected"):
        seed_demo_database(workspace)

    assert tuple(workspace.runtime_dir.iterdir()) == ()


def test_demo_container_rejects_runtime_database_hardlink_without_touching_source(
    tmp_path: Path,
) -> None:
    workspace = _workspace(tmp_path)
    external = tmp_path / "external-database"
    external.write_bytes(b"caller-owned-database")
    before = external.read_bytes()
    os.link(external, workspace.paths.database)

    with pytest.raises(ValueError, match="demo runtime rejected"):
        build_demo_container(workspace)

    assert external.read_bytes() == before


def test_seed_runtime_path_replacement_never_writes_to_the_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    original_runtime = workspace.runtime_dir.with_name("runtime-original")
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o700)
    real_validate = DemoWorkspace.validate_runtime
    replaced = False

    def replace_after_validation(candidate: DemoWorkspace) -> None:
        nonlocal replaced
        real_validate(candidate)
        if candidate is workspace and not replaced:
            replaced = True
            candidate.runtime_dir.rename(original_runtime)
            candidate.runtime_dir.symlink_to(replacement, target_is_directory=True)

    monkeypatch.setattr(DemoWorkspace, "validate_runtime", replace_after_validation)

    with pytest.raises(ValueError, match="demo runtime rejected"):
        seed_demo_database(workspace)

    assert tuple(replacement.iterdir()) == ()
