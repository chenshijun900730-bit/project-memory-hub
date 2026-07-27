from __future__ import annotations

import hashlib
import re
import socket
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import urlopen
from uuid import UUID, uuid4

import pytest
from playwright.sync_api import (
    Browser,
    CDPSession,
    Error as PlaywrightError,
    Page,
    expect,
    sync_playwright,
)

from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import ServiceContainer, build_container
from project_memory_hub.domain import (
    BehaviorMemoryInput,
    CapturePayload,
    LifecycleState,
    MemoryKind,
    Namespace,
    NamespaceVerification,
    ProjectCandidate,
    SourceAgent,
)
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.security.web import LocalAccessToken
from tests.fixtures.chatgpt.build_fixtures import build_export, conversation


_SERVER_SCRIPT = """
import socket
import sys
from pathlib import Path

import uvicorn

from project_memory_hub.container import build_container
from project_memory_hub.web.app import create_app

config_path = Path(sys.argv[1])
probe_home = Path(sys.argv[3])
container = build_container(config_path, probe_home=probe_home)
listener = socket.socket(fileno=int(sys.argv[2]))
server = uvicorn.Server(
    uvicorn.Config(
        create_app(container),
        access_log=False,
        log_level="critical",
    )
)
try:
    server.run(sockets=[listener])
finally:
    container.close()
"""

RESOLVED_CARD = "BROWSER_RESOLVED_EXACT_NAMESPACE"
ARCHIVED_CARD = "BROWSER_ARCHIVED_EXACT_NAMESPACE"
_SYNTHETIC_TRAE_SCHEMA = "TRAE_E2E_PRIVATE_SCHEMA"
_SYNTHETIC_TRAE_BODY = "TRAE_E2E_PRIVATE_CHAT_BODY"
_I18N_JAVASCRIPT_COVERAGE_MINIMUM = 98.0
_PROJECTS_JAVASCRIPT_COVERAGE_MINIMUM = 85.0
_ZERO_WRITE_TABLES = (
    "project_facts",
    "source_refs",
    "behavior_memories",
    "pending_captures",
    "pending_capture_history",
    "checkpoints",
    "import_receipts_v2",
    "codex_deferred_records",
)


@dataclass(frozen=True, slots=True)
class _DashboardState:
    base_url: str
    config_path: Path
    database_path: Path
    archive_path: Path
    project_id: UUID
    project_ids: tuple[UUID, ...]
    private_project_path: Path
    approve_proposal_id: UUID
    reject_proposal_id: UUID
    token: str = field(repr=False)


def _insert_memory(
    container: ServiceContainer,
    project_id: UUID,
    *,
    model_id: str,
    content: str,
    memory_kind: MemoryKind = MemoryKind.DECISION,
) -> UUID:
    source_reference_id = uuid4()
    observed_at = datetime.now(timezone.utc)
    database = container.database
    with database.transaction() as connection:
        connection.execute(
            """
            insert into source_refs(
                source_reference_id, source_agent, source_record_id, source_path,
                content_hash, source_timestamp, parser_version, created_at
            ) values (?, 'codex', ?, null, ?, ?, 'dashboard-e2e-v1', ?)
            """,
            (
                str(source_reference_id),
                f"dashboard-record-{source_reference_id}",
                hashlib.sha256(str(source_reference_id).encode()).hexdigest(),
                observed_at.isoformat(),
                observed_at.isoformat(),
            ),
        )
    result = container.memories.insert(
        BehaviorMemoryInput(
            project_id=project_id,
            namespace=Namespace(
                source_agent=SourceAgent.CODEX,
                model_id=model_id,
            ),
            task_fingerprint=hashlib.sha256(
                f"dashboard-task-{source_reference_id}".encode()
            ).hexdigest(),
            memory_kind=memory_kind,
            normalized_content=content,
            content_hash=hashlib.sha256(content.encode()).hexdigest(),
            source_reference_id=source_reference_id,
            created_at=observed_at,
            confidence=0.9,
        )
    )
    assert result.record_id is not None
    return result.record_id


def _seed_dashboard(root: Path) -> _DashboardState:
    project_root = root / "projects"
    project_root.mkdir()
    project = project_root / "browser-project"
    project.mkdir()
    runtime = root / "runtime"
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

    with build_container(config_path) as container:
        registered = container.projects.register(
            ProjectCandidate(
                canonical_path=project,
                display_name="Browser project",
            )
        )
        collection_records = []
        for index in range(1, 25):
            collection_path = project_root / f"collection-private-path-{index:02d}"
            collection_path.mkdir()
            collection_records.append(
                container.projects.register(
                    ProjectCandidate(
                        canonical_path=collection_path,
                        display_name=f"Collection project {index:02d}",
                    )
                )
            )
        container.projects.set_enabled(collection_records[0].project_id, False)
        with container.database.transaction() as connection:
            connection.execute(
                "update projects set inactivity_state = 'inactive' where project_id = ?",
                (str(collection_records[1].project_id),),
            )
        _insert_memory(
            container,
            registered.project_id,
            model_id="browser-model-a",
            content="BROWSER_MODEL_A_ONLY",
        )
        _insert_memory(
            container,
            registered.project_id,
            model_id="browser-model-b",
            content="BROWSER_MODEL_B_ONLY",
        )
        selected_namespace = Namespace(
            source_agent=SourceAgent.CODEX,
            model_id="browser-model-a",
        )
        _insert_memory(
            container,
            registered.project_id,
            model_id=selected_namespace.model_id,
            content=RESOLVED_CARD,
            memory_kind=MemoryKind.OPEN_ISSUE,
        )
        archived_id = _insert_memory(
            container,
            registered.project_id,
            model_id=selected_namespace.model_id,
            content=ARCHIVED_CARD,
            memory_kind=MemoryKind.OPEN_ISSUE,
        )
        container.memories.set_lifecycle_scoped(
            registered.project_id,
            selected_namespace,
            archived_id,
            LifecycleState.ARCHIVED,
        )
        resolution_payload = CapturePayload(
            cwd=project,
            namespace=selected_namespace,
            source_record_id="dashboard-resolution-record",
            objective="",
            outcome="",
            resolved_open_issues=[RESOLVED_CARD],
        )
        resolution = container.capture.capture(
            resolution_payload,
            NamespaceVerification(
                namespace=selected_namespace,
                source_record_id=resolution_payload.source_record_id,
                verified_by="codex_adapter",
                verified_at=datetime.now(timezone.utc),
            ),
        )
        assert resolution.resolved_count == 1
        with container.database.transaction() as connection:
            connection.execute(
                "update projects set permission_status = 'blocked_permission' where project_id = ?",
                (str(registered.project_id),),
            )
        approved_candidate = container.proposal_service.create(
            ProposalDraft(
                signature="dashboard.browser-approve",
                title="Approve browser proposal",
                description="Safe bounded browser approval check.",
                risk="low",
                patch=None,
                verification_argv=(),
                target_version=None,
                origin="control_panel",
            )
        ).record
        rejected_candidate = container.proposal_service.create(
            ProposalDraft(
                signature="dashboard.browser-reject",
                title="Reject browser proposal",
                description="Safe bounded browser rejection check.",
                risk="low",
                patch=None,
                verification_argv=(),
                target_version=None,
                origin="control_panel",
            )
        ).record
        container.reconcile.record_success()
        token = LocalAccessToken.load_or_create(container.paths)
        database_path = container.paths.database

    archive_path = build_export(
        root / "chatgpt-dry-run.zip",
        {
            "conversations.json": [
                conversation(
                    "dashboard-conversation",
                    user_text=f"In {project} inspect the browser import",
                    assistant_text=(
                        "Decision: keep the import local\n"
                        "Verified: pytest tests/e2e/test_dashboard.py\n"
                        "Outcome: browser dry run matched"
                    ),
                )
            ]
        },
    )
    return _DashboardState(
        base_url="",
        config_path=config_path,
        database_path=database_path,
        archive_path=archive_path,
        project_id=registered.project_id,
        project_ids=(
            registered.project_id,
            *(record.project_id for record in collection_records),
        ),
        private_project_path=collection_records[-1].canonical_path,
        approve_proposal_id=approved_candidate.proposal_id,
        reject_proposal_id=rejected_candidate.proposal_id,
        token=token,
    )


