from __future__ import annotations

import asyncio
import fcntl
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import Request
from starlette import formparsers
from starlette.responses import StreamingResponse

from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.container import build_container
from project_memory_hub.domain import SourceAgent
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.security.web import (
    LocalAccessToken,
    WebRequestLimits,
    loopback_bind_host,
)
from project_memory_hub.services.control import ControlPanelService
from project_memory_hub.web.app import create_app
from project_memory_hub.web.errors import error_response


def _container(tmp_path: Path):
    root = tmp_path / "runtime"
    root.mkdir(mode=0o700)
    config_path = root / "config.toml"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(tmp_path / "projects",),
            enabled_sources=(SourceAgent.CODEX, SourceAgent.CHATGPT),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    probe_home = tmp_path / "probe-home"
    probe_home.mkdir()
    return build_container(config_path, probe_home=probe_home)


class BoundaryProbeSpy:
    def __init__(self) -> None:
        self.reserve_calls = 0
        self.light_calls = 0

    def reserve_structure(self, _source: SourceAgent) -> None:
        self.reserve_calls += 1
        raise AssertionError("rejected request reached structure reservation")

    def probe_all_light(self) -> None:
        self.light_calls += 1
        raise AssertionError("rejected request reached a light probe")


async def _request(app, method: str, url: str, **kwargs):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        return await client.request(method, url, **kwargs)


async def _bootstrap(app) -> tuple[str, str]:
    token = app.state.container.paths.access_token.read_text(encoding="ascii")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1",
    ) as client:
        response = await client.get(f"/?token={token}", follow_redirects=False)
    return (
        response.headers["set-cookie"].split(";", 1)[0],
        response.headers["x-project-memory-hub-csrf"],
    )


