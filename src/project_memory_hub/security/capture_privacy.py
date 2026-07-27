from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from project_memory_hub.domain import CapturePayload
from project_memory_hub.security.redaction import Redactor, normalize_redacted_text
from project_memory_hub.utf8 import (
    contains_unsafe_text_control,
    strict_utf8_bytes,
    strict_utf8_size,
)


TEXT_FIELDS = ("objective", "outcome")
LIST_FIELDS = (
    "decisions",
    "failed_attempts",
    "verified_commands",
    "changed_paths",
    "preferences",
    "risks",
    "open_issues",
    "reusable_lessons",
)
OPTIONAL_LIST_FIELDS = ("resolved_open_issues",)
ALL_LIST_FIELDS = (*LIST_FIELDS, *OPTIONAL_LIST_FIELDS)
MAX_FIELD_BYTES = 32 * 1024
MAX_LIST_ITEMS = 100
MAX_CAPTURE_BYTES = 192 * 1024
MAX_PRIVATE_STRUCTURE_BYTES = 224 * 1024
MAX_PERSISTED_PAYLOAD_BYTES = 256 * 1024

_REMOTE_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://"
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
_ALIAS_PATH_REMOTE = re.compile(
    r"(?i)(?<![A-Za-z0-9._:\-])(?P<label>[A-Za-z][A-Za-z0-9._-]*):"
    r"(?P<value>[^\s<>\"'`,;]*[/\\][^\s<>\"'`,;]*)"
)
_IPV6_SCP_REMOTE = re.compile(
    r"(?i)(?<![A-Za-z0-9._-])(?:[A-Za-z0-9._-]+@)?"
    r"\[[0-9A-F:.]+(?:%[A-Za-z0-9._~-]+)?\]:[^\s<>\"'`]+"
)
_CANONICAL_PRIVATE_VALUE = re.compile(
    r"\[REDACTED:(?:(?:remote|absolute_path|remote_command|capture_field):[0-9a-f]{16}|"
    r"api_key|bearer_token|private_key|password|sensitive_path)\]"
)
_GIT_URL_REWRITE = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:"
    r'"url\.[^"\r\n]{1,32768}?\.(?:push)?insteadof"'
    r"|'url\.[^'\r\n]{1,32768}?\.(?:push)?insteadof'"
    r"|url\.[^\s,;|&}\]\r\n]{1,32768}?\.(?:push)?insteadof"
    r"(?![A-Za-z0-9_.-]))"
)
_GIT_URL_REWRITE_PLACEHOLDER = re.compile(r"\[PMH_GIT_URL_REWRITE:(?P<digest>[0-9a-f]{16})\]")
_GIT_URL_REWRITE_VALUE = re.compile(
    r"(?i)(?P<label>\[PMH_GIT_URL_REWRITE:[0-9a-f]{16}\]"
    r"(?:\s*[:=]\s*[\"']?|\s+))"
    r"(?P<value>[^\s\"',;(){}\[\]]+)"
)
_GIT_URL_SECTION_REWRITE = re.compile(
    r"(?i)(?P<label>\[\s*url(?:\s+[\"'][^\]\r\n]+[\"'])?\s*\]\s+"
    r"(?:push)?insteadof\s*(?:[:=]\s*|\s+))"
    r"(?P<value>\[REDACTED:(?:remote|absolute_path|remote_command|capture_field):"
    r"[0-9a-f]{16}\]|\[REDACTED:(?:api_key|bearer_token|private_key|password|"
    r"sensitive_path)\]|[\"'][^\"'\r\n]*[\"']|[^\s,;(){}\[\]]+)"
)
_STRONG_REMOTE_VALUE = re.compile(
    r"(?i)(?P<label>"
    r"(?<![A-Za-z0-9_.:\-])[\"']?(?:"
    r"remote(?:\.[A-Za-z0-9_-]+)*\.(?:pushurl|url)"
    r"|submodule(?:\.[A-Za-z0-9_-]+)+\.url"
    r"|branch\.[A-Za-z0-9_-]+\.(?:pushremote|remote)"
    r"|remote\.pushdefault|checkout\.defaultremote)[\"']?"
    r"(?![A-Za-z0-9_.-])\s*(?:[:=]\s*|\s+)"
    r"|(?<![A-Za-z0-9_.:\-])[\"']?(?:(?:remote|repository|git|ssh)\s+"
    r"|submodule(?:\s+[A-Za-z0-9_-]+)?\s+)?(?:pushurl|url)[\"']?"
    r"(?![A-Za-z0-9_.-])\s*(?:[:=]\s*)?"
    r"|(?<![A-Za-z0-9_.:\-])[\"']?(?:remote|origin|upstream|mirror)[\"']?"
    r"(?![A-Za-z0-9_.-])\s*[:=]\s*)"
    r"(?P<value>.+)$"
)
_WEAK_REMOTE_VALUE = re.compile(
    r"(?i)(?P<label>"
    r"(?<![A-Za-z0-9_.:\-\[])(?:clone|fetch|pull|push|sync)(?![A-Za-z0-9_.-])"
    r"(?:\s+(?:from|to)(?![A-Za-z0-9_.-]))?"
    r"|(?<![A-Za-z0-9_.:\-\[])remote(?!\s+url\b)(?![A-Za-z0-9_.-])"
    r"(?:\s+(?:origin|upstream)(?![A-Za-z0-9_.-]))?"
    r"|(?<![A-Za-z0-9_.:\-\[])(?:origin|upstream|mirror)(?![A-Za-z0-9_.-])"
    r"\s*(?:[:=]\s*)?)(?P<value>.+)$"
)
_GIT_REMOTE_VERBS = frozenset({"clone", "fetch", "pull", "push"})
_GIT_REMOTE_SUBCOMMANDS = frozenset({"ls-remote", "remote", "submodule"})
_GIT_GLOBAL_OPTIONS_WITH_VALUE = frozenset(
    {
        "--config-env",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
        "-c",
        "-C",
    }
)
_GIT_QUERY_OPTIONS = frozenset(
    {
        "--help",
        "--html-path",
        "--info-path",
        "--man-path",
        "--version",
        "-h",
        "-v",
    }
)
_ROOT_QUERY_OPTIONS: dict[str, frozenset[str]] = {
    "caffeinate": frozenset({"-h", "--help", "--version"}),
    "env": frozenset({"--help", "--version"}),
    "nice": frozenset({"--help", "--version"}),
    "nohup": frozenset({"--help", "--version"}),
    "sudo": frozenset({"-K", "-V", "-l", "-v", "--help", "--list", "--validate", "--version"}),
    "time": frozenset({"--help", "--version"}),
    "xargs": frozenset({"--help", "--version"}),
    "xcrun": frozenset(
        {
            "-f",
            "--find",
            "--help",
            "--show-sdk-build-version",
            "--show-sdk-path",
            "--show-sdk-platform-path",
            "--show-sdk-version",
            "--version",
        }
    ),
}
_GIT_COMMAND_MARKER = re.compile(r"\[REDACTED:remote_command:[0-9a-f]{16}\]")
_GIT_PROSE_NOUNS = frozenset({"command", "example", "invocation", "operation", "url"})
_INERT_SHELL_COMMANDS = frozenset(
    {"deno", "echo", "node", "perl", "php", "printf", "python", "python3", "ruby"}
)
_COMMAND_SHELLS = frozenset({"bash", "dash", "fish", "ksh", "sh", "zsh"})
_FILE_TRANSFER_COMMANDS = frozenset({"rsync", "scp"})
_SHELL_OPTIONS_WITH_VALUE = frozenset(
    {
        "-O",
        "-o",
        "+O",
        "+o",
        "--init-file",
        "--rcfile",
        "--startup-file",
    }
)
_CONTROL_WORDS = frozenset({"do", "elif", "else", "if", "then", "until", "while"})
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_BACKTICK_CONTENT = re.compile(r"`([^`]*)`")
_CODE_REFERENCE_LABELS = frozenset(
    {
        "api",
        "class",
        "code",
        "choice",
        "config",
        "cwd",
        "dir",
        "directory",
        "endpoint",
        "error",
        "file",
        "home",
        "id",
        "model",
        "metric",
        "module",
        "package",
        "parser",
        "path",
        "project",
        "provider",
        "pytest",
        "python",
        "repo",
        "root",
        "route",
        "request",
        "result",
        "notification",
        "status",
        "validator",
        "workspace",
    }
)
_REMOTE_CONTEXTUAL_CODE_LABELS = frozenset({"project", "repo"})
_CODE_REFERENCE_START = re.compile(r"^[\"'`‘“\[({<]*(?P<label>[A-Za-z][A-Za-z0-9._-]*):[^\s]+")
_REMOTE_ALIAS_ANYWHERE = re.compile(
    r"(?i)(?<![A-Za-z0-9._\-\[])(?P<label>[A-Za-z][A-Za-z0-9._-]*):"
    r"[^\s<>\"'`,;]+"
)
_METADATA_CONTEXT_START = re.compile(
    r"(?i)^[\"'`‘“\[({<]*(?P<context>coordinates?|(?:http\s+)?status|config|metric|"
    r"result|request|response|model|option)\s*:?\s+"
)
_REMOTE_PROSE_CONNECTOR = re.compile(
    r"(?i)^(?:is|was|were|became|changed\s+(?:from|to)|points?\s+to|set\s+to|"
    r"(?:now\s+)?uses?|using)\b"
)
_RELATIVE_REMOTE = re.compile(r"(?i)^(?:\.\.?/|[A-Za-z0-9._-]+/)[^\s<>\"'`]+")
_SCHEMELESS_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._/\-])(?:"
    r"www[.](?:[A-Za-z0-9-]+[.])+[A-Za-z0-9-]{1,63}"
    r"(?::[0-9]{1,5})?(?:[/?#][^\s<>\"'`]*)?"
    r"|(?:[A-Za-z0-9-]+[.])+[A-Za-z0-9-]{1,63}"
    r"(?:(?::[0-9]{1,5})[/?#][^\s<>\"'`]*|[/?#][^\s<>\"'`]*)"
    r")"
)
_USERINFO_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._/\-])[A-Za-z0-9._+-]+@"
    r"(?:[A-Za-z0-9-]+[.])+[A-Za-z0-9-]{1,63}"
    r"[/?#][^\s<>\"'`]*"
)
_SINGLE_HOST_USERINFO_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._/\-])[A-Za-z0-9._+-]+@"
    r"(?:localhost|[A-Za-z][A-Za-z0-9-]*)(?::[0-9]{1,5})?"
    r"[/?#][^\s<>\"'`]*"
)
_BRACKETED_HOST_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._/\-])(?:[A-Za-z0-9._+-]+@)?"
    r"\[[0-9A-F:.]+(?:%[A-Za-z0-9._~-]+)?\]"
    r"(?::[0-9]{1,5})?[/?#][^\s<>\"'`]*"
)
_SINGLE_HOST_URL = re.compile(
    r"(?i)(?<![A-Za-z0-9@._/\-])(?:"
    r"localhost(?::[0-9]{1,5})?[/?#][^\s<>\"'`]*"
    r"|[A-Za-z][A-Za-z0-9-]*:[0-9]{1,5}[/?#][^\s<>\"'`]*"
    r"|[A-Za-z][A-Za-z0-9-]*/[^\s<>\"'`?#]*[?#][^\s<>\"'`]*"
    r")"
)
_QUOTED_ABSOLUTE_PATH = re.compile(
    r"(?i)(?P<quote>[\"'])(?:~[/\\]|/|\\\\)[^\"'\r\n]*"
    r"(?P=quote)"
)
_QUOTED_WINDOWS_DRIVE_PATH = re.compile(
    r"(?i)(?P<quote>[\"'])(?P<path>[A-Z]:[^\"'\r\n]*)(?P=quote)"
)
_UNQUOTED_PATH_ATOM = r"[^\s<>\"'`,;()\[\]{}]+"
_UNC_ABSOLUTE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])(?:\\\\|//)" + _UNQUOTED_PATH_ATOM)
_WINDOWS_DRIVE_PATH = re.compile(r"(?i)(?<![A-Za-z0-9])[A-Z]:(?=\S)" + _UNQUOTED_PATH_ATOM)
_WINDOWS_DRIVE_PREFIX = re.compile(r"(?i)^[A-Z]:")
_WINDOWS_PATH_CONTEXT_PREFIX = re.compile(
    r"(?i)(?:^|\b)(?:open|read|write|inspect|check|review|edit|execute|cd|"
    r"copy|move|delete|remove|file|path|cwd|root|home|dir|"
    r"directory|repo|workspace)\s*(?:[:=]\s*)?$"
)
_WINDOWS_NON_PATH_CONTEXT_PREFIX = re.compile(
    r"(?i)\b(?:choice|config|coordinates|option|point|result|status|type|variant)\b"
    r"[^\r\n,;]{0,80}$"
)
_ORDINARY_DRIVE_VALUE = re.compile(r"(?:[A-Za-z][A-Za-z0-9-]*|[0-9]+)$")
_LABELED_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    r"(?:cwd|path|file|root|home|project|dir|directory|repo|workspace):"
    r"(?:~[/\\]|/)" + _UNQUOTED_PATH_ATOM
)
_POSIX_ABSOLUTE_PATH = re.compile(r"(?<![\w._~:-])(?:~[/\\]|/)" + _UNQUOTED_PATH_ATOM)
_API_ROUTE_PREFIX = re.compile(
    r"(?i)\b(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|CONNECT|TRACE|"
    r"call|fetch|use|route|endpoint|api(?:\s+route)?)"
    r"\s*(?:[:=]\s*)?[\"']?$"
)
_API_ROUTE_SUFFIX = re.compile(r"(?i)^[\"']?\s+(?:route|endpoint)(?:\b|$)")
_PRIVATE_LOCAL_ROOT_NAMES = (
    r"Users|home|private|var|tmp|Volumes|root|workspace|opt|etc|usr|srv|mnt|"
    r"Applications|Library"
)
_PRIVATE_POSIX_ROOTS = rf"(?:{_PRIVATE_LOCAL_ROOT_NAMES})"
_PRIVATE_POSIX_PATH = re.compile(rf"^/{_PRIVATE_POSIX_ROOTS}(?:/|$)")
_PRIVATE_LOCAL_PATH_START = re.compile(
    rf"(?<![A-Za-z0-9_-])(?:"
    rf"(?:(?i:cwd|path|file|root|home|project|dir|directory|repo|workspace):)?"
    rf"(?:~[/\\]|/{_PRIVATE_POSIX_ROOTS}(?:[/\\]|(?=$|[\s,;])))"
    r"|(?:\\\\|//)"
    r"|~[A-Za-z0-9._-]+[/\\]"
    r"|\\(?!\\)(?=[A-Za-z][A-Za-z0-9._ -]{1,}[\\/])"
    r")"
)
_GENERIC_POSIX_PATH_START = re.compile(r"(?<![\w._~:/-])/(?!/)")
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


