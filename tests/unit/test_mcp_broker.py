from __future__ import annotations

import io
import json
from typing import Any
from uuid import UUID

import pytest

import project_memory_hub.services.capture as capture_module
from project_memory_hub.domain import CaptureResult, ReconcileReport
from project_memory_hub.integration import mcp_broker


def _request(
    request_id: int,
    method: str,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params is not None:
        value["params"] = params
    return value


def _initialized_messages(*requests: dict[str, object]) -> list[dict[str, object]]:
    return [
        _request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1"},
            },
        ),
        {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
            "params": {},
        },
        *requests,
    ]


def _run(
    messages: list[dict[str, object]],
    *,
    container_factory: Any,
) -> list[dict[str, object]]:
    input_bytes = b"".join(
        json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n" for message in messages
    )
    source = io.BytesIO(input_bytes)
    sink = io.BytesIO()

    mcp_broker.serve(source, sink, container_factory=container_factory)

    return [json.loads(line) for line in sink.getvalue().splitlines()]


class RecordingCapture:
    def __init__(self, result: CaptureResult) -> None:
        self.result = result
        self.payloads: list[object] = []

    def capture(self, payload: object) -> CaptureResult:
        self.payloads.append(payload)
        return self.result


class RecordingReconcile:
    def __init__(self, report: ReconcileReport) -> None:
        self.report = report
        self.force_values: list[bool] = []

    def run(self, force: bool = False) -> ReconcileReport:
        self.force_values.append(force)
        return self.report


class RecordingContainer:
    def __init__(
        self,
        *,
        capture_result: CaptureResult | None = None,
        reconcile_report: ReconcileReport | None = None,
    ) -> None:
        self.capture = RecordingCapture(
            capture_result or CaptureResult(status="pending_verification")
        )
        self.reconcile = RecordingReconcile(
            reconcile_report
            or ReconcileReport(
                run_id=UUID("00000000-0000-0000-0000-000000000001"),
                status="skipped",
            )
        )
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


def _capture_arguments() -> dict[str, object]:
    return {
        "cwd": "/private/project",
        "namespace": {"source_agent": "codex", "model_id": "gpt-test"},
        "source_record_id": "thread-1",
        "objective": "remember this objective",
        "outcome": "remember this outcome",
        "decisions": ["keep the narrow broker"],
        "failed_attempts": [],
        "verified_commands": [],
        "changed_paths": [],
        "preferences": [],
        "risks": [],
        "open_issues": [],
        "resolved_open_issues": [],
        "reusable_lessons": [],
    }


def test_initialize_ping_and_tools_list_follow_mcp_2025_06_18() -> None:
    responses = _run(
        _initialized_messages(
            _request(2, "ping", {}),
            _request(3, "tools/list", {}),
        ),
        container_factory=lambda: pytest.fail("listing tools must not open runtime"),
    )

    assert responses[0] == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "project-memory-hub", "version": "0.2.1"},
        },
    }
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    tools = responses[2]["result"]["tools"]
    assert [tool["name"] for tool in tools] == [
        "capture_pending_v1",
        "reconcile_if_due_v1",
    ]
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


@pytest.mark.parametrize("forbidden", ["verification", "config", "force", "database_path"])
def test_capture_arguments_are_extra_forbid_and_do_not_open_runtime(forbidden: str) -> None:
    arguments = _capture_arguments()
    arguments[forbidden] = "must-not-be-accepted"

    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "capture_pending_v1", "arguments": arguments},
            )
        ),
        container_factory=lambda: pytest.fail("invalid arguments must not open runtime"),
    )

    assert responses[-1]["result"] == {
        "content": [{"type": "text", "text": '{"code":"invalid_arguments"}'}],
        "isError": True,
        "structuredContent": {"code": "invalid_arguments"},
    }


def test_capture_rejects_non_codex_source_before_opening_runtime() -> None:
    arguments = _capture_arguments()
    arguments["namespace"] = {"source_agent": "chatgpt", "model_id": "gpt-test"}

    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "capture_pending_v1", "arguments": arguments},
            )
        ),
        container_factory=lambda: pytest.fail("foreign source must not open runtime"),
    )

    assert responses[-1]["result"]["isError"] is True
    assert responses[-1]["result"]["structuredContent"] == {"code": "invalid_arguments"}


