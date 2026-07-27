from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest
import typer.testing as typer_testing
from typer.testing import CliRunner

import project_memory_hub.cli as cli_module
from project_memory_hub.cli import app
from project_memory_hub.improvement.models import (
    ApplyResult,
    ProposalCreateResult,
    ProposalDraft,
    ProposalRecord,
    ProposalSummary,
)
from project_memory_hub.improvement.service import (
    ProposalActionPreview,
    ProposalCreatePreview,
)
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.security.web import LocalAccessToken
from project_memory_hub.storage.proposals import ProposalError


runner = CliRunner()
PROPOSAL_ID = UUID("11111111-2222-4333-8444-555555555555")
PATCH_MARKER = "PRIVATE_PATCH_BODY_MUST_NOT_ECHO"
VERIFICATION_MARKER = "PRIVATE_VERIFICATION_OUTPUT_MUST_NOT_ECHO"
PRIVATE_PATH_MARKER = "PRIVATE_REPOSITORY_PATH_MUST_NOT_ECHO"
PATCH = f"""\
diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-seed
+{PATCH_MARKER}
"""
VERIFICATION_ARGV = ("/usr/bin/true", "--exact-proposal-verification")
NOW = datetime(2026, 7, 15, tzinfo=timezone.utc)


def _record(status: str = "draft") -> ProposalRecord:
    return ProposalRecord(
        proposal_id=PROPOSAL_ID,
        signature="cli.proposal.safe-change.v1",
        title="Review a bounded local improvement",
        description="A safe summary without raw execution material.",
        patch=PATCH,
        risk="low",
        verification_argv=VERIFICATION_ARGV,
        verification_summary=VERIFICATION_MARKER,
        status=status,
        target_version=None,
        rollback_ref=None,
        created_at=NOW,
        approved_at=NOW if status != "draft" else None,
        origin="local_cli",
        approval_actor="local-cli" if status != "draft" else None,
        updated_at=NOW,
        apply_attempt_id=None,
        repository_root=Path("/private") / PRIVATE_PATH_MARKER,
        original_branch="main",
        base_commit="a" * 40,
        proposal_branch=f"codex/memory-hub-proposal-{PROPOSAL_ID.hex}",
        applied_commit="b" * 40 if status in {"applied", "rolled_back"} else None,
        applied_at=NOW if status in {"applied", "rolled_back"} else None,
        rolled_back_at=NOW if status == "rolled_back" else None,
        failure_code=None,
    )


