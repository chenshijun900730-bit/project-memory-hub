import hashlib
import json
import sqlite3
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

import project_memory_hub.storage.path_identity as path_identity_module
import project_memory_hub.storage.projects as projects_module
from project_memory_hub.adapters.base import IngestionError, ReconcileRequiredError
from project_memory_hub.adapters.chatgpt import (
    ChatGPTExportAdapter,
    ExplicitTaskExtractor,
    NormalizedConversation,
    ProjectMatcher,
    VisibleMessage,
)
from project_memory_hub.discovery.fingerprint import fingerprint_git_remote
from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.security.archive import UnsafeArchiveError
from project_memory_hub.security.capture_privacy import MAX_CAPTURE_BYTES, MAX_FIELD_BYTES
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.storage.checkpoints import (
    CheckpointConflictError,
    CheckpointRepository,
)
from project_memory_hub.storage.database import Database, ReadonlySnapshotChangedError
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository
from tests.fixtures.chatgpt.build_fixtures import (
    build_export,
    build_traversal_export,
    conversation,
)


def _services(
    tmp_path: Path,
    project: Path,
    *,
    display_name: str | None = None,
    remote: str | None = None,
):
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    projects = ProjectRepository(database)
    project_record = projects.register(
        ProjectCandidate(
            canonical_path=project,
            display_name=display_name or project.name,
            git_remote_fingerprint=(fingerprint_git_remote(remote) if remote is not None else None),
        )
    )
    memories = MemoryRepository(database)
    redactor = Redactor()
    capture = CaptureService(database, projects, memories, redactor)
    checkpoints = CheckpointRepository(database)
    adapter = ChatGPTExportAdapter(
        matcher=ProjectMatcher(database),
        extractor=ExplicitTaskExtractor(redactor),
        capture=capture,
        checkpoints=checkpoints,
        redactor=redactor,
        database=database,
    )
    return database, project_record, adapter


def _write_counts(database: Database) -> dict[str, int]:
    with database.connect(readonly=True) as connection:
        return {
            table: int(connection.execute(f"select count(*) from {table}").fetchone()[0])
            for table in (
                "source_refs",
                "behavior_memories",
                "memory_issue_resolutions",
                "pending_captures",
                "pending_capture_history",
                "import_receipts",
                "app_state",
            )
        }


def _seed_chatgpt_open_issue(
    adapter: ChatGPTExportAdapter,
    project: Path,
    issue: str,
    *,
    source_record_id: str = "seed-open-issue",
) -> str:
    namespace = Namespace(source_agent=SourceAgent.CHATGPT, model_id="gpt-5")
    verification = NamespaceVerification(
        namespace=namespace,
        source_record_id=source_record_id,
        verified_by="chatgpt_adapter",
        verified_at=datetime.fromtimestamp(1, timezone.utc),
    )
    result = adapter._capture.capture(
        CapturePayload(
            cwd=project,
            namespace=namespace,
            source_record_id=source_record_id,
            objective="",
            outcome="",
            open_issues=[issue],
        ),
        verification,
    )
    assert result.status == "inserted"
    return str(result.inserted_ids[0])


def test_official_export_imports_explicit_chatgpt_memory(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "export.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-1",
                    user_text=f"In {project} fix cache.py",
                    assistant_text=(
                        "Decision: use bounded cache\n"
                        "Verified: pytest tests/test_cache.py\n"
                        "Outcome: cache fixed"
                    ),
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    assert report.confirmation_count == 0
    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select project_id, source_agent, model_id, normalized_content
            from behavior_memories order by normalized_content
            """
        ).fetchall()
    assert {row["project_id"] for row in rows} == {str(project_record.project_id)}
    assert {row["source_agent"] for row in rows} == {"chatgpt"}
    assert {row["model_id"] for row in rows} == {"gpt-5"}
    assert {row["normalized_content"] for row in rows} == {
        "cache fixed",
        "pytest tests/test_cache.py",
        "use bounded cache",
    }


def test_reimport_is_idempotent(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "export.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-1",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: fixed",
                )
            ]
        },
    )

    first = adapter.import_zip(archive)
    second = adapter.import_zip(archive)

    assert first.imported_count == 1
    assert second.duplicate_count == 1
    assert (
        second.resolved_count,
        second.already_resolved_count,
        second.unmatched_resolution_count,
    ) == (0, 0, 0)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 1


def test_resolution_and_receipt_failure_roll_back_only_the_failing_conversation(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    target_id = _seed_chatgpt_open_issue(adapter, project, "exact old issue")
    archive = build_export(
        tmp_path / "receipt-failure.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-safe-before-failure",
                    user_text=f"In {project} fix safe.py",
                    assistant_text="Outcome: safe conversation committed",
                ),
                conversation(
                    "conv-resolution-failure",
                    user_text=f"In {project} fix resolution.py",
                    assistant_text=(
                        "Outcome: resolution attempted\nResolved issue: exact old issue"
                    ),
                ),
            ]
        },
    )
    with database.transaction() as connection:
        connection.execute(
            """
            create trigger inject_chatgpt_receipt_failure
            before insert on import_receipts
            when new.source_record_id = 'conv-resolution-failure'
            begin
                select raise(abort, 'injected receipt failure');
            end
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected receipt failure"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select lifecycle_state from behavior_memories where memory_id = ?",
                (target_id,),
            ).fetchone()[0]
            == "active"
        )
        assert (
            connection.execute("select count(*) from memory_issue_resolutions").fetchone()[0] == 0
        )
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 1
        assert (
            connection.execute("select source_record_id from import_receipts").fetchone()[0]
            == "conv-safe-before-failure"
        )
        assert (
            connection.execute(
                "select count(*) from source_refs where source_record_id = ?",
                ("conv-resolution-failure",),
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "select count(*) from behavior_memories where normalized_content = ?",
                ("resolution attempted",),
            ).fetchone()[0]
            == 0
        )
        safe_source = connection.execute(
            "select created_at from source_refs where source_record_id = ?",
            ("conv-safe-before-failure",),
        ).fetchone()[0]
        observed = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project_record.project_id).lower(),),
        ).fetchone()[0]
    assert observed == safe_source


def test_strict_receipt_conflict_after_capture_rolls_back_every_capture_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "strict-receipt-conflict.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-strict-receipt-conflict",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original = adapter._checkpoints.commit_import_receipt_on_connection

    def conflict_after_capture(
        connection,
        source_hash,
        source_record_id,
        source_agent,
        *,
        confirmation=None,
    ):
        connection.execute(
            """
            insert into import_receipts(
                source_hash, source_record_id, source_agent, imported_at
            ) values (?, ?, ?, ?)
            """,
            (
                source_hash,
                source_record_id,
                source_agent.value,
                "2026-07-16T00:00:00Z",
            ),
        )
        original(
            connection,
            source_hash,
            source_record_id,
            source_agent,
            confirmation=confirmation,
        )

    monkeypatch.setattr(
        adapter._checkpoints,
        "commit_import_receipt_on_connection",
        conflict_after_capture,
    )

    with pytest.raises(CheckpointConflictError, match="checkpoint conflict"):
        adapter.import_zip(archive)

    assert _write_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "app_state": 0,
    }


def test_chatgpt_final_path_guard_rolls_back_capture_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "final-path-guard.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-final-path-guard",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    before = _write_counts(database)
    displaced = tmp_path / "demo-repo-displaced"
    original = adapter._checkpoints.commit_import_receipt_on_connection

    def replace_after_receipt(connection, *args, **kwargs):
        original(connection, *args, **kwargs)
        project.rename(displaced)
        project.mkdir()

    monkeypatch.setattr(
        adapter._checkpoints,
        "commit_import_receipt_on_connection",
        replace_after_receipt,
    )

    try:
        with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
            adapter.import_zip(archive)
    finally:
        if displaced.exists():
            project.rmdir()
            displaced.rename(project)

    assert _write_counts(database) == before


