from __future__ import annotations

import ipaddress
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any, BinaryIO, cast
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException, Request
from python_multipart.multipart import parse_options_header
from starlette.datastructures import FormData, Headers, MutableHeaders, UploadFile
from starlette.formparsers import FormParser, MultiPartParser
from starlette.responses import RedirectResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from project_memory_hub.paths import RuntimePaths


_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SESSION_COOKIE = "pmh_session"
_SETUP_FORM_PATHS = frozenset({"/setup/configure", "/setup/complete"})
_SETUP_MAX_TOTAL_BYTES = 256 * 1024
_SETUP_MAX_FIELDS = 40
_PROXY_HEADERS = frozenset(
    {
        "forwarded",
        "x-real-ip",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-prefix",
        "x-forwarded-proto",
    }
)
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'self'; base-uri 'none'; connect-src 'self'; "
        "form-action 'self'; frame-ancestors 'none'; img-src 'self' data:; "
        "object-src 'none'; script-src 'self'; style-src 'self'"
    ),
    "Permissions-Policy": "camera=(), geolocation=(), microphone=()",
    # Chromium serializes the Origin header as ``null`` for same-origin form
    # submissions when a document uses ``no-referrer``.  That makes our strict
    # Origin check reject every legitimate browser POST.  ``same-origin`` keeps
    # referrers inside this loopback origin, never sends them cross-origin, and
    # preserves a concrete Origin header for CSRF enforcement.
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class RequestBodyLimitExceeded(OSError):
    """Raised before a route can parse an oversized request body."""


class _RequestBodyDisconnected(OSError):
    """Raised so multipart parsers close partial upload files on disconnect."""


@dataclass(frozen=True, slots=True)
class WebRequestLimits:
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    max_chunk_bytes: int = 1024 * 1024
    max_chunks: int = 65_536
    max_files: int = 1
    max_fields: int = 16

    def __post_init__(self) -> None:
        values = (
            self.max_total_bytes,
            self.max_chunk_bytes,
            self.max_chunks,
            self.max_files,
            self.max_fields,
        )
        if any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("web request limits must be positive integers")


class LocalAccessToken:
    _creation_lock = threading.Lock()
    _race_attempts = 20
    _race_delay_seconds = 0.005

    @classmethod
    def load_or_create(cls, paths: RuntimePaths) -> str:
        if not isinstance(paths, RuntimePaths):
            raise TypeError("paths must be RuntimePaths")
        paths.ensure()
        with cls._creation_lock:
            try:
                return cls._load(paths.access_token)
            except FileNotFoundError:
                return cls._create(paths.access_token)

    @classmethod
    def load_existing(cls, paths: RuntimePaths) -> str:
        if not isinstance(paths, RuntimePaths):
            raise TypeError("paths must be RuntimePaths")
        return cls._load(paths.access_token)

    @staticmethod
    def matches(expected: str, supplied: str) -> bool:
        if not isinstance(expected, str) or not isinstance(supplied, str):
            return False
        return secrets.compare_digest(expected, supplied)

    @classmethod
    def _create(cls, path: Path) -> str:
        token = secrets.token_urlsafe(32)
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise RuntimeError("token generator returned an invalid value")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            return cls._load_after_create_race(path)
        except OSError:
            raise PermissionError("local access token file rejected") from None
        try:
            os.fchmod(descriptor, 0o600)
            metadata = os.fstat(descriptor)
            cls._validate_metadata(metadata)
            data = token.encode("ascii")
            written = 0
            while written < len(data):
                count = os.write(descriptor, data[written:])
                if count <= 0:
                    raise OSError("short token write")
                written += count
            os.fsync(descriptor)
        except Exception:
            try:
                path.unlink(missing_ok=True)
            finally:
                raise
        finally:
            os.close(descriptor)
        _sync_directory(path.parent)
        return token

    @classmethod
    def _load_after_create_race(cls, path: Path) -> str:
        for attempt in range(cls._race_attempts):
            try:
                return cls._load(path)
            except (FileNotFoundError, PermissionError):
                if attempt + 1 == cls._race_attempts:
                    break
                time.sleep(cls._race_delay_seconds)
        raise PermissionError("local access token file rejected")

    @classmethod
    def _load(cls, path: Path) -> str:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            raise
        except OSError:
            raise PermissionError("local access token file rejected") from None
        try:
            metadata = os.fstat(descriptor)
            cls._validate_metadata(metadata)
            if metadata.st_size != 43:
                raise PermissionError("local access token file rejected")
            data = os.read(descriptor, 44)
            if len(data) != 43:
                raise PermissionError("local access token file rejected")
        finally:
            os.close(descriptor)
        try:
            token = data.decode("ascii")
        except UnicodeDecodeError:
            raise PermissionError("local access token file rejected") from None
        if _TOKEN_PATTERN.fullmatch(token) is None:
            raise PermissionError("local access token file rejected")
        return token

    @staticmethod
    def _validate_metadata(metadata: os.stat_result) -> None:
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise PermissionError("local access token file rejected")


