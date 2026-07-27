from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from project_memory_hub.domain import (
    CapturePayload,
    CaptureResult,
    Namespace,
    ProjectCandidate,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.retry_queue import RetryQueue
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository
from project_memory_hub.storage.proposals import (
    CorruptProposalRecord,
    InvalidProposalTransition,
    ProposalDraft,
    ProposalError,
    ProposalRepository,
    UnsafeProposalPatch,
)


def _retry_stack(
    tmp_path: Path,
) -> tuple[
    Database,
    Path,
    ProjectRepository,
    RetryQueue,
    CaptureService,
]:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    redactor = Redactor()
    queue = RetryQueue(database, projects, redactor)
    capture = CaptureService(database, projects, MemoryRepository(database), redactor)
    return database, project, projects, queue, capture


def _retry_payload(project: Path, source_record_id: str = "coverage-retry") -> CapturePayload:
    return CapturePayload(
        cwd=project,
        namespace=Namespace(source_agent="codex", model_id="provider/gpt-5"),
        source_record_id=source_record_id,
        objective="retry objective",
        outcome="retry outcome",
        decisions=["keep retries private"],
        changed_paths=["src/app.py"],
    )


def _seed_retry(
    database: Database,
    project: Path,
    queue: RetryQueue,
) -> tuple[str, dict[str, Any]]:
    retry_id = queue.enqueue(_retry_payload(project), "operational_failure")
    with database.connect(readonly=True) as connection:
        document = connection.execute(
            "select payload_json from retry_items where retry_id = ?",
            (str(retry_id),),
        ).fetchone()[0]
    return str(retry_id), json.loads(document)


def _store_retry_document(database: Database, retry_id: str, document: str) -> None:
    with database.transaction() as connection:
        connection.execute(
            "update retry_items set payload_json = ? where retry_id = ?",
            (document, retry_id),
        )


def _corrupt_retry_document(
    case: str,
    stored: dict[str, Any],
    project: Path,
) -> str:
    if case == "duplicate_json_key":
        return '{"duplicate":1,"duplicate":2}'
    if case == "non_object":
        return "[]"
    if case == "v1_boolean_version":
        stored["privacy_version"] = True
        stored.pop("resolved_open_issues")
    elif case == "v2_unknown_version":
        stored["privacy_version"] = 3
    elif case == "namespace_not_object":
        stored["namespace"] = []
    elif case == "source_agent_not_text":
        stored["namespace"]["source_agent"] = 7
    elif case == "source_agent_unknown":
        stored["namespace"]["source_agent"] = "unknown-agent"
    elif case == "text_not_string":
        stored["objective"] = 7
    elif case == "text_control_character":
        stored["objective"] = "unsafe\x00text"
    elif case == "list_not_array":
        stored["decisions"] = "not-an-array"
    elif case == "project_id_not_text":
        stored["project_id"] = 7
    elif case == "project_id_not_uuid":
        stored["project_id"] = "not-a-uuid"
    elif case == "project_id_not_canonical":
        stored["project_id"] = "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"
    elif case == "legacy_private_text":
        stored.pop("privacy_version")
        stored.pop("resolved_open_issues")
        stored["objective"] = "/Users/private/project/secret.py"
    elif case == "legacy_private_list_item":
        stored.pop("privacy_version")
        stored.pop("resolved_open_issues")
        stored["decisions"] = ["https://private.example.invalid/repository.git"]
    elif case == "legacy_path_escape":
        stored.pop("privacy_version")
        stored.pop("resolved_open_issues")
        stored["changed_paths"] = ["../outside/secret.py"]
    elif case == "legacy_mixed_unsafe_paths":
        stored.pop("privacy_version")
        stored.pop("resolved_open_issues")
        stored["changed_paths"] = [
            "",
            "../outside/secret.py",
            "~private/secret.py",
            r"\\server\share\secret.py",
            r"C:\private\secret.py",
            str(project),
            str(project / "src" / "app.py"),
            str(project.parent / "outside" / "secret.py"),
            "src/app.py",
        ]
    elif case == "v1_noncanonical_structure":
        stored["privacy_version"] = 1
        stored.pop("resolved_open_issues")
        stored["objective"] = "retry   objective"
    elif case == "v2_noncanonical_structure":
        stored["objective"] = "retry   objective"
    elif case == "v2_secret_injection":
        stored["objective"] = "Authorization: Bearer abcdefghijklmnop"
    else:  # pragma: no cover - the parametrization is intentionally closed
        raise AssertionError(case)
    return json.dumps(stored, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@pytest.mark.parametrize(
    "case",
    (
        "duplicate_json_key",
        "non_object",
        "v1_boolean_version",
        "v2_unknown_version",
        "namespace_not_object",
        "source_agent_not_text",
        "source_agent_unknown",
        "text_not_string",
        "text_control_character",
        "list_not_array",
        "project_id_not_text",
        "project_id_not_uuid",
        "project_id_not_canonical",
        "legacy_private_text",
        "legacy_private_list_item",
        "legacy_path_escape",
        "legacy_mixed_unsafe_paths",
        "v1_noncanonical_structure",
        "v2_noncanonical_structure",
        "v2_secret_injection",
    ),
)
def test_retry_drain_rejects_corrupt_or_privacy_unsafe_persisted_rows(
    tmp_path: Path,
    case: str,
) -> None:
    database, project, _projects, queue, capture = _retry_stack(tmp_path)
    retry_id, stored = _seed_retry(database, project, queue)
    _store_retry_document(
        database,
        retry_id,
        _corrupt_retry_document(case, stored, project),
    )

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (0, 1, 1)
    with database.connect(readonly=True) as connection:
        retry = connection.execute(
            "select attempts, last_attempt_at from retry_items where retry_id = ?",
            (retry_id,),
        ).fetchone()
        pending_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
    assert retry["attempts"] == 1
    assert retry["last_attempt_at"] is not None
    assert pending_count == 0


@pytest.mark.parametrize("project_state", ("disabled", "deleted"))
def test_retry_drain_keeps_item_when_registered_project_is_no_longer_active(
    tmp_path: Path,
    project_state: str,
) -> None:
    database, project, _projects, queue, capture = _retry_stack(tmp_path)
    retry_id, stored = _seed_retry(database, project, queue)
    with database.transaction() as connection:
        if project_state == "disabled":
            connection.execute(
                "update projects set enabled = 0 where project_id = ?",
                (stored["project_id"],),
            )
        else:
            connection.execute(
                "delete from projects where project_id = ?",
                (stored["project_id"],),
            )

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (0, 1, 1)
    with database.connect(readonly=True) as connection:
        retry = connection.execute(
            "select attempts from retry_items where retry_id = ?",
            (retry_id,),
        ).fetchone()
        assert retry["attempts"] == 1
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0


def test_retry_drain_rolls_back_when_capture_refuses_the_replayed_item(tmp_path: Path) -> None:
    database, project, _projects, queue, _capture = _retry_stack(tmp_path)
    retry_id, _stored = _seed_retry(database, project, queue)

    class RejectingCapture:
        @staticmethod
        def _capture_untrusted_on_connection(
            _connection: sqlite3.Connection,
            _payload: CapturePayload,
            _project_id: UUID,
        ) -> CaptureResult:
            return CaptureResult(status="rejected")

    report = queue.drain(RejectingCapture())  # type: ignore[arg-type]

    assert (report.completed_count, report.failed_count, report.remaining_count) == (0, 1, 1)
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select attempts from retry_items where retry_id = ?",
                (retry_id,),
            ).fetchone()[0]
            == 1
        )
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0