async def _raw_asgi_request(
    app,
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    query_string: bytes = b"",
    messages: list[dict[str, Any]] | None = None,
    sent_out: list[dict[str, Any]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": query_string,
        "headers": headers,
        "client": ("127.0.0.1", 50000),
        "server": ("127.0.0.1", 80),
    }
    pending = iter(messages or [{"type": "http.request", "body": b"", "more_body": False}])
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        try:
            return next(pending)
        except StopIteration:
            return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    try:
        await app(scope, receive, send)
    finally:
        if sent_out is not None:
            sent_out.extend(sent)
    start = next(message for message in sent if message["type"] == "http.response.start")
    response_headers = {
        name.decode("latin-1").casefold(): value.decode("latin-1")
        for name, value in start["headers"]
    }
    body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    return start["status"], response_headers, body


def _assert_security_headers(headers: httpx.Headers | dict[str, str]) -> None:
    assert headers["cache-control"] == "no-store"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["x-frame-options"] == "DENY"
    assert headers["referrer-policy"] == "same-origin"
    assert "default-src 'self'" in headers["content-security-policy"]


def _expected_error_body(status_code: int) -> bytes:
    return bytes(error_response(status_code).body)


@pytest.mark.parametrize("script", ("i18n.js", "projects.js"))
def test_frontend_scripts_run_under_strict_self_only_csp(
    tmp_path: Path,
    script: str,
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            app = create_app(container)
            unauthenticated = await _request(app, "GET", f"/static/{script}")
            cookie, _csrf = await _bootstrap(app)
            authenticated = await _request(
                app,
                "GET",
                f"/static/{script}",
                headers={"cookie": cookie},
            )
            return unauthenticated, authenticated

    unauthenticated, authenticated = asyncio.run(scenario())

    assert unauthenticated.status_code == 401
    assert authenticated.status_code == 200
    assert authenticated.headers["content-type"].startswith(
        ("application/javascript", "text/javascript")
    )
    assert authenticated.headers["x-content-type-options"] == "nosniff"
    csp = authenticated.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp


def test_local_access_token_creates_one_private_regular_file(tmp_path: Path) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()

    token = LocalAccessToken.load_or_create(paths)

    metadata = paths.access_token.stat()
    assert len(token) == 43
    assert metadata.st_uid == os.getuid()
    assert metadata.st_nlink == 1
    assert stat.S_ISREG(metadata.st_mode)
    assert stat.S_IMODE(metadata.st_mode) == 0o600
    assert LocalAccessToken.load_or_create(paths) == token
    assert LocalAccessToken.matches(token, token)
    assert not LocalAccessToken.matches(token, token + "x")


def test_loading_existing_token_never_initializes_a_missing_runtime(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "missing-runtime")

    with pytest.raises(FileNotFoundError):
        LocalAccessToken.load_existing(paths)

    assert not paths.root.exists()


def test_local_access_token_first_creation_is_safe_under_64_threads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()
    callers_ready = threading.Barrier(64)
    writer_paused = threading.Event()
    release_writer = threading.Event()
    original_write = os.write

    def paused_write(descriptor: int, data: bytes) -> int:
        if not writer_paused.is_set():
            writer_paused.set()
            assert release_writer.wait(timeout=5)
        return original_write(descriptor, data)

    def load() -> str:
        callers_ready.wait(timeout=5)
        return LocalAccessToken.load_or_create(paths)

    monkeypatch.setattr(os, "write", paused_write)
    with ThreadPoolExecutor(max_workers=64) as executor:
        futures = [executor.submit(load) for _ in range(64)]
        assert writer_paused.wait(timeout=5)
        time.sleep(0.05)
        release_writer.set()
        tokens = [future.result(timeout=5) for future in futures]

    assert len(set(tokens)) == 1
    assert LocalAccessToken.load_or_create(paths) == tokens[0]


def test_local_access_token_never_accepts_an_incomplete_private_file(
    tmp_path: Path,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()
    paths.access_token.write_bytes(b"")
    paths.access_token.chmod(0o600)

    started = time.monotonic()
    with pytest.raises(PermissionError):
        LocalAccessToken.load_or_create(paths)
    assert time.monotonic() - started < 1


@pytest.mark.parametrize("unsafe_kind", ["symlink", "hardlink", "directory", "mode", "malformed"])
def test_local_access_token_rejects_unsafe_existing_file(tmp_path: Path, unsafe_kind: str) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()
    if unsafe_kind == "symlink":
        target = tmp_path / "target"
        target.write_text("A" * 43, encoding="ascii")
        paths.access_token.symlink_to(target)
    elif unsafe_kind == "hardlink":
        target = tmp_path / "target"
        target.write_text("A" * 43, encoding="ascii")
        target.chmod(0o600)
        os.link(target, paths.access_token)
    elif unsafe_kind == "directory":
        paths.access_token.mkdir(mode=0o700)
    elif unsafe_kind == "mode":
        paths.access_token.write_text("A" * 43, encoding="ascii")
        paths.access_token.chmod(0o644)
    else:
        paths.access_token.write_text("not a token\n" + "A" * 512, encoding="ascii")
        paths.access_token.chmod(0o600)

    with pytest.raises(PermissionError):
        LocalAccessToken.load_or_create(paths)


@pytest.mark.parametrize(
    "limits",
    [
        {"max_total_bytes": 0},
        {"max_chunk_bytes": -1},
        {"max_chunks": True},
        {"max_files": 0},
        {"max_fields": 1.5},
    ],
)
def test_web_request_limits_require_positive_plain_integers(
    limits: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="positive integers"):
        WebRequestLimits(**limits)  # type: ignore[arg-type]


def test_local_access_token_public_api_rejects_wrong_types(tmp_path: Path) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")

    with pytest.raises(TypeError, match="paths must be RuntimePaths"):
        LocalAccessToken.load_or_create(tmp_path)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="paths must be RuntimePaths"):
        LocalAccessToken.load_existing(tmp_path)  # type: ignore[arg-type]

    assert not LocalAccessToken.matches("A" * 43, b"A" * 43)  # type: ignore[arg-type]
    assert not LocalAccessToken.matches(None, "A" * 43)  # type: ignore[arg-type]
    assert not paths.root.exists()


def test_local_access_token_removes_partial_file_after_short_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    monkeypatch.setattr(os, "write", lambda _descriptor, _data: 0)

    with pytest.raises(OSError, match="short token write"):
        LocalAccessToken.load_or_create(paths)

    assert not paths.access_token.exists()


def test_local_access_token_creation_permission_error_is_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    original_open = os.open

    def denied_open(path, flags: int, mode: int = 0o777):
        if Path(path) == paths.access_token and flags & os.O_CREAT:
            raise OSError("NEVER_EXPOSE_TOKEN_PATH")
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", denied_open)

    with pytest.raises(PermissionError, match="local access token file rejected") as error:
        LocalAccessToken.load_or_create(paths)

    assert "NEVER_EXPOSE_TOKEN_PATH" not in str(error.value)
    assert not paths.access_token.exists()


def test_local_access_token_uses_completed_external_creation_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    external_token = "R" * 43
    original_open = os.open

    def racing_open(path, flags: int, mode: int = 0o777):
        if Path(path) == paths.access_token and flags & os.O_CREAT:
            paths.access_token.write_text(external_token, encoding="ascii")
            paths.access_token.chmod(0o600)
            raise FileExistsError(path)
        return original_open(path, flags, mode)

    monkeypatch.setattr(os, "open", racing_open)

    assert LocalAccessToken.load_or_create(paths) == external_token


def test_local_access_token_creation_race_fails_closed_when_file_never_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    open_attempts = 0
    original_open = os.open

    def missing_race_open(path, flags: int, _mode: int = 0o777):
        nonlocal open_attempts
        if Path(path) != paths.access_token:
            return original_open(path, flags, _mode)
        open_attempts += 1
        if flags & os.O_CREAT:
            raise FileExistsError(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(os, "open", missing_race_open)
    with pytest.raises(PermissionError, match="token file rejected"):
        LocalAccessToken.load_or_create(paths)

    assert open_attempts == LocalAccessToken._race_attempts + 2


@pytest.mark.parametrize(
    "data",
    [
        b"!" * 43,
        b"\xff" + (b"A" * 42),
    ],
)
def test_local_access_token_rejects_invalid_fixed_length_bytes(
    tmp_path: Path,
    data: bytes,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()
    paths.access_token.write_bytes(data)
    paths.access_token.chmod(0o600)

    with pytest.raises(PermissionError, match="token file rejected"):
        LocalAccessToken.load_existing(paths)


def test_local_access_token_rejects_short_os_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = RuntimePaths.for_root(tmp_path / "runtime")
    paths.ensure()
    paths.access_token.write_text("A" * 43, encoding="ascii")
    paths.access_token.chmod(0o600)
    monkeypatch.setattr(os, "read", lambda _descriptor, _size: b"A" * 42)

    with pytest.raises(PermissionError, match="token file rejected"):
        LocalAccessToken.load_existing(paths)


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("localhost", "localhost"),
        (" LOCALHOST ", "localhost"),
        ("127.0.0.1", "127.0.0.1"),
        ("127.255.255.254", "127.255.255.254"),
        ("::1", "::1"),
    ],
)
def test_loopback_bind_host_normalizes_only_loopback_addresses(
    host: str,
    expected: str,
) -> None:
    assert loopback_bind_host(host) == expected


@pytest.mark.parametrize(
    "host",
    [None, 7, "", "   ", "0.0.0.0", "192.0.2.1", "attacker.example", "127.0.0.1:8765"],
)
def test_loopback_bind_host_rejects_non_loopback_or_authority_values(host: object) -> None:
    with pytest.raises(ValueError, match="serve host must be loopback"):
        loopback_bind_host(host)  # type: ignore[arg-type]


def test_host_is_checked_before_auth_and_proxy_headers_are_rejected(
    tmp_path: Path,
) -> None:
    with _container(tmp_path) as container:
        app = create_app(container)
        foreign = asyncio.run(_request(app, "GET", "/", headers={"host": "attacker.example"}))
        wildcard = asyncio.run(_request(app, "GET", "/", headers={"host": "0.0.0.0:8765"}))
        forwarded = asyncio.run(
            _request(
                app,
                "GET",
                "/",
                headers={
                    "host": "127.0.0.1",
                    "x-forwarded-host": "attacker.example",
                },
            )
        )
        unauthenticated = asyncio.run(_request(app, "GET", "/", headers={"host": "[::1]:8765"}))

    assert foreign.status_code == wildcard.status_code == forwarded.status_code == 400
    assert unauthenticated.status_code == 401


@pytest.mark.parametrize(
    "host",
    [
        "[localhost]",
        "[127.0.0.1]",
        "[::1%lo0]",
        "[::1%25lo0]",
        "localhost:08765",
        "127.0.0.1:00080",
    ],
)
def test_ambiguous_loopback_host_forms_are_rejected(tmp_path: Path, host: str) -> None:
    with _container(tmp_path) as container:
        response = asyncio.run(_request(create_app(container), "GET", "/", headers={"host": host}))

    assert response.status_code == 400
    _assert_security_headers(response.headers)


@pytest.mark.parametrize(
    ("host", "expected_status"),
    [
        ("localhost", 401),
        ("localhost:65535", 401),
        ("127.255.255.254", 401),
        ("[0:0:0:0:0:0:0:1]:8765", 401),
        (" localhost", 400),
        ("localhost/path", 400),
        ("[::1]suffix", 400),
        ("::1", 400),
        ("127.0.0.1:0", 400),
        ("127.0.0.1:65536", 400),
        ("127.0.0.1:１２", 400),
    ],
)
def test_host_header_accepts_only_unambiguous_loopback_authorities(
    tmp_path: Path,
    host: str,
    expected_status: int,
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            return await _raw_asgi_request(
                create_app(container),
                method="GET",
                path="/",
                headers=[(b"host", host.encode("utf-8"))],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == expected_status
    assert body == _expected_error_body(expected_status)
    _assert_security_headers(headers)


def test_bootstrap_sets_strict_cookie_and_redirects_without_token(
    tmp_path: Path,
) -> None:
    with _container(tmp_path) as container:
        token = LocalAccessToken.load_or_create(container.paths)
        app = create_app(container)
        marker = "NEVER_ECHO_BOOTSTRAP_TOKEN"
        invalid = asyncio.run(_request(app, "GET", f"/?token={marker}"))
        response = asyncio.run(_request(app, "GET", f"/?token={token}"))

    assert invalid.status_code == 401
    assert marker not in invalid.text
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert token not in response.text
    assert token not in response.headers["location"]
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    assert "path=/" in cookie


def test_unsafe_requests_require_same_origin_and_session_csrf(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, int, int]:
        with _container(tmp_path) as container:
            app = create_app(container)
            token = LocalAccessToken.load_or_create(container.paths)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                boot = await client.get(f"/?token={token}", follow_redirects=False)
                csrf = boot.headers["x-project-memory-hub-csrf"]
                no_origin = await client.post("/settings", data={"csrf_token": csrf})
                foreign = await client.post(
                    "/settings",
                    headers={"origin": "https://attacker.example"},
                    data={"csrf_token": csrf},
                )
                no_csrf = await client.post(
                    "/settings",
                    headers={"origin": "http://127.0.0.1"},
                    data={},
                )
                wrong_csrf = await client.post(
                    "/settings",
                    headers={"origin": "http://127.0.0.1"},
                    data={"csrf_token": "wrong"},
                )
                return (
                    no_origin.status_code,
                    foreign.status_code,
                    no_csrf.status_code,
                    wrong_csrf.status_code,
                )

    assert asyncio.run(scenario()) == (403, 403, 403, 403)


@pytest.mark.parametrize(
    ("boundary", "expected_status"),
    (
        ("host", 400),
        ("session", 401),
        ("missing_origin", 403),
        ("foreign_origin", 403),
        ("csrf", 403),
    ),
)
def test_trae_probe_requires_bootstrap_loopback_host_origin_and_csrf(
    tmp_path: Path,
    boundary: str,
    expected_status: int,
) -> None:
    async def scenario() -> tuple[httpx.Response, BoundaryProbeSpy]:
        with _container(tmp_path) as container:
            probes = BoundaryProbeSpy()
            container.source_probes = probes  # type: ignore[assignment]
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            headers = {
                "cookie": cookie,
                "host": "127.0.0.1",
                "origin": "http://127.0.0.1",
                "x-csrf-token": csrf,
                "content-type": "application/x-www-form-urlencoded",
            }
            body = f"csrf_token={csrf}"

            if boundary == "host":
                headers["host"] = "attacker.example"
            elif boundary == "session":
                headers.pop("cookie")
            elif boundary == "missing_origin":
                headers.pop("origin")
            elif boundary == "foreign_origin":
                headers["origin"] = "https://attacker.example"
            elif boundary == "csrf":
                headers.pop("x-csrf-token")
                body = "csrf_token=wrong"
            else:  # pragma: no cover - parametrization is closed above
                raise AssertionError("unsupported boundary case")

            response = await _request(
                app,
                "POST",
                "/sources/trae/probe",
                headers=headers,
                content=body,
            )
            return response, probes

    response, probes = asyncio.run(scenario())

    assert probes.reserve_calls == 0
    assert probes.light_calls == 0
    assert response.status_code == expected_status
    assert response.content == _expected_error_body(expected_status)
    assert "set-cookie" not in response.headers
    _assert_security_headers(response.headers)


@pytest.mark.parametrize(
    ("body_case", "expected_status"),
    (
        ("oversized", 413),
        ("too_many_fields", 400),
        ("duplicate_csrf", 400),
        ("extra_field", 400),
    ),
)
def test_trae_probe_applies_body_and_field_limits_before_reservation(
    tmp_path: Path,
    body_case: str,
    expected_status: int,
) -> None:
    async def scenario() -> tuple[httpx.Response, BoundaryProbeSpy]:
        with _container(tmp_path) as container:
            probes = BoundaryProbeSpy()
            container.source_probes = probes  # type: ignore[assignment]
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=128,
                    max_chunk_bytes=128,
                    max_chunks=8,
                    max_files=1,
                    max_fields=2,
                ),
            )
            cookie, csrf = await _bootstrap(app)
            headers = {
                "cookie": cookie,
                "origin": "http://127.0.0.1",
                "x-csrf-token": csrf,
                "content-type": "application/x-www-form-urlencoded",
            }
            bodies = {
                "oversized": f"csrf_token={csrf}&padding={'x' * 256}",
                "too_many_fields": f"csrf_token={csrf}&one=1&two=2",
                "duplicate_csrf": f"csrf_token={csrf}&csrf_token={csrf}",
                "extra_field": f"csrf_token={csrf}&extra=1",
            }
            response = await _request(
                app,
                "POST",
                "/sources/trae/probe",
                headers=headers,
                content=bodies[body_case],
            )
            return response, probes

    response, probes = asyncio.run(scenario())

    assert response.status_code == expected_status
    assert response.content == _expected_error_body(expected_status)
    assert "set-cookie" not in response.headers
    _assert_security_headers(response.headers)
    assert probes.reserve_calls == 0
    assert probes.light_calls == 0


def test_trae_probe_rejects_multipart_without_creating_temporary_cache(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, BoundaryProbeSpy, bool]:
        with _container(tmp_path) as container:
            probes = BoundaryProbeSpy()
            container.source_probes = probes  # type: ignore[assignment]
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            temporary_cache = container.paths.imports / "web-uploads"
            assert not temporary_cache.exists()
            response = await _request(
                app,
                "POST",
                "/sources/trae/probe",
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                },
                files={"csrf_token": (None, csrf)},
            )
            return response, probes, temporary_cache.exists()

    response, probes, cache_created = asyncio.run(scenario())

    assert response.status_code == 400
    assert response.content == _expected_error_body(400)
    assert "set-cookie" not in response.headers
    _assert_security_headers(response.headers)
    assert not cache_created
    assert probes.reserve_calls == 0
    assert probes.light_calls == 0


def test_malformed_boundary_inputs_fail_closed_with_security_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            bad_host = await _request(app, "GET", "/", headers={"host": "[::1"})
            bad_origin = await _request(
                app,
                "POST",
                "/sources/codex/enable",
                headers={
                    "cookie": cookie,
                    "host": "127.0.0.1",
                    "origin": "http://[::1",
                    "x-csrf-token": csrf,
                },
            )
            bad_length = await _request(
                app,
                "GET",
                "/",
                headers={
                    "host": "127.0.0.1",
                    "content-length": "9" * 5000,
                },
            )
            return bad_host, bad_origin, bad_length

    bad_host, bad_origin, bad_length = asyncio.run(scenario())
    assert bad_host.status_code == 400
    assert bad_origin.status_code == 403
    assert bad_length.status_code == 400
    for response in (bad_host, bad_origin, bad_length):
        assert response.content == _expected_error_body(response.status_code)
        _assert_security_headers(response.headers)


def test_origin_is_compared_by_canonical_http_authority(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int, list[int]]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            common = {
                "cookie": cookie,
                "x-csrf-token": csrf,
            }
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                canonical_case = await client.post(
                    "/sources/codex/enable",
                    headers={
                        **common,
                        "host": "LOCALHOST",
                        "origin": "HTTP://localhost",
                    },
                )
                canonical_port = await client.post(
                    "/sources/codex/enable",
                    headers={
                        **common,
                        "host": "127.0.0.1",
                        "origin": "http://127.0.0.1:80",
                    },
                )
                denied = []
                for origin in (
                    "http://127.0.0.1/",
                    "http://user@127.0.0.1",
                    "http://127.0.0.1?query=1",
                    "http://127.0.0.1#fragment",
                    "http://127.0.0.1:080",
                ):
                    response = await client.post(
                        "/sources/codex/enable",
                        headers={
                            **common,
                            "host": "127.0.0.1",
                            "origin": origin,
                        },
                    )
                    denied.append(response.status_code)
            return canonical_case.status_code, canonical_port.status_code, denied

    canonical_case, canonical_port, denied = asyncio.run(scenario())
    assert canonical_case == canonical_port == 303
    assert denied == [403] * 5


def test_oversized_origin_port_fails_closed_with_security_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            return await _request(
                app,
                "POST",
                "/sources/codex/enable",
                headers={
                    "cookie": cookie,
                    "host": "127.0.0.1",
                    "origin": "http://127.0.0.1:" + ("9" * 5000),
                    "x-csrf-token": csrf,
                },
            )

    response = asyncio.run(scenario())
    assert response.status_code == 403
    assert response.content == _expected_error_body(403)
    _assert_security_headers(response.headers)


def test_duplicate_session_cookie_and_csrf_header_are_rejected(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, int]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            duplicate_cookie = await _request(
                app,
                "GET",
                "/",
                headers={"cookie": f"{cookie}; {cookie}"},
            )
            status, _headers, _body = await _raw_asgi_request(
                app,
                method="POST",
                path="/sources/codex/enable",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"http://127.0.0.1"),
                    (b"x-csrf-token", csrf.encode("ascii")),
                    (b"x-csrf-token", csrf.encode("ascii")),
                    (b"content-length", b"0"),
                ],
            )
            return duplicate_cookie.status_code, status

    assert asyncio.run(scenario()) == (401, 403)


