# Explicit Open Issue Resolution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship Project Memory Hub 0.1.2 with adapter-verified, exact-namespace, auditable resolution of prior `Open issue` memories while preserving unverified-capture safety and replay idempotency.

**Architecture:** Extend the structured capture contract with an optional `resolved_open_issues` list whose empty value preserves the 0.1.1 canonical hash. Migration v9 adds a resolution audit table plus nullable verified-capture project/model provenance on `source_refs`, so replay is accepted only when the original exact namespace is provable. A focused repository resolves exact full-text matches inside that namespace. Codex owns one outer transaction per `AdapterBatch`; ChatGPT owns one per conversation, so capture, lifecycle changes, audit rows, checkpoint or receipt commit atomically.

**Tech Stack:** Python 3.11+, Pydantic 2, stdlib `sqlite3`, Typer, FastAPI/Jinja2, pytest, Ruff, mypy, Hatchling/uv, Graphify.

---

## Scope Strategy

This remains one plan because storage, capture, adapter receipts/checkpoints, reconcile metrics, and the local console share one lifecycle contract. Splitting them into independently shippable plans would create an unsafe intermediate release in which an issue could be archived without its receipt or displayed as resolved without an audit row.

The implementation has four checkpoints:

1. Tasks 1–4 establish compatible input, retry, schema, and repository contracts.
2. Tasks 5–8 make verified capture atomic for Codex and ChatGPT.
3. Tasks 9–11 expose counts, UI state, documentation, and version 0.1.2.
4. Tasks 12–13 run release gates and upgrade the stable local installation only after branch integration.

## Locked Boundaries

- Source design: `docs/superpowers/specs/2026-07-16-explicit-issue-resolution-design.md`.
- Only the explicit label `Resolved issue: <exact prior Open issue text>` can resolve a memory.
- Matching is `project_id + source_agent + exact model_id + content_hash + normalized_content`.
- No semantic similarity, newest-wins inference, keyword inference, cross-model access, or automatic merge.
- Unverified direct capture may persist only a pending structured payload; it cannot query or update old memories.
- A complete source-record replay returns `duplicate` with all three resolution counts equal to zero.
- Replay must prove the persisted capture project and exact model; ambiguous legacy provenance fails closed.
- `not_found` is auditable and nonfatal; its warning never contains the issue text.
- Trae, WorkBuddy, Zcode, QoderWork, and Claude Code remain unavailable in 0.1.2.
- Do not create, restore, or require the daily 03:30 automation. `codex_automation_missing` remains an accepted nonblocking doctor warning.
- Do not write the real runtime database, install 0.1.2, or stop live processes before Task 13.
- The stable launcher remains an editable install from the non-worktree checkout. A wheel is a verification artifact only.
- Each code-changing task follows red → green → refactor and creates one Chinese conventional commit.

## File and Responsibility Map

### New files

- `src/project_memory_hub/storage/migrations/0009_explicit_issue_resolution.sql` — verified-capture provenance columns/backfill, audit table, partial unique indexes, and namespace ownership triggers.
- `src/project_memory_hub/storage/resolutions.py` — exact-match resolution, audit insertion, archive updates, already-resolved full-text checks, and scoped display lookup.
- `tests/unit/storage/test_resolutions.py` — repository-level exactness, time/source bounds, idempotency, and full-text collision tests.

### Domain, privacy, and retry

- `src/project_memory_hub/domain.py` — new payload/record field and `CaptureResult` statuses/counts.
- `src/project_memory_hub/security/capture_privacy.py` — optional-list canonicalization without changing the hash of empty 0.1.1 payloads.
- `src/project_memory_hub/services/retry_queue.py` — privacy version 2 plus explicit migration of legacy/v1 retry envelopes.
- `tests/unit/test_domain.py` — domain defaults and status validation.
- `tests/unit/security/test_capture_privacy.py` — empty-field hash compatibility and bounded resolution text.
- `tests/integration/test_reconcile_hardening.py` — existing retry rows remain drainable after the contract expands.

### Storage and verified capture

- `src/project_memory_hub/services/capture.py` — normalization, contradiction rejection, prepared verified capture, connection-scoped writes, status priority, and replay short-circuit.
- `src/project_memory_hub/storage/checkpoints.py` — connection-scoped checkpoint, receipt, and receipt-existence methods.
- `src/project_memory_hub/storage/projects.py` — registry generation snapshot and all-touched-project transaction guards.
- `src/project_memory_hub/container.py` — construct/inject the repository and new ingestion dependencies.
- `tests/unit/services/test_capture.py` — pending safety, status matrix, replay, source/time bounds, rollback, and namespace isolation.
- `tests/unit/storage/test_database.py` — v9 schema, migration prefix, trigger, index, and rollback contracts.

### Adapters and orchestration

- `src/project_memory_hub/adapters/codex.py` — `Resolved issue` marker grammar while retaining parser version 3 for historical-byte compatibility.
- `src/project_memory_hub/adapters/base.py` — Codex outer transaction and resolution count aggregation.
- `src/project_memory_hub/adapters/chatgpt.py` — explicit ChatGPT label plus one transaction per conversation.
- `src/project_memory_hub/services/reconcile.py` — stage-level resolution counters and redacted warning aggregation.
- `src/project_memory_hub/cli.py` — ChatGPT import JSON projection; direct capture already uses `model_dump`.
- `tests/integration/test_codex_adapter.py` — parser, batch atomicity, checkpoint conflict, and multi-project drift.
- `tests/integration/test_chatgpt_adapter.py` — extractor, receipt atomicity, replay, warning, and registry drift.
- `tests/integration/test_reconcile.py` — count propagation and stage metrics.
- `tests/integration/test_cli_core.py` — stable JSON output, no issue-text leakage, and 0.1.2 version.

### Console, documentation, and release

- `src/project_memory_hub/services/control.py` — read-only `Resolved` versus `Archived` display metadata.
- `src/project_memory_hub/web/templates/memories.html` — render the lifecycle label without changing action scope.
- `tests/integration/test_web_routes.py` — exact namespace display isolation.
- `tests/e2e/test_memory_hub.py` — verified resolve-to-recall flow.
- `tests/e2e/test_dashboard.py` — browser display of resolved and manually archived records.
- `README.md` — new JSON field, marker label, explicit semantics, and source boundaries.
- `docs/operations.md` — v9 backup/migration/recovery and known automation warning.
- `pyproject.toml` and `src/project_memory_hub/__init__.py` — tracked version 0.1.2 metadata; `uv.lock` remains an ignored local resolver artifact.

## Stable Interfaces

Use these names consistently in every task:

```python
@dataclass(frozen=True, slots=True)
class ResolutionApplyResult:
    resolved_count: int = 0
    already_resolved_count: int = 0
    unmatched_resolution_count: int = 0


@dataclass(frozen=True, slots=True)
class PreparedVerifiedCapture:
    project: ProjectRecord
    payload: CapturePayload
    verification: NamespaceVerification
    source_record_id: str
    structured: dict[str, object]
    structured_hash: str
    mapped_rows: tuple[tuple[MemoryKind, str], ...]
    resolved_open_issues: tuple[str, ...]
    captured_at: datetime
    task_fingerprint: str
```

Add a stateless `CheckpointConflictError(RuntimeError)` and keep these exact connection-scoped
contracts:

| Method | Positional parameters | Keyword-only parameters | Return |
| --- | --- | --- | --- |
| `commit_on_connection` | `self, connection: sqlite3.Connection, adapter: SourceAgent, scope: str` | `expected_checkpoint: AdapterCheckpoint \| None, next_checkpoint: AdapterCheckpoint, source_record_ids: tuple[str, ...] = ()` | `None` |
| `receipt_exists_on_connection` | `self, connection: sqlite3.Connection, source_hash: str, source_record_id: str, source_agent: SourceAgent` | none | `bool` |
| `commit_import_receipt_on_connection` | `self, connection: sqlite3.Connection, source_hash: str, source_record_id: str, source_agent: SourceAgent` | `confirmation: dict[str, object] \| None = None` | `None` |

Tasks 7 and 8 define the SQL and every branch for these contracts; do not add commits, rollbacks, or
secondary connections inside them.

`IssueResolutionRepository.apply_on_connection()` receives an existing SQLite connection,
the exact project and namespace, the source reference, normalized declarations, and trusted
verification/capture times; it returns `ResolutionApplyResult` and never commits. Its
`resolved_target_ids_scoped()` read API receives at most 100 memory IDs and always requires the
exact project, source agent, and model ID.

`CaptureService.prepare_verified()` returns either `PreparedVerifiedCapture` or a terminal rejected
result without writing. `capture_prepared_on_connection()` receives an existing connection and
never opens, commits, or rolls back a transaction.

Every caller must branch on the preparation union explicitly:

```python
prepared = capture.prepare_verified(payload, verification)
if isinstance(prepared, CaptureResult):
    raise IngestionError(f"capture preparation rejected: {prepared.status}")
```

Do not use truthiness or inspect private attributes to distinguish the two variants.

### Task 1: Extend domain and canonical capture without changing empty 0.1.1 hashes