def _summary() -> ProposalSummary:
    record = _record()
    return ProposalSummary(
        proposal_id=record.proposal_id,
        signature=record.signature,
        title=record.title,
        description=record.description,
        risk=record.risk,
        status=record.status,
        origin=record.origin,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


class _FakeProposalService:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.raise_on: str | None = None
        self.failure: Exception | None = None

    def _raise_if_selected(self, operation: str) -> None:
        if self.raise_on == operation:
            if self.failure is not None:
                raise self.failure
            raise RuntimeError(f"{PATCH_MARKER} {VERIFICATION_MARKER} {PRIVATE_PATH_MARKER}")

    def list_summaries(self) -> tuple[ProposalSummary, ...]:
        self.calls.append(("list",))
        self._raise_if_selected("list")
        return (_summary(),)

    def get(self, proposal_id: UUID) -> ProposalRecord:
        self.calls.append(("get", proposal_id))
        self._raise_if_selected("get")
        return _record()

    def create(self, draft: ProposalDraft) -> ProposalCreateResult:
        self.calls.append(("create", draft))
        self._raise_if_selected("create")
        return ProposalCreateResult(inserted=True, duplicate=False, record=_record())

    def preview_create(self, draft: ProposalDraft) -> ProposalCreatePreview:
        self.calls.append(("preview_create", draft))
        self._raise_if_selected("preview_create")
        return ProposalCreatePreview(
            draft,
            None,
            False,
            ("database_write_boundary",),
        )

    def preview_action(
        self,
        proposal_id: UUID,
        *,
        action: str,
    ) -> ProposalActionPreview:
        self.calls.append(("preview_action", action, proposal_id))
        self._raise_if_selected("preview_action")
        mode = action
        return ProposalActionPreview(
            _record(),
            False,
            mode,  # type: ignore[arg-type]
            ("write_boundary",),
        )

    def approve(self, proposal_id: UUID, *, actor: str) -> ProposalRecord:
        self.calls.append(("approve", proposal_id, actor))
        self._raise_if_selected("approve")
        return _record("approved")

    def reject(self, proposal_id: UUID) -> ProposalRecord:
        self.calls.append(("reject", proposal_id))
        self._raise_if_selected("reject")
        return _record("rejected")

    def apply(self, proposal_id: UUID) -> ApplyResult:
        self.calls.append(("apply", proposal_id))
        self._raise_if_selected("apply")
        return ApplyResult(
            proposal_id=proposal_id,
            repository_root=Path("/private") / PRIVATE_PATH_MARKER,
            original_branch="main",
            base_commit="a" * 40,
            proposal_branch=f"codex/memory-hub-proposal-{proposal_id.hex}",
            applied_commit="b" * 40,
            verification_summary=VERIFICATION_MARKER,
        )

    def rollback(self, proposal_id: UUID) -> ProposalRecord:
        self.calls.append(("rollback", proposal_id))
        self._raise_if_selected("rollback")
        return _record("rolled_back")


class _FakeContainer:
    def __init__(self, root: Path, service: _FakeProposalService) -> None:
        self.paths = RuntimePaths.for_root(root)
        self.paths.ensure()
        self.proposal_service = service
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _TTYBytesIO(io.BytesIO):
    def isatty(self) -> bool:
        return True


def _container(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    service: _FakeProposalService | None = None,
) -> tuple[_FakeContainer, _FakeProposalService, str]:
    selected_service = service or _FakeProposalService()
    container = _FakeContainer(tmp_path / "runtime", selected_service)
    token = LocalAccessToken.load_or_create(container.paths)
    monkeypatch.setattr(cli_module, "build_container", lambda _path: container)
    monkeypatch.setattr(
        cli_module,
        "build_readonly_proposal_container",
        lambda _path: container,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "runtime_paths_for_config",
        lambda _path: container.paths,
    )
    return container, selected_service, token


def _create_document(token: str | None) -> dict[str, object]:
    document: dict[str, object] = {
        "signature": "cli.proposal.safe-change.v1",
        "title": "Review a bounded local improvement",
        "description": "Create an approval-gated proposal.",
        "risk": "low",
        "patch": PATCH,
        "verification_argv": list(VERIFICATION_ARGV),
        "target_version": None,
    }
    if token is not None:
        document["token"] = token
    return document


def _invoke_mutation(
    command: str,
    token: str | None,
    *,
    dry_run: bool = False,
):
    arguments = ["proposal", command]
    document: dict[str, object]
    if command == "create":
        document = _create_document(token)
    else:
        arguments.append(str(PROPOSAL_ID))
        document = {} if token is None else {"token": token}
    arguments.extend(("--stdin-json", "--format", "json"))
    if dry_run:
        arguments.append("--dry-run")
    return runner.invoke(app, arguments, input=json.dumps(document))


def _assert_no_private_output(result, token: str) -> None:
    combined = result.stdout + result.stderr
    for marker in (token, PATCH_MARKER, VERIFICATION_MARKER, PRIVATE_PATH_MARKER):
        assert marker not in combined


def test_proposal_help_exposes_all_review_workflow_commands() -> None:
    result = runner.invoke(app, ["proposal", "--help"])

    assert result.exit_code == 0
    for command in ("list", "create", "approve", "reject", "apply", "rollback"):
        assert command in result.stdout
    assert "Unavailable in this release" not in result.stdout


@pytest.mark.parametrize("command", ("list", "approve-dry-run"))
def test_read_only_commands_never_initialize_a_missing_runtime(
    tmp_path: Path,
    command: str,
) -> None:
    runtime = tmp_path / "missing-runtime"
    arguments = ["--config", str(runtime / "config.toml"), "proposal"]
    input_value = None
    if command == "list":
        arguments.extend(("list", "--format", "json"))
    else:
        arguments.extend(
            (
                "approve",
                str(PROPOSAL_ID),
                "--stdin-json",
                "--dry-run",
                "--format",
                "json",
            )
        )
        input_value = json.dumps({"token": "A" * 43})

    result = runner.invoke(app, arguments, input=input_value)

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "permission_denied"
    assert not runtime.exists()


def test_proposal_list_is_read_only_and_emits_only_safe_summaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, service, token = _container(monkeypatch, tmp_path)

    result = runner.invoke(app, ["proposal", "list", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "ok"
    assert payload["proposals"][0]["proposal_id"] == str(PROPOSAL_ID)
    assert payload["proposals"][0]["status"] == "draft"
    assert service.calls == [("list",)]
    assert container.closed is True
    _assert_no_private_output(result, token)


def test_proposal_list_default_text_contains_the_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container_value, _service, token = _container(monkeypatch, tmp_path)

    result = runner.invoke(app, ["proposal", "list"])

    assert result.exit_code == 0
    assert str(PROPOSAL_ID) in result.stdout
    assert "Review a bounded local improvement" in result.stdout
    assert "draft/low" in result.stdout
    _assert_no_private_output(result, token)


def test_proposal_create_reads_exact_bounded_json_stdin_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    container, service, token = _container(monkeypatch, tmp_path)

    result = _invoke_mutation("create", token)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "draft"
    assert payload["proposal_id"] == str(PROPOSAL_ID)
    operation, draft = service.calls[0]
    assert operation == "create"
    assert isinstance(draft, ProposalDraft)
    assert draft.patch == PATCH
    assert draft.verification_argv == VERIFICATION_ARGV
    assert draft.origin == "local_cli"
    assert container.closed is True
    _assert_no_private_output(result, token)


@pytest.mark.parametrize("command", ("create", "approve", "reject", "apply", "rollback"))
def test_non_tty_proposal_mutations_require_token_in_json_stdin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _container_value, service, _token = _container(monkeypatch, tmp_path)

    result = _invoke_mutation(command, None)

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "permission_denied"
    assert not service.calls


@pytest.mark.parametrize(
    ("command", "expected_status"),
    (
        ("approve", "approved"),
        ("reject", "rejected"),
        ("apply", "applied"),
        ("rollback", "rolled_back"),
    ),
)
def test_non_tty_proposal_mutations_accept_only_valid_stdin_token_and_emit_safe_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    expected_status: str,
) -> None:
    container, service, token = _container(monkeypatch, tmp_path)

    result = _invoke_mutation(command, token)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["proposal_id"] == str(PROPOSAL_ID)
    assert payload["status"] == expected_status
    assert service.calls[0][0] == command
    assert service.calls[0][1] == PROPOSAL_ID
    assert container.closed is True
    _assert_no_private_output(result, token)


def test_tty_mutation_requires_a_clear_confirmation_without_stdin_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _container_value, service, _token = _container(monkeypatch, tmp_path)

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)

    result = runner.invoke(
        app,
        ["proposal", "approve", str(PROPOSAL_ID)],
        input="y\n",
    )

    assert result.exit_code == 0
    assert "approve" in result.stdout.casefold()
    assert str(PROPOSAL_ID) in result.stdout
    assert service.calls[0][0] == "approve"


@pytest.mark.parametrize("valid_token", (True, False))
def test_yes_requires_hidden_tty_token_authentication_before_skipping_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    valid_token: bool,
) -> None:
    _container_value, service, token = _container(monkeypatch, tmp_path)

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)
    supplied = token if valid_token else "A" * 43

    result = runner.invoke(
        app,
        ["proposal", "approve", str(PROPOSAL_ID), "--yes"],
        input=f"{supplied}\n",
    )

    assert result.exit_code == (0 if valid_token else 2)
    assert bool(service.calls) is valid_token
    assert token not in result.stdout + result.stderr


