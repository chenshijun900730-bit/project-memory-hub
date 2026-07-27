from __future__ import annotations

import re

from project_memory_hub.security.redaction import Redactor
from project_memory_hub.utf8 import strict_utf8_size


_SAFE_PERSISTED_IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,513}$")
_SAFE_PROVENANCE_COMPONENT = re.compile(r"^[A-Za-z0-9._-]{1,256}$")
_MODEL_IDENTIFIER_PUNCTUATION = frozenset("-._/@+:")
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
_WINDOWS_ABSOLUTE_PREFIX = re.compile(r"(?i)^[A-Z]:[\\/]")
_UNQUOTED_PATH_ATOM = r"[^\s<>\"'`,;()\[\]{}]+"
_LABELED_ABSOLUTE_PATH = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])"
    r"(?:cwd|path|file|root|home|project|dir|directory|repo|workspace):"
    r"(?:~[/\\]|/)" + _UNQUOTED_PATH_ATOM
)
_EMBEDDED_POSIX_ROOT_PATH = re.compile(
    r"(?i)/(?:Users|home|private|var|tmp|Volumes|root|workspace|opt|etc|usr|srv|mnt|"
    r"Applications|Library)(?:/|$)"
)
_ENV_COMPONENT = re.compile(r"^\.env(?:$|rc(?:$|[._-])|[._-])", re.IGNORECASE)
_HARD_SENSITIVE_EXTENSIONS = (".pem", ".key", ".p12", ".pfx")
_PRIVATE_KEY_COMPONENTS = frozenset({"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", ".ssh"})


def safe_persisted_identifier(value: object, field: str, redactor: Redactor) -> str:
    """Return a bounded ASCII provenance identifier that contains no secret."""
    if (
        not isinstance(value, str)
        or _SAFE_PERSISTED_IDENTIFIER.fullmatch(value) is None
        or strict_utf8_size(value) > 2_049
    ):
        raise ValueError(f"invalid {field}")
    redacted = redactor.redact(value)
    if (
        set(redacted.findings) - {"sensitive_path"}
        or _has_hard_sensitive_component(value)
        or _REMOTE_URL.search(value) is not None
        or _SCP_REMOTE.search(value) is not None
        or _ALIAS_SCP_REMOTE.search(value) is not None
        or _SCHEMELESS_URL.search(value) is not None
        or _USERINFO_URL.search(value) is not None
        or _SINGLE_HOST_USERINFO_URL.search(value) is not None
        or _BRACKETED_HOST_URL.search(value) is not None
        or _SINGLE_HOST_URL.search(value) is not None
        or _LABELED_ABSOLUTE_PATH.search(value) is not None
        or _EMBEDDED_POSIX_ROOT_PATH.search(value) is not None
    ):
        raise ValueError(f"invalid {field}")
    return value


def safe_provenance_component(value: object, field: str, redactor: Redactor) -> str:
    """Return one unambiguous session/turn component for a composite source ID."""
    if not isinstance(value, str) or _SAFE_PROVENANCE_COMPONENT.fullmatch(value) is None:
        raise ValueError(f"invalid {field}")
    redacted = redactor.redact(value)
    if set(redacted.findings) - {"sensitive_path"} or _has_hard_sensitive_component(value):
        raise ValueError(f"invalid {field}")
    return value


def _has_hard_sensitive_component(value: str) -> bool:
    for component in value.split(":"):
        lowered = component.casefold()
        if (
            _ENV_COMPONENT.search(lowered) is not None
            or lowered.endswith(_HARD_SENSITIVE_EXTENSIONS)
            or lowered in _PRIVATE_KEY_COMPONENTS
        ):
            return True
    return False


def safe_model_identifier(value: object, redactor: Redactor) -> str:
    """Return an exact non-sensitive model identifier or fail closed."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 513
        or strict_utf8_size(value) > 2_049
        or any(character.isspace() for character in value)
        or "://" in value
        or any(
            not character.isalnum() and character not in _MODEL_IDENTIFIER_PUNCTUATION
            for character in value
        )
    ):
        raise ValueError("invalid model_id")
    model_parts = value.split("/")
    if any(
        not part or part in {".", ".."} or not part[0].isalnum() or not part[-1].isalnum()
        for part in model_parts
    ):
        raise ValueError("invalid model_id")
    colon_parts = tuple(index for index, part in enumerate(model_parts) if ":" in part)
    if colon_parts:
        tagged_part = model_parts[-1]
        tag_name, separator, tag_value = tagged_part.partition(":")
        if (
            colon_parts != (len(model_parts) - 1,)
            or tagged_part.count(":") != 1
            or not separator
            or not tag_name
            or not tag_value
            or "@" in tag_name
        ):
            raise ValueError("invalid model_id")
    redacted = redactor.redact(value)
    if (
        redacted.text != value
        or redacted.findings
        or value.startswith(("/", "~/", "~\\", "\\"))
        or _WINDOWS_ABSOLUTE_PREFIX.match(value) is not None
        or _REMOTE_URL.search(value) is not None
        or _SCP_REMOTE.search(value) is not None
        or _ALIAS_SCP_REMOTE.search(value) is not None
        or _SCHEMELESS_URL.search(value) is not None
        or _USERINFO_URL.search(value) is not None
        or _SINGLE_HOST_USERINFO_URL.search(value) is not None
        or _BRACKETED_HOST_URL.search(value) is not None
        or _SINGLE_HOST_URL.search(value) is not None
        or _LABELED_ABSOLUTE_PATH.search(value) is not None
        or _EMBEDDED_POSIX_ROOT_PATH.search(value) is not None
        or "?" in value
        or "#" in value
    ):
        raise ValueError("invalid model_id")
    return value
