from __future__ import annotations

import asyncio
import json
import shutil
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from project_memory_hub.adapters.codex import CAPTURE_END, CAPTURE_START
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import ServiceContainer, build_container
from project_memory_hub.domain import (
    CapturePayload,
    LifecycleState,
    Namespace,
    RecallRequest,
    SourceAgent,
)
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.services.compaction import CompactionService
from project_memory_hub.services.tokens import ConservativeTokenCounter
from project_memory_hub.web.app import create_app
from tests.fixtures.chatgpt.build_fixtures import build_export, conversation
from tests.fixtures.repos.build_repos import (
    DefaultRootRepositories,
    build_default_root_repositories,
    build_git_repository,
    git_output,
)


CODEX_MODEL = "gpt-5-e2e"
CHATGPT_MODEL = "gpt-5-chatgpt-e2e"
CODEX_MARKER = "CODEX_ONLY_CACHE_PATH cache namespace retained"
CHATGPT_MARKER = "CHATGPT_ONLY_EXPORT_PATH cache namespace retained"
CURRENT_STATE = "CODEX_CURRENT_STATE cache pipeline is green"
VERIFIED_COMMAND = "pytest tests/e2e/test_memory_hub.py -q"
OPEN_ISSUE = "CODEX_OPEN_ISSUE verify cache eviction on the next release"
CHANGED_PATH = "src/cache.py"
REUSABLE_LESSON = "keep adapter labels aligned with capture fields"
FAILED_ATTEMPT = "cache probe initially used a stale fixture"
PREFERENCE = "prefer bounded deterministic cache checks"
RISK = 'password="unterminated'
API_KEY = "sk-proj-" + "A" * 24
BEARER_VALUE = "B" * 24
PASSWORD_VALUE = "local-password-e2e-73"
SEEDED_SECRETS = (API_KEY, BEARER_VALUE, PASSWORD_VALUE)
RESOLUTION_TARGET = "CODEX_RESOLUTION_TARGET retire the legacy cache probe"
RESOLUTION_UNRELATED = "CODEX_RESOLUTION_UNRELATED document the new cache metrics"
FOREIGN_CODEX_MODEL = "gpt-5-e2e-foreign"


