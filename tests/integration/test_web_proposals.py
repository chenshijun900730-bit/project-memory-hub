from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx

from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import SourceAgent
from project_memory_hub.improvement.models import ProposalDraft
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.web.app import create_app


def _git_repository(path: Path) -> Path:
    path.mkdir()
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Test User"),
        ("config", "user.email", "test@example.invalid"),
    ):
        subprocess.run(
            ["git", "-C", str(path), *command],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    for command in (("add", "README.md"), ("commit", "-m", "initial")):
        subprocess.run(
            ["git", "-C", str(path), *command],
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        )
    return path


def _true_executable() -> str:
    executable = shutil.which("true")
    assert executable is not None
    return str(Path(executable).resolve(strict=True))


def _container(
    tmp_path: Path,
    *,
    repository_root: Path | None = None,
    commands: tuple[tuple[str, ...], ...] = (),
):
    project_root = tmp_path / "projects"
    project_root.mkdir(exist_ok=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    config_path = runtime / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(project_root,),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
            improvement_repository_root=repository_root,
            improvement_verification_commands=commands,
        )
    )
    return build_container(config_path)


async def _client(container):
    app = create_app(container)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    token = LocalAccessToken.load_or_create(container.paths)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as bootstrap:
        boot = await bootstrap.get(f"/?token={token}", follow_redirects=False)
    client = httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
        cookies=boot.cookies,
    )
    return client, boot.headers["x-project-memory-hub-csrf"]


def _headers(csrf: str) -> dict[str, str]:
    return {"origin": "http://127.0.0.1", "x-csrf-token": csrf}


def _patch(replacement: str = "updated") -> str:
    return (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-seed\n"
        f"+{replacement}\n"
    )


def _draft(
    signature: str,
    command: tuple[str, ...],
    *,
    replacement: str = "updated",
) -> ProposalDraft:
    return ProposalDraft(
        signature=signature,
        title=f"Review {signature}",
        description="Bounded review description for a local proposal.",
        risk="low",
        patch=_patch(replacement),
        verification_argv=command,
        target_version=None,
        origin="local_cli",
    )


def test_proposal_page_exposes_only_safe_review_projection_and_valid_actions(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(), "PRIVATE_VERIFIER_ARGUMENT")

    async def scenario() -> tuple[str, str, str, str]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            draft = container.proposal_service.create(
                _draft("web.safe-draft", command, replacement="DRAFT_PATCH_SENTINEL")
            ).record
            approved = container.proposal_service.create(
                _draft(
                    "web.safe-approved",
                    command,
                    replacement="APPROVED_PATCH_SENTINEL",
                )
            ).record
            container.proposal_service.approve(
                approved.proposal_id,
                actor="test-actor",
            )
            with container.database.transaction() as connection:
                connection.execute(
                    "update improvement_proposals set verification_summary = ? "
                    "where proposal_id = ?",
                    ("bounded verification summary", str(draft.proposal_id)),
                )
            client, _csrf = await _client(container)
            async with client:
                response = await client.get("/proposals")
            return (
                response.text,
                str(draft.proposal_id),
                str(approved.proposal_id),
                hashlib.sha256(_patch("DRAFT_PATCH_SENTINEL").encode()).hexdigest(),
            )

    page, draft_id, approved_id, patch_digest = asyncio.run(scenario())
    assert "Bounded review description" in page
    assert "bounded verification summary" in page
    assert patch_digest in page
    assert "Configured check 1" in page
    assert f"/proposals/{draft_id}/approve" in page
    assert f"/proposals/{draft_id}/reject" in page
    assert f"/proposals/{draft_id}/apply" not in page
    assert f"/proposals/{approved_id}/reject" in page
    assert f"/proposals/{approved_id}/apply" in page
    for hidden in (
        "DRAFT_PATCH_SENTINEL",
        "APPROVED_PATCH_SENTINEL",
        "PRIVATE_VERIFIER_ARGUMENT",
        command[0],
        str(repository),
        "codex/memory-hub-proposal-",
    ):
        assert hidden not in page