def test_browser_sessions_are_lru_bounded_and_invalid_cookie_fails_closed(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, int, int, int, int]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookies = [(await _bootstrap(app))[0] for _ in range(64)]
            touched = await _request(app, "GET", "/", headers={"cookie": cookies[0]})
            newest, _csrf = await _bootstrap(app)
            retained = await _request(app, "GET", "/", headers={"cookie": cookies[0]})
            evicted = await _request(app, "GET", "/", headers={"cookie": cookies[1]})
            malformed = await _request(
                app,
                "GET",
                "/",
                headers={"cookie": "pmh_session=!"},
            )
            illegal_key = await _request(
                app,
                "GET",
                "/",
                headers={"cookie": f"{newest}; bad@key=value"},
            )
            latest = await _request(app, "GET", "/", headers={"cookie": newest})
            assert latest.status_code == 200
            return (
                touched.status_code,
                retained.status_code,
                evicted.status_code,
                malformed.status_code,
                illegal_key.status_code,
            )

    assert asyncio.run(scenario()) == (200, 200, 401, 401, 401)


def test_duplicate_or_foreign_origin_is_rejected_before_csrf_processing(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[
        tuple[int, dict[str, str], bytes],
        httpx.Response,
    ]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            duplicate = await _raw_asgi_request(
                app,
                method="POST",
                path="/sources/codex/enable",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"http://127.0.0.1"),
                    (b"origin", b"http://127.0.0.1"),
                    (b"x-csrf-token", csrf.encode("ascii")),
                    (b"content-length", b"0"),
                ],
            )
            foreign = await _request(
                app,
                "POST",
                "/sources/codex/enable",
                headers={
                    "cookie": cookie,
                    "origin": "http://attacker.example",
                    "x-csrf-token": csrf,
                },
            )
            return duplicate, foreign

    (status, headers, body), foreign = asyncio.run(scenario())
    assert status == 403
    assert body == _expected_error_body(403)
    assert foreign.status_code == 403
    assert foreign.content == _expected_error_body(403)
    _assert_security_headers(headers)
    _assert_security_headers(foreign.headers)


