import hashlib
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

import project_memory_hub.storage.database as database_module
from project_memory_hub.storage.database import (
    Database,
    ReadonlyDatabaseSnapshot,
    SchemaUpgradeRequiredError,
)


EXPECTED_COLUMNS = {
    "schema_migrations": ("version", "applied_at"),
    "projects": (
        "project_id",
        "canonical_path",
        "display_name",
        "git_root",
        "git_remote_fingerprint",
        "manifest_fingerprint",
        "discovery_status",
        "permission_status",
        "last_observed_change",
        "inactivity_state",
        "enabled",
        "created_at",
        "updated_at",
        "last_observed_change_epoch_us",
        "path_device",
        "path_inode",
    ),
    "project_facts": (
        "fact_id",
        "project_id",
        "category",
        "normalized_content",
        "evidence_type",
        "evidence_reference",
        "observed_at",
        "confidence",
        "supersedes_fact_id",
        "stale_at",
        "lifecycle_state",
        "created_at",
    ),
    "source_refs": (
        "source_reference_id",
        "source_agent",
        "source_record_id",
        "source_path",
        "content_hash",
        "source_timestamp",
        "parser_version",
        "created_at",
        "capture_project_id",
        "capture_model_id",
        "capture_correlation_id",
    ),
    "behavior_memories": (
        "memory_id",
        "project_id",
        "source_agent",
        "model_id",
        "task_fingerprint",
        "memory_kind",
        "normalized_content",
        "content_hash",
        "source_reference_id",
        "created_at",
        "confidence",
        "lifecycle_state",
    ),
    "memory_issue_resolutions": (
        "resolution_id",
        "project_id",
        "source_agent",
        "model_id",
        "target_content_hash",
        "target_memory_id",
        "source_reference_id",
        "status",
        "resolved_at",
    ),
    "pending_captures": (
        "pending_id",
        "project_id",
        "claimed_source_agent",
        "claimed_model_id",
        "source_record_id",
        "structured_payload_json",
        "structured_hash",
        "created_at",
        "expires_at",
        "verification_state",
    ),
    "pending_capture_history": (
        "pending_id",
        "project_id",
        "claimed_source_agent",
        "claimed_model_id",
        "source_record_id",
        "structured_hash",
        "created_at",
        "expires_at",
        "finalized_at",
        "final_state",
        "source_reference_id",
    ),
    "memory_promotions": (
        "promotion_id",
        "memory_id",
        "proposed_rule",
        "requester",
        "approval_actor",
        "requested_at",
        "approved_at",
        "status",
    ),
    "checkpoints": ("adapter", "scope", "cursor_json", "parser_version", "updated_at"),
    "import_receipts": (
        "source_hash",
        "source_record_id",
        "source_agent",
        "imported_at",
    ),
    "codex_deferred_records": (
        "deferred_id",
        "source_agent",
        "scope",
        "source_record_id",
        "parser_version",
        "source_device",
        "source_inode",
        "prefix_length",
        "prefix_sha256",
        "reason_code",
        "state",
        "first_seen_at",
        "last_attempt_at",
        "attempt_count",
        "last_error_code",
        "recovered_at",
    ),
    "retry_items": (
        "retry_id",
        "payload_json",
        "reason_code",
        "created_at",
        "attempts",
        "last_attempt_at",
    ),
    "improvement_proposals": (
        "proposal_id",
        "signature",
        "title",
        "description",
        "patch",
        "risk",
        "verification_argv_json",
        "verification_summary",
        "approval_status",
        "target_version",
        "rollback_ref",
        "created_at",
        "approved_at",
        "origin",
        "approval_actor",
        "updated_at",
        "apply_attempt_id",
        "repository_root",
        "original_branch",
        "base_commit",
        "proposal_branch",
        "applied_commit",
        "applied_at",
        "rolled_back_at",
        "failure_code",
    ),
    "app_state": ("name", "value_json", "updated_at"),
    "project_registry_state": ("singleton", "generation"),
    "discovery_issues": (
        "path",
        "code",
        "affected_capability",
        "remediation",
        "observed_at",
    ),
    "discovery_duplicate_candidates": (
        "fingerprint_kind",
        "fingerprint",
        "candidate_path",
        "observed_at",
    ),
}


def _insert_project(conn: sqlite3.Connection, project_id: str = "p1") -> None:
    conn.execute(
        "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
        (project_id, f"/tmp/{project_id}", project_id),
    )


def _insert_verified_source(
    connection: sqlite3.Connection,
    source_reference_id: str,
    *,
    project_id: str | None = "p1",
    source_agent: str = "codex",
    model_id: str | None = "gpt-5.6-sol",
) -> None:
    connection.execute(
        """
        insert into source_refs(
            source_reference_id, source_agent, source_record_id, source_path,
            content_hash, source_timestamp, parser_version, created_at,
            capture_project_id, capture_model_id
        ) values (?, ?, ?, null, ?, ?, 'capture-v1', ?, ?, ?)
        """,
        (
            source_reference_id,
            source_agent,
            f"record-{source_reference_id}",
            hashlib.sha256(source_reference_id.encode()).hexdigest(),
            "2026-07-16T00:00:00Z",
            "2026-07-16T00:00:00Z",
            project_id,
            model_id,
        ),
    )


def _insert_behavior_memory(
    connection: sqlite3.Connection,
    memory_id: str,
    source_reference_id: str,
    *,
    project_id: str = "p1",
    source_agent: str = "codex",
    model_id: str = "gpt-5.6-sol",
    memory_kind: str = "open_issue",
) -> None:
    connection.execute(
        """
        insert into behavior_memories(
            memory_id, project_id, source_agent, model_id, task_fingerprint,
            memory_kind, normalized_content, content_hash, source_reference_id,
            created_at, confidence, lifecycle_state
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        """,
        (
            memory_id,
            project_id,
            source_agent,
            model_id,
            f"task-{memory_id}",
            memory_kind,
            f"content-{memory_id}",
            hashlib.sha256(memory_id.encode()).hexdigest(),
            source_reference_id,
            "2026-07-16T00:00:00Z",
            1.0,
        ),
    )