def test_local_memory_hub_e2e_is_bounded_private_and_namespace_safe(
    tmp_path: Path,
    monkeypatch,
    record_property,
) -> None:
    home = tmp_path / "private-home"
    repositories = build_default_root_repositories(home)
    proposal_repository = build_git_repository(tmp_path / "proposal-repository")
    true_command = shutil.which("true")
    assert true_command is not None
    verification_argv = (str(Path(true_command).resolve(strict=True)),)
    decisions = _codex_decisions()
    verified_at = datetime.now(timezone.utc).replace(microsecond=0)
    codex_session = _write_codex_session(
        home,
        repositories.documents_project,
        decisions,
        verified_at,
    )
    monkeypatch.setenv("HOME", str(home))
    config_path = _save_config(
        tmp_path,
        home,
        proposal_repository,
        verification_argv,
    )

    with build_container(config_path) as container:
        assert container.config.project_roots == AppConfig.defaults(home).project_roots
        projects = _discover_register_and_scan(container, repositories)
        project = projects[repositories.documents_project]
        codex_namespace = Namespace(
            source_agent=SourceAgent.CODEX,
            model_id=CODEX_MODEL,
        )
        chatgpt_namespace = Namespace(
            source_agent=SourceAgent.CHATGPT,
            model_id=CHATGPT_MODEL,
        )

        pending = container.capture.capture(
            CapturePayload(
                cwd=project.canonical_path,
                namespace=codex_namespace,
                source_record_id="direct-capture-e2e",
                objective="stabilize the cache adapter",
                outcome=CURRENT_STATE,
                decisions=list(decisions),
                failed_attempts=[FAILED_ATTEMPT],
                verified_commands=[VERIFIED_COMMAND],
                changed_paths=[CHANGED_PATH],
                preferences=[PREFERENCE],
                risks=[RISK],
                open_issues=[OPEN_ISSUE],
                reusable_lessons=[REUSABLE_LESSON],
            )
        )
        assert pending.status == "pending_verification"
        assert (
            container.memories.list_scoped(
                project.project_id,
                codex_namespace,
            )
            == ()
        )
        assert _capture_lifecycle_states(container) == ("pending",)

        chatgpt_archive = _write_chatgpt_archive(container, project.canonical_path)
        first_reconcile = container.reconcile.run(force=True)

        assert first_reconcile.status == "success"
        assert first_reconcile.inserted_count == 2
        assert first_reconcile.stages["codex_0"] == "pass"
        assert first_reconcile.stages["chatgpt"] == "pass"
        assert _capture_lifecycle_states(container) == ("verified",)
        assert _capture_provenance(container) == {
            ("chatgpt", "conversation-e2e", "capture-v1"),
            ("codex", "session-e2e:turn-1", "capture-v1"),
        }
        assert _behavior_namespace_counts(container, project.project_id).keys() == {
            ("chatgpt", CHATGPT_MODEL),
            ("codex", CODEX_MODEL),
        }

        _assert_namespace_isolation(
            container,
            project.canonical_path,
            codex_namespace,
            chatgpt_namespace,
        )
        _assert_bounded_recall(
            container,
            project.project_id,
            project.canonical_path,
            codex_namespace,
            record_property,
        )
        _approve_exactly_one_shared_rule(
            container,
            project.project_id,
            codex_namespace,
        )

        before_second = _idempotence_counts(container)
        second_reconcile = container.reconcile.run(force=True)
        assert second_reconcile.status == "success"
        assert second_reconcile.inserted_count == 0
        assert _idempotence_counts(container) == before_second
        assert _approved_rule_count(container, project.project_id) == 1

        _compact_namespaces_after_22_days(
            container,
            project.project_id,
            codex_namespace,
            chatgpt_namespace,
        )
        _apply_safe_proposal(container, proposal_repository, verification_argv)
        rendered_html = asyncio.run(
            _assert_loopback_dashboard(
                container,
                project.project_id,
                codex_namespace,
                chatgpt_namespace,
            )
        )

        assert stat.S_IMODE(container.paths.root.stat().st_mode) == 0o700
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(container.paths.database.stat().st_mode) == 0o600
        assert stat.S_IMODE(container.paths.access_token.stat().st_mode) == 0o600
        assert stat.S_IMODE(codex_session.stat().st_mode) == 0o600
        assert stat.S_IMODE(chatgpt_archive.stat().st_mode) == 0o600
        assert not _contains_seeded_secret(_database_bytes(container))
        assert not _contains_seeded_secret(_log_bytes(container))
        assert not _contains_seeded_secret(rendered_html.encode("utf-8"))


