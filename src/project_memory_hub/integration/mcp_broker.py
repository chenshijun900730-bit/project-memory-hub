from __future__ import annotations

import json
import sys
from collections.abc import Callable
from json import JSONDecodeError
from pathlib import Path
from typing import BinaryIO, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from project_memory_hub import __version__
from project_memory_hub.container import (
    build_mcp_capture_container,
    build_mcp_reconcile_container,
)
from project_memory_hub.domain import (
    CapturePayload,
    Namespace,
    ReconcileReport,
    SourceAgent,
)
from project_memory_hub.security.json_limits import JsonNestingError, loads_json_bounded
from project_memory_hub.services.capture import CaptureService, PendingCaptureCapacityError
from project_memory_hub.services.reconcile import ReconcileService


PROTOCOL_VERSION: Final = "2025-06-18"
MAX_LINE_BYTES: Final = 1024 * 1024
_MAX_SAFE_COUNT: Final = 2**31 - 1


class _CaptureContainer(Protocol):
    capture: CaptureService

    def close(self) -> None: ...


class _ReconcileContainer(Protocol):
    reconcile: ReconcileService

    def close(self) -> None: ...


CaptureContainerFactory = Callable[[], _CaptureContainer]
ReconcileContainerFactory = Callable[[], _ReconcileContainer]
JsonObject = dict[str, object]
RequestId = str | int | None


class _StrictNamespace(Namespace, frozen=True):
    model_config = ConfigDict(extra="forbid")

    source_agent: Literal[SourceAgent.CODEX]


class _CapturePendingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cwd: Path
    namespace: _StrictNamespace
    source_record_id: str
    objective: str
    outcome: str
    decisions: list[str] = Field(default_factory=list)
    failed_attempts: list[str] = Field(default_factory=list)
    verified_commands: list[str] = Field(default_factory=list)
    changed_paths: list[str] = Field(default_factory=list)
    preferences: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    open_issues: list[str] = Field(default_factory=list)
    resolved_open_issues: list[str] = Field(default_factory=list)
    reusable_lessons: list[str] = Field(default_factory=list)

    def as_capture_payload(self) -> CapturePayload:
        return CapturePayload.model_validate(self.model_dump())


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _ToolCallParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    arguments: dict[str, object] = Field(default_factory=dict)
    meta: dict[str, object] | None = Field(default=None, alias="_meta")


class _InitializeParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol_version: str = Field(alias="protocolVersion")
    capabilities: dict[str, object]
    client_info: dict[str, object] = Field(alias="clientInfo")
    meta: dict[str, object] | None = Field(default=None, alias="_meta")


