from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from project_memory_hub.storage.database import Database
from project_memory_hub.storage.proposals import (
    ApplyResult,
    CorruptProposalRecord,
    InvalidProposalOrigin,
    InvalidProposalTransition,
    ProposalDraft,
    ProposalError,
    ProposalRepository,
    UnsafeProposalPatch,
)


def _repository(tmp_path: Path) -> ProposalRepository:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    return ProposalRepository(database)


def _draft(
    signature: str = "local-cli-safe-signature",
    *,
    origin: str = "local_cli",
    patch: str | None = "safe [REDACTED:api_key]",
) -> ProposalDraft:
    analyzer = origin == "analyzer"
    return ProposalDraft(
        signature=signature,
        title="  Improve   retry handling  ",
        description="Keep failures bounded and observable.",
        risk="low",
        patch=patch,
        verification_argv=() if analyzer else ("uv", "run", "pytest", "-q"),
        target_version=None if analyzer else "0.2.0",
        origin=origin,
    )


def test_proposal_models_are_frozen_and_creation_normalizes_human_metadata(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    draft = _draft(patch=None).model_copy(
        update={
            "title": "  Retry\n\tpassword=hunter-two   safely  ",
            "description": "  One\n two  ",
        }
    )

    result = repository.create(draft)

    assert result.inserted is True
    assert result.duplicate is False
    assert result.record.title == "Retry password=[REDACTED:password] safely"
    assert result.record.description == "One two"
    with pytest.raises(ValidationError):
        result.record.title = "mutable"  # type: ignore[misc]

    apply_result = ApplyResult(
        proposal_id=result.record.proposal_id,
        repository_root=Path("/tmp/project-memory-hub"),
        original_branch="main",
        base_commit="a" * 40,
        proposal_branch=f"codex/memory-hub-proposal-{result.record.proposal_id.hex}",
        applied_commit="b" * 40,
        verification_summary="checks passed",
    )
    with pytest.raises(ValidationError):
        apply_result.applied_commit = "c" * 40  # type: ignore[misc]


def test_preview_validation_reuses_state_rules_without_writing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    normalized, duplicate = repository.preview_create(_draft())

    assert normalized.title == "Improve retry handling"
    assert duplicate is None
    assert repository.list_summaries() == ()

    capable = repository.create(_draft("preview-capable")).record
    analyzer = repository.create(_draft("preview-analyzer", origin="analyzer", patch=None)).record
    assert repository.preview_approve(capable.proposal_id) == capable
    assert repository.preview_reject(capable.proposal_id) == capable
    _prepared, active_duplicate = repository.preview_create(_draft("preview-capable"))
    assert active_duplicate == capable
    with pytest.raises(ProposalError, match="origin conflict"):
        repository.preview_create(_draft("preview-capable", origin="analyzer", patch=None))
    with pytest.raises(InvalidProposalOrigin):
        repository.preview_approve(analyzer.proposal_id)

    assert repository.get(capable.proposal_id).status == "draft"
    assert repository.get(analyzer.proposal_id).status == "draft"
    rejected = repository.reject(capable.proposal_id, expected_status="draft")
    with pytest.raises(InvalidProposalTransition):
        repository.preview_reject(rejected.proposal_id)


def test_secret_patch_is_rejected_before_active_signature_dedupe(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    repository.create(_draft(patch="safe patch"))

    with pytest.raises(UnsafeProposalPatch, match="secret"):
        repository.create(_draft(patch="Authorization: Bearer abcdefghijklmnop"))

    assert len(repository.list_summaries()) == 1


@pytest.mark.parametrize(
    "controlled_title",
    (
        "safe\x1b]0;terminal-title\x07text",
        "safe\x9b31mtext",
        "safe\u202etext",
    ),
)
def test_display_metadata_rejects_terminal_and_bidi_control_characters(
    tmp_path: Path,
    controlled_title: str,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(ProposalError, match="title"):
        repository.create(_draft().model_copy(update={"title": controlled_title}))

    assert repository.list_summaries() == ()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("title", "A" * 201),
        ("description", "B" * 2001),
    ),
)
def test_creation_rejects_human_metadata_that_would_be_silently_truncated(
    tmp_path: Path,
    field_name: str,
    value: str,
) -> None:
    repository = _repository(tmp_path)
    draft = _draft().model_copy(update={field_name: value})

    with pytest.raises(ProposalError, match=field_name):
        repository.preview_create(draft)
    with pytest.raises(ProposalError, match=field_name):
        repository.create(draft)

    assert repository.list_summaries() == ()


def test_existing_stable_redaction_marker_is_allowed_when_patch_is_unchanged(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    result = repository.create(_draft())

    assert result.record.patch == "safe [REDACTED:api_key]"


def test_analyzer_draft_cannot_contain_a_patch_and_legacy_cannot_be_created(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)

    with pytest.raises(InvalidProposalOrigin, match="analyzer"):
        repository.create(_draft(origin="analyzer", patch="diff --git a/x b/x"))
    with pytest.raises(ValidationError):
        _draft(origin="legacy", patch=None)


def test_active_signature_dedupe_is_explicit_and_inactive_signature_can_recur(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    first = repository.create(_draft())
    duplicate = repository.create(_draft())

    assert first.inserted is True
    assert duplicate.inserted is False
    assert duplicate.duplicate is True
    assert duplicate.record.proposal_id == first.record.proposal_id

    repository.reject(first.record.proposal_id, expected_status="draft")
    replacement = repository.create(_draft())
    assert replacement.inserted is True
    assert replacement.record.proposal_id != first.record.proposal_id


def test_exact_valid_state_machine_records_execution_metadata(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft()).record

    approved = repository.approve(proposal.proposal_id, actor="local-user")
    attempt_id = uuid4()
    applying = repository.begin_apply(
        proposal.proposal_id,
        apply_attempt_id=attempt_id,
        repository_root=Path("/tmp/project-memory-hub"),
        original_branch="codex/project-memory-hub",
        base_commit="a" * 40,
        proposal_branch=f"codex/memory-hub-proposal-{proposal.proposal_id.hex}",
    )
    applied = repository.mark_applied(
        proposal.proposal_id,
        apply_attempt_id=attempt_id,
        applied_commit="b" * 40,
        verification_summary="42 checks passed",
    )
    rolled_back = repository.mark_rolled_back(proposal.proposal_id)

    assert approved.status == "approved"
    assert approved.approval_actor == "local-user"
    assert approved.approved_at is not None
    assert applying.status == "applying"
    assert applying.apply_attempt_id == attempt_id
    assert applying.repository_root == Path("/tmp/project-memory-hub")
    assert applied.status == "applied"
    assert applied.applied_commit == "b" * 40
    assert applied.applied_at is not None
    assert applied.verification_summary == "42 checks passed"
    assert rolled_back.status == "rolled_back"
    assert rolled_back.rolled_back_at is not None


def test_rejection_and_failure_are_only_allowed_from_documented_states(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    draft = repository.create(_draft("draft-reject")).record
    approved = repository.approve(
        repository.create(_draft("approved-reject")).record.proposal_id,
        actor="local-user",
    )
    applying_source = repository.approve(
        repository.create(_draft("applying-fail")).record.proposal_id,
        actor="local-user",
    )
    attempt_id = uuid4()
    applying = repository.begin_apply(
        applying_source.proposal_id,
        apply_attempt_id=attempt_id,
        repository_root=Path("/tmp/project-memory-hub"),
        original_branch="main",
        base_commit="a" * 40,
        proposal_branch=f"codex/memory-hub-proposal-{applying_source.proposal_id.hex}",
    )

    assert repository.reject(draft.proposal_id, expected_status="draft").status == "rejected"
    assert repository.reject(approved.proposal_id, expected_status="approved").status == "rejected"
    assert (
        repository.mark_failed(
            applying.proposal_id,
            apply_attempt_id=attempt_id,
            failure_code="verification_failed",
            verification_summary="verification failed",
        ).status
        == "failed"
    )


def test_invalid_transitions_leave_every_field_unchanged(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft()).record
    before = repository.get(proposal.proposal_id)

    with pytest.raises(InvalidProposalTransition, match="draft.*applied"):
        repository.mark_applied(
            proposal.proposal_id,
            apply_attempt_id=uuid4(),
            applied_commit="b" * 40,
            verification_summary="should not persist",
        )

    assert repository.get(proposal.proposal_id) == before


def test_approval_is_immutable_and_a_stale_second_approval_loses(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft()).record
    first = repository.approve(proposal.proposal_id, actor="first-local-user")

    with pytest.raises(InvalidProposalTransition):
        repository.approve(proposal.proposal_id, actor="second-local-user")

    after = repository.get(proposal.proposal_id)
    assert after.approval_actor == first.approval_actor == "first-local-user"
    assert after.approved_at == first.approved_at


def test_secret_approval_actor_is_rejected_without_persistence(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft()).record
    secret = "Bearer abcdefghijklmnop"

    with pytest.raises(ProposalError, match="approval_actor") as failure:
        repository.approve(proposal.proposal_id, actor=secret)

    assert secret not in str(failure.value)
    stored = repository.get(proposal.proposal_id)
    assert stored.status == "draft"
    assert stored.approval_actor is None
    for database_file in tmp_path.glob("memory.db*"):
        assert secret.encode() not in database_file.read_bytes()


def test_two_thread_approval_compare_and_swap_has_exactly_one_winner(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft()).record
    barrier = Barrier(2)

    def approve(actor: str) -> str:
        barrier.wait(timeout=5)
        try:
            repository.approve(proposal.proposal_id, actor=actor)
        except InvalidProposalTransition:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(approve, ("actor-one", "actor-two")))

    assert sorted(outcomes) == ["lost", "won"]
    stored = repository.get(proposal.proposal_id)
    assert stored.status == "approved"
    assert stored.approval_actor in {"actor-one", "actor-two"}


def test_two_thread_active_signature_creation_returns_one_winner(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repositories = (ProposalRepository(database), ProposalRepository(database))
    barrier = Barrier(2)

    def create(repository: ProposalRepository):
        barrier.wait(timeout=5)
        return repository.create(_draft("concurrent-signature"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = tuple(executor.map(create, repositories))

    assert sorted(result.inserted for result in results) == [False, True]
    assert sorted(result.duplicate for result in results) == [False, True]
    assert len({result.record.proposal_id for result in results}) == 1
    with database.connect(readonly=True) as connection:
        count = connection.execute(
            "select count(*) from improvement_proposals where signature = ?",
            ("concurrent-signature",),
        ).fetchone()[0]
    assert count == 1


def test_active_signature_conflict_is_classified_before_lock_release(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    winner = ProposalRepository(database).create(_draft("locked-dedupe")).record

    class RaceDatabase:
        def transaction(self):
            @contextmanager
            def transaction_context():
                try:
                    with database.transaction() as connection:
                        yield connection
                except sqlite3.IntegrityError:
                    # This simulates the winner leaving the partial active index
                    # in the instant after a loser releases BEGIN IMMEDIATE.
                    with database.transaction() as connection:
                        connection.execute(
                            "update improvement_proposals "
                            "set approval_status = 'rejected' "
                            "where proposal_id = ?",
                            (str(winner.proposal_id),),
                        )
                    raise

            return transaction_context()

        def connect(self, *args, **kwargs):
            return database.connect(*args, **kwargs)

    result = ProposalRepository(RaceDatabase()).create(_draft("locked-dedupe"))  # type: ignore[arg-type]

    assert result.duplicate is True
    assert result.record.proposal_id == winner.proposal_id
    assert ProposalRepository(database).get(winner.proposal_id).status == "draft"


def test_concurrent_approve_and_draft_reject_have_one_winner(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    creator = ProposalRepository(database)
    proposal = creator.create(_draft("approve-reject-race")).record
    repositories = (ProposalRepository(database), ProposalRepository(database))
    barrier = Barrier(2)

    def approve() -> str:
        barrier.wait(timeout=5)
        try:
            repositories[0].approve(proposal.proposal_id, actor="local-user")
        except InvalidProposalTransition:
            return "lost"
        return "won"

    def reject() -> str:
        barrier.wait(timeout=5)
        try:
            repositories[1].reject(proposal.proposal_id, expected_status="draft")
        except InvalidProposalTransition:
            return "lost"
        return "won"

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve_future = executor.submit(approve)
        reject_future = executor.submit(reject)
        outcomes = (approve_future.result(), reject_future.result())

    assert sorted(outcomes) == ["lost", "won"]
    assert creator.get(proposal.proposal_id).status in {"approved", "rejected"}


def test_stored_row_is_revalidated_before_approval(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft(patch="safe patch")).record
    database = Database(tmp_path / "memory.db")
    with database.transaction() as connection:
        connection.execute(
            "update improvement_proposals set patch = ? where proposal_id = ?",
            (
                "Authorization: Bearer abcdefghijklmnop",
                str(proposal.proposal_id),
            ),
        )

    with pytest.raises(UnsafeProposalPatch):
        repository.approve(proposal.proposal_id, actor="local-user")

    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select approval_status, approval_actor, approved_at "
            "from improvement_proposals where proposal_id = ?",
            (str(proposal.proposal_id),),
        ).fetchone()
    assert tuple(row) == ("draft", None, None)


def test_stored_patch_without_verification_argv_cannot_be_approved(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft(patch="safe patch")).record
    database = Database(tmp_path / "memory.db")
    with database.transaction() as connection:
        connection.execute(
            "update improvement_proposals set verification_argv_json = '[]' where proposal_id = ?",
            (str(proposal.proposal_id),),
        )

    with pytest.raises(CorruptProposalRecord):
        repository.approve(proposal.proposal_id, actor="local-user")

    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select approval_status, approval_actor, approved_at "
            "from improvement_proposals where proposal_id = ?",
            (str(proposal.proposal_id),),
        ).fetchone()
    assert tuple(row) == ("draft", None, None)


def test_hostile_persisted_title_is_hidden_and_cannot_transition(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft()).record
    database = Database(tmp_path / "memory.db")
    secret = "Bearer abcdefghijklmnop"
    with database.transaction() as connection:
        connection.execute(
            "update improvement_proposals set title = ? where proposal_id = ?",
            (secret, str(proposal.proposal_id)),
        )

    assert repository.list_summaries() == ()
    with pytest.raises(CorruptProposalRecord) as read_failure:
        repository.get(proposal.proposal_id)
    with pytest.raises(CorruptProposalRecord) as approval_failure:
        repository.approve(proposal.proposal_id, actor="local-user")
    assert secret not in str(read_failure.value)
    assert secret not in str(approval_failure.value)
    with database.connect(readonly=True) as connection:
        status = connection.execute(
            "select approval_status from improvement_proposals where proposal_id = ?",
            (str(proposal.proposal_id),),
        ).fetchone()[0]
    assert status == "draft"


def test_partial_execution_metadata_fails_closed_before_begin_apply(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft()).record
    repository.approve(proposal.proposal_id, actor="local-user")
    database = Database(tmp_path / "memory.db")
    hostile_attempt = uuid4()
    with database.transaction() as connection:
        connection.execute(
            "update improvement_proposals set apply_attempt_id = ? where proposal_id = ?",
            (str(hostile_attempt), str(proposal.proposal_id)),
        )

    with pytest.raises(CorruptProposalRecord):
        repository.begin_apply(
            proposal.proposal_id,
            apply_attempt_id=uuid4(),
            repository_root=Path("/tmp/project-memory-hub"),
            original_branch="main",
            base_commit="a" * 40,
            proposal_branch=f"codex/memory-hub-proposal-{proposal.proposal_id.hex}",
        )

    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select approval_status, apply_attempt_id, repository_root "
            "from improvement_proposals where proposal_id = ?",
            (str(proposal.proposal_id),),
        ).fetchone()
    assert tuple(row) == ("approved", str(hostile_attempt), None)


def test_patchless_proposal_cannot_enter_applying(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(
        _draft(patch=None).model_copy(update={"verification_argv": ()})
    ).record
    repository.approve(proposal.proposal_id, actor="local-user")

    with pytest.raises(InvalidProposalTransition, match="no executable patch"):
        repository.begin_apply(
            proposal.proposal_id,
            apply_attempt_id=uuid4(),
            repository_root=Path("/tmp/project-memory-hub"),
            original_branch="main",
            base_commit="a" * 40,
            proposal_branch=f"codex/memory-hub-proposal-{proposal.proposal_id.hex}",
        )

    assert repository.get(proposal.proposal_id).status == "approved"


def test_hostile_analyzer_execution_state_cannot_finish_or_roll_back(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    proposal = repository.create(_draft(origin="analyzer", patch=None)).record
    database = Database(tmp_path / "memory.db")
    attempt = uuid4()
    timestamp = proposal.created_at.isoformat().replace("+00:00", "Z")
    with database.transaction() as connection:
        connection.execute(
            """
            update improvement_proposals
            set approval_status = 'applying', approval_actor = 'hostile',
                approved_at = ?, updated_at = ?, apply_attempt_id = ?,
                repository_root = '/tmp/project-memory-hub',
                original_branch = 'main', base_commit = ?,
                proposal_branch = ?
            where proposal_id = ?
            """,
            (
                timestamp,
                timestamp,
                str(attempt),
                "a" * 40,
                f"codex/memory-hub-proposal-{proposal.proposal_id.hex}",
                str(proposal.proposal_id),
            ),
        )

    with pytest.raises(CorruptProposalRecord):
        repository.mark_applied(
            proposal.proposal_id,
            apply_attempt_id=attempt,
            applied_commit="b" * 40,
            verification_summary="checks passed",
        )
    with database.connect(readonly=True) as connection:
        status = connection.execute(
            "select approval_status from improvement_proposals where proposal_id = ?",
            (str(proposal.proposal_id),),
        ).fetchone()[0]
    assert status == "applying"


def test_persisted_time_inversion_and_clock_rollback_fail_closed(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    created = datetime(2026, 7, 15, tzinfo=timezone.utc)
    repository = ProposalRepository(database, now=lambda: created)
    proposal = repository.create(_draft()).record
    past_repository = ProposalRepository(database, now=lambda: created - timedelta(seconds=1))

    with pytest.raises(InvalidProposalTransition, match="clock moved backward"):
        past_repository.preview_approve(proposal.proposal_id)
    with pytest.raises(InvalidProposalTransition, match="clock moved backward"):
        past_repository.approve(proposal.proposal_id, actor="local-user")
    assert repository.get(proposal.proposal_id).status == "draft"

    with database.transaction() as connection:
        connection.execute(
            "update improvement_proposals set created_at = ?, updated_at = ? where proposal_id = ?",
            (
                "2099-01-01T00:00:00Z",
                "1900-01-01T00:00:00Z",
                str(proposal.proposal_id),
            ),
        )
    with pytest.raises(CorruptProposalRecord):
        repository.get(proposal.proposal_id)


def test_analyzer_and_legacy_rows_have_no_approval_or_apply_capability(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    analyzer = repository.create(_draft(origin="analyzer", patch=None)).record
    with pytest.raises(InvalidProposalOrigin):
        repository.approve(analyzer.proposal_id, actor="local-user")

    legacy_id = UUID("00000000-0000-0000-0000-000000000099")
    database = Database(tmp_path / "memory.db")
    with database.transaction() as connection:
        connection.execute(
            """
            insert into improvement_proposals(
                proposal_id, signature, title, description, patch, risk,
                verification_argv_json, verification_summary, approval_status,
                target_version, rollback_ref, created_at, approved_at,
                origin, updated_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(legacy_id),
                "legacy-active",
                "Legacy",
                "Migrated row",
                None,
                "low",
                "[]",
                "",
                "draft",
                None,
                None,
                "2026-07-14T00:00:00Z",
                None,
                "legacy",
                "2026-07-14T00:00:00Z",
            ),
        )
    with pytest.raises(InvalidProposalOrigin):
        repository.approve(legacy_id, actor="local-user")


def test_list_summaries_never_exposes_patch(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.create(_draft(patch="PRIVATE PATCH CONTENT"))

    summaries = repository.list_summaries()

    assert len(summaries) == 1
    assert not hasattr(summaries[0], "patch")
    assert "PRIVATE PATCH CONTENT" not in repr(summaries)
    record = repository.get(summaries[0].proposal_id)
    assert "PRIVATE PATCH CONTENT" not in repr(record)
    assert "patch" not in record.model_dump()