def test_adapter_verified_issue_resolution_is_exact_private_and_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "private-home"
    repositories = build_default_root_repositories(home)
    project_root = home / "Documents"
    project = repositories.documents_project
    monkeypatch.setenv("HOME", str(home))
    config_path = _save_resolution_config(tmp_path, project_root)
    opened_at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=2)
    target_session = _write_resolution_codex_session(
        home,
        project,
        session_id="resolution-target-session",
        model_id=CODEX_MODEL,
        timestamp=opened_at,
        lines=(
            "Objective: record exact issue lifecycle fixtures",
            "Outcome: initial target namespace issues are verified",
            f"Open issue: {RESOLUTION_TARGET}",
            f"Open issue: {RESOLUTION_UNRELATED}",
        ),
    )
    _write_resolution_codex_session(
        home,
        project,
        session_id="resolution-foreign-model-session",
        model_id=FOREIGN_CODEX_MODEL,
        timestamp=opened_at,
        lines=(
            "Objective: retain a foreign model issue",
            "Outcome: foreign model issue is verified",
            f"Open issue: {RESOLUTION_TARGET}",
        ),
    )

    with build_container(config_path) as container:
        foreign_archive = _write_resolution_chatgpt_archive(container, project)
        initial = container.reconcile.run(force=True)
        initial_json = _last_reconcile_json(container)
        assert initial.status == "success", initial_json
        foreign_archive.unlink()
        registered = container.projects.find_by_cwd(project)
        assert registered is not None
        target_namespace = Namespace(source_agent=SourceAgent.CODEX, model_id=CODEX_MODEL)
        foreign_model_namespace = Namespace(
            source_agent=SourceAgent.CODEX,
            model_id=FOREIGN_CODEX_MODEL,
        )
        foreign_source_namespace = Namespace(
            source_agent=SourceAgent.CHATGPT,
            model_id=CODEX_MODEL,
        )
        target = _memory_with_content(
            container,
            registered.project_id,
            target_namespace,
            RESOLUTION_TARGET,
        )
        unrelated = _memory_with_content(
            container,
            registered.project_id,
            target_namespace,
            RESOLUTION_UNRELATED,
        )
        foreign_model = _memory_with_content(
            container,
            registered.project_id,
            foreign_model_namespace,
            RESOLUTION_TARGET,
        )
        foreign_source = _memory_with_content(
            container,
            registered.project_id,
            foreign_source_namespace,
            RESOLUTION_TARGET,
        )
        assert {
            target.lifecycle_state,
            unrelated.lifecycle_state,
            foreign_model.lifecycle_state,
            foreign_source.lifecycle_state,
        } == {LifecycleState.ACTIVE}

        _append_resolution_codex_turn(
            target_session,
            project,
            turn_id="resolution-turn-2",
            model_id=CODEX_MODEL,
            timestamp=opened_at + timedelta(minutes=1),
            lines=(
                "Objective: close one exact verified issue",
                "Outcome: target issue lifecycle is closed",
                f"Resolved issue: {RESOLUTION_TARGET}",
            ),
        )
        resolved = container.reconcile.run(force=True)
        resolved_json = _last_reconcile_json(container)

        assert resolved.status == "success"
        assert resolved.warning_count == 0
        resolved_codex = resolved_json["stage_metrics"]["codex_0"]
        assert {
            key: resolved_codex[key]
            for key in (
                "resolved_count",
                "already_resolved_count",
                "unmatched_resolution_count",
            )
        } == {
            "resolved_count": 1,
            "already_resolved_count": 0,
            "unmatched_resolution_count": 0,
        }
        assert (
            container.memories.get_scoped(
                registered.project_id,
                target_namespace,
                target.memory_id,
            ).lifecycle_state
            == LifecycleState.ARCHIVED
        )
        assert (
            container.memories.get_scoped(
                registered.project_id,
                target_namespace,
                unrelated.memory_id,
            ).lifecycle_state
            == LifecycleState.ACTIVE
        )
        assert (
            RESOLUTION_UNRELATED
            in _recall(
                container,
                project,
                target_namespace,
                RESOLUTION_UNRELATED,
            ).text
        )
        for namespace, memory_id in (
            (foreign_model_namespace, foreign_model.memory_id),
            (foreign_source_namespace, foreign_source.memory_id),
        ):
            assert (
                container.memories.get_scoped(
                    registered.project_id,
                    namespace,
                    memory_id,
                ).lifecycle_state
                == LifecycleState.ACTIVE
            )

        audit_rows = _resolution_audit_rows(container)
        assert len(audit_rows) == 1
        assert audit_rows[0]["status"] == "resolved"
        assert audit_rows[0]["target_memory_id"] == str(target.memory_id)
        assert RESOLUTION_TARGET not in json.dumps(audit_rows, sort_keys=True)
        assert RESOLUTION_TARGET not in json.dumps(resolved_json, sort_keys=True)

        audit_count = len(audit_rows)
        replay = container.reconcile.run(force=True)
        replay_json = _last_reconcile_json(container)
        replay_codex = replay_json["stage_metrics"]["codex_0"]
        assert replay.status == "success"
        assert replay.inserted_count == 0
        assert replay.duplicate_count == 0
        assert replay.warning_count == 0
        for stage in (replay_codex, replay_json["stage_metrics"]["chatgpt"]):
            assert {
                key: stage[key]
                for key in (
                    "inserted_count",
                    "duplicate_count",
                    "resolved_count",
                    "already_resolved_count",
                    "unmatched_resolution_count",
                    "warning_count",
                )
            } == {
                "inserted_count": 0,
                "duplicate_count": 0,
                "resolved_count": 0,
                "already_resolved_count": 0,
                "unmatched_resolution_count": 0,
                "warning_count": 0,
            }
        assert len(_resolution_audit_rows(container)) == audit_count
        assert RESOLUTION_TARGET not in json.dumps(replay_json, sort_keys=True)


def _save_resolution_config(tmp_path: Path, project_root: Path) -> Path:
    runtime = tmp_path / "resolution-runtime"
    runtime.mkdir(mode=0o700)
    config_path = runtime / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    return config_path


def _capture_marker(lines: tuple[str, ...]) -> str:
    return "\n".join((CAPTURE_START, *lines, CAPTURE_END))


