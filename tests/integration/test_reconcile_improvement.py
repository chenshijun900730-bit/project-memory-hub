from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import project_memory_hub.discovery.scanner as scanner_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import _duplicate_candidate_count, build_container
from project_memory_hub.domain import (
    DiscoveryResult,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.improvement.analyzer import (
    HealthSnapshot,
    ImprovementAnalyzer,
)
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.services.reconcile import DiscoveryStageResult, ReconcileService
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.proposals import (
    ProposalCreateResult,
    ProposalDraft,
    ProposalRepository,
)


def _database(tmp_path: Path) -> tuple[Database, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    database = Database(runtime / "memory.db")
    database.initialize()
    return database, runtime


def _analyzer_draft(signature: str = "analyzer.health.v1.synthetic.gte_1"):
    return ProposalDraft(
        signature=signature,
        title="Synthetic health finding",
        description="Synthetic counter: 1.",
        risk="low",
        patch=None,
        verification_argv=(),
        target_version=None,
        origin="analyzer",
    )


def _stored_report(database: Database) -> dict[str, object]:
    with database.connect(readonly=True) as connection:
        document = connection.execute(
            "select value_json from app_state where name='last_reconcile_report'"
        ).fetchone()[0]
    return json.loads(document)


def test_reconcile_builds_truthful_numeric_health_snapshot_even_on_core_failure(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    proposals = ProposalRepository(database)
    snapshots: list[HealthSnapshot] = []
    retry = SimpleNamespace(
        drain=lambda _capture: SimpleNamespace(
            completed_count=0,
            failed_count=5,
            remaining_count=6,
        )
    )

    def analyze(snapshot: HealthSnapshot):
        snapshots.append(snapshot)
        return ImprovementAnalyzer().analyze(snapshot)

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        discover=lambda: DiscoveryStageResult(
            (),
            failure_count=2,
            permission_failure_count=3,
            duplicate_candidate_count=4,
        ),
        retry_queue=retry,  # type: ignore[arg-type]
        retry_capture=object(),  # type: ignore[arg-type]
        codex_runs=(
            lambda: SimpleNamespace(
                inserted_count=8,
                duplicate_count=9,
                failure_count=7,
                warning_count=0,
            ),
        ),
        improvement_analyzer=analyze,
        improvement_draft_sink=proposals.create,
    )

    report = service.run(force=True)

    assert report.status == "failed"
    assert snapshots == [
        HealthSnapshot(
            discovery_failure_count=2,
            permission_failure_count=3,
            adapter_failure_count=7,
            retry_failure_count=5,
            retry_remaining_count=6,
            inserted_count=8,
            duplicate_count=9,
            duplicate_candidate_count=4,
            compaction_failure_count=0,
            compaction_remaining_count=0,
        )
    ]
    assert report.stages["improvement"] == "pass"


def test_improvement_runs_after_compaction_before_app_state_and_records_counts_only(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    proposals = ProposalRepository(database)
    events: list[str] = []

    def compact(_now):
        events.append("compaction")
        return SimpleNamespace(failure_count=0, remaining_count=0)

    def analyze(snapshot: HealthSnapshot):
        events.append("analyze")
        with database.connect(readonly=True) as connection:
            assert (
                connection.execute(
                    "select count(*) from app_state where name='last_reconcile_report'"
                ).fetchone()[0]
                == 0
            )
        return ImprovementAnalyzer().analyze(snapshot)

    def sink(draft: ProposalDraft):
        events.append("sink")
        with database.connect(readonly=True) as connection:
            assert (
                connection.execute(
                    "select count(*) from app_state where name='last_reconcile_report'"
                ).fetchone()[0]
                == 0
            )
        return proposals.create(draft)

    report = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        compact=compact,
        codex_runs=(
            lambda: SimpleNamespace(
                inserted_count=4,
                duplicate_count=16,
                failure_count=0,
                warning_count=0,
            ),
        ),
        improvement_analyzer=analyze,
        improvement_draft_sink=sink,
    ).run(force=True)

    assert report.status == "success"
    assert events == ["compaction", "analyze", "sink"]
    stored = _stored_report(database)
    assert stored["stage_metrics"]["improvement"] == {
        "analyzed_count": 1,
        "created_count": 1,
        "duplicate_count": 0,
        "failure_count": 0,
        "skipped_count": 0,
    }
    serialized = json.dumps(stored)
    assert "analyzer.health.v1.duplicate_pressure" not in serialized
    assert "Reduce duplicate pressure" not in serialized


def test_repeated_reconcile_deduplicates_active_analyzer_signature(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    proposals = ProposalRepository(database)
    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        discover=lambda: DiscoveryStageResult((), failure_count=1),
        improvement_analyzer=ImprovementAnalyzer().analyze,
        improvement_draft_sink=proposals.create,
    )

    first = service.run(force=True)
    second = service.run(force=True)

    assert first.status == second.status == "failed"
    assert len(proposals.list_summaries()) == 1
    assert _stored_report(database)["stage_metrics"]["improvement"] == {
        "analyzed_count": 1,
        "created_count": 0,
        "duplicate_count": 1,
        "failure_count": 0,
        "skipped_count": 0,
    }


@pytest.mark.parametrize("failure_at", ("analyzer", "sink"))
def test_improvement_exceptions_are_noncritical_and_record_due_success(
    tmp_path: Path, failure_at: str
) -> None:
    database, runtime = _database(tmp_path)
    marker = "PRIVATE_PROPOSAL_TEXT"

    def analyze(snapshot: HealthSnapshot):
        if failure_at == "analyzer":
            raise RuntimeError(marker)
        return ImprovementAnalyzer().analyze(snapshot)

    def sink(_draft: ProposalDraft):
        raise RuntimeError(marker)

    service = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(
            lambda: SimpleNamespace(
                inserted_count=4,
                duplicate_count=16,
                failure_count=0,
                warning_count=0,
            ),
        ),
        improvement_analyzer=analyze,
        improvement_draft_sink=sink,
    )

    report = service.run(force=True)

    assert report.status == "degraded"
    assert report.warning_count == 1
    assert report.stages["improvement"] == "warn"
    assert service.should_run() is False
    stored = _stored_report(database)
    assert stored["stage_errors"]["improvement"] == "improvement_analysis_failed"
    assert marker not in json.dumps(stored)
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from app_state where name='last_reconcile_success'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "select count(*) from app_state where name='reconcile_catchup_required'"
            ).fetchone()[0]
            == 0
        )


