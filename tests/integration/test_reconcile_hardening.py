import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
from project_memory_hub.cli import app, _capture_with_transient_retry
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import (
    CapturePayload,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.services.reconcile import ReconcileService
from project_memory_hub.services.retry_queue import RetryQueue
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository


runner = CliRunner()


def _database(tmp_path: Path) -> tuple[Database, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    database = Database(runtime / "memory.db")
    database.initialize()
    return database, runtime


def _capture_stack(tmp_path: Path):
    database, runtime = _database(tmp_path)
    project = tmp_path / "project"
    project.mkdir()
    projects = ProjectRepository(database)
    projects.register(ProjectCandidate(canonical_path=project, display_name="project"))
    redactor = Redactor()
    capture = CaptureService(database, projects, MemoryRepository(database), redactor)
    return database, runtime, project, projects, redactor, capture


def _payload(project: Path, source_record_id: str, **changes) -> CapturePayload:
    values = {
        "cwd": project,
        "namespace": Namespace(source_agent="codex", model_id="provider/gpt-5"),
        "source_record_id": source_record_id,
        "objective": "retry objective",
        "outcome": "retry outcome",
    }
    values.update(changes)
    return CapturePayload(**values)


def test_retry_payload_scrubs_remotes_paths_and_keeps_only_project_relative_paths(
    tmp_path,
):
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    inside = project / "src" / "inside.py"
    inside.parent.mkdir()
    inside.write_text("pass\n", encoding="utf-8")
    outside = tmp_path / "PRIVATE_OUTSIDE_MARKER"
    outside.mkdir()
    (outside / "secret.py").write_text("pass\n", encoding="utf-8")
    (project / "escape").symlink_to(outside, target_is_directory=True)
    private_text = " ".join(
        (
            "https://PRIVATE_USER:RAW_PASSWORD@example.invalid/private.git"
            "?token=RAW_QUERY#RAW_FRAGMENT",
            "git@github.com:private-org/RAW_REMOTE.git",
            "/Users/PRIVATE_PATH_USER/Documents/PRIVATE_PROJECT/file.py",
            '"/Users/PRIVATE SPACE USER/Documents/PRIVATE SPACED PROJECT/file.py"',
            r"\\PRIVATE_SERVER_MARKER\PRIVATE_SHARE_MARKER\file.py",
            "github.com:PRIVATE_SCP_ORG/RAW_OPTIONAL_USER_REMOTE.git",
            "www.private-schemeless-host.invalid/private?token=RAW_SCHEMELESS_QUERY",
            "work:PRIVATE_ALIAS_ORG/RAW_ALIAS_REMOTE.git",
            "127.0.0.1/private?token=RAW_IP_QUERY",
            "[2001:db8::1]/private?token=RAW_IPV6_QUERY",
            "localhost/private?token=RAW_LOCALHOST_QUERY",
            "cwd:/Users/PRIVATE_LABELED_PATH/Documents/project/file.py",
            "PRIVATE_USER@localhost/private?token=RAW_LOCAL_USERINFO_QUERY",
            "PRIVATE_USER@intranet/private?token=RAW_INTRANET_USERINFO_QUERY",
        )
    )
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(
        _payload(
            project,
            "retry-private",
            objective=private_text,
            outcome=private_text,
            decisions=[
                private_text,
                "github.com:PRIVATE_SCP_ORG/RAW_OPTIONAL_USER_REMOTE.git",
                "www.private-schemeless-host.invalid/private?token=RAW_SCHEMELESS_QUERY",
                "work:PRIVATE_ALIAS_ORG/RAW_ALIAS_REMOTE.git",
                "127.0.0.1/private?token=RAW_IP_QUERY",
                "[2001:db8::1]/private?token=RAW_IPV6_QUERY",
                "localhost/private?token=RAW_LOCALHOST_QUERY",
                "cwd:/Users/PRIVATE_LABELED_PATH/Documents/project/file.py",
                "PRIVATE_USER@localhost/private?token=RAW_LOCAL_USERINFO_QUERY",
                "PRIVATE_USER@intranet/private?token=RAW_INTRANET_USERINFO_QUERY",
            ],
            failed_attempts=[private_text],
            verified_commands=[private_text],
            changed_paths=[
                "src/inside.py",
                str(inside),
                "../PRIVATE_TRAVERSAL_MARKER/secret.py",
                str(outside / "secret.py"),
                "escape/secret.py",
                r"C:\PRIVATE_WINDOWS_MARKER\secret.py",
            ],
            preferences=[private_text],
            risks=[private_text],
            open_issues=[private_text],
            reusable_lessons=[private_text],
        ),
        "operational_failure",
    )

    with database.connect(readonly=True) as connection:
        payload_json = connection.execute("select payload_json from retry_items").fetchone()[0]
    stored = json.loads(payload_json)
    assert stored["namespace"]["model_id"] == "provider/gpt-5"
    assert stored["changed_paths"] == ["src/inside.py", "src/inside.py"]
    for marker in (
        "PRIVATE_USER",
        "RAW_PASSWORD",
        "example.invalid",
        "RAW_QUERY",
        "RAW_FRAGMENT",
        "private-org",
        "RAW_REMOTE",
        "PRIVATE_PATH_USER",
        "PRIVATE_PROJECT",
        "PRIVATE SPACE USER",
        "PRIVATE SPACED PROJECT",
        "PRIVATE_OUTSIDE_MARKER",
        "PRIVATE_TRAVERSAL_MARKER",
        "PRIVATE_WINDOWS_MARKER",
        "PRIVATE_SERVER_MARKER",
        "PRIVATE_SHARE_MARKER",
        "PRIVATE_SCP_ORG",
        "RAW_OPTIONAL_USER_REMOTE",
        "private-schemeless-host",
        "RAW_SCHEMELESS_QUERY",
        "PRIVATE_ALIAS_ORG",
        "RAW_ALIAS_REMOTE",
        "RAW_IP_QUERY",
        "RAW_IPV6_QUERY",
        "RAW_LOCALHOST_QUERY",
        "PRIVATE_LABELED_PATH",
        "RAW_LOCAL_USERINFO_QUERY",
        "RAW_INTRANET_USERINFO_QUERY",
        str(project),
    ):
        assert marker not in payload_json

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (
        1,
        0,
        0,
    )
    with database.connect(readonly=True) as connection:
        pending = connection.execute(
            "select claimed_model_id, structured_payload_json from pending_captures"
        ).fetchone()
    assert pending["claimed_model_id"] == "provider/gpt-5"
    for marker in (
        "example.invalid",
        "RAW_QUERY",
        "RAW_REMOTE",
        "PRIVATE_PATH_USER",
        "PRIVATE_OUTSIDE_MARKER",
    ):
        assert marker not in pending["structured_payload_json"]


def test_direct_retry_and_trusted_capture_share_one_private_canonical_hash(tmp_path):
    stacks = {}
    for name in ("direct", "retry", "trusted"):
        root = tmp_path / name
        root.mkdir()
        stacks[name] = _capture_stack(root)

    def private_payload(project):
        return _payload(
            project,
            "canonical-private",
            objective=(
                "Inspect /Users/PRIVATE_DIRECT_USER/project/file.py; "
                "git clone --filter=blob:none --branch 'research&dev' "
                "intranet:PRIVATE_OPTION_REPO; "
                "Choice A:enabled"
            ),
            outcome=(
                "Fetched s3://PRIVATE_DIRECT_BUCKET/PRIVATE_DIRECT_OBJECT; "
                "git clone `intranet:PRIVATE_BACKTICK_REPO`"
            ),
            decisions=["Keep \\Users\\PRIVATE_DIRECT_DECISION\\file.py private"],
            changed_paths=["src/inside.py"],
            reusable_lessons=["Never expose intranet:PRIVATE_DIRECT_ORG/repo.git"],
        )

    direct_database, _runtime, direct_project, _projects, _redactor, direct_capture = stacks[
        "direct"
    ]
    direct_payload = private_payload(direct_project)
    assert direct_capture.capture(direct_payload).status == "pending_verification"
    with direct_database.connect(readonly=True) as connection:
        direct = connection.execute(
            """
            select structured_payload_json, structured_hash
            from pending_captures where source_record_id = ?
            """,
            (direct_payload.source_record_id,),
        ).fetchone()

    retry_database, _runtime, retry_project, retry_projects, retry_redactor, retry_capture = stacks[
        "retry"
    ]
    retry_payload = private_payload(retry_project)
    retry_queue = RetryQueue(retry_database, retry_projects, retry_redactor)
    retry_queue.enqueue(retry_payload, "operational_failure")
    report = retry_queue.drain(retry_capture)
    assert (report.completed_count, report.failed_count, report.remaining_count) == (1, 0, 0)
    with retry_database.connect(readonly=True) as connection:
        retry = connection.execute(
            """
            select structured_payload_json, structured_hash
            from pending_captures where source_record_id = ?
            """,
            (retry_payload.source_record_id,),
        ).fetchone()

    trusted_database, _runtime, trusted_project, _projects, _redactor, trusted_capture = stacks[
        "trusted"
    ]
    trusted_payload = private_payload(trusted_project)
    verification = NamespaceVerification(
        namespace=trusted_payload.namespace,
        source_record_id=trusted_payload.source_record_id,
        verified_by="codex_adapter",
        verified_at=datetime(2026, 7, 15, tzinfo=timezone.utc),
    )
    assert trusted_capture.capture(trusted_payload, verification).status == "inserted"
    with trusted_database.connect(readonly=True) as connection:
        trusted_hash = connection.execute(
            "select content_hash from source_refs where source_record_id = ?",
            (trusted_payload.source_record_id,),
        ).fetchone()[0]

    assert direct["structured_payload_json"] == retry["structured_payload_json"]
    assert direct["structured_hash"] == retry["structured_hash"] == trusted_hash
    assert (
        direct["structured_hash"]
        == hashlib.sha256(direct["structured_payload_json"].encode("utf-8")).hexdigest()
    )
    for marker in (
        "PRIVATE_DIRECT_USER",
        "PRIVATE_DIRECT_BUCKET",
        "PRIVATE_DIRECT_OBJECT",
        "PRIVATE_DIRECT_DECISION",
        "PRIVATE_DIRECT_ORG",
        "PRIVATE_OPTION_REPO",
        "PRIVATE_BACKTICK_REPO",
    ):
        assert marker not in direct["structured_payload_json"]
        assert marker not in retry["structured_payload_json"]
    assert "Choice A:enabled" in direct["structured_payload_json"]
    assert "Choice A:enabled" in retry["structured_payload_json"]
    assert "--filter=blob:none" not in direct["structured_payload_json"]
    assert "--filter=blob:none" not in retry["structured_payload_json"]
    assert "REDACTED:remote_command" in direct["structured_payload_json"]
    assert "REDACTED:remote_command" in retry["structured_payload_json"]


def test_retry_enqueue_marks_the_current_privacy_schema(tmp_path):
    database, _runtime, project, projects, redactor, _capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)

    queue.enqueue(_payload(project, "retry-privacy-version"), "operational_failure")

    with database.connect(readonly=True) as connection:
        stored = json.loads(
            connection.execute("select payload_json from retry_items").fetchone()[0]
        )
    assert stored["privacy_version"] == 2


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
        stored = json.loads(
            connection.execute("select structured_payload_json from pending_captures").fetchone()[0]
        )
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
        stored = json.loads(
            connection.execute("select structured_payload_json from pending_captures").fetchone()[0]
        )
    assert stored["resolved_open_issues"] == ["exact old issue"]


def test_retry_v2_resolution_only_stays_pending_without_resolution_side_effects(
    tmp_path: Path,
) -> None:
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    old_issue = _payload(
        project,
        "retry-resolution-target",
        objective="",
        outcome="",
        open_issues=["exact old issue"],
    )
    verified_at = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    seeded = capture.capture(
        old_issue,
        NamespaceVerification(
            namespace=old_issue.namespace,
            source_record_id=old_issue.source_record_id,
            verified_by="codex_adapter",
            verified_at=verified_at,
        ),
    )
    assert seeded.status == "inserted"
    assert len(seeded.inserted_ids) == 1
    target_id = seeded.inserted_ids[0]
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(
        _payload(
            project,
            "retry-v2-resolution-only",
            objective="",
            outcome="",
            resolved_open_issues=["exact old issue"],
        ),
        "operational_failure",
    )

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (1, 0, 0)
    with database.connect(readonly=True) as connection:
        pending_rows = connection.execute(
            "select structured_payload_json from pending_captures"
        ).fetchall()
        target = connection.execute(
            "select lifecycle_state from behavior_memories where memory_id = ?",
            (str(target_id).lower(),),
        ).fetchone()
        audit_count = connection.execute(
            "select count(*) from memory_issue_resolutions"
        ).fetchone()[0]
    assert len(pending_rows) == 1
    stored = json.loads(pending_rows[0]["structured_payload_json"])
    assert stored["resolved_open_issues"] == ["exact old issue"]
    assert target is not None
    assert target["lifecycle_state"] == "active"
    assert audit_count == 0


def test_direct_and_retry_capture_share_the_same_payload_envelope(tmp_path):
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    bounded_items = ["x" * (32 * 1024)] * 5

    direct_payload = _payload(
        project,
        "direct-bounded-envelope",
        decisions=bounded_items,
    )
    retry_payload = _payload(
        project,
        "retry-bounded-envelope",
        decisions=bounded_items,
    )

    assert capture.capture(direct_payload).status == "pending_verification"
    queue.enqueue(retry_payload, "operational_failure")

    expanding_value = ("localhost:x " * 900).strip()
    expanding_items = [expanding_value] * 5
    expanding_direct = _payload(
        project,
        "direct-expanding-envelope",
        objective=expanding_value,
        outcome=expanding_value,
        decisions=expanding_items,
    )
    expanding_retry = _payload(
        project,
        "retry-expanding-envelope",
        objective=expanding_value,
        outcome=expanding_value,
        decisions=expanding_items,
    )

    assert capture.capture(expanding_direct).status == "pending_verification"
    queue.enqueue(expanding_retry, "operational_failure")

    with database.connect(readonly=True) as connection:
        retry_document = connection.execute(
            "select payload_json from retry_items where payload_json like ?",
            ('%"source_record_id":"retry-expanding-envelope"%',),
        ).fetchone()[0]
    assert len(retry_document.encode("utf-8")) <= 256 * 1024

    report = queue.drain(capture)
    assert (report.completed_count, report.failed_count) == (2, 0)
    with database.connect(readonly=True) as connection:
        direct_row = connection.execute(
            """
            select structured_payload_json, structured_hash from pending_captures
            where source_record_id = ?
            """,
            (expanding_direct.source_record_id,),
        ).fetchone()
        retry_row = connection.execute(
            """
            select structured_payload_json, structured_hash from pending_captures
            where source_record_id = ?
            """,
            (expanding_retry.source_record_id,),
        ).fetchone()
    assert direct_row["structured_payload_json"] == retry_row["structured_payload_json"]
    assert direct_row["structured_hash"] == retry_row["structured_hash"]
    assert 192 * 1024 < len(direct_row["structured_payload_json"].encode("utf-8"))
    assert len(direct_row["structured_payload_json"].encode("utf-8")) <= 224 * 1024

    expanding_trusted = _payload(
        project,
        "trusted-expanding-envelope",
        objective=expanding_value,
        outcome=expanding_value,
        decisions=expanding_items,
    )
    verification = NamespaceVerification(
        namespace=expanding_trusted.namespace,
        source_record_id=expanding_trusted.source_record_id,
        verified_by="codex_adapter",
        verified_at=datetime.now(timezone.utc),
    )
    assert capture.capture(expanding_trusted, verification).status == "inserted"

    with database.connect(readonly=True) as connection:
        trusted_hash = connection.execute(
            "select content_hash from source_refs where source_record_id = ?",
            (expanding_trusted.source_record_id,),
        ).fetchone()[0]
    assert direct_row["structured_hash"] == trusted_hash

    oversized_items = ["x" * (32 * 1024)] * 6
    oversized_direct = _payload(
        project,
        "direct-oversized-envelope",
        decisions=oversized_items,
    )
    oversized_retry = _payload(
        project,
        "retry-oversized-envelope",
        decisions=oversized_items,
    )

    with pytest.raises(ValueError, match="capture payload exceeds bound"):
        capture.capture(oversized_direct)
    with pytest.raises(ValueError, match="capture payload exceeds bound"):
        queue.enqueue(oversized_retry, "operational_failure")


def test_retry_drain_migrates_a_legacy_private_structure_before_capture(tmp_path):
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(
        _payload(
            project,
            "retry-legacy-private-structure",
            objective="fix   parser",
            outcome="done\n\nnow",
        ),
        "operational_failure",
    )
    with database.transaction() as connection:
        row = connection.execute("select retry_id, payload_json from retry_items").fetchone()
        legacy = json.loads(row["payload_json"])
        legacy.pop("privacy_version", None)
        legacy.pop("resolved_open_issues", None)
        legacy["objective"] = "fix   parser"
        legacy["outcome"] = "done\n\nnow"
        connection.execute(
            "update retry_items set payload_json = ? where retry_id = ?",
            (
                json.dumps(
                    legacy,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                row["retry_id"],
            ),
        )

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (1, 0, 0)
    with database.connect(readonly=True) as connection:
        pending = connection.execute(
            "select structured_payload_json from pending_captures"
        ).fetchone()
        assert connection.execute("select count(*) from retry_items").fetchone()[0] == 0
    assert json.loads(pending["structured_payload_json"])["objective"] == "fix parser"
    assert json.loads(pending["structured_payload_json"])["outcome"] == "done now"


def test_retry_drain_preserves_a_legacy_bare_colon_model_namespace(tmp_path):
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(
        CapturePayload(
            cwd=project,
            namespace=Namespace(source_agent="codex", model_id="ollama/llama3:8b"),
            source_record_id="retry-legacy-bare-colon-model",
            objective="legacy model",
            outcome="done",
        ),
        "operational_failure",
    )
    with database.transaction() as connection:
        row = connection.execute("select retry_id, payload_json from retry_items").fetchone()
        legacy = json.loads(row["payload_json"])
        legacy.pop("privacy_version", None)
        legacy.pop("resolved_open_issues", None)
        legacy["namespace"]["model_id"] = "llama3:8b"
        connection.execute(
            "update retry_items set payload_json = ? where retry_id = ?",
            (
                json.dumps(legacy, sort_keys=True, separators=(",", ":")),
                row["retry_id"],
            ),
        )

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (1, 0, 0)
    with database.connect(readonly=True) as connection:
        assert (
            connection.execute("select claimed_model_id from pending_captures").fetchone()[0]
            == "llama3:8b"
        )


def test_retry_drain_rejects_a_project_root_replaced_by_a_symlink(tmp_path):
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(_payload(project, "retry-replaced-root"), "operational_failure")
    moved_project = tmp_path / "moved-project"
    replacement = tmp_path / "replacement"
    replacement.mkdir()
    project.rename(moved_project)
    project.symlink_to(replacement, target_is_directory=True)

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (0, 1, 1)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0


def test_retry_drain_rolls_back_when_the_project_root_changes_before_commit(
    tmp_path,
    monkeypatch,
):
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    queue.enqueue(_payload(project, "retry-root-race"), "operational_failure")
    moved_project = tmp_path / "moved-during-capture"
    replacement = tmp_path / "replacement-during-capture"
    replacement.mkdir()
    capture_on_connection = capture._capture_untrusted_on_connection

    def replace_root_after_capture(connection, payload, project_id):
        result = capture_on_connection(connection, payload, project_id)
        project.rename(moved_project)
        project.symlink_to(replacement, target_is_directory=True)
        return result

    monkeypatch.setattr(
        capture,
        "_capture_untrusted_on_connection",
        replace_root_after_capture,
    )

    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count, report.remaining_count) == (0, 1, 1)
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 0


def test_retry_model_id_preserves_namespace_valid_unicode_revision(tmp_path):
    database, _runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    model_id = "提供商/model@rev+β"
    queue = RetryQueue(database, projects, redactor)
    payload = CapturePayload(
        cwd=project,
        namespace=Namespace(source_agent="codex", model_id=model_id),
        source_record_id="retry-unicode-model",
        objective="retry model",
        outcome="done",
    )

    queue.enqueue(payload, "operational_failure")
    report = queue.drain(capture)

    assert (report.completed_count, report.failed_count) == (1, 0)
    with database.connect(readonly=True) as connection:
        stored = connection.execute("select claimed_model_id from pending_captures").fetchone()[0]
    assert stored == model_id


@pytest.mark.parametrize(
    "code_fact",
    (
        (
            "Fix app.py and utils.py for release.2026; "
            "path:src/app.py model:provider/gpt-5 route:/api/v1"
        ),
        "Failure at app.py:42 in package.module",
        "config.yaml:production",
        "src.app.py:42",
        "package.sub.module:Class",
        "config.prod.yaml:production",
        "GET /api/v1/users returned 404",
        "GET /users/123 returned 200",
        "Call /api/v1/users",
        "Use /healthz",
        "Fetch '/v1/items'",
        'POST "/api/v1/users" returned 201',
        "Route /healthz and endpoint /v1/items are public API names",
    ),
)
def test_retry_text_preserves_code_diagnostics_that_are_not_private(tmp_path, code_fact):
    database, _runtime, project, projects, redactor, _capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)

    queue.enqueue(
        _payload(project, "retry-code-fact", objective=code_fact, outcome=code_fact),
        "operational_failure",
    )

    with database.connect(readonly=True) as connection:
        stored = json.loads(
            connection.execute("select payload_json from retry_items").fetchone()[0]
        )
    assert stored["objective"] == stored["outcome"] == code_fact