def test_non_form_csrf_body_is_rejected_before_route_execution(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, _csrf = await _bootstrap(app)
            return await _raw_asgi_request(
                app,
                method="POST",
                path="/sources/codex/enable",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"http://127.0.0.1"),
                    (b"content-type", b"application/octet-stream"),
                    (b"content-length", b"1"),
                ],
                messages=[
                    {"type": "http.request", "body": b"x", "more_body": False},
                ],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 400
    assert body == _expected_error_body(400)
    _assert_security_headers(headers)


def test_body_limit_runs_before_form_parsing(tmp_path: Path) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=128,
                    max_chunk_bytes=128,
                    max_chunks=4,
                    max_files=1,
                    max_fields=16,
                ),
            )
            token = LocalAccessToken.load_or_create(container.paths)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                boot = await client.get(f"/?token={token}", follow_redirects=False)
                return await client.post(
                    "/imports/chatgpt",
                    headers={"origin": "http://127.0.0.1"},
                    files={"archive": ("private-name.zip", b"x" * 1024)},
                    data={"csrf_token": boot.headers["x-project-memory-hub-csrf"]},
                )

    response = asyncio.run(scenario())
    assert response.status_code == 413
    assert "private-name.zip" not in response.text


def test_empty_asgi_body_frames_count_toward_the_chunk_limit(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=64,
                    max_chunk_bytes=64,
                    max_chunks=2,
                    max_files=1,
                    max_fields=4,
                ),
            )
            cookie, csrf = await _bootstrap(app)
            return await _raw_asgi_request(
                app,
                method="POST",
                path="/sources/codex/enable",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"http://127.0.0.1"),
                    (b"x-csrf-token", csrf.encode("ascii")),
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"content-length", b"3"),
                ],
                messages=[
                    {"type": "http.request", "body": b"", "more_body": True},
                    {"type": "http.request", "body": b"", "more_body": True},
                    {"type": "http.request", "body": b"", "more_body": True},
                    {"type": "http.request", "body": b"x=y", "more_body": False},
                ],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 413
    assert body == _expected_error_body(413)
    _assert_security_headers(headers)