def _probe_home_for(config_path: Path) -> Path:
    return config_path.parent.parent / "probe-home"


def _seed_probe_home(probe_home: Path) -> None:
    session_memory = probe_home / ".trae" / "session_memory"
    session_memory.mkdir(parents=True)
    database = session_memory / "metadata.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            f'CREATE TABLE "{_SYNTHETIC_TRAE_SCHEMA}"'
            "(id INTEGER PRIMARY KEY, model_id TEXT, private_body TEXT)"
        )
        connection.execute(
            f'INSERT INTO "{_SYNTHETIC_TRAE_SCHEMA}"(model_id, private_body) VALUES (?, ?)',
            ("synthetic-model", _SYNTHETIC_TRAE_BODY),
        )


@contextmanager
def _uvicorn_subprocess(config_path: Path) -> Iterator[str]:
    probe_home = _probe_home_for(config_path)
    _seed_probe_home(probe_home)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    listener.set_inheritable(True)
    port = int(listener.getsockname()[1])
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _SERVER_SCRIPT,
            str(config_path),
            str(listener.fileno()),
            str(probe_home),
        ],
        close_fds=True,
        pass_fds=(listener.fileno(),),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    listener.close()
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 12
    ready = False
    try:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError("Dashboard subprocess exited before readiness.")
            try:
                with urlopen(f"{base_url}/", timeout=0.25):
                    pass
            except HTTPError as error:
                error.close()
                if error.code == 401:
                    ready = True
                    break
            except (TimeoutError, URLError):
                pass
            time.sleep(0.05)
        if not ready:
            raise AssertionError("Dashboard subprocess did not become ready.")
        yield base_url
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def _database_digest(path: Path) -> str:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        document = "\n".join(connection.iterdump()).encode("utf-8")
    finally:
        connection.close()
    return hashlib.sha256(document).hexdigest()


def _database_row_counts(path: Path) -> tuple[tuple[str, int], ...]:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        present = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
        }
        return tuple(
            (
                table,
                int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]),
            )
            for table in _ZERO_WRITE_TABLES
            if table in present
        )
    finally:
        connection.close()


def _probe_persistence_snapshot(
    state: _DashboardState,
) -> tuple[bytes, str, tuple[tuple[str, int], ...], tuple[str, ...]]:
    runtime_files = tuple(
        sorted(
            str(path.relative_to(state.config_path.parent))
            for path in state.config_path.parent.rglob("*")
            if path.is_file()
            and path.name
            not in {
                f"{state.database_path.name}-shm",
                f"{state.database_path.name}-wal",
            }
        )
    )
    return (
        state.config_path.read_bytes(),
        _database_digest(state.database_path),
        _database_row_counts(state.database_path),
        runtime_files,
    )


def _bootstrap(page: Page, state: _DashboardState) -> None:
    bootstrap_url = f"{state.base_url}/?token={state.token}"
    try:
        page.goto(bootstrap_url, wait_until="domcontentloaded", timeout=10_000)
    except PlaywrightError:
        raise AssertionError("Browser bootstrap failed.") from None
    location = urlsplit(page.url)
    if location.path != "/" or location.query:
        raise AssertionError("Browser bootstrap did not clear the token URL.")


def _start_javascript_coverage(page: Page) -> CDPSession:
    session = page.context.new_cdp_session(page)
    session.send("Profiler.enable")
    session.send(
        "Profiler.startPreciseCoverage",
        {"callCount": True, "detailed": True},
    )
    return session


def _i18n_javascript_source() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "src"
        / "project_memory_hub"
        / "web"
        / "static"
        / "i18n.js"
    ).read_text(encoding="utf-8")


def _projects_javascript_source() -> str:
    return (
        Path(__file__).resolve().parents[2]
        / "src"
        / "project_memory_hub"
        / "web"
        / "static"
        / "projects.js"
    ).read_text(encoding="utf-8")


