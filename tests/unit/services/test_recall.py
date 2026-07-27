from __future__ import annotations

import hashlib
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

import project_memory_hub.storage.path_identity as path_identity_module
import project_memory_hub.storage.projects as projects_module
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    CapturePayload,
    MemoryKind,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    ProjectFactInput,
    RecallRequest,
    SourceAgent,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.recall import (
    RecallService,
    _Candidate,
    _fit_mandatory,
    _render,
)
from project_memory_hub.services.tokens import TokenCounterRegistry
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository


FIXED_TIME = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)
CODEX_NAMESPACE = Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-local")
FOREIGN_NAMESPACE = Namespace(source_agent=SourceAgent.CHATGPT, model_id="gpt-local")


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


class RecordingMemoryRepository:
    def __init__(self, repository: MemoryRepository) -> None:
        self._repository = repository
        self.calls: list[tuple[UUID, Namespace, str, int]] = []
        self.loaded_contents: list[str] = []

    def search(
        self,
        project_id: UUID,
        namespace: Namespace,
        query: str,
        limit: int,
    ):
        self.calls.append((project_id, namespace, query, limit))
        rows = self._repository.search(project_id, namespace, query, limit)
        self.loaded_contents.extend(row.normalized_content for row in rows)
        return rows


class CharacterCounter:
    def count(self, text: str) -> int:
        return len(text)


class RaisingCounter:
    def count(self, text: str) -> int:
        raise RuntimeError("local counter unavailable")


class InvalidCounter:
    def count(self, text: str) -> int:
        return -1


class NonzeroEmptyCounter:
    def count(self, text: str) -> int:
        return 10_000 if not text else len(text)


class FailsOnContentCounter:
    def count(self, text: str) -> int:
        if text:
            raise RuntimeError("cannot count content")
        return 0


class LaterNonzeroEmptyCounter:
    def __init__(self) -> None:
        self.empty_calls = 0

    def count(self, text: str) -> int:
        if text:
            return len(text)
        self.empty_calls += 1
        return 0 if self.empty_calls == 1 else 10_000


class FailsAfterFirstNonemptyCounter:
    def __init__(self) -> None:
        self.nonempty_calls = 0

    def count(self, text: str) -> int:
        if not text:
            return 0
        self.nonempty_calls += 1
        if self.nonempty_calls == 1:
            return 1
        raise RuntimeError("exact counter became unavailable")


class CallBudgetCharacterCounter:
    def __init__(self, max_calls: int) -> None:
        self.max_calls = max_calls
        self.calls = 0

    def count(self, text: str) -> int:
        self.calls += 1
        if self.calls > self.max_calls:
            raise AssertionError("mandatory fitting exceeded count-call budget")
        return len(text)


@pytest.fixture
def recall_context(tmp_path: Path):
    database = TracingDatabase(tmp_path / "memory.db")
    database.initialize()
    root = tmp_path / "project"
    nested = root / "src"
    nested.mkdir(parents=True)
    projects = ProjectRepository(database)
    project = projects.register(ProjectCandidate(canonical_path=root, display_name="Synthetic"))
    facts = FactRepository(database)
    raw_memories = MemoryRepository(database)
    memories = RecordingMemoryRepository(raw_memories)
    service = RecallService(
        projects,
        facts,
        memories,
        TokenCounterRegistry(),
    )
    return database, root, nested, project, facts, raw_memories, memories, service


def _source_ref(
    database: TracingDatabase,
    namespace: Namespace,
    source_record_id: str,
) -> UUID:
    source_reference_id = uuid4()
    timestamp = FIXED_TIME.isoformat().replace("+00:00", "Z")
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
                namespace.source_agent.value,
                source_record_id,
                None,
                hashlib.sha256(source_record_id.encode()).hexdigest(),
                timestamp,
                "test-v1",
                timestamp,
            ),
        )
    return source_reference_id