def test_tty_create_reads_one_json_line_then_requires_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _container_value, service, _token = _container(monkeypatch, tmp_path)

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)
    document = json.dumps(_create_document(None), separators=(",", ":"))

    result = runner.invoke(
        app,
        ["proposal", "create"],
        input=f"{document}\ny\n",
    )

    assert result.exit_code == 0
    assert "draft" in result.stdout
    assert service.calls[0][0] == "create"
    assert PATCH_MARKER not in result.stdout + result.stderr


def test_tty_create_yes_reads_token_only_through_hidden_auth_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container_value, service, token = _container(monkeypatch, tmp_path)

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)
    document = json.dumps(_create_document(None), separators=(",", ":"))

    result = runner.invoke(
        app,
        ["proposal", "create", "--yes"],
        input=f"{document}\n{token}\n",
    )

    assert result.exit_code == 0
    assert service.calls[0][0] == "create"
    assert "Local access token" in result.stdout
    assert token not in result.stdout + result.stderr


def test_tty_create_rejects_a_token_embedded_in_the_visible_json_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container_value, service, token = _container(monkeypatch, tmp_path)

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)

    result = runner.invoke(
        app,
        ["proposal", "create", "--yes"],
        input=f"{json.dumps(_create_document(token))}\n",
    )

    assert result.exit_code == 4
    assert not service.calls
    assert token not in result.stdout + result.stderr


def test_tty_create_enforces_the_same_one_mib_stdin_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container_value, service, _token = _container(monkeypatch, tmp_path)

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)
    document = _create_document(None)
    document["patch"] = PATCH_MARKER + "x" * (1024 * 1024)

    result = runner.invoke(
        app,
        ["proposal", "create", "--format", "text"],
        input=f"{json.dumps(document)}\ny\n",
    )

    assert result.exit_code == 4
    assert not service.calls
    assert PATCH_MARKER not in result.stdout + result.stderr