def _stop_javascript_coverage(
    session: CDPSession,
    *,
    source: str,
    script_path: str,
) -> float:
    entries = session.send("Profiler.takePreciseCoverage")["result"]
    session.send("Profiler.stopPreciseCoverage")
    session.send("Profiler.disable")

    covered_ranges: list[tuple[int, int]] = []
    matching_entries = [
        entry for entry in entries if urlsplit(entry.get("url", "")).path == script_path
    ]
    if not matching_entries:
        raise AssertionError(f"V8 did not report coverage for {script_path}.")

    source_units = len(source.encode("utf-16-le")) // 2
    for entry in matching_entries:
        ranges = [
            (
                coverage_range["startOffset"],
                coverage_range["endOffset"],
                coverage_range["count"],
            )
            for function in entry["functions"]
            for coverage_range in function["ranges"]
        ]
        if any(start < 0 or start > end or end > source_units for start, end, _count in ranges):
            raise AssertionError(
                f"V8 coverage ranges do not match the served {script_path} source."
            )
        boundaries = sorted({offset for start, end, _count in ranges for offset in (start, end)})
        for start, end in zip(boundaries, boundaries[1:]):
            containing = [
                coverage_range
                for coverage_range in ranges
                if coverage_range[0] <= start and end <= coverage_range[1]
            ]
            if not containing:
                continue
            most_specific = min(
                containing,
                key=lambda coverage_range: (
                    coverage_range[1] - coverage_range[0],
                    coverage_range[2],
                ),
            )
            if most_specific[2] > 0:
                covered_ranges.append((start, end))

    merged: list[list[int]] = []
    for start, end in sorted(covered_ranges):
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    covered_units = sum(end - start for start, end in merged)
    coverage = covered_units / source_units * 100
    if not 0.0 <= coverage <= 100.0:
        raise AssertionError(f"Invalid {script_path} V8 source coverage: {coverage:.2f}%.")
    return coverage


def _stop_i18n_javascript_coverage(session: CDPSession, *, source: str) -> float:
    return _stop_javascript_coverage(
        session,
        source=source,
        script_path="/static/i18n.js",
    )


@pytest.fixture(scope="module")
def dashboard(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_DashboardState]:
    seeded = _seed_dashboard(tmp_path_factory.mktemp("dashboard-e2e"))
    with _uvicorn_subprocess(seeded.config_path) as base_url:
        yield replace(seeded, base_url=base_url)


@pytest.fixture(scope="module")
def chromium() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except PlaywrightError as error:
            detail = str(error).casefold()
            if "executable doesn't exist" not in detail and "playwright install" not in detail:
                raise AssertionError("Chromium failed to launch.") from None
            try:
                browser = playwright.chromium.launch(channel="chrome", headless=True)
            except PlaywrightError as fallback_error:
                fallback_detail = str(fallback_error).casefold()
                if (
                    "executable doesn't exist" in fallback_detail
                    or "distribution 'chrome' is not found" in fallback_detail
                    or "playwright install" in fallback_detail
                ):
                    pytest.skip(
                        "Local Playwright Chromium is missing; run "
                        "`uv run playwright install chromium`.",
                    )
                raise AssertionError("Chromium failed to launch.") from None
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(chromium: Browser, dashboard: _DashboardState) -> Iterator[Page]:
    context = chromium.new_context(locale="zh-CN")
    selected_page = context.new_page()
    _bootstrap(selected_page, dashboard)
    try:
        yield selected_page
    finally:
        context.close()


def test_bootstrap_sources_permissions_and_exact_namespace(
    page: Page,
    dashboard: _DashboardState,
) -> None:
    if urlsplit(page.url).query:
        raise AssertionError("Browser bootstrap did not clear the token URL.")
    expect(page.get_by_text("1 permission error", exact=True)).to_be_visible()

    page.get_by_role("link", name="Sources", exact=True).click()
    for source, label in (("codex", "Codex"), ("chatgpt", "ChatGPT")):
        row = page.locator(f'tr[data-source="{source}"]')
        expect(row.locator("th")).to_have_text(label)
        expect(row).to_contain_text("Available")
        expect(row).to_contain_text("Desired: Enabled")
        expect(row).to_contain_text("Runtime: Enabled")
        expect(row.get_by_role("button", name="Disable", exact=True)).to_be_visible()

    optional_sources = {
        "trae": "Trae",
        "workbuddy": "WorkBuddy",
        "zcode": "Zcode",
        "qoderwork": "QoderWork",
        "claude_code": "Claude Code",
    }
    for source, label in optional_sources.items():
        row = page.locator(f'tr[data-source="{source}"]')
        expect(row.locator("th")).to_have_text(label)
        expect(row.get_by_text("Unavailable", exact=True)).to_be_visible()
        expect(row).to_contain_text("Desired: Unavailable")
        expect(row).to_contain_text("Runtime: Unavailable")
        expect(row).to_contain_text("Not checked")
        expect(row).to_contain_text("Not run")
        expect(row).to_contain_text("Locked")
        if source == "trae":
            expect(row).to_contain_text("Readable")
            expect(row).to_contain_text("Structure metadata check")
            expect(row.get_by_role("button", name="Further check", exact=True)).to_be_enabled()
        else:
            expect(row).to_contain_text("Missing")
            expect(row).to_contain_text("Presence and access check")
            expect(row).to_contain_text("No control available")
            expect(row.get_by_role("button")).to_have_count(0)

    page.get_by_role("link", name="Projects", exact=True).click()
    expect(page.get_by_text("blocked_permission", exact=True)).to_be_visible()

    page.get_by_role("link", name="Memories", exact=True).click()
    expect(page.get_by_text("Choose a project, source, and model", exact=False)).to_be_visible()
    expect(page.get_by_text("BROWSER_MODEL_A_ONLY", exact=True)).to_have_count(0)
    expect(page.get_by_text("BROWSER_MODEL_B_ONLY", exact=True)).to_have_count(0)

    page.locator('select[name="project_id"]').select_option(str(dashboard.project_id))
    page.get_by_role("button", name="Load project memory", exact=True).click()
    expect(page.get_by_text("Choose an exact source and model", exact=False)).to_be_visible()
    expect(page.get_by_text("BROWSER_MODEL_A_ONLY", exact=True)).to_have_count(0)
    expect(page.get_by_text("BROWSER_MODEL_B_ONLY", exact=True)).to_have_count(0)

    page.locator('select[name="source_agent"]').select_option("codex")
    model_filter = page.locator('form.filter-panel input[name="model_id"]')
    model_filter.fill("browser-model-a")
    page.get_by_role("button", name="Load project memory", exact=True).click()
    expect(page.get_by_text("BROWSER_MODEL_A_ONLY", exact=True)).to_be_visible()
    expect(page.get_by_text("BROWSER_MODEL_B_ONLY", exact=True)).to_have_count(0)