def test_proposal_posts_revalidate_state_confirmation_and_csrf(tmp_path: Path) -> None:
    command = (_true_executable(),)

    async def scenario() -> tuple[list[int], tuple[object, ...]]:
        with _container(tmp_path) as container:
            record = container.proposal_service.create(_draft("web.state-matrix", command)).record
            client, csrf = await _client(container)
            path = f"/proposals/{record.proposal_id}"
            async with client:
                statuses = [
                    (
                        await client.post(
                            f"{path}/approve",
                            headers={"origin": "http://127.0.0.1"},
                            data={"confirmation": "APPROVE"},
                        )
                    ).status_code,
                    (
                        await client.post(
                            f"{path}/apply",
                            headers=_headers(csrf),
                            data={"confirmation": "APPLY"},
                        )
                    ).status_code,
                    (
                        await client.post(
                            f"{path}/approve",
                            headers=_headers(csrf),
                            data={"confirmation": "WRONG"},
                        )
                    ).status_code,
                    (
                        await client.post(
                            f"{path}/approve",
                            headers={
                                **_headers(csrf),
                                "content-type": "application/x-www-form-urlencoded",
                            },
                            content=("confirmation=APPROVE&confirmation=APPROVE"),
                        )
                    ).status_code,
                    (
                        await client.post(
                            f"{path}/approve",
                            headers=_headers(csrf),
                            data={
                                "confirmation": "APPROVE",
                                "actor": "attacker-controlled-actor",
                            },
                        )
                    ).status_code,
                    (
                        await client.post(
                            f"{path}/approve",
                            headers=_headers(csrf),
                            data={"confirmation": "APPROVE"},
                        )
                    ).status_code,
                    (
                        await client.post(
                            f"{path}/reject",
                            headers=_headers(csrf),
                            data={"confirmation": "REJECT"},
                        )
                    ).status_code,
                    (
                        await client.post(
                            f"/proposals/{uuid4()}/reject",
                            headers=_headers(csrf),
                            data={"confirmation": "REJECT"},
                        )
                    ).status_code,
                ]
            with container.database.connect(readonly=True) as connection:
                state = tuple(
                    connection.execute(
                        "select approval_status, approval_actor, approved_at "
                        "from improvement_proposals where proposal_id = ?",
                        (str(record.proposal_id),),
                    ).fetchone()
                )
            return statuses, state

    statuses, state = asyncio.run(scenario())
    assert statuses == [403, 409, 409, 400, 303, 409, 303, 404]
    assert state[0] == "rejected"
    assert state[1] == "local-control-panel"
    assert state[2] is not None


def test_web_apply_and_rollback_use_isolated_git_result(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(),)

    async def scenario() -> tuple[int, int, str, str, str]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(_draft("web.real-apply", command)).record
            approved = container.proposal_service.approve(
                created.proposal_id,
                actor="test-actor",
            )
            client, csrf = await _client(container)
            path = f"/proposals/{approved.proposal_id}"
            async with client:
                applied_response = await client.post(
                    f"{path}/apply",
                    headers=_headers(csrf),
                    data={"confirmation": "APPLY"},
                )
                applied = container.proposal_service.get(approved.proposal_id)
                page = await client.get("/proposals")
                rollback_response = await client.post(
                    f"{path}/rollback",
                    headers=_headers(csrf),
                    data={"confirmation": "ROLLBACK"},
                )
                rolled_back = container.proposal_service.get(approved.proposal_id)
            return (
                applied_response.status_code,
                rollback_response.status_code,
                applied.status,
                rolled_back.status,
                page.text,
            )

    apply_status, rollback_status, applied, rolled_back, page = asyncio.run(scenario())
    assert (apply_status, rollback_status) == (303, 303)
    assert (applied, rolled_back) == ("applied", "rolled_back")
    assert "Rollback" in page
    assert "seed\n" == (repository / "README.md").read_text(encoding="utf-8")
    assert (
        subprocess.run(
            ["git", "-C", str(repository), "branch", "--show-current"],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=8,
        ).stdout.strip()
        == "main"
    )