def _insert_memory(
    database: TracingDatabase,
    repository: MemoryRepository,
    project_id: UUID,
    namespace: Namespace,
    kind: MemoryKind,
    content: str,
    source_record_id: str,
    *,
    confidence: float = 1.0,
    created_at: datetime = FIXED_TIME,
) -> UUID:
    normalized = content.strip()
    result = repository.insert(
        BehaviorMemoryInput(
            project_id=project_id,
            namespace=namespace,
            task_fingerprint=hashlib.sha256(source_record_id.encode()).hexdigest(),
            memory_kind=kind,
            normalized_content=normalized,
            content_hash=hashlib.sha256(normalized.encode()).hexdigest(),
            source_reference_id=_source_ref(database, namespace, source_record_id),
            created_at=created_at,
            confidence=confidence,
        )
    )
    assert result.record_id is not None
    return result.record_id


def _observe_fact(
    repository: FactRepository,
    project_id: UUID,
    category: str,
    content: str,
    reference: str,
) -> UUID:
    return repository.observe(
        project_id,
        ProjectFactInput(
            category=category,
            normalized_content=content,
            evidence_type="deterministic_scan",
            evidence_reference=reference,
            observed_at=FIXED_TIME,
            confidence=1.0,
        ),
    ).fact_id


def _request(root: Path, task: str, max_tokens: int = 800) -> RecallRequest:
    return RecallRequest(
        cwd=root,
        task=task,
        namespace=CODEX_NAMESPACE,
        max_tokens=max_tokens,
    )


def test_recall_is_hard_scoped_deterministic_and_within_budget(
    recall_context,
) -> None:
    database, root, nested, project, facts, raw, recording, service = recall_context
    branch_id = _observe_fact(facts, project.project_id, "git_branch", "main", "git:branch")
    method_id = _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.VERIFIED_METHOD,
        "uv run pytest tests/cache",
        "codex-method",
    )
    issue_id = _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OPEN_ISSUE,
        "cache race remains",
        "codex-issue",
    )
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.REUSABLE_LESSON,
        "low priority background " + ("detail " * 300),
        "codex-background",
        confidence=0.1,
    )
    _insert_memory(
        database,
        raw,
        project.project_id,
        FOREIGN_NAMESPACE,
        MemoryKind.VERIFIED_METHOD,
        "npm test cache foreign model",
        "chatgpt-method",
    )

    request = _request(nested, "fix cache and run pytest", max_tokens=128)
    first = service.recall(request)
    second = service.recall(request)

    assert first == second
    assert first.estimated_tokens <= request.max_tokens
    assert "Current state" in first.text
    assert "main" in first.text
    assert "Verified methods" in first.text
    assert "uv run pytest tests/cache" in first.text
    assert "Open issues" in first.text
    assert "cache race remains" in first.text
    assert "low priority background" not in first.text
    assert "npm test" not in first.text
    assert first.selected_ids == (branch_id, method_id, issue_id)
    assert first.omitted_count == 1
    assert all(call[0] == project.project_id for call in recording.calls)
    assert all(call[1] == CODEX_NAMESPACE for call in recording.calls)
    assert all(1 <= call[3] <= 100 for call in recording.calls)
    assert "npm test cache foreign model" not in recording.loaded_contents
    scoped_sql = [
        statement.casefold()
        for statement in database.traces
        if "from behavior_memories" in statement.casefold()
    ]
    assert scoped_sql
    assert all("where project_id =" in statement for statement in scoped_sql)
    assert all("and source_agent =" in statement for statement in scoped_sql)
    assert all("and model_id =" in statement for statement in scoped_sql)


def test_recall_omits_resolved_target_but_retains_unrelated_active_issue(
    recall_context,
) -> None:
    database, root, _, project, _, raw, _, service = recall_context
    target_id = _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OPEN_ISSUE,
        "resolved cache target issue",
        "recall-resolution-target",
    )
    unrelated_id = _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OPEN_ISSUE,
        "unrelated active cache issue",
        "recall-resolution-unrelated",
    )
    payload = CapturePayload(
        cwd=root,
        namespace=CODEX_NAMESPACE,
        source_record_id="recall-explicit-resolution",
        objective="",
        outcome="",
        resolved_open_issues=["resolved cache target issue"],
    )
    verification = NamespaceVerification(
        namespace=CODEX_NAMESPACE,
        source_record_id=payload.source_record_id,
        verified_by="codex_adapter",
        verified_at=FIXED_TIME + timedelta(seconds=1),
    )
    capture = CaptureService(
        database,
        ProjectRepository(database),
        raw,
        Redactor(),
        now=lambda: FIXED_TIME + timedelta(seconds=2),
    )

    resolution = capture.capture(payload, verification)
    brief = service.recall(_request(root, "cache issue", max_tokens=256))

    assert resolution.status == "resolved"
    assert resolution.resolved_count == 1
    assert "resolved cache target issue" not in brief.text
    assert "unrelated active cache issue" in brief.text
    assert target_id not in brief.selected_ids
    assert unrelated_id in brief.selected_ids


