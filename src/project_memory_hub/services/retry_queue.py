from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from project_memory_hub.domain import CapturePayload, SourceAgent
from project_memory_hub.security.capture_privacy import (
    ALL_LIST_FIELDS,
    LIST_FIELDS,
    MAX_FIELD_BYTES,
    MAX_LIST_ITEMS,
    MAX_PERSISTED_PAYLOAD_BYTES,
    TEXT_FIELDS,
    CapturePrivacyCanonicalizer,
)
from project_memory_hub.security.identifiers import (
    safe_model_identifier,
    safe_persisted_identifier,
)
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.storage.database import Database
from project_memory_hub.storage.projects import ProjectRepository
from project_memory_hub.utf8 import (
    contains_unsafe_text_control,
    strict_utf8_bytes,
    strict_utf8_size,
)


_REASONS = frozenset({"operational_failure"})
_REMOTE_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:git\+)?(?:https?|ssh|git|file|ftps?|rsync)://"
    r"[^\s<>\"'`]+"
)
_NETWORK_TLD = (
    r"(?:com|org|net|edu|gov|mil|int|io|ai|dev|app|cloud|tech|invalid|"
    r"internal|local|test|example)"
)
_NETWORK_HOST = (
    r"(?:localhost|"
    r"(?:[0-9]{1,3}[.]){3}[0-9]{1,3}|"
    rf"(?:[A-Za-z0-9-]+[.])+{_NETWORK_TLD})"
)
_SCP_REMOTE = re.compile(
    rf"(?i)(?<![A-Za-z0-9._-])(?:"
    r"[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s<>\"'`]+"
    rf"|{_NETWORK_HOST}:[^\s<>\"'`]+"
    r"|(?:[A-Za-z0-9-]+[.])+[A-Za-z0-9-]+:"
    r"(?=[^\s<>\"'`]*(?:/|[.]git|[?#]))[^\s<>\"'`]+"
    r")"
)
_ALIAS_SCP_REMOTE = re.compile(
    r"(?i)(?<![A-Za-z0-9._-])[A-Za-z0-9._-]+:"
    r"[^\s<>\"'`]+[.]git(?:[?#][^\s<>\"'`]*)?"
)
_SCHEMELESS_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._-])(?:"
    r"www[.](?:[A-Za-z0-9-]+[.])+[A-Za-z0-9-]{1,63}"
    r"(?::[0-9]{1,5})?(?:[/?#][^\s<>\"'`]*)?"
    r"|(?:[A-Za-z0-9-]+[.])+[A-Za-z0-9-]{1,63}"
    r"(?:(?::[0-9]{1,5})[/?#][^\s<>\"'`]*|[/?#][^\s<>\"'`]*)"
    r")"
)
_USERINFO_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._-])[A-Za-z0-9._+-]+@"
    r"(?:[A-Za-z0-9-]+[.])+[A-Za-z0-9-]{1,63}"
    r"[/?#][^\s<>\"'`]*"
)
_SINGLE_HOST_USERINFO_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._-])[A-Za-z0-9._+-]+@"
    r"(?:localhost|[A-Za-z][A-Za-z0-9-]*)(?::[0-9]{1,5})?"
    r"[/?#][^\s<>\"'`]*"
)
_BRACKETED_HOST_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._-])(?:[A-Za-z0-9._+-]+@)?"
    r"\[[0-9A-F:.%]+\](?::[0-9]{1,5})?[/?#][^\s<>\"'`]*"
)
_SINGLE_HOST_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._-])(?:"
    r"localhost(?::[0-9]{1,5})?[/?#][^\s<>\"'`]*"
    r"|[A-Za-z][A-Za-z0-9-]*:[0-9]{1,5}[/?#][^\s<>\"'`]*"
    r"|[A-Za-z][A-Za-z0-9-]*/[^\s<>\"'`?#]*[?#][^\s<>\"'`]*"
    r")"
)
_QUOTED_ABSOLUTE_PATH = re.compile(
    r"(?i)(?P<quote>[\"'])(?:~[/\\]|/|[A-Z]:[\\/]|\\\\)[^\"'\r\n]*"
    r"(?P=quote)"
)
_UNQUOTED_PATH_ATOM = r"[^\s<>\"'`,;()\[\]{}]+"
_UNQUOTED_PATH_TAIL = _UNQUOTED_PATH_ATOM
_UNC_ABSOLUTE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:\\\\|//)" + _UNQUOTED_PATH_TAIL)
_WINDOWS_ABSOLUTE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:[\\/]" + _UNQUOTED_PATH_TAIL)
_WINDOWS_ABSOLUTE_PREFIX = re.compile(r"(?i)^[A-Z]:[\\/]")
_LABELED_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    r"(?:cwd|path|file|root|home|project|dir|directory|repo|workspace):"
    r"(?:~[/\\]|/)" + _UNQUOTED_PATH_TAIL
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![\w._~:-])(?:~[/\\]|/)" + _UNQUOTED_PATH_TAIL)
_API_ROUTE_PREFIX = re.compile(
    r"(?i)\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT|TRACE|"
    r"route|endpoint|api(?:\s+route)?)"
    r"\s*(?:[:=]\s*)?[\"']?$"
)
_API_ROUTE_SUFFIX = re.compile(r"(?i)^[\"']?\s+(?:route|endpoint)(?:\b|$)")
_PRIVATE_POSIX_ROOTS = (
    r"(?:Users|home|private|var|tmp|Volumes|root|workspace|opt|etc|usr|srv|mnt|"
    r"Applications|Library)"
)
_PRIVATE_POSIX_PATH = re.compile(rf"^/{_PRIVATE_POSIX_ROOTS}(?:/|$)")
_PRIVATE_LOCAL_PATH_START = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:"
    rf"(?:(?i:cwd|path|file|root|home|project|dir|directory|repo|workspace):)?"
    rf"(?:~[/\\]|/{_PRIVATE_POSIX_ROOTS}(?:[/\\]|(?=$|[\s,;])))"
    r"|(?i:[A-Z]:[\\/])"
    r"|(?:\\\\|//)"
    r")"
)
_PATH_FILE_EXTENSION = re.compile(
    r"(?i)[.](?:py|pyw|pyi|js|jsx|ts|tsx|mjs|cjs|java|kt|kts|go|rs|rb|php|"
    r"swift|scala|c|h|cc|cpp|cxx|hpp|cs|sh|bash|zsh|fish|ps1|sql|proto|"
    r"graphql|json|jsonl|yaml|yml|toml|ini|cfg|conf|xml|html|htm|css|scss|"
    r"sass|less|md|mdx|rst|txt|csv|tsv|pdf|doc|docx|xls|xlsx|ppt|pptx|png|"
    r"jpg|jpeg|gif|webp|svg|ico|mp3|wav|mp4|mov|avi|zip|tar|gz|bz2|xz|7z|"
    r"dmg|pkg|app|bin|exe|dll|so|dylib|db|sqlite|sqlite3|log|lock|env)$"
)
_PATH_HARD_BOUNDARIES = "\r\n<>\"'`,;"
_PATH_PROSE_PREFIX = re.compile(
    r"(?i)^(?:and|or|but|then|in|at|from|to|for|with|without|after|before|"
    r"while|because|when|where|failed|fails|failure|error|errored|crashed|"
    r"returned|returns|reported|reports|inspect|check|review|run|use|used|"
    r"open|see|fix|update|read|write|call|called|found|caused|during|via)\b"
)
_API_ROUTE_PATH = re.compile(
    r"(?i)^/(?:api|v[0-9]+|healthz|readyz|livez|metrics|status|graphql)"
    r"(?:[/?#][^\s]*)?$"
)
_LEGACY_PAYLOAD_KEYS = frozenset(
    {"namespace", "project_id", "source_record_id", *TEXT_FIELDS, *LIST_FIELDS}
)
_V1_PAYLOAD_KEYS = _LEGACY_PAYLOAD_KEYS | {"privacy_version"}
_V2_PAYLOAD_KEYS = frozenset(
    {
        "namespace",
        "project_id",
        "source_record_id",
        *TEXT_FIELDS,
        *ALL_LIST_FIELDS,
        "privacy_version",
    }
)
_CURRENT_PRIVACY_VERSION = 2
_NAMESPACE_KEYS = frozenset({"source_agent", "model_id"})
_MAX_PAYLOAD_BYTES = MAX_PERSISTED_PAYLOAD_BYTES
_MAX_ATTEMPTS = 2**31 - 1
_MAX_DRAIN_ITEMS = 10_000


