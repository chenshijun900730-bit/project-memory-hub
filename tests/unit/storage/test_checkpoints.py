import sqlite3

import pytest

from project_memory_hub.domain import SourceAgent
from project_memory_hub.storage import checkpoints as checkpoints_module
from project_memory_hub.storage import database as database_module
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database


def test_import_receipt_identity_includes_source_agent(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_hash = "a" * 64

    repository.commit_import_receipt(
        source_hash,
        "shared-record",
        SourceAgent.CODEX,
    )
    repository.commit_import_receipt(
        source_hash,
        "shared-record",
        SourceAgent.CHATGPT,
    )

    assert repository.receipt_exists(source_hash, "shared-record", SourceAgent.CODEX)
    assert repository.receipt_exists(source_hash, "shared-record", SourceAgent.CHATGPT)
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                """
            select count(*) from import_receipts
            where source_hash = ? and source_record_id = ?
            """,
                (source_hash, "shared-record"),
            ).fetchone()[0]
            == 2
        )


def test_import_receipt_transaction_guard_rolls_back_a_late_identity_change(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    guard_calls = 0

    def guard(_connection):
        nonlocal guard_calls
        guard_calls += 1
        if guard_calls == 2:
            raise RuntimeError("project identity changed")

    with pytest.raises(RuntimeError, match="identity changed"):
        repository.commit_import_receipt(
            "b" * 64,
            "guarded-record",
            SourceAgent.CHATGPT,
            confirmation={"status": "confirmation_required"},
            transaction_guard=guard,
        )

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from import_receipts").fetchone()[0] == 0
        assert (
            connection.execute(
                "select count(*) from app_state where name like 'chatgpt_confirmation:%'"
            ).fetchone()[0]
            == 0
        )


def test_prior_codex_receipt_proof_is_typed_codex_only_and_connection_bound(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_record_id = "session:committed-record"
    repository.commit_import_receipt(
        "c" * 64,
        source_record_id,
        SourceAgent.CODEX,
    )
    repository.commit_import_receipt(
        "d" * 64,
        "conversation:chatgpt-only",
        SourceAgent.CHATGPT,
    )

    with database.connect(readonly=True) as connection:
        with pytest.raises(ValueError, match="active transaction"):
            repository.prior_codex_receipt_proof_on_connection(
                connection,
                source_record_id,
            )

    with database.transaction() as connection:
        proof = repository.prior_codex_receipt_proof_on_connection(
            connection,
            source_record_id,
        )
        assert proof is not None
        assert not isinstance(proof, bool)
        assert proof.source_agent is SourceAgent.CODEX
        assert proof.source_record_id == source_record_id
        assert proof.matches(connection, SourceAgent.CODEX, source_record_id)
        assert not proof.matches(connection, SourceAgent.CHATGPT, source_record_id)
        assert (
            repository.prior_codex_receipt_proof_on_connection(
                connection,
                "conversation:chatgpt-only",
            )
            is None
        )

    with database.transaction() as other_connection:
        assert not proof.matches(
            other_connection,
            SourceAgent.CODEX,
            source_record_id,
        )


def test_prior_codex_receipt_proof_rejects_receipt_inserted_in_current_transaction(
    tmp_path,
):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_record_id = "session:uncommitted-receipt"

    with database.transaction() as connection:
        repository.commit_import_receipt_on_connection(
            connection,
            "e" * 64,
            source_record_id,
            SourceAgent.CODEX,
        )
        with pytest.raises(ValueError, match="prior committed"):
            repository.prior_codex_receipt_proof_on_connection(
                connection,
                source_record_id,
            )


def test_prior_codex_receipt_proof_revalidates_exact_receipt_on_live_connection(
    tmp_path,
):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_hash = "f" * 64
    source_record_id = "session:live-revalidation"
    repository.commit_import_receipt(
        source_hash,
        source_record_id,
        SourceAgent.CODEX,
    )

    with database.transaction() as connection:
        proof = repository.prior_codex_receipt_proof_on_connection(
            connection,
            source_record_id,
        )
        assert proof is not None
        assert proof.matches(connection, SourceAgent.CODEX, source_record_id)
        connection.execute(
            """
            insert into app_state(name, value_json, updated_at)
            values ('proof-unrelated-write', '{}', '2026-07-16T12:00:00Z')
            """
        )
        assert proof.matches(connection, SourceAgent.CODEX, source_record_id)
        imported_at = connection.execute(
            """
            select imported_at from import_receipts
            where source_hash = ? and source_record_id = ? and source_agent = ?
            """,
            (source_hash, source_record_id, SourceAgent.CODEX.value),
        ).fetchone()[0]
        connection.execute(
            """
            delete from import_receipts
            where source_hash = ? and source_record_id = ? and source_agent = ?
            """,
            (source_hash, source_record_id, SourceAgent.CODEX.value),
        )

        assert not proof.matches(
            connection,
            SourceAgent.CODEX,
            source_record_id,
        )
        connection.execute(
            """
            insert into import_receipts(
                source_hash, source_record_id, source_agent, imported_at
            ) values (?, ?, ?, ?)
            """,
            (
                source_hash,
                source_record_id,
                SourceAgent.CODEX.value,
                imported_at,
            ),
        )
        assert not proof.matches(
            connection,
            SourceAgent.CODEX,
            source_record_id,
        )


def test_prior_codex_receipt_proof_tracks_cached_statement_mutations(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_hash = "9" * 64
    source_record_id = "session:cached-statement-mutation"
    repository.commit_import_receipt(
        source_hash,
        source_record_id,
        SourceAgent.CODEX,
    )
    delete_sql = """
        delete from import_receipts
        where source_hash = ? and source_record_id = ? and source_agent = ?
    """
    insert_sql = """
        insert or ignore into import_receipts(
            source_hash, source_record_id, source_agent, imported_at
        ) values (?, ?, ?, ?)
    """

    with database.transaction() as connection:
        imported_at = connection.execute(
            """
            select imported_at from import_receipts
            where source_hash = ? and source_record_id = ? and source_agent = ?
            """,
            (source_hash, source_record_id, SourceAgent.CODEX.value),
        ).fetchone()[0]
        connection.execute(
            delete_sql,
            ("0" * 64, "session:missing", SourceAgent.CODEX.value),
        )
        connection.execute(
            insert_sql,
            (
                source_hash,
                source_record_id,
                SourceAgent.CODEX.value,
                imported_at,
            ),
        )
        proof = repository.prior_codex_receipt_proof_on_connection(
            connection,
            source_record_id,
        )
        assert proof is not None

        connection.execute(
            delete_sql,
            (source_hash, source_record_id, SourceAgent.CODEX.value),
        )
        connection.execute(
            insert_sql,
            (
                source_hash,
                source_record_id,
                SourceAgent.CODEX.value,
                imported_at,
            ),
        )

        assert not proof.matches(
            connection,
            SourceAgent.CODEX,
            source_record_id,
        )


def test_prior_codex_receipt_proof_ignores_preexisting_fixed_noop_triggers(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_hash = "8" * 64
    source_record_id = "session:preexisting-noop-triggers"
    repository.commit_import_receipt(
        source_hash,
        source_record_id,
        SourceAgent.CODEX,
    )

    with database.connect() as connection:
        for operation in ("insert", "update", "delete"):
            connection.execute(
                f"""
                create temp trigger _pmh_import_receipts_{operation}
                after {operation} on main.import_receipts
                begin
                    select 0;
                end
                """
            )
        with database_module._managed_transaction(connection):
            imported_at = connection.execute(
                """
                select imported_at from import_receipts
                where source_hash = ? and source_record_id = ? and source_agent = ?
                """,
                (source_hash, source_record_id, SourceAgent.CODEX.value),
            ).fetchone()[0]
            proof = repository.prior_codex_receipt_proof_on_connection(
                connection,
                source_record_id,
            )
            assert proof is not None
            connection.execute(
                """
                delete from import_receipts
                where source_hash = ? and source_record_id = ? and source_agent = ?
                """,
                (source_hash, source_record_id, SourceAgent.CODEX.value),
            )
            connection.execute(
                """
                insert into import_receipts(
                    source_hash, source_record_id, source_agent, imported_at
                ) values (?, ?, ?, ?)
                """,
                (
                    source_hash,
                    source_record_id,
                    SourceAgent.CODEX.value,
                    imported_at,
                ),
            )

            assert not proof.matches(
                connection,
                SourceAgent.CODEX,
                source_record_id,
            )


def test_prior_codex_receipt_proof_requires_a_database_managed_transaction(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_record_id = "session:managed-transaction-only"
    repository.commit_import_receipt(
        "a" * 64,
        source_record_id,
        SourceAgent.CODEX,
    )

    with database.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(ValueError, match="managed transaction"):
            repository.prior_codex_receipt_proof_on_connection(
                connection,
                source_record_id,
            )
        connection.rollback()


def test_managed_transaction_tracker_install_failure_rolls_back_before_token_exposure(
    tmp_path,
    monkeypatch,
):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    tracker_id = "7" * 32

    class FixedUuid:
        hex = tracker_id

    monkeypatch.setattr(database_module, "uuid4", lambda: FixedUuid(), raising=False)
    statements: list[str] = []

    with database.connect() as connection:
        connection.execute(
            f"""
            create temp trigger _pmh_import_receipts_update_{tracker_id}
            after update on main.import_receipts
            begin
                select 0;
            end
            """
        )
        connection.set_trace_callback(statements.append)
        with pytest.raises(sqlite3.OperationalError):
            with database_module._managed_transaction(connection):
                pytest.fail("transaction with incomplete receipt tracking became visible")
        connection.set_trace_callback(None)

        assert statements[0].strip().upper() == "BEGIN IMMEDIATE"
        assert not connection.in_transaction
        assert database_module._active_transaction_token(connection) is None
        assert (
            connection.execute(
                """
                select group_concat(name, ',') from sqlite_temp_master
                where type = 'trigger' and name like '_pmh_import_receipts_%'
                """
            ).fetchone()[0]
            == f"_pmh_import_receipts_update_{tracker_id}"
        )
        connection.execute(f'drop trigger temp."_pmh_import_receipts_update_{tracker_id}"')
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            insert into import_receipts(
                source_hash, source_record_id, source_agent, imported_at
            ) values (?, ?, ?, ?)
            """,
            (
                "6" * 64,
                "session:post-install-failure",
                SourceAgent.CODEX.value,
                "2026-07-16T12:00:00Z",
            ),
        )
        connection.commit()


def test_prior_codex_receipt_proof_is_bound_to_same_connection_transaction(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_record_id = "session:same-connection-new-transaction"
    repository.commit_import_receipt(
        "b" * 64,
        source_record_id,
        SourceAgent.CODEX,
    )

    with database.connect() as connection:
        with database_module._managed_transaction(connection):
            proof = repository.prior_codex_receipt_proof_on_connection(
                connection,
                source_record_id,
            )
            assert proof is not None
            assert proof.matches(connection, SourceAgent.CODEX, source_record_id)

        with database_module._managed_transaction(connection):
            assert not proof.matches(
                connection,
                SourceAgent.CODEX,
                source_record_id,
            )


def test_prior_codex_receipt_proofs_batch_uses_one_reverse_lookup(tmp_path):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    requested_ids = (
        "session:batch-one",
        "session:batch-missing",
        "session:batch-two",
    )
    for source_hash, source_record_id in (
        ("c" * 64, requested_ids[0]),
        ("d" * 64, requested_ids[2]),
        ("e" * 64, "session:unrelated"),
    ):
        repository.commit_import_receipt(
            source_hash,
            source_record_id,
            SourceAgent.CODEX,
        )
    statements: list[str] = []

    with database.transaction() as connection:
        connection.set_trace_callback(statements.append)
        proofs = repository.prior_codex_receipt_proofs_on_connection(
            connection,
            requested_ids,
        )
        connection.set_trace_callback(None)

    assert tuple(proof is not None for proof in proofs) == (True, False, True)
    reverse_lookups = [
        statement for statement in statements if "from import_receipts" in statement.lower()
    ]
    assert len(reverse_lookups) == 1


def test_prior_codex_receipt_proofs_batch_selects_deterministic_witness_for_multiple_receipts(
    tmp_path,
):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    multiple_id = "session:multiple-receipts"
    stable_id = "session:repeated-request"
    for source_hash, source_record_id in (
        ("2" * 64, multiple_id),
        ("1" * 64, multiple_id),
        ("3" * 64, stable_id),
    ):
        repository.commit_import_receipt(
            source_hash,
            source_record_id,
            SourceAgent.CODEX,
        )

    with database.transaction() as connection:
        proofs = repository.prior_codex_receipt_proofs_on_connection(
            connection,
            (multiple_id, stable_id, stable_id, multiple_id),
        )
        assert proofs[0] is not None
        assert proofs[3] is not None
        assert proofs[1] is not None
        assert proofs[2] is not None
        assert proofs[0] == proofs[3]
        assert proofs[0]._source_hash == "1" * 64
        assert proofs[0].matches(connection, SourceAgent.CODEX, multiple_id)
        assert proofs[3].matches(connection, SourceAgent.CODEX, multiple_id)
        assert proofs[1].matches(connection, SourceAgent.CODEX, stable_id)
        assert proofs[2].matches(connection, SourceAgent.CODEX, stable_id)


@pytest.mark.parametrize(
    "mutation",
    ("insert", "selected-update", "unselected-update", "unselected-delete"),
)
def test_multiple_prior_codex_receipt_proof_rejects_any_later_receipt_mutation(
    tmp_path,
    mutation,
):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_record_id = "session:multiple-live-revalidation"
    for source_hash in ("1" * 64, "2" * 64):
        repository.commit_import_receipt(
            source_hash,
            source_record_id,
            SourceAgent.CODEX,
        )

    with database.transaction() as connection:
        proof = repository.prior_codex_receipt_proof_on_connection(
            connection,
            source_record_id,
        )
        assert proof is not None
        assert proof._source_hash == "1" * 64
        assert proof.matches(connection, SourceAgent.CODEX, source_record_id)
        if mutation == "insert":
            connection.execute(
                """
                insert into import_receipts(
                    source_hash, source_record_id, source_agent, imported_at
                ) values (?, ?, ?, ?)
                """,
                (
                    "3" * 64,
                    source_record_id,
                    SourceAgent.CODEX.value,
                    "2026-07-17T01:00:00Z",
                ),
            )
        elif mutation.endswith("update"):
            mutated_hash = "1" * 64 if mutation == "selected-update" else "2" * 64
            connection.execute(
                """
                update import_receipts set imported_at = ?
                where source_hash = ? and source_record_id = ? and source_agent = ?
                """,
                (
                    "2026-07-17T01:00:01Z",
                    mutated_hash,
                    source_record_id,
                    SourceAgent.CODEX.value,
                ),
            )
        else:
            connection.execute(
                """
                delete from import_receipts
                where source_hash = ? and source_record_id = ? and source_agent = ?
                """,
                ("2" * 64, source_record_id, SourceAgent.CODEX.value),
            )

        assert not proof.matches(connection, SourceAgent.CODEX, source_record_id)


@pytest.mark.parametrize(
    ("source_hash", "imported_at"),
    (
        ("not-a-sha256", "2026-07-17T01:00:00Z"),
        (sqlite3.Binary(b"4" * 64), "2026-07-17T01:00:00Z"),
        ("4" * 64, "not-a-timestamp"),
        ("4" * 64, sqlite3.Binary(b"2026-07-17T01:00:00Z")),
    ),
)
def test_multiple_prior_codex_receipts_fail_closed_on_malformed_extra_row(
    tmp_path,
    source_hash,
    imported_at,
):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_record_id = "session:malformed-extra-receipt"
    repository.commit_import_receipt(
        "1" * 64,
        source_record_id,
        SourceAgent.CODEX,
    )
    with database.connect() as connection:
        connection.execute(
            """
            insert into import_receipts(
                source_hash, source_record_id, source_agent, imported_at
            ) values (?, ?, ?, ?)
            """,
            (source_hash, source_record_id, SourceAgent.CODEX.value, imported_at),
        )
        connection.commit()

    with database.transaction() as connection:
        with pytest.raises(ValueError, match="malformed prior Codex receipt"):
            repository.prior_codex_receipt_proof_on_connection(
                connection,
                source_record_id,
            )


def test_prior_codex_receipt_proofs_batch_fails_closed_above_receipt_row_budget(
    tmp_path,
    monkeypatch,
):
    database = Database(tmp_path / "memory.db")
    database.initialize()
    repository = CheckpointRepository(database)
    source_record_id = "session:receipt-row-budget"
    for source_hash in ("1" * 64, "2" * 64):
        repository.commit_import_receipt(
            source_hash,
            source_record_id,
            SourceAgent.CODEX,
        )
    monkeypatch.setattr(
        checkpoints_module,
        "_MAX_PRIOR_CODEX_RECEIPT_ROWS",
        1,
        raising=False,
    )

    with database.transaction() as connection:
        with pytest.raises(ValueError, match="too many prior Codex receipt rows"):
            repository.prior_codex_receipt_proof_on_connection(
                connection,
                source_record_id,
            )
