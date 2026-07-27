from __future__ import annotations


class InvalidUtf8Text(ValueError):
    """Raised when decoded text cannot be represented as strict UTF-8."""


def strict_utf8_bytes(value: str) -> bytes:
    """Encode text without allowing escaped lone surrogates to escape validation."""
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise InvalidUtf8Text("text must be valid UTF-8") from None


def strict_utf8_size(value: str) -> int:
    return len(strict_utf8_bytes(value))


_BIDI_CONTROLS = frozenset(
    {
        0x061C,
        0x200E,
        0x200F,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    }
)
_NORMAL_TEXT_WHITESPACE = frozenset("\t\n\r")


def contains_unsafe_text_control(
    value: str,
    *,
    allow_normal_text_whitespace: bool = False,
) -> bool:
    for character in value:
        codepoint = ord(character)
        if codepoint in _BIDI_CONTROLS or 0x7F <= codepoint <= 0x9F:
            return True
        if codepoint < 32 and not (
            allow_normal_text_whitespace and character in _NORMAL_TEXT_WHITESPACE
        ):
            return True
    return False