**Files:**
- Modify: `src/project_memory_hub/domain.py:142-209`
- Modify: `src/project_memory_hub/security/capture_privacy.py:18-33,343-505`
- Test: `tests/unit/test_domain.py`
- Test: `tests/unit/security/test_capture_privacy.py`

- [ ] **Step 1: Write failing domain contract tests**

Add `CaptureResult`, `NormalizedTaskRecord`, `NamespaceVerification`, `datetime`, and `timezone`
imports, then instantiate old payloads without the new field and assert the new defaults and
statuses:

```python
def test_resolution_fields_are_backward_compatible(tmp_path: Path) -> None:
    namespace_value = namespace("gpt-5.6-sol")
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=namespace_value,
        source_record_id="legacy-record",
        objective="legacy objective",
        outcome="legacy outcome",
    )
    assert payload.resolved_open_issues == []

    verification = NamespaceVerification(
        namespace=namespace_value,
        source_record_id="legacy-record",
        verified_by="codex_adapter",
        verified_at=datetime(2026, 7, 16, tzinfo=timezone.utc),
    )
    record = NormalizedTaskRecord(
        cwd=tmp_path,
        namespace=namespace_value,
        source_record_id="legacy-record",
        objective="legacy objective",
        outcome="legacy outcome",
        verification=verification,
    )
    assert record.resolved_open_issues == ()

    result = CaptureResult(status="resolved", resolved_count=2)
    assert result.model_dump(mode="json") == {
        "inserted_ids": [],
        "duplicate": False,
        "status": "resolved",
        "resolved_count": 2,
        "already_resolved_count": 0,
        "unmatched_resolution_count": 0,
    }
    assert CaptureResult(status="partial", unmatched_resolution_count=1).status == "partial"
```

- [ ] **Step 2: Write failing canonical compatibility tests**

Add `hashlib` and `json` imports. Use a payload with all legacy fields and freeze the legacy
private structure in the test; this avoids comparing two code paths that both contain the same
regression:

```python
def test_empty_resolution_list_preserves_legacy_private_structure(tmp_path: Path) -> None:
    canonicalizer = CapturePrivacyCanonicalizer(Redactor())
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
        source_record_id="compatibility-record",
        objective="objective",
        outcome="outcome",
        decisions=["decision"],
        resolved_open_issues=[],
    )
    structure = canonicalizer.structure(payload, tmp_path)
    expected = {
        "objective": "objective",
        "outcome": "outcome",
        "decisions": ["decision"],
        "failed_attempts": [],
        "verified_commands": [],
        "changed_paths": [],
        "preferences": [],
        "risks": [],
        "open_issues": [],
        "reusable_lessons": [],
    }
    assert "resolved_open_issues" not in structure
    assert structure == expected
    canonical_json = json.dumps(structure, sort_keys=True, separators=(",", ":"))
    assert hashlib.sha256(canonical_json.encode()).hexdigest() == (
        "f214abfb70fffe382ef096ed63c2baa496b81db6bd8f40e4b8ab46f7c368c175"
    )


def test_nonempty_resolution_list_is_canonicalized_and_counted(tmp_path: Path) -> None:
    payload = CapturePayload(
        cwd=tmp_path,
        namespace=Namespace(source_agent=SourceAgent.CODEX, model_id="gpt-5.6-sol"),
        source_record_id="resolution-record",
        objective="objective",
        outcome="outcome",
        resolved_open_issues=["  exact   issue  "],
    )
    structure = CapturePrivacyCanonicalizer(Redactor()).structure(
        payload, tmp_path
    )
    assert structure["resolved_open_issues"] == ["exact issue"]
```

- [ ] **Step 3: Run the focused tests and verify red**

Run:

```bash
uv run pytest tests/unit/test_domain.py tests/unit/security/test_capture_privacy.py -q
```

Expected: FAIL because `resolved_open_issues`, `resolved`, and `partial` do not exist.

- [ ] **Step 4: Add the domain fields and counts**

Apply these exact field additions in `domain.py`:

```python
class CapturePayload(BaseModel):
    open_issues: list[str] = Field(default_factory=list)
    resolved_open_issues: list[str] = Field(default_factory=list)
    reusable_lessons: list[str] = Field(default_factory=list)


class NormalizedTaskRecord(BaseModel, frozen=True):
    open_issues: tuple[str, ...] = ()
    resolved_open_issues: tuple[str, ...] = ()
    reusable_lessons: tuple[str, ...] = ()


class CaptureResult(BaseModel, frozen=True):
    inserted_ids: tuple[UUID, ...] = ()
    duplicate: bool = False
    status: Literal[
        "inserted", "duplicate", "resolved", "partial",
        "pending_verification", "project_not_found", "rejected",
    ]
    resolved_count: int = Field(default=0, ge=0)
    already_resolved_count: int = Field(default=0, ge=0)
    unmatched_resolution_count: int = Field(default=0, ge=0)
```

- [ ] **Step 5: Separate legacy and optional canonical list fields**

Keep the old list tuple byte-for-byte and add an optional tuple:

```python
LIST_FIELDS = (
    "decisions", "failed_attempts", "verified_commands", "changed_paths",
    "preferences", "risks", "open_issues", "reusable_lessons",
)
OPTIONAL_LIST_FIELDS = ("resolved_open_issues",)
ALL_LIST_FIELDS = (*LIST_FIELDS, *OPTIONAL_LIST_FIELDS)
```

In `_private_structure`, canonicalize the optional field but persist it only when nonempty:

```python
for field in LIST_FIELDS:
    values = getattr(payload, field)
    structure[field] = (
        self.changed_paths(values, project_path)
        if field == "changed_paths" and project_path is not None
        else self.private_list(values)
    )
for field in OPTIONAL_LIST_FIELDS:
    values = self.private_list(getattr(payload, field))
    if values:
        structure[field] = values
```

Change `_validate_capture_bound` to iterate over `ALL_LIST_FIELDS`. This includes resolution text in size limits without adding an empty JSON key.

Also add `"resolved_open_issues"` to the `list_fields` tuple in
`test_capture_payload_list_defaults_are_independent` so the independent default-factory contract is
covered.

- [ ] **Step 6: Run focused tests and verify green**

Run:

```bash
uv run pytest tests/unit/test_domain.py tests/unit/security/test_capture_privacy.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the compatible capture contract**

```bash
git add src/project_memory_hub/domain.py src/project_memory_hub/security/capture_privacy.py tests/unit/test_domain.py tests/unit/security/test_capture_privacy.py
git commit -m "feat(capture): 扩展显式问题解决输入"
```

### Task 2: Upgrade retry envelopes without rejecting privacy version 1 rows

**Files:**
- Modify: `src/project_memory_hub/services/retry_queue.py:11-18,143-148,182-223,268-312,343-424`
- Test: `tests/integration/test_reconcile_hardening.py:293-454`

- [ ] **Step 1: Write failing v1-to-v2 retry tests**

Persist one current 0.1.1 envelope with `privacy_version=1` and no resolution key, then verify it drains to a pending capture. Also verify a new envelope retains a nonempty resolution list:

```python
def test_retry_drain_migrates_privacy_v1_with_empty_resolution_list(tmp_path: Path) -> None:
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(_payload(project, "retry-v1"), "operational_failure")
    with database.transaction() as connection:
        row = connection.execute("select retry_id, payload_json from retry_items").fetchone()
        stored = json.loads(row["payload_json"])
        stored["privacy_version"] = 1
        stored.pop("resolved_open_issues", None)
        connection.execute(
            "update retry_items set payload_json = ? where retry_id = ?",
            (json.dumps(stored, sort_keys=True, separators=(",", ":")), row["retry_id"]),
        )
    report = queue.drain(capture)
    assert report.completed_count == 1
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from retry_items").fetchone()[0] == 0
        stored = json.loads(connection.execute(
            "select structured_payload_json from pending_captures"
        ).fetchone()[0])
    assert "resolved_open_issues" not in stored


def test_retry_v2_round_trips_resolution_declarations(tmp_path: Path) -> None:
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(
        _payload(project, "retry-v2", resolved_open_issues=["exact old issue"]),
        "operational_failure",
    )
    assert queue.drain(capture).completed_count == 1
    with database.connect(readonly=True) as connection:
        stored = json.loads(connection.execute(
            "select structured_payload_json from pending_captures"
        ).fetchone()[0])
    assert stored["resolved_open_issues"] == ["exact old issue"]