def test_web_apply_does_not_block_the_event_loop(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(),)

    async def scenario() -> tuple[int, float]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(
                _draft("web.threaded-apply", command)
            ).record
            approved = container.proposal_service.approve(
                created.proposal_id,
                actor="test-actor",
            )

            def slow_apply(_proposal_id):
                time.sleep(0.2)

            container.proposal_service.apply = slow_apply  # type: ignore[method-assign]
            client, csrf = await _client(container)
            started = time.monotonic()

            async def ticker() -> float:
                await asyncio.sleep(0.01)
                return time.monotonic() - started

            async with client:
                response, ticked_at = await asyncio.gather(
                    client.post(
                        f"/proposals/{approved.proposal_id}/apply",
                        headers=_headers(csrf),
                        data={"confirmation": "APPLY"},
                    ),
                    ticker(),
                )
            return response.status_code, ticked_at

    status, ticked_at = asyncio.run(scenario())
    assert status == 303
    assert ticked_at < 0.1


def test_corrupt_proposal_rows_are_skipped_and_actions_fail_closed(
    tmp_path: Path,
) -> None:
    secret = "TOPSECRETTOKEN123456789"
    proposal_id = uuid4()

    async def scenario() -> tuple[int, int, str, str]:
        with _container(tmp_path) as container:
            valid = container.proposal_service.create(
                ProposalDraft(
                    signature="web.valid-neighbor",
                    title="Visible safe neighbor",
                    description="A valid row remains visible.",
                    risk="low",
                    origin="analyzer",
                )
            ).record
            now = datetime.now(timezone.utc).isoformat()
            with container.database.transaction() as connection:
                connection.execute(
                    """
                    insert into improvement_proposals(
                        proposal_id, signature, title, description, patch, risk,
                        verification_argv_json, approval_status, created_at,
                        updated_at, origin
                    ) values (?, ?, ?, ?, ?, 'low', ?, 'draft', ?, ?, 'local_cli')
                    """,
                    (
                        str(proposal_id),
                        "web.corrupt-row",
                        f"Bearer {secret}",
                        "unsafe persisted metadata",
                        _patch(),
                        "not-json",
                        now,
                        now,
                    ),
                )
            client, csrf = await _client(container)
            async with client:
                page = await client.get("/proposals")
                action = await client.post(
                    f"/proposals/{proposal_id}/approve",
                    headers=_headers(csrf),
                    data={"confirmation": "APPROVE"},
                )
            with container.database.connect(readonly=True) as connection:
                status = connection.execute(
                    "select approval_status from improvement_proposals where proposal_id = ?",
                    (str(proposal_id),),
                ).fetchone()[0]
            assert str(valid.proposal_id) in page.text
            return page.status_code, action.status_code, page.text, status

    page_status, action_status, page, state = asyncio.run(scenario())
    assert page_status == 200
    assert action_status == 409
    assert secret not in page
    assert str(proposal_id) not in page
    assert state == "draft"


def test_direct_posts_cannot_bypass_disabled_action_matrix(tmp_path: Path) -> None:
    command = (_true_executable(),)

    async def scenario() -> tuple[int, int, str, str]:
        with _container(tmp_path) as container:
            executable = container.proposal_service.create(
                _draft("web.execution-disabled", command)
            ).record
            approved = container.proposal_service.approve(
                executable.proposal_id,
                actor="test-actor",
            )
            analyzer = container.proposal_service.create(
                ProposalDraft(
                    signature="web.analyzer-inert",
                    title="Analyzer suggestion",
                    description="Health metadata only.",
                    risk="low",
                    origin="analyzer",
                )
            ).record
            client, csrf = await _client(container)
            async with client:
                page = await client.get("/proposals")
                apply_response = await client.post(
                    f"/proposals/{approved.proposal_id}/apply",
                    headers=_headers(csrf),
                    data={"confirmation": "APPLY"},
                )
                approve_response = await client.post(
                    f"/proposals/{analyzer.proposal_id}/approve",
                    headers=_headers(csrf),
                    data={"confirmation": "APPROVE"},
                )
            return (
                apply_response.status_code,
                approve_response.status_code,
                container.proposal_service.get(approved.proposal_id).status,
                page.text,
            )

    apply_status, approve_status, approved_state, page = asyncio.run(scenario())
    assert (apply_status, approve_status) == (409, 409)
    assert approved_state == "approved"
    assert "/apply" not in page
    assert "Analyzer suggestion" in page


