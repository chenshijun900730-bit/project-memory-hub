from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    LifecycleState,
    MemoryKind,
    Namespace,
    ProjectCandidate,
    ProjectFactInput,
    SourceAgent,
)
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.projects import ProjectRepository


class TracingDatabase:
    def __init__(self, path: Path) -> None:
        self._database = Database(path)
        self.path = self._database.path
        self.traces: list[str] = []

    def initialize(self) -> None:
        self._database.initialize()

    @contextmanager
    def connect(self, readonly: bool = False):
        with self._database.connect(readonly=readonly) as connection:
            connection.set_trace_callback(self.traces.append)
            yield connection

    @contextmanager
    def transaction(self):
        with self.connect() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise


@pytest.fixture
def database(tmp_path: Path) -> TracingDatabase:
    database = TracingDatabase(tmp_path / "memory.db")
    database.initialize()
    return database


@pytest.fixture
def project_ids(database: TracingDatabase, tmp_path: Path) -> tuple[UUID, UUID]:
    repository = ProjectRepository(database)
    roots = (tmp_path / "project-one", tmp_path / "project-two")
    for root in roots:
        root.mkdir()
    return tuple(
        repository.register(
            ProjectCandidate(canonical_path=root, display_name=root.name)
        ).project_id
        for root in roots
    )


def _source_ref(
    database: TracingDatabase,
    *,
    source_agent: SourceAgent,
    source_record_id: str,
) -> UUID:
    source_reference_id = uuid4()
    now = datetime.now(timezone.utc).isoformat()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(source_reference_id),
                source_agent.value,
                source_record_id,
                None,
                hashlib.sha256(source_record_id.encode()).hexdigest(),
                now,
                "test-v1",
                now,
            ),
        )
    return source_reference_id


def _memory(
    database: TracingDatabase,
    project_id: UUID,
    source_agent: SourceAgent,
    model_id: str,
    content: str,
    *,
    source_record_id: str,
    created_at: datetime | None = None,
) -> BehaviorMemoryInput:
    return BehaviorMemoryInput(
        project_id=project_id,
        namespace=Namespace(source_agent=source_agent, model_id=model_id),
        task_fingerprint=hashlib.sha256(source_record_id.encode()).hexdigest(),
        memory_kind=MemoryKind.VERIFIED_METHOD,
        normalized_content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        source_reference_id=_source_ref(
            database,
            source_agent=source_agent,
            source_record_id=source_record_id,
        ),
        created_at=created_at or datetime.now(timezone.utc),
        confidence=1.0,
    )