def test_safe_onboarding_navigation_and_structured_empty_states(
    page: Page,
) -> None:
    current_navigation = page.locator('nav.rail a[aria-current="page"]')
    expect(current_navigation).to_have_count(1)
    expect(current_navigation).to_have_attribute("href", "/")

    next_step = page.locator('section[data-next-safe-step="doctor"]')
    expect(next_step).to_be_visible()
    expect(next_step.locator("code")).to_have_text("memory-hub doctor --format json")
    expect(next_step).to_contain_text("Success condition:")

    page.get_by_role("link", name="Memories", exact=True).click()
    expect(current_navigation).to_have_count(1)
    expect(current_navigation).to_have_attribute("href", "/memories")
    exact_context_command = 'memory-hub codex-context --cwd "$PWD" --format json'
    expect(page.locator("section.guidance-panel code")).to_have_text(exact_context_command)
    expect(page.locator("section.empty-state [data-empty-next-step] code")).to_have_text(
        exact_context_command
    )
    expect(page.get_by_text("browser-model-a", exact=True)).to_have_count(0)
    expect(page.get_by_text("browser-model-b", exact=True)).to_have_count(0)

    memory_empty = page.locator("section.empty-state")
    expect(memory_empty).to_have_count(1)
    expect(memory_empty.locator("[data-empty-reason]")).to_have_count(1)
    expect(memory_empty.locator("[data-empty-next-step] code")).to_have_count(1)
    expect(memory_empty.locator("[data-empty-success]")).to_have_count(1)

    page.get_by_role("link", name="Proposals", exact=True).click()
    expect(current_navigation).to_have_count(1)
    expect(current_navigation).to_have_attribute("href", "/proposals")
    proposal_empty = page.locator("section.empty-state")
    expect(proposal_empty).to_have_count(1)
    expect(proposal_empty.locator("[data-empty-reason]")).to_have_count(1)
    expect(proposal_empty.locator("[data-empty-next-step] code")).to_have_count(1)
    expect(proposal_empty.locator("[data-empty-success]")).to_have_count(1)


def test_projects_progressive_browser_is_client_only_and_state_preserving(
    page: Page,
    dashboard: _DashboardState,
) -> None:
    before = _probe_persistence_snapshot(dashboard)
    page.get_by_role("link", name="Projects", exact=True).click()

    root = page.locator("[data-project-browser]")
    cards = root.locator("[data-project-card]")
    visible_cards = root.locator("[data-project-card]:visible")
    expect(root).to_have_class(re.compile(r"\bprojects-enhanced\b"))
    expect(cards).to_have_count(25)
    expect(visible_cards).to_have_count(12)
    expect(root.locator("[data-project-visible-count]")).to_have_text("12")
    expect(root.locator("[data-project-total-count]")).to_have_text("25")

    form_contract_script = """
    forms => forms.map(form => ({
      action: form.getAttribute("action"),
      method: form.getAttribute("method"),
      controls: Array.from(form.elements).map(control => ({
        tag: control.tagName,
        type: control.getAttribute("type"),
        name: control.getAttribute("name"),
        value: control.getAttribute("value")
      }))
    }))
    """
    forms_before = root.locator("form").evaluate_all(form_contract_script)

    show_more = root.locator("[data-project-show-more]")
    show_more.click()
    expect(visible_cards).to_have_count(24)
    expect(root.locator("[data-project-visible-count]")).to_have_text("24")
    show_more.click()
    expect(visible_cards).to_have_count(25)
    expect(show_more).to_be_hidden()

    search = root.locator("[data-project-search]")
    search.fill("Collection project 24")
    expect(visible_cards).to_have_count(1)
    expect(visible_cards.first).to_have_attribute("data-project-id", str(dashboard.project_ids[-1]))
    search.fill(str(dashboard.project_ids[-1]).upper())
    expect(visible_cards).to_have_count(1)
    search.fill(str(dashboard.private_project_path))
    expect(visible_cards).to_have_count(0)
    expect(root.locator("[data-project-no-results]")).to_be_visible()

    search.fill("")
    status = root.locator("[data-project-status-filter]")
    status.select_option("disabled")
    expect(visible_cards).to_have_count(1)
    status.select_option("permission")
    expect(visible_cards).to_have_count(1)
    status.select_option("inactive")
    expect(visible_cards).to_have_count(1)
    status.select_option("all")
    expect(visible_cards).to_have_count(12)

    first_card = visible_cards.first
    path_details = first_card.locator("details[data-project-path]")
    expect(path_details).not_to_have_attribute("open", "")
    path_details.locator("summary").click()
    expect(path_details).to_have_attribute("open", "")
    expect(path_details.locator(".path")).to_be_visible()

    page.get_by_role("button", name="中文", exact=True).click()
    expect(root.get_by_text("当前显示", exact=True)).to_be_visible()
    expect(root.get_by_role("button", name="显示更多", exact=True)).to_be_visible()
    page.get_by_role("button", name="English", exact=True).click()

    assert root.locator("form").evaluate_all(form_contract_script) == forms_before
    assert _probe_persistence_snapshot(dashboard) == before


def test_projects_remain_complete_without_javascript(
    chromium: Browser,
    dashboard: _DashboardState,
) -> None:
    context = chromium.new_context(locale="en-US", java_script_enabled=False)
    selected_page = context.new_page()
    try:
        _bootstrap(selected_page, dashboard)
        selected_page.get_by_role("link", name="Projects", exact=True).click()
        root = selected_page.locator("[data-project-browser]")
        expect(root.locator("[data-project-card]")).to_have_count(25)
        expect(root.locator("[data-project-card]:visible")).to_have_count(25)
        expect(root.locator("[data-project-controls]")).to_be_hidden()
        expect(root.locator("[data-project-show-more]")).to_be_hidden()
        expect(root.locator("details[data-project-path]")).to_have_count(25)
        first_path = root.locator("details[data-project-path]").first
        first_path.locator("summary").click()
        expect(first_path.locator(".path")).to_be_visible()
        expect(root.locator("form")).to_have_count(50)
    finally:
        context.close()