def _write_resolution_codex_session(
    home: Path,
    project: Path,
    *,
    session_id: str,
    model_id: str,
    timestamp: datetime,
    lines: tuple[str, ...],
) -> Path:
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
    session = sessions / f"{session_id}.jsonl"
    timestamp_text = timestamp.isoformat().replace("+00:00", "Z")
    records = (
        {
            "timestamp": timestamp_text,
            "type": "session_meta",
            "payload": {"id": session_id},
        },
        {
            "timestamp": timestamp_text,
            "type": "turn_context",
            "payload": {
                "turn_id": "resolution-turn-1",
                "cwd": str(project),
                "model": model_id,
                "summary": "seed exact issue lifecycle",
            },
        },
        {
            "timestamp": timestamp_text,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "resolution-turn-1",
                "last_agent_message": _capture_marker(lines),
            },
        },
    )
    session.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
        newline="\n",
    )
    session.chmod(0o600)
    return session


def _append_resolution_codex_turn(
    session: Path,
    project: Path,
    *,
    turn_id: str,
    model_id: str,
    timestamp: datetime,
    lines: tuple[str, ...],
) -> None:
    timestamp_text = timestamp.isoformat().replace("+00:00", "Z")
    records = (
        {
            "timestamp": timestamp_text,
            "type": "turn_context",
            "payload": {
                "turn_id": turn_id,
                "cwd": str(project),
                "model": model_id,
                "summary": "resolve one exact issue",
            },
        },
        {
            "timestamp": timestamp_text,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": turn_id,
                "last_agent_message": _capture_marker(lines),
            },
        },
    )
    with session.open("a", encoding="utf-8", newline="\n") as stream:
        for record in records:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def _write_resolution_chatgpt_archive(
    container: ServiceContainer,
    project: Path,
) -> Path:
    inbox = container.paths.imports / "chatgpt"
    inbox.mkdir(mode=0o700)
    archive = build_export(
        inbox / "resolution-foreign-source.zip",
        {
            "conversations.json": [
                conversation(
                    "resolution-foreign-source",
                    user_text=f"In {project} retain a foreign source issue",
                    assistant_text="\n".join(
                        (
                            "Objective: retain a foreign source issue",
                            "Outcome: foreign source issue is verified",
                            f"Open issue: {RESOLUTION_TARGET}",
                        )
                    ),
                    model_slug=CODEX_MODEL,
                )
            ]
        },
    )
    archive.chmod(0o600)
    return archive


def _memory_with_content(
    container: ServiceContainer,
    project_id,
    namespace: Namespace,
    content: str,
):
    matches = tuple(
        memory
        for memory in container.memories.list_scoped(project_id, namespace)
        if memory.normalized_content == content
    )
    assert len(matches) == 1
    return matches[0]


def _resolution_audit_rows(container: ServiceContainer) -> list[dict[str, object]]:
    with container.database.connect(readonly=True) as connection:
        rows = connection.execute(
            "select * from memory_issue_resolutions order by resolution_id"
        ).fetchall()
    return [dict(row) for row in rows]


def _last_reconcile_json(container: ServiceContainer) -> dict[str, object]:
    with container.database.connect(readonly=True) as connection:
        row = connection.execute(
            "select value_json from app_state where name = 'last_reconcile_report'"
        ).fetchone()
    assert row is not None
    document = json.loads(str(row["value_json"]))
    assert isinstance(document, dict)
    return document


def _save_config(
    tmp_path: Path,
    home: Path,
    proposal_repository: Path,
    verification_argv: tuple[str, ...],
) -> Path:
    defaults = AppConfig.defaults(home)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config_path = runtime / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=defaults.project_roots,
            enabled_sources=defaults.enabled_sources,
            inactive_days=defaults.inactive_days,
            max_recall_tokens=defaults.max_recall_tokens,
            daily_reconcile_time=defaults.daily_reconcile_time,
            improvement_repository_root=proposal_repository,
            improvement_verification_commands=(verification_argv,),
        )
    )
    return config_path


def _discover_register_and_scan(
    container: ServiceContainer,
    repositories: DefaultRootRepositories,
):
    discovery = container.project_scanner.discover()
    assert discovery.issues == ()
    assert {candidate.canonical_path for candidate in discovery.candidates} == set(
        repositories.paths
    )
    projects = {
        candidate.canonical_path: container.projects.register(candidate)
        for candidate in discovery.candidates
    }
    reports = tuple(container.project_facts.scan(project) for project in projects.values())
    assert all(report.observed_count > 0 for report in reports)
    assert all(report.warnings == () for report in reports)
    return projects