```

- [ ] **Step 2: Run the retry tests and verify red**

Run:

```bash
uv run pytest tests/integration/test_reconcile_hardening.py -k 'privacy_v1 or retry_v2' -q
```

Expected: the v1 compatibility case may already pass on 0.1.1; the v2 round-trip case FAILS because
the exact key set treats version 1 as current and knows no resolution field.

- [ ] **Step 3: Define explicit envelope versions**

Replace the single current key set with these contracts:

```python
_LEGACY_PAYLOAD_KEYS = frozenset(
    {"namespace", "project_id", "source_record_id", *TEXT_FIELDS, *LIST_FIELDS}
)
_V1_PAYLOAD_KEYS = _LEGACY_PAYLOAD_KEYS | {"privacy_version"}
_V2_PAYLOAD_KEYS = frozenset(
    {"namespace", "project_id", "source_record_id", *TEXT_FIELDS, *ALL_LIST_FIELDS,
     "privacy_version"}
)
_CURRENT_PRIVACY_VERSION = 2
```

Import `ALL_LIST_FIELDS` from `capture_privacy.py`. In `enqueue()`, add the optional key to the retry envelope even when canonical capture omits it:

```python
private_structure = self._canonicalizer.structure(payload, project_path)
private_structure.setdefault("resolved_open_issues", [])
values = {
    "namespace": namespace,
    "privacy_version": _CURRENT_PRIVACY_VERSION,
    "project_id": str(project.project_id).lower(),
    "source_record_id": source_record_id,
    **private_structure,
}
```

- [ ] **Step 4: Migrate legacy and v1 rows in memory before validation**

Change the exact signature to
`_validate_stored(value: object) -> tuple[UUID, Literal[0, 1, 2]]`; import `Literal`. Accept only the
three exact key sets and exact numeric version. Preserve all existing version-0 fail-closed checks:
private-text recanonicalization for text/list values and bounded project-relative validation for
`changed_paths`. Versions 1 and 2 are already-private envelopes and are revalidated against their
canonical structure.

Before `_validated_current_payload`, unpack the return value and rebuild older envelopes as follows:

```python
project_id, stored_version = self._validate_stored(value)
payload = self._payload_from_stored(value, project_path, stored_version)
if stored_version < 2:
    current_structure = self._canonicalizer.stored_structure(payload, project_path)
    current_structure.setdefault("resolved_open_issues", [])
    current_value = {
        **{key: item for key, item in value.items() if key != "privacy_version"},
        "privacy_version": 2,
        **current_structure,
    }
else:
    current_value = value
```

`_payload_from_stored()` must use `LIST_FIELDS` for versions 0/1 and `ALL_LIST_FIELDS` for version 2; Pydantic supplies an empty resolution list for older rows. `_validated_current_payload()` must compare all v2 fields and require `privacy_version == 2`.

In `_validated_current_payload()`, recalculate the private structure, call
`stored_structure.setdefault("resolved_open_issues", [])`, and only then compare it with all v2
fields. Replace every old `is_legacy` branch with `stored_version == 0`; do not weaken the legacy
private-text/path checks while changing the return type.

- [ ] **Step 5: Run retry compatibility tests**

Run:

```bash
uv run pytest tests/integration/test_reconcile_hardening.py -q
```

Expected: PASS, including the existing bare-legacy migration cases.

- [ ] **Step 6: Commit retry compatibility**

```bash
git add src/project_memory_hub/services/retry_queue.py tests/integration/test_reconcile_hardening.py
git commit -m "fix(retry): 兼容旧版捕获信封"
```

### Task 3: Add migration v9 and enforce audit ownership in SQLite

**Files:**
- Create: `src/project_memory_hub/storage/migrations/0009_explicit_issue_resolution.sql`
- Modify: `tests/unit/storage/test_database.py:14-151,190-250,502-526,968-1060`

- [ ] **Step 1: Extend the failing schema contract**

Add this table contract and change expected migration versions to `range(1, 10)`:

```python
"memory_issue_resolutions": (
    "resolution_id", "project_id", "source_agent", "model_id",
    "target_content_hash", "target_memory_id", "source_reference_id",
    "status", "resolved_at",
),
```

Append `capture_project_id` and `capture_model_id` to the existing `source_refs` column contract.
Require indexes `idx_issue_resolutions_resolved_unique`, `idx_issue_resolutions_not_found_unique`, and
`idx_issue_resolutions_target`, plus these six triggers:

```text
capture_provenance_pair_insert
capture_provenance_pair_update
issue_resolution_target_insert
issue_resolution_target_update
issue_resolution_source_insert
issue_resolution_source_update
```

- [ ] **Step 2: Write failing migration behavior tests**

Create a v8 database with one Codex open issue, upgrade it, and assert its unambiguous source is
backfilled with project `p1` and exact model `gpt-5.6-sol`. Insert a distinct later Codex source
reference named `source-2` with the two new provenance columns, then use concrete SQL for both audit
statuses:

```python
assert versions == tuple(range(1, 10))
assert old_open_issue["lifecycle_state"] == "active"

connection.execute(
    """insert into memory_issue_resolutions(
           resolution_id, project_id, source_agent, model_id, target_content_hash,
           target_memory_id, source_reference_id, status, resolved_at
       ) values(?,?,?,?,?,?,?,?,?)""",
    (
        "r1", "p1", "codex", "gpt-5.6-sol", "a" * 64, "m1", "source-2",
        "resolved", "2026-07-16T00:00:00Z",
    ),
)
not_found_values = (
    "nf1", "p1", "codex", "gpt-5.6-sol", "b" * 64, None,
    "source-2", "not_found", "2026-07-16T00:00:00Z",
)
connection.execute(
    """insert or ignore into memory_issue_resolutions(
           resolution_id, project_id, source_agent, model_id, target_content_hash,
           target_memory_id, source_reference_id, status, resolved_at
       ) values(?,?,?,?,?,?,?,?,?)""",
    not_found_values,
)
connection.execute(
    """insert or ignore into memory_issue_resolutions(
           resolution_id, project_id, source_agent, model_id, target_content_hash,
           target_memory_id, source_reference_id, status, resolved_at
       ) values(?,?,?,?,?,?,?,?,?)""",
    ("nf2", *not_found_values[1:]),
)
assert connection.execute(
    "select count(*) from memory_issue_resolutions where status='not_found'"
).fetchone()[0] == 1
```

Parameterize invalid `target_memory_id` ownership by wrong project, source, model, and non-`open_issue`; each insert must raise `sqlite3.IntegrityError`.

Also cover:

- a legacy capture source referenced by memories in more than one project/model remains
  `capture_project_id=NULL, capture_model_id=NULL` rather than being guessed;
- setting only one provenance column is rejected on insert and update;
- an audit whose `source_reference_id` has another source agent, project, exact model, or ambiguous
  legacy provenance is rejected on insert and update.

Update every existing assertion that uses the full real migration set from versions 1–8 to 1–9.
Existing synthetic migration tests currently append a fake version 9; change those fake migrations
and their assertions to version 10 so they do not collide with the new real file. Keep tests that
intentionally stop at v6 or v7 unchanged.

- [ ] **Step 3: Run the database tests and verify red**

Run:

```bash
uv run pytest tests/unit/storage/test_database.py -q
```

Expected: FAIL because migration 9 and its schema objects do not exist.

- [ ] **Step 4: Create the v9 migration**

First add and safely backfill verified-capture provenance:

```sql
ALTER TABLE source_refs ADD COLUMN capture_project_id TEXT
    REFERENCES projects(project_id) ON DELETE RESTRICT;
ALTER TABLE source_refs ADD COLUMN capture_model_id TEXT
    CHECK (capture_model_id IS NULL OR length(trim(capture_model_id)) > 0);

UPDATE source_refs AS source
SET capture_project_id = (
        SELECT baseline.project_id
        FROM behavior_memories AS baseline
        WHERE baseline.source_reference_id = source.source_reference_id
        ORDER BY baseline.memory_id
        LIMIT 1
    ),
    capture_model_id = (
        SELECT baseline.model_id
        FROM behavior_memories AS baseline
        WHERE baseline.source_reference_id = source.source_reference_id
        ORDER BY baseline.memory_id
        LIMIT 1
    )
WHERE source.parser_version = 'capture-v1'
  AND source.source_path IS NULL
  AND EXISTS (
      SELECT 1 FROM behavior_memories AS present
      WHERE present.source_reference_id = source.source_reference_id
  )
  AND NOT EXISTS (
      SELECT 1
      FROM behavior_memories AS candidate
      WHERE candidate.source_reference_id = source.source_reference_id
        AND (
          candidate.source_agent <> source.source_agent
          OR candidate.project_id <> (
              SELECT baseline.project_id FROM behavior_memories AS baseline
              WHERE baseline.source_reference_id = source.source_reference_id
              ORDER BY baseline.memory_id LIMIT 1
          )
          OR candidate.model_id <> (
              SELECT baseline.model_id FROM behavior_memories AS baseline
              WHERE baseline.source_reference_id = source.source_reference_id
              ORDER BY baseline.memory_id LIMIT 1
          )
        )
  );
```

Add insert/update triggers that reject a row when exactly one of `capture_project_id` and
`capture_model_id` is null. Compaction and ambiguous legacy sources may keep both null; all newly
verified captures write both.

Then create this audit schema and exact partial-index keys:

```sql
CREATE TABLE memory_issue_resolutions (
    resolution_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    source_agent TEXT NOT NULL,
    model_id TEXT NOT NULL CHECK (length(trim(model_id)) > 0),
    target_content_hash TEXT NOT NULL CHECK (length(target_content_hash) = 64),
    target_memory_id TEXT REFERENCES behavior_memories(memory_id) ON DELETE RESTRICT,
    source_reference_id TEXT NOT NULL REFERENCES source_refs(source_reference_id) ON DELETE RESTRICT,
    status TEXT NOT NULL CHECK (status IN ('resolved', 'not_found')),
    resolved_at TEXT NOT NULL,
    CHECK (
        (status = 'resolved' AND target_memory_id IS NOT NULL)
        OR (status = 'not_found' AND target_memory_id IS NULL)
    )
);