def _insert_issue_resolution(
    connection: sqlite3.Connection,
    resolution_id: str,
    source_reference_id: str,
    *,
    project_id: str = "p1",
    source_agent: str = "codex",
    model_id: str = "gpt-5.6-sol",
    target_content_hash: str = "a" * 64,
    target_memory_id: str | None = "m1",
    status: str = "resolved",
) -> None:
    connection.execute(
        """
        insert into memory_issue_resolutions(
            resolution_id, project_id, source_agent, model_id, target_content_hash,
            target_memory_id, source_reference_id, status, resolved_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            resolution_id,
            project_id,
            source_agent,
            model_id,
            target_content_hash,
            target_memory_id,
            source_reference_id,
            status,
            "2026-07-16T00:00:00Z",
        ),
    )


def _database_physical_state(path: Path) -> tuple[object, ...]:
    state: list[object] = []
    for candidate in (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    ):
        if not candidate.exists():
            state.append(None)
            continue
        digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        metadata = candidate.stat()
        state.append(
            (
                digest,
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_mode,
                metadata.st_nlink,
                metadata.st_uid,
                metadata.st_size,
                metadata.st_mtime_ns,
                metadata.st_ctime_ns,
            )
        )
    return tuple(state)


def test_initialize_creates_schema_and_private_file(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.connect() as conn:
        names = {
            row[0]
            for row in conn.execute("select name from sqlite_master where type in ('table','view')")
        }

    assert set(EXPECTED_COLUMNS) | {"project_facts_fts"} <= names
    assert db.path.stat().st_mode & 0o777 == 0o600


def test_initialize_uses_memory_temp_store_before_schema_migrations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_temp_stores: list[int] = []
    original_create_migration_table = database_module._create_migration_table

    def observe_temp_store(connection: sqlite3.Connection) -> None:
        observed_temp_stores.append(connection.execute("PRAGMA temp_store").fetchone()[0])
        original_create_migration_table(connection)

    monkeypatch.setattr(database_module, "_create_migration_table", observe_temp_store)

    Database(tmp_path / "memory.db").initialize()

    assert observed_temp_stores == [2]


def test_v11_moves_terminal_pending_rows_to_non_content_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 10)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000001"
    created_at = "2026-07-16T00:00:00Z"
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.executemany(
            """
            insert into pending_captures(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_payload_json, structured_hash,
                created_at, expires_at, verification_state
            ) values (?, ?, 'codex', 'gpt-test', ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    "00000000-0000-4000-8000-000000000011",
                    project_id,
                    "active-source",
                    '{"outcome":"active body"}',
                    "a" * 64,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    "pending",
                ),
                (
                    "00000000-0000-4000-8000-000000000012",
                    project_id,
                    "verified-source",
                    '{"outcome":"verified body"}',
                    "b" * 64,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    "verified",
                ),
                (
                    "00000000-0000-4000-8000-000000000013",
                    project_id,
                    "expired-source",
                    '{"outcome":"expired body"}',
                    "c" * 64,
                    created_at,
                    "2026-07-17T00:00:00Z",
                    "expired",
                ),
                (
                    "00000000-0000-4000-8000-000000000014",
                    project_id,
                    "rejected-source",
                    '{"outcome":"rejected body"}',
                    "d" * 64,
                    created_at,
                    "2026-07-18T00:00:00Z",
                    "rejected",
                ),
            ),
        )
        connection.execute(
            """
            insert into app_state(name, value_json, updated_at)
            values ('pending_confirmation:legacy', '{"status":"expired_unverified"}', ?)
            """,
            (created_at,),
        )
        connection.executemany(
            """
            insert into app_state(name, value_json, updated_at)
            values (?, '{"status":"must_survive"}', ?)
            """,
            (
                ("pendingXconfirmation:keep", created_at),
                ("pending-confirmation:keep", created_at),
            ),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.connect(readonly=True) as connection:
        tables = {
            row["name"]
            for row in connection.execute("select name from sqlite_master where type='table'")
        }
        assert "pending_capture_history" in tables
        pending = connection.execute(
            "select source_record_id, verification_state, structured_payload_json "
            "from pending_captures"
        ).fetchall()
        history_columns = tuple(
            row["name"] for row in connection.execute("pragma table_info(pending_capture_history)")
        )
        history = connection.execute(
            """
            select source_record_id, structured_hash, expires_at,
                   finalized_at, final_state, source_reference_id
            from pending_capture_history order by source_record_id
            """
        ).fetchall()
        versions = tuple(
            row[0]
            for row in connection.execute("select version from schema_migrations order by version")
        )
        quick_check = connection.execute("pragma quick_check").fetchone()[0]
        foreign_key_violations = connection.execute("pragma foreign_key_check").fetchall()
        legacy_confirmations = connection.execute(
            "select count(*) from app_state where name = 'pending_confirmation:legacy'"
        ).fetchone()[0]
        retained_similar_names = tuple(
            row["name"]
            for row in connection.execute(
                """
                select name from app_state
                where name in ('pendingXconfirmation:keep', 'pending-confirmation:keep')
                order by name
                """
            )
        )

    assert [tuple(row) for row in pending] == [
        ("active-source", "pending", '{"outcome":"active body"}')
    ]
    assert "structured_payload_json" not in history_columns
    assert [tuple(row) for row in history] == [
        (
            "expired-source",
            "c" * 64,
            "2026-07-17T00:00:00Z",
            "2026-07-17T00:00:00Z",
            "expired",
            None,
        ),
        (
            "rejected-source",
            "d" * 64,
            "2026-07-18T00:00:00Z",
            created_at,
            "rejected",
            None,
        ),
        (
            "verified-source",
            "b" * 64,
            "2026-07-23T00:00:00Z",
            created_at,
            "verified",
            None,
        ),
    ]
    assert versions == tuple(range(1, 13))
    assert quick_check == "ok"
    assert foreign_key_violations == []
    assert legacy_confirmations == 0
    assert retained_similar_names == (
        "pending-confirmation:keep",
        "pendingXconfirmation:keep",
    )


def test_v12_backfills_only_unique_verified_capture_correlations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 11)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000101"
    unique_source = "00000000-0000-4000-8000-000000000102"
    ambiguous_source = "00000000-0000-4000-8000-000000000103"
    no_verified_source = "00000000-0000-4000-8000-000000000106"
    created_at = "2026-07-16T00:00:00Z"
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.executemany(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, 'codex', ?, null, ?, ?, 'capture-v1', ?, ?, 'gpt-test')
            """,
            (
                (
                    unique_source,
                    "session-a:turn-a",
                    "a" * 64,
                    created_at,
                    created_at,
                    project_id,
                ),
                (
                    ambiguous_source,
                    "session-b:turn-b",
                    "b" * 64,
                    created_at,
                    created_at,
                    project_id,
                ),
                (
                    no_verified_source,
                    "session-e:turn-e",
                    "e" * 64,
                    created_at,
                    created_at,
                    project_id,
                ),
            ),
        )
        connection.executemany(
            """
            insert into pending_capture_history(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_hash, created_at, expires_at,
                finalized_at, final_state, source_reference_id
            ) values (?, ?, 'codex', 'gpt-test', ?, ?, ?, ?, ?, 'verified', ?)
            """,
            (
                (
                    "00000000-0000-4000-8000-000000000111",
                    project_id,
                    "thread-a",
                    "a" * 64,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    unique_source,
                ),
                (
                    "00000000-0000-4000-8000-000000000112",
                    project_id,
                    "thread-b",
                    "b" * 64,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    ambiguous_source,
                ),
                (
                    "00000000-0000-4000-8000-000000000113",
                    project_id,
                    "thread-c",
                    "b" * 64,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    ambiguous_source,
                ),
            ),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.connect(readonly=True) as connection:
        correlations = {
            row["source_record_id"]: row["capture_correlation_id"]
            for row in connection.execute(
                "select source_record_id, capture_correlation_id from source_refs"
            )
        }
        versions = tuple(
            row[0]
            for row in connection.execute("select version from schema_migrations order by version")
        )
        indexes = {row["name"] for row in connection.execute("pragma index_list(source_refs)")}

    assert correlations == {
        "session-a:turn-a": "thread-a",
        "session-b:turn-b": None,
        "session-e:turn-e": None,
    }
    assert versions == tuple(range(1, 13))
    assert "idx_source_refs_capture_correlation" in indexes


def test_v12_backfill_keeps_cross_source_reference_conflicts_null(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 11)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.connect() as connection:
        connection.execute("drop table pending_capture_history")
        connection.execute(
            """
            create table pending_capture_history (
                pending_id text primary key,
                project_id text not null references projects(project_id) on delete cascade,
                claimed_source_agent text not null,
                claimed_model_id text not null,
                source_record_id text not null,
                structured_hash text not null,
                created_at text not null,
                expires_at text not null,
                finalized_at text not null,
                final_state text not null
                    check (final_state in ('verified', 'expired', 'rejected')),
                source_reference_id text
                    references source_refs(source_reference_id) on delete set null
            )
            """
        )
        connection.commit()

    project_id = "00000000-0000-4000-8000-000000000151"
    created_at = "2026-07-16T00:00:00Z"
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.executemany(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, 'codex', ?, null, ?, ?, 'capture-v1', ?, ?, 'gpt-test')
            """,
            (
                (
                    "00000000-0000-4000-8000-000000000152",
                    "session-conflict-a:turn",
                    "c" * 64,
                    created_at,
                    created_at,
                    project_id,
                ),
                (
                    "00000000-0000-4000-8000-000000000153",
                    "session-conflict-b:turn",
                    "c" * 64,
                    created_at,
                    created_at,
                    project_id,
                ),
            ),
        )
        connection.executemany(
            """
            insert into pending_capture_history(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_hash, created_at, expires_at,
                finalized_at, final_state, source_reference_id
            ) values (?, ?, 'codex', 'gpt-test', 'thread-shared', ?, ?, ?, ?, 'verified', ?)
            """,
            (
                (
                    "00000000-0000-4000-8000-000000000161",
                    project_id,
                    "c" * 64,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    "00000000-0000-4000-8000-000000000152",
                ),
                (
                    "00000000-0000-4000-8000-000000000162",
                    project_id,
                    "c" * 64,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    "00000000-0000-4000-8000-000000000153",
                ),
            ),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.connect(readonly=True) as connection:
        correlations = tuple(
            row["capture_correlation_id"]
            for row in connection.execute(
                "select capture_correlation_id from source_refs order by source_record_id"
            )
        )

    assert correlations == (None, None)


def test_v12_backfill_rejects_each_verified_history_provenance_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 11)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000201"
    other_project_id = "00000000-0000-4000-8000-000000000202"
    created_at = "2026-07-16T00:00:00Z"
    cases = (
        (
            "00000000-0000-4000-8000-000000000211",
            "session-agent:turn",
            "a" * 64,
            "chatgpt",
            project_id,
            "gpt-test",
            "a" * 64,
        ),
        (
            "00000000-0000-4000-8000-000000000212",
            "session-project:turn",
            "b" * 64,
            "codex",
            other_project_id,
            "gpt-test",
            "b" * 64,
        ),
        (
            "00000000-0000-4000-8000-000000000213",
            "session-model:turn",
            "c" * 64,
            "codex",
            project_id,
            "gpt-other",
            "c" * 64,
        ),
        (
            "00000000-0000-4000-8000-000000000214",
            "session-hash:turn",
            "d" * 64,
            "codex",
            project_id,
            "gpt-test",
            "e" * 64,
        ),
    )
    with database.transaction() as connection:
        connection.executemany(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (
                (project_id, str(tmp_path / "project"), "Project"),
                (other_project_id, str(tmp_path / "other-project"), "Other Project"),
            ),
        )
        connection.executemany(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, 'codex', ?, null, ?, ?, 'capture-v1', ?, ?, 'gpt-test')
            """,
            (
                (source_id, source_record_id, content_hash, created_at, created_at, project_id)
                for source_id, source_record_id, content_hash, *_mismatch in cases
            ),
        )
        connection.executemany(
            """
            insert into pending_capture_history(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_hash, created_at, expires_at,
                finalized_at, final_state, source_reference_id
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, 'verified', ?)
            """,
            (
                (
                    f"00000000-0000-4000-8000-00000000022{index}",
                    claimed_project_id,
                    claimed_source_agent,
                    claimed_model_id,
                    f"thread-mismatch-{index}",
                    structured_hash,
                    created_at,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    source_id,
                )
                for index, (
                    source_id,
                    _source_record_id,
                    _content_hash,
                    claimed_source_agent,
                    claimed_project_id,
                    claimed_model_id,
                    structured_hash,
                ) in enumerate(cases, start=1)
            ),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.connect(readonly=True) as connection:
        correlations = tuple(
            row["capture_correlation_id"]
            for row in connection.execute(
                "select capture_correlation_id from source_refs order by source_record_id"
            )
        )

    assert correlations == (None, None, None, None)


def test_v12_migration_has_bounded_work_at_scale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 11)
    version, sql = next(item for item in migrations if item[0] == 12)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000301"
    created_at = "2026-07-16T00:00:00Z"
    source_count = 4_000
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.executemany(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at,
                capture_project_id, capture_model_id
            ) values (?, 'codex', ?, null, ?, ?, 'capture-v1', ?, ?, 'gpt-test')
            """,
            (
                (
                    f"source-{index:06d}",
                    f"session-{index:06d}:turn",
                    hashlib.sha256(f"payload-{index}".encode()).hexdigest(),
                    created_at,
                    created_at,
                    project_id,
                )
                for index in range(source_count)
            ),
        )
        connection.executemany(
            """
            insert into pending_capture_history(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_hash, created_at, expires_at,
                finalized_at, final_state, source_reference_id
            ) values (?, ?, 'codex', 'gpt-test', ?, ?, ?, ?, ?, 'verified', ?)
            """,
            (
                (
                    f"pending-{index:06d}",
                    project_id,
                    f"thread-{index:06d}",
                    hashlib.sha256(f"payload-{index}".encode()).hexdigest(),
                    created_at,
                    "2026-07-23T00:00:00Z",
                    created_at,
                    f"source-{index:06d}",
                )
                for index in range(source_count)
            ),
        )

    progress_calls = 0

    def stop_quadratic_migration() -> int:
        nonlocal progress_calls
        progress_calls += 1
        return int(progress_calls > 20_000)

    with database.connect() as connection:
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.set_progress_handler(stop_quadratic_migration, 1_000)
        database_module._apply_migration(connection, version, sql)
        connection.set_progress_handler(None, 0)
        backfilled = connection.execute(
            "select count(*) from source_refs where capture_correlation_id is not null"
        ).fetchone()[0]

    assert backfilled == source_count
    assert progress_calls < 20_000