@pytest.mark.parametrize("command", ("create", "approve"))
def test_json_mode_never_falls_back_to_tty_confirmation_without_stdin_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    _container_value, service, _token = _container(monkeypatch, tmp_path)

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)
    arguments = ["proposal", command]
    if command != "create":
        arguments.append(str(PROPOSAL_ID))
    arguments.extend(("--format", "json"))

    result = runner.invoke(app, arguments, input="y\n")

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert not service.calls


@pytest.mark.parametrize("command", ("create", "approve", "reject", "apply", "rollback"))
def test_proposal_dry_run_never_calls_a_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    _container(monkeypatch, tmp_path)
    container = cli_module.build_container(None)
    token = LocalAccessToken.load_or_create(container.paths)

    result = _invoke_mutation(command, token, dry_run=True)

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["verification"] == "partial"
    assert payload["unverified"]
    mutation_names = {"create", "approve", "reject", "apply", "rollback"}
    assert not any(call[0] in mutation_names for call in container.proposal_service.calls)
    assert container.proposal_service.calls[0][0] in {
        "preview_create",
        "preview_action",
    }


def test_default_text_dry_run_discloses_partial_and_unverified_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _container_value, service, token = _container(monkeypatch, tmp_path)

    result = runner.invoke(
        app,
        [
            "proposal",
            "apply",
            str(PROPOSAL_ID),
            "--stdin-json",
            "--dry-run",
        ],
        input=json.dumps({"token": token}),
    )

    assert result.exit_code == 0
    assert "verification=partial" in result.stdout
    assert "unverified=write_boundary" in result.stdout
    assert service.calls[0][0] == "preview_action"
    _assert_no_private_output(result, token)


@pytest.mark.parametrize("command", ("list", "create", "approve", "apply"))
def test_read_only_proposal_commands_never_use_the_writable_container_or_token_creator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    container, _service, token = _container(monkeypatch, tmp_path)

    def writable_builder_rejected(_path):
        raise AssertionError("writable container must not be built")

    def token_creator_rejected(_paths):
        raise AssertionError("dry-run must not create a token")

    monkeypatch.setattr(cli_module, "build_container", writable_builder_rejected)
    monkeypatch.setattr(
        LocalAccessToken,
        "load_or_create",
        classmethod(lambda cls, paths: token_creator_rejected(paths)),
    )

    if command == "list":
        result = runner.invoke(app, ["proposal", "list", "--format", "json"])
    else:
        result = _invoke_mutation(command, token, dry_run=True)

    assert result.exit_code == 0
    assert container.closed is True


def test_dry_run_without_an_existing_token_fails_closed_without_creating_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, service, _token = _container(monkeypatch, tmp_path)
    container.paths.access_token.unlink()

    def token_creator_rejected(_paths):
        raise AssertionError("dry-run must not create a token")

    monkeypatch.setattr(
        LocalAccessToken,
        "load_or_create",
        classmethod(lambda cls, paths: token_creator_rejected(paths)),
    )

    result = _invoke_mutation("approve", "A" * 32, dry_run=True)

    assert result.exit_code == 2
    assert json.loads(result.stdout)["error"]["code"] == "permission_denied"
    assert not container.paths.access_token.exists()
    assert not service.calls


@pytest.mark.parametrize(
    ("input_value", "expected_code"),
    (
        ("not-json", "invalid_input"),
        (json.dumps({"token": "A" * 43}), "permission_denied"),
    ),
)
def test_unauthorized_normal_mutation_cannot_initialize_a_missing_runtime(
    tmp_path: Path,
    input_value: str,
    expected_code: str,
) -> None:
    runtime = tmp_path / "missing-runtime"

    result = runner.invoke(
        app,
        [
            "--config",
            str(runtime / "config.toml"),
            "proposal",
            "approve",
            str(PROPOSAL_ID),
            "--stdin-json",
            "--format",
            "json",
        ],
        input=input_value,
    )

    assert result.exit_code in {2, 4}
    assert json.loads(result.stdout)["error"]["code"] == expected_code
    assert not runtime.exists()


def test_cancelled_tty_mutation_cannot_initialize_a_missing_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "missing-runtime"

    def tty_stream(value, charset: str) -> _TTYBytesIO:
        if isinstance(value, str):
            value = value.encode(charset)
        return _TTYBytesIO(value or b"")

    monkeypatch.setattr(typer_testing, "make_input_stream", tty_stream)

    result = runner.invoke(
        app,
        [
            "--config",
            str(runtime / "config.toml"),
            "proposal",
            "approve",
            str(PROPOSAL_ID),
        ],
        input="n\n",
    )

    assert result.exit_code == 2
    assert not runtime.exists()