def test_projects_empty_runtime_shows_three_complete_next_steps(
    chromium: Browser,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    runtime = tmp_path / "runtime"
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
    with build_container(config_path) as container:
        container.reconcile.record_success()
        token = LocalAccessToken.load_or_create(container.paths)

    with _uvicorn_subprocess(config_path) as base_url:
        context = chromium.new_context(locale="en-US")
        selected_page = context.new_page()
        try:
            response = selected_page.goto(
                f"{base_url}/?token={token}",
                wait_until="domcontentloaded",
            )
            location = urlsplit(selected_page.url)
            assert response is not None
            assert location.path == "/" and not location.query, (
                f"empty runtime bootstrap failed with status {response.status}"
            )
            selected_page.goto(f"{base_url}/projects", wait_until="domcontentloaded")
            empty_states = selected_page.locator("section.empty-state")
            expect(empty_states).to_have_count(3)
            expect(empty_states.filter(has_text="no action required")).to_have_count(2)
            for index in range(3):
                state = empty_states.nth(index)
                expect(state).to_be_visible()
                expect(state.locator("[data-empty-reason]")).to_have_count(1)
                expect(state.locator("[data-empty-next-step] code")).to_have_count(1)
                expect(state.locator("[data-empty-success]")).to_have_count(1)
        finally:
            context.close()


def test_first_run_setup_is_bilingual_resumable_and_mobile_readable(
    chromium: Browser,
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "projects"
    project_root.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    config_path = runtime / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
            setup_completed=False,
        )
    )
    with build_container(config_path) as container:
        container.reconcile.record_success()
        token = LocalAccessToken.load_or_create(container.paths)

    with _uvicorn_subprocess(config_path) as base_url:
        context = chromium.new_context(locale="en-US", viewport={"width": 520, "height": 900})
        selected_page = context.new_page()
        try:
            response = selected_page.goto(
                f"{base_url}/?token={token}",
                wait_until="domcontentloaded",
            )
            assert response is not None and response.status == 200
            expect(selected_page.locator('[data-setup-incomplete="true"]')).to_be_visible()
            selected_page.get_by_role("link", name="Setup", exact=True).click()
            expect(selected_page).to_have_url(f"{base_url}/setup")
            expect(selected_page.get_by_role("heading", name="Setup", exact=True)).to_be_visible()
            expect(
                selected_page.locator('input[name="enabled_sources"][value="codex"]')
            ).to_be_checked()
            expect(
                selected_page.locator('input[name="enabled_sources"][value="chatgpt"]')
            ).to_be_checked()
            expect(
                selected_page.locator('input[name="enabled_sources"][value="trae"]')
            ).to_have_count(0)

            selected_page.get_by_role("button", name="中文", exact=True).click()
            expect(
                selected_page.get_by_role("heading", name="配置向导", exact=True)
            ).to_be_visible()
            selected_page.reload(wait_until="domcontentloaded")
            expect(selected_page.locator("html")).to_have_attribute("lang", "zh-CN")
            expect(
                selected_page.get_by_role("heading", name="配置向导", exact=True)
            ).to_be_visible()
            selected_page.get_by_role("button", name="English", exact=True).click()

            selected_page.locator('input[name="daily_reconcile_time"]').fill("04:15")
            selected_page.locator("[data-setup-configure]").get_by_role(
                "button", name="Save and continue", exact=True
            ).click()
            expect(selected_page).to_have_url(f"{base_url}/setup?saved=1")
            expect(selected_page.get_by_role("status")).to_contain_text("Saved safely")

            reopened = context.new_page()
            try:
                reopened.goto(f"{base_url}/setup", wait_until="domcontentloaded")
                expect(reopened.locator('input[name="daily_reconcile_time"]')).to_have_value(
                    "04:15"
                )
                expect(reopened.locator("[data-setup]")).to_be_visible()
                layout = reopened.locator("[data-setup]").evaluate(
                    """
                    element => ({
                      width: element.getBoundingClientRect().width,
                      viewport: window.innerWidth,
                      documentWidth: document.documentElement.scrollWidth
                    })
                    """
                )
                assert layout["width"] <= layout["viewport"]
                assert layout["documentWidth"] <= layout["viewport"]

                reopened.locator("[data-setup-complete]").get_by_role(
                    "button", name="Finish local setup", exact=True
                ).click()
                expect(reopened).to_have_url(f"{base_url}/?setup-complete=1")
                expect(reopened.get_by_role("status")).to_contain_text("Local setup is complete")
            finally:
                reopened.close()
        finally:
            context.close()

    persisted = ConfigManager(config_path).load()
    assert persisted.daily_reconcile_time == "04:15"
    assert persisted.setup_completed is True


def test_sources_use_readable_vertical_cards_at_mobile_width(page: Page) -> None:
    page.set_viewport_size({"width": 520, "height": 900})
    page.get_by_role("link", name="Sources", exact=True).click()

    probes = page.locator('[data-source-collection="probes"]')
    trae = probes.locator('tr[data-source="trae"]')
    expect(trae.locator(".mobile-field-label").first).to_be_visible()
    expect(trae.locator(".mobile-field-label").first).to_have_text("Implementation")
    layout = trae.evaluate(
        """
        row => ({
          rowDisplay: getComputedStyle(row).display,
          cellDisplays: Array.from(row.querySelectorAll("td")).map(
            cell => getComputedStyle(cell).display
          ),
          labelDisplay: getComputedStyle(
            row.querySelector(".mobile-field-label")
          ).display,
          overflowX: getComputedStyle(
            row.closest(".table-wrap")
          ).overflowX
        })
        """
    )
    assert layout == {
        "rowDisplay": "block",
        "cellDisplays": ["block"] * 6,
        "labelDisplay": "block",
        "overflowX": "visible",
    }