def test_receipt_only_final_guard_revalidates_unrelated_project_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched_project = tmp_path / "matched-project"
    unrelated_project = tmp_path / "unrelated-project"
    matched_project.mkdir()
    unrelated_project.mkdir()
    database, _project_record, adapter = _services(tmp_path, matched_project)
    ProjectRepository(database).register(
        ProjectCandidate(
            canonical_path=unrelated_project,
            display_name="unrelated-project",
        )
    )
    archive = build_export(
        tmp_path / "receipt-only-full-guard.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-receipt-only-full-guard",
                    user_text=f"In {matched_project} fix cache.py",
                    assistant_text="Outcome: done\nResolved issue:",
                )
            ]
        },
    )
    before = _write_counts(database)
    displaced = tmp_path / "unrelated-project-displaced"
    original = adapter._checkpoints.commit_import_receipt_on_connection

    def replace_unrelated_after_receipt(connection, *args, **kwargs):
        original(connection, *args, **kwargs)
        unrelated_project.rename(displaced)
        unrelated_project.mkdir()

    monkeypatch.setattr(
        adapter._checkpoints,
        "commit_import_receipt_on_connection",
        replace_unrelated_after_receipt,
    )

    try:
        with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
            adapter.import_zip(archive)
    finally:
        if displaced.exists():
            unrelated_project.rmdir()
            displaced.rename(unrelated_project)

    assert _write_counts(database) == before


def test_chatgpt_final_generation_guard_rolls_back_registry_capture_and_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "final-generation-guard.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-final-generation-guard",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    before = _write_counts(database)
    with database.connect(readonly=True) as connection:
        generation_before = connection.execute(
            "select generation from project_registry_state where singleton = 1"
        ).fetchone()[0]
    original = adapter._checkpoints.commit_import_receipt_on_connection

    def drift_after_receipt(connection, *args, **kwargs):
        original(connection, *args, **kwargs)
        connection.execute(
            "update projects set display_name = ? where project_id = ?",
            ("drifted-name", str(project_record.project_id).lower()),
        )

    monkeypatch.setattr(
        adapter._checkpoints,
        "commit_import_receipt_on_connection",
        drift_after_receipt,
    )

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)

    assert _write_counts(database) == before
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select display_name from projects where project_id = ?",
            (str(project_record.project_id).lower(),),
        ).fetchone()
        generation_after = connection.execute(
            "select generation from project_registry_state where singleton = 1"
        ).fetchone()[0]
    assert row[0] == project.name
    assert generation_after == generation_before


def test_same_conversation_in_different_archives_commits_new_receipt_as_duplicate(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    value = conversation(
        "conv-cross-archive",
        user_text=f"In {project} fix cache.py",
        assistant_text="Outcome: cache fixed",
    )
    first_archive = build_export(
        tmp_path / "first-archive.zip",
        {"conversations.json": [value]},
    )
    second_archive = build_export(
        tmp_path / "second-archive.zip",
        {"conversations-1.json": [value]},
    )

    first = adapter.import_zip(first_archive)
    second = adapter.import_zip(second_archive)

    assert first.imported_count == 1
    assert second.imported_count == 0
    assert second.duplicate_count == 1
    assert second.results[0].status == "duplicate"
    assert (
        second.resolved_count,
        second.already_resolved_count,
        second.unmatched_resolution_count,
    ) == (0, 0, 0)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 2
        assert connection.execute("select count(*) from source_refs").fetchone()[0] == 1


@pytest.mark.parametrize("reuse_scope", ("project", "model"))
@pytest.mark.parametrize("dry_run", (False, True))
def test_chatgpt_source_reuse_across_project_or_model_is_rejected_without_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reuse_scope: str,
    dry_run: bool,
) -> None:
    first_project = tmp_path / "first-project"
    first_project.mkdir()
    database, _first_record, adapter = _services(tmp_path, first_project)
    first_archive = build_export(
        tmp_path / "first-source.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-shared-source",
                    user_text=f"In {first_project} fix cache.py",
                    assistant_text="Outcome: exact shared outcome",
                    model_slug="model-one",
                )
            ]
        },
    )
    adapter.import_zip(first_archive)
    if reuse_scope == "project":
        second_project = tmp_path / "second-project"
        second_project.mkdir()
        ProjectRepository(database).register(
            ProjectCandidate(canonical_path=second_project, display_name="second-project")
        )
        second_model = "model-one"
    else:
        second_project = first_project
        second_model = "model-two"
    second_archive = build_export(
        tmp_path / "second-source.zip",
        {
            "conversations-1.json": [
                conversation(
                    "conv-shared-source",
                    user_text=f"In {second_project} fix cache.py",
                    assistant_text="Outcome: exact shared outcome",
                    model_slug=second_model,
                )
            ]
        },
    )
    before = _write_counts(database)

    if dry_run:

        def reject_transaction(_self):
            raise AssertionError("dry-run opened a write transaction")

        monkeypatch.setattr(Database, "transaction", reject_transaction)

    with pytest.raises(IngestionError, match="capture provenance mismatch"):
        adapter.import_zip(second_archive, dry_run=dry_run)

    assert _write_counts(database) == before


def test_chatgpt_rejects_extractor_record_from_another_source_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "wrong-source-agent.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-wrong-source-agent",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original_extract = adapter._extractor.extract

    def wrong_source_extract(*args, **kwargs):
        record = original_extract(*args, **kwargs)[0]
        namespace = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5")
        verification = NamespaceVerification(
            namespace=namespace,
            source_record_id=record.source_record_id,
            verified_by="codex_adapter",
            verified_at=record.verification.verified_at,
        )
        return [record.model_copy(update={"namespace": namespace, "verification": verification})]

    monkeypatch.setattr(adapter._extractor, "extract", wrong_source_extract)

    with pytest.raises(IngestionError, match="source namespace mismatch"):
        adapter.import_zip(archive)

    assert _write_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "app_state": 0,
    }


@pytest.mark.parametrize("dry_run", (False, True))
def test_chatgpt_empty_capture_is_rejected_before_live_or_dry_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / f"empty-capture-{dry_run}.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-empty-capture",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original_extract = adapter._extractor.extract

    def empty_capture_extract(*args, **kwargs):
        record = original_extract(*args, **kwargs)[0]
        return [record.model_copy(update={"objective": "", "outcome": ""})]

    monkeypatch.setattr(adapter._extractor, "extract", empty_capture_extract)

    with pytest.raises(IngestionError, match="capture preparation rejected"):
        adapter.import_zip(archive, dry_run=dry_run)

    assert _write_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "app_state": 0,
    }


@pytest.mark.parametrize("dry_run", (False, True))
def test_chatgpt_naive_verification_is_rejected_before_live_or_dry_run_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / f"naive-verification-{dry_run}.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-naive-verification",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original_extract = adapter._extractor.extract

    def naive_verification_extract(*args, **kwargs):
        record = original_extract(*args, **kwargs)[0]
        verification = record.verification.model_copy(
            update={"verified_at": record.verification.verified_at.replace(tzinfo=None)}
        )
        return [record.model_copy(update={"verification": verification})]

    monkeypatch.setattr(adapter._extractor, "extract", naive_verification_extract)

    with pytest.raises(IngestionError, match="capture preparation rejected"):
        adapter.import_zip(archive, dry_run=dry_run)

    assert _write_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "app_state": 0,
    }


def test_chatgpt_rejects_record_bound_to_different_project_before_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    matched_project = tmp_path / "matched-project"
    forged_project = tmp_path / "forged-project"
    matched_project.mkdir()
    forged_project.mkdir()
    database, _matched_record, adapter = _services(tmp_path, matched_project)
    ProjectRepository(database).register(
        ProjectCandidate(canonical_path=forged_project, display_name="forged-project")
    )
    archive = build_export(
        tmp_path / "forged-project-record.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-forged-project-record",
                    user_text=f"In {matched_project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original_extract = adapter._extractor.extract
    original_receipt = adapter._checkpoints.commit_import_receipt_on_connection
    displaced = tmp_path / "forged-project-displaced"
    receipt_called = False

    def forged_project_extract(*args, **kwargs):
        record = original_extract(*args, **kwargs)[0]
        return [record.model_copy(update={"cwd": forged_project})]

    def replace_forged_project_after_receipt(connection, *args, **kwargs):
        nonlocal receipt_called
        original_receipt(connection, *args, **kwargs)
        receipt_called = True
        forged_project.rename(displaced)
        forged_project.mkdir()

    monkeypatch.setattr(adapter._extractor, "extract", forged_project_extract)
    monkeypatch.setattr(
        adapter._checkpoints,
        "commit_import_receipt_on_connection",
        replace_forged_project_after_receipt,
    )

    try:
        with pytest.raises(IngestionError, match="capture binding mismatch"):
            adapter.import_zip(archive)
    finally:
        if displaced.exists():
            forged_project.rmdir()
            displaced.rename(forged_project)

    assert receipt_called is False
    assert _write_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "app_state": 0,
    }