@pytest.mark.parametrize(
    ("private_fact", "marker", "preserved_fragment"),
    (
        (
            "cwd:/Users/PRIVATE_LABELED_PATH/Documents/project/file.py, then continue",
            "PRIVATE_LABELED_PATH",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE_ABSOLUTE_USER/Documents/project/file.py; keep diagnostic",
            "PRIVATE_ABSOLUTE_USER",
            "; keep diagnostic",
        ),
        (
            "Open /Users/PRIVATE_API_PATH/project/file.py API credential file",
            "PRIVATE_API_PATH",
            " API credential file",
        ),
        (
            "Route /Users/PRIVATE_ROUTE_LEAK/Documents/project/file.py",
            "PRIVATE_ROUTE_LEAK",
            "Route ",
        ),
        (
            'Route "/api/v1 /Users/PRIVATE_NESTED_ROUTE/file.py"',
            "PRIVATE_NESTED_ROUTE",
            "Route ",
        ),
        (
            "Open /Users/PRIVATE USER_PATH_MARKER/Documents/PRIVATE "
            "PROJECT_PATH_MARKER/file.py, then continue",
            "USER_PATH_MARKER",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE/Project (PRIVATE_COPY_MARKER)/file.py, then continue",
            "PRIVATE_COPY_MARKER",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE USER/Documents/PRIVATE FILE_PATH_MARKER.py, then continue",
            "FILE_PATH_MARKER",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE FINAL FILE_MULTI_MARKER.py, then continue",
            "FILE_MULTI_MARKER",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE/Project My FINAL_COMPONENT_MARKER/Documents/file.py, "
            "then continue",
            "FINAL_COMPONENT_MARKER",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE/Project(PRIVATE_ATTACHED_MARKER)/file.py, then continue",
            "PRIVATE_ATTACHED_MARKER",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE/Project[PRIVATE_BRACKET_MARKER]/file.py, then continue",
            "PRIVATE_BRACKET_MARKER",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE/Report(PRIVATE_FINAL_GROUP).pdf, then continue",
            "PRIVATE_FINAL_GROUP",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE/Report[PRIVATE_FINAL_BRACKET].pdf, then continue",
            "PRIVATE_FINAL_BRACKET",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE_RELEASE_PATH/file.py release.2026",
            "PRIVATE_RELEASE_PATH",
            " release.2026",
        ),
        (
            "Open /Users/PRIVATE/Q2.pdf PRIVATE_SIGNED_COPY.pdf, then continue",
            "PRIVATE_SIGNED_COPY",
            ", then continue",
        ),
        (
            "Open /Users/PRIVATE_ADJACENT_CODE/file.py app.py failed",
            "PRIVATE_ADJACENT_CODE",
            " app.py failed",
        ),
        (
            "Open /Users/PRIVATE_DIAGNOSTIC/app.py failed in utils.py, then continue",
            "PRIVATE_DIAGNOSTIC",
            " failed in utils.py, then continue",
        ),
        (
            "Open /Users/PRIVATE_INSTRUCTION/app.py then inspect src/app.py, then continue",
            "PRIVATE_INSTRUCTION",
            " then inspect src/app.py, then continue",
        ),
        (
            r"Open C:\PRIVATE_WINDOWS_USER\project\file.py (keep diagnostic)",
            "PRIVATE_WINDOWS_USER",
            " (keep diagnostic)",
        ),
        (
            r"Open C:\PRIVATE WINDOWS_PATH_MARKER\project\file.py, then continue",
            "WINDOWS_PATH_MARKER",
            ", then continue",
        ),
        (
            r"Open C:\PRIVATE USER\project\PRIVATE FILE_WINDOWS_MARKER.py, then continue",
            "FILE_WINDOWS_MARKER",
            ", then continue",
        ),
        (
            r"Open C:\PRIVATE FINAL FILE_WINDOWS_MULTI.py, then continue",
            "FILE_WINDOWS_MULTI",
            ", then continue",
        ),
        (
            r"Open C:\Users\PRIVATE\Q2.pdf PRIVATE_WINDOWS_COPY.pdf, then continue",
            "PRIVATE_WINDOWS_COPY",
            ", then continue",
        ),
        (
            r"Open \\PRIVATE_SERVER\PRIVATE_SHARE\file.py, keep UNC note",
            "PRIVATE_SERVER",
            ", keep UNC note",
        ),
        (
            r"Open \\PRIVATE SERVER_PATH_MARKER\share\file.py, then continue",
            "SERVER_PATH_MARKER",
            ", then continue",
        ),
        (
            r"Open \\PRIVATE SERVER\share\PRIVATE FILE_UNC_MARKER.py, then continue",
            "FILE_UNC_MARKER",
            ", then continue",
        ),
        (
            r"Open \\PRIVATE FINAL FILE_UNC_MULTI.py, then continue",
            "FILE_UNC_MULTI",
            ", then continue",
        ),
        (
            r"Open \\server\PRIVATE\Q2.pdf PRIVATE_UNC_COPY.pdf, then continue",
            "PRIVATE_UNC_COPY",
            ", then continue",
        ),
        (
            "git@github.com:PRIVATE_REMOTE_ORG/RAW_REMOTE.git",
            "PRIVATE_REMOTE_ORG",
            "",
        ),
        ("git@github.com:PRIVATE_BARE_REPO", "PRIVATE_BARE_REPO", ""),
        ("github.com:PRIVATE_HOST_REPO", "PRIVATE_HOST_REPO", ""),
        ("localhost:3000", "localhost:3000", ""),
        (
            "private.example.invalid:8443",
            "private.example.invalid:8443",
            "",
        ),
        (
            "PRIVATE_USER@localhost/private?token=RAW_LOCAL_QUERY",
            "PRIVATE_USER",
            "",
        ),
    ),
)
def test_retry_text_still_redacts_real_paths_and_remotes(
    tmp_path, private_fact, marker, preserved_fragment
):
    database, _runtime, project, projects, redactor, _capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)

    queue.enqueue(
        _payload(
            project,
            "retry-private-fact",
            objective=private_fact,
            outcome=private_fact,
        ),
        "operational_failure",
    )

    with database.connect(readonly=True) as connection:
        payload_json = connection.execute("select payload_json from retry_items").fetchone()[0]
    stored = json.loads(payload_json)
    assert marker not in payload_json
    assert "[REDACTED:" in payload_json
    assert preserved_fragment in stored["objective"]
    assert preserved_fragment in stored["outcome"]


