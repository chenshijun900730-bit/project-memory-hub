from __future__ import annotations

import math
import socket
import urllib.request

import pytest

from project_memory_hub.services.tokens import (
    ConservativeTokenCounter,
    TokenCounterRegistry,
)


class FixedCounter:
    def __init__(self, value: int) -> None:
        self.value = value
        self.seen: list[str] = []

    def count(self, text: str) -> int:
        self.seen.append(text)
        return self.value


def test_conservative_counter_uses_required_unicode_formula() -> None:
    counter = ConservativeTokenCounter()
    text = "汉あア한A!"

    assert counter.count("") == 0
    assert counter.count(text) == math.ceil((4 + 2 / 4) * 1.10)
    assert counter.count("⺀") == math.ceil((0 + 1 / 4) * 1.10)


def test_conservative_counter_includes_supplementary_kana_ranges() -> None:
    supplementary_kana = "\U0001aff0\U0001b000\U0001b100\U0001b130"

    assert ConservativeTokenCounter().count(supplementary_kana) == math.ceil(4 * 1.10)


def test_conservative_counter_overestimates_mixed_text() -> None:
    text = "修复缓存 bug and run pytest"

    assert ConservativeTokenCounter().count(text) >= 10


def test_registry_selects_exact_model_and_reuses_offline_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("token registry attempted network access")

    monkeypatch.setattr(socket, "create_connection", fail_network)
    monkeypatch.setattr(urllib.request, "urlopen", fail_network)
    exact = FixedCounter(7)
    registry = TokenCounterRegistry(
        {
            "gpt-local": exact,
            "   ": exact,
            "unavailable": None,
        }
    )

    assert registry.for_model("gpt-local") is exact
    fallback = registry.for_model("unknown")
    assert fallback is registry.for_model("")
    assert fallback is registry.for_model("   ")
    assert fallback is registry.for_model("unavailable")
    assert fallback is registry.for_model(" gpt-local ")
    assert fallback.count("offline") == ConservativeTokenCounter().count("offline")