def test_dry_run_reports_invalid_preview_instead_of_fake_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeProposalService()
    service.raise_on = "preview_action"
    service.failure = ProposalError(f"invalid state {PRIVATE_PATH_MARKER}")
    _container_value, _service, token = _container(
        monkeypatch,
        tmp_path,
        service=service,
    )

    result = _invoke_mutation("approve", token, dry_run=True)

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    _assert_no_private_output(result, token)


def test_proposal_token_is_ignored_in_environment_and_rejected_in_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _container(monkeypatch, tmp_path)
    container = cli_module.build_container(None)
    token = LocalAccessToken.load_or_create(container.paths)
    monkeypatch.setenv("MEMORY_HUB_PROPOSAL_TOKEN", token)

    from_environment = _invoke_mutation("approve", None)
    from_argv = runner.invoke(
        app,
        [
            "proposal",
            "approve",
            str(PROPOSAL_ID),
            "--token",
            token,
            "--stdin-json",
            "--format",
            "json",
        ],
        input="{}",
    )

    assert from_environment.exit_code == 2
    assert json.loads(from_environment.stdout)["error"]["code"] == "permission_denied"
    assert from_argv.exit_code == 4
    assert json.loads(from_argv.stdout)["error"]["code"] == "invalid_input"
    assert token not in from_environment.stdout + from_environment.stderr
    assert token not in from_argv.stdout + from_argv.stderr
    assert token not in caplog.text
    assert not container.proposal_service.calls


def test_proposal_failure_never_discloses_stdin_or_private_operation_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    service = _FakeProposalService()
    service.raise_on = "apply"
    _container(monkeypatch, tmp_path, service=service)
    container = cli_module.build_container(None)
    token = LocalAccessToken.load_or_create(container.paths)

    result = _invoke_mutation("apply", token)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["code"] == "operation_failed"
    _assert_no_private_output(result, token)
    assert PATCH_MARKER not in caplog.text
    assert VERIFICATION_MARKER not in caplog.text
    assert PRIVATE_PATH_MARKER not in caplog.text
    assert token not in caplog.text


def test_proposal_validation_failure_has_stable_redacted_invalid_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeProposalService()
    service.raise_on = "create"
    service.failure = ProposalError(f"unsafe proposal {PRIVATE_PATH_MARKER}")
    _container_value, _service, token = _container(
        monkeypatch,
        tmp_path,
        service=service,
    )

    result = _invoke_mutation("create", token)

    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"] == {
        "code": "invalid_input",
        "message": "Invalid proposal input.",
    }
    _assert_no_private_output(result, token)


def test_container_close_failure_is_redacted_and_cannot_escape_run_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    container, _service, token = _container(monkeypatch, tmp_path)

    def close_failure() -> None:
        raise RuntimeError(f"close failed {PRIVATE_PATH_MARKER}")

    container.close = close_failure  # type: ignore[method-assign]

    result = runner.invoke(app, ["proposal", "list", "--format", "json"])

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == {
        "code": "operation_failed",
        "message": "Operation failed.",
    }
    _assert_no_private_output(result, token)


def test_close_failure_cannot_override_an_existing_redacted_operation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _FakeProposalService()
    service.raise_on = "apply"
    container, _service, token = _container(
        monkeypatch,
        tmp_path,
        service=service,
    )

    def close_failure() -> None:
        raise RuntimeError(f"secondary close {PRIVATE_PATH_MARKER}")

    container.close = close_failure  # type: ignore[method-assign]

    result = _invoke_mutation("apply", token)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"] == {
        "code": "operation_failed",
        "message": "Operation failed.",
    }
    _assert_no_private_output(result, token)


def test_proposal_create_rejects_stdin_over_global_bound_before_service_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _container(monkeypatch, tmp_path)
    container = cli_module.build_container(None)
    token = LocalAccessToken.load_or_create(container.paths)
    document = _create_document(token)
    document["patch"] = PATCH_MARKER + "x" * (1024 * 1024)

    result = runner.invoke(
        app,
        ["proposal", "create", "--stdin-json", "--format", "json"],
        input=json.dumps(document),
    )

    assert result.exit_code == 4
    error = json.loads(result.stdout)["error"]
    assert error == {"code": "invalid_input", "message": "Invalid JSON input."}
    assert not container.proposal_service.calls
    _assert_no_private_output(result, token)