def test_page_state_drift_is_revalidated_before_mutation(tmp_path: Path) -> None:
    command = (_true_executable(),)

    async def scenario() -> tuple[int, str]:
        with _container(tmp_path) as container:
            record = container.proposal_service.create(_draft("web.state-drift", command)).record
            client, csrf = await _client(container)
            async with client:
                page = await client.get("/proposals")
                assert f"/proposals/{record.proposal_id}/approve" in page.text
                container.proposal_service.reject(record.proposal_id)
                response = await client.post(
                    f"/proposals/{record.proposal_id}/approve",
                    headers=_headers(csrf),
                    data={"confirmation": "APPROVE"},
                )
            return (
                response.status_code,
                container.proposal_service.get(record.proposal_id).status,
            )

    assert asyncio.run(scenario()) == (409, "rejected")


def test_git_failures_are_conflicts_without_private_error_content(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(),)
    patch_marker = "PRIVATE_PATCH_MARKER"

    async def scenario() -> tuple[httpx.Response, str]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(
                _draft("web.git-conflict", command, replacement=patch_marker)
            ).record
            approved = container.proposal_service.approve(
                created.proposal_id,
                actor="test-actor",
            )
            (repository / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            client, csrf = await _client(container)
            async with client:
                response = await client.post(
                    f"/proposals/{approved.proposal_id}/apply",
                    headers=_headers(csrf),
                    data={"confirmation": "APPLY"},
                )
            return response, container.proposal_service.get(approved.proposal_id).status

    response, state = asyncio.run(scenario())
    assert response.status_code == 409
    assert state == "approved"
    assert patch_marker not in response.text
    assert str(repository) not in response.text
    assert "worktree" not in response.text.lower()


def test_proposal_html_is_autoescaped_and_has_no_safe_filter(tmp_path: Path) -> None:
    dangerous = "<script>alert('proposal')</script>"

    async def scenario() -> str:
        with _container(tmp_path) as container:
            container.proposal_service.create(
                ProposalDraft(
                    signature="web.html-escape",
                    title=dangerous,
                    description="Safe description.",
                    risk="low",
                    origin="analyzer",
                )
            )
            client, _csrf = await _client(container)
            async with client:
                return (await client.get("/proposals")).text

    page = asyncio.run(scenario())
    template = (
        Path(__file__).parents[2] / "src/project_memory_hub/web/templates/proposals.html"
    ).read_text(encoding="utf-8")
    assert dangerous not in page
    assert "&lt;script&gt;" in page
    assert "|safe" not in template


def test_web_rollback_does_not_block_the_event_loop(tmp_path: Path) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(),)

    async def scenario() -> tuple[int, float]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(
                _draft("web.threaded-rollback", command)
            ).record
            approved = container.proposal_service.approve(
                created.proposal_id,
                actor="test-actor",
            )
            container.proposal_service.apply(approved.proposal_id)

            def slow_rollback(_proposal_id):
                time.sleep(0.2)

            container.proposal_service.rollback = (  # type: ignore[method-assign]
                slow_rollback
            )
            client, csrf = await _client(container)
            started = time.monotonic()

            async def ticker() -> float:
                await asyncio.sleep(0.01)
                return time.monotonic() - started

            async with client:
                response, ticked_at = await asyncio.gather(
                    client.post(
                        f"/proposals/{approved.proposal_id}/rollback",
                        headers=_headers(csrf),
                        data={"confirmation": "ROLLBACK"},
                    ),
                    ticker(),
                )
            return response.status_code, ticked_at

    status, ticked_at = asyncio.run(scenario())
    assert status == 303
    assert ticked_at < 0.1