def test_retry_enqueue_rejects_missing_project_and_private_identifier(tmp_path: Path) -> None:
    database, project, projects, queue, _capture = _retry_stack(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(KeyError, match="project_not_found"):
        queue.enqueue(_retry_payload(outside), "operational_failure")
    with pytest.raises(ValueError, match="source_record_id"):
        queue.enqueue(
            _retry_payload(project, "Authorization: Bearer abcdefghijklmnop"),
            "operational_failure",
        )

    assert projects.find_by_cwd(project) is not None
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from retry_items").fetchone()[0] == 0


def _proposal_stack(tmp_path: Path) -> tuple[Database, ProposalRepository]:
    database = Database(tmp_path / "proposals.db")
    database.initialize()
    return database, ProposalRepository(database)


def _proposal_draft(
    signature: str,
    *,
    origin: str = "local_cli",
    patch: str | None = "safe patch",
) -> ProposalDraft:
    analyzer = origin == "analyzer"
    return ProposalDraft(
        signature=signature,
        title="Bounded retry",
        description="Keep state transitions fail closed.",
        risk="low",
        patch=patch,
        verification_argv=() if analyzer else ("uv", "run", "pytest", "-q"),
        target_version=None if analyzer else "0.2.0",
        origin=origin,
    )


def _seed_proposal_state(
    repository: ProposalRepository,
    state: str,
) -> Any:
    origin = "analyzer" if state in {"analyzer", "legacy"} else "local_cli"
    patch = None if origin == "analyzer" else "safe patch"
    record = repository.create(
        _proposal_draft(f"coverage-{state}-{uuid4().hex}", origin=origin, patch=patch)
    ).record
    if state == "legacy":
        return record
    if state == "analyzer" or state == "draft":
        return record
    if state == "rejected":
        return repository.reject(record.proposal_id, expected_status="draft")
    approved = repository.approve(record.proposal_id, actor="local-user")
    if state == "approved":
        return approved
    applying = repository.begin_apply(
        record.proposal_id,
        apply_attempt_id=uuid4(),
        repository_root=Path("/tmp/project-memory-hub"),
        original_branch="main",
        base_commit="a" * 40,
        proposal_branch=f"codex/memory-hub-proposal-{record.proposal_id.hex}",
    )
    if state == "applying":
        return applying
    if state == "failed":
        return repository.mark_failed(
            record.proposal_id,
            apply_attempt_id=applying.apply_attempt_id,
            failure_code="verification_failed",
            verification_summary="verification failed",
        )
    applied = repository.mark_applied(
        record.proposal_id,
        apply_attempt_id=applying.apply_attempt_id,
        applied_commit="b" * 40,
        verification_summary="verification passed",
    )
    if state == "applied":
        return applied
    if state == "rolled_back":
        return repository.mark_rolled_back(record.proposal_id)
    raise AssertionError(state)


def _proposal_invariant_corruption(case: str) -> tuple[str, dict[str, object]]:
    if case == "analyzer_execution_capability":
        return "analyzer", {"patch": "safe patch"}
    if case == "approval_pair_mismatch":
        return "draft", {"approval_actor": "local-user"}
    if case == "approval_time_before_creation":
        return "approved", {"approved_at": "1900-01-01T00:00:00Z"}
    if case == "legacy_approval_metadata":
        return "legacy", {"origin": "legacy", "approval_pair": True}
    if case == "applied_time_without_approval":
        return "draft", {"applied_time": True}
    if case == "rollback_time_without_apply":
        return "draft", {"rollback_time": True}
    if case == "draft_execution_residue":
        return "draft", {"failure_code": "verification_failed"}
    if case == "approved_execution_residue":
        return "approved", {"failure_code": "verification_failed"}
    if case == "rejected_execution_residue":
        return "rejected", {"failure_code": "verification_failed"}
    if case == "applying_without_approval":
        return "applying", {"approval_actor": None, "approved_at": None}
    if case == "failed_without_code":
        return "failed", {"failure_code": None}
    if case == "failed_with_applied_metadata":
        return "failed", {"applied_metadata": True}
    if case == "applied_without_commit":
        return "applied", {"applied_commit": None}
    if case == "applied_with_rollback_time":
        return "applied", {"rollback_from_applied": True}
    if case == "rolled_back_without_timestamp":
        return "rolled_back", {"rolled_back_at": None}
    raise AssertionError(case)


def _resolve_dynamic_updates(record: Any, updates: dict[str, object]) -> dict[str, object]:
    resolved = dict(updates)
    if resolved.pop("approval_pair", False):
        timestamp = record.created_at.isoformat().replace("+00:00", "Z")
        resolved.update(approval_actor="local-user", approved_at=timestamp)
    if resolved.pop("applied_time", False):
        resolved["applied_at"] = record.updated_at.isoformat().replace("+00:00", "Z")
    if resolved.pop("rollback_time", False):
        resolved["rolled_back_at"] = record.updated_at.isoformat().replace("+00:00", "Z")
    if resolved.pop("applied_metadata", False):
        resolved["applied_commit"] = "b" * 40
        resolved["applied_at"] = record.updated_at.isoformat().replace("+00:00", "Z")
    if resolved.pop("rollback_from_applied", False):
        resolved["rolled_back_at"] = record.applied_at.isoformat().replace("+00:00", "Z")
    return resolved


def _update_proposal_row(
    database: Database,
    proposal_id: UUID,
    updates: dict[str, object],
    *,
    ignore_checks: bool = False,
) -> None:
    assignments = ", ".join(f"{column} = ?" for column in updates)
    with database.transaction() as connection:
        if ignore_checks:
            connection.execute("pragma ignore_check_constraints = on")
        connection.execute(
            f"update improvement_proposals set {assignments} where proposal_id = ?",  # noqa: S608
            (*updates.values(), str(proposal_id)),
        )


@pytest.mark.parametrize(
    "case",
    (
        "analyzer_execution_capability",
        "approval_pair_mismatch",
        "approval_time_before_creation",
        "legacy_approval_metadata",
        "applied_time_without_approval",
        "rollback_time_without_apply",
        "draft_execution_residue",
        "approved_execution_residue",
        "rejected_execution_residue",
        "applying_without_approval",
        "failed_without_code",
        "failed_with_applied_metadata",
        "applied_without_commit",
        "applied_with_rollback_time",
        "rolled_back_without_timestamp",
    ),
)
def test_proposal_repository_hides_records_that_break_state_invariants(
    tmp_path: Path,
    case: str,
) -> None:
    database, repository = _proposal_stack(tmp_path)
    state, raw_updates = _proposal_invariant_corruption(case)
    record = _seed_proposal_state(repository, state)
    updates = _resolve_dynamic_updates(record, raw_updates)
    _update_proposal_row(database, record.proposal_id, updates)

    with pytest.raises(CorruptProposalRecord, match="record is invalid"):
        repository.get(record.proposal_id)
    assert repository.list_summaries() == ()


@pytest.mark.parametrize(
    ("column", "value", "ignore_checks", "expected_error"),
    (
        ("signature", "not a valid signature", False, CorruptProposalRecord),
        ("title", "not  normalized", False, CorruptProposalRecord),
        ("patch", "Authorization: Bearer abcdefghijklmnop", False, UnsafeProposalPatch),
        ("risk", "critical", True, CorruptProposalRecord),
        ("verification_argv_json", "{broken", False, CorruptProposalRecord),
        ("approval_status", "unknown", True, CorruptProposalRecord),
        ("origin", "unknown", True, CorruptProposalRecord),
        ("target_version", " 0.2.0", False, CorruptProposalRecord),
        ("updated_at", "2026-07-18T00:00:00", False, CorruptProposalRecord),
        ("apply_attempt_id", "not-a-uuid", False, CorruptProposalRecord),
        ("repository_root", "relative/path", False, CorruptProposalRecord),
        ("original_branch", "-unsafe", False, CorruptProposalRecord),
        ("base_commit", "not-a-commit", False, CorruptProposalRecord),
        ("failure_code", "UPPERCASE", False, CorruptProposalRecord),
    ),
)
def test_proposal_repository_fails_closed_on_malformed_database_fields(
    tmp_path: Path,
    column: str,
    value: object,
    ignore_checks: bool,
    expected_error: type[Exception],
) -> None:
    database, repository = _proposal_stack(tmp_path)
    record = _seed_proposal_state(repository, "draft")
    _update_proposal_row(
        database,
        record.proposal_id,
        {column: value},
        ignore_checks=ignore_checks,
    )

    with pytest.raises(expected_error):
        repository.get(record.proposal_id)
    assert repository.list_summaries() == ()


def test_proposal_preview_apply_and_rollback_enforce_exact_states_without_writes(
    tmp_path: Path,
) -> None:
    _database, repository = _proposal_stack(tmp_path)
    draft = _seed_proposal_state(repository, "draft")
    approved = _seed_proposal_state(repository, "approved")
    applying = _seed_proposal_state(repository, "applying")
    applied = _seed_proposal_state(repository, "applied")
    rolled_back = _seed_proposal_state(repository, "rolled_back")

    with pytest.raises(InvalidProposalTransition, match="draft -> applying"):
        repository.preview_apply(draft.proposal_id)
    assert repository.preview_apply(approved.proposal_id) == approved
    assert repository.preview_apply(applying.proposal_id) == applying
    assert repository.preview_rollback(applied.proposal_id) == applied
    with pytest.raises(InvalidProposalTransition, match="rolled_back -> rolled_back"):
        repository.preview_rollback(rolled_back.proposal_id)

    assert repository.get(approved.proposal_id).status == "approved"
    assert repository.get(applying.proposal_id).status == "applying"
    assert repository.get(applied.proposal_id).status == "applied"


def test_proposal_invalid_inputs_and_transitions_leave_persisted_state_unchanged(
    tmp_path: Path,
) -> None:
    _database, repository = _proposal_stack(tmp_path)
    draft = _seed_proposal_state(repository, "draft")
    rejected = _seed_proposal_state(repository, "rejected")
    applying = _seed_proposal_state(repository, "applying")

    with pytest.raises(KeyError):
        repository.get(uuid4())
    for limit in (True, 0, 1_001):
        with pytest.raises(ValueError, match="list limit"):
            repository.list_summaries(limit=limit)
    with pytest.raises(ValueError, match="expected proposal status"):
        repository.reject(draft.proposal_id, expected_status="invalid")  # type: ignore[arg-type]
    with pytest.raises(InvalidProposalTransition, match="rejected -> rejected"):
        repository.reject(rejected.proposal_id, expected_status="draft")
    with pytest.raises(InvalidProposalTransition, match="draft -> rejected"):
        repository.reject(draft.proposal_id, expected_status="approved")
    with pytest.raises(InvalidProposalTransition, match="attempt mismatch"):
        repository.mark_applied(
            applying.proposal_id,
            apply_attempt_id=uuid4(),
            applied_commit="b" * 40,
            verification_summary="verification passed",
        )
    with pytest.raises(ValueError, match="failure_code"):
        repository.mark_failed(
            applying.proposal_id,
            apply_attempt_id=applying.apply_attempt_id,
            failure_code="INVALID",
            verification_summary="verification failed",
        )
    with pytest.raises(InvalidProposalTransition, match="attempt mismatch"):
        repository.mark_failed(
            applying.proposal_id,
            apply_attempt_id=uuid4(),
            failure_code="verification_failed",
            verification_summary="verification failed",
        )

    assert repository.get(draft.proposal_id).status == "draft"
    assert repository.get(rejected.proposal_id).status == "rejected"
    assert repository.get(applying.proposal_id).status == "applying"


def test_proposal_creation_rejects_capability_conflicts_before_writing(tmp_path: Path) -> None:
    _database, repository = _proposal_stack(tmp_path)
    repository.create(_proposal_draft("active-origin-conflict"))

    analyzer_conflict = _proposal_draft(
        "active-origin-conflict",
        origin="analyzer",
        patch=None,
    )
    with pytest.raises(ProposalError, match="origin conflict"):
        repository.create(analyzer_conflict)

    malformed = _proposal_draft("malformed-capability").model_copy(update={"verification_argv": ()})
    with pytest.raises(ProposalError, match="verification command"):
        repository.create(malformed)

    assert len(repository.list_summaries()) == 1