def test_language_switch_persists_across_pages_without_runtime_write(
    page: Page,
    dashboard: _DashboardState,
) -> None:
    before = _probe_persistence_snapshot(dashboard)
    html = page.locator("html")

    expect(html).to_have_attribute("lang", "en")
    expect(page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="中文", exact=True)).to_have_attribute("type", "button")
    expect(page.get_by_role("button", name="English", exact=True)).to_have_attribute(
        "type", "button"
    )
    page.get_by_role("button", name="中文", exact=True).click()

    expect(html).to_have_attribute("lang", "zh-CN")
    assert page.evaluate("localStorage.getItem('pmh-language')") == "zh-CN"
    expect(page.get_by_role("heading", name="总览", exact=True)).to_be_visible()
    page.get_by_role("link", name="来源", exact=True).click()
    expect(page).to_have_url(f"{dashboard.base_url}/sources")
    expect(page.get_by_role("heading", name="来源", exact=True)).to_be_visible()

    form_contract_script = """
    forms => forms.map(form => ({
      action: form.getAttribute("action"),
      method: form.getAttribute("method"),
      controls: Array.from(form.elements).map(control => ({
        tag: control.tagName,
        type: control.getAttribute("type"),
        name: control.getAttribute("name"),
        value: control.getAttribute("value"),
        pattern: control.getAttribute("pattern")
      }))
    }))
    """
    chinese_form_contract = page.locator("form").evaluate_all(form_contract_script)
    page.get_by_role("button", name="English", exact=True).click()
    assert page.locator("form").evaluate_all(form_contract_script) == chinese_form_contract
    page.get_by_role("button", name="中文", exact=True).click()
    assert page.locator("form").evaluate_all(form_contract_script) == chinese_form_contract

    trae = page.locator('tr[data-source="trae"]')
    expect(trae).to_contain_text("不可用")
    expect(trae).to_contain_text("期望：不可用")
    expect(trae).to_contain_text("运行中：不可用")
    expect(trae).to_contain_text("未检查")
    expect(trae).to_contain_text("未运行")
    expect(trae).to_contain_text("已锁定")
    expect(trae.get_by_role("button", name="进一步检测", exact=True)).to_be_enabled()

    for source in ("trae", "workbuddy", "zcode", "qoderwork", "claude_code"):
        optional_row = page.locator(f'tr[data-source="{source}"]')
        expect(optional_row.locator('form[action$="/enable"]')).to_have_count(0)
        expect(optional_row.locator('form[action*="/import"]')).to_have_count(0)

    trae.get_by_role("button", name="进一步检测", exact=True).click()
    expect(page).to_have_url(f"{dashboard.base_url}/sources")
    installation = trae.locator(".status.detected, .status.not-detected")
    expect(installation).to_have_count(1)
    expect(installation).to_have_text(re.compile(r"^(已检测到|未检测到)$"))
    expect(trae.locator(".status.readable")).to_have_text("可读取")
    expect(trae.locator(".status.unverifiable")).to_have_text("无法验证")
    expect(trae.get_by_text("不支持", exact=True)).to_be_visible()
    expect(trae.locator(".status.locked")).to_have_text("已锁定")
    expect(trae).to_contain_text("model_id_unverifiable")
    expect(trae.locator("ul.warning-codes")).to_have_attribute("aria-label", "探针警告")

    page.reload(wait_until="domcontentloaded")
    expect(html).to_have_attribute("lang", "zh-CN")
    expect(page.get_by_role("heading", name="来源", exact=True)).to_be_visible()
    expect(trae).to_contain_text("未检查")
    expect(trae).to_contain_text("未运行")

    for nav_label, heading in (
        ("项目", "项目"),
        ("记忆", "记忆"),
        ("导入", "导入"),
        ("提案", "提案"),
        ("设置", "设置"),
        ("总览", "总览"),
        ("来源", "来源"),
    ):
        page.get_by_role("link", name=nav_label, exact=True).click()
        expect(page.get_by_role("heading", name=heading, exact=True)).to_be_visible()
        expect(html).to_have_attribute("lang", "zh-CN")

    page.get_by_role("button", name="English", exact=True).click()
    assert page.evaluate("localStorage.getItem('pmh-language')") == "en"
    expect(html).to_have_attribute("lang", "en")
    expect(page.get_by_role("heading", name="Sources", exact=True)).to_be_visible()

    for source in ("trae", "workbuddy", "zcode", "qoderwork", "claude_code"):
        optional_row = page.locator(f'tr[data-source="{source}"]')
        expect(optional_row.locator('form[action$="/enable"]')).to_have_count(0)
        expect(optional_row.locator('form[action*="/import"]')).to_have_count(0)
        expect(
            optional_row.get_by_role("button", name=re.compile(r"^(Enable|Import)$"))
        ).to_have_count(0)

    page.evaluate("localStorage.setItem('pmh-language', 'unsupported-locale')")
    page.reload(wait_until="domcontentloaded")
    expect(html).to_have_attribute("lang", "en")
    expect(page.get_by_role("heading", name="Sources", exact=True)).to_be_visible()
    assert _probe_persistence_snapshot(dashboard) == before


def test_language_switch_falls_back_to_english_when_storage_is_blocked(
    chromium: Browser,
    dashboard: _DashboardState,
) -> None:
    context = chromium.new_context(locale="zh-CN")
    context.add_init_script(
        """
        Object.defineProperty(window, "localStorage", {
          configurable: true,
          get() { throw new DOMException("blocked", "SecurityError"); }
        });
        """
    )
    selected_page = context.new_page()
    try:
        _bootstrap(selected_page, dashboard)
        expect(selected_page.locator("html")).to_have_attribute("lang", "en")
        expect(selected_page.get_by_role("heading", name="Overview", exact=True)).to_be_visible()
        selected_page.get_by_role("button", name="中文", exact=True).click()
        expect(selected_page.locator("html")).to_have_attribute("lang", "zh-CN")
        selected_page.reload(wait_until="domcontentloaded")
        expect(selected_page.locator("html")).to_have_attribute("lang", "en")
    finally:
        context.close()