def test_missing_analyzer_is_a_pass_and_preserves_minimal_reconcile(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)

    report = ReconcileService.minimal(database, ProcessLock(runtime / "lock")).run(force=True)

    assert report.status == "success"
    assert report.stages["improvement"] == "pass"
    assert _stored_report(database)["stage_metrics"]["improvement"] == {
        "analyzed_count": 0,
        "created_count": 0,
        "duplicate_count": 0,
        "failure_count": 0,
        "skipped_count": 1,
    }


@pytest.mark.parametrize(
    "field", ("failure_count", "permission_failure_count", "duplicate_candidate_count")
)
@pytest.mark.parametrize("invalid", (-1, 2**31, True, "1"))
def test_discovery_stage_counts_are_strictly_bounded(field: str, invalid: object) -> None:
    values: dict[str, object] = {
        "failure_count": 0,
        "permission_failure_count": 0,
        "duplicate_candidate_count": 0,
    }
    values[field] = invalid

    with pytest.raises(ValueError):
        DiscoveryStageResult((), **values)  # type: ignore[arg-type]


def test_catchup_branch_runs_improvement_before_recording_incomplete_state(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    proposals = ProposalRepository(database)
    snapshots: list[HealthSnapshot] = []
    events: list[str] = []
    retry = SimpleNamespace(
        drain=lambda _capture: SimpleNamespace(
            completed_count=0,
            failed_count=0,
            remaining_count=1,
        )
    )

    def analyze(snapshot: HealthSnapshot):
        snapshots.append(snapshot)
        events.append("analyze")
        with database.connect(readonly=True) as connection:
            assert (
                connection.execute(
                    "select count(*) from app_state where name='last_reconcile_report'"
                ).fetchone()[0]
                == 0
            )
        return ImprovementAnalyzer().analyze(snapshot)

    def sink(draft: ProposalDraft):
        events.append("sink")
        return proposals.create(draft)

    report = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        retry_queue=retry,  # type: ignore[arg-type]
        retry_capture=object(),  # type: ignore[arg-type]
        improvement_analyzer=analyze,
        improvement_draft_sink=sink,
    ).run(force=True)

    assert report.status == "degraded"
    assert events == ["analyze", "sink"]
    assert snapshots[0].retry_remaining_count == 1
    stored = _stored_report(database)
    assert stored["stage_metrics"]["improvement"]["created_count"] == 1
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute(
                "select count(*) from app_state where name='reconcile_catchup_required'"
            ).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "select count(*) from app_state where name='last_reconcile_success'"
            ).fetchone()[0]
            == 0
        )