class _SessionStore:
    def __init__(self, max_sessions: int = 64) -> None:
        self._max_sessions = max_sessions
        self._sessions: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()

    def create(self) -> tuple[str, str]:
        session_id = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[session_id] = csrf
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self._max_sessions:
                self._sessions.popitem(last=False)
        return session_id, csrf

    def csrf_for(self, session_id: str | None) -> str | None:
        if session_id is None:
            return None
        with self._lock:
            value = self._sessions.get(session_id)
            if value is not None:
                self._sessions.move_to_end(session_id)
            return value


class _LimitedReceive:
    def __init__(self, receive: Receive, limits: WebRequestLimits) -> None:
        self._receive = receive
        self._limits = limits
        self._bytes = 0
        self._chunks = 0

    async def __call__(self) -> Message:
        message = await self._receive()
        if message["type"] == "http.disconnect":
            raise _RequestBodyDisconnected
        if message["type"] != "http.request":
            raise _RequestBodyDisconnected
        self._chunks += 1
        body = message.get("body", b"")
        self._bytes += len(body)
        if (
            len(body) > self._limits.max_chunk_bytes
            or self._bytes > self._limits.max_total_bytes
            or self._chunks > self._limits.max_chunks
        ):
            raise RequestBodyLimitExceeded
        return message


class _PrivateMultiPartParser(MultiPartParser):
    def __init__(self, *args: Any, directory: Path, **kwargs: Any) -> None:
        self._directory = directory
        super().__init__(*args, **kwargs)

    def on_headers_finished(self) -> None:
        super().on_headers_finished()
        upload = self._current_part.file
        if upload is None:
            return
        original = upload.file
        original_identity: object = original
        replacement = tempfile.SpooledTemporaryFile(
            max_size=self.spool_max_size,
            mode="w+b",
            dir=os.fspath(self._directory),
        )
        try:
            index = next(
                index
                for index, candidate in enumerate(self._files_to_close_on_error)
                if candidate is original_identity
            )
            self._files_to_close_on_error[index] = replacement
            upload.file = cast(BinaryIO, replacement)
        except BaseException:
            replacement.close()
            raise
        finally:
            original.close()

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException:
            for file in self._files_to_close_on_error:
                file.close()
            raise