CREATE UNIQUE INDEX idx_issue_resolutions_resolved_unique
ON memory_issue_resolutions(
    project_id, source_agent, model_id, source_reference_id,
    target_content_hash, target_memory_id
)
WHERE status = 'resolved';

CREATE UNIQUE INDEX idx_issue_resolutions_not_found_unique
ON memory_issue_resolutions(
    project_id, source_agent, model_id, source_reference_id, target_content_hash
)
WHERE status = 'not_found';

CREATE INDEX idx_issue_resolutions_target
ON memory_issue_resolutions(project_id, source_agent, model_id, target_memory_id)
WHERE status = 'resolved';
```

Add both `BEFORE INSERT` and `BEFORE UPDATE OF project_id, source_agent, model_id,
target_memory_id, status` target triggers. For a non-null target, each trigger must use
`RAISE(ABORT, 'resolution target namespace mismatch')` unless an `open_issue` row exists with the
same `project_id`, `source_agent`, and `model_id`.

Add insert/update source-ownership triggers over `project_id`, `source_agent`, `model_id`, and
`source_reference_id`. They must require a `source_refs` row whose `source_agent`,
`capture_project_id`, and exact `capture_model_id` equal the audit row; otherwise raise
`resolution source namespace mismatch`. This makes ambiguous legacy provenance fail closed and
prevents a cross-source audit even if application code is wrong.

- [ ] **Step 5: Verify migration atomicity and prefix safety**

Run:

```bash
uv run pytest tests/unit/storage/test_database.py -q
```

Expected: PASS. Existing future-version, gapped-history, snapshot, and failed-migration tests must remain green.

- [ ] **Step 6: Commit schema v9**

```bash
git add src/project_memory_hub/storage/migrations/0009_explicit_issue_resolution.sql tests/unit/storage/test_database.py
git commit -m "feat(storage): 增加问题解决审计与来源证明"
```

### Task 4: Implement exact-match resolution repository

**Files:**
- Create: `src/project_memory_hub/storage/resolutions.py`
- Create: `tests/unit/storage/test_resolutions.py`

- [ ] **Step 1: Write failing exact-scope and multi-target tests**

Build two projects and three exact namespaces. Import `datetime` and `timezone`, set
`verified_at = datetime(2026, 7, 16, tzinfo=timezone.utc)`, and insert two active, same-text open
issues in the target namespace plus same-text rows in the other scopes. Assert one declaration
archives only the two target rows and writes two `resolved` audit rows:

```python
def _states(connection: sqlite3.Connection, memory_ids: tuple[UUID, ...]) -> set[str]:
    question_marks = ",".join("?" for _memory_id in memory_ids)
    rows = connection.execute(
        f"select lifecycle_state from behavior_memories where memory_id in ({question_marks})",
        tuple(str(memory_id).lower() for memory_id in memory_ids),
    ).fetchall()
    return {str(row["lifecycle_state"]) for row in rows}
```

Seed through explicit SQL helpers named `_insert_project`, `_insert_source`, and `_insert_open_issue`.
`_insert_source` must populate source agent, source record ID, hash, timestamp, parser version
`capture-v1`, and the new capture project/model provenance; `_insert_open_issue` must populate all
required `behavior_memories` columns and use the same source/project/namespace. Do not bypass foreign
keys or disable triggers in these tests.

```python
result = repository.apply_on_connection(
    connection,
    project_id=target_project_id,
    namespace=Namespace(source_agent="codex", model_id="gpt-5.6-sol"),
    source_reference_id=current_source_id,
    declarations=("exact old issue",),
    verified_at=verified_at,
    resolved_at=verified_at,
)
assert result == ResolutionApplyResult(resolved_count=2)
assert _states(connection, target_ids) == {"archived"}
assert _states(connection, foreign_ids) == {"active"}
```

- [ ] **Step 2: Write failing replay, time, source, and collision tests**

Define a local `apply_declaration(source_reference_id, text, verified_at)` helper that delegates to
`repository.apply_on_connection()` with the fixed target project/namespace and current connection.
Cover these exact cases:

```python
def apply_declaration(
    source_reference_id: UUID,
    text: str,
    when: datetime,
) -> ResolutionApplyResult:
    return repository.apply_on_connection(
        connection,
        project_id=target_project_id,
        namespace=target_namespace,
        source_reference_id=source_reference_id,
        declarations=(text,),
        verified_at=when,
        resolved_at=when,
    )
```

```python
# Same source reference is never allowed to resolve itself.
same_source = apply_declaration(current_source_id, "same text", verified_at)
assert same_source.unmatched_resolution_count == 1
assert _states(connection, (same_source_target_id,)) == {"active"}

# A target source timestamp later than verification.verified_at remains active.
assert apply_declaration(
    older_declaration_source, "future issue", verified_at
).unmatched_resolution_count == 1

# A successful historical audit is already_resolved only after joined full-text equality.
assert apply_declaration(
    new_source, "exact old issue", verified_at
).already_resolved_count == 1

# Force the same content hash onto a different normalized_content row.
collision = apply_declaration(new_source, "hash collision text", verified_at)
assert collision.already_resolved_count == 0
assert collision.unmatched_resolution_count == 1
```

Also assert an unmatched declaration inserts one `not_found` row, and repeating the same source record through the repository's unique key does not increment unmatched count twice.

- [ ] **Step 3: Run repository tests and verify red**

Run:

```bash
uv run pytest tests/unit/storage/test_resolutions.py -q
```

Expected: FAIL because `IssueResolutionRepository` does not exist.

- [ ] **Step 4: Implement active target lookup and archive**

Create `ResolutionApplyResult` and `IssueResolutionRepository`. Before touching a target, query the
current `source_reference_id` and require its source agent, capture project ID, and exact capture
model ID to match the method arguments; ambiguous or foreign provenance raises `ValueError` before
any update. The migration triggers remain the database-level defense.

For each declaration compute lowercase SHA-256, then fetch active targets in bounded batches of 256:

```sql
SELECT bm.memory_id
FROM behavior_memories AS bm
JOIN source_refs AS source ON source.source_reference_id = bm.source_reference_id
WHERE bm.project_id = ?
  AND bm.source_agent = ?
  AND bm.model_id = ?
  AND bm.memory_kind = 'open_issue'
  AND bm.lifecycle_state = 'active'
  AND bm.content_hash = ?
  AND bm.normalized_content = ?
  AND bm.source_reference_id <> ?
  AND strict_utc_epoch_us(source.source_timestamp) IS NOT NULL
  AND strict_utc_epoch_us(source.source_timestamp) <= strict_utc_epoch_us(?)
ORDER BY bm.created_at, bm.memory_id
LIMIT 256
```

Update each selected row with a SQL predicate that repeats the
project/source/model/kind/active constraints; require `rowcount == 1`. Insert one `resolved` audit
row per target with `uuid4()` and the passed `resolved_at`. Repeat the same bounded query until it
returns no rows, so every exact target is archived without building an unbounded Python list.

Extend the multi-target test to create 257 exact active targets and assert all 257 are archived and
audited. This proves the batching boundary does not become a behavioral cap.

- [ ] **Step 5: Implement full-text already-resolved and not-found behavior**

When no active target exists, use this query; do not accept a hash-only match:

```sql
SELECT 1
FROM memory_issue_resolutions AS resolution
JOIN behavior_memories AS target
  ON target.memory_id = resolution.target_memory_id
WHERE resolution.project_id = ?
  AND resolution.source_agent = ?
  AND resolution.model_id = ?
  AND resolution.status = 'resolved'
  AND resolution.target_content_hash = ?
  AND target.normalized_content = ?
LIMIT 1
```

If it matches, increment `already_resolved_count`. Otherwise execute `INSERT OR IGNORE` for one `not_found` row; increment `unmatched_resolution_count` only when `rowcount == 1`. Return accumulated immutable counts.

- [ ] **Step 6: Implement exact-scoped display lookup**

`resolved_target_ids_scoped()` must return empty for no IDs, cap input at 100 IDs, and query with all isolation keys before the dynamic `IN` predicate:

```python
question_marks = ",".join("?" for _memory_id in memory_ids)
rows = connection.execute(
    f"""select target_memory_id
        from memory_issue_resolutions
        where project_id = ? and source_agent = ? and model_id = ?
          and status = 'resolved' and target_memory_id in ({question_marks})""",
    (
        str(project_id).lower(),
        namespace.source_agent.value,
        namespace.model_id,
        *(str(memory_id).lower() for memory_id in memory_ids),
    ),
).fetchall()
```

Validate each returned UUID and return a `frozenset[UUID]`.

- [ ] **Step 7: Run repository tests and verify green**

Run:

```bash
uv run pytest tests/unit/storage/test_resolutions.py tests/unit/storage/test_namespace_isolation.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit the repository**

```bash
git add src/project_memory_hub/storage/resolutions.py tests/unit/storage/test_resolutions.py
git commit -m "feat(memory): 实现旧问题精确解决"
```

### Task 5: Make verified capture resolution-aware and connection-scoped

**Files:**
- Modify: `src/project_memory_hub/services/capture.py:1-430`
- Modify: `src/project_memory_hub/container.py:164-245`
- Test: `tests/unit/services/test_capture.py`
- Test: `tests/unit/services/test_recall.py`
- Test: `tests/integration/test_reconcile_hardening.py`