@pytest.mark.parametrize(
    "model_id",
    (
        "/Users/PRIVATE_MODEL_PATH/model.bin",
        r"C:\PRIVATE_MODEL_PATH\model.bin",
        "https://private-model.example.invalid/model?token=RAW_MODEL_QUERY",
        "git@private-model.example:owner/RAW_MODEL_REMOTE.git",
        "work:PRIVATE_MODEL_ORG/RAW_MODEL_ALIAS.git",
        "private-model.example.invalid/owner/RAW_MODEL_PATH",
        "PRIVATE_USER@example.invalid/model",
        "provider/model?token=RAW_MODEL_QUERY",
        "provider/model#PRIVATE_MODEL_FRAGMENT",
        "127.0.0.1/private/model",
        "PRIVATE_USER@127.0.0.1/private/model",
        "[2001:db8::1]/private/model",
        "localhost/private/model",
        "../PRIVATE_MODEL_PATH",
        "./PRIVATE_MODEL_PATH",
        "cwd:/Users/PRIVATE_MODEL_LABELED_PATH/model.bin",
        "PRIVATE_USER@localhost/private/model",
        "PRIVATE_USER@intranet/private/model",
        "provider/password=RAW_MODEL_CREDENTIAL",
    ),
)
def test_retry_model_id_rejects_private_path_remote_and_credential_shapes(tmp_path, model_id):
    database, _runtime, project, projects, redactor, _capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor)
    payload = CapturePayload(
        cwd=project,
        namespace=Namespace(source_agent="codex", model_id=model_id),
        source_record_id="retry-private-model",
        objective="retry model",
        outcome="done",
    )

    with pytest.raises(ValueError, match="invalid model_id"):
        queue.enqueue(payload, "operational_failure")

    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from retry_items").fetchone()[0] == 0