def test_content_length_smuggling_forms_are_rejected_before_auth(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, int]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cl_te, _headers, _body = await _raw_asgi_request(
                app,
                method="GET",
                path="/",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"content-length", b"0"),
                    (b"transfer-encoding", b"chunked"),
                ],
            )
            duplicate_cl, _headers, _body = await _raw_asgi_request(
                app,
                method="GET",
                path="/",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"content-length", b"0"),
                    (b"content-length", b"0"),
                ],
            )
            return cl_te, duplicate_cl

    assert asyncio.run(scenario()) == (400, 400)


def test_endpoint_consumes_body_even_when_csrf_uses_a_header(tmp_path: Path) -> None:
    async def scenario() -> int:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=8,
                    max_chunk_bytes=8,
                    max_chunks=2,
                    max_files=1,
                    max_fields=4,
                ),
            )
            cookie, csrf = await _bootstrap(app)
            status, _headers, _body = await _raw_asgi_request(
                app,
                method="POST",
                path="/sources/codex/enable",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"http://127.0.0.1"),
                    (b"x-csrf-token", csrf.encode("ascii")),
                    (b"content-type", b"application/x-www-form-urlencoded"),
                    (b"content-length", b"0"),
                ],
                messages=[
                    {
                        "type": "http.request",
                        "body": b"ignored=oversized",
                        "more_body": False,
                    }
                ],
            )
            return status

    assert asyncio.run(scenario()) == 413