class LocalWebBoundary:
    def __init__(
        self,
        app: ASGIApp,
        *,
        access_token: str,
        request_limits: WebRequestLimits,
    ) -> None:
        self.app = app
        self._access_token = access_token
        self._limits = request_limits
        self._sessions = _SessionStore()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            headers = Headers(scope=scope)
            host_values = headers.getlist("host")
            host_is_allowed = len(host_values) == 1 and _allowed_host(host_values[0])
            content_length = _content_length(headers)
        except Exception:
            await _send_response(_error_response(400), scope, receive, send)
            return
        if not host_is_allowed:
            await _send_response(_error_response(400), scope, receive, send)
            return
        if any(name.decode("latin-1").casefold() in _PROXY_HEADERS for name, _ in scope["headers"]):
            await _send_response(_error_response(400), scope, receive, send)
            return
        if _has_ambiguous_framing(headers) or content_length is None:
            await _send_response(_error_response(400), scope, receive, send)
            return
        if content_length > self._limits.max_total_bytes:
            await _send_response(_error_response(413), scope, receive, send)
            return
        safe_method = scope["method"] in _SAFE_METHODS
        if safe_method and content_length > 0:
            await _send_response(_error_response(400), scope, receive, send)
            return
        limited_receive = _LimitedReceive(receive, self._limits)
        app_receive: Receive = limited_receive

        query = parse_qs(
            scope.get("query_string", b"").decode("latin-1"),
            keep_blank_values=True,
        )
        supplied_tokens = query.get("token", ())
        if supplied_tokens:
            if (
                scope["method"] != "GET"
                or scope["path"] != "/"
                or len(supplied_tokens) != 1
                or not LocalAccessToken.matches(self._access_token, supplied_tokens[0])
            ):
                await _send_response(_error_response(401), scope, receive, send)
                return
            rejection = await _safe_body_rejection(limited_receive) if safe_method else None
            if rejection is not None:
                await _send_response(rejection, scope, receive, send)
                return
            session_id, csrf = self._sessions.create()
            response = RedirectResponse("/", status_code=303)
            response.set_cookie(
                _SESSION_COOKIE,
                session_id,
                httponly=True,
                samesite="strict",
                path="/",
                secure=False,
            )
            response.headers["X-Project-Memory-Hub-CSRF"] = csrf
            _apply_security_headers(response.headers)
            await response(scope, receive, send)
            return

        existing_session_id: str | None
        try:
            existing_session_id = _session_cookie(headers)
        except Exception:
            existing_session_id = None
        existing_csrf = self._sessions.csrf_for(existing_session_id)
        if existing_csrf is None:
            await _send_response(_error_response(401), scope, receive, send)
            return

        if safe_method:
            rejection = await _safe_body_rejection(limited_receive)
            if rejection is not None:
                await _send_response(rejection, scope, receive, send)
                return
            app_receive = _empty_request_receive()
        elif not _same_origin(headers.getlist("origin"), host_values[0]):
            await _send_response(_error_response(403), scope, receive, send)
            return

        scope.setdefault("state", {})["pmh_csrf"] = existing_csrf
        scope["state"]["pmh_request_limits"] = self._limits
        response_started = False

        async def secure_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                if response_started:
                    raise RuntimeError("response already started")
                mutable = MutableHeaders(scope=message)
                _apply_security_headers(mutable)
                response_started = True
            await send(message)

        try:
            await self.app(scope, app_receive, secure_send)
        except RequestBodyLimitExceeded:
            if response_started:
                raise
            await _send_response(_error_response(413), scope, receive, send)
        except _RequestBodyDisconnected:
            if response_started:
                raise
            await _send_response(_error_response(400), scope, receive, send)
        except Exception:
            if response_started:
                raise
            await _send_response(_error_response(500), scope, receive, send)
        finally:
            await _close_request_form(scope)


async def _drain_request(receive: Receive) -> bool:
    received_body = False
    while True:
        message = await receive()
        if message.get("body", b""):
            received_body = True
        if not message.get("more_body", False):
            return received_body


async def _safe_body_rejection(receive: Receive) -> Response | None:
    try:
        received_body = await _drain_request(receive)
    except RequestBodyLimitExceeded:
        return _error_response(413)
    except _RequestBodyDisconnected:
        return _error_response(400)
    if received_body:
        return _error_response(400)
    return None


def _empty_request_receive() -> Receive:
    delivered = False

    async def receive() -> Message:
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive


async def require_csrf(request: Request) -> None:
    if request.method in _SAFE_METHODS:
        await _require_empty_body(request)
        return
    if request.url.path == "/sources/trae/probe":
        await _require_urlencoded_probe_form(request)
    expected = getattr(request.state, "pmh_csrf", None)
    header_values = request.headers.getlist("x-csrf-token")
    if len(header_values) > 1:
        raise HTTPException(status_code=403, detail="request_denied")
    supplied = header_values[0] if header_values else None
    if not header_values:
        form = await (
            limited_setup_form(request)
            if request.url.path in _SETUP_FORM_PATHS
            else limited_form(request)
        )
        values = form.getlist("csrf_token")
        if len(values) == 1 and isinstance(values[0], str):
            supplied = values[0]
    if (
        not isinstance(expected, str)
        or not isinstance(supplied, str)
        or not secrets.compare_digest(expected, supplied)
    ):
        raise HTTPException(status_code=403, detail="request_denied")