def test_applying_state_offers_recovery_without_rendering_git_metadata(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    subprocess.run(
        [
            "git",
            "-C",
            str(repository),
            "switch",
            "-c",
            "private-original-branch-sentinel",
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=8,
    )
    command = (_true_executable(),)

    async def scenario() -> tuple[str, str, str, str]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(_draft("web.recovery", command)).record
            approved = container.proposal_service.approve(
                created.proposal_id,
                actor="test-actor",
            )
            assert container.proposal_applier is not None
            plan = container.proposal_applier.preflight(approved)
            container.proposals.begin_apply(
                approved.proposal_id,
                apply_attempt_id=uuid4(),
                repository_root=plan.repository_root,
                original_branch=plan.original_branch,
                base_commit=plan.base_commit,
                proposal_branch=plan.proposal_branch,
            )
            client, _csrf = await _client(container)
            async with client:
                page = (await client.get("/proposals")).text
            return (
                page,
                plan.original_branch,
                plan.base_commit,
                plan.proposal_branch,
            )

    page, original_branch, base_commit, proposal_branch = asyncio.run(scenario())
    assert "Recover interrupted apply" in page
    assert "/apply" in page
    for hidden in (
        str(repository),
        original_branch,
        base_commit,
        proposal_branch,
    ):
        assert hidden not in page


def test_ref_inconsistent_applied_record_is_inert_in_ui_and_direct_post(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(),)
    drifted_ref = "codex/memory-hub-proposal-drifted-ref"

    async def scenario() -> tuple[str, int, str, str]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(_draft("web.ref-drift", command)).record
            approved = container.proposal_service.approve(
                created.proposal_id,
                actor="test-actor",
            )
            container.proposal_service.apply(approved.proposal_id)
            with container.database.transaction() as connection:
                connection.execute(
                    "update improvement_proposals set proposal_branch = ? where proposal_id = ?",
                    (drifted_ref, str(approved.proposal_id)),
                )
            client, csrf = await _client(container)
            async with client:
                page = await client.get("/proposals")
                response = await client.post(
                    f"/proposals/{approved.proposal_id}/rollback",
                    headers=_headers(csrf),
                    data={"confirmation": "ROLLBACK"},
                )
            return (
                page.text,
                response.status_code,
                container.proposal_service.get(approved.proposal_id).status,
                str(approved.proposal_id),
            )

    page, status, state, proposal_id = asyncio.run(scenario())
    assert drifted_ref not in page
    assert f"/proposals/{proposal_id}/rollback" not in page
    assert status == 409
    assert state == "applied"


def test_truncated_review_metadata_disables_ui_and_direct_mutation(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(),)
    hidden_tail = "RISK_DETAIL_HIDDEN_BY_WEB_BOUND"

    async def scenario() -> tuple[str, int, str, str]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(
                ProposalDraft(
                    signature="web.truncated-review",
                    title="Review complete metadata before approval",
                    description=("A" * 650) + hidden_tail,
                    risk="low",
                    patch=_patch(),
                    verification_argv=command,
                    origin="local_cli",
                )
            ).record
            client, csrf = await _client(container)
            async with client:
                page = await client.get("/proposals")
                response = await client.post(
                    f"/proposals/{created.proposal_id}/approve",
                    headers=_headers(csrf),
                    data={"confirmation": "APPROVE"},
                )
            return (
                page.text,
                response.status_code,
                container.proposal_service.get(created.proposal_id).status,
                str(created.proposal_id),
            )

    page, status, state, proposal_id = asyncio.run(scenario())
    assert hidden_tail not in page
    assert "Review metadata was redacted or truncated" in page
    assert f"/proposals/{proposal_id}/approve" not in page
    assert f"/proposals/{proposal_id}/reject" not in page
    assert status == 409
    assert state == "draft"


def test_redacted_review_metadata_disables_ui_and_direct_mutation(
    tmp_path: Path,
) -> None:
    repository = _git_repository(tmp_path / "repository")
    command = (_true_executable(),)

    async def scenario() -> tuple[str, int, str, str]:
        with _container(
            tmp_path,
            repository_root=repository,
            commands=(command,),
        ) as container:
            created = container.proposal_service.create(
                ProposalDraft(
                    signature="web.redacted-review",
                    title="Review password=private-value before approval",
                    description="A secret-bearing title must become read-only.",
                    risk="low",
                    patch=_patch(),
                    verification_argv=command,
                    origin="local_cli",
                )
            ).record
            client, csrf = await _client(container)
            async with client:
                page = await client.get("/proposals")
                response = await client.post(
                    f"/proposals/{created.proposal_id}/approve",
                    headers=_headers(csrf),
                    data={"confirmation": "APPROVE"},
                )
            return (
                page.text,
                response.status_code,
                container.proposal_service.get(created.proposal_id).status,
                str(created.proposal_id),
            )

    page, status, state, proposal_id = asyncio.run(scenario())
    assert "private-value" not in page
    assert "[REDACTED:password]" in page
    assert "Review metadata was redacted or truncated" in page
    assert f"/proposals/{proposal_id}/approve" not in page
    assert f"/proposals/{proposal_id}/reject" not in page
    assert status == 409
    assert state == "draft"