def test_non_form_limit_and_declared_safe_body_rejection(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, int]:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=8,
                    max_chunk_bytes=8,
                    max_chunks=4,
                    max_files=1,
                    max_fields=4,
                ),
            )
            cookie, csrf = await _bootstrap(app)
            common = [
                (b"host", b"127.0.0.1"),
                (b"cookie", cookie.encode("ascii")),
                (b"content-type", b"application/octet-stream"),
                (b"content-length", b"1"),
            ]
            unsafe, _headers, _body = await _raw_asgi_request(
                app,
                method="POST",
                path="/sources/codex/disable",
                headers=[
                    *common,
                    (b"origin", b"http://127.0.0.1"),
                    (b"x-csrf-token", csrf.encode("ascii")),
                ],
                messages=[
                    {
                        "type": "http.request",
                        "body": b"x" * 1024,
                        "more_body": False,
                    }
                ],
            )
            safe, _headers, _body = await _raw_asgi_request(
                app,
                method="GET",
                path="/",
                headers=common,
                messages=[
                    {
                        "type": "http.request",
                        "body": b"x" * 1024,
                        "more_body": False,
                    }
                ],
            )
            return unsafe, safe

    assert asyncio.run(scenario()) == (413, 400)


def test_static_route_body_is_drained_through_the_global_limit(tmp_path: Path) -> None:
    async def scenario() -> tuple[int, dict[str, str]]:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=8,
                    max_chunk_bytes=8,
                    max_chunks=4,
                    max_files=1,
                    max_fields=4,
                ),
            )
            cookie, _csrf = await _bootstrap(app)
            status, headers, _body = await _raw_asgi_request(
                app,
                method="GET",
                path="/static/app.css",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"content-type", b"application/octet-stream"),
                    (b"transfer-encoding", b"chunked"),
                ],
                messages=[
                    {
                        "type": "http.request",
                        "body": b"x" * 1024,
                        "more_body": False,
                    }
                ],
            )
            return status, headers

    status, headers = asyncio.run(scenario())
    assert status == 413
    _assert_security_headers(headers)


@pytest.mark.parametrize(
    "path",
    ["/", "/not-found", "/static/app.css", "/sources/codex/enable"],
)
@pytest.mark.parametrize("authenticated", [False, True])
def test_declared_safe_method_bodies_fail_closed_before_routing(
    tmp_path: Path,
    path: str,
    authenticated: bool,
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(container)
            headers = [
                (b"host", b"127.0.0.1"),
                (b"content-length", b"1"),
            ]
            if authenticated:
                cookie, _csrf = await _bootstrap(app)
                headers.append((b"cookie", cookie.encode("ascii")))
            return await _raw_asgi_request(
                app,
                method="GET",
                path=path,
                headers=headers,
                messages=[{"type": "http.request", "body": b"x", "more_body": False}],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 400
    assert body == _expected_error_body(400)
    _assert_security_headers(headers)


@pytest.mark.parametrize(
    "path",
    ["/", "/not-found", "/static/app.css", "/sources/codex/enable"],
)
def test_unauthenticated_chunked_safe_requests_are_rejected_without_drain(
    tmp_path: Path,
    path: str,
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=8,
                    max_chunk_bytes=8,
                    max_chunks=2,
                    max_files=1,
                    max_fields=4,
                ),
            )
            return await _raw_asgi_request(
                app,
                method="GET",
                path=path,
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"transfer-encoding", b"chunked"),
                ],
                messages=[
                    {
                        "type": "http.request",
                        "body": b"x" * 1024,
                        "more_body": False,
                    }
                ],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 401
    assert body == _expected_error_body(401)
    _assert_security_headers(headers)


@pytest.mark.parametrize(
    "path",
    ["/", "/not-found", "/static/app.css", "/sources/codex/enable"],
)
def test_authenticated_chunked_safe_method_bodies_fail_closed(
    tmp_path: Path,
    path: str,
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, _csrf = await _bootstrap(app)
            return await _raw_asgi_request(
                app,
                method="GET",
                path=path,
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"transfer-encoding", b"chunked"),
                ],
                messages=[{"type": "http.request", "body": b"x", "more_body": False}],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 400
    assert body == _expected_error_body(400)
    _assert_security_headers(headers)


def test_bootstrap_session_is_created_only_after_a_clean_safe_body(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(container)
            token = LocalAccessToken.load_or_create(container.paths)
            return await _raw_asgi_request(
                app,
                method="GET",
                path="/",
                query_string=f"token={token}".encode("ascii"),
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"transfer-encoding", b"chunked"),
                ],
                messages=[{"type": "http.request", "body": b"x", "more_body": False}],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 400
    assert body == _expected_error_body(400)
    assert "set-cookie" not in headers
    _assert_security_headers(headers)