def test_v11_history_cap_retains_latest_legacy_terminal_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 10)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000031"
    terminal_count = 50_002

    def timestamp(index: int) -> str:
        return datetime.fromtimestamp(index, timezone.utc).isoformat().replace("+00:00", "Z")

    def terminal_rows():
        for index in range(terminal_count):
            yield (
                f"00000000-0000-4000-8000-{terminal_count - index:012x}",
                project_id,
                f"legacy-source-{index}",
                '{"outcome":"legacy body"}',
                f"{index:064x}",
                timestamp(index),
                timestamp(index),
                (
                    "expired"
                    if index == terminal_count - 1
                    else "verified"
                    if index % 2 == 0
                    else "rejected"
                ),
            )

    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.executemany(
            """
            insert into pending_captures(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_payload_json, structured_hash,
                created_at, expires_at, verification_state
            ) values (?, ?, 'codex', 'gpt-test', ?, ?, ?, ?, ?, ?)
            """,
            terminal_rows(),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.connect(readonly=True) as connection:
        history_count = connection.execute(
            "select count(*) from pending_capture_history"
        ).fetchone()[0]
        retained = {
            row["source_record_id"]: row["finalized_at"]
            for row in connection.execute(
                """
                select source_record_id, finalized_at
                from pending_capture_history
                where source_record_id in (?, ?)
                """,
                ("legacy-source-0", f"legacy-source-{terminal_count - 1}"),
            )
        }

    assert history_count == 50_000
    assert "legacy-source-0" not in retained
    assert retained[f"legacy-source-{terminal_count - 1}"] == timestamp(terminal_count - 1)


def test_v11_active_pending_table_rejects_terminal_state_updates(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000041"
    pending_id = "00000000-0000-4000-8000-000000000042"
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.execute(
            """
            insert into pending_captures(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_payload_json, structured_hash,
                created_at, expires_at, verification_state
            ) values (?, ?, 'codex', 'gpt-test', 'active-source', '{}', ?, ?, ?, 'pending')
            """,
            (
                pending_id,
                project_id,
                "f" * 64,
                "2026-07-16T00:00:00Z",
                "2026-07-23T00:00:00Z",
            ),
        )

    with pytest.raises(sqlite3.IntegrityError):
        with database.transaction() as connection:
            connection.execute(
                "update pending_captures set verification_state = 'verified' where pending_id = ?",
                (pending_id,),
            )

    with database.connect(readonly=True) as connection:
        state = connection.execute(
            "select verification_state from pending_captures where pending_id = ?",
            (pending_id,),
        ).fetchone()[0]
    assert state == "pending"


def test_v11_history_cleanup_order_uses_the_finalized_epoch_index(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.connect(readonly=True) as connection:
        plan = " ".join(
            row["detail"]
            for row in connection.execute(
                """
                explain query plan
                select pending_id from pending_capture_history
                order by strict_utc_epoch_us(finalized_at), finalized_at, pending_id
                limit 1
                """
            )
        )

    assert "idx_pending_capture_history_finalized" in plan
    assert "TEMP B-TREE" not in plan


def test_failed_v11_history_migration_rolls_back_payload_and_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 10)
    version, sql = next(item for item in migrations if item[0] == 11)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000021"
    pending_id = "00000000-0000-4000-8000-000000000022"
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.execute(
            """
            insert into pending_captures(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_payload_json, structured_hash,
                created_at, expires_at, verification_state
            ) values (?, ?, 'codex', 'gpt-test', 'legacy-source', ?, ?, ?, ?, 'verified')
            """,
            (
                pending_id,
                project_id,
                '{"outcome":"must survive rollback"}',
                "e" * 64,
                "2026-07-16T00:00:00Z",
                "2026-07-23T00:00:00Z",
            ),
        )

    poisoned = (*legacy_migrations, (version, f"{sql}\nthis is invalid sql;"))
    monkeypatch.setattr(database_module, "_load_migrations", lambda: poisoned)
    with pytest.raises(sqlite3.Error):
        database.initialize()

    with database.connect(readonly=True) as connection:
        versions = tuple(
            row[0]
            for row in connection.execute("select version from schema_migrations order by version")
        )
        history_table = connection.execute(
            "select name from sqlite_master where name = 'pending_capture_history'"
        ).fetchone()
        pending = connection.execute(
            """
            select structured_payload_json, verification_state
            from pending_captures where pending_id = ?
            """,
            (pending_id,),
        ).fetchone()

    assert versions == tuple(range(1, 11))
    assert history_table is None
    assert tuple(pending) == ('{"outcome":"must survive rollback"}', "verified")


def test_failed_v12_correlation_migration_rolls_back_schema_and_temp_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 11)
    version, sql = next(item for item in migrations if item[0] == 12)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    migration_prefix, _remainder = sql.split("UPDATE source_refs AS source", maxsplit=1)
    poisoned_sql = f"{migration_prefix}\nselect * from missing_v12_failure_table;"

    with database.connect() as connection:
        connection.execute("PRAGMA temp_store=MEMORY")
        with pytest.raises(sqlite3.OperationalError, match="missing_v12_failure_table"):
            database_module._apply_migration(connection, version, poisoned_sql)

        columns = tuple(row["name"] for row in connection.execute("pragma table_info(source_refs)"))
        temp_objects = tuple(
            row["name"]
            for row in connection.execute(
                "select name from sqlite_temp_master where name glob 'pmh_v12_*'"
            )
        )
        version_applied = connection.execute(
            "select count(*) from schema_migrations where version = 12"
        ).fetchone()[0]
        persistent_objects = tuple(
            row["name"]
            for row in connection.execute(
                """
                select name from sqlite_master
                where name in (
                    'idx_source_refs_capture_correlation',
                    'capture_correlation_insert',
                    'capture_correlation_update'
                )
                """
            )
        )

    assert "capture_correlation_id" not in columns
    assert temp_objects == ()
    assert version_applied == 0
    assert persistent_objects == ()


def test_v12_correlation_trigger_and_unique_index_fail_closed(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    project_id = "00000000-0000-4000-8000-000000000401"
    created_at = "2026-07-16T00:00:00Z"
    insert_source = """
        insert into source_refs(
            source_reference_id, source_agent, source_record_id, source_path,
            content_hash, source_timestamp, parser_version, created_at,
            capture_project_id, capture_model_id, capture_correlation_id
        ) values (?, 'codex', ?, ?, ?, ?, ?, ?, ?, 'gpt-test', ?)
    """
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values (?, ?, ?)",
            (project_id, str(tmp_path / "project"), "Project"),
        )
        connection.execute(
            insert_source,
            (
                "00000000-0000-4000-8000-000000000411",
                "session-valid:turn",
                None,
                "a" * 64,
                created_at,
                "capture-v1",
                created_at,
                project_id,
                "thread-shared",
            ),
        )

    with pytest.raises(sqlite3.IntegrityError, match="capture correlation requires"):
        with database.transaction() as connection:
            connection.execute(
                insert_source,
                (
                    "00000000-0000-4000-8000-000000000412",
                    "session-invalid:turn",
                    str(tmp_path / "source.jsonl"),
                    "b" * 64,
                    created_at,
                    "codex-v3",
                    created_at,
                    project_id,
                    "thread-invalid",
                ),
            )

    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint failed"):
        with database.transaction() as connection:
            connection.execute(
                insert_source,
                (
                    "00000000-0000-4000-8000-000000000413",
                    "session-duplicate:turn",
                    None,
                    "a" * 64,
                    created_at,
                    "capture-v1",
                    created_at,
                    project_id,
                    "thread-shared",
                ),
            )

    with pytest.raises(sqlite3.IntegrityError, match="capture correlation requires"):
        with database.transaction() as connection:
            connection.execute(
                "update source_refs set source_path = ? where source_record_id = ?",
                (str(tmp_path / "source.jsonl"), "session-valid:turn"),
            )

    with database.connect(readonly=True) as connection:
        retained = connection.execute(
            """
            select parser_version, source_path, capture_correlation_id
            from source_refs where source_record_id = 'session-valid:turn'
            """
        ).fetchone()
        source_count = connection.execute("select count(*) from source_refs").fetchone()[0]

    assert tuple(retained) == ("capture-v1", None, "thread-shared")
    assert source_count == 1


def test_readonly_schema_check_accepts_current_database_without_physical_changes(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    before = _database_physical_state(database.path)

    database.require_current_schema_readonly()

    assert _database_physical_state(database.path) == before


def test_readonly_schema_check_rejects_v11_without_migrating(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("delete from schema_migrations where version = 12")
    before = _database_physical_state(database.path)

    with pytest.raises(SchemaUpgradeRequiredError, match="schema upgrade required"):
        database.require_current_schema_readonly()

    assert _database_physical_state(database.path) == before
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from schema_migrations where version = 12"
            ).fetchone()[0]
            == 0
        )


def test_initialize_creates_exact_contract_columns_indexes_and_triggers(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.connect(readonly=True) as conn:
        for table, expected in EXPECTED_COLUMNS.items():
            actual = tuple(row["name"] for row in conn.execute(f"pragma table_info({table})"))
            assert actual == expected

        indexes = {
            row["name"]
            for table in EXPECTED_COLUMNS
            for row in conn.execute(f"pragma index_list({table})")
        }
        triggers = {
            row["name"]
            for row in conn.execute("select name from sqlite_master where type='trigger'")
        }
        resolution_index_rows = conn.execute(
            "pragma index_list(memory_issue_resolutions)"
        ).fetchall()
        resolution_indexes = {
            row["name"]: (
                row["unique"],
                row["partial"],
                tuple(
                    column["name"] for column in conn.execute(f"pragma index_info({row['name']})")
                ),
            )
            for row in resolution_index_rows
            if row["name"].startswith("idx_issue_resolutions_")
        }
        deferred_index_rows = conn.execute("pragma index_list(codex_deferred_records)").fetchall()
        deferred_indexes = {
            row["name"]: (
                row["unique"],
                row["partial"],
                tuple(
                    column["name"] for column in conn.execute(f"pragma index_info({row['name']})")
                ),
            )
            for row in deferred_index_rows
        }
        pending_indexes = {
            name: tuple(column["name"] for column in conn.execute(f"pragma index_info({name})"))
            for name in (
                "idx_pending_captures_verification_expiry",
                "idx_pending_captures_project_state",
                "idx_pending_capture_history_finalized",
                "idx_pending_capture_history_project_finalized",
            )
        }
        versions = conn.execute("select version from schema_migrations order by version").fetchall()

    assert {
        "idx_projects_canonical_path",
        "idx_project_facts_project_category_lifecycle",
        "idx_behavior_memories_project_namespace_lifecycle",
        "idx_behavior_memories_compaction_kind_order",
        "idx_behavior_memories_active_namespace",
        "idx_projects_active_observed_epoch",
        "idx_projects_path_identity",
        "idx_pending_captures_verification_expiry",
        "idx_pending_captures_project_state",
        "idx_pending_capture_history_finalized",
        "idx_pending_capture_history_project_finalized",
        "idx_checkpoints_adapter_scope",
        "idx_retry_items_created_at",
        "idx_improvement_proposals_status_created_at",
        "idx_improvement_proposals_active_signature",
        "idx_discovery_issues_code",
        "idx_discovery_duplicates_fingerprint",
        "idx_issue_resolutions_resolved_unique",
        "idx_issue_resolutions_not_found_unique",
        "idx_issue_resolutions_target",
        "idx_codex_deferred_pending",
        "idx_source_refs_capture_correlation",
    } <= indexes
    assert {
        "project_facts_ai",
        "project_facts_ad",
        "project_facts_au",
        "projects_registry_generation_insert",
        "projects_registry_generation_delete",
        "projects_registry_generation_update",
        "capture_provenance_pair_insert",
        "capture_provenance_pair_update",
        "capture_correlation_insert",
        "capture_correlation_update",
        "issue_resolution_target_insert",
        "issue_resolution_target_update",
        "issue_resolution_source_insert",
        "issue_resolution_source_update",
        "issue_resolution_source_ref_update",
        "issue_resolution_target_memory_update",
    } <= triggers
    assert resolution_indexes == {
        "idx_issue_resolutions_resolved_unique": (
            1,
            1,
            (
                "project_id",
                "source_agent",
                "model_id",
                "source_reference_id",
                "target_content_hash",
                "target_memory_id",
            ),
        ),
        "idx_issue_resolutions_not_found_unique": (
            1,
            1,
            (
                "project_id",
                "source_agent",
                "model_id",
                "source_reference_id",
                "target_content_hash",
            ),
        ),
        "idx_issue_resolutions_target": (
            0,
            1,
            ("project_id", "source_agent", "model_id", "target_memory_id"),
        ),
    }
    assert deferred_indexes["idx_codex_deferred_pending"] == (
        0,
        1,
        ("state", "last_attempt_at", "first_seen_at", "deferred_id"),
    )
    assert (
        1,
        0,
        (
            "source_agent",
            "scope",
            "source_device",
            "source_inode",
            "parser_version",
            "source_record_id",
        ),
    ) in deferred_indexes.values()
    assert pending_indexes == {
        "idx_pending_captures_verification_expiry": (
            "verification_state",
            "expires_at",
            "pending_id",
        ),
        "idx_pending_captures_project_state": ("project_id", "verification_state"),
        "idx_pending_capture_history_finalized": (None, "finalized_at", "pending_id"),
        "idx_pending_capture_history_project_finalized": (
            "project_id",
            "finalized_at",
            "pending_id",
        ),
    }
    assert tuple(row[0] for row in versions) == tuple(range(1, 13))


def test_codex_deferred_schema_rejects_private_or_malformed_locator_rows(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    valid = (
        "00000000-0000-4000-8000-000000000010",
        "codex",
        "sessions/example.jsonl",
        "session:turn",
        "codex-v3",
        1,
        2,
        100,
        "a" * 64,
        "project_not_found",
        "pending",
        "2026-07-17T00:00:00Z",
        None,
        0,
        "project_not_found",
        None,
    )
    columns = tuple(EXPECTED_COLUMNS["codex_deferred_records"])
    placeholders = ",".join("?" for _ in columns)
    statement = f"insert into codex_deferred_records({','.join(columns)}) values ({placeholders})"

    with database.transaction() as connection:
        connection.execute(statement, valid)

    def invalid_row(case_number: int, **changes: object) -> tuple[object, ...]:
        row = list(valid)
        row[0] = f"{case_number:08x}-0000-4000-8000-{case_number:012x}"
        row[6] = 100 + case_number
        for name, value in changes.items():
            row[columns.index(name)] = value
        return tuple(row)

    invalid_rows = (
        invalid_row(1, source_agent="chatgpt"),
        invalid_row(2, deferred_id=""),
        invalid_row(3, deferred_id="not-a-uuid"),
        invalid_row(4, deferred_id="00000000-0000-4000-8000-00000000000A"),
        invalid_row(5, scope="/absolute/session.jsonl"),
        invalid_row(6, scope="../secret.jsonl"),
        invalid_row(7, scope="sessions\\session.jsonl"),
        invalid_row(8, scope="sessions//session.jsonl"),
        invalid_row(9, scope="sessions/./session.jsonl"),
        invalid_row(10, scope="sessions/.git/session.jsonl"),
        invalid_row(11, scope="sessions/\u202e/session.jsonl"),
        invalid_row(12, scope="sessions/\x1f/session.jsonl"),
        invalid_row(13, source_record_id="session:/private/path"),
        invalid_row(14, source_record_id="会话:turn"),
        invalid_row(15, parser_version="codex-v2"),
        invalid_row(16, scope=sqlite3.Binary(b"session.jsonl")),
        invalid_row(17, source_record_id=sqlite3.Binary(b"session:turn")),
        invalid_row(18, parser_version=sqlite3.Binary(b"codex-v3")),
        invalid_row(19, prefix_length=0),
        invalid_row(20, prefix_sha256="A" * 64),
        invalid_row(21, prefix_sha256=sqlite3.Binary(b"a" * 64)),
        invalid_row(22, state="recovered", recovered_at=None),
        invalid_row(
            23,
            deferred_id=sqlite3.Binary(b"00000000-0000-4000-8000-000000000023"),
        ),
        invalid_row(24, source_agent=sqlite3.Binary(b"codex")),
        invalid_row(25, source_device=sqlite3.Binary(b"1")),
        invalid_row(26, reason_code=sqlite3.Binary(b"project_not_found")),
        invalid_row(27, reason_code="rejected"),
        invalid_row(28, state=sqlite3.Binary(b"pending")),
        invalid_row(29, first_seen_at=sqlite3.Binary(b"2026-07-17T00:00:00Z")),
        invalid_row(30, last_attempt_at=sqlite3.Binary(b"2026-07-17T00:00:00Z")),
        invalid_row(31, attempt_count=sqlite3.Binary(b"1")),
        invalid_row(32, last_error_code=sqlite3.Binary(b"project_not_found")),
        invalid_row(33, last_error_code="private_error"),
        invalid_row(
            34,
            state="recovered",
            recovered_at=sqlite3.Binary(b"2026-07-17T00:00:00Z"),
        ),
    )
    for row in invalid_rows:
        with pytest.raises(sqlite3.IntegrityError):
            with database.transaction() as connection:
                connection.execute(statement, row)

    assert {
        "cwd",
        "project_id",
        "model_id",
        "objective",
        "outcome",
        "changed_paths",
        "payload_json",
    }.isdisjoint(columns)


def test_project_registry_generation_tracks_only_matching_identity_changes(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.transaction() as connection:
        assert (
            connection.execute(
                "select generation from project_registry_state where singleton = 1"
            ).fetchone()[0]
            == 0
        )
        _insert_project(connection)
        assert (
            connection.execute(
                "select generation from project_registry_state where singleton = 1"
            ).fetchone()[0]
            == 1
        )
        connection.execute(
            "update projects set last_observed_change = ? where project_id = 'p1'",
            ("2026-07-16T00:00:00Z",),
        )
        assert (
            connection.execute(
                "select generation from project_registry_state where singleton = 1"
            ).fetchone()[0]
            == 1
        )
        connection.execute("update projects set display_name = 'renamed' where project_id = 'p1'")
        assert (
            connection.execute(
                "select generation from project_registry_state where singleton = 1"
            ).fetchone()[0]
            == 2
        )
        connection.execute("delete from projects where project_id = 'p1'")
        assert (
            connection.execute(
                "select generation from project_registry_state where singleton = 1"
            ).fetchone()[0]
            == 3
        )


def test_connection_configures_rows_pragmas_wal_and_private_sidecars(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.connect() as conn:
        row = conn.execute("select 1 as value").fetchone()
        assert isinstance(row, sqlite3.Row)
        assert row["value"] == 1
        assert conn.execute("pragma foreign_keys").fetchone()[0] == 1
        assert conn.execute("pragma busy_timeout").fetchone()[0] == 5000
        assert conn.execute("pragma journal_mode").fetchone()[0] == "wal"
        for path in (db.path, Path(f"{db.path}-wal"), Path(f"{db.path}-shm")):
            if path.exists():
                assert path.stat().st_mode & 0o777 == 0o600


def test_readonly_connection_never_changes_journal_mode(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("pragma journal_mode=delete").fetchone()[0] == "delete"
    db.path.chmod(0o644)

    with db.connect(readonly=True) as conn:
        assert db.path.stat().st_mode & 0o777 == 0o644
        assert conn.execute("pragma journal_mode").fetchone()[0] == "delete"
        assert conn.execute("pragma foreign_keys").fetchone()[0] == 1
        assert conn.execute("pragma busy_timeout").fetchone()[0] == 5000
        with pytest.raises(sqlite3.OperationalError):
            _insert_project(conn)

    with sqlite3.connect(db.path) as conn:
        assert conn.execute("pragma journal_mode").fetchone()[0] == "delete"


@pytest.mark.parametrize("remove_shm", (True, False))
def test_readonly_connection_fails_closed_without_touching_live_wal_sidecars(
    tmp_path: Path, remove_shm: bool
) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    script = "\n".join(
        (
            "import os, sqlite3, sys",
            "connection = sqlite3.connect(sys.argv[1])",
            "connection.execute('pragma journal_mode=wal')",
            "connection.execute('pragma wal_autocheckpoint=0')",
            'connection.execute("insert into app_state(name,value_json,updated_at) '
            "values('crash-row','{}','now')\")",
            "connection.commit()",
            "os._exit(0)",
        )
    )
    subprocess.run([sys.executable, "-c", script, str(db.path)], check=True)
    wal = Path(f"{db.path}-wal")
    shm = Path(f"{db.path}-shm")
    assert wal.stat().st_size > 0
    if remove_shm:
        shm.unlink()

    def metadata(path: Path):
        if not path.exists():
            return None
        value = path.stat()
        return (
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    before = tuple(metadata(path) for path in (db.path, wal, shm))
    before_names = tuple(sorted(path.name for path in tmp_path.iterdir()))

    with pytest.raises(RuntimeError, match="read-only snapshot unavailable"):
        with db.connect(readonly=True) as connection:
            connection.execute("select count(*) from app_state").fetchone()

    assert tuple(metadata(path) for path in (db.path, wal, shm)) == before
    assert tuple(sorted(path.name for path in tmp_path.iterdir())) == before_names


def test_successful_readonly_connection_is_physically_no_write(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    tracked = (db.path, Path(f"{db.path}-wal"), Path(f"{db.path}-shm"))

    def metadata(path: Path):
        if not path.exists():
            return None
        value = path.stat()
        return (
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )

    before = tuple(metadata(path) for path in tracked)
    before_names = tuple(sorted(path.name for path in tmp_path.iterdir()))

    with db.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from app_state").fetchone()[0] == 0

    assert tuple(metadata(path) for path in tracked) == before
    assert tuple(sorted(path.name for path in tmp_path.iterdir())) == before_names


def test_transaction_commits_and_closes_connection(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.transaction() as conn:
        _insert_project(conn)
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("select 1")

    with db.connect() as read_conn:
        assert read_conn.execute("select count(*) from projects").fetchone()[0] == 1


def test_transaction_rolls_back_on_error_and_closes_connection(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            _insert_project(conn)
            raise RuntimeError("stop")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("select 1")

    with db.connect() as read_conn:
        count = read_conn.execute("select count(*) from projects").fetchone()[0]
    assert count == 0


def test_project_fact_fts_stays_synchronized(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()

    with db.transaction() as conn:
        _insert_project(conn)
        conn.execute(
            """
            insert into project_facts(
                fact_id, project_id, category, normalized_content, evidence_type,
                evidence_reference, observed_at, confidence, created_at
            ) values(?,?,?,?,?,?,?,?,?)
            """,
            (
                "f1",
                "p1",
                "decision",
                "alpha memory",
                "file",
                "README",
                "now",
                0.8,
                "now",
            ),
        )

    with db.connect() as conn:
        assert (
            conn.execute(
                "select count(*) from project_facts_fts where project_facts_fts match 'alpha'"
            ).fetchone()[0]
            == 1
        )

    with db.transaction() as conn:
        conn.execute(
            "update project_facts set normalized_content='beta memory', "
            "category='risk' where fact_id='f1'"
        )
    with db.connect() as conn:
        assert (
            conn.execute(
                "select count(*) from project_facts_fts where project_facts_fts match 'alpha'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "select count(*) from project_facts_fts where project_facts_fts match 'beta'"
            ).fetchone()[0]
            == 1
        )

    with db.transaction() as conn:
        conn.execute("delete from project_facts where fact_id='f1'")
    with db.connect() as conn:
        assert (
            conn.execute(
                "select count(*) from project_facts_fts where project_facts_fts match 'beta'"
            ).fetchone()[0]
            == 0
        )


def test_failed_migration_rolls_back_schema_and_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    migrations = database_module._load_migrations()
    monkeypatch.setattr(
        database_module,
        "_load_migrations",
        lambda: (
            *migrations,
            (13, "create table rolled_back(value text);\nthis is invalid sql;"),
        ),
    )

    with pytest.raises(sqlite3.Error):
        db.initialize()

    with db.connect() as conn:
        versions = [
            row[0] for row in conn.execute("select version from schema_migrations order by version")
        ]
        table = conn.execute("select name from sqlite_master where name='rolled_back'").fetchone()
    assert versions == list(range(1, 13))
    assert table is None


def test_project_path_identity_migration_leaves_legacy_rows_untrusted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 7)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "insert into projects(project_id, canonical_path, display_name) values(?,?,?)",
            ("legacy-project", str(tmp_path / "legacy-project"), "Legacy"),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select path_device, path_inode from projects where project_id = ?",
            ("legacy-project",),
        ).fetchone()
        catchup = connection.execute(
            "select value_json from app_state where name = 'reconcile_catchup_required'"
        ).fetchone()

    assert row is not None
    assert (row["path_device"], row["path_inode"]) == (None, None)
    assert catchup is not None
    assert catchup["value_json"] == '{"required":true}'


def test_project_path_identity_columns_are_paired_and_unique(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into projects(
                    project_id, canonical_path, display_name, path_device, path_inode
                ) values ('partial', '/tmp/partial', 'Partial', 1, null)
                """
            )
        for index, (device, inode) in enumerate(
            ((-1, 2), (1, -2), (1.5, 2), (1, 2.5), ("invalid", 2))
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    insert into projects(
                        project_id, canonical_path, display_name,
                        path_device, path_inode
                    ) values (?, ?, ?, ?, ?)
                    """,
                    (
                        f"invalid-{index}",
                        f"/tmp/invalid-{index}",
                        "Invalid",
                        device,
                        inode,
                    ),
                )
        connection.execute(
            """
            insert into projects(
                project_id, canonical_path, display_name, path_device, path_inode
            ) values ('first', '/tmp/first', 'First', 1, 2)
            """
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into projects(
                    project_id, canonical_path, display_name, path_device, path_inode
                ) values ('second', '/tmp/second', 'Second', 1, 2)
                """
            )


def test_explicit_issue_resolution_migration_upgrades_v8_and_backfills_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    v8_migrations = tuple(item for item in migrations if item[0] <= 8)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: v8_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_project(connection, "p2")
        legacy_sources = (
            ("source-1", None, "capture-v1"),
            ("ambiguous-project", None, "capture-v1"),
            ("ambiguous-model", None, "capture-v1"),
            ("ambiguous-agent", None, "capture-v1"),
            ("wrong-parser", None, "codex-v3"),
            ("persisted-path", "/tmp/capture.jsonl", "capture-v1"),
        )
        for source_reference_id, source_path, parser_version in legacy_sources:
            connection.execute(
                """
                insert into source_refs(
                    source_reference_id, source_agent, source_record_id, source_path,
                    content_hash, source_timestamp, parser_version, created_at
                ) values (?, 'codex', ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_reference_id,
                    f"record-{source_reference_id}",
                    source_path,
                    hashlib.sha256(source_reference_id.encode()).hexdigest(),
                    "2026-07-16T00:00:00Z",
                    parser_version,
                    "2026-07-16T00:00:00Z",
                ),
            )
        _insert_behavior_memory(connection, "m1", "source-1")
        _insert_behavior_memory(connection, "project-p1", "ambiguous-project")
        _insert_behavior_memory(
            connection,
            "project-p2",
            "ambiguous-project",
            project_id="p2",
        )
        _insert_behavior_memory(connection, "model-sol", "ambiguous-model")
        _insert_behavior_memory(
            connection,
            "model-other",
            "ambiguous-model",
            model_id="gpt-5.6-other",
        )
        _insert_behavior_memory(connection, "agent-codex", "ambiguous-agent")
        _insert_behavior_memory(
            connection,
            "agent-chatgpt",
            "ambiguous-agent",
            source_agent="chatgpt",
        )
        _insert_behavior_memory(connection, "parser-memory", "wrong-parser")
        _insert_behavior_memory(connection, "path-memory", "persisted-path")

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.transaction() as connection:
        versions = tuple(
            row[0]
            for row in connection.execute("select version from schema_migrations order by version")
        )
        provenance = {
            row["source_reference_id"]: (
                row["capture_project_id"],
                row["capture_model_id"],
            )
            for row in connection.execute(
                """
                select source_reference_id, capture_project_id, capture_model_id
                from source_refs
                """
            )
        }
        old_open_issue = connection.execute(
            "select lifecycle_state from behavior_memories where memory_id = 'm1'"
        ).fetchone()

        _insert_verified_source(connection, "source-2")
        _insert_issue_resolution(connection, "r1", "source-2")
        connection.execute(
            """
            insert or ignore into memory_issue_resolutions(
                resolution_id, project_id, source_agent, model_id,
                target_content_hash, target_memory_id, source_reference_id,
                status, resolved_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "r2",
                "p1",
                "codex",
                "gpt-5.6-sol",
                "a" * 64,
                "m1",
                "source-2",
                "resolved",
                "2026-07-16T00:00:00Z",
            ),
        )
        not_found_values = (
            "nf1",
            "p1",
            "codex",
            "gpt-5.6-sol",
            "b" * 64,
            None,
            "source-2",
            "not_found",
            "2026-07-16T00:00:00Z",
        )
        connection.execute(
            """
            insert or ignore into memory_issue_resolutions(
                resolution_id, project_id, source_agent, model_id,
                target_content_hash, target_memory_id, source_reference_id,
                status, resolved_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            not_found_values,
        )
        connection.execute(
            """
            insert or ignore into memory_issue_resolutions(
                resolution_id, project_id, source_agent, model_id,
                target_content_hash, target_memory_id, source_reference_id,
                status, resolved_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("nf2", *not_found_values[1:]),
        )
        status_counts = {
            row["status"]: row["count"]
            for row in connection.execute(
                """
                select status, count(*) as count
                from memory_issue_resolutions group by status
                """
            )
        }

    assert versions == tuple(range(1, 13))
    assert provenance == {
        "source-1": ("p1", "gpt-5.6-sol"),
        "ambiguous-project": (None, None),
        "ambiguous-model": (None, None),
        "ambiguous-agent": (None, None),
        "wrong-parser": (None, None),
        "persisted-path": (None, None),
    }
    assert old_open_issue is not None
    assert old_open_issue["lifecycle_state"] == "active"
    assert status_counts == {"not_found": 1, "resolved": 1}


def test_capture_provenance_requires_a_complete_pair_on_insert_and_update(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_verified_source(connection, "project-only", model_id=None)
        with pytest.raises(sqlite3.IntegrityError):
            _insert_verified_source(connection, "model-only", project_id=None)

        _insert_verified_source(
            connection,
            "legacy-ambiguous",
            project_id=None,
            model_id=None,
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                update source_refs set capture_project_id = 'p1'
                where source_reference_id = 'legacy-ambiguous'
                """
            )

        pair = connection.execute(
            """
            select capture_project_id, capture_model_id from source_refs
            where source_reference_id = 'legacy-ambiguous'
            """
        ).fetchone()

    assert pair is not None
    assert tuple(pair) == (None, None)


def test_issue_resolution_status_requires_matching_target_presence(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_verified_source(connection, "target-source")
        _insert_verified_source(connection, "audit-source")
        _insert_behavior_memory(connection, "m1", "target-source")

        with pytest.raises(sqlite3.IntegrityError):
            _insert_issue_resolution(
                connection,
                "resolved-without-target",
                "audit-source",
                target_memory_id=None,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_issue_resolution(
                connection,
                "not-found-with-target",
                "audit-source",
                target_memory_id="m1",
                status="not_found",
            )


def test_issue_resolution_rejects_null_resolution_id(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_verified_source(connection, "target-source")
        _insert_verified_source(connection, "audit-source")
        _insert_behavior_memory(connection, "m1", "target-source")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                insert into memory_issue_resolutions(
                    resolution_id, project_id, source_agent, model_id,
                    target_content_hash, target_memory_id, source_reference_id,
                    status, resolved_at
                ) values (null, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "p1",
                    "codex",
                    "gpt-5.6-sol",
                    "a" * 64,
                    "m1",
                    "audit-source",
                    "resolved",
                    "2026-07-16T00:00:00Z",
                ),
            )


@pytest.mark.parametrize(
    ("target_project_id", "target_source_agent", "target_model_id", "memory_kind"),
    (
        ("p2", "codex", "gpt-5.6-sol", "open_issue"),
        ("p1", "chatgpt", "gpt-5.6-sol", "open_issue"),
        ("p1", "codex", "gpt-5.6-other", "open_issue"),
        ("p1", "codex", "gpt-5.6-sol", "decision"),
    ),
    ids=("wrong-project", "wrong-source", "wrong-model", "not-open-issue"),
)
def test_issue_resolution_target_insert_rejects_wrong_namespace_or_kind(
    tmp_path: Path,
    target_project_id: str,
    target_source_agent: str,
    target_model_id: str,
    memory_kind: str,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_project(connection, "p2")
        _insert_verified_source(connection, "audit-source")
        _insert_verified_source(
            connection,
            "wrong-target-source",
            project_id=target_project_id,
            source_agent=target_source_agent,
            model_id=target_model_id,
        )
        _insert_behavior_memory(
            connection,
            "wrong-target",
            "wrong-target-source",
            project_id=target_project_id,
            source_agent=target_source_agent,
            model_id=target_model_id,
            memory_kind=memory_kind,
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="resolution target namespace mismatch",
        ):
            _insert_issue_resolution(
                connection,
                "r1",
                "audit-source",
                target_memory_id="wrong-target",
            )


def test_issue_resolution_target_update_revalidates_target_ownership(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_verified_source(connection, "target-source")
        _insert_verified_source(connection, "audit-source")
        _insert_behavior_memory(connection, "m1", "target-source")
        _insert_behavior_memory(
            connection,
            "wrong-target",
            "target-source",
            model_id="gpt-5.6-other",
        )
        _insert_issue_resolution(connection, "r1", "audit-source")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="resolution target namespace mismatch",
        ):
            connection.execute(
                """
                update memory_issue_resolutions set target_memory_id = 'wrong-target'
                where resolution_id = 'r1'
                """
            )


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("project_id", "p2"),
        ("source_agent", "chatgpt"),
        ("model_id", "gpt-5.6-other"),
        ("memory_kind", "decision"),
    ),
)
def test_target_memory_update_cannot_invalidate_existing_resolution(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_project(connection, "p2")
        _insert_verified_source(connection, "target-source")
        _insert_verified_source(connection, "audit-source")
        _insert_behavior_memory(connection, "m1", "target-source")
        _insert_issue_resolution(connection, "r1", "audit-source")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="resolution target namespace mismatch",
        ):
            connection.execute(
                f"update behavior_memories set {column} = ? where memory_id = 'm1'",
                (value,),
            )

        connection.execute(
            """
            update behavior_memories
            set project_id = project_id,
                source_agent = source_agent,
                model_id = model_id,
                memory_kind = memory_kind
            where memory_id = 'm1'
            """
        )
        row = connection.execute(
            """
            select project_id, source_agent, model_id, memory_kind
            from behavior_memories where memory_id = 'm1'
            """
        ).fetchone()

    assert row is not None
    assert tuple(row) == ("p1", "codex", "gpt-5.6-sol", "open_issue")


@pytest.mark.parametrize(
    ("capture_source_agent", "capture_project_id", "capture_model_id"),
    (
        ("chatgpt", "p1", "gpt-5.6-sol"),
        ("codex", "p2", "gpt-5.6-sol"),
        ("codex", "p1", "gpt-5.6-other"),
        ("codex", None, None),
    ),
    ids=("wrong-source", "wrong-project", "wrong-model", "ambiguous-legacy"),
)
def test_issue_resolution_source_insert_rejects_unowned_capture(
    tmp_path: Path,
    capture_source_agent: str,
    capture_project_id: str | None,
    capture_model_id: str | None,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_project(connection, "p2")
        _insert_verified_source(connection, "target-source")
        _insert_behavior_memory(connection, "m1", "target-source")
        _insert_verified_source(
            connection,
            "unowned-source",
            project_id=capture_project_id,
            source_agent=capture_source_agent,
            model_id=capture_model_id,
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="resolution source namespace mismatch",
        ):
            _insert_issue_resolution(connection, "r1", "unowned-source")


@pytest.mark.parametrize(
    ("capture_source_agent", "capture_project_id", "capture_model_id"),
    (
        ("chatgpt", "p1", "gpt-5.6-sol"),
        ("codex", "p2", "gpt-5.6-sol"),
        ("codex", "p1", "gpt-5.6-other"),
        ("codex", None, None),
    ),
    ids=("wrong-source", "wrong-project", "wrong-model", "ambiguous-legacy"),
)
def test_issue_resolution_source_update_rejects_unowned_capture(
    tmp_path: Path,
    capture_source_agent: str,
    capture_project_id: str | None,
    capture_model_id: str | None,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_project(connection, "p2")
        _insert_verified_source(connection, "target-source")
        _insert_verified_source(connection, "audit-source")
        _insert_verified_source(
            connection,
            "unowned-source",
            project_id=capture_project_id,
            source_agent=capture_source_agent,
            model_id=capture_model_id,
        )
        _insert_behavior_memory(connection, "m1", "target-source")
        _insert_issue_resolution(connection, "r1", "audit-source")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="resolution source namespace mismatch",
        ):
            connection.execute(
                """
                update memory_issue_resolutions
                set source_reference_id = 'unowned-source'
                where resolution_id = 'r1'
                """
            )


@pytest.mark.parametrize(
    ("statement", "parameters"),
    (
        (
            "update source_refs set source_agent = ? where source_reference_id = 'audit-source'",
            ("chatgpt",),
        ),
        (
            "update source_refs set capture_project_id = ? "
            "where source_reference_id = 'audit-source'",
            ("p2",),
        ),
        (
            "update source_refs set capture_model_id = ? "
            "where source_reference_id = 'audit-source'",
            ("gpt-5.6-other",),
        ),
        (
            "update source_refs set capture_project_id = null, capture_model_id = null "
            "where source_reference_id = 'audit-source'",
            (),
        ),
    ),
    ids=("wrong-source", "wrong-project", "wrong-model", "clear-provenance"),
)
def test_source_ref_update_cannot_invalidate_existing_resolution(
    tmp_path: Path,
    statement: str,
    parameters: tuple[object, ...],
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_project(connection, "p2")
        _insert_verified_source(connection, "target-source")
        _insert_verified_source(connection, "audit-source")
        _insert_behavior_memory(connection, "m1", "target-source")
        _insert_issue_resolution(connection, "r1", "audit-source")

        with pytest.raises(
            sqlite3.IntegrityError,
            match="resolution source namespace mismatch",
        ):
            connection.execute(statement, parameters)

        connection.execute(
            """
            update source_refs
            set source_agent = source_agent,
                capture_project_id = capture_project_id,
                capture_model_id = capture_model_id
            where source_reference_id = 'audit-source'
            """
        )
        row = connection.execute(
            """
            select source_agent, capture_project_id, capture_model_id
            from source_refs where source_reference_id = 'audit-source'
            """
        ).fetchone()

    assert row is not None
    assert tuple(row) == ("codex", "p1", "gpt-5.6-sol")


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("project_id", "p2"),
        ("source_agent", "chatgpt"),
        ("model_id", "gpt-5.6-other"),
    ),
)
def test_issue_resolution_source_update_revalidates_namespace_columns(
    tmp_path: Path,
    column: str,
    value: str,
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()

    with database.transaction() as connection:
        _insert_project(connection)
        _insert_project(connection, "p2")
        _insert_verified_source(connection, "audit-source")
        _insert_issue_resolution(
            connection,
            "nf1",
            "audit-source",
            target_content_hash="b" * 64,
            target_memory_id=None,
            status="not_found",
        )

        with pytest.raises(
            sqlite3.IntegrityError,
            match="resolution source namespace mismatch",
        ):
            connection.execute(
                f"update memory_issue_resolutions set {column} = ? where resolution_id = 'nf1'",
                (value,),
            )


def test_proposal_execution_migration_upgrades_v6_and_backfills_legacy_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database_module._load_migrations()
    v6_migrations = tuple(item for item in migrations if item[0] <= 6)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: v6_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into improvement_proposals(
                proposal_id, signature, title, description, patch, risk,
                verification_argv_json, verification_summary, approval_status,
                target_version, rollback_ref, created_at, approved_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000007",
                "legacy-signature",
                "Legacy title",
                "Legacy description",
                None,
                "low",
                "[]",
                "",
                "draft",
                None,
                None,
                "2026-07-14T00:00:00Z",
                None,
            ),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select * from improvement_proposals where signature = ?",
            ("legacy-signature",),
        ).fetchone()
        versions = tuple(
            value[0]
            for value in connection.execute(
                "select version from schema_migrations order by version"
            )
        )

    assert row is not None
    assert row["origin"] == "legacy"
    assert row["updated_at"] == row["created_at"] == "2026-07-14T00:00:00Z"
    assert all(
        row[name] is None
        for name in (
            "approval_actor",
            "apply_attempt_id",
            "repository_root",
            "original_branch",
            "base_commit",
            "proposal_branch",
            "applied_commit",
            "applied_at",
            "rolled_back_at",
            "failure_code",
        )
    )
    assert versions == tuple(range(1, 13))


def test_failed_proposal_execution_migration_is_atomic_from_v6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database_module._load_migrations()
    v6_migrations = tuple(item for item in migrations if item[0] <= 6)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: v6_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.connect(readonly=True) as connection:
        columns_before = tuple(
            row["name"] for row in connection.execute("pragma table_info(improvement_proposals)")
        )

    monkeypatch.setattr(
        database_module,
        "_load_migrations",
        lambda: (
            *v6_migrations,
            (
                7,
                "alter table improvement_proposals add column origin text; this is invalid sql;",
            ),
        ),
    )
    with pytest.raises(sqlite3.Error):
        database.initialize()

    with database.connect(readonly=True) as connection:
        columns_after = tuple(
            row["name"] for row in connection.execute("pragma table_info(improvement_proposals)")
        )
        versions = tuple(
            row[0]
            for row in connection.execute("select version from schema_migrations order by version")
        )
    assert columns_after == columns_before
    assert versions == (1, 2, 3, 4, 5, 6)


def test_readonly_snapshot_upgrades_v6_proposals_without_touching_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database_module._load_migrations()
    v6_migrations = tuple(item for item in migrations if item[0] <= 6)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: v6_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into improvement_proposals(
                proposal_id, signature, title, description, patch, risk,
                verification_argv_json, verification_summary, approval_status,
                created_at
            ) values (?, ?, ?, ?, null, 'low', '[]', '', 'draft', ?)
            """,
            (
                "00000000-0000-0000-0000-000000000077",
                "snapshot-v6",
                "Snapshot",
                "Upgrade only in memory",
                "2026-07-14T00:00:00Z",
            ),
        )
    before = _database_physical_state(database.path)

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    snapshot = ReadonlyDatabaseSnapshot(database.path)
    try:
        with snapshot.connect(readonly=True) as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "select version from schema_migrations order by version"
                )
            )
            columns = tuple(
                row["name"]
                for row in connection.execute("pragma table_info(improvement_proposals)")
            )
            row = connection.execute(
                "select origin, updated_at from improvement_proposals "
                "where signature = 'snapshot-v6'"
            ).fetchone()
    finally:
        snapshot.close()

    assert versions == tuple(range(1, 13))
    assert "origin" in columns
    assert tuple(row) == ("legacy", "2026-07-14T00:00:00Z")
    assert _database_physical_state(database.path) == before


def test_strict_observation_epoch_migration_backfills_offsets_and_isolates_invalid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "memory.db")
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 4)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database.initialize()
    values = {
        "utc": "2026-06-22T04:00:00Z",
        "offset": "2026-06-22T12:00:00+08:00",
        "invalid": "2026-02-30T12:00:00+00:00",
        "naive": "2026-06-22T04:00:00",
        "unknown": None,
    }
    with database.transaction() as connection:
        for project_id, timestamp in values.items():
            connection.execute(
                """
                insert into projects(
                    project_id, canonical_path, display_name,
                    last_observed_change
                ) values (?, ?, ?, ?)
                """,
                (project_id, f"/tmp/{project_id}", project_id, timestamp),
            )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    database.initialize()

    with database.transaction() as connection:
        rows = {
            row["project_id"]: row["last_observed_change_epoch_us"]
            for row in connection.execute(
                """
                select project_id, last_observed_change_epoch_us
                from projects order by project_id
                """
            ).fetchall()
        }
        versions = [
            row[0]
            for row in connection.execute("select version from schema_migrations order by version")
        ]
        indexes = {row["name"] for row in connection.execute("pragma index_list(projects)")}
        triggers = {
            row["name"]
            for row in connection.execute("select name from sqlite_master where type = 'trigger'")
        }
        connection.execute(
            "update projects set last_observed_change = ? where project_id = 'invalid'",
            (values["utc"],),
        )
        repaired = connection.execute(
            """
            select last_observed_change_epoch_us from projects
            where project_id = 'invalid'
            """
        ).fetchone()[0]

    observed = datetime(2026, 6, 22, 4, tzinfo=timezone.utc)
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = observed - epoch
    expected_epoch_us = delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds
    assert rows == {
        "invalid": None,
        "naive": None,
        "offset": expected_epoch_us,
        "unknown": None,
        "utc": expected_epoch_us,
    }
    assert repaired == expected_epoch_us
    assert versions == list(range(1, 13))
    assert "idx_projects_active_observed_epoch" in indexes
    assert "idx_projects_active_observed_julianday" not in indexes
    assert {
        "projects_observed_epoch_ai",
        "projects_observed_epoch_au",
    } <= triggers


def test_readonly_snapshot_migration_failure_discards_memory_and_preserves_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 4)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        _insert_project(connection)

    def contract() -> tuple[object, ...]:
        with database.connect(readonly=True) as connection:
            return (
                tuple(
                    row[0]
                    for row in connection.execute(
                        "select version from schema_migrations order by version"
                    )
                ),
                tuple(row["name"] for row in connection.execute("pragma table_info(projects)")),
                tuple(tuple(row) for row in connection.execute("select * from projects")),
            )

    before_contract = contract()
    before_physical = _database_physical_state(database.path)
    failing_migrations = (
        *legacy_migrations,
        (
            5,
            "alter table projects add column last_observed_change_epoch_us integer; "
            "this is invalid sql;",
        ),
    )
    monkeypatch.setattr(
        database_module,
        "_load_migrations",
        lambda: failing_migrations,
    )

    def open_snapshot() -> None:
        snapshot = ReadonlyDatabaseSnapshot(database.path)
        snapshot.close()

    with pytest.raises(RuntimeError, match="snapshot migration failed"):
        open_snapshot()

    assert contract() == before_contract
    assert before_contract[0] == (1, 2, 3, 4)
    assert _database_physical_state(database.path) == before_physical


def test_readonly_snapshot_does_not_reapply_current_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    migrations = database_module._load_migrations()
    poisoned = tuple(
        (version, "drop table projects;") if version == 5 else (version, sql)
        for version, sql in migrations
    )
    monkeypatch.setattr(database_module, "_load_migrations", lambda: poisoned)
    before_physical = _database_physical_state(database.path)

    snapshot = ReadonlyDatabaseSnapshot(database.path)
    try:
        with snapshot.connect(readonly=True) as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "select version from schema_migrations order by version"
                )
            )
            columns = tuple(
                row["name"] for row in connection.execute("pragma table_info(projects)")
            )
    finally:
        snapshot.close()

    assert versions == tuple(range(1, 13))
    assert "last_observed_change_epoch_us" in columns
    assert _database_physical_state(database.path) == before_physical


def test_readonly_snapshot_keeps_migration_temp_storage_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 4)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)

    snapshot = ReadonlyDatabaseSnapshot(database.path)
    try:
        with snapshot.connect(readonly=True) as connection:
            temp_store = connection.execute("pragma temp_store").fetchone()[0]
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "select version from schema_migrations order by version"
                )
            )
    finally:
        snapshot.close()

    assert temp_store == 2
    assert versions == tuple(range(1, 13))


def test_migration_is_applied_only_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    migrations = database_module._load_migrations()
    monkeypatch.setattr(
        database_module,
        "_load_migrations",
        lambda: (*migrations, (13, "create table migration_probe(value text);")),
    )

    db.initialize()
    db.initialize()

    with db.connect() as conn:
        assert (
            conn.execute("select count(*) from schema_migrations where version=13").fetchone()[0]
            == 1
        )


def test_migration_accepts_multiple_statements_on_one_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    migrations = database_module._load_migrations()
    monkeypatch.setattr(
        database_module,
        "_load_migrations",
        lambda: (
            *migrations,
            (
                13,
                "create table compact_one(value text); create table compact_two(value text);",
            ),
        ),
    )

    db.initialize()

    with db.connect() as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "select name from sqlite_master where name in ('compact_one', 'compact_two')"
            )
        }
        version_count = conn.execute(
            "select count(*) from schema_migrations where version=13"
        ).fetchone()[0]
    assert tables == {"compact_one", "compact_two"}
    assert version_count == 1


def test_initialize_rejects_future_migration_before_applying_known_updates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migrations = database_module._load_migrations()
    legacy_migrations = tuple(item for item in migrations if item[0] <= 7)
    monkeypatch.setattr(database_module, "_load_migrations", lambda: legacy_migrations)
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "insert into schema_migrations(version, applied_at) values (999, ?)",
            ("2026-07-16T00:00:00Z",),
        )

    monkeypatch.setattr(database_module, "_load_migrations", lambda: migrations)
    with pytest.raises(RuntimeError, match="migration history is incompatible"):
        database.initialize()

    with database.connect(readonly=True) as connection:
        versions = tuple(
            row[0]
            for row in connection.execute("select version from schema_migrations order by version")
        )
        project_columns = tuple(
            row["name"] for row in connection.execute("pragma table_info(projects)")
        )
    assert versions == (1, 2, 3, 4, 5, 6, 7, 999)
    assert "path_device" not in project_columns


def test_initialize_rejects_gapped_migration_history(tmp_path: Path) -> None:
    database = Database(tmp_path / "memory.db")
    database.initialize()
    with database.transaction() as connection:
        connection.execute("delete from schema_migrations where version = 4")

    with pytest.raises(RuntimeError, match="migration history is incompatible"):
        database.initialize()


def test_initialize_requires_existing_directory_without_chmodding_it(
    tmp_path: Path,
) -> None:
    missing_parent = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        Database(missing_parent / "memory.db").initialize()
    assert not missing_parent.exists()

    non_directory = tmp_path / "not-a-directory"
    non_directory.write_text("file", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        Database(non_directory / "memory.db").initialize()

    parent = tmp_path / "runtime"
    parent.mkdir()
    parent.chmod(0o755)
    Database(parent / "memory.db").initialize()
    assert parent.stat().st_mode & 0o777 == 0o755


def test_backup_uses_live_database_and_is_private(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    destination = tmp_path / "backups" / "memory.db"
    destination.parent.mkdir()

    with db.connect() as conn:
        _insert_project(conn)
        conn.commit()
        assert Path(f"{db.path}-wal").exists()
        result = db.backup(destination)

    assert result == destination
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as backup_conn:
        assert backup_conn.execute("select count(*) from projects").fetchone()[0] == 1


def test_backup_rejects_an_existing_destination(tmp_path: Path) -> None:
    db = Database(tmp_path / "memory.db")
    db.initialize()
    destination = tmp_path / "existing.db"
    destination.write_bytes(b"existing")

    with pytest.raises(FileExistsError):
        db.backup(destination)
    assert destination.read_bytes() == b"existing"