def test_behavior_search_never_crosses_namespace(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, other_project_id = project_ids
    repository = MemoryRepository(database)
    repository.insert(
        _memory(
            database,
            project_id,
            SourceAgent.CODEX,
            "gpt-5.6-sol",
            "run uv test",
            source_record_id="codex-1",
        )
    )
    repository.insert(
        _memory(
            database,
            project_id,
            SourceAgent.CHATGPT,
            "gpt-5",
            "run npm test",
            source_record_id="chatgpt-1",
        )
    )
    repository.insert(
        _memory(
            database,
            project_id,
            SourceAgent.CODEX,
            "gpt-5.7",
            "run cargo test",
            source_record_id="codex-2",
        )
    )
    repository.insert(
        _memory(
            database,
            other_project_id,
            SourceAgent.CODEX,
            "gpt-5.6-sol",
            "run leaked test",
            source_record_id="codex-3",
        )
    )

    database.traces.clear()
    rows = repository.search(
        project_id,
        Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
        "test",
        limit=20,
    )

    assert [row.normalized_content for row in rows] == ["run uv test"]
    scoped_selects = [
        statement.casefold()
        for statement in database.traces
        if "from behavior_memories" in statement.casefold()
    ]
    assert len(scoped_selects) == 1
    statement = scoped_selects[0]
    assert "where project_id =" in statement
    assert "and source_agent =" in statement
    assert "and model_id =" in statement
    assert "and lifecycle_state = 'active'" in statement
    assert "order by created_at desc, memory_id" in statement
    assert "limit 1000" in statement
    assert str(project_id).lower() in statement
    assert "gpt-5.6-sol" in statement
    assert "codex" in statement
    assert "match" not in statement

    with database.connect(readonly=True) as connection:
        behavior_fts = connection.execute(
            """
            select name from sqlite_master
            where type = 'table' and name like '%behavior%fts%'
            """
        ).fetchall()
    assert behavior_fts == []


def test_behavior_insert_is_idempotent_and_validates_source_and_hash(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = MemoryRepository(database)
    item = _memory(
        database,
        project_id,
        SourceAgent.CODEX,
        "gpt-5.6-sol",
        "run uv test",
        source_record_id="codex-idempotent",
    )

    inserted = repository.insert(item)
    duplicate = repository.insert(item)

    assert inserted.inserted is True
    assert inserted.duplicate is False
    assert inserted.record_id is not None
    assert duplicate.inserted is False
    assert duplicate.duplicate is True
    assert duplicate.record_id == inserted.record_id

    with pytest.raises(ValueError, match="content_hash"):
        repository.insert(item.model_copy(update={"content_hash": "0" * 64}))
    with pytest.raises(KeyError):
        repository.insert(
            item.model_copy(
                update={
                    "source_reference_id": uuid4(),
                    "task_fingerprint": hashlib.sha256(b"other").hexdigest(),
                }
            )
        )


@pytest.mark.parametrize("limit", [0, 101])
def test_memory_search_rejects_out_of_range_limits(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
    limit: int,
) -> None:
    project_id, _ = project_ids
    with pytest.raises(ValueError, match="limit"):
        MemoryRepository(database).search(
            project_id,
            Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
            "",
            limit,
        )


def test_blank_behavior_query_returns_newest_with_deterministic_ties(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = MemoryRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    older = _memory(
        database,
        project_id,
        SourceAgent.CODEX,
        "gpt-5.6-sol",
        "older method",
        source_record_id="older",
        created_at=base,
    )
    newer = _memory(
        database,
        project_id,
        SourceAgent.CODEX,
        "gpt-5.6-sol",
        "newer method",
        source_record_id="newer",
        created_at=base + timedelta(seconds=1),
    )
    repository.insert(older)
    repository.insert(newer)

    rows = repository.search(
        project_id,
        Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
        "   ",
        limit=20,
    )

    assert [row.normalized_content for row in rows] == [
        "newer method",
        "older method",
    ]


def test_behavior_search_ranks_oldest_relevant_chinese_within_bounded_window(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = MemoryRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    repository.insert(
        _memory(
            database,
            project_id,
            SourceAgent.CODEX,
            "gpt-5.6-sol",
            "缓存命令应该被召回",
            source_record_id="oldest-relevant",
            created_at=base,
        )
    )
    for index in range(100):
        repository.insert(
            _memory(
                database,
                project_id,
                SourceAgent.CODEX,
                "gpt-5.6-sol",
                f"newer unrelated item {index:03d}",
                source_record_id=f"newer-{index:03d}",
                created_at=base + timedelta(seconds=index + 1),
            )
        )

    rows = repository.search(
        project_id,
        Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
        "缓存命令",
        limit=20,
    )

    assert [row.normalized_content for row in rows] == ["缓存命令应该被召回"]


def test_fact_search_is_project_scoped_and_neutralizes_raw_fts(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, other_project_id = project_ids
    repository = FactRepository(database)
    observed_at = datetime.now(timezone.utc)
    repository.observe(
        project_id,
        ProjectFactInput(
            category="manifest",
            normalized_content="alpha shared needle",
            evidence_type="manifest_metadata",
            evidence_reference="package.json",
            observed_at=observed_at,
            confidence=1.0,
        ),
    )
    repository.observe(
        other_project_id,
        ProjectFactInput(
            category="manifest",
            normalized_content="beta shared needle",
            evidence_type="manifest_metadata",
            evidence_reference="package.json",
            observed_at=observed_at,
            confidence=1.0,
        ),
    )

    rows = repository.search(project_id, "shared needle", limit=20)
    hostile_rows = repository.search(project_id, '" OR * NOT', limit=20)

    assert [row.normalized_content for row in rows] == ["alpha shared needle"]
    assert hostile_rows == []


def test_newer_fact_stales_but_does_not_delete_conflicting_observation(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    old = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="main",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base,
            confidence=1.0,
        ),
    )
    new = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="feature",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base + timedelta(microseconds=500_000),
            confidence=1.0,
        ),
    )
    duplicate = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="feature",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base + timedelta(microseconds=500_000),
            confidence=1.0,
        ),
    )

    assert duplicate.fact_id == new.fact_id
    assert [row.fact_id for row in repository.search(project_id, "", 20)] == [new.fact_id]
    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select fact_id, lifecycle_state, stale_at, supersedes_fact_id
            from project_facts where project_id = ? order by observed_at, fact_id
            """,
            (str(project_id),),
        ).fetchall()
    assert len(rows) == 2
    old_row = next(row for row in rows if row["fact_id"] == str(old.fact_id))
    new_row = next(row for row in rows if row["fact_id"] == str(new.fact_id))
    assert old_row["lifecycle_state"] == LifecycleState.COLD.value
    assert old_row["stale_at"] is not None
    assert new_row["supersedes_fact_id"] == str(old.fact_id)


def test_newer_reobservation_restores_a_previously_stale_exact_fact(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def branch(content: str, observed_at: datetime) -> ProjectFactInput:
        return ProjectFactInput(
            category="git_branch",
            normalized_content=content,
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=observed_at,
            confidence=1.0,
        )

    original = repository.observe(project_id, branch("main", base))
    feature = repository.observe(project_id, branch("feature", base + timedelta(seconds=1)))
    restored = repository.observe(project_id, branch("main", base + timedelta(seconds=2)))

    assert restored.fact_id == original.fact_id
    assert [row.fact_id for row in repository.search(project_id, "", 20)] == [original.fact_id]
    with database.connect(readonly=True) as connection:
        feature_row = connection.execute(
            "select lifecycle_state, stale_at from project_facts where fact_id = ?",
            (str(feature.fact_id),),
        ).fetchone()
    assert feature_row["lifecycle_state"] == LifecycleState.COLD.value
    assert feature_row["stale_at"] is not None


def test_late_older_conflict_is_stored_cold_without_changing_current_fact(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="current",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base + timedelta(seconds=2),
            confidence=1.0,
        ),
    )

    late = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="late-old",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base,
            confidence=1.0,
        ),
    )

    assert [row.fact_id for row in repository.search(project_id, "", 20)] == [current.fact_id]
    with database.connect(readonly=True) as connection:
        late_row = connection.execute(
            "select lifecycle_state, stale_at from project_facts where fact_id = ?",
            (str(late.fact_id),),
        ).fetchone()
    assert late_row["lifecycle_state"] == LifecycleState.COLD.value
    assert late_row["stale_at"] is not None


def test_equal_time_conflict_is_stored_cold_and_first_observation_stays_active(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    observed_at = datetime(2026, 1, 1, tzinfo=timezone.utc)

    first = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="first",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=observed_at,
            confidence=1.0,
        ),
    )
    second = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="second",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=observed_at,
            confidence=1.0,
        ),
    )

    assert [row.fact_id for row in repository.search(project_id, "", 20)] == [first.fact_id]
    with database.connect(readonly=True) as connection:
        second_row = connection.execute(
            "select lifecycle_state, stale_at from project_facts where fact_id = ?",
            (str(second.fact_id),),
        ).fetchone()
    assert second_row["lifecycle_state"] == LifecycleState.COLD.value
    assert second_row["stale_at"] is not None


def test_newer_exact_active_reobservation_updates_time_and_stales_legacy_conflict(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="current",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base,
            confidence=0.8,
        ),
    )
    legacy_conflict_id = str(uuid4()).lower()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into project_facts(
                fact_id, project_id, category, normalized_content, evidence_type,
                evidence_reference, observed_at, confidence, supersedes_fact_id,
                stale_at, lifecycle_state, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, null, null, 'active', ?)
            """,
            (
                legacy_conflict_id,
                str(project_id),
                "git_branch",
                "legacy-conflict",
                "git_metadata",
                "git:branch",
                (base - timedelta(seconds=1)).isoformat(),
                1.0,
                base.isoformat(),
            ),
        )

    records, stale_count = repository._observe_many(
        project_id,
        (
            ProjectFactInput(
                category="git_branch",
                normalized_content="current",
                evidence_type="git_metadata",
                evidence_reference="git:branch",
                observed_at=base + timedelta(seconds=2),
                confidence=1.0,
            ),
        ),
    )

    assert records[0].fact_id == current.fact_id
    assert records[0].observed_at == base + timedelta(seconds=2)
    assert records[0].confidence == 1.0
    assert stale_count == 1
    assert [row.fact_id for row in repository.search(project_id, "", 20)] == [current.fact_id]
    with database.connect(readonly=True) as connection:
        legacy = connection.execute(
            "select lifecycle_state, stale_at from project_facts where fact_id = ?",
            (legacy_conflict_id,),
        ).fetchone()
    assert legacy["lifecycle_state"] == LifecycleState.COLD.value
    assert legacy["stale_at"] is not None


