from __future__ import annotations

from collections.abc import Iterable

from project_memory_hub.adapters.base import SourceAdapter
from project_memory_hub.domain import SourceAgent


class AdapterRegistry:
    def __init__(
        self,
        adapters: Iterable[SourceAdapter],
        enabled_sources: Iterable[SourceAgent] = (
            SourceAgent.CODEX,
            SourceAgent.CHATGPT,
        ),
    ) -> None:
        self._adapters = tuple(adapters)
        self._enabled_sources = frozenset(SourceAgent(source) for source in enabled_sources)

    def enabled(self) -> tuple[SourceAdapter, ...]:
        return tuple(
            adapter for adapter in self._adapters if adapter.source_agent in self._enabled_sources
        )