class _MetaOnlyParams(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    meta: dict[str, object] | None = Field(default=None, alias="_meta")


class _ListToolsParams(_MetaOnlyParams):
    cursor: str | None = None


_TOOLS: Final[tuple[JsonObject, ...]] = (
    {
        "name": "capture_pending_v1",
        "title": "Capture pending project memory",
        "description": (
            "Submit an unverified structured capture for later source verification. "
            "This tool cannot supply verification or runtime configuration."
        ),
        "inputSchema": _CapturePendingArguments.model_json_schema(),
    },
    {
        "name": "reconcile_if_due_v1",
        "title": "Reconcile project memory if due",
        "description": "Run the standard non-forced reconcile schedule.",
        "inputSchema": _NoArguments.model_json_schema(),
    },
)


def build_default_capture_container() -> _CaptureContainer:
    """Build only the existing-runtime pending capture service."""

    return build_mcp_capture_container(None)


def build_default_reconcile_container() -> _ReconcileContainer:
    """Build the existing-runtime non-forced reconcile service."""

    return build_mcp_reconcile_container(None)


class McpBroker:
    """A narrow newline-delimited JSON-RPC broker for two PMH write operations."""

    def __init__(
        self,
        capture_container_factory: CaptureContainerFactory,
        reconcile_container_factory: ReconcileContainerFactory,
    ) -> None:
        self._capture_container_factory = capture_container_factory
        self._reconcile_container_factory = reconcile_container_factory
        self._initialize_seen = False
        self._initialized = False

    def serve(self, source: BinaryIO, sink: BinaryIO) -> None:
        while True:
            line, oversized = _read_line(source)
            if line is None:
                return
            if oversized:
                self._write(
                    sink,
                    _protocol_error(
                        None,
                        -32600,
                        "Invalid Request",
                        "request_too_large",
                    ),
                )
                continue

            try:
                document = line.decode("utf-8", errors="strict")
                value = loads_json_bounded(document)
            except (UnicodeDecodeError, JSONDecodeError, JsonNestingError, RecursionError):
                self._write(
                    sink,
                    _protocol_error(None, -32700, "Parse error", "invalid_json"),
                )
                continue

            response = self._handle(value)
            if response is not None:
                self._write(sink, response)

    def _handle(self, value: object) -> JsonObject | None:
        if not isinstance(value, dict):
            return _protocol_error(None, -32600, "Invalid Request", "invalid_request")

        request = value
        if request.get("jsonrpc") != "2.0":
            return _protocol_error(None, -32600, "Invalid Request", "invalid_request")
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return _protocol_error(None, -32600, "Invalid Request", "invalid_request")

        has_id = "id" in request
        request_id = request.get("id")
        if has_id and not _valid_request_id(request_id):
            return _protocol_error(None, -32600, "Invalid Request", "invalid_request")

        allowed_keys = {"jsonrpc", "id", "method", "params"}
        if any(key not in allowed_keys for key in request):
            response_id = request_id if has_id else None
            return _protocol_error(
                response_id,
                -32600,
                "Invalid Request",
                "invalid_request",
            )

        if not has_id:
            self._handle_notification(method, request.get("params"))
            return None

        assert isinstance(request_id, (str, int)) or request_id is None
        return self._handle_request(request_id, method, request.get("params"))

    def _handle_notification(self, method: str, params: object) -> None:
        if method != "notifications/initialized":
            return
        if not self._initialize_seen or self._initialized:
            return
        try:
            _MetaOnlyParams.model_validate({} if params is None else params)
        except ValidationError:
            return
        self._initialized = True

    def _handle_request(
        self,
        request_id: RequestId,
        method: str,
        params: object,
    ) -> JsonObject:
        if method == "initialize":
            return self._initialize(request_id, params)
        if method not in {"ping", "tools/list", "tools/call"}:
            return _protocol_error(
                request_id,
                -32601,
                "Method not found",
                "method_not_found",
            )
        if not self._initialized:
            return _protocol_error(
                request_id,
                -32002,
                "Server not initialized",
                "not_initialized",
            )
        if method == "ping":
            try:
                _MetaOnlyParams.model_validate({} if params is None else params)
            except ValidationError:
                return _protocol_error(
                    request_id,
                    -32602,
                    "Invalid params",
                    "invalid_params",
                )
            return _result(request_id, {})
        if method == "tools/list":
            try:
                _ListToolsParams.model_validate({} if params is None else params)
            except ValidationError:
                return _protocol_error(
                    request_id,
                    -32602,
                    "Invalid params",
                    "invalid_params",
                )
            return _result(request_id, {"tools": list(_TOOLS)})
        try:
            call = _ToolCallParams.model_validate(params)
        except ValidationError:
            return _result(request_id, _tool_error("invalid_arguments"))
        if call.name not in {"capture_pending_v1", "reconcile_if_due_v1"}:
            return _protocol_error(
                request_id,
                -32602,
                "Invalid params",
                "unknown_tool",
            )
        return _result(request_id, self._call_tool(call))

    def _initialize(self, request_id: RequestId, params: object) -> JsonObject:
        if self._initialize_seen:
            return _protocol_error(
                request_id,
                -32600,
                "Invalid Request",
                "already_initialized",
            )
        try:
            _InitializeParams.model_validate(params)
        except ValidationError:
            return _protocol_error(
                request_id,
                -32602,
                "Invalid params",
                "invalid_params",
            )
        self._initialize_seen = True
        return _result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {
                    "name": "project-memory-hub",
                    "version": __version__,
                },
            },
        )

    def _call_tool(self, call: _ToolCallParams) -> JsonObject:
        if call.name == "capture_pending_v1":
            return self._capture_pending(call.arguments)
        return self._reconcile_if_due(call.arguments)

    def _capture_pending(self, arguments: dict[str, object]) -> JsonObject:
        try:
            parsed = _CapturePendingArguments.model_validate(arguments)
        except ValidationError:
            return _tool_error("invalid_arguments")

        try:
            container = self._capture_container_factory()
        except Exception:
            return _tool_error("runtime_unavailable")
        try:
            result = container.capture.capture(parsed.as_capture_payload())
            if result.status not in {"pending_verification", "duplicate"}:
                return _tool_error("capture_status_not_allowed")
            return _tool_success(
                {
                    "duplicate": result.duplicate is True,
                    "status": result.status,
                }
            )
        except PendingCaptureCapacityError:
            return _tool_error("capacity_exceeded")
        except Exception:
            return _tool_error("tool_execution_failed")
        finally:
            _safe_close(container)

    def _reconcile_if_due(self, arguments: dict[str, object]) -> JsonObject:
        try:
            _NoArguments.model_validate(arguments)
        except ValidationError:
            return _tool_error("invalid_arguments")

        try:
            container = self._reconcile_container_factory()
        except Exception:
            return _tool_error("runtime_unavailable")
        try:
            report = container.reconcile.run(force=False)
            return _tool_success(_safe_reconcile_report(report))
        except Exception:
            return _tool_error("tool_execution_failed")
        finally:
            _safe_close(container)

    @staticmethod
    def _write(sink: BinaryIO, response: JsonObject) -> None:
        encoded = json.dumps(
            response,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        sink.write(encoded + b"\n")
        sink.flush()


def serve(
    source: BinaryIO,
    sink: BinaryIO,
    *,
    container_factory: Callable[[], object] | None = None,
    capture_container_factory: CaptureContainerFactory | None = None,
    reconcile_container_factory: ReconcileContainerFactory | None = None,
) -> None:
    if container_factory is not None:
        if capture_container_factory is not None or reconcile_container_factory is not None:
            raise ValueError("container factories are mutually exclusive")
        capture_container_factory = container_factory  # type: ignore[assignment]
        reconcile_container_factory = container_factory  # type: ignore[assignment]
    selected_capture = capture_container_factory or build_default_capture_container
    selected_reconcile = reconcile_container_factory or build_default_reconcile_container
    McpBroker(selected_capture, selected_reconcile).serve(source, sink)


def main() -> int:
    try:
        serve(sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        return 0
    return 0


def _read_line(source: BinaryIO) -> tuple[bytes | None, bool]:
    chunk = source.readline(MAX_LINE_BYTES + 2)
    if chunk == b"":
        return None, False

    oversized = len(chunk) == MAX_LINE_BYTES + 2 and not chunk.endswith(b"\n")
    if oversized:
        _drain_line(source)
        return b"", True

    if chunk.endswith(b"\n"):
        chunk = chunk[:-1]
        if chunk.endswith(b"\r"):
            chunk = chunk[:-1]
    if len(chunk) > MAX_LINE_BYTES:
        return b"", True
    return chunk, False


def _drain_line(source: BinaryIO) -> None:
    while True:
        chunk = source.readline(MAX_LINE_BYTES + 2)
        if chunk == b"" or chunk.endswith(b"\n"):
            return


def _valid_request_id(value: object) -> bool:
    if value is None or isinstance(value, str):
        return True
    return isinstance(value, int) and not isinstance(value, bool)


def _result(request_id: RequestId, result: JsonObject) -> JsonObject:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _protocol_error(
    request_id: RequestId,
    code: int,
    message: str,
    stable_code: str,
) -> JsonObject:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message,
            "data": {"code": stable_code},
        },
    }


def _tool_success(structured: JsonObject) -> JsonObject:
    return _tool_result(structured, is_error=False)


def _tool_error(code: str) -> JsonObject:
    return _tool_result({"code": code}, is_error=True)


def _tool_result(structured: JsonObject, *, is_error: bool) -> JsonObject:
    text = json.dumps(
        structured,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return {
        "content": [{"type": "text", "text": text}],
        "isError": is_error,
        "structuredContent": structured,
    }


def _safe_reconcile_report(report: ReconcileReport) -> JsonObject:
    allowed_statuses = {"success", "degraded", "failed", "skipped", "already_running"}
    status = report.status if report.status in allowed_statuses else "failed"
    return {
        "duplicate_count": _safe_count(report.duplicate_count),
        "inserted_count": _safe_count(report.inserted_count),
        "status": status,
        "warning_count": _safe_count(report.warning_count),
    }


def _safe_count(value: int) -> int:
    if type(value) is not int or value < 0:
        return 0
    return min(value, _MAX_SAFE_COUNT)


def _safe_close(container: _CaptureContainer | _ReconcileContainer) -> None:
    try:
        container.close()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