def test_malicious_injected_draft_fails_closed_before_sink_and_never_reaches_state(
    tmp_path: Path,
) -> None:
    database, runtime = _database(tmp_path)
    marker = "Authorization: Bearer abcdefghijklmnop"
    calls = 0

    def analyze(snapshot: HealthSnapshot):
        canonical = ImprovementAnalyzer().analyze(snapshot)[0]
        return [canonical.model_copy(update={"patch": marker})]

    def sink(_draft: ProposalDraft):
        nonlocal calls
        calls += 1

    report = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(
            lambda: SimpleNamespace(
                inserted_count=4,
                duplicate_count=16,
                failure_count=0,
                warning_count=0,
            ),
        ),
        improvement_analyzer=analyze,
        improvement_draft_sink=sink,
    ).run(force=True)

    assert report.status == "degraded"
    assert calls == 0
    stored = _stored_report(database)
    assert stored["stage_errors"]["improvement"] == "improvement_analysis_failed"
    assert marker not in json.dumps(stored)


@pytest.mark.parametrize("construction", ("model_copy", "model_construct"))
@pytest.mark.parametrize("field", ("title", "description"))
@pytest.mark.parametrize(
    "project_path",
    (
        "/Users/alice/Documents/ConfidentialClient/ProjectPhoenix",
        r"C:\Users\alice\ProjectPhoenix",
        r"\\server\share\ProjectPhoenix",
    ),
)
def test_forged_exact_draft_with_project_path_rejects_batch_before_sink(
    tmp_path: Path,
    construction: str,
    field: str,
    project_path: str,
) -> None:
    database, runtime = _database(tmp_path)
    proposals = ProposalRepository(database)
    sink_calls: list[ProposalDraft] = []

    def analyze(snapshot: HealthSnapshot):
        canonical = ImprovementAnalyzer().analyze(snapshot)[0]
        forged_value = f"{getattr(canonical, field)} {project_path}"
        if construction == "model_copy":
            forged = canonical.model_copy(update={field: forged_value})
        else:
            fields = {name: getattr(canonical, name) for name in ProposalDraft.model_fields}
            fields[field] = forged_value
            forged = ProposalDraft.model_construct(**fields)
        return [forged]

    def sink(draft: ProposalDraft):
        sink_calls.append(draft)
        return proposals.create(draft)

    report = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        discover=lambda: DiscoveryStageResult((), failure_count=1),
        improvement_analyzer=analyze,
        improvement_draft_sink=sink,
    ).run(force=True)

    assert report.stages["improvement"] == "warn"
    assert sink_calls == []
    assert proposals.list_summaries() == ()
    stored = _stored_report(database)
    assert stored["stage_errors"]["improvement"] == "improvement_analysis_failed"
    assert "ProjectPhoenix" not in json.dumps(stored)


@pytest.mark.parametrize("failure_mode", ("sixth", "generator_error"))
def test_invalid_analyzer_batch_is_validated_before_any_sink_write(
    tmp_path: Path, failure_mode: str
) -> None:
    database, runtime = _database(tmp_path)
    calls = 0

    def analyze(_snapshot: HealthSnapshot):
        def drafts():
            for index in range(6):
                if failure_mode == "generator_error" and index == 3:
                    raise RuntimeError("PRIVATE_GENERATOR_FAILURE")
                yield _analyzer_draft(f"analyzer.health.v1.synthetic_{index}.gte_1")

        return drafts()

    def sink(_draft: ProposalDraft):
        nonlocal calls
        calls += 1

    report = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        improvement_analyzer=analyze,
        improvement_draft_sink=sink,
    ).run(force=True)

    assert report.status == "degraded"
    assert calls == 0
    stored = _stored_report(database)
    assert stored["stage_metrics"]["improvement"]["created_count"] == 0
    assert "PRIVATE_GENERATOR_FAILURE" not in json.dumps(stored)