def _codex_decisions() -> tuple[str, ...]:
    lesson = (
        "keep each local cache adapter deterministic namespace scoped and provenance "
        "verified while retaining bounded evidence for later maintenance reviews "
    )
    long_lessons = tuple(f"CODEX_LESSON_{index:02d} {lesson * 3}" for index in range(64))
    return (
        CODEX_MARKER,
        *long_lessons,
        f"scrub adapter credential {API_KEY}",
        f"scrub transport Bearer {BEARER_VALUE}",
        f"scrub password={PASSWORD_VALUE}",
    )


def _write_codex_session(
    home: Path,
    project: Path,
    decisions: tuple[str, ...],
    verified_at: datetime,
) -> Path:
    sessions = home / ".codex" / "sessions"
    sessions.mkdir(mode=0o700, parents=True)
    sessions.chmod(0o700)
    timestamp = verified_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    message = "\n".join(
        (
            CAPTURE_START,
            "Objective: stabilize the cache adapter",
            f"Outcome: {CURRENT_STATE}",
            f"Failed: {FAILED_ATTEMPT}",
            f"Verified: {VERIFIED_COMMAND}",
            f"Changed: {CHANGED_PATH}",
            f"Preference: {PREFERENCE}",
            f"Risk: {RISK}",
            f"Open issue: {OPEN_ISSUE}",
            f"Lesson: {REUSABLE_LESSON}",
            *(f"Decision: {decision}" for decision in decisions),
            CAPTURE_END,
        )
    )
    records = (
        {
            "timestamp": timestamp,
            "type": "session_meta",
            "payload": {"id": "session-e2e"},
        },
        {
            "timestamp": timestamp,
            "type": "turn_context",
            "payload": {
                "turn_id": "turn-1",
                "cwd": str(project),
                "model": CODEX_MODEL,
                "summary": "stabilize the cache adapter",
            },
        },
        {
            "timestamp": timestamp,
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "last_agent_message": message,
            },
        },
    )
    session = sessions / "session-e2e.jsonl"
    session.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
        newline="\n",
    )
    session.chmod(0o600)
    return session


def _write_chatgpt_archive(container: ServiceContainer, project: Path) -> Path:
    inbox = container.paths.imports / "chatgpt"
    inbox.mkdir(mode=0o700)
    archive = build_export(
        inbox / "chatgpt-e2e.zip",
        {
            "conversations.json": [
                conversation(
                    "conversation-e2e",
                    user_text=f"In {project} verify cache.py with pytest",
                    assistant_text="\n".join(
                        (
                            f"Decision: {CHATGPT_MARKER}",
                            f"Decision: redact exported credential {API_KEY}",
                            f"Decision: redact exported Bearer {BEARER_VALUE}",
                            f"Decision: redact exported password={PASSWORD_VALUE}",
                            "Verified: pytest tests/chatgpt/test_export.py -q",
                            "Open issue: CHATGPT_OPEN_ISSUE review export retention",
                            "Outcome: CHATGPT_CURRENT_STATE export cache is verified",
                        )
                    ),
                    model_slug=CHATGPT_MODEL,
                )
            ]
        },
    )
    archive.chmod(0o600)
    return archive


def _capture_lifecycle_states(container: ServiceContainer) -> tuple[str, ...]:
    with container.database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select pending_id, verification_state as state from pending_captures
            union all
            select pending_id, final_state as state from pending_capture_history
            order by pending_id
            """
        ).fetchall()
    return tuple(row["state"] for row in rows)


def _capture_provenance(container: ServiceContainer) -> set[tuple[str, str, str]]:
    with container.database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select source_agent, source_record_id, parser_version
            from source_refs where parser_version = 'capture-v1'
            """
        ).fetchall()
    return {(row["source_agent"], row["source_record_id"], row["parser_version"]) for row in rows}