@dataclass(frozen=True)
class _ShellToken:
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class _ShellSegment:
    tokens: tuple[_ShellToken, ...]
    end: int


class CapturePrivacyCanonicalizer:
    def __init__(self, redactor: Redactor) -> None:
        self._redactor = redactor

    def structure(self, payload: CapturePayload, project_path: Path) -> dict[str, object]:
        _validate_capture_bound(payload)
        return self._private_structure(payload, project_path)

    def stored_structure(
        self,
        payload: CapturePayload,
        project_path: Path,
    ) -> dict[str, object]:
        return self._private_structure(payload, project_path)

    def portable_structure(self, payload: CapturePayload) -> dict[str, object]:
        """Validate adapter fields without trusting or resolving a project path."""
        _validate_capture_bound(payload)
        return self._private_structure(payload, None)

    def _private_structure(
        self,
        payload: CapturePayload,
        project_path: Path | None,
    ) -> dict[str, object]:
        structure: dict[str, object] = {
            field: self.private_text(getattr(payload, field)) for field in TEXT_FIELDS
        }
        for field in LIST_FIELDS:
            values = getattr(payload, field)
            structure[field] = (
                self.changed_paths(values, project_path)
                if field == "changed_paths" and project_path is not None
                else self.private_list(values)
            )
        for field in OPTIONAL_LIST_FIELDS:
            values = self.private_list(getattr(payload, field))
            if values:
                structure[field] = values
        _validate_private_structure_bound(structure)
        return structure

    def private_text(self, value: str) -> str:
        _validate_bounded_text(value)
        result = normalize_redacted_text(self._redactor, value)
        if _CANONICAL_PRIVATE_VALUE.fullmatch(result) is not None:
            return result
        result = _GIT_URL_REWRITE.sub(_fingerprint_git_url_rewrite, result)
        result = _GIT_URL_REWRITE_VALUE.sub(_private_git_url_rewrite_value, result)
        result = _GIT_URL_REWRITE_PLACEHOLDER.sub(_finalize_git_url_rewrite, result)
        result = _GIT_URL_SECTION_REWRITE.sub(_private_strong_remote_value, result)
        result = _STRONG_REMOTE_VALUE.sub(_private_strong_remote_value, result)
        result = _redact_git_remote_commands(result)
        result = _redact_file_transfer_remotes(result)
        result = _WEAK_REMOTE_VALUE.sub(_private_weak_remote_value, result)
        result = _REMOTE_URL.sub(_fingerprint_remote_match, result)
        result = _SCP_REMOTE.sub(_fingerprint_remote_match, result)
        result = _ALIAS_SCP_REMOTE.sub(_fingerprint_remote_match, result)
        result = _ALIAS_PATH_REMOTE.sub(_fingerprint_alias_path_remote, result)
        result = _IPV6_SCP_REMOTE.sub(_fingerprint_remote_match, result)
        result = _USERINFO_URL.sub(_fingerprint_remote_match, result)
        result = _SINGLE_HOST_USERINFO_URL.sub(_fingerprint_remote_match, result)
        result = _BRACKETED_HOST_URL.sub(_fingerprint_remote_match, result)
        result = _SINGLE_HOST_URL.sub(_fingerprint_remote_match, result)
        result = _SCHEMELESS_URL.sub(_fingerprint_remote_match, result)
        source = result
        result = _QUOTED_ABSOLUTE_PATH.sub(
            lambda match: _private_path_or_api_route(source, match), source
        )
        source = result
        result = _QUOTED_WINDOWS_DRIVE_PATH.sub(
            lambda match: _private_quoted_windows_path(source, match), source
        )
        result = _redact_private_local_paths(result)
        result = _UNC_ABSOLUTE_PATH.sub(
            lambda match: _fingerprint("absolute_path", match.group(0)), result
        )
        result = _redact_windows_drive_paths(result)
        result = _LABELED_ABSOLUTE_PATH.sub(
            lambda match: _fingerprint("absolute_path", match.group(0)), result
        )
        source = result
        result = _POSIX_ABSOLUTE_PATH.sub(
            lambda match: _private_path_or_api_route(source, match), source
        )
        result = " ".join(self._redactor.redact(result).text.split())
        if strict_utf8_size(result) > MAX_FIELD_BYTES:
            result = _fingerprint("capture_field", result)
        return result

    def private_list(self, values: list[str]) -> list[str]:
        if len(values) > MAX_LIST_ITEMS:
            raise ValueError("capture list exceeds bound")
        return [self.private_text(value) for value in values]

    def changed_paths(self, values: list[str], project_path: Path) -> list[str]:
        if len(values) > MAX_LIST_ITEMS:
            raise ValueError("capture list exceeds bound")
        root = project_path.resolve(strict=True)
        safe: list[str] = []
        for value in values:
            _validate_bounded_text(value)
            prepared_value = " ".join(value.split())
            if (
                not prepared_value
                or "\x00" in prepared_value
                or _CANONICAL_PRIVATE_VALUE.fullmatch(prepared_value) is not None
            ):
                continue
            lexical = Path(prepared_value)
            portable_parts = prepared_value.replace("\\", "/").split("/")
            if (
                ".." in portable_parts
                or prepared_value.startswith(("~", "\\"))
                or _WINDOWS_DRIVE_PREFIX.match(prepared_value) is not None
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
            prepared = self._redactor.redact(relative.as_posix()).text
            _validate_bounded_text(prepared)
            safe.append(prepared)
        return safe


def _validate_bounded_text(value: object) -> None:
    if (
        not isinstance(value, str)
        or strict_utf8_size(value) > MAX_FIELD_BYTES
        or contains_unsafe_text_control(value, allow_normal_text_whitespace=True)
    ):
        raise ValueError("capture field exceeds bound")


def _validate_capture_bound(payload: CapturePayload) -> None:
    total_bytes = 0
    for field in TEXT_FIELDS:
        value = getattr(payload, field)
        _validate_bounded_text(value)
        total_bytes += strict_utf8_size(value)
    for field in ALL_LIST_FIELDS:
        values = getattr(payload, field)
        if len(values) > MAX_LIST_ITEMS:
            raise ValueError("capture list exceeds bound")
        for value in values:
            _validate_bounded_text(value)
            total_bytes += strict_utf8_size(value)
    if total_bytes > MAX_CAPTURE_BYTES:
        raise ValueError("capture payload exceeds bound")


def _validate_private_structure_bound(structure: dict[str, object]) -> None:
    document = json.dumps(
        structure,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if strict_utf8_size(document) > MAX_PRIVATE_STRUCTURE_BYTES:
        raise ValueError("capture payload exceeds bound")


def _fingerprint(category: str, value: str) -> str:
    digest = hashlib.sha256(strict_utf8_bytes(value)).hexdigest()[:16]
    return f"[REDACTED:{category}:{digest}]"


def _fingerprint_remote_match(match: re.Match[str]) -> str:
    return _fingerprint_remote_token(match.group(0))


def _fingerprint_alias_path_remote(match: re.Match[str]) -> str:
    if not _is_alias_path_remote_candidate(match):
        return match.group(0)
    return _fingerprint_remote_token(match.group(0))


def _fingerprint_git_url_rewrite(match: re.Match[str]) -> str:
    digest = hashlib.sha256(strict_utf8_bytes(match.group(0))).hexdigest()[:16]
    return f"[PMH_GIT_URL_REWRITE:{digest}]"


def _private_git_url_rewrite_value(match: re.Match[str]) -> str:
    value = match.group("value")
    if not _looks_like_remote_value(value):
        return match.group(0)
    return match.group("label") + _fingerprint_remote_token(value)


def _finalize_git_url_rewrite(match: re.Match[str]) -> str:
    return f"[REDACTED:remote:{match.group('digest')}]"


def _fingerprint_remote_token(token: str) -> str:
    core = token.rstrip(")]}.!?:")
    if not core:
        return _fingerprint("remote", token)
    return _fingerprint("remote", core) + token[len(core) :]


def _private_strong_remote_value(match: re.Match[str]) -> str:
    value = match.group("value")
    stripped = value.strip()
    label = match.group("label")
    if not label.rstrip().endswith((":", "=")):
        scan = _REMOTE_PROSE_CONNECTOR.match(stripped) is not None
        if not _looks_like_remote_value(stripped, scan=scan):
            return match.group(0)
    if _CANONICAL_PRIVATE_VALUE.fullmatch(stripped) is not None:
        return match.group(0)
    leading = value[: len(value) - len(value.lstrip())]
    trailing = value[len(value.rstrip()) :]
    return label + leading + _fingerprint("remote", stripped) + trailing


def _looks_like_remote_value(value: str, *, scan: bool = False) -> bool:
    candidate = value.lstrip("\"'`‘“({<")
    canonical_prefix = _CANONICAL_PRIVATE_VALUE.match(candidate)
    if canonical_prefix is not None:
        candidate = candidate[canonical_prefix.end() :].lstrip(")]}>.!?: ")
        if not candidate:
            return False
    else:
        candidate = candidate.lstrip("[")
    candidate = _CANONICAL_PRIVATE_VALUE.sub(" ", candidate).strip()
    parts = candidate.split(maxsplit=1)
    first = parts[0] if parts else ""
    if (
        _REMOTE_URL.search(candidate) is not None
        or _SCP_REMOTE.search(candidate) is not None
        or _ALIAS_SCP_REMOTE.search(candidate) is not None
    ):
        return True
    if any(
        _is_alias_path_remote_candidate(remote) for remote in _ALIAS_PATH_REMOTE.finditer(candidate)
    ):
        return True
    remote_candidates = (
        list(_REMOTE_ALIAS_ANYWHERE.finditer(candidate))
        if scan
        else list(_REMOTE_ALIAS_ANYWHERE.finditer(first))
    )
    if any(_is_remote_alias_candidate(candidate, remote) for remote in remote_candidates):
        return True
    relative_candidates = candidate.split() if scan else [first]
    return any(_looks_like_relative_remote(token) for token in relative_candidates)


def _looks_like_relative_remote(value: str) -> bool:
    candidate = value.lstrip("\"'`‘“[({<").rstrip(")]}>.!?:")
    return _RELATIVE_REMOTE.match(candidate) is not None and not _path_ends_with_file_extension(
        candidate
    )


def _private_weak_remote_value(match: re.Match[str]) -> str:
    value = match.group("value").strip()
    if (
        _has_git_context(match.string[: match.start()])
        and _GIT_COMMAND_MARKER.match(value) is not None
    ):
        return match.group(0)
    analysis_value = _CANONICAL_PRIVATE_VALUE.sub("PRIVATE_MARKER", value)
    if _CANONICAL_PRIVATE_VALUE.fullmatch(value) is not None:
        private_value = value
    elif (
        _is_code_reference_value(analysis_value)
        and not _is_contextual_remote_code_reference(analysis_value)
    ) or not _looks_like_remote_value(analysis_value, scan=True):
        return match.group(0)
    else:
        private_value = _fingerprint("remote", value)
    return match.group("label").rstrip() + " " + private_value


def _is_code_reference_value(value: str) -> bool:
    reference = _CODE_REFERENCE_START.match(value)
    if reference is None or reference.group("label").casefold() not in _CODE_REFERENCE_LABELS:
        return False
    tail = value[reference.end() :]
    return ":" not in tail and "@" not in tail


def _is_contextual_remote_code_reference(value: str) -> bool:
    reference = _CODE_REFERENCE_START.match(value)
    return (
        reference is not None
        and reference.group("label").casefold() in _REMOTE_CONTEXTUAL_CODE_LABELS
    )


def _is_remote_alias_candidate(candidate: str, remote: re.Match[str]) -> bool:
    label = remote.group("label").casefold()
    if label in _REMOTE_CONTEXTUAL_CODE_LABELS:
        return True
    if label in _CODE_REFERENCE_LABELS:
        return False
    if remote.start() == 0:
        return True
    context_match = _METADATA_CONTEXT_START.match(candidate)
    if context_match is None:
        return True
    context = context_match.group("context").casefold()
    if context.startswith("coordinate"):
        return label not in {"x", "y", "z"}
    if context.endswith("status"):
        return not (len(label) == 1 and label.isalpha())
    if context in {"config", "option"}:
        return not (len(label) == 1 and label.isalpha())
    if context == "metric":
        return re.fullmatch(r"p[0-9]{1,3}", label) is None
    if context == "result":
        return label != "code"
    if context == "request":
        return label != "id"
    if context == "response":
        return label != "code"
    if context == "model":
        return label != "provider"
    return True


def _is_alias_path_remote_candidate(remote: re.Match[str]) -> bool:
    raw_label = remote.group("label")
    label = raw_label.casefold()
    if raw_label != label or len(label) == 1:
        return False
    return label not in _CODE_REFERENCE_LABELS or label in _REMOTE_CONTEXTUAL_CODE_LABELS


def _has_git_context(value: str) -> bool:
    segments = _shellish_segments(value)
    return bool(
        segments and any(_git_token(token.value)[0] == "git" for token in segments[-1].tokens)
    )


def _redact_git_remote_commands(value: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    for segment in _shellish_segments(value):
        command = _segment_git_remote_command(value, segment)
        if command is None:
            continue
        verb, command_start, command_end = command
        replacements.append(
            (
                command_start,
                command_end,
                f"git {verb} {_fingerprint('remote_command', value[command_start:command_end])}",
            )
        )

    if not replacements:
        return value
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        pieces.append(value[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _redact_file_transfer_remotes(value: str) -> str:
    replacements: list[tuple[int, int, str]] = []
    for segment in _shellish_segments(value):
        transfer_indexes = [
            index
            for index, token in enumerate(segment.tokens)
            if _executable_name(token.value) in _FILE_TRANSFER_COMMANDS
        ]
        for transfer_index in transfer_indexes:
            for token in segment.tokens[transfer_index + 1 :]:
                if _is_code_reference_value(
                    token.value
                ) and not _is_contextual_remote_code_reference(token.value):
                    continue
                if _looks_like_file_transfer_remote(token.value):
                    replacements.append(
                        (token.start, token.end, _fingerprint_remote_token(token.value))
                    )

    if not replacements:
        return value
    replacements.sort()
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in replacements:
        if start < cursor:
            continue
        pieces.append(value[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _looks_like_file_transfer_remote(value: str) -> bool:
    candidate = value.lstrip("\"'`‘“({<")
    if candidate.startswith(("./", "../", "/", "~", "\\")):
        return False
    return _looks_like_remote_value(candidate)


def _segment_git_remote_command(
    source: str,
    segment: _ShellSegment,
    *,
    depth: int = 0,
) -> tuple[str, int, int] | None:
    if depth > 8:
        return None
    tokens = segment.tokens
    shell_command = _nested_shell_command(source, tokens, depth)
    if shell_command is not None:
        return shell_command

    root_index, authoritative_root = _execution_root(tokens)
    prose_git_root = _is_prose_git_root(tokens, root_index)
    for git_index in range(len(tokens)):
        command = _git_remote_command_at(tokens, git_index)
        if command is None:
            continue
        verb, verb_index, nested = command
        if not _command_has_private_operand(tokens, verb_index):
            continue
        if (
            prose_git_root
            and git_index != root_index
            and not _tail_has_private_remote_shape(
                [token.value for token in tokens],
                verb_index + 1,
            )
        ):
            continue
        if nested or git_index == root_index or not authoritative_root or prose_git_root:
            return verb, tokens[git_index].start, tokens[-1].end
    return None


def _nested_shell_command(
    source: str,
    tokens: tuple[_ShellToken, ...],
    depth: int,
) -> tuple[str, int, int] | None:
    split_command = _env_split_command(tokens)
    if split_command is not None:
        command_value, command_start = split_command
        if verb := _git_remote_verb_in_text(command_value, depth + 1):
            return verb, command_start, tokens[-1].end
    for token in tokens:
        raw = source[token.start : token.end]
        executable_raw = _without_literal_single_quotes(raw)
        nested_values = [match.group(1) for match in _BACKTICK_CONTENT.finditer(executable_raw)]
        nested_values.extend(_command_substitution_contents(executable_raw))
        for nested_value in nested_values:
            if verb := _git_remote_verb_in_text(nested_value, depth + 1):
                return verb, token.start, token.end

    root_index, _authoritative = _execution_root(tokens)
    if root_index is None:
        return None
    executable = _executable_name(tokens[root_index].value)
    if executable == "eval":
        nested_value = " ".join(token.value for token in tokens[root_index + 1 :])
        if verb := _git_remote_verb_in_text(nested_value, depth + 1):
            return verb, tokens[root_index].start, tokens[-1].end
        return None
    if executable not in _COMMAND_SHELLS:
        return None
    command_index = _shell_command_argument(tokens, root_index, executable)
    if command_index is None:
        return None
    if verb := _git_remote_verb_in_text(tokens[command_index].value, depth + 1):
        return verb, tokens[root_index].start, tokens[-1].end
    trailing_arguments = " ".join(token.value for token in tokens[command_index + 1 :])
    if verb := _git_remote_verb_in_text(trailing_arguments, depth + 1):
        return verb, tokens[root_index].start, tokens[-1].end
    return None


def _env_split_command(tokens: tuple[_ShellToken, ...]) -> tuple[str, int] | None:
    if not tokens or _executable_name(tokens[0].value) != "env":
        return None
    for index in range(1, len(tokens)):
        option, separator, option_value = tokens[index].value.partition("=")
        if option not in {"-S", "--split-string"}:
            continue
        if separator:
            trailing = " ".join(token.value for token in tokens[index + 1 :])
            return " ".join(part for part in (option_value, trailing) if part), tokens[0].start
        command_index = index + 1
        if command_index < len(tokens):
            return " ".join(token.value for token in tokens[command_index:]), tokens[0].start
        return None
    return None


def _without_literal_single_quotes(value: str) -> str:
    result: list[str] = []
    quote_close = ""
    index = 0
    while index < len(value):
        character = value[index]
        if quote_close:
            result.append(" ")
            if character == quote_close:
                quote_close = ""
            index += 1
            continue
        if character == "\\" and value[index + 1 : index + 2] in {"$", "`"}:
            result.extend((" ", " "))
            index += 2
            continue
        if character in {"'", "‘"}:
            quote_close = "'" if character == "'" else "’"
            result.append(" ")
            index += 1
            continue
        result.append(character)
        index += 1
    return "".join(result)


def _git_remote_verb_in_text(value: str, depth: int) -> str | None:
    for nested_segment in _shellish_segments(value):
        command = _segment_git_remote_command(value, nested_segment, depth=depth)
        if command is not None:
            return command[0]
    return None


def _command_substitution_contents(value: str) -> list[str]:
    contents: list[str] = []
    cursor = 0
    while True:
        start = value.find("$(", cursor)
        if start < 0:
            return contents
        index = start + 2
        depth = 1
        while index < len(value) and depth:
            if value.startswith("$(", index):
                depth += 1
                index += 2
                continue
            if value[index] == ")":
                depth -= 1
                if depth == 0:
                    contents.append(value[start + 2 : index])
                    break
            index += 1
        if depth:
            contents.append(value[start + 2 :])
            return contents
        cursor = index + 1


def _command_has_private_operand(tokens: tuple[_ShellToken, ...], verb_index: int) -> bool:
    arguments = tokens[verb_index + 1 :]
    return not (
        len(arguments) == 1 and _GIT_COMMAND_MARKER.fullmatch(arguments[0].value) is not None
    )


def _git_remote_command_at(
    tokens: tuple[_ShellToken, ...],
    git_index: int,
) -> tuple[str, int, bool] | None:
    executable, nested = _git_token(tokens[git_index].value)
    if executable != "git":
        return None
    values = [token.value for token in tokens]
    if _has_leading_query_option(
        values,
        git_index + 1,
        query_options=_GIT_QUERY_OPTIONS,
        value_options=_GIT_GLOBAL_OPTIONS_WITH_VALUE,
    ) and not _tail_has_private_remote_shape(values, git_index + 1):
        return None
    consumes_next = False
    for index in range(git_index + 1, len(tokens)):
        argument = tokens[index].value
        lowered = argument.casefold()
        if consumes_next:
            consumes_next = False
            continue
        if lowered in _GIT_REMOTE_VERBS or lowered in _GIT_REMOTE_SUBCOMMANDS:
            return lowered, index, nested
        if lowered == "archive" and any(
            candidate.value.casefold() == "--remote"
            or candidate.value.casefold().startswith("--remote=")
            for candidate in tokens[index + 1 :]
        ):
            return lowered, index, nested
        if argument.startswith("-"):
            option, separator, _value = argument.partition("=")
            consumes_next = not separator and option in _GIT_GLOBAL_OPTIONS_WITH_VALUE
            continue
        if "$" in argument or any(character.isspace() for character in argument):
            return "remote", index, nested
        return None
    return None


def _is_prose_git_root(
    tokens: tuple[_ShellToken, ...],
    root_index: int | None,
) -> bool:
    if root_index is None or _executable_name(tokens[root_index].value) != "git":
        return False
    consumes_next = False
    for token in tokens[root_index + 1 :]:
        argument = token.value
        if consumes_next:
            consumes_next = False
            continue
        if argument.startswith("-"):
            option, separator, _value = argument.partition("=")
            consumes_next = not separator and option in _GIT_GLOBAL_OPTIONS_WITH_VALUE
            continue
        return argument.casefold().rstrip(":") in _GIT_PROSE_NOUNS
    return False


def _execution_root(tokens: tuple[_ShellToken, ...]) -> tuple[int | None, bool]:
    if not tokens:
        return None, False
    index = 0
    values = [token.value for token in tokens]
    while index < len(values):
        value = values[index]
        if value in {"$", "!", "(", "{", "-"} or value.casefold() in _CONTROL_WORDS:
            index += 1
            continue
        if _ASSIGNMENT.match(value) is not None:
            index += 1
            continue
        break
    if index >= len(values):
        return None, False

    used_wrapper = False
    while index < len(values):
        executable = _executable_name(values[index])
        query_options = _ROOT_QUERY_OPTIONS.get(executable)
        if (
            query_options is not None
            and _has_leading_query_option(
                values,
                index + 1,
                query_options=query_options,
                value_options=_wrapper_value_options(executable),
            )
            and not _tail_has_private_remote_shape(values, index + 1)
        ):
            return index, True
        if executable == "env":
            used_wrapper = True
            index = _skip_wrapper_options(
                values,
                index + 1,
                value_options={"-C", "-S", "-u", "--argv0", "--chdir", "--split-string", "--unset"},
                assignments=True,
            )
            continue
        if executable == "sudo":
            used_wrapper = True
            index = _skip_wrapper_options(
                values,
                index + 1,
                value_options={
                    "-C",
                    "-R",
                    "-T",
                    "-g",
                    "-h",
                    "-p",
                    "-r",
                    "-t",
                    "-u",
                    "--chroot",
                    "--close-from",
                    "--command-timeout",
                    "--group",
                    "--host",
                    "--prompt",
                    "--role",
                    "--type",
                    "--user",
                },
            )
            continue
        if (
            executable == "command"
            and _has_leading_query_option(
                values,
                index + 1,
                query_options=frozenset({"-V", "-v", "--help"}),
                value_options=frozenset(),
            )
            and not _tail_has_private_remote_shape(values, index + 1)
        ):
            return index, True
        wrapper_options: dict[str, set[str]] = {
            "command": set(),
            "exec": {"-a"},
            "nohup": set(),
            "time": {"-f", "-o", "--format", "--output"},
            "xcrun": {"--sdk", "--toolchain"},
        }
        if executable in wrapper_options:
            used_wrapper = True
            index = _skip_wrapper_options(
                values,
                index + 1,
                value_options=wrapper_options[executable],
            )
            continue
        authoritative = used_wrapper or executable in {
            "git",
            *_COMMAND_SHELLS,
            *_INERT_SHELL_COMMANDS,
        }
        return index, authoritative
    return None, used_wrapper


def _wrapper_value_options(executable: str) -> frozenset[str]:
    if executable == "env":
        return frozenset({"-C", "-S", "-u", "--argv0", "--chdir", "--split-string", "--unset"})
    if executable == "sudo":
        return frozenset(
            {
                "-C",
                "-R",
                "-T",
                "-g",
                "-h",
                "-p",
                "-r",
                "-t",
                "-u",
                "--chroot",
                "--close-from",
                "--command-timeout",
                "--group",
                "--host",
                "--prompt",
                "--role",
                "--type",
                "--user",
            }
        )
    if executable == "time":
        return frozenset({"-f", "-o", "--format", "--output"})
    if executable == "xcrun":
        return frozenset({"--sdk", "--toolchain"})
    return frozenset()


def _has_leading_query_option(
    values: list[str],
    index: int,
    *,
    query_options: frozenset[str],
    value_options: frozenset[str],
) -> bool:
    consumes_next = False
    while index < len(values):
        value = values[index]
        if consumes_next:
            consumes_next = False
            index += 1
            continue
        if value == "--" or not value.startswith("-") or value == "-":
            return False
        option, separator, _option_value = value.partition("=")
        if option in query_options:
            return True
        consumes_next = not separator and option in value_options
        index += 1
    return False


def _tail_has_private_remote_shape(values: list[str], index: int) -> bool:
    for value in values[index:]:
        if _is_code_reference_value(value) and not _is_contextual_remote_code_reference(value):
            continue
        if _looks_like_remote_value(value):
            return True
    return False


def _skip_wrapper_options(
    values: list[str],
    index: int,
    *,
    value_options: set[str],
    assignments: bool = False,
) -> int:
    consumes_next = False
    while index < len(values):
        value = values[index]
        if consumes_next:
            consumes_next = False
            index += 1
            continue
        if value == "--":
            return index + 1
        if assignments and _ASSIGNMENT.match(value) is not None:
            index += 1
            continue
        if not value.startswith("-") or value == "-":
            return index
        option, separator, _option_value = value.partition("=")
        consumes_next = not separator and option in value_options
        index += 1
    return index


def _shell_command_argument(
    tokens: tuple[_ShellToken, ...],
    shell_index: int,
    executable: str,
) -> int | None:
    consumes_next = False
    for index in range(shell_index + 1, len(tokens)):
        option = tokens[index].value
        if consumes_next:
            consumes_next = False
            continue
        if not option.startswith(("-", "+")) or option in {"-", "+"}:
            return None
        if executable == "fish" and option in {"-C", "--init-command"}:
            command_index = index + 1
            return command_index if command_index < len(tokens) else None
        if option.startswith("-") and not option.startswith("--") and "c" in option[1:]:
            command_index = index + 1
            return command_index if command_index < len(tokens) else None
        name, separator, _value = option.partition("=")
        consumes_next = not separator and name in _SHELL_OPTIONS_WITH_VALUE
    return None


def _git_token(value: str) -> tuple[str, bool]:
    stripped = value.lstrip("$!([{`")
    nested = stripped != value and "$(" in value[: len(value) - len(stripped) + 1]
    return _executable_name(stripped), nested


def _executable_name(value: str) -> str:
    return value.strip().rstrip(")]}`").rsplit("/", 1)[-1].casefold()


def _shellish_segments(value: str) -> list[_ShellSegment]:
    segments: list[_ShellSegment] = []
    tokens: list[_ShellToken] = []
    current: list[str] = []
    token_start: int | None = None
    quote_close = ""
    substitution_depth = 0
    quote_pairs = {"'": "'", '"': '"', "`": "`", "‘": "’", "“": "”"}

    def finish_token(end: int) -> None:
        nonlocal token_start
        if token_start is not None:
            tokens.append(_ShellToken("".join(current), token_start, end))
            current.clear()
            token_start = None

    def finish_segment(end: int) -> None:
        finish_token(end)
        if tokens:
            segments.append(_ShellSegment(tuple(tokens), end))
            tokens.clear()

    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and quote_close != "'":
            newline_size = 0
            if value[index + 1 : index + 3] == "\r\n":
                newline_size = 2
            elif value[index + 1 : index + 2] == "\n":
                newline_size = 1
            if newline_size:
                index += 1 + newline_size
                continue
            if index + 1 < len(value):
                if token_start is None:
                    token_start = index
                current.append(value[index + 1])
                index += 2
                continue
        if quote_close:
            if character == quote_close:
                quote_close = ""
            else:
                current.append(character)
            index += 1
            continue
        if value.startswith("$(", index):
            if token_start is None:
                token_start = index
            current.append("$(")
            substitution_depth += 1
            index += 2
            continue
        if character in quote_pairs:
            if token_start is None:
                token_start = index
            quote_close = quote_pairs[character]
            index += 1
            continue
        if substitution_depth:
            if character == "(":
                substitution_depth += 1
            elif character == ")":
                substitution_depth -= 1
            current.append(character)
            index += 1
            continue
        if character in "\r\n;|" or (
            character == "&"
            and not (current and current[-1] in "<>")
            and value[index + 1 : index + 2] != ">"
        ):
            finish_segment(index)
            index += 1
            continue
        if character.isspace():
            finish_token(index)
            index += 1
            continue
        if token_start is None:
            token_start = index
        current.append(character)
        index += 1

    finish_segment(len(value))
    return segments


def _private_quoted_windows_path(source: str, match: re.Match[str]) -> str:
    path = match.group("path")
    prefix = source[max(0, match.start() - 80) : match.start()]
    if _looks_like_windows_drive_path(path, prefix):
        return _fingerprint("absolute_path", match.group(0))
    return match.group(0)


def _looks_like_windows_drive_path(path: str, prefix: str) -> bool:
    suffix = path[2:]
    ordinary_suffix = suffix.rstrip(".!?:")
    if (
        any(separator in suffix for separator in ("/", "\\"))
        or suffix.startswith(".")
        or any(character.isspace() for character in suffix)
        or "_" in suffix
        or _path_ends_with_file_extension(path)
        or _WINDOWS_PATH_CONTEXT_PREFIX.search(prefix) is not None
    ):
        return True
    return not (
        _WINDOWS_NON_PATH_CONTEXT_PREFIX.search(prefix) is not None
        and _ORDINARY_DRIVE_VALUE.fullmatch(ordinary_suffix) is not None
    )


def _redact_windows_drive_paths(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    while match := _WINDOWS_DRIVE_PATH.search(value, cursor):
        prefix = value[max(0, match.start() - 80) : match.start()]
        if not _looks_like_windows_drive_path(match.group(0), prefix):
            pieces.append(value[cursor : match.end()])
            cursor = match.end()
            continue
        end = _private_local_path_end(value, match.start(), match.end())
        pieces.append(value[cursor : match.start()])
        pieces.append(_fingerprint("absolute_path", value[match.start() : end]))
        cursor = end
    pieces.append(value[cursor:])
    return "".join(pieces)


def _redact_private_local_paths(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    while match := _PRIVATE_LOCAL_PATH_START.search(value, cursor):
        end = _private_local_path_end(value, match.start(), match.end())
        pieces.append(value[cursor : match.start()])
        pieces.append(_fingerprint("absolute_path", value[match.start() : end]))
        cursor = end
    pieces.append(value[cursor:])
    return _redact_generic_posix_paths("".join(pieces))


def _redact_generic_posix_paths(value: str) -> str:
    pieces: list[str] = []
    cursor = 0
    while match := _GENERIC_POSIX_PATH_START.search(value, cursor):
        token = _POSIX_ABSOLUTE_PATH.match(value, match.start())
        if token is None:
            pieces.append(value[cursor : match.end()])
            cursor = match.end()
            continue
        if _is_api_route(value, token.start(), token.end(), token.group(0)):
            pieces.append(value[cursor : token.end()])
            cursor = token.end()
            continue
        end = _private_local_path_end(value, match.start(), token.end())
        pieces.append(value[cursor : match.start()])
        pieces.append(_fingerprint("absolute_path", value[match.start() : end]))
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
    if not candidate or _PATH_PROSE_PREFIX.search(candidate):
        return False
    return _path_ends_with_file_extension(candidate)


def _private_path_or_api_route(source: str, match: re.Match[str]) -> str:
    value = match.group(0)
    candidate = value
    if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
        candidate = value[1:-1]
    if candidate.startswith("/") and not candidate.startswith("//"):
        if _PRIVATE_POSIX_PATH.search(candidate):
            return _fingerprint("absolute_path", value)
        if any(character.isspace() for character in candidate):
            return _fingerprint("absolute_path", value)
        if _is_api_route(source, match.start(), match.end(), candidate):
            return value
    return _fingerprint("absolute_path", value)


def _is_api_route(source: str, start: int, end: int, candidate: str) -> bool:
    prefix = source[max(0, start - 80) : start]
    suffix = source[end : end + 40]
    return (
        _API_ROUTE_PREFIX.search(prefix) is not None or _API_ROUTE_SUFFIX.search(suffix) is not None
    )