def test_older_exact_replay_never_demotes_or_regresses_current_fact(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    current = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="current",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base + timedelta(seconds=2),
            confidence=1.0,
        ),
    )

    replay = repository.observe(
        project_id,
        ProjectFactInput(
            category="git_branch",
            normalized_content="current",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=base,
            confidence=0.1,
        ),
    )

    assert replay.fact_id == current.fact_id
    assert replay.lifecycle_state == LifecycleState.ACTIVE
    assert replay.observed_at == base + timedelta(seconds=2)
    assert replay.confidence == 1.0
    assert [row.fact_id for row in repository.search(project_id, "", 20)] == [current.fact_id]


def test_same_content_with_different_evidence_type_remains_corroborating(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    first = repository.observe(
        project_id,
        ProjectFactInput(
            category="manifest",
            normalized_content="shared-package",
            evidence_type="manifest_metadata",
            evidence_reference="package.json",
            observed_at=base,
            confidence=1.0,
        ),
    )
    second = repository.observe(
        project_id,
        ProjectFactInput(
            category="manifest",
            normalized_content="shared-package",
            evidence_type="corroborating_metadata",
            evidence_reference="package.json",
            observed_at=base + timedelta(seconds=1),
            confidence=0.9,
        ),
    )

    active_ids = {row.fact_id for row in repository.search(project_id, "", 20)}
    assert active_ids == {first.fact_id, second.fact_id}
    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select lifecycle_state, stale_at from project_facts
            where fact_id in (?, ?) order by fact_id
            """,
            (str(first.fact_id), str(second.fact_id)),
        ).fetchall()
    assert [(row["lifecycle_state"], row["stale_at"]) for row in rows] == [
        (LifecycleState.ACTIVE.value, None),
        (LifecycleState.ACTIVE.value, None),
    ]


@pytest.mark.parametrize("arrival_offset", [0, 2], ids=["older", "equal"])
def test_older_or_equal_conflict_preserves_all_winning_content_corroborators(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
    arrival_offset: int,
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def observation(content: str, evidence_type: str, offset: int) -> ProjectFactInput:
        return ProjectFactInput(
            category="manifest",
            normalized_content=content,
            evidence_type=evidence_type,
            evidence_reference="package.json",
            observed_at=base + timedelta(seconds=offset),
            confidence=1.0,
        )

    first = repository.observe(project_id, observation("winning-package", "manifest_metadata", 1))
    second = repository.observe(
        project_id, observation("winning-package", "corroborating_metadata", 2)
    )

    records, stale_count = repository._observe_many(
        project_id,
        (observation("late-conflict", "conflicting_metadata", arrival_offset),),
    )

    assert stale_count == 0
    assert {row.fact_id for row in repository.search(project_id, "", 20)} == {
        first.fact_id,
        second.fact_id,
    }
    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select fact_id, lifecycle_state, stale_at from project_facts
            where fact_id in (?, ?, ?) order by fact_id
            """,
            (str(first.fact_id), str(second.fact_id), str(records[0].fact_id)),
        ).fetchall()
    states = {row["fact_id"]: (row["lifecycle_state"], row["stale_at"]) for row in rows}
    assert states[str(first.fact_id)] == (LifecycleState.ACTIVE.value, None)
    assert states[str(second.fact_id)] == (LifecycleState.ACTIVE.value, None)
    assert states[str(records[0].fact_id)][0] == LifecycleState.COLD.value
    assert states[str(records[0].fact_id)][1] is not None