async def _require_urlencoded_probe_form(request: Request) -> None:
    try:
        content_type, _options = parse_options_header(request.headers.get("content-type"))
    except Exception:
        content_type = b""
    if content_type == b"application/x-www-form-urlencoded":
        return
    async for _chunk in request.stream():
        pass
    raise HTTPException(status_code=400, detail="invalid_request")


async def limited_form(request: Request) -> FormData:
    limits: WebRequestLimits = request.state.pmh_request_limits
    try:
        form = await _parse_limited_form(request, limits)
    except RequestBodyLimitExceeded:
        raise
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_request") from None
    file_count = sum(isinstance(value, UploadFile) for _, value in form.multi_items())
    field_count = sum(not isinstance(value, UploadFile) for _, value in form.multi_items())
    if file_count > limits.max_files or field_count > limits.max_fields:
        raise HTTPException(status_code=400, detail="invalid_request")
    request.state.pmh_form = form
    return form


async def limited_setup_form(request: Request) -> FormData:
    if request._form is not None:  # noqa: SLF001 - Starlette's own form cache
        return request._form  # noqa: SLF001
    content_lengths = request.headers.getlist("content-length")
    if len(content_lengths) == 1 and int(content_lengths[0]) > _SETUP_MAX_TOTAL_BYTES:
        raise RequestBodyLimitExceeded
    try:
        content_type, _options = parse_options_header(request.headers.get("content-type"))
    except Exception:
        content_type = b""
    if content_type != b"application/x-www-form-urlencoded":
        async for _chunk in _setup_limited_stream(request):
            pass
        raise HTTPException(status_code=400, detail="invalid_request")
    try:
        form = await FormParser(
            request.headers,
            _setup_limited_stream(request),
            max_fields=_SETUP_MAX_FIELDS,
            max_part_size=_SETUP_MAX_TOTAL_BYTES,
        ).parse()
    except RequestBodyLimitExceeded:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_request") from None
    if len(form.multi_items()) > _SETUP_MAX_FIELDS:
        raise HTTPException(status_code=400, detail="invalid_request")
    request._form = form  # noqa: SLF001 - preserve Starlette's parse-once contract
    request.state.pmh_form = form
    return form


async def _setup_limited_stream(request: Request) -> AsyncGenerator[bytes, None]:
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _SETUP_MAX_TOTAL_BYTES:
            raise RequestBodyLimitExceeded
        yield chunk


async def _parse_limited_form(request: Request, limits: WebRequestLimits) -> FormData:
    if request._form is not None:  # noqa: SLF001 - Starlette's own form cache
        return request._form  # noqa: SLF001
    try:
        content_type, _options = parse_options_header(request.headers.get("content-type"))
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_request") from None
    if content_type == b"multipart/form-data":
        multipart_parser = _PrivateMultiPartParser(
            request.headers,
            request.stream(),
            max_files=limits.max_files,
            max_fields=limits.max_fields,
            max_part_size=min(limits.max_total_bytes, 1024 * 1024),
            directory=_private_multipart_directory(request),
        )
        form = await multipart_parser.parse()
    elif content_type == b"application/x-www-form-urlencoded":
        form_parser = FormParser(
            request.headers,
            request.stream(),
            max_fields=limits.max_fields,
            max_part_size=min(limits.max_total_bytes, 1024 * 1024),
        )
        form = await form_parser.parse()
    else:
        await _require_empty_body(request)
        form = FormData()
    request._form = form  # noqa: SLF001 - preserve Starlette's parse-once contract
    return form


async def _require_empty_body(request: Request) -> None:
    received_body = False
    async for chunk in request.stream():
        if chunk:
            received_body = True
    if received_body:
        raise HTTPException(status_code=400, detail="invalid_request")


async def _close_request_form(scope: Scope) -> None:
    state = scope.get("state")
    form = state.pop("pmh_form", None) if isinstance(state, dict) else None
    if isinstance(form, FormData):
        try:
            await form.close()
        except Exception:
            pass


def _private_multipart_directory(request: Request) -> Path:
    imports = cast(Path, request.app.state.container.paths.imports)
    path = imports / "web-uploads"
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass
    flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise PermissionError("private upload directory rejected") from None
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise PermissionError("private upload directory rejected")
        if stat.S_IMODE(metadata.st_mode) != 0o700:
            os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return path