def test_low_value_background_is_removed_before_mandatory_sections(
    recall_context,
) -> None:
    database, root, _, project, facts, raw, _, service = recall_context
    _observe_fact(facts, project.project_id, "git_dirty", "false", "git:dirty")
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.VERIFIED_METHOD,
        "run pytest cache/test_store.py",
        "method",
    )
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OPEN_ISSUE,
        "cache invalidation still needs a regression test",
        "issue",
    )
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.RETROSPECTIVE,
        "background " + ("noise " * 400),
        "background",
    )

    brief = service.recall(_request(root, "pytest cache regression", 128))

    assert "false" in brief.text
    assert "run pytest cache/test_store.py" in brief.text
    assert "cache invalidation still needs" in brief.text
    assert "background noise" not in brief.text
    assert brief.estimated_tokens <= 128


def test_tight_budget_shortens_or_omits_mandatory_items_deterministically(
    recall_context,
) -> None:
    database, root, _, project, _, raw, _, _ = recall_context
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OPEN_ISSUE,
        "critical cache issue " + ("very-long-detail " * 200),
        "long-issue",
    )
    service = RecallService(
        ProjectRepository(database),
        FactRepository(database),
        MemoryRepository(database),
        TokenCounterRegistry({CODEX_NAMESPACE.model_id: CharacterCounter()}),
    )
    request = _request(root, "critical cache issue", 128)

    first = service.recall(request)
    second = service.recall(request)

    assert first == second
    assert first.estimated_tokens == len(first.text)
    assert first.estimated_tokens <= request.max_tokens
    assert "Open issues" in first.text
    assert "refs:1" in first.text
    assert "mandatory_content_shortened" in first.warnings


@pytest.mark.parametrize(
    ("configured_budget", "effective_budget"),
    ((700, 700), (800, 800), (1200, 800)),
)
def test_recall_clamps_to_configured_and_product_hard_budget(
    recall_context,
    configured_budget: int,
    effective_budget: int,
) -> None:
    database, root, _, project, _, raw, _, _ = recall_context
    for index in range(20):
        _insert_memory(
            database,
            raw,
            project.project_id,
            CODEX_NAMESPACE,
            MemoryKind.OPEN_ISSUE,
            f"critical cache issue {index} " + ("long-detail " * 50),
            f"configured-budget-{configured_budget}-{index}",
        )
    service = RecallService(
        ProjectRepository(database),
        FactRepository(database),
        MemoryRepository(database),
        TokenCounterRegistry({CODEX_NAMESPACE.model_id: CharacterCounter()}),
        max_recall_tokens=configured_budget,
    )

    brief = service.recall(_request(root, "critical cache issue", 4096))

    assert brief.estimated_tokens == len(brief.text)
    assert effective_budget // 2 < brief.estimated_tokens <= effective_budget
    assert "mandatory_content_shortened" in brief.warnings


@pytest.mark.parametrize(
    "counter",
    [
        RaisingCounter(),
        InvalidCounter(),
        NonzeroEmptyCounter(),
        FailsOnContentCounter(),
    ],
)
def test_unavailable_or_invalid_exact_counter_falls_back_without_failing_recall(
    recall_context,
    counter,
) -> None:
    database, root, _, project, _, raw, _, _ = recall_context
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OPEN_ISSUE,
        "cache issue remains",
        "counter-fallback-issue",
    )
    service = RecallService(
        ProjectRepository(database),
        FactRepository(database),
        MemoryRepository(database),
        TokenCounterRegistry({CODEX_NAMESPACE.model_id: counter}),
    )

    brief = service.recall(_request(root, "cache issue", 128))

    assert brief.estimated_tokens <= 128
    assert "cache issue remains" in brief.text
    assert "token_counter_fallback" in brief.warnings


