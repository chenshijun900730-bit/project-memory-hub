import re
from pathlib import Path
from typing import Final, Match

from project_memory_hub.domain import RedactionResult


class SensitivePathError(ValueError):
    """Raised when a lexical path contains a sensitive component."""


_FINDING_ORDER: Final = (
    "api_key",
    "bearer_token",
    "private_key",
    "password",
    "sensitive_path",
    "input_truncated",
)

_PRIVATE_KEY_BEGIN_PATTERN: Final = re.compile(
    r"-----BEGIN (?P<kind>(?:RSA |DSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY)-----"
)

_API_KEY_PATTERN: Final = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{16,}"
    r"|sk-ant-[A-Za-z0-9_-]{16,}"
    r"|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|xox[a-z]?-[A-Za-z0-9-]{10,}"
    r"|AIza[A-Za-z0-9_-]{20,}"
    r"|(?:AKIA|ASIA)[A-Z0-9]{16}"
    r")(?![A-Za-z0-9_-])"
)

_BEARER_PATTERN: Final = re.compile(r"\bBearer[ \t]+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)

_PASSWORD_ASSIGNMENT_PATTERN: Final = re.compile(
    r"(?P<prefix>[\"']?\b(?:password|passwd|pwd|client_secret)\b[\"']?"
    r"[ \t]*(?:=|:)[ \t]*)",
    re.IGNORECASE,
)

_STABLE_LABEL_FRAGMENT: Final = (
    r"\[(?:"
    r"REDACTED:(?:api_key|bearer_token|private_key|password|sensitive_path)"
    r"|TRUNCATED:redaction_input"
    r")\]"
)
_PATH_TOKEN_PATTERN: Final = re.compile(
    _STABLE_LABEL_FRAGMENT + r"|[^\s\"'<>|,;()\[\]{}=:]{1,4096}",
    re.IGNORECASE,
)
_ENV_COMPONENT_PATTERN: Final = re.compile(r"^\.env(?:$|rc(?:$|[._-])|[._-])", re.IGNORECASE)
_TOKEN_COMPONENT_PATTERN: Final = re.compile(
    r"(?:^|[._-])(?:access[._-]?)?token(?:$|[._-])", re.IGNORECASE
)
_SECRET_COMPONENT_PATTERN: Final = re.compile(
    r"(?:^|[._-])(?:credential|credentials|secret|secrets)(?:$|[._-])",
    re.IGNORECASE,
)
_FILENAME_EXTENSION_PATTERN: Final = re.compile(r"\.[A-Za-z0-9]{1,16}$")
_SENSITIVE_EXTENSIONS: Final = (".pem", ".key", ".p12", ".pfx")
_PRIVATE_KEY_NAMES: Final = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"})
_CREDENTIAL_ASSIGNMENT_KEYS: Final = frozenset({"password", "passwd", "pwd", "client_secret"})
_STABLE_LABEL_PATTERN: Final = re.compile(
    r"\[(?:"
    r"(?P<redacted>REDACTED):"
    r"(?P<redacted_category>api_key|bearer_token|private_key|password|sensitive_path)"
    r"|(?P<truncated>TRUNCATED):(?P<truncated_category>redaction_input)"
    r")\]"
)
_REDACTED_LABEL_CATEGORIES: Final = frozenset(
    {"api_key", "bearer_token", "private_key", "password", "sensitive_path"}
)
_TRUNCATION_MARKER: Final = "[TRUNCATED:redaction_input]"
_BARE_PASSWORD_TERMINATORS: Final = frozenset(" \t\r\n,;}]")
_TRAILING_PROSE_PUNCTUATION: Final = frozenset(".!?")


def _component_is_hard_sensitive(component: str) -> bool:
    lowered = component.casefold()
    if _ENV_COMPONENT_PATTERN.search(lowered):
        return True
    if lowered.endswith(_SENSITIVE_EXTENSIONS):
        return True
    if lowered in _PRIVATE_KEY_NAMES or lowered == ".ssh":
        return True
    return False


def _component_has_generic_sensitive_name(component: str) -> bool:
    return (
        _SECRET_COMPONENT_PATTERN.search(component) is not None
        or _TOKEN_COMPONENT_PATTERN.search(component) is not None
    )


def _component_looks_like_filename(component: str) -> bool:
    return (
        "_" in component
        or "-" in component
        or _FILENAME_EXTENSION_PATTERN.search(component) is not None
    )


def _component_is_sensitive(component: str) -> bool:
    return _component_is_hard_sensitive(component) or _component_has_generic_sensitive_name(
        component
    )


def _token_is_sensitive_path(token: str) -> bool:
    normalized = token.replace("\\", "/")
    components = tuple(component for component in normalized.split("/") if component)
    if any(_component_is_hard_sensitive(component) for component in components):
        return True

    has_path_separator = "/" in normalized
    return any(
        _component_has_generic_sensitive_name(component)
        and (has_path_separator or _component_looks_like_filename(component))
        for component in components
    )


def _redact_private_key_blocks(text: str) -> tuple[str, bool]:
    output: list[str] = []
    cursor = 0
    found = False
    while match := _PRIVATE_KEY_BEGIN_PATTERN.search(text, cursor):
        output.append(text[cursor : match.start()])
        output.append("[REDACTED:private_key]")
        found = True
        end_marker = f"-----END {match.group('kind')}-----"
        end_start = text.find(end_marker, match.end())
        if end_start < 0:
            cursor = len(text)
            break
        cursor = end_start + len(end_marker)
    output.append(text[cursor:])
    return "".join(output), found


def _existing_findings(text: str) -> set[str]:
    findings: set[str] = set()
    for match in _STABLE_LABEL_PATTERN.finditer(text):
        category = match.group("redacted_category")
        if category in _REDACTED_LABEL_CATEGORIES:
            findings.add(category)
        elif match.group("truncated_category") == "redaction_input":
            findings.add("input_truncated")
    return findings


def _bound_redaction_input(text: str, max_raw_chars: int) -> tuple[str, bool]:
    had_terminal_marker = text.endswith(_TRUNCATION_MARKER)
    text_end = len(text) - len(_TRUNCATION_MARKER) if had_terminal_marker else len(text)

    output: list[str] = []
    cursor = 0
    remaining_budget = max_raw_chars
    while cursor < text_end and remaining_budget > 0:
        label = _STABLE_LABEL_PATTERN.match(text, cursor, text_end)
        if label is not None:
            output.append(label.group(0))
            cursor = label.end()
            remaining_budget -= 1
            continue

        window_end = min(text_end, cursor + remaining_budget)
        next_open_bracket = text.find("[", cursor, window_end)
        if next_open_bracket < 0:
            next_open_bracket = window_end
        if next_open_bracket == cursor:
            output.append(text[cursor])
            cursor += 1
            remaining_budget -= 1
            continue

        output.append(text[cursor:next_open_bracket])
        remaining_budget -= next_open_bracket - cursor
        cursor = next_open_bracket

    return "".join(output), had_terminal_marker or cursor < text_end


def _find_unescaped_quote(text: str, start: int, quote: str) -> int:
    backslash_run = 0
    for position in range(start, len(text)):
        character = text[position]
        if character == "\\":
            backslash_run += 1
            continue
        if character == quote and backslash_run % 2 == 0:
            return position
        backslash_run = 0
    return -1


def _redact_password_assignments(text: str) -> tuple[str, bool]:
    output: list[str] = []
    cursor = 0
    found = False
    while match := _PASSWORD_ASSIGNMENT_PATTERN.search(text, cursor):
        output.append(text[cursor : match.start()])
        prefix = match.group("prefix")
        value_start = match.end()
        if value_start >= len(text):
            output.append(prefix)
            cursor = value_start
            break

        quote = text[value_start] if text[value_start] in {"'", '"'} else ""
        if quote:
            value_start += 1
            value_end = _find_unescaped_quote(text, value_start, quote)
            if value_end < 0:
                output.append(prefix + quote + "[REDACTED:password]" + quote)
                found = True
                cursor = len(text)
                break
            value = text[value_start:value_end]
            if _STABLE_LABEL_PATTERN.fullmatch(value):
                output.append(prefix + quote + value + quote)
            else:
                output.append(prefix + quote + "[REDACTED:password]" + quote)
                found = True
            cursor = value_end + 1
            continue

        label = _STABLE_LABEL_PATTERN.match(text, value_start)
        if label is not None and (
            label.end() == len(text) or text[label.end()] in _BARE_PASSWORD_TERMINATORS
        ):
            output.append(prefix + label.group(0))
            cursor = label.end()
            continue

        value_end = label.end() if label is not None else value_start
        while value_end < len(text) and text[value_end] not in _BARE_PASSWORD_TERMINATORS:
            value_end += 1
        if value_end == value_start:
            output.append(prefix)
            cursor = value_start
            continue

        output.append(prefix + "[REDACTED:password]")
        found = True
        cursor = value_end

    output.append(text[cursor:])
    return "".join(output), found


def _split_trailing_prose_punctuation(token: str) -> tuple[str, str]:
    core_end = len(token)
    while core_end > 0 and token[core_end - 1] in _TRAILING_PROSE_PUNCTUATION:
        core_end -= 1
    return token[:core_end], token[core_end:]


class Redactor:
    def __init__(self, max_input_chars: int = 1_000_000) -> None:
        if type(max_input_chars) is not int or max_input_chars <= 0:
            raise ValueError("max_input_chars must be a positive integer")
        self._max_input_chars = max_input_chars

    def redact(self, text: str) -> RedactionResult:
        if not isinstance(text, str):
            raise TypeError("redaction input must be text")

        redacted, truncated = _bound_redaction_input(text, self._max_input_chars)
        findings = _existing_findings(redacted)

        redacted, private_key_found = _redact_private_key_blocks(redacted)
        if private_key_found:
            findings.add("private_key")

        redacted, count = _API_KEY_PATTERN.subn("[REDACTED:api_key]", redacted)
        if count:
            findings.add("api_key")

        redacted, count = _BEARER_PATTERN.subn("[REDACTED:bearer_token]", redacted)
        if count:
            findings.add("bearer_token")

        redacted, password_found = _redact_password_assignments(redacted)
        if password_found:
            findings.add("password")

        def replace_sensitive_path(match: Match[str]) -> str:
            token = match.group(0)
            if token.startswith("["):
                return token
            core, punctuation = _split_trailing_prose_punctuation(token)
            if core.casefold() in _CREDENTIAL_ASSIGNMENT_KEYS and re.match(
                r"[ \t]*(?:=|:)", match.string[match.end() :]
            ):
                return token
            if not _token_is_sensitive_path(core):
                return token
            findings.add("sensitive_path")
            return "[REDACTED:sensitive_path]" + punctuation

        redacted = _PATH_TOKEN_PATTERN.sub(replace_sensitive_path, redacted)

        if truncated:
            redacted += _TRUNCATION_MARKER
            findings.add("input_truncated")

        return RedactionResult(
            text=redacted,
            findings=tuple(category for category in _FINDING_ORDER if category in findings),
        )

    def assert_safe_path(self, path: Path) -> None:
        if not isinstance(path, Path):
            raise TypeError("path must be a pathlib Path")
        if any(_component_is_sensitive(component) for component in path.parts):
            raise SensitivePathError("sensitive path rejected: sensitive_path")


def normalize_redacted_text(redactor: Redactor, value: str) -> str:
    """Normalize and redact one structured field without crossing field boundaries."""
    normalized = _normalize_whitespace(value)
    return _normalize_whitespace(redactor.redact(normalized).text)


def _normalize_whitespace(value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("structured capture values must be text")
    return " ".join(value.split())