def test_retry_enqueue_failure_preserves_the_original_transient_error(tmp_path, monkeypatch):
    original = sqlite3.OperationalError("ORIGINAL_TRANSIENT_MARKER")
    payload = _payload(tmp_path, "retry-original-error")

    class FailingCapture:
        @staticmethod
        def capture(_payload):
            raise original

    class FailingQueue:
        @staticmethod
        def enqueue(_payload, _reason):
            raise RuntimeError("QUEUE_FAILURE_MARKER")

    monkeypatch.setattr(cli_module, "_is_transient_database_error", lambda _error: True)
    with pytest.raises(sqlite3.OperationalError) as raised:
        _capture_with_transient_retry(
            SimpleNamespace(capture=FailingCapture(), retry_queue=FailingQueue()),
            payload,
        )

    assert raised.value is original
    assert str(raised.value) == "ORIGINAL_TRANSIENT_MARKER"


def test_retry_backlog_remains_due_and_replays_each_item_exactly_once(tmp_path):
    database, runtime, project, projects, redactor, capture = _capture_stack(tmp_path)
    queue = RetryQueue(database, projects, redactor, max_items_per_drain=100)
    for index in range(101):
        queue.enqueue(_payload(project, f"retry-backlog-{index}"), "operational_failure")
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    prior_success = now - timedelta(hours=1)
    service = ReconcileService(
        database,
        ProcessLock(runtime / "reconcile.lock"),
        retry_queue=queue,
        retry_capture=capture,
        now=lambda: now,
    )
    service.record_success(prior_success)

    first = service.run(force=True)

    assert first.status == "degraded"
    assert first.stages["retry"] == "warn"
    assert service.should_run(now=now) is True
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from retry_items").fetchone()[0] == 1
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 100
        success = json.loads(
            connection.execute(
                "select value_json from app_state where name='last_reconcile_success'"
            ).fetchone()[0]
        )
        report = json.loads(
            connection.execute(
                "select value_json from app_state where name='last_reconcile_report'"
            ).fetchone()[0]
        )
    assert success["timestamp"] == "2026-07-13T11:00:00Z"
    assert report["stage_metrics"]["retry"]["remaining_count"] == 1
    assert report["stage_errors"]["retry"] == "retry_backlog_remaining"

    second = service.run(force=False)

    assert second.status == "success"
    assert service.should_run(now=now) is False
    with database.connect(readonly=True) as connection:
        assert connection.execute("select count(*) from retry_items").fetchone()[0] == 0
        assert connection.execute("select count(*) from pending_captures").fetchone()[0] == 101
        assert (
            connection.execute(
                "select count(distinct source_record_id) from pending_captures"
            ).fetchone()[0]
            == 101
        )