def test_stateful_nonzero_empty_count_permanently_falls_back(recall_context) -> None:
    database, root, _, _, _, _, _, _ = recall_context
    counter = LaterNonzeroEmptyCounter()
    service = RecallService(
        ProjectRepository(database),
        FactRepository(database),
        MemoryRepository(database),
        TokenCounterRegistry({CODEX_NAMESPACE.model_id: counter}),
    )

    brief = service.recall(_request(root, "empty project", 128))

    assert brief.text == ""
    assert brief.estimated_tokens == 0
    assert brief.estimated_tokens <= 128
    assert "token_counter_fallback" in brief.warnings


def test_fallback_transition_reselects_and_shortens_mandatory_issue(
    recall_context,
) -> None:
    database, root, _, project, _, raw, _, _ = recall_context
    issue_id = _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OPEN_ISSUE,
        "critical issue " + ("long-detail " * 250),
        "fallback-reselection-issue",
    )
    service = RecallService(
        ProjectRepository(database),
        FactRepository(database),
        MemoryRepository(database),
        TokenCounterRegistry({CODEX_NAMESPACE.model_id: FailsAfterFirstNonemptyCounter()}),
    )

    brief = service.recall(_request(root, "critical issue", 128))

    assert brief.estimated_tokens <= 128
    assert issue_id in brief.selected_ids
    assert brief.omitted_count == 0
    assert "Open issues" in brief.text
    assert "refs:1" in brief.text
    assert "token_counter_fallback" in brief.warnings
    assert "mandatory_content_shortened" in brief.warnings


def test_mandatory_fitting_has_bounded_count_calls_for_400_candidates() -> None:
    candidates = [
        _Candidate(
            record_id=UUID(int=index + 1),
            content=f"mandatory-{index:03d}-" + ("detail" * 30),
            section="Current state",
            mandatory=True,
            path_command_match=0,
            overlap=0,
            evidence_strength=2,
            observed_at=0,
            confidence=1.0,
        )
        for index in range(400)
    ]
    counter = CallBudgetCharacterCounter(max_calls=100)

    selected, overrides = _fit_mandatory(candidates, counter, max_tokens=128)

    assert selected
    assert selected == candidates[: len(selected)]
    assert counter.calls <= 100
    assert counter.count(_render(selected, overrides)) <= 128


def test_duplicate_normalized_content_renders_once_with_stable_selected_id(
    recall_context,
) -> None:
    database, root, _, project, facts, raw, _, service = recall_context
    content = "run pytest tests/cache"
    _observe_fact(facts, project.project_id, "package_script", content, "package:test")
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.VERIFIED_METHOD,
        content,
        "duplicate-method",
    )

    first = service.recall(_request(root, "run pytest cache"))
    second = service.recall(_request(root, "run pytest cache"))

    assert first == second
    assert first.text.count(content) == 1
    assert len(first.selected_ids) == 1
    assert first.omitted_count == 0


def test_punctuation_query_is_plain_local_input_and_cannot_broaden_namespace(
    recall_context,
) -> None:
    database, root, _, project, _, raw, recording, service = recall_context
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.VERIFIED_METHOD,
        "cache command safe",
        "safe",
    )
    _insert_memory(
        database,
        raw,
        project.project_id,
        FOREIGN_NAMESPACE,
        MemoryKind.VERIFIED_METHOD,
        "foreign broadened marker",
        "foreign",
    )
    task = 'cache") OR * NOT ('

    brief = service.recall(_request(root, task))

    assert "cache command safe" in brief.text
    assert "foreign broadened marker" not in brief.text
    assert "foreign broadened marker" not in recording.loaded_contents
    assert all(task not in warning for warning in brief.warnings)