@pytest.mark.parametrize("dry_run", (False, True))
def test_chatgpt_rejects_forged_record_source_before_import_or_dry_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dry_run: bool,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / f"forged-record-source-{dry_run}.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-real-source",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original_extract = adapter._extractor.extract

    def forged_source_extract(*args, **kwargs):
        record = original_extract(*args, **kwargs)[0]
        verification = record.verification.model_copy(update={"source_record_id": "forged-source"})
        return [
            record.model_copy(
                update={
                    "source_record_id": "forged-source",
                    "verification": verification,
                }
            )
        ]

    monkeypatch.setattr(adapter._extractor, "extract", forged_source_extract)

    with pytest.raises(IngestionError, match="capture binding mismatch"):
        adapter.import_zip(archive, dry_run=dry_run)

    assert _write_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "app_state": 0,
    }


@pytest.mark.parametrize("prepared_mismatch", ("source", "project"))
def test_chatgpt_rejects_forged_prepared_binding_before_capture_consumption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepared_mismatch: str,
) -> None:
    project = tmp_path / "demo-repo"
    forged_project = tmp_path / "forged-prepared-project"
    project.mkdir()
    forged_project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    ProjectRepository(database).register(
        ProjectCandidate(
            canonical_path=forged_project,
            display_name="forged-prepared-project",
        )
    )
    archive = build_export(
        tmp_path / "forged-prepared-source.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-prepared-source",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original_prepare = adapter._capture.prepare_verified
    capture_consumed = False

    def forged_prepare(payload, verification):
        selected_payload = (
            payload.model_copy(update={"cwd": forged_project})
            if prepared_mismatch == "project"
            else payload
        )
        prepared = original_prepare(selected_payload, verification)
        assert not isinstance(prepared, CaptureResult)
        return (
            replace(prepared, source_record_id="forged-prepared-source")
            if prepared_mismatch == "source"
            else prepared
        )

    def reject_capture_consumption(*_args, **_kwargs):
        nonlocal capture_consumed
        capture_consumed = True
        raise AssertionError("forged prepared capture was consumed")

    monkeypatch.setattr(adapter._capture, "prepare_verified", forged_prepare)
    monkeypatch.setattr(
        adapter._capture,
        "capture_prepared_on_connection",
        reject_capture_consumption,
    )

    with pytest.raises(IngestionError, match="capture binding mismatch"):
        adapter.import_zip(archive)

    assert capture_consumed is False
    assert _write_counts(database) == {
        "source_refs": 0,
        "behavior_memories": 0,
        "memory_issue_resolutions": 0,
        "pending_captures": 0,
        "pending_capture_history": 0,
        "import_receipts": 0,
        "app_state": 0,
    }


def test_oversized_explicit_labels_are_receipted_without_capture_or_retry(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "oversized-labels.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-field-limit",
                    user_text=f"In {project} fix field.py",
                    assistant_text="Outcome: " + ("x" * (32 * 1024 + 1)),
                ),
                conversation(
                    "conv-list-limit",
                    user_text=f"In {project} fix list.py",
                    assistant_text="\n".join(f"Decision: item-{index}" for index in range(101)),
                ),
            ]
        },
    )

    first = adapter.import_zip(archive)
    second = adapter.import_zip(archive)

    assert first.imported_count == 0
    assert first.warnings == ("no_explicit_statements:2",)
    assert second.duplicate_count == 2
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 2


def test_oversized_resolved_issue_list_is_receipted_without_capture(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "oversized-resolution-list.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-resolution-list-limit",
                    user_text=f"In {project} fix list.py",
                    assistant_text="\n".join(
                        ["Outcome: done"]
                        + [f"Resolved issue: issue-{index}" for index in range(101)]
                    ),
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 0
    assert report.warnings == ("no_explicit_statements:1",)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from source_refs").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 1


def test_resolved_issue_aggregate_over_capture_limit_rejects_the_record():
    extractor = ExplicitTaskExtractor(Redactor())
    declaration_count = MAX_CAPTURE_BYTES // MAX_FIELD_BYTES + 1
    declarations: list[str] = []
    for index in range(declaration_count):
        prefix = f"issue-{index}-"
        declarations.append(prefix + ("x" * (MAX_FIELD_BYTES - len(prefix))))
    assert all(len(value.encode("utf-8")) == MAX_FIELD_BYTES for value in declarations)
    assert sum(len(value.encode("utf-8")) for value in declarations) > MAX_CAPTURE_BYTES
    conversation_value = NormalizedConversation(
        conversation_id="conv-resolution-aggregate-limit",
        title="title",
        messages=(
            VisibleMessage(role="user", text="close issues", model_slug=None),
            VisibleMessage(
                role="assistant",
                text="\n".join(
                    ["Outcome: done"] + [f"Resolved issue: {value}" for value in declarations]
                ),
                model_slug="gpt-5",
            ),
        ),
    )

    assert extractor.extract(conversation_value, project_path=Path("/fixture")) == []


def test_chatgpt_resolution_counts_commit_without_leaking_declaration(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    target_id = _seed_chatgpt_open_issue(adapter, project, "exact old issue")
    archive = build_export(
        tmp_path / "resolved.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-resolve",
                    user_text=f"In {project} fix resolution.py",
                    assistant_text=(
                        "Outcome: resolution verified\nResolved issue: exact old issue"
                    ),
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    assert (
        report.resolved_count,
        report.already_resolved_count,
        report.unmatched_resolution_count,
        report.warning_count,
    ) == (1, 0, 0, 0)
    assert "exact old issue" not in repr(report)
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select lifecycle_state from behavior_memories where memory_id = ?",
                (target_id,),
            ).fetchone()[0]
            == "archived"
        )
        assert (
            connection.execute("select status from memory_issue_resolutions").fetchone()[0]
            == "resolved"
        )


def test_chatgpt_already_resolved_count_is_reported_after_commit(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    _seed_chatgpt_open_issue(adapter, project, "exact old issue")
    first_archive = build_export(
        tmp_path / "first-resolution.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-first-resolution",
                    user_text=f"In {project} fix resolution.py",
                    assistant_text="Outcome: first check\nResolved issue: exact old issue",
                )
            ]
        },
    )
    second_archive = build_export(
        tmp_path / "already-resolved.zip",
        {
            "conversations-1.json": [
                conversation(
                    "conv-already-resolved",
                    user_text=f"In {project} verify resolution.py",
                    assistant_text="Outcome: second check\nResolved issue: exact old issue",
                )
            ]
        },
    )

    first = adapter.import_zip(first_archive)
    second = adapter.import_zip(second_archive)

    assert first.resolved_count == 1
    assert second.imported_count == 1
    assert (
        second.resolved_count,
        second.already_resolved_count,
        second.unmatched_resolution_count,
        second.warning_count,
    ) == (0, 1, 0, 0)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 2


def test_unmatched_resolution_commits_partial_capture_and_compressed_warning(tmp_path):
    markers = (
        "UNKNOWN_EXACT_ISSUE_MARKER_ONE",
        "UNKNOWN_EXACT_ISSUE_MARKER_TWO",
        "UNKNOWN_EXACT_ISSUE_MARKER_THREE",
    )
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "unmatched-resolution.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-unmatched-resolution",
                    user_text=f"In {project} fix resolution.py",
                    assistant_text="\n".join(
                        ["Outcome: checked"] + [f"Resolved issue: {marker}" for marker in markers]
                    ),
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    assert (
        report.resolved_count,
        report.already_resolved_count,
        report.unmatched_resolution_count,
        report.warning_count,
    ) == (0, 0, 3, 3)
    assert report.warnings == ("resolution_not_found:3",)
    assert all(marker not in repr(report) for marker in markers)
    with database.connect(readonly=True) as connection:
        rows = connection.execute("select status from memory_issue_resolutions").fetchall()
    assert [row[0] for row in rows] == ["not_found", "not_found", "not_found"]


def test_chatgpt_dry_run_never_opens_write_transaction_or_changes_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "dry-run-no-transaction.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-dry-run-no-transaction",
                    user_text=f"In {project} fix cache.py",
                    assistant_text=("Outcome: checked\nResolved issue: unknown exact issue"),
                )
            ]
        },
    )
    before = _write_counts(database)
    with database.connect(readonly=True) as connection:
        observed_before = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project_record.project_id).lower(),),
        ).fetchone()[0]

    def reject_transaction(_self):
        raise AssertionError("dry-run opened a write transaction")

    monkeypatch.setattr(Database, "transaction", reject_transaction)

    report = adapter.import_zip(archive, dry_run=True)

    assert report.imported_count == 1
    assert _write_counts(database) == before
    with database.connect(readonly=True) as connection:
        observed_after = connection.execute(
            "select last_observed_change from projects where project_id = ?",
            (str(project_record.project_id).lower(),),
        ).fetchone()[0]
    assert observed_after == observed_before