@pytest.mark.parametrize("result_kind", ("duck", "invalid_exact"))
def test_sink_result_must_be_an_exact_revalidated_create_result(
    tmp_path: Path, result_kind: str
) -> None:
    database, runtime = _database(tmp_path)
    seed = ProposalRepository(database).create(_analyzer_draft("analyzer.health.v1.seed.gte_1"))
    invalid = (
        SimpleNamespace(inserted=True, duplicate=False, record=seed.record)
        if result_kind == "duck"
        else ProposalCreateResult.model_construct(
            inserted="yes", duplicate=False, record=seed.record
        )
    )

    report = ReconcileService(
        database,
        ProcessLock(runtime / "lock"),
        codex_runs=(
            lambda: SimpleNamespace(
                inserted_count=4,
                duplicate_count=16,
                failure_count=0,
                warning_count=0,
            ),
        ),
        improvement_analyzer=ImprovementAnalyzer().analyze,
        improvement_draft_sink=lambda _draft: invalid,
    ).run(force=True)

    assert report.status == "degraded"
    assert _stored_report(database)["stage_errors"]["improvement"] == (
        "improvement_analysis_failed"
    )


def test_duplicate_candidate_metric_counts_fingerprint_groups_not_paths() -> None:
    fingerprint = "a" * 64
    result = DiscoveryResult(
        candidates=(
            ProjectCandidate(
                canonical_path=Path("/tmp/first"),
                display_name="first",
                git_remote_fingerprint=fingerprint,
                manifest_fingerprint=fingerprint,
            ),
            ProjectCandidate(
                canonical_path=Path("/tmp/second"),
                display_name="second",
                git_remote_fingerprint=fingerprint,
                manifest_fingerprint=fingerprint,
            ),
        ),
        issues=(),
    )

    assert _duplicate_candidate_count(result) == 2


def test_container_computes_permission_and_duplicate_counts_from_discovery_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan_root = tmp_path / "scan-root"
    blocked_root = tmp_path / "blocked-root"
    scan_root.mkdir()
    blocked_root.mkdir()
    for name in ("first", "second"):
        project = scan_root / name
        project.mkdir()
        (project / "package.json").write_text('{"name":"duplicate-app"}', encoding="utf-8")
    runtime = tmp_path / "container-runtime"
    runtime.mkdir(mode=0o700)
    config_path = runtime / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(scan_root, blocked_root),
            enabled_sources=(SourceAgent.CHATGPT,),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    original_open = scanner_module._open_allowed_root

    def selective_open(path: Path) -> int:
        if path == blocked_root.resolve():
            raise PermissionError("synthetic blocked root")
        return original_open(path)

    monkeypatch.setattr(scanner_module, "_open_allowed_root", selective_open)
    with build_container(config_path) as container:
        report = container.reconcile.run(force=True)
        summaries = container.proposals.list_summaries()
        records = {
            summary.signature: container.proposals.get(summary.proposal_id) for summary in summaries
        }
        stored = _stored_report(container.database)

    assert report.status == "failed"
    assert stored["stage_metrics"]["discover"] == {
        "duplicate_candidate_count": 1,
        "failure_count": 1,
        "permission_failure_count": 1,
        "project_count": 2,
    }
    assert stored["stage_metrics"]["improvement"]["created_count"] == 2
    assert set(records) == {
        "analyzer.health.v1.discovery_health.gte_1",
        "analyzer.health.v1.duplicate_pressure.gte_1",
    }
    assert (
        "permission failures: 1" in records["analyzer.health.v1.discovery_health.gte_1"].description
    )
    assert (
        "Duplicate candidates: 1"
        in records["analyzer.health.v1.duplicate_pressure.gte_1"].description
    )