def test_recall_keeps_oldest_relevant_chinese_outside_blank_top_100(
    recall_context,
) -> None:
    database, root, _, project, _, raw, _, service = recall_context
    relevant_id = _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.VERIFIED_METHOD,
        "缓存命令应该被召回",
        "oldest-relevant-chinese",
        created_at=FIXED_TIME - timedelta(days=1),
    )
    for index in range(100):
        _insert_memory(
            database,
            raw,
            project.project_id,
            CODEX_NAMESPACE,
            MemoryKind.RETROSPECTIVE,
            f"newer unrelated item {index:03d}",
            f"newer-unrelated-{index:03d}",
            created_at=FIXED_TIME + timedelta(seconds=index),
        )

    brief = service.recall(_request(root, "缓存命令", 128))

    assert "缓存命令应该被召回" in brief.text
    assert relevant_id in brief.selected_ids
    assert brief.estimated_tokens <= 128


def test_unknown_project_returns_stable_empty_brief(recall_context, tmp_path: Path) -> None:
    _, _, _, _, _, _, _, service = recall_context
    unknown = tmp_path / "unknown"
    unknown.mkdir()

    brief = service.recall(_request(unknown, "private task text"))

    assert brief.text == ""
    assert brief.estimated_tokens == 0
    assert brief.selected_ids == ()
    assert brief.omitted_count == 0
    assert brief.warnings == ("project_not_found",)


def test_recall_discards_private_memory_if_project_is_replaced_after_resolution(
    recall_context,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, root, _, project, _, raw, _, service = recall_context
    marker = "OLD_PROJECT_PRIVATE_MEMORY"
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OUTCOME,
        marker,
        "stale-project-memory",
    )
    projects = service._projects
    original_find = projects.find_by_cwd
    displaced = tmp_path / "project-displaced"

    def replace_after_find(cwd: Path):
        found = original_find(cwd)
        assert found is not None
        root.rename(displaced)
        root.mkdir()
        return found

    monkeypatch.setattr(projects, "find_by_cwd", replace_after_find)

    brief = service.recall(_request(root, "private memory"))

    assert brief.text == ""
    assert brief.selected_ids == ()
    assert brief.warnings == ("project_not_found",)
    assert marker not in brief.text


def test_recall_rejects_darwin_device_drift_during_one_read(
    recall_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, root, _, project, _, raw, _, service = recall_context
    marker = "DEVICE_DRIFT_PRIVATE_MEMORY"
    _insert_memory(
        database,
        raw,
        project.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OUTCOME,
        marker,
        "device-drift-memory",
    )
    real_find = service._projects.find_by_cwd
    real_identity = projects_module.complete_directory_identity
    state = {"armed": False, "checks": 0}

    def find_and_arm(cwd: Path):
        found = real_find(cwd)
        assert found is not None
        state["armed"] = True
        return found

    def drift_after_first_check(path: Path):
        identity = real_identity(path)
        if identity is None or not state["armed"]:
            return identity
        state["checks"] += 1
        if state["checks"] == 1:
            return identity
        return (identity[0] + 1, identity[1])

    monkeypatch.setattr(path_identity_module.sys, "platform", "darwin")
    monkeypatch.setattr(service._projects, "find_by_cwd", find_and_arm)
    monkeypatch.setattr(
        projects_module,
        "complete_directory_identity",
        drift_after_first_check,
    )

    brief = service.recall(_request(root, "private memory"))

    assert state["checks"] >= 2
    assert brief.text == ""
    assert brief.selected_ids == ()
    assert brief.warnings == ("project_not_found",)
    assert marker not in brief.text


def test_recall_does_not_leak_outer_memory_when_inner_project_is_disabled(
    recall_context,
) -> None:
    database, root, _, outer, _, raw, _, service = recall_context
    inner = root / "packages" / "inner"
    inner.mkdir(parents=True)
    projects = service._projects
    inner_record = projects.register(ProjectCandidate(canonical_path=inner, display_name="Inner"))
    projects.set_enabled(inner_record.project_id, False)
    marker = "OUTER_PROJECT_PRIVATE_MEMORY"
    _insert_memory(
        database,
        raw,
        outer.project_id,
        CODEX_NAMESPACE,
        MemoryKind.OUTCOME,
        marker,
        "outer-private-memory",
    )

    brief = service.recall(_request(inner, "private memory"))

    assert brief.text == ""
    assert brief.warnings == ("project_not_found",)
    assert marker not in brief.text