def test_i18n_javascript_has_enforced_numeric_coverage(
    chromium: Browser,
    dashboard: _DashboardState,
) -> None:
    before = _probe_persistence_snapshot(dashboard)
    context = chromium.new_context(locale="zh-CN")
    selected_page = context.new_page()
    session = _start_javascript_coverage(selected_page)
    try:
        _bootstrap(selected_page, dashboard)
        expect(selected_page.locator("html")).to_have_attribute("lang", "en")
        source = _i18n_javascript_source()
        served_source = selected_page.evaluate(
            "async () => await (await fetch('/static/i18n.js')).text()"
        )
        assert served_source == source

        selected_page.evaluate(
            """
            () => {
              const option = document.querySelector("[data-language-option]");
              option.setAttribute("data-language-option", "unsupported-locale");
              option.click();
              option.setAttribute("data-language-option", "zh-CN");

              const missingText = document.createElement("span");
              missingText.id = "coverage-missing-text";
              missingText.setAttribute("data-i18n", "");
              missingText.textContent = "EMPTY_KEY_SENTINEL";
              document.body.appendChild(missingText);

              const unknownText = document.createElement("span");
              unknownText.id = "coverage-unknown-text";
              unknownText.setAttribute("data-i18n", "coverage.unknown");
              unknownText.textContent = "UNKNOWN_KEY_SENTINEL";
              document.body.appendChild(unknownText);

              const missingCount = document.createElement("span");
              missingCount.id = "coverage-missing-count";
              missingCount.setAttribute("data-i18n-count", "overview.projects");
              missingCount.textContent = "MISSING_COUNT_SENTINEL";
              document.body.appendChild(missingCount);

              const unknownCount = document.createElement("span");
              unknownCount.id = "coverage-unknown-count";
              unknownCount.setAttribute("data-i18n-count", "coverage.unknown");
              unknownCount.setAttribute("data-count", "2");
              unknownCount.textContent = "UNKNOWN_COUNT_SENTINEL";
              document.body.appendChild(unknownCount);

              const unknownTitle = document.createElement("span");
              unknownTitle.id = "coverage-unknown-title";
              unknownTitle.setAttribute("data-i18n-title", "coverage.unknown");
              unknownTitle.setAttribute("title", "UNKNOWN_TITLE_SENTINEL");
              document.body.appendChild(unknownTitle);
            }
            """
        )
        expect(selected_page.locator("html")).to_have_attribute("lang", "en")
        assert selected_page.evaluate("localStorage.getItem('pmh-language')") is None
        selected_page.get_by_role("button", name="中文", exact=True).click()
        expect(selected_page.locator("html")).to_have_attribute("lang", "zh-CN")
        expect(selected_page.locator("#coverage-missing-text")).to_have_text("EMPTY_KEY_SENTINEL")
        expect(selected_page.locator("#coverage-unknown-text")).to_have_text("UNKNOWN_KEY_SENTINEL")
        expect(selected_page.locator("#coverage-missing-count")).to_have_text(
            "MISSING_COUNT_SENTINEL"
        )
        expect(selected_page.locator("#coverage-unknown-count")).to_have_text(
            "UNKNOWN_COUNT_SENTINEL"
        )
        expect(selected_page.locator("#coverage-unknown-title")).to_have_attribute(
            "title", "UNKNOWN_TITLE_SENTINEL"
        )

        for nav_label, heading in (
            ("来源", "来源"),
            ("项目", "项目"),
            ("记忆", "记忆"),
            ("导入", "导入"),
            ("提案", "提案"),
            ("设置", "设置"),
            ("总览", "总览"),
        ):
            selected_page.get_by_role("link", name=nav_label, exact=True).click()
            expect(selected_page.get_by_role("heading", name=heading, exact=True)).to_be_visible()

        selected_page.get_by_role("button", name="English", exact=True).click()
        selected_page.evaluate("localStorage.setItem('pmh-language', 'unsupported-locale')")
        selected_page.reload(wait_until="domcontentloaded")
        expect(selected_page.locator("html")).to_have_attribute("lang", "en")

        context.add_init_script(
            """
            Object.defineProperty(window, "localStorage", {
              configurable: true,
              get() { throw new DOMException("blocked", "SecurityError"); }
            });
            """
        )
        selected_page.reload(wait_until="domcontentloaded")
        selected_page.get_by_role("button", name="中文", exact=True).click()
        expect(selected_page.locator("html")).to_have_attribute("lang", "zh-CN")

        coverage = _stop_i18n_javascript_coverage(session, source=source)
    finally:
        context.close()

    assert _probe_persistence_snapshot(dashboard) == before
    assert coverage >= _I18N_JAVASCRIPT_COVERAGE_MINIMUM, (
        f"i18n.js V8 source coverage was {coverage:.2f}%; "
        f"minimum is {_I18N_JAVASCRIPT_COVERAGE_MINIMUM:.2f}%"
    )


def test_projects_javascript_has_enforced_numeric_and_branch_coverage(
    chromium: Browser,
    dashboard: _DashboardState,
) -> None:
    before = _probe_persistence_snapshot(dashboard)
    context = chromium.new_context(locale="en-US")
    selected_page = context.new_page()
    session = _start_javascript_coverage(selected_page)
    source = _projects_javascript_source()

    def append_script() -> None:
        selected_page.evaluate(
            """
            () => new Promise((resolve, reject) => {
              const script = document.createElement("script");
              script.src = `/static/projects.js?coverage=${Date.now()}`;
              script.onload = () => { script.remove(); resolve(); };
              script.onerror = reject;
              document.head.appendChild(script);
            })
            """
        )

    try:
        _bootstrap(selected_page, dashboard)
        selected_page.get_by_role("link", name="Projects", exact=True).click()
        root = selected_page.locator("[data-project-browser]")
        cards = root.locator("[data-project-card]:visible")
        expect(cards).to_have_count(12)
        root.locator("[data-project-show-more]").click()
        expect(cards).to_have_count(24)

        search = root.locator("[data-project-search]")
        status = root.locator("[data-project-status-filter]")
        search.fill("Collection project")
        status.select_option("disabled")
        expect(cards).to_have_count(1)
        status.select_option("inactive")
        expect(cards).to_have_count(1)
        status.select_option("permission")
        expect(cards).to_have_count(0)
        search.fill("no matching project")
        expect(root.locator("[data-project-no-results]")).to_be_visible()
        search.fill("")
        status.select_option("all")

        selected_page.evaluate(
            """
            () => {
              const select = document.querySelector("[data-project-status-filter]");
              const option = document.createElement("option");
              option.value = "unexpected";
              option.textContent = "Unexpected";
              select.appendChild(option);
              select.value = "unexpected";
              select.dispatchEvent(new Event("change", { bubbles: true }));
            }
            """
        )
        expect(cards).to_have_count(12)

        selected_page.evaluate(
            """
            () => {
              const total = document.querySelector("[data-project-total-count]");
              total.setAttribute = () => { throw new Error("coverage failure"); };
              const search = document.querySelector("[data-project-search]");
              search.value = "Collection";
              search.dispatchEvent(new Event("input", { bubbles: true }));
            }
            """
        )
        expect(root.locator("[data-project-card]:visible")).to_have_count(25)
        expect(root.locator("[data-project-controls]")).to_be_hidden()
        expect(root).not_to_have_class(re.compile(r"\bprojects-enhanced\b"))

        selected_page.reload(wait_until="domcontentloaded")
        selected_page.locator("[data-project-browser]").evaluate(
            "element => element.removeAttribute('data-project-browser')"
        )
        append_script()

        selected_page.reload(wait_until="domcontentloaded")
        selected_page.locator("[data-project-browser]").evaluate(
            "element => element.setAttribute('data-project-page-size', '13')"
        )
        append_script()

        selected_page.reload(wait_until="domcontentloaded")
        selected_page.locator("[data-project-total-count]").evaluate("element => element.remove()")
        append_script()

        selected_page.reload(wait_until="domcontentloaded")
        selected_page.locator("[data-project-card]").first.evaluate(
            "element => element.setAttribute('data-project-status', 'invalid')"
        )
        append_script()

        served_source = selected_page.evaluate(
            "async () => await (await fetch('/static/projects.js')).text()"
        )
        assert served_source == source
        coverage = _stop_javascript_coverage(
            session,
            source=source,
            script_path="/static/projects.js",
        )
    finally:
        context.close()

    assert _probe_persistence_snapshot(dashboard) == before
    assert coverage >= _PROJECTS_JAVASCRIPT_COVERAGE_MINIMUM, (
        f"projects.js V8 block coverage was {coverage:.2f}%; "
        f"minimum is {_PROJECTS_JAVASCRIPT_COVERAGE_MINIMUM:.2f}%"
    )