def test_empty_safe_method_frames_still_count_toward_chunk_limit(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=64,
                    max_chunk_bytes=64,
                    max_chunks=2,
                    max_files=1,
                    max_fields=4,
                ),
            )
            cookie, _csrf = await _bootstrap(app)
            return await _raw_asgi_request(
                app,
                method="GET",
                path="/",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"transfer-encoding", b"chunked"),
                ],
                messages=[
                    {"type": "http.request", "body": b"", "more_body": True},
                    {"type": "http.request", "body": b"", "more_body": True},
                    {"type": "http.request", "body": b"", "more_body": False},
                ],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 413
    assert body == _expected_error_body(413)
    _assert_security_headers(headers)


@pytest.mark.parametrize(
    "message",
    [
        {"type": "http.disconnect"},
        {"type": "websocket.receive", "bytes": b"unexpected"},
    ],
)
def test_authenticated_safe_requests_fail_closed_on_invalid_asgi_frames(
    tmp_path: Path,
    message: dict[str, object],
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, _csrf = await _bootstrap(app)
            return await _raw_asgi_request(
                app,
                method="GET",
                path="/",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"transfer-encoding", b"chunked"),
                ],
                messages=[message],  # type: ignore[list-item]
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 400
    assert body == _expected_error_body(400)
    _assert_security_headers(headers)


def test_unsafe_request_disconnect_is_rejected_before_route_completion(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[int, dict[str, str], bytes]:
        with _container(tmp_path) as container:
            app = create_app(container)

            @app.post("/raw-body-probe")
            async def raw_body_probe(request: Request) -> dict[str, bool]:
                await request.body()
                return {"accepted": True}

            cookie, _csrf = await _bootstrap(app)
            return await _raw_asgi_request(
                app,
                method="POST",
                path="/raw-body-probe",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"http://127.0.0.1"),
                    (b"transfer-encoding", b"chunked"),
                ],
                messages=[{"type": "http.disconnect"}],
            )

    status, headers, body = asyncio.run(scenario())
    assert status == 400
    assert body == _expected_error_body(400)
    _assert_security_headers(headers)


def test_multipart_spool_stays_private_and_is_closed_after_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed_paths: list[Path] = []
    observed_uploads = []

    async def inspect_upload(self, upload, *, dry_run: bool):
        del self
        raw_path = fcntl.fcntl(upload.file.fileno(), fcntl.F_GETPATH, b"\0" * 1024)
        observed_paths.append(Path(raw_path.split(b"\0", 1)[0].decode()))
        observed_uploads.append(upload)
        return SimpleNamespace(
            dry_run=dry_run,
            imported_count=0,
            confirmation_count=0,
        )

    monkeypatch.setattr(ControlPanelService, "import_chatgpt", inspect_upload)

    async def scenario() -> tuple[int, Path]:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=3 * 1024 * 1024,
                    max_chunk_bytes=2 * 1024 * 1024,
                    max_chunks=16,
                    max_files=1,
                    max_fields=4,
                ),
            )
            cookie, csrf = await _bootstrap(app)
            response = await _request(
                app,
                "POST",
                "/imports/chatgpt",
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    "x-csrf-token": csrf,
                },
                data={"dry_run": "true"},
                files={"archive": ("private.zip", b"x" * (1024 * 1024 + 1))},
            )
            return response.status_code, container.paths.imports.resolve()

    status, private_imports = asyncio.run(scenario())
    assert status == 303
    assert len(observed_paths) == len(observed_uploads) == 1
    assert observed_paths[0].is_relative_to(private_imports)
    assert observed_uploads[0].file.closed


def test_private_upload_directory_symlink_is_rejected_without_external_write(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, list[Path]]:
        with _container(tmp_path) as container:
            external = tmp_path / "external-uploads"
            external.mkdir()
            (container.paths.imports / "web-uploads").symlink_to(external)
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            response = await _request(
                app,
                "POST",
                "/imports/chatgpt",
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    "x-csrf-token": csrf,
                },
                data={"dry_run": "true"},
                files={"archive": ("must-not-escape.zip", b"payload")},
            )
            return response, list(external.iterdir())

    response, external_entries = asyncio.run(scenario())
    assert response.status_code == 400
    assert response.content == _expected_error_body(400)
    assert not external_entries
    assert "must-not-escape.zip" not in response.text
    _assert_security_headers(response.headers)


def test_private_upload_directory_mode_is_repaired_before_spooling(
    tmp_path: Path,
) -> None:
    async def scenario() -> tuple[httpx.Response, int]:
        with _container(tmp_path) as container:
            upload_cache = container.paths.imports / "web-uploads"
            upload_cache.mkdir(mode=0o777)
            upload_cache.chmod(0o777)
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            response = await _request(
                app,
                "POST",
                "/imports/chatgpt",
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    "x-csrf-token": csrf,
                },
                data={"dry_run": "true"},
                files={"archive": ("invalid.zip", b"not-a-zip")},
            )
            return response, stat.S_IMODE(upload_cache.stat().st_mode)

    response, mode = asyncio.run(scenario())
    assert response.status_code == 400
    assert mode == 0o700
    _assert_security_headers(response.headers)


