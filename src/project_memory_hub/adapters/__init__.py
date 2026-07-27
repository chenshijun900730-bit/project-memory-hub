"""Trusted, source-specific ingestion adapters."""

from project_memory_hub.adapters.base import (
    IngestionError,
    IngestionResult,
    IngestionService,
    ReconcileRequiredError,
    SourceAdapter,
)
from project_memory_hub.adapters.chatgpt import (
    ChatGPTExportAdapter,
    ExplicitTaskExtractor,
    ImportReport,
    ProjectMatcher,
)
from project_memory_hub.adapters.codex import CodexAdapter
from project_memory_hub.adapters.registry import AdapterRegistry

__all__ = [
    "AdapterRegistry",
    "ChatGPTExportAdapter",
    "CodexAdapter",
    "IngestionError",
    "IngestionResult",
    "IngestionService",
    "ReconcileRequiredError",
    "ImportReport",
    "ExplicitTaskExtractor",
    "ProjectMatcher",
    "SourceAdapter",
]