@pytest.mark.parametrize(
    ("capture_result", "expected_status", "expected_duplicate"),
    [
        (CaptureResult(status="pending_verification"), "pending_verification", False),
        (CaptureResult(status="duplicate", duplicate=True), "duplicate", True),
        (
            CaptureResult(status="pending_verification", duplicate=True),
            "pending_verification",
            True,
        ),
    ],
)
def test_capture_calls_unverified_service_and_closes_each_container(
    capture_result: CaptureResult,
    expected_status: str,
    expected_duplicate: bool,
) -> None:
    container = RecordingContainer(capture_result=capture_result)

    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "capture_pending_v1", "arguments": _capture_arguments()},
            )
        ),
        container_factory=lambda: container,
    )

    assert len(container.capture.payloads) == 1
    payload = container.capture.payloads[0]
    assert payload.objective == "remember this objective"
    assert payload.cwd.as_posix() == "/private/project"
    assert container.close_count == 1
    assert responses[-1]["result"]["isError"] is False
    assert responses[-1]["result"]["structuredContent"] == {
        "duplicate": expected_duplicate,
        "status": expected_status,
    }


def test_capture_rejects_any_service_status_that_is_not_pending_or_duplicate() -> None:
    container = RecordingContainer(capture_result=CaptureResult(status="inserted"))

    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "capture_pending_v1", "arguments": _capture_arguments()},
            )
        ),
        container_factory=lambda: container,
    )

    assert container.close_count == 1
    assert responses[-1]["result"]["isError"] is True
    assert responses[-1]["result"]["structuredContent"] == {"code": "capture_status_not_allowed"}


def test_reconcile_requires_empty_arguments_runs_if_due_and_closes() -> None:
    container = RecordingContainer(
        reconcile_report=ReconcileReport(
            run_id=UUID("00000000-0000-0000-0000-000000000099"),
            status="degraded",
            inserted_count=5,
            duplicate_count=7,
            warning_count=11,
            stages={"private/path": "must-not-be-returned"},
        )
    )

    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "reconcile_if_due_v1", "arguments": {}},
            )
        ),
        container_factory=lambda: container,
    )

    assert container.reconcile.force_values == [False]
    assert container.close_count == 1
    assert responses[-1]["result"]["structuredContent"] == {
        "duplicate_count": 7,
        "inserted_count": 5,
        "status": "degraded",
        "warning_count": 11,
    }
    serialized = json.dumps(responses[-1])
    assert "private/path" not in serialized
    assert "00000000-0000-0000-0000-000000000099" not in serialized


def test_reconcile_rejects_force_without_opening_runtime() -> None:
    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "reconcile_if_due_v1", "arguments": {"force": True}},
            )
        ),
        container_factory=lambda: pytest.fail("force must be rejected before runtime opens"),
    )

    assert responses[-1]["result"]["isError"] is True
    assert responses[-1]["result"]["structuredContent"] == {"code": "invalid_arguments"}


def test_tool_exception_is_sanitized_and_container_is_still_closed() -> None:
    secret = "TOP-SECRET-task-token-/Users/private/project"
    container = RecordingContainer()

    def explode(_payload: object) -> CaptureResult:
        raise RuntimeError(secret)

    container.capture.capture = explode  # type: ignore[method-assign]

    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "capture_pending_v1", "arguments": _capture_arguments()},
            )
        ),
        container_factory=lambda: container,
    )

    serialized = json.dumps(responses)
    assert secret not in serialized
    assert "remember this objective" not in serialized
    assert "/private/project" not in serialized
    assert container.close_count == 1
    assert responses[-1]["result"]["structuredContent"] == {"code": "tool_execution_failed"}


