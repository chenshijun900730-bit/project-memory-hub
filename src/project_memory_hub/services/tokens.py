from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Protocol


class TokenCounter(Protocol):
    def count(self, text: str) -> int:
        raise NotImplementedError


class ConservativeTokenCounter:
    def count(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        if not text:
            return 0
        cjk_characters = sum(_is_cjk(character) for character in text)
        other_characters = len(text) - cjk_characters
        return math.ceil((cjk_characters + other_characters / 4.0) * 1.10)


class TokenCounterRegistry:
    def __init__(
        self,
        exact_counters: Mapping[str, TokenCounter | None] | None = None,
        *,
        fallback: TokenCounter | None = None,
    ) -> None:
        self._exact_counters = {
            model_id: counter
            for model_id, counter in (exact_counters or {}).items()
            if isinstance(model_id, str)
            and model_id.strip()
            and counter is not None
            and callable(getattr(counter, "count", None))
        }
        self._fallback = fallback if fallback is not None else ConservativeTokenCounter()

    def for_model(self, model_id: str) -> TokenCounter:
        if not isinstance(model_id, str) or not model_id.strip():
            return self._fallback
        return self._exact_counters.get(model_id, self._fallback)


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return any(
        start <= codepoint <= end
        for start, end in (
            (0x1100, 0x11FF),
            (0x3040, 0x30FF),
            (0x3130, 0x318F),
            (0x31F0, 0x31FF),
            (0x3400, 0x4DBF),
            (0x4E00, 0x9FFF),
            (0xA960, 0xA97F),
            (0xAC00, 0xD7AF),
            (0xD7B0, 0xD7FF),
            (0xF900, 0xFAFF),
            (0xFF65, 0xFF9F),
            (0x1AFF0, 0x1AFFF),
            (0x1B000, 0x1B0FF),
            (0x1B100, 0x1B12F),
            (0x1B130, 0x1B16F),
            (0x20000, 0x2A6DF),
            (0x2A700, 0x2B73F),
            (0x2B740, 0x2B81F),
            (0x2B820, 0x2CEAF),
            (0x2CEB0, 0x2EBEF),
            (0x2EBF0, 0x2EE5F),
            (0x2F800, 0x2FA1F),
            (0x30000, 0x3134F),
            (0x31350, 0x323AF),
        )
    )