def test_multipart_uploads_close_on_csrf_and_route_validation_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_uploads = []
    original_upload_file = formparsers.UploadFile

    class TrackingUploadFile(original_upload_file):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created_uploads.append(self)

    monkeypatch.setattr(formparsers, "UploadFile", TrackingUploadFile)

    async def scenario() -> tuple[int, int]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            denied = await _request(
                app,
                "POST",
                "/imports/chatgpt",
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                },
                data={"csrf_token": "wrong"},
                files={"archive": ("denied.zip", b"denied")},
            )
            invalid = await _request(
                app,
                "POST",
                "/imports/chatgpt",
                headers={
                    "cookie": cookie,
                    "origin": "http://127.0.0.1",
                    "x-csrf-token": csrf,
                },
                files=[
                    ("archive", ("invalid.zip", b"invalid")),
                    ("dry_run", (None, "true")),
                    ("dry_run", (None, "true")),
                ],
            )
            return denied.status_code, invalid.status_code

    assert asyncio.run(scenario()) == (403, 400)
    assert len(created_uploads) == 2
    assert all(upload.file.closed for upload in created_uploads)


def test_multipart_disconnect_closes_partial_upload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    created_files = []
    original_spooled_file = formparsers.SpooledTemporaryFile

    def tracked_spooled_file(*args, **kwargs):
        file = original_spooled_file(*args, **kwargs)
        created_files.append(file)
        return file

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracked_spooled_file)

    async def scenario() -> tuple[int, dict[str, str]]:
        with _container(tmp_path) as container:
            app = create_app(container)
            cookie, csrf = await _bootstrap(app)
            boundary = b"pmh-safe-boundary"
            partial = (
                b"--" + boundary + b'\r\nContent-Disposition: form-data; name="archive"; '
                b'filename="never-render.zip"\r\n'
                b"Content-Type: application/zip\r\n\r\npartial"
            )
            status, headers, _body = await _raw_asgi_request(
                app,
                method="POST",
                path="/imports/chatgpt",
                headers=[
                    (b"host", b"127.0.0.1"),
                    (b"cookie", cookie.encode("ascii")),
                    (b"origin", b"http://127.0.0.1"),
                    (b"x-csrf-token", csrf.encode("ascii")),
                    (
                        b"content-type",
                        b"multipart/form-data; boundary=" + boundary,
                    ),
                ],
                messages=[
                    {"type": "http.request", "body": partial, "more_body": True},
                    {"type": "http.disconnect"},
                ],
            )
            return status, headers

    status, headers = asyncio.run(scenario())
    assert status == 400
    assert created_files
    assert all(file.closed for file in created_files)
    _assert_security_headers(headers)


def test_multipart_file_count_is_limited_even_with_csrf_header(tmp_path: Path) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            app = create_app(
                container,
                request_limits=WebRequestLimits(
                    max_total_bytes=4096,
                    max_chunk_bytes=4096,
                    max_chunks=8,
                    max_files=1,
                    max_fields=4,
                ),
            )
            token = LocalAccessToken.load_or_create(container.paths)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                boot = await client.get(f"/?token={token}", follow_redirects=False)
                return await client.post(
                    "/imports/chatgpt",
                    headers={
                        "origin": "http://127.0.0.1",
                        "x-csrf-token": boot.headers["x-project-memory-hub-csrf"],
                    },
                    files=[
                        ("archive", ("one.zip", b"one")),
                        ("extra", ("two.zip", b"two")),
                    ],
                )

    response = asyncio.run(scenario())
    assert response.status_code in {400, 413}
    assert "one.zip" not in response.text
    assert "two.zip" not in response.text


def test_validation_and_exception_responses_are_redacted(tmp_path: Path) -> None:
    async def scenario() -> tuple[httpx.Response, httpx.Response]:
        with _container(tmp_path) as container:
            app = create_app(container)
            token = LocalAccessToken.load_or_create(container.paths)
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1",
            ) as client:
                await client.get(f"/?token={token}", follow_redirects=False)
                marker = "NEVER_ECHO_INVALID_PROJECT"
                invalid = await client.get(f"/memories?project_id={marker}")

                def explode():
                    raise RuntimeError("NEVER_ECHO_INTERNAL_SECRET")

                container.projects.list_control = explode  # type: ignore[attr-defined]
                failure = await client.get("/projects")
                return invalid, failure

    invalid, failure = asyncio.run(scenario())
    assert invalid.status_code in {400, 422}
    assert "NEVER_ECHO_INVALID_PROJECT" not in invalid.text
    assert failure.status_code == 500
    assert "NEVER_ECHO_INTERNAL_SECRET" not in failure.text
    for response in (invalid, failure):
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]


def test_exception_after_response_start_never_sends_a_second_start(
    tmp_path: Path,
) -> None:
    async def scenario() -> list[dict[str, Any]]:
        with _container(tmp_path) as container:
            app = create_app(container)

            async def partial_response() -> StreamingResponse:
                async def body():
                    yield b"safe partial body"
                    raise RuntimeError("NEVER_RENDER_LATE_EXCEPTION")

                return StreamingResponse(body(), media_type="text/plain")

            app.add_api_route("/partial-response", partial_response, methods=["GET"])
            cookie, _csrf = await _bootstrap(app)
            sent: list[dict[str, Any]] = []
            with pytest.raises(Exception):
                await _raw_asgi_request(
                    app,
                    method="GET",
                    path="/partial-response",
                    headers=[
                        (b"host", b"127.0.0.1"),
                        (b"cookie", cookie.encode("ascii")),
                    ],
                    sent_out=sent,
                )
            return sent

    sent = asyncio.run(scenario())
    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1
    headers = {
        name.decode("latin-1").casefold(): value.decode("latin-1")
        for name, value in starts[0]["headers"]
    }
    _assert_security_headers(headers)
    client_body = b"".join(
        message.get("body", b"") for message in sent if message["type"] == "http.response.body"
    )
    assert b"NEVER_RENDER_LATE_EXCEPTION" not in client_body