def _behavior_namespace_counts(
    container: ServiceContainer,
    project_id,
) -> dict[tuple[str, str], int]:
    with container.database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select source_agent, model_id, count(*) as count
            from behavior_memories where project_id = ?
            group by source_agent, model_id
            """,
            (str(project_id).lower(),),
        ).fetchall()
    return {(row["source_agent"], row["model_id"]): row["count"] for row in rows}


def _recall(
    container: ServiceContainer,
    project: Path,
    namespace: Namespace,
    task: str,
):
    return container.recall.recall(
        RecallRequest(
            cwd=project,
            task=task,
            namespace=namespace,
            max_tokens=800,
        )
    )


def _assert_namespace_isolation(
    container: ServiceContainer,
    project: Path,
    codex_namespace: Namespace,
    chatgpt_namespace: Namespace,
) -> None:
    codex = _recall(container, project, codex_namespace, CODEX_MARKER)
    chatgpt = _recall(container, project, chatgpt_namespace, CHATGPT_MARKER)
    assert CODEX_MARKER in codex.text
    assert CHATGPT_MARKER not in codex.text
    assert CHATGPT_MARKER in chatgpt.text
    assert CODEX_MARKER not in chatgpt.text

    crossed = (
        Namespace(source_agent=SourceAgent.CODEX, model_id=CHATGPT_MODEL),
        Namespace(source_agent=SourceAgent.CHATGPT, model_id=CODEX_MODEL),
    )
    for namespace in crossed:
        text = _recall(container, project, namespace, "cache namespace").text
        assert CODEX_MARKER not in text
        assert CHATGPT_MARKER not in text


def _assert_bounded_recall(
    container: ServiceContainer,
    project_id,
    project: Path,
    namespace: Namespace,
    record_property,
) -> None:
    facts = container.facts.search(project_id, "", 100)
    memories = container.memories.search(project_id, namespace, "", 100)
    candidate_text = "\n".join(record.normalized_content for record in (*facts, *memories))
    candidate_tokens = ConservativeTokenCounter().count(candidate_text)
    brief = _recall(container, project, namespace, "cache pytest")
    reduction = 1 - brief.estimated_tokens / candidate_tokens

    assert candidate_tokens > 4_000
    assert brief.estimated_tokens <= 800
    assert reduction >= 0.80
    assert all(
        mandatory in brief.text for mandatory in (CURRENT_STATE, VERIFIED_COMMAND, OPEN_ISSUE)
    )
    record_property("candidate_tokens", candidate_tokens)
    record_property("brief_tokens", brief.estimated_tokens)
    record_property("selected_count", len(brief.selected_ids))
    record_property("omitted_count", brief.omitted_count)
    record_property("reduction", round(reduction, 4))


def _approve_exactly_one_shared_rule(
    container: ServiceContainer,
    project_id,
    namespace: Namespace,
) -> None:
    memory = next(
        record
        for record in container.memories.list_scoped(project_id, namespace)
        if record.normalized_content == CODEX_MARKER
    )
    requested = container.promotions.request_scoped(
        project_id,
        namespace,
        memory.memory_id,
        "Keep cache verification evidence source neutral only after approval",
    )
    assert requested.status == "pending"
    assert _approved_rule_count(container, project_id) == 0
    approved = container.promotions.approve_scoped(
        project_id,
        namespace,
        requested.promotion_id,
        "local-e2e",
    )
    assert approved.category == "approved_shared_rule"
    assert _approved_rule_count(container, project_id) == 1


def _approved_rule_count(container: ServiceContainer, project_id) -> int:
    with container.database.connect(readonly=True) as connection:
        return connection.execute(
            """
            select count(*) from project_facts
            where project_id = ? and category = 'approved_shared_rule'
              and lifecycle_state = 'active'
            """,
            (str(project_id).lower(),),
        ).fetchone()[0]


def _idempotence_counts(container: ServiceContainer) -> tuple[int, ...]:
    with container.database.connect(readonly=True) as connection:
        return tuple(
            connection.execute(f"select count(*) from {table}").fetchone()[0]
            for table in (
                "behavior_memories",
                "project_facts",
                "import_receipts",
                "pending_captures",
                "pending_capture_history",
                "source_refs",
            )
        )


def _compact_namespaces_after_22_days(
    container: ServiceContainer,
    project_id,
    codex_namespace: Namespace,
    chatgpt_namespace: Namespace,
) -> None:
    project = container.projects.get(project_id)
    assert project.last_observed_change is not None
    future = project.last_observed_change + timedelta(days=22)
    compaction = CompactionService(
        container.database,
        container.memories,
        container.redactor,
        inactive_days=21,
        now=lambda: future,
    )
    assert project_id in {record.project_id for record in compaction.find_inactive(future)}
    codex = compaction.compact(project_id, codex_namespace)
    chatgpt = compaction.compact(project_id, chatgpt_namespace)
    assert codex.status == chatgpt.status == "compacted"
    assert codex.source_count > 0 and chatgpt.source_count > 0
    assert codex.retrospective_count == chatgpt.retrospective_count == 1

    with container.database.connect(readonly=True) as connection:
        rows = connection.execute(
            """
            select source_agent, model_id, count(*) as count
            from behavior_memories
            where project_id = ? and memory_kind = 'retrospective'
            group by source_agent, model_id
            """,
            (str(project_id).lower(),),
        ).fetchall()
    assert {(row["source_agent"], row["model_id"]): row["count"] for row in rows} == {
        (SourceAgent.CODEX.value, CODEX_MODEL): 1,
        (SourceAgent.CHATGPT.value, CHATGPT_MODEL): 1,
    }


def _apply_safe_proposal(
    container: ServiceContainer,
    repository: Path,
    verification_argv: tuple[str, ...],
) -> None:
    assert container.proposal_applier is not None
    created = container.proposal_service.create(
        ProposalDraft(
            signature="e2e.safe-local-patch",
            title="Apply a bounded local improvement",
            description="Exercise approval and isolated Git application.",
            risk="low",
            patch=(
                "diff --git a/README.md b/README.md\n"
                "--- a/README.md\n"
                "+++ b/README.md\n"
                "@@ -1 +1 @@\n"
                "-seed\n"
                "+updated\n"
            ),
            verification_argv=verification_argv,
            target_version=None,
            origin="local_cli",
        )
    ).record
    approved = container.proposal_service.approve(
        created.proposal_id,
        actor="local-e2e",
    )
    assert approved.status == "approved"
    applied = container.proposal_service.apply(approved.proposal_id)
    assert container.proposal_service.get(approved.proposal_id).status == "applied"
    assert applied.original_branch == "main"
    assert applied.proposal_branch == (f"codex/memory-hub-proposal-{approved.proposal_id.hex}")
    assert git_output(repository, "show", f"{applied.applied_commit}:README.md") == ("updated")
    assert (repository / "README.md").read_text(encoding="utf-8") == "seed\n"
    assert git_output(repository, "worktree", "list", "--porcelain").count("worktree ") == 1


async def _assert_loopback_dashboard(
    container: ServiceContainer,
    project_id,
    codex_namespace: Namespace,
    chatgpt_namespace: Namespace,
) -> str:
    app = create_app(container)
    token = LocalAccessToken.load_or_create(container.paths)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        bad_host = await client.get("/", headers={"host": "attacker.example"})
        boot = await client.get(f"/?token={token}", follow_redirects=False)
        csrf = boot.headers["x-project-memory-hub-csrf"]
        page = await client.get("/")
        codex_memories = await client.get(
            "/memories",
            params={
                "project_id": str(project_id),
                "source_agent": codex_namespace.source_agent.value,
                "model_id": codex_namespace.model_id,
            },
        )
        chatgpt_memories = await client.get(
            "/memories",
            params={
                "project_id": str(project_id),
                "source_agent": chatgpt_namespace.source_agent.value,
                "model_id": chatgpt_namespace.model_id,
            },
        )
        foreign = await client.post(
            "/settings",
            headers={
                "origin": "https://attacker.example",
                "x-csrf-token": csrf,
            },
        )

    assert bad_host.status_code == 400
    assert boot.status_code == 303
    assert boot.headers["location"] == "/"
    assert token not in boot.text + boot.headers["location"]
    assert "httponly" in boot.headers["set-cookie"].casefold()
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert token not in page.text
    assert codex_memories.status_code == chatgpt_memories.status_code == 200
    assert CODEX_MARKER in codex_memories.text
    assert CHATGPT_MARKER in chatgpt_memories.text
    assert foreign.status_code == 403
    return "\n".join((page.text, codex_memories.text, chatgpt_memories.text))


def _database_bytes(container: ServiceContainer) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(container.paths.root.glob("memory.db*"), key=str)
        if path.is_file()
    )


def _log_bytes(container: ServiceContainer) -> bytes:
    return b"".join(
        path.read_bytes()
        for path in sorted(container.paths.logs.rglob("*"), key=str)
        if path.is_file()
    )


def _contains_seeded_secret(payload: bytes) -> bool:
    return any(secret.encode("utf-8") in payload for secret in SEEDED_SECRETS)