def test_pending_capacity_error_is_stable_and_sanitized() -> None:
    container = RecordingContainer()

    def capacity(_payload: object) -> CaptureResult:
        raise capture_module.PendingCaptureCapacityError("private capacity detail")

    container.capture.capture = capacity  # type: ignore[method-assign]
    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "capture_pending_v1", "arguments": _capture_arguments()},
            )
        ),
        container_factory=lambda: container,
    )

    assert responses[-1]["result"]["isError"] is True
    assert responses[-1]["result"]["structuredContent"] == {"code": "capacity_exceeded"}
    assert "private capacity detail" not in json.dumps(responses)


def test_standard_meta_and_cursor_params_are_accepted() -> None:
    messages = _initialized_messages(
        _request(2, "ping", {"_meta": {"progressToken": "safe"}}),
        _request(3, "tools/list", {"cursor": "opaque", "_meta": {}}),
    )
    messages[1]["params"] = {"_meta": {"progressToken": 1}}

    responses = _run(
        messages,
        container_factory=lambda: pytest.fail("protocol calls must not open runtime"),
    )

    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert [tool["name"] for tool in responses[2]["result"]["tools"]] == [
        "capture_pending_v1",
        "reconcile_if_due_v1",
    ]


def test_unknown_tool_is_an_mcp_protocol_error() -> None:
    responses = _run(
        _initialized_messages(
            _request(
                2,
                "tools/call",
                {"name": "unknown_tool", "arguments": {}},
            )
        ),
        container_factory=lambda: pytest.fail("unknown tool must not open runtime"),
    )

    assert responses[-1] == {
        "jsonrpc": "2.0",
        "id": 2,
        "error": {
            "code": -32602,
            "message": "Invalid params",
            "data": {"code": "unknown_tool"},
        },
    }


def test_protocol_errors_are_stable_and_tools_require_initialization() -> None:
    source = io.BytesIO(
        b"{not-json}\n"
        + json.dumps(_request(7, "tools/list", {})).encode("utf-8")
        + b"\n"
        + json.dumps(_request(8, "unknown/method", {})).encode("utf-8")
        + b"\n"
    )
    sink = io.BytesIO()

    mcp_broker.serve(
        source,
        sink,
        container_factory=lambda: pytest.fail("protocol errors must not open runtime"),
    )

    responses = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert responses == [
        {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32700,
                "message": "Parse error",
                "data": {"code": "invalid_json"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 7,
            "error": {
                "code": -32002,
                "message": "Server not initialized",
                "data": {"code": "not_initialized"},
            },
        },
        {
            "jsonrpc": "2.0",
            "id": 8,
            "error": {
                "code": -32601,
                "message": "Method not found",
                "data": {"code": "method_not_found"},
            },
        },
    ]


def test_oversized_line_is_rejected_without_echo_and_next_request_is_processed() -> None:
    secret = b"private-task-token" * 70_000
    source = io.BytesIO(
        secret
        + b"\n"
        + json.dumps(
            _request(
                9,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            )
        ).encode("utf-8")
        + b"\n"
    )
    sink = io.BytesIO()

    mcp_broker.serve(
        source,
        sink,
        container_factory=lambda: pytest.fail("oversized input must not open runtime"),
    )

    output = sink.getvalue()
    responses = [json.loads(line) for line in output.splitlines()]
    assert b"private-task-token" not in output
    assert responses[0]["error"] == {
        "code": -32600,
        "message": "Invalid Request",
        "data": {"code": "request_too_large"},
    }
    assert responses[1]["id"] == 9
    assert responses[1]["result"]["protocolVersion"] == "2025-06-18"


def test_default_builders_use_separate_existing_runtime_containers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capture_sentinel = object()
    reconcile_sentinel = object()
    seen: list[tuple[str, object]] = []

    def build_capture(config_path: object) -> object:
        seen.append(("capture", config_path))
        return capture_sentinel

    def build_reconcile(config_path: object) -> object:
        seen.append(("reconcile", config_path))
        return reconcile_sentinel

    monkeypatch.setattr(mcp_broker, "build_mcp_capture_container", build_capture)
    monkeypatch.setattr(mcp_broker, "build_mcp_reconcile_container", build_reconcile)

    assert mcp_broker.build_default_capture_container() is capture_sentinel
    assert mcp_broker.build_default_reconcile_container() is reconcile_sentinel
    assert seen == [("capture", None), ("reconcile", None)]