def test_post_canonicalization_expansion_is_receipted_without_blocking_next_conversation(
    tmp_path,
):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    expanding_decisions = [
        " ".join(
            f"a:{hashlib.sha256(f'{line}-{index}'.encode()).hexdigest()[:6]}.git"
            for index in range(400)
        )
        for line in range(24)
    ]
    archive = build_export(
        tmp_path / "post-canonical-expansion.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-expanding",
                    user_text=f"In {project} fix expansion.py",
                    assistant_text="\n".join(
                        f"Decision: {decision}" for decision in expanding_decisions
                    ),
                ),
                conversation(
                    "conv-safe-after-expansion",
                    user_text=f"In {project} fix safe.py",
                    assistant_text="Outcome: SAFE_CONVERSATION_IMPORTED",
                ),
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    assert report.warnings == ("no_explicit_statements:1",)
    assert report.processed_conversation_ids == (
        "conv-expanding",
        "conv-safe-after-expansion",
    )
    with database.connect(readonly=True) as connection:
        contents = {
            row[0]
            for row in connection.execute(
                "select normalized_content from behavior_memories"
            ).fetchall()
        }
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 2
    assert contents == {"SAFE_CONVERSATION_IMPORTED"}


@pytest.mark.parametrize(
    "malformation",
    (
        "lone_surrogate_message",
        "lone_surrogate_title",
        "terminal_control_message",
        "non_string_role",
    ),
)
def test_malformed_conversation_is_receipted_without_blocking_the_next_one(
    tmp_path,
    malformation,
):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    malformed = conversation(
        "conv-malformed",
        user_text=f"In {project} fix malformed.py",
        assistant_text="Outcome: rejected",
    )
    if malformation == "lone_surrogate_message":
        malformed["mapping"]["a1"]["message"]["content"]["parts"] = ["Outcome: bad-\ud800-text"]
    elif malformation == "lone_surrogate_title":
        malformed["title"] = "bad-\ud800-title"
    elif malformation == "terminal_control_message":
        malformed["mapping"]["a1"]["message"]["content"]["parts"] = [
            "Outcome: bad-\x1b]0;PMH-PWN\x07-text"
        ]
    else:
        malformed["mapping"]["a1"]["message"]["author"]["role"] = []
    safe = conversation(
        "conv-safe-after-malformed",
        user_text=f"In {project} fix safe.py",
        assistant_text="Outcome: SAFE_AFTER_MALFORMED",
    )
    archive = tmp_path / f"{malformation}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        output.writestr(
            "conversations.json",
            json.dumps([malformed, safe], ensure_ascii=True, separators=(",", ":")),
        )

    first = adapter.import_zip(archive)
    second = adapter.import_zip(archive)

    assert first.imported_count == 1
    assert first.processed_conversation_ids == (
        "conv-malformed",
        "conv-safe-after-malformed",
    )
    if malformation.startswith("lone_surrogate"):
        expected_warning = "invalid_unicode:1"
    elif malformation == "terminal_control_message":
        expected_warning = "unsafe_text_control:1"
    else:
        expected_warning = "malformed_conversation:1"
    assert first.warnings == (expected_warning,)
    assert second.duplicate_count == 2
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 2
        contents = {
            row[0]
            for row in connection.execute(
                "select normalized_content from behavior_memories"
            ).fetchall()
        }
    assert contents == {"SAFE_AFTER_MALFORMED"}