def _allowed_host(value: str) -> bool:
    parsed = _split_host(value)
    if parsed is None:
        return False
    hostname, _port = parsed
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def loopback_bind_host(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("serve host must be loopback")
    host = value.strip()
    if host.casefold() == "localhost":
        return "localhost"
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("serve host must be loopback") from None
    if not address.is_loopback:
        raise ValueError("serve host must be loopback")
    return str(address)


def _split_host(value: str) -> tuple[str, int | None] | None:
    if not isinstance(value, str) or not value or any(character.isspace() for character in value):
        return None
    host: str
    port_text: str | None
    bracketed = value.startswith("[")
    if bracketed:
        closing = value.find("]")
        if closing <= 1:
            return None
        host = value[1:closing]
        remainder = value[closing + 1 :]
        if not remainder:
            port_text = None
        elif remainder.startswith(":") and remainder.count(":") == 1:
            port_text = remainder[1:]
        else:
            return None
    else:
        if value.count(":") > 1:
            return None
        host, separator, port_text = value.partition(":")
        if not separator:
            port_text = None
    if not host or any(character in host for character in "/\\@#?"):
        return None
    if bracketed:
        if "%" in host:
            return None
        try:
            bracketed_address = ipaddress.ip_address(host)
        except ValueError:
            return None
        if not isinstance(bracketed_address, ipaddress.IPv6Address):
            return None
    if port_text is None:
        return host, None
    if not port_text.isascii() or not port_text.isdigit() or len(port_text) > 5:
        return None
    if len(port_text) > 1 and port_text.startswith("0"):
        return None
    port = int(port_text)
    if not 1 <= port <= 65_535:
        return None
    return host, port


def _same_origin(values: list[str], host: str) -> bool:
    if len(values) != 1:
        return False
    try:
        parsed = urlsplit(values[0])
    except (UnicodeError, ValueError):
        return False
    if (
        parsed.scheme.casefold() != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or not parsed.netloc
    ):
        return False
    host_authority = _canonical_authority(host)
    origin_authority = _canonical_authority(parsed.netloc)
    return (
        host_authority is not None
        and origin_authority is not None
        and host_authority == origin_authority
        and _allowed_host(host)
    )


def _canonical_authority(value: str) -> tuple[str, int] | None:
    parsed = _split_host(value)
    if parsed is None:
        return None
    hostname, port = parsed
    if hostname.casefold() == "localhost":
        canonical_host = "localhost"
    else:
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            return None
        if getattr(address, "scope_id", None) is not None:
            return None
        canonical_host = str(address)
    return canonical_host, port if port is not None else 80


def _session_cookie(headers: Headers) -> str | None:
    cookie_values = headers.getlist("cookie")
    if len(cookie_values) != 1:
        return None
    assignments = re.findall(rf"(?:^|;)\s*{re.escape(_SESSION_COOKIE)}\s*=", cookie_values[0])
    if len(assignments) != 1:
        return None
    parsed = SimpleCookie()
    try:
        parsed.load(cookie_values[0])
    except CookieError:
        return None
    morsel = parsed.get(_SESSION_COOKIE)
    if morsel is None or _TOKEN_PATTERN.fullmatch(morsel.value) is None:
        return None
    return morsel.value


def _content_length(headers: Headers) -> int | None:
    values = headers.getlist("content-length")
    if not values:
        return 0
    if (
        len(values) != 1
        or not values[0].isascii()
        or not values[0].isdigit()
        or len(values[0]) > 20
        or (len(values[0]) > 1 and values[0].startswith("0"))
    ):
        return None
    return int(values[0])


def _has_ambiguous_framing(headers: Headers) -> bool:
    return bool(headers.getlist("content-length")) and bool(headers.getlist("transfer-encoding"))


async def _send_response(response: Response, scope: Scope, receive: Receive, send: Send) -> None:
    _apply_security_headers(response.headers)
    await response(scope, receive, send)


def _error_response(status_code: int) -> Response:
    # A local import avoids the web package's public create_app re-export
    # during security module initialization.
    from project_memory_hub.web.errors import error_response

    return error_response(status_code)


def _apply_security_headers(headers: Any) -> None:
    for name, value in _SECURITY_HEADERS.items():
        headers[name] = value


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