@dataclass(frozen=True, slots=True)
class RetryReport:
    completed_count: int = 0
    failed_count: int = 0
    remaining_count: int = 0


class RetryQueue:
    def __init__(
        self,
        database: Database,
        projects: ProjectRepository,
        redactor: Redactor,
        *,
        max_items_per_drain: int = 100,
    ) -> None:
        if (
            type(max_items_per_drain) is not int
            or max_items_per_drain <= 0
            or max_items_per_drain > _MAX_DRAIN_ITEMS
        ):
            raise ValueError("max_items_per_drain is out of bounds")
        self._database = database
        self._projects = projects
        self._redactor = redactor
        self._canonicalizer = CapturePrivacyCanonicalizer(redactor)
        self._max_items = max_items_per_drain

    def enqueue(self, payload: CapturePayload, reason: str) -> UUID:
        if reason not in _REASONS:
            raise ValueError("unsupported retry reason")
        project = self._projects.find_by_cwd(payload.cwd)
        if project is None:
            raise KeyError("project_not_found")
        project_path = Path(project.canonical_path)
        self._redactor.assert_safe_path(project_path)
        source_record_id = self._safe_persisted_identifier(
            payload.source_record_id, "source_record_id"
        )
        namespace = payload.namespace.model_dump(mode="json")
        namespace["model_id"] = self._safe_model_identifier(namespace["model_id"])
        private_structure = self._canonicalizer.structure(payload, project_path)
        private_structure.setdefault("resolved_open_issues", [])
        values: dict[str, object] = {
            "namespace": namespace,
            "privacy_version": _CURRENT_PRIVACY_VERSION,
            "project_id": str(project.project_id).lower(),
            "source_record_id": source_record_id,
            **private_structure,
        }
        validated_project_id, _validated_payload = self._validated_current_payload(
            values,
            project_path,
        )
        if validated_project_id != project.project_id:
            raise ValueError("retry payload rejected")
        document = _canonical_json(values)
        if strict_utf8_size(document) > _MAX_PAYLOAD_BYTES:
            raise ValueError("retry payload exceeds bound")
        retry_id = uuid4()
        with self._database.transaction() as connection:
            connection.execute(
                """
                insert into retry_items(
                    retry_id, payload_json, reason_code, created_at,
                    attempts, last_attempt_at
                ) values (?, ?, ?, ?, 0, null)
                """,
                (str(retry_id), document, reason, _utc_now()),
            )
        return retry_id

    def drain(self, capture: CaptureService) -> RetryReport:
        with self._database.connect(readonly=True) as connection:
            retry_ids = [
                row["retry_id"]
                for row in connection.execute(
                    """
                    select retry_id from retry_items
                    order by
                        case when last_attempt_at is null then 0 else 1 end,
                        coalesce(last_attempt_at, created_at),
                        created_at,
                        retry_id
                    limit ?
                    """,
                    (self._max_items + 1,),
                ).fetchall()
            ][: self._max_items]
        completed = failed = 0
        for retry_id in retry_ids:
            try:
                with self._database.transaction() as connection:
                    row = connection.execute(
                        """
                        select payload_json, reason_code,
                               length(cast(payload_json as blob)) as payload_bytes
                        from retry_items where retry_id = ?
                        """,
                        (retry_id,),
                    ).fetchone()
                    if row is None:
                        continue
                    payload_json = row["payload_json"]
                    payload_bytes = row["payload_bytes"]
                    if (
                        not isinstance(payload_json, str)
                        or type(payload_bytes) is not int
                        or payload_bytes > _MAX_PAYLOAD_BYTES
                        or row["reason_code"] not in _REASONS
                    ):
                        raise ValueError("retry payload rejected")
                    value = json.loads(
                        payload_json,
                        object_pairs_hook=_unique_object,
                    )
                    project_id, stored_version = self._validate_stored(value)
                    project = connection.execute(
                        """
                        select canonical_path from projects
                        where project_id = ? and enabled = 1
                        """,
                        (str(project_id).lower(),),
                    ).fetchone()
                    if project is None:
                        raise KeyError("project_not_found")
                    project_path = Path(project["canonical_path"])
                    self._redactor.assert_safe_path(project_path)
                    self._assert_project_identity(project_id, project_path)
                    if stored_version == 0:
                        stored_paths = value["changed_paths"]
                        assert isinstance(stored_paths, list)
                        if self._bounded_changed_paths(stored_paths, project_path) != stored_paths:
                            raise ValueError("retry payload rejected")
                    payload = self._payload_from_stored(
                        value,
                        project_path,
                        stored_version,
                    )
                    if stored_version < 2:
                        current_structure = self._canonicalizer.stored_structure(
                            payload,
                            project_path,
                        )
                        if stored_version == 1:
                            stored_structure = {
                                field: value[field] for field in (*TEXT_FIELDS, *LIST_FIELDS)
                            }
                            if current_structure != stored_structure:
                                raise ValueError("retry payload rejected")
                        current_structure.setdefault("resolved_open_issues", [])
                        current_value = {
                            **{
                                key: item for key, item in value.items() if key != "privacy_version"
                            },
                            "privacy_version": _CURRENT_PRIVACY_VERSION,
                            **current_structure,
                        }
                    else:
                        current_value = value
                    current_project_id, payload = self._validated_current_payload(
                        current_value,
                        project_path,
                    )
                    if current_project_id != project_id:
                        raise ValueError("retry payload rejected")
                    result = capture._capture_untrusted_on_connection(
                        connection,
                        payload,
                        project_id,
                    )
                    if result.status != "pending_verification":
                        raise RuntimeError("retry_not_accepted")
                    self._assert_project_identity(project_id, project_path)
                    connection.execute("delete from retry_items where retry_id = ?", (retry_id,))
                completed += 1
            except Exception:
                failed += 1
                self._record_failure(retry_id)
        with self._database.connect(readonly=True) as connection:
            remaining = len(
                connection.execute(
                    "select 1 from retry_items limit ?",
                    (self._max_items + 1,),
                ).fetchall()
            )
        return RetryReport(completed, failed, remaining)

    def _record_failure(self, retry_id: str) -> None:
        try:
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    update retry_items
                    set attempts = case
                            when attempts < ? then attempts + 1
                            else ?
                        end,
                        last_attempt_at = ?
                    where retry_id = ?
                    """,
                    (_MAX_ATTEMPTS, _MAX_ATTEMPTS, _utc_now(), retry_id),
                )
        except Exception:
            return

    def _validate_stored(self, value: object) -> tuple[UUID, Literal[0, 1, 2]]:
        if not isinstance(value, dict):
            raise ValueError("retry payload rejected")
        keys = set(value)
        if keys == _LEGACY_PAYLOAD_KEYS:
            stored_version: Literal[0, 1, 2] = 0
        elif keys == _V1_PAYLOAD_KEYS:
            if type(value.get("privacy_version")) is not int or value["privacy_version"] != 1:
                raise ValueError("retry payload rejected")
            stored_version = 1
        elif keys == _V2_PAYLOAD_KEYS:
            if type(value.get("privacy_version")) is not int or value["privacy_version"] != 2:
                raise ValueError("retry payload rejected")
            stored_version = 2
        else:
            raise ValueError("retry payload rejected")
        namespace = value.get("namespace")
        if not isinstance(namespace, dict) or set(namespace) != _NAMESPACE_KEYS:
            raise ValueError("retry payload rejected")
        source_agent = namespace.get("source_agent")
        if not isinstance(source_agent, str):
            raise ValueError("retry payload rejected")
        try:
            SourceAgent(source_agent)
        except (TypeError, ValueError):
            raise ValueError("retry payload rejected")
        self._safe_model_identifier(namespace.get("model_id"))
        self._safe_persisted_identifier(value.get("source_record_id"), "source_record_id")
        for field in TEXT_FIELDS:
            item = value.get(field)
            if not isinstance(item, str):
                raise ValueError("retry payload rejected")
            self._validate_bounded_text(item)
            if stored_version == 0 and self._bounded_private_text(item) != item:
                raise ValueError("retry payload rejected")
        list_fields = ALL_LIST_FIELDS if stored_version == 2 else LIST_FIELDS
        for field in list_fields:
            items = value.get(field)
            if not isinstance(items, list) or len(items) > MAX_LIST_ITEMS:
                raise ValueError("retry payload rejected")
            for item in items:
                self._validate_bounded_text(item)
                if (
                    stored_version == 0
                    and field != "changed_paths"
                    and self._bounded_private_text(item) != item
                ):
                    raise ValueError("retry payload rejected")
        project_id_value = value.get("project_id")
        if not isinstance(project_id_value, str):
            raise ValueError("retry payload rejected")
        try:
            project_id = UUID(project_id_value)
        except ValueError:
            raise ValueError("retry payload rejected") from None
        if project_id_value != str(project_id).lower():
            raise ValueError("retry payload rejected")
        return project_id, stored_version

    def _validated_current_payload(
        self,
        value: object,
        project_path: Path,
    ) -> tuple[UUID, CapturePayload]:
        project_id, stored_version = self._validate_stored(value)
        if stored_version != _CURRENT_PRIVACY_VERSION:
            raise ValueError("retry payload rejected")
        assert isinstance(value, dict)
        payload = self._payload_from_stored(value, project_path, stored_version)
        stored_structure = self._canonicalizer.stored_structure(payload, project_path)
        stored_structure.setdefault("resolved_open_issues", [])
        expected_structure = {field: value[field] for field in (*TEXT_FIELDS, *ALL_LIST_FIELDS)}
        if stored_structure != expected_structure:
            raise ValueError("retry payload rejected")
        return project_id, payload

    @staticmethod
    def _payload_from_stored(
        value: dict[object, object],
        project_path: Path,
        stored_version: Literal[0, 1, 2],
    ) -> CapturePayload:
        list_fields = ALL_LIST_FIELDS if stored_version == 2 else LIST_FIELDS
        return CapturePayload.model_validate(
            {
                "cwd": project_path,
                "namespace": value["namespace"],
                "source_record_id": value["source_record_id"],
                **{field: value[field] for field in (*TEXT_FIELDS, *list_fields)},
            }
        )

    def _assert_project_identity(self, project_id: UUID, project_path: Path) -> None:
        verified = self._projects.find_by_cwd(project_path)
        if (
            verified is None
            or verified.project_id != project_id
            or Path(verified.canonical_path) != project_path
        ):
            raise ValueError("retry project rejected")

    def _safe_persisted_identifier(self, value: object, field: str) -> str:
        return safe_persisted_identifier(value, field, self._redactor)

    def _safe_model_identifier(self, value: object) -> str:
        return safe_model_identifier(value, self._redactor)

    @staticmethod
    def _validate_bounded_text(value: object) -> None:
        if (
            not isinstance(value, str)
            or strict_utf8_size(value) > MAX_FIELD_BYTES
            or contains_unsafe_text_control(value, allow_normal_text_whitespace=True)
        ):
            raise ValueError("retry payload rejected")

    def _bounded_private_text(self, value: str) -> str:
        self._validate_bounded_text(value)
        result = _REMOTE_URL.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), value
        )
        result = _SCP_REMOTE.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), result
        )
        result = _ALIAS_SCP_REMOTE.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), result
        )
        result = _USERINFO_URL.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), result
        )
        result = _SINGLE_HOST_USERINFO_URL.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), result
        )
        result = _BRACKETED_HOST_URL.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), result
        )
        result = _SINGLE_HOST_URL.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), result
        )
        result = _SCHEMELESS_URL.sub(
            lambda match: _private_fingerprint("remote", match.group(0)), result
        )
        source = result
        result = _QUOTED_ABSOLUTE_PATH.sub(
            lambda match: _private_path_or_api_route(source, match), source
        )
        result = _redact_private_local_paths(result)
        result = _UNC_ABSOLUTE_PATH.sub(
            lambda match: _private_fingerprint("absolute_path", match.group(0)),
            result,
        )
        result = _WINDOWS_ABSOLUTE_PATH.sub(
            lambda match: _private_fingerprint("absolute_path", match.group(0)),
            result,
        )
        result = _LABELED_ABSOLUTE_PATH.sub(
            lambda match: _private_fingerprint("absolute_path", match.group(0)),
            result,
        )
        source = result
        result = _POSIX_ABSOLUTE_PATH.sub(
            lambda match: _private_path_or_api_route(source, match), source
        )
        result = self._redactor.redact(result).text
        self._validate_bounded_text(result)
        return result

    def _bounded_list(self, values: list[str]) -> list[str]:
        if len(values) > MAX_LIST_ITEMS:
            raise ValueError("retry list exceeds bound")
        return [self._bounded_private_text(value) for value in values]

    def _bounded_changed_paths(
        self,
        values: list[str],
        project_path: Path,
    ) -> list[str]:
        if len(values) > MAX_LIST_ITEMS:
            raise ValueError("retry list exceeds bound")
        root = project_path.resolve(strict=True)
        safe: list[str] = []
        for value in values:
            self._validate_bounded_text(value)
            if not value or "\x00" in value:
                continue
            lexical = Path(value)
            if (
                ".." in lexical.parts
                or value.startswith(("~", "\\\\"))
                or _WINDOWS_ABSOLUTE_PREFIX.match(value) is not None
            ):
                continue
            candidate = lexical if lexical.is_absolute() else root / lexical
            try:
                resolved = candidate.resolve(strict=False)
                relative = resolved.relative_to(root)
                if not relative.parts:
                    continue
                self._redactor.assert_safe_path(relative)
            except (OSError, ValueError):
                continue
            prepared = relative.as_posix()
            self._validate_bounded_text(prepared)
            safe.append(prepared)
        return safe


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("retry payload rejected")
        value[key] = item
    return value


def _private_fingerprint(category: str, value: str) -> str:
    digest = hashlib.sha256(strict_utf8_bytes(value)).hexdigest()[:16]
    return f"[REDACTED:{category}:{digest}]"


def _redact_private_local_paths(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    while match := _PRIVATE_LOCAL_PATH_START.search(value, cursor):
        end = _private_local_path_end(value, match.start(), match.end())
        pieces.append(value[cursor : match.start()])
        pieces.append(_private_fingerprint("absolute_path", value[match.start() : end]))
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _private_local_path_end(value: str, start: int, end: int) -> int:
    while end < len(value):
        character = value[end]
        if character in _PATH_HARD_BOUNDARIES:
            break
        if (
            character.isspace()
            and _path_ends_with_file_extension(value[start:end])
            and not _path_remainder_looks_like_path(value, end)
        ):
            break
        end += 1
    return end


def _path_ends_with_file_extension(value: str) -> bool:
    candidate = value.rstrip().rstrip(")]}")
    candidate = candidate.split("?", 1)[0].split("#", 1)[0]
    return _PATH_FILE_EXTENSION.search(candidate) is not None


def _path_remainder_looks_like_path(value: str, start: int) -> bool:
    end = start
    while end < len(value) and value[end] not in _PATH_HARD_BOUNDARIES:
        end += 1
    candidate = value[start:end].strip().rstrip(")]}")
    if not candidate:
        return False
    if _PATH_PROSE_PREFIX.search(candidate):
        return False
    return _path_ends_with_file_extension(candidate)


def _private_path_or_api_route(source: str, match: re.Match[str]) -> str:
    value = match.group(0)
    candidate = value
    if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
        candidate = value[1:-1]
    if candidate.startswith("/") and not candidate.startswith("//"):
        if _PRIVATE_POSIX_PATH.search(candidate):
            return _private_fingerprint("absolute_path", value)
        if any(character.isspace() for character in candidate):
            return _private_fingerprint("absolute_path", value)
        if _API_ROUTE_PATH.fullmatch(candidate):
            return value
        prefix = source[max(0, match.start() - 80) : match.start()]
        suffix = source[match.end() : match.end() + 40]
        if _API_ROUTE_PREFIX.search(prefix) or _API_ROUTE_SUFFIX.search(suffix):
            return value
    return _private_fingerprint("absolute_path", value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