def test_huge_create_time_is_ignored_without_blocking_other_conversations(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    huge_time = conversation(
        "conv-huge-time",
        user_text=f"In {project} fix huge_time.py",
        assistant_text="Outcome: HUGE_TIME_IMPORTED",
    )
    huge_time["mapping"]["a1"]["message"]["create_time"] = 10**1000
    safe = conversation(
        "conv-safe-after-time",
        user_text=f"In {project} fix safe_time.py",
        assistant_text="Outcome: SAFE_TIME_IMPORTED",
    )
    archive = build_export(
        tmp_path / "huge-time.zip",
        {"conversations.json": [huge_time, safe]},
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 2
    assert report.warnings == ()
    with database.connect(readonly=True) as connection:
        contents = {
            row[0]
            for row in connection.execute(
                "select normalized_content from behavior_memories"
            ).fetchall()
        }
    assert contents == {"HUGE_TIME_IMPORTED", "SAFE_TIME_IMPORTED"}


def test_system_and_tool_nodes_do_not_hide_a_later_completed_task(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    _database_value, _project_record, adapter = _services(tmp_path, project)
    value = conversation(
        "conv-with-internal-roles",
        user_text=f"In {project} fix roles.py",
        assistant_text="Outcome: INTERNAL_ROLES_SKIPPED",
    )
    value["mapping"]["u1"]["children"] = ["system1"]
    value["mapping"]["system1"] = {
        "id": "system1",
        "parent": "u1",
        "children": ["tool1"],
        "message": {
            "author": {"role": "system"},
            "content": {"parts": ["internal system text"]},
            "metadata": {},
            "create_time": 1.2,
        },
    }
    value["mapping"]["tool1"] = {
        "id": "tool1",
        "parent": "system1",
        "children": ["a1"],
        "message": {
            "author": {"role": "tool"},
            "content": {"parts": ["internal tool text"]},
            "metadata": {},
            "create_time": 1.5,
        },
    }
    value["mapping"]["a1"]["parent"] = "tool1"
    archive = build_export(
        tmp_path / "internal-roles.zip",
        {"conversations.json": [value]},
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    assert report.warnings == ()


def test_project_match_confidence_path_remote_name_and_ambiguity(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, _adapter = _services(
        tmp_path,
        project,
        remote="https://github.com/example/demo-repo.git",
    )
    matcher = ProjectMatcher(database)

    absolute = matcher.match(
        NormalizedConversation.synthetic("c-path", f"Run pytest in {project}/tests/test_cache.py")
    )
    quoted_absolute = matcher.match(
        NormalizedConversation.synthetic("c-quoted-path", f'Run pytest in "{project}"')
    )
    remote = matcher.match(
        NormalizedConversation.synthetic(
            "c-remote",
            "Fix cache.py in https://github.com/example/demo-repo.git",
        )
    )
    name = matcher.match(
        NormalizedConversation.synthetic("c-name", "Fix cache.py in demo-repo and run pytest")
    )
    ambiguous = matcher.match(
        NormalizedConversation.synthetic("c-none", "Discuss Python architecture")
    )

    assert absolute.project_id == project_record.project_id
    assert absolute.confidence == 1.0
    assert quoted_absolute.project_id == project_record.project_id
    assert quoted_absolute.confidence == 1.0
    assert remote.project_id == project_record.project_id
    assert remote.confidence >= 0.95
    assert name.project_id == project_record.project_id
    assert name.confidence == 0.85
    assert ambiguous.project_id is None
    assert ambiguous.requires_confirmation is True
    assert all("demo-repo" not in item for item in ambiguous.evidence)


def test_project_matcher_ignores_a_replaced_registered_directory(tmp_path: Path) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, _adapter = _services(tmp_path, project)
    displaced = tmp_path / "demo-repo-displaced"
    project.rename(displaced)
    project.mkdir()

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        ProjectMatcher(database).match(
            NormalizedConversation.synthetic(
                "c-replaced",
                f"Run pytest in {project}/tests/test_cache.py",
            )
        )


def test_project_matcher_revalidates_a_cached_snapshot_before_returning(tmp_path: Path) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, _adapter = _services(tmp_path, project)
    matcher = ProjectMatcher(database)
    snapshot = matcher.verified_project_snapshot()
    displaced = tmp_path / "demo-repo-displaced"
    project.rename(displaced)
    project.mkdir()

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        matcher.match(
            NormalizedConversation.synthetic(
                "c-replaced-after-snapshot",
                f"Run pytest in {project}/tests/test_cache.py",
            ),
            project_snapshot=snapshot,
        )


@pytest.mark.parametrize("mutation", ("relink", "disable"))
def test_project_matcher_revalidates_database_state_before_using_a_snapshot(
    tmp_path: Path,
    mutation: str,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, _adapter = _services(tmp_path, project)
    matcher = ProjectMatcher(database)
    snapshot = matcher.verified_project_snapshot()
    projects = ProjectRepository(database)
    if mutation == "relink":
        replacement = tmp_path / "replacement"
        replacement.mkdir()
        projects.relink(project_record.project_id, replacement)
    else:
        projects.set_enabled(project_record.project_id, False)

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        matcher.match(
            NormalizedConversation.synthetic(
                f"c-{mutation}-after-snapshot",
                f"Run pytest in {project}/tests/test_cache.py",
            ),
            project_snapshot=snapshot,
        )


@pytest.mark.parametrize(
    ("column", "replacement"),
    (("display_name", "renamed-project"), ("git_remote_fingerprint", "changed-remote")),
)
def test_project_matcher_revalidates_matching_metadata_before_using_a_snapshot(
    tmp_path: Path,
    column: str,
    replacement: str,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, _adapter = _services(
        tmp_path,
        project,
        remote="https://github.com/example/demo-repo.git",
    )
    matcher = ProjectMatcher(database)
    snapshot = matcher.verified_project_snapshot()
    assert column in {"display_name", "git_remote_fingerprint"}
    with database.transaction() as connection:
        connection.execute(
            f"update projects set {column} = ? where project_id = ?",
            (replacement, str(project_record.project_id)),
        )

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        matcher.match(
            NormalizedConversation.synthetic(
                f"c-{column}-after-snapshot",
                "Fix cache.py in demo-repo and run pytest",
            ),
            project_snapshot=snapshot,
        )


def test_project_matcher_maps_readonly_snapshot_drift_to_reconcile_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, _adapter = _services(tmp_path, project)
    matcher = ProjectMatcher(database)
    real_generation = matcher._project_generation
    mutated = False

    def mutate_after_generation(connection):
        nonlocal mutated
        generation = real_generation(connection)
        if not mutated:
            mutated = True
            ProjectRepository(database).set_enabled(project_record.project_id, False)
        return generation

    monkeypatch.setattr(matcher, "_project_generation", mutate_after_generation)

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        matcher.verified_project_snapshot()


def test_project_matcher_rejects_in_process_identity_change_after_darwin_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, _adapter = _services(tmp_path, project)
    metadata = project.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project_record.project_id)),
        )
    identities = iter(
        (
            (metadata.st_dev, metadata.st_ino),
            (metadata.st_dev + 2, metadata.st_ino),
        )
    )
    latest = (metadata.st_dev + 2, metadata.st_ino)

    def changing_identity(_path: Path):
        return next(identities, latest)

    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    monkeypatch.setattr(
        path_identity_module,
        "complete_directory_identity",
        changing_identity,
    )

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        ProjectMatcher(database).verified_project_snapshot()


def test_project_matcher_accepts_stable_persisted_darwin_device_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, _adapter = _services(tmp_path, project)
    metadata = project.stat()
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = ? where project_id = ?",
            (metadata.st_dev + 1, str(project_record.project_id)),
        )
    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")

    snapshot = ProjectMatcher(database).verified_project_snapshot()

    assert tuple(item.project_id for item in snapshot.projects) == (project_record.project_id,)


def test_project_matcher_maps_winner_read_drift_to_reconcile_required(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, _adapter = _services(tmp_path, project)
    snapshot = ProjectMatcher(database).verified_project_snapshot()
    real_connect = database.connect

    class DriftingDatabase:
        def __init__(self) -> None:
            self.readonly_calls = 0

        @contextmanager
        def connect(self, readonly: bool = False):
            if readonly:
                self.readonly_calls += 1
            with real_connect(readonly=readonly) as connection:
                yield connection
            if readonly and self.readonly_calls == 2:
                raise ReadonlySnapshotChangedError("read-only snapshot unavailable")

    matcher = ProjectMatcher(DriftingDatabase())  # type: ignore[arg-type]

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        matcher.match(
            NormalizedConversation.synthetic(
                "c-winner-read-drift",
                f"Run pytest in {project}/tests/test_cache.py",
            ),
            project_snapshot=snapshot,
        )


def test_chatgpt_import_does_not_consume_receipts_for_untrusted_legacy_projects(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    with database.transaction() as connection:
        connection.execute(
            "update projects set path_device = null, path_inode = null where project_id = ?",
            (str(project_record.project_id),),
        )
    archive = build_export(
        tmp_path / "legacy-project.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-legacy-project",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0

    ProjectRepository(database).register(
        ProjectCandidate(canonical_path=project, display_name=project.name)
    )
    report = adapter.import_zip(archive)

    assert report.imported_count == 1


def test_chatgpt_import_does_not_consume_receipts_for_a_replaced_project(
    tmp_path: Path,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "replaced-project.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-replaced-project",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    displaced = tmp_path / "demo-repo-displaced"
    project.rename(displaced)
    project.mkdir()

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0


@pytest.mark.parametrize("conversation_kind", ["malformed", "no_completed_segment"])
def test_receipt_only_conversations_do_not_consume_receipts_after_registry_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conversation_kind: str,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    conversation_id = f"conv-receipt-drift-{conversation_kind}"
    value = (
        {"id": conversation_id, "title": "Malformed", "mapping": []}
        if conversation_kind == "malformed"
        else conversation(
            conversation_id,
            user_text=f"In {project} discuss cache.py",
            assistant_text="The discussion is still in progress.",
        )
    )
    archive = build_export(
        tmp_path / f"{conversation_kind}.zip",
        {"conversations.json": [value]},
    )
    checkpoints = adapter._checkpoints
    original_commit = checkpoints.commit_import_receipt_on_connection
    drifted = False

    def drifting_commit(connection, *args, **kwargs):
        nonlocal drifted
        original_commit(connection, *args, **kwargs)
        if not drifted:
            connection.execute(
                "update projects set enabled = 0 where project_id = ?",
                (str(project_record.project_id).lower(),),
            )
            drifted = True

    monkeypatch.setattr(
        checkpoints,
        "commit_import_receipt_on_connection",
        drifting_commit,
    )

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)

    assert drifted is True
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "conversation_kind",
    ["malformed", "no_completed_segment", "no_project_match"],
)
def test_unmatched_receipts_revalidate_physical_project_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    conversation_kind: str,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    conversation_id = f"conv-physical-receipt-drift-{conversation_kind}"
    if conversation_kind == "malformed":
        value = {"id": conversation_id, "title": "Malformed", "mapping": []}
    elif conversation_kind == "no_completed_segment":
        value = conversation(
            conversation_id,
            user_text=f"In {project} discuss cache.py",
            assistant_text="The discussion is still in progress.",
        )
    else:
        value = conversation(
            conversation_id,
            user_text="Discuss Python architecture",
            assistant_text="Outcome: discussion completed",
        )
    archive = build_export(
        tmp_path / f"physical-{conversation_kind}.zip",
        {"conversations.json": [value]},
    )
    checkpoints = adapter._checkpoints
    original_commit = checkpoints.commit_import_receipt_on_connection
    displaced = tmp_path / "demo-repo-displaced"
    drifted = False

    def drifting_commit(connection, *args, **kwargs):
        nonlocal drifted
        original_commit(connection, *args, **kwargs)
        if not drifted:
            project.rename(displaced)
            project.mkdir()
            drifted = True

    monkeypatch.setattr(
        checkpoints,
        "commit_import_receipt_on_connection",
        drifting_commit,
    )

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)

    assert drifted is True
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0


def test_chatgpt_disabled_inner_project_shadows_enabled_outer_without_receipt(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "workspace"
    inner = outer / "packages" / "inner"
    inner.mkdir(parents=True)
    database, outer_record, adapter = _services(tmp_path, outer)
    projects = ProjectRepository(database)
    inner_record = projects.register(ProjectCandidate(canonical_path=inner, display_name="Inner"))
    projects.set_enabled(inner_record.project_id, False)
    archive = build_export(
        tmp_path / "disabled-inner.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-disabled-inner",
                    user_text=f"In {inner} fix cache.py",
                    assistant_text="Outcome: DISABLED_INNER_SENTINEL",
                )
            ]
        },
    )

    blocked = adapter.import_zip(archive)

    assert blocked.imported_count == 0
    assert blocked.confirmation_count == 1
    assert "disabled_project_match:1" in blocked.warnings
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
        assert (
            connection.execute(
                "select count(*) from app_state where name like 'chatgpt_confirmation:%'"
            ).fetchone()[0]
            == 0
        )

    projects.set_enabled(inner_record.project_id, True)
    imported = adapter.import_zip(archive)

    assert imported.imported_count == 1
    with database.connect(readonly=True) as connection:
        project_ids = {
            row["project_id"]
            for row in connection.execute("select project_id from behavior_memories").fetchall()
        }
    assert project_ids == {str(inner_record.project_id)}
    assert str(outer_record.project_id) not in project_ids


@pytest.mark.parametrize(("opening", "closing"), [('"', '"'), ("`", "`")])
def test_quoted_disabled_inner_path_shadows_outer_after_relink(
    tmp_path: Path,
    opening: str,
    closing: str,
) -> None:
    outer = tmp_path / "workspace"
    old_inner = tmp_path / "old-service"
    new_inner = outer / "packages" / "inner"
    outer.mkdir()
    old_inner.mkdir()
    new_inner.mkdir(parents=True)
    database, outer_record, adapter = _services(tmp_path, outer)
    projects = ProjectRepository(database)
    inner_record = projects.register(
        ProjectCandidate(canonical_path=old_inner, display_name=old_inner.name)
    )
    projects.relink(inner_record.project_id, new_inner)
    projects.set_enabled(inner_record.project_id, False)
    archive = build_export(
        tmp_path / f"quoted-disabled-{ord(opening)}.zip",
        {
            "conversations.json": [
                conversation(
                    f"conv-quoted-disabled-{ord(opening)}",
                    user_text=(
                        f"Fix cache.py in {opening}{new_inner}/src/cache.py{closing} and run pytest"
                    ),
                    assistant_text="Outcome: QUOTED_DISABLED_INNER_SENTINEL",
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 0
    assert report.confirmation_count == 1
    assert "disabled_project_match:1" in report.warnings
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
    assert outer_record.project_id != inner_record.project_id


def test_weaker_disabled_child_candidate_shadows_stronger_outer_remote(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "workspace"
    inner = outer / "packages" / "inner"
    inner.mkdir(parents=True)
    database, outer_record, adapter = _services(
        tmp_path,
        outer,
        remote="https://github.com/example/mono.git",
    )
    projects = ProjectRepository(database)
    inner_record = projects.register(ProjectCandidate(canonical_path=inner, display_name="inner"))
    projects.set_enabled(inner_record.project_id, False)
    archive = build_export(
        tmp_path / "weaker-disabled-child.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-weaker-disabled-child",
                    user_text=(
                        "Fix cache.py in inner and run pytest for "
                        "https://github.com/example/mono.git"
                    ),
                    assistant_text="Outcome: WEAKER_DISABLED_CHILD_SENTINEL",
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 0
    assert report.confirmation_count == 1
    assert "disabled_project_match:1" in report.warnings
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
    assert outer_record.project_id != inner_record.project_id


def test_relative_disabled_inner_path_uses_canonical_name_after_relink(
    tmp_path: Path,
) -> None:
    outer = tmp_path / "workspace"
    old_inner = tmp_path / "old-service"
    new_inner = outer / "packages" / "inner"
    outer.mkdir()
    old_inner.mkdir()
    new_inner.mkdir(parents=True)
    database, outer_record, adapter = _services(tmp_path, outer)
    projects = ProjectRepository(database)
    inner_record = projects.register(
        ProjectCandidate(canonical_path=old_inner, display_name=old_inner.name)
    )
    projects.relink(inner_record.project_id, new_inner)
    projects.set_enabled(inner_record.project_id, False)
    archive = build_export(
        tmp_path / "relative-disabled-inner.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-relative-disabled-inner",
                    user_text="Fix cache.py in workspace/packages/inner and run pytest",
                    assistant_text="Outcome: RELATIVE_DISABLED_INNER_SENTINEL",
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 0
    assert report.confirmation_count == 1
    assert "disabled_project_match:1" in report.warnings
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
    assert outer_record.project_id != inner_record.project_id


def test_chatgpt_import_aborts_if_the_project_registry_drifts_mid_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "registry-drift.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-registry-drift",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    matcher = adapter._matcher
    original_match = matcher.match
    replacement = tmp_path / "replacement"
    replacement.mkdir()

    def drifting_match(*args, **kwargs):
        match = original_match(*args, **kwargs)
        ProjectRepository(database).relink(project_record.project_id, replacement)
        return match

    monkeypatch.setattr(matcher, "match", drifting_match)

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0


def test_chatgpt_import_maps_capture_registry_drift_to_reconcile_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "capture-registry-drift.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-capture-registry-drift",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    replacement = tmp_path / "capture-replacement"
    replacement.mkdir()

    original_prepare = adapter._capture.prepare_verified

    def drifting_prepare(*args, **kwargs):
        prepared = original_prepare(*args, **kwargs)
        ProjectRepository(database).relink(project_record.project_id, replacement)
        return prepared

    monkeypatch.setattr(adapter._capture, "prepare_verified", drifting_prepare)

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0


def test_chatgpt_import_maps_receipt_read_drift_to_reconcile_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "receipt-read-drift.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-receipt-read-drift",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )

    def drifting_receipt(*_args, **_kwargs):
        raise ReadonlySnapshotChangedError("read-only snapshot unavailable")

    monkeypatch.setattr(adapter._checkpoints, "receipt_exists", drifting_receipt)

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0


def test_chatgpt_dry_run_revalidates_a_match_after_extraction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    _database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "dry-run-extraction-drift.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-dry-run-extraction-drift",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: cache fixed",
                )
            ]
        },
    )
    original_extract = adapter._extractor.extract
    displaced = tmp_path / "demo-repo-displaced"

    def drifting_extract(*args, **kwargs):
        records = original_extract(*args, **kwargs)
        project.rename(displaced)
        project.mkdir()
        return records

    monkeypatch.setattr(adapter._extractor, "extract", drifting_extract)

    with pytest.raises(ReconcileRequiredError, match="requires reconcile"):
        adapter.import_zip(archive, dry_run=True)


def test_chatgpt_import_revalidates_only_matching_project_paths_per_conversation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    repository = ProjectRepository(database)
    extra_projects = tuple(tmp_path / f"extra-{index}" for index in range(24))
    for extra_project in extra_projects:
        extra_project.mkdir()
        repository.register(
            ProjectCandidate(
                canonical_path=extra_project,
                display_name=extra_project.name,
            )
        )
    conversation_count = 12
    archive = build_export(
        tmp_path / "path-validation-scale.zip",
        {
            "conversations.json": [
                conversation(
                    f"conv-scale-{index}",
                    user_text=f"In {project} fix cache-{index}.py",
                    assistant_text="Outcome: cache fixed",
                )
                for index in range(conversation_count)
            ]
        },
    )
    real_validation = path_identity_module.complete_directory_identity
    validation_count = 0
    matcher = adapter._matcher
    real_project_rows = matcher._project_rows
    project_row_queries = 0
    real_repository_identity = projects_module.complete_directory_identity
    repository_identity_reads = 0

    def counted_validation(*args, **kwargs):
        nonlocal validation_count
        validation_count += 1
        return real_validation(*args, **kwargs)

    def counted_project_rows(connection):
        nonlocal project_row_queries
        project_row_queries += 1
        return real_project_rows(connection)

    def counted_repository_identity(path: Path):
        nonlocal repository_identity_reads
        repository_identity_reads += 1
        return real_repository_identity(path)

    monkeypatch.setattr(
        path_identity_module,
        "complete_directory_identity",
        counted_validation,
    )
    monkeypatch.setattr(matcher, "_project_rows", counted_project_rows)
    monkeypatch.setattr(
        projects_module,
        "complete_directory_identity",
        counted_repository_identity,
    )

    report = adapter.import_zip(archive)

    project_count = len(extra_projects) + 1
    assert report.imported_count == conversation_count
    assert validation_count <= project_count * 2 + conversation_count * 5
    assert project_row_queries <= 2
    assert repository_identity_reads <= conversation_count * 6


def test_missing_model_uses_unknown_and_never_writes_codex_namespace(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "missing-model.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-unknown",
                    user_text=f"In {project} fix cache.py",
                    assistant_text="Outcome: fixed",
                    model_slug=None,
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    with database.connect(readonly=True) as connection:
        rows = connection.execute("select source_agent, model_id from behavior_memories").fetchall()
    assert [(row["source_agent"], row["model_id"]) for row in rows] == [("chatgpt", "unknown")]


def test_numbered_members_are_processed_in_numeric_order(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    _database_value, _project_record, adapter = _services(tmp_path, project)
    members = {
        "conversations-2.json": [
            conversation(
                "conv-2",
                user_text=f"In {project} edit two.py",
                assistant_text="Outcome: two",
            )
        ],
        "conversations-10.json": [
            conversation(
                "conv-10",
                user_text=f"In {project} edit ten.py",
                assistant_text="Outcome: ten",
            )
        ],
        "conversations.json": [
            conversation(
                "conv-0",
                user_text=f"In {project} edit zero.py",
                assistant_text="Outcome: zero",
            )
        ],
        "conversations-1.json": [
            conversation(
                "conv-1",
                user_text=f"In {project} edit one.py",
                assistant_text="Outcome: one",
            )
        ],
        "other.json": [conversation("ignored", user_text="x", assistant_text="x")],
    }
    archive = build_export(tmp_path / "numbered.zip", members)

    report = adapter.import_zip(archive)

    assert report.processed_members == (
        "conversations.json",
        "conversations-1.json",
        "conversations-2.json",
        "conversations-10.json",
    )
    assert report.processed_conversation_ids == (
        "conv-0",
        "conv-1",
        "conv-2",
        "conv-10",
    )


@pytest.mark.parametrize(
    "unsafe_id",
    ("bad/id", " conv-space ", "intranet:PRIVATE_REPO.git", ".env", "id_rsa"),
)
def test_unsafe_conversation_id_is_skipped_without_aborting_other_imports(
    tmp_path,
    unsafe_id,
):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "unsafe-id.zip",
        {
            "conversations.json": [
                conversation(
                    unsafe_id,
                    user_text=f"In {project} edit rejected.py",
                    assistant_text="Outcome: rejected",
                ),
                conversation(
                    "conv-safe",
                    user_text=f"In {project} edit safe.py",
                    assistant_text="Outcome: exact outcome",
                ),
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    assert report.processed_conversation_ids == ("conv-safe",)
    assert "malformed_conversation:1" in report.warnings
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from source_refs").fetchone()[0] == 1


def test_model_slug_requiring_whitespace_normalization_stays_in_unknown_namespace(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "model-whitespace.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-model-whitespace",
                    user_text=f"In {project} edit model.py",
                    assistant_text="Outcome: exact outcome",
                    model_slug=" gpt-5 ",
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute("select distinct model_id from behavior_memories").fetchone()[0]
            == "unknown"
        )


def test_ambiguous_match_is_confirmation_only_and_dry_run_writes_nothing(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "ambiguous.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-ambiguous",
                    user_text="Discuss Python architecture",
                    assistant_text="Decision: use a cache",
                )
            ]
        },
    )

    dry = adapter.import_zip(archive, dry_run=True)
    with database.connect(readonly=True) as connection:
        after_dry = tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in ("behavior_memories", "import_receipts", "app_state")
        )
    persisted = adapter.import_zip(archive)

    assert dry.confirmation_count == persisted.confirmation_count == 1
    assert after_dry == (0, 0, 0)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 1
        state = connection.execute(
            "select value_json from app_state where name like 'chatgpt_confirmation:%'"
        ).fetchone()[0]
    assert "Discuss Python" not in state
    assert "use a cache" not in state


def test_secret_is_redacted_from_database_report_and_warnings(tmp_path):
    marker = "SUPER_PRIVATE_PASSWORD_MARKER"
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "secret.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-secret",
                    user_text="In demo-repo fix safe.py",
                    assistant_text=f"Risk: password={marker}\nOutcome: safe",
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert marker not in repr(report)
    with database.connect(readonly=True) as connection:
        dump = " ".join(
            str(tuple(row))
            for table in (
                "behavior_memories",
                "source_refs",
                "import_receipts",
                "app_state",
            )
            for row in connection.execute(f"select * from {table}").fetchall()
        )
    assert marker not in dump
    assert "[REDACTED:password]" in dump


def test_branch_cycle_or_orphan_is_ignored_with_fixed_warning(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    _database_value, _project_record, adapter = _services(tmp_path, project)
    cyclic = conversation(
        "conv-cycle",
        user_text=f"In {project} fix cycle.py",
        assistant_text="Outcome: never",
    )
    cyclic["mapping"]["u1"]["parent"] = "a1"
    cyclic["mapping"]["a1"]["children"] = ["u1"]
    orphan = conversation(
        "conv-orphan",
        user_text=f"In {project} fix orphan.py",
        assistant_text="Outcome: never",
    )
    orphan["mapping"]["a1"]["parent"] = "missing"
    archive = build_export(
        tmp_path / "bad-tree.zip",
        {"conversations.json": [cyclic, orphan]},
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 0
    assert set(report.warnings) == {
        "conversation_cycle:1",
        "conversation_orphan:1",
    }


def test_hostile_archive_is_rejected_before_conversation_parsing(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    _database_value, _project_record, adapter = _services(tmp_path, project)
    archive = build_traversal_export(tmp_path / "hostile.zip")

    with pytest.raises(UnsafeArchiveError, match="path traversal"):
        adapter.import_zip(archive)


def test_duplicate_conversation_members_are_rejected_before_any_write(tmp_path):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    duplicate = conversation(
        "conv-duplicate",
        user_text=f"In {project} fix duplicate.py",
        assistant_text="Outcome: fixed",
    )
    archive = build_export(
        tmp_path / "duplicate.zip",
        {
            "conversations.json": [duplicate],
            "conversations-1.json": [duplicate],
        },
    )

    with pytest.raises(UnsafeArchiveError, match="duplicate conversation id"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0


def test_extractor_keeps_labels_and_discards_unlabeled_prose():
    extractor = ExplicitTaskExtractor(Redactor())
    conversation_value = NormalizedConversation(
        conversation_id="conv",
        title="title",
        messages=(
            VisibleMessage(role="user", text="unlabeled user prose", model_slug=None),
            VisibleMessage(
                role="assistant",
                text=(
                    "unlabeled assistant prose\n"
                    "Decision: bounded\nVerified: pytest\nOutcome: done\n"
                    "Failed: old way\nPreference: concise\nRisk: race\n"
                    "Open issue: Windows"
                ),
                model_slug="gpt-5",
            ),
        ),
    )

    extracted = extractor.extract(conversation_value, project_path=Path("/fixture"))

    assert len(extracted) == 1
    record = extracted[0]
    assert record.decisions == ("bounded",)
    assert record.verified_commands == ("pytest",)
    assert record.outcome == "done"
    assert record.failed_attempts == ("old way",)
    assert record.preferences == ("concise",)
    assert record.risks == ("race",)
    assert record.open_issues == ("Windows",)
    assert "unlabeled" not in repr(record)


def test_extractor_keeps_explicit_resolved_issues_and_deduplicates_first_seen():
    extractor = ExplicitTaskExtractor(Redactor())
    conversation_value = NormalizedConversation(
        conversation_id="conv-resolved",
        title="title",
        messages=(
            VisibleMessage(role="user", text="close issues", model_slug=None),
            VisibleMessage(
                role="assistant",
                text=(
                    "Outcome: done\n"
                    "Resolved issue:   first   exact issue  \n"
                    "Resolved issue: second exact issue\n"
                    "Resolved issue: first exact issue\n"
                    "Resolved issue:  second   exact issue "
                ),
                model_slug="gpt-5",
            ),
        ),
    )

    extracted = extractor.extract(conversation_value, project_path=Path("/fixture"))

    assert len(extracted) == 1
    assert extracted[0].resolved_open_issues == (
        "first exact issue",
        "second exact issue",
    )


def test_extractor_does_not_infer_resolution_from_ordinary_prose():
    extractor = ExplicitTaskExtractor(Redactor())
    conversation_value = NormalizedConversation(
        conversation_id="conv-prose-resolution",
        title="title",
        messages=(
            VisibleMessage(role="user", text="close issues", model_slug=None),
            VisibleMessage(
                role="assistant",
                text="I resolved issue exact old issue.\nOutcome: done",
                model_slug="gpt-5",
            ),
        ),
    )

    extracted = extractor.extract(conversation_value, project_path=Path("/fixture"))

    assert len(extracted) == 1
    assert extracted[0].resolved_open_issues == ()


def test_extractor_rejects_normalized_open_resolved_intersection():
    extractor = ExplicitTaskExtractor(Redactor())
    conversation_value = NormalizedConversation(
        conversation_id="conv-conflicting-resolution",
        title="title",
        messages=(
            VisibleMessage(role="user", text="close issues", model_slug=None),
            VisibleMessage(
                role="assistant",
                text=(
                    "Outcome: done\n"
                    "Open issue: exact   old issue\n"
                    "Resolved issue:  exact old issue "
                ),
                model_slug="gpt-5",
            ),
        ),
    )

    assert extractor.extract(conversation_value, project_path=Path("/fixture")) == []


@pytest.mark.parametrize("resolution_line", ("Resolved issue:", "Resolved issue:   "))
def test_extractor_rejects_empty_resolution_label(resolution_line: str):
    extractor = ExplicitTaskExtractor(Redactor())
    conversation_value = NormalizedConversation(
        conversation_id="conv-empty-resolution",
        title="title",
        messages=(
            VisibleMessage(role="user", text="close issues", model_slug=None),
            VisibleMessage(
                role="assistant",
                text=f"Outcome: done\n{resolution_line}",
                model_slug="gpt-5",
            ),
        ),
    )

    assert extractor.extract(conversation_value, project_path=Path("/fixture")) == []


def test_latest_completed_segment_is_the_only_resolution_source():
    extractor = ExplicitTaskExtractor(Redactor())
    conversation_value = NormalizedConversation(
        conversation_id="conv-resolution-segments",
        title="title",
        messages=(
            VisibleMessage(role="user", text="old task", model_slug=None),
            VisibleMessage(
                role="assistant",
                text="Outcome: old\nResolved issue: old issue",
                model_slug="model-old",
            ),
            VisibleMessage(role="user", text="new task", model_slug=None),
            VisibleMessage(
                role="assistant",
                text="Outcome: new\nResolved issue: new issue",
                model_slug="model-new",
            ),
        ),
    )

    segment = extractor.select_completed_segment(conversation_value)

    assert segment is not None
    extracted = extractor.extract(segment, project_path=Path("/fixture"))
    assert len(extracted) == 1
    assert extracted[0].namespace.model_id == "model-new"
    assert extracted[0].resolved_open_issues == ("new issue",)


def test_extractor_never_merges_labeled_messages_from_different_models():
    extractor = ExplicitTaskExtractor(Redactor())
    conversation_value = NormalizedConversation(
        conversation_id="conv-models",
        title="title",
        messages=(
            VisibleMessage("assistant", "Decision: old model decision", "model-a"),
            VisibleMessage("assistant", "Outcome: new model result", "model-b"),
        ),
    )

    records = extractor.extract(conversation_value, project_path=Path("/fixture"))

    assert len(records) == 1
    assert records[0].namespace.model_id == "model-b"
    assert records[0].outcome == "new model result"
    assert records[0].decisions == ()


def test_incomplete_trailing_user_cannot_lend_project_to_older_assistant(tmp_path):
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    database, _project_a_record, adapter = _services(tmp_path, project_a)
    ProjectRepository(database).register(
        ProjectCandidate(canonical_path=project_b, display_name="project-b")
    )
    value = conversation(
        "conv-cross-project",
        user_text="Discuss a generic task",
        assistant_text="Outcome: OLD_TASK",
        model_slug="model-old",
    )
    value["mapping"]["a1"]["children"] = ["u2"]
    value["mapping"]["u2"] = {
        "id": "u2",
        "parent": "a1",
        "children": [],
        "message": {
            "author": {"role": "user"},
            "content": {"parts": [f"In {project_b} fix later.py"]},
            "metadata": {},
            "create_time": 3,
        },
    }
    archive = build_export(tmp_path / "cross-project.zip", {"conversations.json": [value]})

    report = adapter.import_zip(archive)

    assert report.imported_count == 0
    assert report.confirmation_count == 1
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from behavior_memories").fetchone()[0] == 0


def test_latest_completed_segment_binds_project_model_and_labels(tmp_path):
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"
    project_a.mkdir()
    project_b.mkdir()
    database, _project_a_record, adapter = _services(tmp_path, project_a)
    project_b_record = ProjectRepository(database).register(
        ProjectCandidate(canonical_path=project_b, display_name="project-b")
    )
    value = conversation(
        "conv-segments",
        user_text=f"In {project_a} fix a.py",
        assistant_text="Outcome: result-a",
        model_slug="model-a",
    )
    value["mapping"]["a1"]["children"] = ["u2"]
    value["mapping"]["u2"] = {
        "id": "u2",
        "parent": "a1",
        "children": ["a2"],
        "message": {
            "author": {"role": "user"},
            "content": {"parts": [f"In {project_b} fix b.py"]},
            "metadata": {},
            "create_time": 3,
        },
    }
    value["mapping"]["a2"] = {
        "id": "a2",
        "parent": "u2",
        "children": ["u3"],
        "message": {
            "author": {"role": "assistant"},
            "content": {"parts": ["Outcome: result-b"]},
            "metadata": {"model_slug": "model-b"},
            "create_time": 4,
        },
    }
    value["mapping"]["u3"] = {
        "id": "u3",
        "parent": "a2",
        "children": [],
        "message": {
            "author": {"role": "user"},
            "content": {"parts": [f"Later inspect {project_a}/c.py"]},
            "metadata": {},
            "create_time": 5,
        },
    }
    archive = build_export(tmp_path / "segments.zip", {"conversations.json": [value]})

    report = adapter.import_zip(archive)

    assert report.imported_count == 1
    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select project_id, model_id, normalized_content
            from behavior_memories
            """
        ).fetchall()
    assert [(row["project_id"], row["model_id"], row["normalized_content"]) for row in rows] == [
        (str(project_b_record.project_id), "model-b", "result-b")
    ]


@pytest.mark.parametrize(
    "members",
    [
        {"conversations-10001.json": []},
        {"conversations-1.json": [], "conversations-01.json": []},
    ],
)
def test_extra_or_duplicate_numeric_conversation_members_are_rejected(tmp_path, members):
    project = tmp_path / "demo-repo"
    project.mkdir()
    _database_value, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(tmp_path / "extra-members.zip", members)

    with pytest.raises(UnsafeArchiveError, match="conversation member"):
        adapter.import_zip(archive)


@pytest.mark.parametrize(
    "member_name",
    ["conversations-01.json", "conversations-00.json", "conversations-0.json"],
)
def test_lone_noncanonical_numeric_conversation_member_is_rejected(tmp_path, member_name):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(tmp_path / "noncanonical.zip", {member_name: []})

    with pytest.raises(UnsafeArchiveError, match="conversation member"):
        adapter.import_zip(archive)

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0


def test_unsafe_model_slug_becomes_unknown_without_secret_leak(tmp_path):
    marker = "SUPER_PRIVATE_MODEL_MARKER"
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "unsafe-model.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-model-secret",
                    user_text=f"In {project} fix model.py",
                    assistant_text="Outcome: fixed",
                    model_slug=f"password={marker}",
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert marker not in repr(report)
    with database.connect(readonly=True) as connection:
        dump = " ".join(
            str(tuple(row))
            for table in ("behavior_memories", "source_refs", "import_receipts")
            for row in connection.execute(f"select * from {table}").fetchall()
        )
        assert (
            connection.execute("select distinct model_id from behavior_memories").fetchone()[0]
            == "unknown"
        )
    assert marker not in dump


@pytest.mark.parametrize(
    "unsafe_slug",
    ["gpt/../../private/path", "gpt/private/model", "gpt..private"],
)
def test_path_like_model_slug_becomes_unknown_without_leak(tmp_path, unsafe_slug):
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    archive = build_export(
        tmp_path / "path-model.zip",
        {
            "conversations.json": [
                conversation(
                    "conv-path-model",
                    user_text=f"In {project} fix model.py",
                    assistant_text="Outcome: fixed",
                    model_slug=unsafe_slug,
                )
            ]
        },
    )

    report = adapter.import_zip(archive)

    assert unsafe_slug not in repr(report)
    with database.connect(readonly=True) as connection:
        rows = connection.execute("select model_id from behavior_memories").fetchall()
        dump = " ".join(
            str(tuple(row))
            for table in ("behavior_memories", "source_refs", "import_receipts")
            for row in connection.execute(f"select * from {table}").fetchall()
        )
    assert [row["model_id"] for row in rows] == ["unknown"]
    assert unsafe_slug not in dump


@pytest.mark.parametrize("hidden_mode", ["metadata", "recipient"])
def test_hidden_or_tool_directed_assistant_cannot_create_memory(tmp_path, hidden_mode):
    marker = "HIDDEN_ASSISTANT_MARKER"
    project = tmp_path / "demo-repo"
    project.mkdir()
    database, _project_record, adapter = _services(tmp_path, project)
    value = conversation(
        "conv-hidden",
        user_text=f"In {project} fix hidden.py",
        assistant_text=f"Outcome: {marker}",
    )
    message = value["mapping"]["a1"]["message"]
    if hidden_mode == "metadata":
        message["metadata"]["is_visually_hidden_from_conversation"] = True
    else:
        message["recipient"] = "python"
    archive = build_export(
        tmp_path / f"hidden-{hidden_mode}.zip",
        {"conversations.json": [value]},
    )

    report = adapter.import_zip(archive)

    assert report.imported_count == 0
    assert marker not in repr(report)
    with database.connect(readonly=True) as connection:
        dump = " ".join(
            str(tuple(row))
            for table in ("behavior_memories", "source_refs", "app_state")
            for row in connection.execute(f"select * from {table}").fetchall()
        )
    assert marker not in dump