def test_pending_backlog_remains_due_and_confirms_each_item_exactly_once(tmp_path):
    database, runtime, project, _projects, _redactor, capture = _capture_stack(tmp_path)
    capture.capture(_payload(project, "pending-backlog-0"))
    with database.transaction() as connection:
        base = dict(connection.execute("select * from pending_captures").fetchone())
        base["expires_at"] = "2020-01-01T00:00:00.000000Z"
        connection.execute(
            "update pending_captures set expires_at = ?",
            (base["expires_at"],),
        )
        connection.executemany(
            """
            insert into pending_captures(
                pending_id, project_id, claimed_source_agent, claimed_model_id,
                source_record_id, structured_payload_json, structured_hash,
                created_at, expires_at, verification_state
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    str(uuid4()),
                    base["project_id"],
                    base["claimed_source_agent"],
                    base["claimed_model_id"],
                    f"pending-backlog-{index}",
                    base["structured_payload_json"],
                    base["structured_hash"],
                    base["created_at"],
                    base["expires_at"],
                    "pending",
                )
                for index in range(1, 1001)
            ),
        )
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    service = ReconcileService.minimal(
        database, ProcessLock(runtime / "reconcile.lock"), now=lambda: now
    )
    service.record_success(now - timedelta(hours=1))

    first = service.run(force=True)

    assert first.status == "degraded"
    assert first.stages["pending"] == "warn"
    assert service.should_run(now=now) is True
    with database.connect(readonly=True) as connection:
        active_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
        history_states = dict(
            connection.execute(
                "select final_state, count(*) from pending_capture_history group by final_state"
            ).fetchall()
        )
        confirmations = connection.execute(
            "select count(*) from app_state where name like 'pending_confirmation:%'"
        ).fetchone()[0]
        report = json.loads(
            connection.execute(
                "select value_json from app_state where name='last_reconcile_report'"
            ).fetchone()[0]
        )
    assert active_count == 1
    assert history_states == {"expired": 1000}
    assert confirmations == 0
    assert report["stage_metrics"]["pending"]["remaining_count"] == 1
    assert report["stage_errors"]["pending"] == "pending_backlog_remaining"

    second = service.run(force=False)
    third = service.run(force=True)

    assert second.status == third.status == "success"
    with database.connect(readonly=True) as connection:
        active_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
        history_states = dict(
            connection.execute(
                "select final_state, count(*) from pending_capture_history group by final_state"
            ).fetchall()
        )
        confirmations = connection.execute(
            "select count(*) from app_state where name like 'pending_confirmation:%'"
        ).fetchone()[0]
    assert active_count == 0
    assert history_states == {"expired": 1001}
    assert confirmations == 0


def test_pending_expiry_uses_exact_subsecond_utc_boundary(tmp_path):
    database, runtime, project, _projects, _redactor, capture = _capture_stack(tmp_path)
    capture.capture(_payload(project, "pending-subsecond-expiry"))
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    expires_at = now + timedelta(microseconds=500_000)
    with database.transaction() as connection:
        connection.execute(
            "update pending_captures set expires_at = ?",
            (expires_at.isoformat(timespec="microseconds").replace("+00:00", "Z"),),
        )

    before = ReconcileService.minimal(
        database, ProcessLock(runtime / "reconcile.lock"), now=lambda: now
    ).run(force=True)
    with database.connect(readonly=True) as connection:
        state_before = connection.execute(
            "select verification_state from pending_captures"
        ).fetchone()[0]

    at_boundary = ReconcileService.minimal(
        database, ProcessLock(runtime / "reconcile.lock"), now=lambda: expires_at
    ).run(force=True)
    with database.connect(readonly=True) as connection:
        active_after = connection.execute("select count(*) from pending_captures").fetchone()[0]
        history_after = connection.execute(
            "select final_state from pending_capture_history"
        ).fetchone()[0]

    assert before.status == at_boundary.status == "success"
    assert state_before == "pending"
    assert active_after == 0
    assert history_after == "expired"


@pytest.mark.parametrize(
    ("offset", "expected_state"),
    (
        (timedelta(hours=-24, microseconds=-1), "pending"),
        (timedelta(hours=-24), "verified"),
        (timedelta(hours=24), "verified"),
        (timedelta(hours=24, microseconds=1), "pending"),
    ),
)
def test_pending_verification_window_is_microsecond_exact(tmp_path, offset, expected_state):
    database, _runtime, project, _projects, _redactor, capture = _capture_stack(tmp_path)
    capture.capture(_payload(project, "pending-window"))
    verified_at = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    created_at = (verified_at + offset).isoformat(timespec="microseconds").replace("+00:00", "Z")
    with database.transaction() as connection:
        connection.execute("update pending_captures set created_at = ?", (created_at,))

    result = capture.capture(
        _payload(project, "trusted-window"),
        NamespaceVerification(
            namespace=Namespace(source_agent="codex", model_id="provider/gpt-5"),
            source_record_id="trusted-window",
            verified_by="codex_adapter",
            verified_at=verified_at,
        ),
    )

    assert result.status == "inserted"
    with database.connect(readonly=True) as connection:
        active_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
        history_count = connection.execute(
            "select count(*) from pending_capture_history where final_state = 'verified'"
        ).fetchone()[0]
    state = "verified" if history_count else "pending"
    assert state == expected_state
    assert active_count + history_count == 1


def test_pending_verification_window_normalizes_timezone_offsets(tmp_path):
    database, _runtime, project, _projects, _redactor, capture = _capture_stack(tmp_path)
    capture.capture(_payload(project, "pending-timezone"))
    verified_at = datetime(2026, 7, 13, 12, tzinfo=timezone(timedelta(hours=8)))
    boundary = (verified_at.astimezone(timezone.utc) - timedelta(hours=24)).astimezone(
        timezone(timedelta(hours=-5))
    )
    with database.transaction() as connection:
        connection.execute(
            "update pending_captures set created_at = ?",
            (boundary.isoformat(timespec="microseconds"),),
        )

    capture.capture(
        _payload(project, "trusted-timezone"),
        NamespaceVerification(
            namespace=Namespace(source_agent="codex", model_id="provider/gpt-5"),
            source_record_id="trusted-timezone",
            verified_by="codex_adapter",
            verified_at=verified_at,
        ),
    )

    with database.connect(readonly=True) as connection:
        active_count = connection.execute("select count(*) from pending_captures").fetchone()[0]
        history_state = connection.execute(
            "select final_state from pending_capture_history"
        ).fetchone()[0]
    assert active_count == 0
    assert history_state == "verified"


def _chatgpt_config(tmp_path: Path, project: Path) -> Path:
    config = tmp_path / "runtime" / "config.toml"
    config.parent.mkdir(mode=0o700)
    ConfigManager(config).save(
        AppConfig(
            project_roots=(project,),
            enabled_sources=("chatgpt",),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    return config


@pytest.mark.parametrize("inbox_kind", ("file", "symlink", "fifo", "unreadable"))
def test_real_cli_rejects_existing_invalid_chatgpt_inbox_and_remains_due(tmp_path, inbox_kind):
    project = tmp_path / "project"
    project.mkdir()
    config = _chatgpt_config(tmp_path, project)
    now = datetime(2026, 7, 13, 12, tzinfo=timezone.utc)
    with build_container(config) as container:
        container.reconcile.record_success(now - timedelta(hours=1))
    inbox = config.parent / "imports" / "chatgpt"
    restore_mode = False
    if inbox_kind == "file":
        inbox.write_text("PRIVATE_INBOX_PATH_MARKER", encoding="utf-8")
    elif inbox_kind == "symlink":
        target = tmp_path / "PRIVATE_INBOX_TARGET_MARKER"
        target.mkdir()
        inbox.symlink_to(target, target_is_directory=True)
    elif inbox_kind == "fifo":
        os.mkfifo(inbox)
    else:
        inbox.mkdir(mode=0o700)
        inbox.chmod(0)
        restore_mode = True
        if os.access(inbox, os.R_OK):
            inbox.chmod(0o700)
            pytest.skip("current user can still read chmod-000 directories")

    try:
        result = runner.invoke(
            app,
            ["--config", str(config), "reconcile", "--force", "--format", "json"],
        )
    finally:
        if restore_mode:
            inbox.chmod(0o700)

    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)
    assert output["status"] == "degraded"
    assert output["stages"]["chatgpt"] == "error"
    assert "PRIVATE_INBOX" not in result.stdout
    with build_container(config) as container:
        assert container.reconcile.should_run(now=now) is True
        with container.database.connect(readonly=True) as connection:
            success = json.loads(
                connection.execute(
                    "select value_json from app_state where name='last_reconcile_success'"
                ).fetchone()[0]
            )
            report = json.loads(
                connection.execute(
                    "select value_json from app_state where name='last_reconcile_report'"
                ).fetchone()[0]
            )
    assert success["timestamp"] == "2026-07-13T11:00:00Z"
    assert report["stage_errors"]["chatgpt"] == "inbox_rejected"

    due = runner.invoke(
        app,
        ["--config", str(config), "reconcile", "--if-due", "--format", "json"],
    )
    assert due.exit_code == 0, due.stdout
    assert json.loads(due.stdout)["status"] != "skipped"


def test_real_cli_treats_missing_chatgpt_inbox_as_healthy_empty(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    config = _chatgpt_config(tmp_path, project)

    result = runner.invoke(
        app,
        ["--config", str(config), "reconcile", "--force", "--format", "json"],
    )

    assert result.exit_code == 0, result.stdout
    output = json.loads(result.stdout)
    assert output["status"] == "success"
    assert output["stages"]["chatgpt"] == "pass"