def test_older_exact_replay_uses_stored_time_across_legacy_active_conflict(
    database: TracingDatabase,
    project_ids: tuple[UUID, UUID],
) -> None:
    project_id, _ = project_ids
    repository = FactRepository(database)
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def current_fact(observed_at: datetime, confidence: float) -> ProjectFactInput:
        return ProjectFactInput(
            category="git_branch",
            normalized_content="current",
            evidence_type="git_metadata",
            evidence_reference="git:branch",
            observed_at=observed_at,
            confidence=confidence,
        )

    current = repository.observe(project_id, current_fact(base + timedelta(seconds=3), 0.8))
    legacy_id = str(uuid4()).lower()
    with database.transaction() as connection:
        connection.execute(
            """
            insert into project_facts(
                fact_id, project_id, category, normalized_content, evidence_type,
                evidence_reference, observed_at, confidence, supersedes_fact_id,
                stale_at, lifecycle_state, created_at
            ) values (?, ?, ?, ?, ?, ?, ?, ?, null, null, 'active', ?)
            """,
            (
                legacy_id,
                str(project_id),
                "git_branch",
                "legacy",
                "legacy_metadata",
                "git:branch",
                (base + timedelta(seconds=2)).isoformat(),
                1.0,
                base.isoformat(),
            ),
        )

    records, stale_count = repository._observe_many(project_id, (current_fact(base, 0.1),))

    assert records[0].fact_id == current.fact_id
    assert records[0].lifecycle_state == LifecycleState.ACTIVE
    assert records[0].observed_at == base + timedelta(seconds=3)
    assert records[0].confidence == 0.8
    assert stale_count == 1
    assert [row.fact_id for row in repository.search(project_id, "", 20)] == [current.fact_id]
    with database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select fact_id, lifecycle_state, stale_at from project_facts
            where fact_id in (?, ?) order by fact_id
            """,
            (str(current.fact_id), legacy_id),
        ).fetchall()
    states = {row["fact_id"]: (row["lifecycle_state"], row["stale_at"]) for row in rows}
    assert states[str(current.fact_id)] == (LifecycleState.ACTIVE.value, None)
    assert states[legacy_id][0] == LifecycleState.COLD.value
    assert states[legacy_id][1] is not None