- [ ] **Step 1: Write failing normalization and pending-safety tests**

Extend the test stack with `IssueResolutionRepository`. Seed one verified active `open_issue`, then
submit an unverified resolution-only payload and assert:

```python
result = service.capture(
    _payload(root, objective="", outcome="", open_issues=[],
             resolved_open_issues=["exact old issue"])
)
assert result.status == "pending_verification"
assert _lifecycle(database, old_issue_id) == "active"
assert _resolution_audit_count(database) == 0
```

Define the two test helpers in `test_capture.py` exactly as bounded scalar queries:

```python
def _lifecycle(database: Database, memory_id: UUID) -> str:
    with database.connect(readonly=True) as connection:
        row = connection.execute(
            "select lifecycle_state from behavior_memories where memory_id = ?",
            (str(memory_id).lower(),),
        ).fetchone()
    assert row is not None
    return str(row["lifecycle_state"])


def _resolution_audit_count(database: Database) -> int:
    with database.connect(readonly=True) as connection:
        return int(
            connection.execute("select count(*) from memory_issue_resolutions").fetchone()[0]
        )
```

Add a second test with duplicate declarations containing different whitespace; after privacy
normalization they must retain the first occurrence only. A resolution item that normalizes to an
empty string must reject the whole capture rather than be discarded. Add a contradiction test where
`open_issues` and `resolved_open_issues` normalize to the same text; assert `rejected` and no source,
memory, pending, or audit row was written.

In `test_reconcile_hardening.py`, enqueue a v2 payload with empty objective/outcome and only one
resolution declaration. Draining it must create one pending capture containing the declaration and
must leave the seeded old issue active with no audit row. This is the retry-only regression that the
Task 2 round-trip test cannot cover before capture preparation is refactored.

- [ ] **Step 2: Write the failing verified status matrix**

Parameterize these outcomes and assert all three counts exactly:

| New memory | New archived target | New not-found | Expected status |
| --- | --- | --- | --- |
| yes | no | no | `inserted` |
| yes | yes | no | `inserted` |
| yes | no | yes | `partial` |
| no | yes | no | `resolved` |
| no | no | yes | `partial` |
| no | previously resolved only | no | `duplicate` with nonzero `already_resolved_count` |

For a complete replay of the same `source_record_id` and structured hash, assert `duplicate=True`
and all three resolution counts are zero.

- [ ] **Step 3: Write failing isolation, time, self-source, and rollback tests**

Cover all of the following in `test_capture.py`:

- exact project/source/model matches archive;
- same text in another project, source agent, or exact model stays active;
- a target whose `source_refs.source_timestamp` is later than `verification.verified_at` stays active;
- a same-source-reference target is not archived;
- an exact same-source replay in the same project/model returns duplicate with zero counts;
- the same source agent, source record ID, and canonical hash presented under another project or
  exact model is rejected before replay and changes nothing;
- a migrated legacy source whose project/model provenance is null is rejected rather than guessed;
- raising a sentinel exception after `capture_prepared_on_connection()` inside an outer
  `Database.transaction()` rolls back new memories, lifecycle changes, source refs, and audit rows.

In `test_recall.py`, seed one target issue and one unrelated active issue, resolve the target, and
assert recall omits the target while still returning the unrelated issue.

- [ ] **Step 4: Run the capture and recall tests and verify red**

Run:

```bash
uv run pytest tests/unit/services/test_capture.py tests/unit/services/test_recall.py tests/integration/test_reconcile_hardening.py -q
```

Expected: FAIL because resolution-only input is rejected and verified capture owns its own
transaction with no resolution repository.

- [ ] **Step 5: Normalize and prepare verified captures before any write**

Inject `IssueResolutionRepository` into `CaptureService`. Extract one common private preparation
helper used by public unverified capture, `_capture_untrusted_on_connection()` during retry drain,
and `prepare_verified()`. It takes the canonical
private `open_issues` and `resolved_open_issues`, rejects any blank normalized resolution value,
deduplicates declarations in first-seen order, and rejects a nonempty set intersection. Do this after
redaction and whitespace normalization, not on raw strings. Remove the optional key again when the
deduplicated list is empty so legacy hashes remain stable.

Implement `prepare_verified()` so it performs project lookup/currentness, safe identifiers,
canonicalization, contradiction validation, verification equality, canonical JSON/hash,
task-fingerprint construction, mapped-row construction, and trusted capture time without writing.
Accept the record when either mapped rows or normalized resolution declarations are nonempty.
The retry connection-scoped path must use this same predicate; it must not retain its old
`if not self._mapped_rows(structured): rejected` shortcut.

Keep public `capture()` behavior split explicitly:

- without verification, store the canonical payload in `pending_captures` and never call the
  resolution repository;
- with verification, prepare once, open one local transaction, guard project identity, call the
  connection-scoped method, guard project identity again, and let `Database.transaction()` own
  commit or rollback.

- [ ] **Step 6: Implement connection-scoped verified capture**

In `capture_prepared_on_connection()`:

1. Call `_source_ref()` on the supplied connection and pass the prepared project ID plus exact model.
   On insert, persist both as `source_refs.capture_project_id` and `capture_model_id`.
2. If the source row exists, require `source_agent`, parser provenance, capture project ID, and exact
   capture model ID to match before treating it as replay. Null legacy provenance or a cross-project/
   cross-model reuse raises `_IncompatibleSourceProvenance`; the public wrapper returns `rejected`
   and adapter callers roll back without advancing checkpoint/receipt.
3. If that fully proven trusted source reference already exists, return a replay `duplicate` immediately
   with zero resolution counts.
4. Insert mapped behavior rows on the same connection.
5. Call `IssueResolutionRepository.apply_on_connection()` with the same connection, exact project
   and namespace, new source reference, normalized declarations, `verification.verified_at`, and
   capture time.
6. Mark a matching pending capture verified only for a newly created source reference.
7. Advance `last_observed_change` only when this transaction inserts a behavior memory, archives a
   target, or writes a new `not_found` audit.

Return status in this fixed order:

```python
if resolution.unmatched_resolution_count:
    status = "partial"
elif inserted_ids:
    status = "inserted"
elif resolution.resolved_count:
    status = "resolved"
else:
    status = "duplicate"
```

Set `duplicate=True` only for the final branch. Preserve nonzero `already_resolved_count` there, but
never on the complete-source replay short-circuit.

- [ ] **Step 7: Run focused tests and verify green**

Run:

```bash
uv run pytest tests/unit/services/test_capture.py tests/unit/services/test_recall.py tests/integration/test_reconcile_hardening.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit connection-scoped capture**

```bash
git add src/project_memory_hub/services/capture.py src/project_memory_hub/container.py tests/unit/services/test_capture.py tests/unit/services/test_recall.py tests/integration/test_reconcile_hardening.py
git commit -m "feat(capture): 原子处理显式问题解决"
```

### Task 6: Parse explicit Codex resolution labels without replaying all history

**Files:**
- Modify: `src/project_memory_hub/adapters/codex.py:43-96,811-931`
- Modify: `src/project_memory_hub/integration/agents.py:465-490`
- Test: `tests/integration/test_codex_adapter.py`
- Test: `tests/integration/test_agents_integration.py`

- [ ] **Step 1: Write failing Codex grammar tests**

Add a completed marker block containing two `Resolved issue:` lines and assert the resulting
`NormalizedTaskRecord.resolved_open_issues` preserves their order. Add cases for:

- duplicate declarations collapsing after normalization;
- a normalized open/resolved intersection producing `invalid_capture_block` and no record;
- a line outside the final marker pair remaining invalid;
- ordinary prose saying an issue was fixed producing no declaration;
- a resolution-only block with the required single `Objective` and `Outcome` labels being accepted.

Assert the next checkpoint still reports `parser_version == "codex-v3"`.

- [ ] **Step 2: Write a failing managed-AGENTS contract test**

Regenerate the managed block in the integration test and assert both the mapping sentence and the
allowed-label sentence contain:

```text
Resolved issue: -> resolved_open_issues
```

The exact generated prose may wrap the mapping in backticks, but the semantic label and JSON field
must both be present.

- [ ] **Step 3: Run the grammar tests and verify red**

Run:

```bash
uv run pytest tests/integration/test_codex_adapter.py tests/integration/test_agents_integration.py -k 'capture or managed' -q
```

Expected: FAIL because the label is not recognized.

- [ ] **Step 4: Extend the Codex marker grammar**

Add `Resolved issue` to `_LABEL`, map it to the internal key `resolved issue`, initialize that list,
copy it into both the adapter-validation `CapturePayload` and the final `NormalizedTaskRecord`, and
apply the same `MAX_LIST_ITEMS`, UTF-8, redaction, and aggregate bounds as `Open issue`.

After normalization, deduplicate `resolved issue` by first appearance and reject the entire block
when its set intersects `open issue`. Keep `_PARSER_VERSION = "codex-v3"`: older bytes contain no new
label, and changing the version would force an unnecessary full replay of historical sessions.

- [ ] **Step 5: Update managed capture guidance**

In `integration/agents.py`, add the new exact mapping and allowed repeated label to both managed
workflow sentences. Do not add Trae or automation instructions.

- [ ] **Step 6: Run tests and commit the grammar**

Run:

```bash
uv run pytest tests/integration/test_codex_adapter.py tests/integration/test_agents_integration.py -q
```

Expected: PASS.

```bash
git add src/project_memory_hub/adapters/codex.py src/project_memory_hub/integration/agents.py tests/integration/test_codex_adapter.py tests/integration/test_agents_integration.py
git commit -m "feat(codex): 识别显式问题解决标记"
```

### Task 7: Commit each Codex AdapterBatch atomically

**Files:**
- Modify: `src/project_memory_hub/storage/checkpoints.py:1-162`
- Modify: `src/project_memory_hub/storage/projects.py:248-307`
- Modify: `src/project_memory_hub/adapters/base.py:1-88`
- Modify: `src/project_memory_hub/container.py:164-266`
- Test: `tests/integration/test_codex_adapter.py`

- [ ] **Step 1: Replace the old crash expectation with failing batch-rollback tests**

Replace `test_capture_before_checkpoint_crash_retries_as_duplicate_and_commits` with a test that
injects a failure after lifecycle update but before checkpoint write. Assert the old issue remains
active and that the batch leaves no source ref, behavior memory, audit, receipt, or checkpoint.

Add tests for:

- a checkpoint compare-and-swap conflict after the adapter read;
- failure during checkpoint/receipt insertion;
- a two-project batch where the first project directory is replaced after the second project writes;
- replaying a committed batch, which returns duplicate capture results and zero resolution counts.
- a source record/hash reused under another project or exact model, which is rejected and does not
  advance checkpoint;
- a normal inserted/resolved batch whose final project guard succeeds after
  `last_observed_change` is updated by the same transaction.

Every failure case must assert the old checkpoint remains unchanged.
The committed-batch replay case must also assert receipt count is unchanged and no conflict is
raised when the exact receipt key already exists.

Use deterministic injection points rather than production-only fault flags:

- CAS: a fake adapter's `read_incremental()` commits a different checkpoint after ingestion reads
  the expected checkpoint but before it returns the batch;
- checkpoint failure: monkeypatch `CheckpointRepository.commit_on_connection` to raise
  `sqlite3.IntegrityError("injected checkpoint failure")` when called after captures;
- receipt failure: install a test-only `BEFORE INSERT ON import_receipts` trigger that executes
  `RAISE(ABORT, 'injected receipt failure')`;
- early-project replacement: wrap the real `commit_on_connection`, let its SQL run, replace the
  first project's directory, then return so the final all-project guard detects the live identity
  change and the outer database transaction rolls back.

- [ ] **Step 2: Run the atomicity tests and verify red**

Run:

```bash
uv run pytest tests/integration/test_codex_adapter.py -k 'rollback or checkpoint or multi_project or replay' -q
```

Expected: FAIL because capture and checkpoint currently commit in separate transactions.

- [ ] **Step 3: Extract connection-scoped checkpoint operations**

Keep public wrappers, but move SQL into the exact connection-scoped signatures in Stable Interfaces:

- `commit_on_connection()` validates adapter/scope, compares the currently stored checkpoint with
  `expected_checkpoint` byte-for-byte after canonical cursor JSON normalization, inserts import
  receipts, and writes `next_checkpoint`;
- a mismatch raises a dedicated checkpoint-conflict exception before changing the row;
- for each Codex receipt key, query on the same connection first: an exact existing receipt is an
  idempotent replay and is skipped; a missing receipt uses strict `INSERT` and must affect exactly
  one row, otherwise raise the same conflict exception;
- `receipt_exists_on_connection()` and `commit_import_receipt_on_connection()` validate inputs and
  perform SQL without opening or committing a transaction.

Public `commit()`, `receipt_exists()`, and `commit_import_receipt()` open their current connection or
transaction and delegate. Codex uses the per-receipt idempotent check above; ChatGPT short-circuits a
conversation-level existing receipt before capture and uses a strict insert only for a missing key.
Never use `INSERT OR IGNORE` after capture writes.

- [ ] **Step 4: Add project-generation and all-record guards**

Add `ProjectRepository.registry_generation_on_connection()` with strict integer validation. Add
`require_records_current_on_connection()` that receives the transaction-start generation and all
unique `ProjectRecord` values touched by the batch. Compare only registry-owned identity fields:
global generation, project ID, canonical path, enabled state, persisted path device/inode, and live
physical path identity. Explicitly exclude `last_observed_change`, `updated_at`, inactivity state,
and other fields that this same capture transaction may legitimately change. Raise
`ReconcileRequiredError` through the ingestion layer when any check fails.

- [ ] **Step 5: Move Codex ingestion under one outer transaction**

Inject `Database` and `ProjectRepository` into `IngestionService`. Prepare every batch record before
the write transaction using the explicit `isinstance(prepared, CaptureResult)` branch in Stable
Interfaces; any terminal preparation result raises `IngestionError`. Then:

1. Open one `Database.transaction()` for the bounded `AdapterBatch`.
2. Read and retain the registry generation on that connection.
3. Before each capture, add its project to the touched-project map and validate the generation plus
   all touched records.
4. Call `capture_prepared_on_connection()` for each record and accept `inserted`, `duplicate`,
   `resolved`, or `partial`.
5. Call `CheckpointRepository.commit_on_connection(connection, adapter.source_agent, scope,
   expected_checkpoint=checkpoint, next_checkpoint=batch.next_checkpoint,
   source_record_ids=tuple(source_record_ids))`.
6. Revalidate the original generation and every touched project after checkpoint SQL, then return
   and let the outer context commit once.

Any exception must escape the context so rollback occurs; convert only expected project drift to
`ReconcileRequiredError` after rollback.

- [ ] **Step 6: Aggregate explicit resolution counts**

Extend `IngestionResult` with default-zero `resolved_count`, `already_resolved_count`, and
`unmatched_resolution_count`. Sum them from the capture results after the transaction succeeds.
Set `warning_count` to `len(batch.warnings) + unmatched_resolution_count`; only newly written
unmatched declarations contribute the second term, and neither term contains issue text.

- [ ] **Step 7: Run the full Codex adapter suite**

Run:

```bash
uv run pytest tests/integration/test_codex_adapter.py -q
```

Expected: PASS, including the new whole-batch rollback semantics.

- [ ] **Step 8: Commit Codex transaction ownership**

```bash
git add src/project_memory_hub/storage/checkpoints.py src/project_memory_hub/storage/projects.py src/project_memory_hub/adapters/base.py src/project_memory_hub/container.py tests/integration/test_codex_adapter.py
git commit -m "refactor(codex): 原子提交适配器批次"
```

### Task 8: Make ChatGPT labels and conversation imports atomic

**Files:**
- Modify: `src/project_memory_hub/adapters/chatgpt.py:50-123,285-399,425-801,911-926`
- Modify: `src/project_memory_hub/container.py`
- Test: `tests/integration/test_chatgpt_adapter.py`

- [ ] **Step 1: Write failing explicit-label tests**

Add `Resolved issue` to a synthetic assistant completion and assert extraction produces the exact
tuple. Add negative cases proving ordinary natural-language claims do not resolve anything,
duplicate declarations are first-seen deduplicated, and normalized open/resolved intersections
return no records. Add assistant segments that also contain a valid `Outcome:` but contain either
`Resolved issue:` or `Resolved issue:   `; both must reject the entire extracted record rather than
silently ignore the empty resolution label.

- [ ] **Step 2: Write failing conversation-transaction tests**

Add these fault-injection cases:

- lifecycle update succeeds, then receipt insertion raises;
- receipt insert conflicts or fails after capture;
- the matched project path changes before the final guard;
- registry generation changes inside the transaction;
- replay of an already receipted conversation returns duplicate counts and zero resolution counts;
- two different ZIP hashes containing the same conversation/source record produce one import and
  then one duplicate; the second receipt may be recorded, but imported count and all resolution
  counts stay zero;
- a source record/hash replayed under another exact model or project is rejected without receipt;
- a `blocked_by_disabled` confirmation remains unreceipted and imports successfully after the
  project is enabled;
- dry-run performs no receipt, source, memory, lifecycle, or audit write.

Assert each failed non-dry-run conversation rolls back all of its own changes without rolling back a
different conversation that committed earlier.

Use real SQLite and guard seams:

- receipt failure/conflict: install a test-only `BEFORE INSERT ON import_receipts` trigger that
  raises `injected receipt failure`; do not rely only on monkeypatching;
- path replacement: wrap the real `commit_import_receipt_on_connection`, let its SQL run, replace
  the matched directory, and return so the final live-identity guard fails;
- generation drift: wrap that same method, let its SQL run, then update the matched project's
  `display_name` on the supplied connection so the existing registry trigger increments generation;
  the final guard must fail and roll back the capture, receipt, and injected registry update.

- [ ] **Step 3: Run ChatGPT tests and verify red**

Run:

```bash
uv run pytest tests/integration/test_chatgpt_adapter.py -k 'resolved or rollback or receipt or registry or dry_run' -q
```

Expected: FAIL because capture and receipt currently use separate transactions.

- [ ] **Step 4: Extend the explicit ChatGPT extractor**

Add `Resolved issue` to `_LABEL` and the candidate label dictionary, copy it into
`NormalizedTaskRecord` and `_capture_payload()`, and include it in aggregate UTF-8 accounting.
Normalize, first-seen deduplicate, and reject open/resolved intersection exactly as for Codex.
Continue selecting only explicit labels from the final completed assistant segment.

Change the regex value group so a recognized label with an empty value is still visible to the
extractor. If the normalized key is `resolved issue` and its stripped value is empty, set
`invalid_capture=True` and reject the segment. Do not leave `(.+)` in place, because it silently
turns an invalid resolution line into an unrecognized line.

- [ ] **Step 5: Give each conversation one write transaction**

Inject `Database` into `ChatGPTExportAdapter`. Archive reading, normalization, matching, and capture
preparation may remain outside the write transaction. For each non-dry-run conversation, open one
outer transaction and:

1. recheck `receipt_exists_on_connection()` on that connection; if present, return a conversation
   duplicate before capture or confirmation, with all three resolution counts zero;
2. retain and validate the verified project-snapshot generation;
3. for matched records, validate the one project and call connection-scoped capture;
4. write the receipt and optional confirmation with strict INSERT on the same connection; an insert
   that does not affect exactly one row raises `CheckpointConflictError` and rolls back;
5. revalidate the project or full receipt-only snapshot and generation after receipt SQL;
6. commit only by returning from the outer transaction context.

Malformed, no-segment, no-explicit-statement, and ordinary confirmation-only conversations still use
the same receipt transaction wrapper. Preserve the existing safety exception:
`blocked_by_disabled` writes neither receipt nor confirmation, so enabling the project permits a
later retry. A dry run remains read-only and does not open a write transaction.

- [ ] **Step 6: Add report counts without leaking declarations**

Extend `ImportReport` with the three default-zero resolution counts and an explicit
`warning_count`. Sum `CaptureResult` counts only after a conversation commits. Increment
`warnings["resolution_not_found"]` by `unmatched_resolution_count`; render the existing compressed
warning tuple, but set `warning_count = sum(warnings.values())` so three unmatched declarations do
not become a single warning merely because the tuple has one code.

After a conversation transaction commits, classify it as duplicate when every capture result has
status `duplicate`, empty `inserted_ids`, and all three resolution counts equal to zero. Increment
`duplicate_count` and emit `ConversationImportResult.status="duplicate"` even when the receipt was
new because this was the same conversation in another archive. Otherwise increment
`imported_count`. Never increment either report before the transaction commits.

- [ ] **Step 7: Run the complete ChatGPT adapter suite**

Run:

```bash
uv run pytest tests/integration/test_chatgpt_adapter.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit ChatGPT transaction ownership**