def test_source_probe_trae_structure_is_transient_and_zero_write(
    page: Page,
    dashboard: _DashboardState,
) -> None:
    before = _probe_persistence_snapshot(dashboard)
    probe_home = _probe_home_for(dashboard.config_path)

    page.get_by_role("link", name="Sources", exact=True).click()
    trae = page.locator('tr[data-source="trae"]')
    trae.get_by_role("button", name="Further check", exact=True).click()

    expect(page).to_have_url(f"{dashboard.base_url}/sources")
    expect(trae).to_contain_text("Unverifiable")
    expect(trae).to_contain_text("model_id_unverifiable")
    expect(trae).to_contain_text("Locked")

    body = page.locator("body")
    expect(body).not_to_contain_text(_SYNTHETIC_TRAE_BODY)
    expect(body).not_to_contain_text(_SYNTHETIC_TRAE_SCHEMA)
    expect(body).not_to_contain_text(str(probe_home))

    page.reload(wait_until="domcontentloaded")

    expect(page).to_have_url(f"{dashboard.base_url}/sources")
    expect(trae).to_contain_text("Not checked")
    expect(trae).to_contain_text("Not run")
    expect(trae).not_to_contain_text("model_id_unverifiable")
    assert _probe_persistence_snapshot(dashboard) == before


def test_exact_namespace_cards_distinguish_resolved_and_archived(
    page: Page,
    dashboard: _DashboardState,
) -> None:
    page.get_by_role("link", name="Memories", exact=True).click()
    page.locator('select[name="project_id"]').select_option(str(dashboard.project_id))
    page.locator('select[name="source_agent"]').select_option("codex")
    model_filter = page.locator('form.filter-panel input[name="model_id"]')
    model_filter.fill("browser-model-a")
    page.get_by_role("button", name="Load project memory", exact=True).click()

    resolved = page.locator("article.memory-card").filter(has_text=RESOLVED_CARD)
    archived = page.locator("article.memory-card").filter(has_text=ARCHIVED_CARD)
    expect(resolved).to_have_count(1)
    expect(resolved.locator("header")).to_contain_text("Resolved")
    expect(archived).to_have_count(1)
    expect(archived.locator("header")).to_contain_text("Archived")

    model_filter.fill("browser-model-b")
    page.get_by_role("button", name="Load project memory", exact=True).click()
    expect(page.get_by_text("BROWSER_MODEL_B_ONLY", exact=True)).to_be_visible()
    expect(page.get_by_text(RESOLVED_CARD, exact=True)).to_have_count(0)
    expect(page.get_by_text(ARCHIVED_CARD, exact=True)).to_have_count(0)


def test_chatgpt_import_dry_run_shows_matches_and_writes_no_database_rows(
    page: Page,
    dashboard: _DashboardState,
) -> None:
    before = _database_digest(dashboard.database_path)
    page.get_by_role("link", name="Imports", exact=True).click()
    page.locator('input[name="archive"]').set_input_files(dashboard.archive_path)
    expect(page.locator('input[name="dry_run"]')).to_be_checked()
    page.get_by_role("button", name="Inspect locally", exact=True).click()

    assert _database_digest(dashboard.database_path) == before
    location = urlsplit(page.url)
    assert location.path == "/imports"
    assert parse_qs(location.query).get("status") == ["checked"]
    expect(page.locator("body")).to_contain_text(
        re.compile(
            r"(?:dry[- ]run matches?\s*[:=]?\s*1|1 dry[- ]run matches?|1 matches?)",
            re.IGNORECASE,
        )
    )


def test_proposal_approve_and_reject_are_csrf_protected_posts(
    page: Page,
    dashboard: _DashboardState,
) -> None:
    page.get_by_role("link", name="Proposals", exact=True).click()
    approve_path = f"/proposals/{dashboard.approve_proposal_id}/approve"
    reject_path = f"/proposals/{dashboard.reject_proposal_id}/reject"

    approve_card = page.locator("article.proposal-card").filter(has_text="Approve browser proposal")
    reject_card = page.locator("article.proposal-card").filter(has_text="Reject browser proposal")
    approve_form = approve_card.locator(f'form[action="{approve_path}"]')
    reject_form = reject_card.locator(f'form[action="{reject_path}"]')
    assert approve_form.get_attribute("method") == "post"
    assert reject_form.get_attribute("method") == "post"
    expect(approve_form.locator('input[type="hidden"][name="csrf_token"]')).to_have_count(1)
    expect(reject_form.locator('input[type="hidden"][name="csrf_token"]')).to_have_count(1)

    denied = page.context.request.post(
        f"{dashboard.base_url}{reject_path}",
        headers={"origin": dashboard.base_url},
        form={"confirmation": "REJECT"},
        fail_on_status_code=False,
    )
    assert denied.status == 403

    approve_card.get_by_role("button", name="Approve", exact=True).click()
    approve_card = page.locator("article.proposal-card").filter(has_text="Approve browser proposal")
    expect(approve_card).to_contain_text("approved")

    reject_card = page.locator("article.proposal-card").filter(has_text="Reject browser proposal")
    reject_card.get_by_role("button", name="Reject", exact=True).click()
    reject_card = page.locator("article.proposal-card").filter(has_text="Reject browser proposal")
    expect(reject_card).to_contain_text("rejected")