```bash
git add src/project_memory_hub/adapters/chatgpt.py src/project_memory_hub/container.py tests/integration/test_chatgpt_adapter.py
git commit -m "feat(chatgpt): 原子导入问题解决声明"
```

### Task 9: Propagate redacted counts through reconcile and CLI JSON

**Files:**
- Modify: `src/project_memory_hub/container.py:251-266`
- Modify: `src/project_memory_hub/services/reconcile.py:298-434,993-1010,1067-1103`
- Modify: `src/project_memory_hub/cli.py:390-452`
- Test: `tests/integration/test_reconcile.py`
- Test: `tests/integration/test_cli_core.py`

- [ ] **Step 1: Write failing count-propagation tests**

Feed reconcile a Codex result and a ChatGPT report with distinct values for all three resolution
counts. Assert each stage metric contains:

```python
{
    "resolved_count": expected_resolved,
    "already_resolved_count": expected_already,
    "unmatched_resolution_count": expected_unmatched,
}
```

Add a capture with both `inserted_ids` and status `partial`; `_result_counts()` must count it as one
insert, not zero. Add a ChatGPT warning tuple containing `resolution_not_found:3` with
`warning_count=3`; reconcile must add three warnings, not `len(tuple) == 1`.

- [ ] **Step 2: Write failing CLI privacy tests**

For `memory-hub import chatgpt --format json`, assert the three counts and explicit warning count
are present, all values are nonnegative integers, and neither the exact resolved text nor the exact
unmatched text appears anywhere in serialized stdout. Existing direct capture JSON automatically
uses `CaptureResult.model_dump()`; add an assertion for its three fields rather than a manual
projection.

- [ ] **Step 3: Run focused tests and verify red**

Run:

```bash
uv run pytest tests/integration/test_reconcile.py tests/integration/test_cli_core.py -k 'resolution or warning_count or partial' -q
```

Expected: FAIL because the metrics and ChatGPT JSON projection omit these counts.

- [ ] **Step 4: Aggregate counts at both orchestration layers**

In `container.ingest_codex()`, sum all three fields returned by each `IngestionResult` and expose them
on the aggregate namespace. In reconcile, add a bounded `_resolution_counts()` helper that reads
explicit report fields or sums `capture_results`, and include its fixed keys in both success and
exception metrics for every Codex and ChatGPT stage.

Change `_result_counts()` for capture-result containers so insertion is determined by a nonempty
`inserted_ids` tuple. Count a duplicate only when status is `duplicate`; do not count `partial` as a
duplicate just because all its behavior rows already existed.

Use `report.warning_count` for ChatGPT reports. Retain `_sequence_count()` only as a compatibility
fallback for report types without an explicit count.

When a stage has a nonzero `unmatched_resolution_count`, mark it `warn` with the stable redacted
stage error `resolution_not_found`. Preserve the existing generic adapter warning code only when the
stage has warnings but no unmatched resolution.

- [ ] **Step 5: Extend ChatGPT CLI JSON projection**

Return these exact additional keys:

```python
"resolved_count": report.resolved_count,
"already_resolved_count": report.already_resolved_count,
"unmatched_resolution_count": report.unmatched_resolution_count,
"warning_count": report.warning_count,
```

Do not serialize `report.warnings`, records, or declaration text.

- [ ] **Step 6: Run tests and commit observability**

Run:

```bash
uv run pytest tests/integration/test_reconcile.py tests/integration/test_cli_core.py -q
```

Expected: PASS.

```bash
git add src/project_memory_hub/container.py src/project_memory_hub/services/reconcile.py src/project_memory_hub/cli.py tests/integration/test_reconcile.py tests/integration/test_cli_core.py
git commit -m "feat(reconcile): 汇总问题解决计数"
```

### Task 10: Distinguish Resolved from manual Archived in the console

**Files:**
- Modify: `src/project_memory_hub/container.py:55-100,164-245`
- Modify: `src/project_memory_hub/services/control.py:138-149,302-332`
- Modify: `src/project_memory_hub/web/templates/memories.html:35-50`
- Test: `tests/integration/test_web_routes.py`

- [ ] **Step 1: Write a failing exact-namespace display test**

Seed two archived open issues in the selected exact namespace: one with a successful resolution
audit and one manually archived. Seed the same target ID shape and text in another model namespace.
Request `/memories` with exact project/source/model filters and assert:

- the audited selected row displays `Resolved`;
- the manual selected row displays `Archived`;
- the other model's audit never changes the selected row label;
- omitting the exact model filter still avoids querying or rendering behavior rows.

- [ ] **Step 2: Run the route test and verify red**

Run:

```bash
uv run pytest tests/integration/test_web_routes.py -k 'resolved or archived' -q
```

Expected: FAIL because the page only knows `lifecycle_state`.

- [ ] **Step 3: Add one bounded audit lookup per page**

Expose `IssueResolutionRepository` on `ServiceContainer`. Add `lifecycle_label` to
`BehaviorMemoryMetadata`. `ControlPanelService.memories()` already loads at most 100 exact-scoped rows;
pass their IDs once to `resolved_target_ids_scoped()` and map each archived row to `Resolved` only
when its ID is in that exact-scoped result. Map all other lifecycle values by title-casing the stored
state. This is one extra bounded query, never one query per card.

- [ ] **Step 4: Render the safe display label**

Change the template's lifecycle span from `memory.lifecycle_state` to
`memory.lifecycle_label`. Keep POST action inputs and authorization based on exact stored namespace
and lifecycle, not on the display label.

- [ ] **Step 5: Run tests and commit the display change**

Run:

```bash
uv run pytest tests/integration/test_web_routes.py -q
```

Expected: PASS.

```bash
git add src/project_memory_hub/container.py src/project_memory_hub/services/control.py src/project_memory_hub/web/templates/memories.html tests/integration/test_web_routes.py
git commit -m "feat(console): 区分已解决与手动归档"
```

### Task 11: Complete end-to-end coverage, docs, and version 0.1.2

**Files:**
- Modify: `tests/e2e/test_memory_hub.py`
- Modify: `tests/e2e/test_dashboard.py`
- Modify: `tests/integration/test_cli_core.py:284`
- Modify: `README.md`
- Modify: `docs/operations.md`
- Modify: `pyproject.toml:3`
- Modify: `src/project_memory_hub/__init__.py`

- [ ] **Step 1: Write the failing end-to-end lifecycle test**

Create an old verified `Open issue`, an unrelated active memory in the same exact namespace, and
same-text issues in another model and another source. Process a later adapter-verified
`Resolved issue`, then assert:

- the target is archived and has one `resolved` audit;
- the unrelated active memory remains recallable;
- foreign namespace rows remain active;
- the declaration body is absent from audit-table columns and reconcile JSON;
- replay adds no audit, count, or warning.

- [ ] **Step 2: Extend the browser test**

Use the existing dashboard server fixture to load an exact namespace containing a resolved row and
a manually archived row. Assert the rendered cards show `Resolved` and `Archived` respectively and
that changing the exact model filter does not leak either card.

- [ ] **Step 3: Run end-to-end tests for the complete slice**

Run:

```bash
uv run playwright install chromium
uv run pytest tests/e2e/test_memory_hub.py tests/e2e/test_dashboard.py -q
```

Expected: PASS because Tasks 1–10 have already wired the behavior. If a new assertion fails, diagnose
the owning task, add a focused regression test there, and rerun that focused suite plus this command
before editing release documentation.

- [ ] **Step 4: Document the exact public contract**

Update README capture JSON and marker examples with `resolved_open_issues` and `Resolved issue:`.
State exact matching, exact project/source/model isolation, pending no-side-effect behavior,
resolution status/count semantics, unavailable sources, and the fact that no daily automation is
created.

Update `docs/operations.md` with migration v9 backup, migration, failure recovery, audit-count checks,
and the accepted `codex_automation_missing` warning. Explicitly state that schema rollback requires
restoring a verified SQLite backup; reinstalling older Python code alone is unsafe.

- [ ] **Step 5: Bump all version assertions and metadata**

Set both package version declarations and the CLI test expectation to `0.1.2`:

```text
pyproject.toml: version = "0.1.2"
src/project_memory_hub/__init__.py: __version__ = "0.1.2"
tests/integration/test_cli_core.py: expected version = "0.1.2"
```

Do not stage `uv.lock`: `.gitignore` intentionally treats it as a local resolver artifact in this
repository. Task 12 refreshes the local environment without changing that repository policy.

- [ ] **Step 6: Run the documentation-adjacent test set**

Run:

```bash
uv run pytest tests/e2e/test_memory_hub.py tests/e2e/test_dashboard.py tests/integration/test_cli_core.py tests/integration/test_agents_integration.py -q
```

Expected: PASS.

- [ ] **Step 7: Commit the release surface**

```bash
git add README.md docs/operations.md pyproject.toml src/project_memory_hub/__init__.py tests/e2e/test_memory_hub.py tests/e2e/test_dashboard.py tests/integration/test_cli_core.py
git commit -m "chore(release): 发布问题解决 0.1.2"
```

### Task 12: Run the complete quality and build gates

**Files:**
- Verify only; no intended tracked-file changes.

- [ ] **Step 1: Synchronize the test environment**

Run:

```bash
uv sync --extra test
```

Expected: exit 0. The ignored local `uv.lock` may refresh; `git status --short` must show no tracked
change from environment synchronization.

- [ ] **Step 2: Run formatting, lint, typing, and the full test suite**

Run each command independently:

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy src/project_memory_hub
uv run playwright install chromium
uv run pytest --cov=project_memory_hub --cov-report=term-missing --cov-fail-under=85
uv run pytest tests/e2e/test_dashboard.py -q
```

Expected: every command exits 0, coverage is at least 85%, Chromium is present in Playwright's local
cache, and the browser test passes on its own. If browser installation needs a download, request the
normal network approval; do not track the browser cache.

- [ ] **Step 3: Build and inspect the wheel without installing it**

Run in one owner shell with fail-fast enabled so no stale wheel can be inspected:

```bash
set -e
WHEEL_DIR="$(mktemp -d /private/tmp/project-memory-hub-0.1.2-wheel.XXXXXX)"
chmod 700 "$WHEEL_DIR"
uv build --wheel --out-dir "$WHEEL_DIR"
uv run python -c 'import sys, zipfile; from pathlib import Path; wheels=list(Path(sys.argv[1]).glob("*.whl")); assert len(wheels)==1, wheels; archive=zipfile.ZipFile(wheels[0]); assert archive.testzip() is None; names=set(archive.namelist()); assert "project_memory_hub/storage/migrations/0009_explicit_issue_resolution.sql" in names; metadata=[name for name in names if name.endswith(".dist-info/METADATA")]; assert len(metadata)==1; document=archive.read(metadata[0]).decode("utf-8"); assert "Version: 0.1.2\n" in document; print(wheels[0])' "$WHEEL_DIR"
```

Expected: the assertion command prints exactly the newly built wheel path after zip integrity,
migration presence, and METADATA version checks pass. Do not use this wheel for the stable launcher
because installation identity rejects site-packages and wheel paths.

- [ ] **Step 4: Refresh Graphify and verify repository hygiene**

Run:

```bash
graphify update .
graphify hook status
git diff --check
git status --short
```

Expected: Graphify update succeeds, both hooks are installed, `git diff --check` is silent, and Git
status is clean. `graphify-out` remains ignored.

### Task 13: Integrate the branch and upgrade the stable local runtime safely

**Files:**
- Runtime state: `~/Library/Application Support/Project Memory Hub/memory.db`
- Stable checkout: `~/Documents/example-project`
- Verification artifacts: runtime backup directory and `/private/tmp`

- [ ] **Step 1: Fast-forward only from the verified implementation branch**

Run from the stable non-worktree checkout:

```bash
git status --short
git merge --ff-only codex/project-memory-hub
git log -1 --oneline --decorate
graphify update .
graphify hook status
```

Expected: status is clean before the merge, the fast-forward succeeds, and the displayed commit is
the exact commit that passed Task 12. The stable checkout's own ignored graph is then refreshed and
both hooks remain installed; the worktree graph is a different file and cannot satisfy this step.
If the stable checkout diverged, stop and reconcile branches; do not force-reset or deploy a
worktree path.

- [ ] **Step 2: Prove no writer is active**

Set the runtime path and run three read-only checks:

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
pgrep -fl 'memory-hub|project_memory_hub'
DB_HANDLES=("$RUNTIME/memory.db")
for candidate in "$RUNTIME/memory.db-wal" "$RUNTIME/memory.db-shm" "$RUNTIME/memory.db-journal"
do
  if [[ -e "$candidate" ]]
  then
    DB_HANDLES+=("$candidate")
  fi
done
lsof "${DB_HANDLES[@]}"
memory-hub doctor --format json
```

Expected: `pgrep` and `lsof` show no verified Project Memory Hub process or open database/WAL handle;
their exit code 1 with no matches is success for this gate. The pre-upgrade 0.1.1 doctor may retain
known nonblocking warnings, but `database_quick_check` and `migration_version` must be healthy. If a
verified writer or handle appears, stop it gracefully using its recorded launch mechanism and repeat
all three checks. Do not kill an unverified process by name alone.

- [ ] **Step 3: Check and back up the real database through SQLite's backup API**

Run:

```bash
sqlite3 -readonly "$HOME/Library/Application Support/Project Memory Hub/memory.db" 'PRAGMA quick_check;'
```

Expected: exactly `ok`. Then run this from the stable checkout:

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
BACKUP="$RUNTIME/backups/pre-0.1.2-$(date +%Y%m%d-%H%M%S).db"
uv run python -c 'import sys; from pathlib import Path; from project_memory_hub.storage.database import Database; print(Database(Path(sys.argv[1])).backup(Path(sys.argv[2])))' "$RUNTIME/memory.db" "$BACKUP"
stat -f '%Lp' "$BACKUP"
sqlite3 -readonly "$BACKUP" 'PRAGMA quick_check;'
```

Expected: the backup path prints, mode is `600`, and backup quick-check is `ok`. Record the exact
backup path in the task log before migration.

- [ ] **Step 4: Install from the stable checkout and apply migration v9**

Run:

```bash
uv tool install --editable --force .
memory-hub version
memory-hub init --format json
sqlite3 -readonly "$HOME/Library/Application Support/Project Memory Hub/memory.db" "select group_concat(version, ',') from (select version from schema_migrations order by version);"
```

Expected: version `0.1.2`, init succeeds, and schema versions are exactly
`1,2,3,4,5,6,7,8,9`.

- [ ] **Step 5: Reconcile once and run doctor**

Run:

```bash
memory-hub reconcile --force --format json
memory-hub doctor --format json
```

Expected: reconcile does not fail; doctor is pass or warn, SQLite quick-check and schema are healthy,
and the only accepted nonblocking warning is `codex_automation_missing`. Do not create the daily
03:30 automation to clear that warning, and do not enable Trae or the other unavailable sources.

- [ ] **Step 6: Apply the recovery gate on any runtime failure**

If migration, reconcile, or doctor reports a blocking failure, stop all writers, preserve the failed
database files, verify the recorded backup again, and follow the v9 restore procedure added to
`docs/operations.md`. Do not continue writing and do not treat reinstalling 0.1.1 code as a database
rollback.

- [ ] **Step 7: Record the deferred self-resolution boundary**

The implementation task's final Project Memory Hub capture may include exact `Resolved issue:`
labels for the stale automation questions. Those declarations are direct pending capture until the
Codex adapter sees the final response on a later reconcile. Report the 0.1.2 runtime as upgraded, but
do not claim those historical rows are archived until that later adapter run verifies them.
