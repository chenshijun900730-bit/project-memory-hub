from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TypeVar
from uuid import UUID

from project_memory_hub.domain import (
    BehaviorMemoryRecord,
    FactRecord,
    MemoryKind,
    RecallBrief,
    RecallRequest,
)
from project_memory_hub.services.tokens import (
    ConservativeTokenCounter,
    TokenCounter,
    TokenCounterRegistry,
)
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository


_CANDIDATE_LIMIT = 100
_PRODUCT_MAX_RECALL_TOKENS = 800
_MIN_RECALL_TOKENS = 128
_SPACE = re.compile(r"\s+")
_LOCAL_TERM = re.compile(
    r"[a-z0-9_./:\\-]+|[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uac00-\ud7af]",
    re.IGNORECASE,
)
_COMMAND_TERMS = frozenset(
    {
        "build",
        "cargo",
        "check",
        "git",
        "lint",
        "npm",
        "pnpm",
        "pytest",
        "run",
        "test",
        "uv",
    }
)
_SECTION_ORDER = (
    "Current state",
    "Verified methods",
    "Relevant failures",
    "Risks",
    "Decisions",
    "Preferences",
    "Open issues",
    "Background",
)
_SECTION_INDEX = {section: index for index, section in enumerate(_SECTION_ORDER)}
_CURRENT_FACT_CATEGORIES = frozenset(
    {
        "build_config",
        "file_extension_count",
        "git_branch",
        "git_dirty",
        "git_head",
        "git_remote_fingerprint",
        "graphify_summary",
        "language_count",
        "manifest",
        "package_script",
        "test_config",
    }
)
_MEMORY_SECTIONS = {
    MemoryKind.DECISION: "Decisions",
    MemoryKind.FAILED_ATTEMPT: "Relevant failures",
    MemoryKind.VERIFIED_METHOD: "Verified methods",
    MemoryKind.PREFERENCE: "Preferences",
    MemoryKind.RISK: "Risks",
    MemoryKind.OPEN_ISSUE: "Open issues",
    MemoryKind.REUSABLE_LESSON: "Background",
    MemoryKind.OUTCOME: "Current state",
    MemoryKind.RETROSPECTIVE: "Background",
}
_RecordT = TypeVar("_RecordT")


@dataclass(frozen=True, slots=True)
class _Candidate:
    record_id: UUID
    content: str
    section: str
    mandatory: bool
    path_command_match: int
    overlap: int
    evidence_strength: int
    observed_at: int
    confidence: float
    reference_count: int = 1


class _SafeTokenCounter:
    def __init__(self, selected: TokenCounter) -> None:
        self._selected = selected
        self._fallback = ConservativeTokenCounter()
        self._using_fallback = False

    @property
    def used_fallback(self) -> bool:
        return self._using_fallback

    def count(self, text: str) -> int:
        if self._using_fallback:
            return self._fallback.count(text)
        try:
            baseline = self._selected.count("")
        except Exception:
            self._using_fallback = True
            return self._fallback.count(text)
        if type(baseline) is not int or baseline != 0:
            self._using_fallback = True
            return self._fallback.count(text)
        if not text:
            return 0
        try:
            result = self._selected.count(text)
        except Exception:
            self._using_fallback = True
            return self._fallback.count(text)
        if type(result) is not int or result < 0:
            self._using_fallback = True
            return self._fallback.count(text)
        return result


class RecallService:
    def __init__(
        self,
        projects: ProjectRepository,
        facts: FactRepository,
        memories: MemoryRepository,
        token_counters: TokenCounterRegistry,
        *,
        max_recall_tokens: int = _PRODUCT_MAX_RECALL_TOKENS,
    ) -> None:
        if type(max_recall_tokens) is not int or max_recall_tokens < _MIN_RECALL_TOKENS:
            raise ValueError("max_recall_tokens is invalid")
        self._projects = projects
        self._facts = facts
        self._memories = memories
        self._token_counters = token_counters
        self._max_recall_tokens = min(max_recall_tokens, _PRODUCT_MAX_RECALL_TOKENS)

    def recall(self, request: RecallRequest) -> RecallBrief:
        project = self._projects.find_by_cwd(request.cwd)
        if project is None:
            return _project_not_found_brief()
        live_identity = self._projects.record_live_identity(project)
        if live_identity is None:
            return _project_not_found_brief()

        task_terms = _terms(request.task)
        facts = _merge_records(
            self._facts.search(project.project_id, request.task, _CANDIDATE_LIMIT),
            self._facts.search(project.project_id, "", _CANDIDATE_LIMIT),
            id_attribute="fact_id",
        )
        memories = _merge_records(
            self._memories.search(
                project.project_id,
                request.namespace,
                request.task,
                _CANDIDATE_LIMIT,
            ),
            self._memories.search(
                project.project_id,
                request.namespace,
                "",
                _CANDIDATE_LIMIT,
            ),
            id_attribute="memory_id",
        )
        if self._projects.record_live_identity(project) != live_identity:
            return _project_not_found_brief()
        candidates = _deduplicate(
            [
                *(_fact_candidate(fact, task_terms) for fact in facts),
                *(_memory_candidate(memory, task_terms) for memory in memories),
            ]
        )
        counter = _SafeTokenCounter(self._token_counters.for_model(request.namespace.model_id))
        effective_budget = min(request.max_tokens, self._max_recall_tokens)
        selected, overrides, shortened = _select_candidates(
            candidates,
            counter,
            effective_budget,
        )
        rendered = _render(selected, overrides)
        estimated_tokens = counter.count(rendered)
        if counter.used_fallback:
            selected, overrides, shortened = _select_candidates(
                candidates,
                counter,
                effective_budget,
            )
            rendered = _render(selected, overrides)
            estimated_tokens = counter.count(rendered)
        while selected and estimated_tokens > effective_budget:
            selected = _remove_lowest_value(selected)
            rendered = _render(selected, overrides)
            estimated_tokens = counter.count(rendered)

        render_order = _render_order(selected)
        omitted_count = max(0, len(candidates) - len(render_order))
        warnings: list[str] = []
        if effective_budget < request.max_tokens:
            warnings.append("token_budget_clamped")
        if counter.used_fallback:
            warnings.append("token_counter_fallback")
        if shortened:
            warnings.append("mandatory_content_shortened")
        if omitted_count:
            warnings.append("token_budget_truncated")
        if self._projects.record_live_identity(project) != live_identity:
            return _project_not_found_brief()
        return RecallBrief(
            text=rendered,
            estimated_tokens=estimated_tokens,
            selected_ids=tuple(candidate.record_id for candidate in render_order),
            omitted_count=omitted_count,
            warnings=tuple(warnings),
        )


def _project_not_found_brief() -> RecallBrief:
    return RecallBrief(
        text="",
        estimated_tokens=0,
        selected_ids=(),
        omitted_count=0,
        warnings=("project_not_found",),
    )


def _merge_records(
    first: list[_RecordT], second: list[_RecordT], *, id_attribute: str
) -> list[_RecordT]:
    records = {getattr(record, id_attribute): record for record in first}
    records.update(
        (getattr(record, id_attribute), record)
        for record in second
        if getattr(record, id_attribute) not in records
    )
    return list(records.values())


def _fact_candidate(fact: FactRecord, task_terms: frozenset[str]) -> _Candidate:
    content = _line(fact.normalized_content)
    overlap, path_command_match = _relevance(content, task_terms)
    is_current = fact.category in _CURRENT_FACT_CATEGORIES or overlap > 0
    section = "Current state" if is_current else "Background"
    return _Candidate(
        record_id=fact.fact_id,
        content=content,
        section=section,
        mandatory=is_current,
        path_command_match=path_command_match,
        overlap=overlap,
        evidence_strength=_fact_evidence_strength(fact.evidence_type),
        observed_at=_timestamp(fact.observed_at),
        confidence=fact.confidence,
    )


def _memory_candidate(
    memory: BehaviorMemoryRecord,
    task_terms: frozenset[str],
) -> _Candidate:
    content = _line(memory.normalized_content)
    overlap, path_command_match = _relevance(content, task_terms)
    section = _MEMORY_SECTIONS[memory.memory_kind]
    mandatory = (
        section == "Current state"
        or memory.memory_kind is MemoryKind.OPEN_ISSUE
        or (
            memory.memory_kind is MemoryKind.VERIFIED_METHOD
            and (overlap > 0 or path_command_match > 0)
        )
    )
    return _Candidate(
        record_id=memory.memory_id,
        content=content,
        section=section,
        mandatory=mandatory,
        path_command_match=path_command_match,
        overlap=overlap,
        evidence_strength=(3 if memory.memory_kind is MemoryKind.VERIFIED_METHOD else 2),
        observed_at=_timestamp(memory.created_at),
        confidence=memory.confidence,
    )


def _deduplicate(candidates: list[_Candidate]) -> list[_Candidate]:
    deduplicated: dict[str, _Candidate] = {}
    for candidate in candidates:
        key = _line(candidate.content).casefold()
        existing = deduplicated.get(key)
        if existing is None:
            deduplicated[key] = candidate
            continue
        preferred = min((existing, candidate), key=_representative_key)
        deduplicated[key] = replace(
            preferred,
            reference_count=existing.reference_count + candidate.reference_count,
        )
    return sorted(deduplicated.values(), key=_ranking_key)


def _select_candidates(
    candidates: list[_Candidate],
    counter: TokenCounter,
    max_tokens: int,
) -> tuple[list[_Candidate], dict[UUID, str], bool]:
    mandatory = sorted(
        (candidate for candidate in candidates if candidate.mandatory),
        key=_ranking_key,
    )
    optional = sorted(
        (candidate for candidate in candidates if not candidate.mandatory),
        key=_ranking_key,
    )
    overrides: dict[UUID, str] = {}
    shortened = False
    if counter.count(_render(mandatory, overrides)) <= max_tokens:
        selected = mandatory.copy()
    else:
        selected, overrides = _fit_mandatory(mandatory, counter, max_tokens)
        shortened = bool(selected)

    for candidate in optional:
        attempt = [*selected, candidate]
        if counter.count(_render(attempt, overrides)) <= max_tokens:
            selected = attempt
    return selected, overrides, shortened


def _fit_mandatory(
    mandatory: list[_Candidate],
    counter: TokenCounter,
    max_tokens: int,
) -> tuple[list[_Candidate], dict[UUID, str]]:
    if not mandatory:
        return [], {}

    keep_count = _largest_fitting_prefix(
        mandatory,
        counter,
        max_tokens,
        character_limit=1,
    )
    if keep_count == 0:
        return [], {}
    retained = mandatory[:keep_count]
    maximum = min(160, max(len(candidate.content) for candidate in retained))
    character_limit = _largest_fitting_character_limit(
        retained,
        counter,
        max_tokens,
        maximum,
    )
    overrides = _mandatory_overrides(retained, character_limit)
    if counter.count(_render(retained, overrides)) <= max_tokens:
        return retained, overrides
    return _enforce_mandatory_budget(retained, counter, max_tokens, character_limit)


def _largest_fitting_prefix(
    mandatory: list[_Candidate],
    counter: TokenCounter,
    max_tokens: int,
    *,
    character_limit: int,
) -> int:
    lower = 0
    upper = len(mandatory)
    while lower < upper:
        middle = (lower + upper + 1) // 2
        retained = mandatory[:middle]
        overrides = _mandatory_overrides(retained, character_limit)
        if counter.count(_render(retained, overrides)) <= max_tokens:
            lower = middle
        else:
            upper = middle - 1
    return lower


def _largest_fitting_character_limit(
    retained: list[_Candidate],
    counter: TokenCounter,
    max_tokens: int,
    maximum: int,
) -> int:
    lower = 1
    upper = maximum
    while lower < upper:
        middle = (lower + upper + 1) // 2
        overrides = _mandatory_overrides(retained, middle)
        if counter.count(_render(retained, overrides)) <= max_tokens:
            lower = middle
        else:
            upper = middle - 1
    return lower


def _mandatory_overrides(
    retained: list[_Candidate],
    character_limit: int,
) -> dict[UUID, str]:
    return {
        candidate.record_id: _shortened_content(
            candidate.content,
            character_limit,
            candidate.reference_count,
        )
        for candidate in retained
    }


def _enforce_mandatory_budget(
    retained: list[_Candidate],
    counter: TokenCounter,
    max_tokens: int,
    character_limit: int,
) -> tuple[list[_Candidate], dict[UUID, str]]:
    while retained:
        overrides = _mandatory_overrides(retained, character_limit)
        if counter.count(_render(retained, overrides)) <= max_tokens:
            return retained, overrides
        if character_limit > 1:
            character_limit = max(1, character_limit // 2)
        else:
            retained = retained[:-1]
    return [], {}


def _shortened_content(content: str, character_limit: int, references: int) -> str:
    if len(content) > character_limit:
        prefix = content[:character_limit].rstrip()
        return f"{prefix}… [refs:{references}]"
    return f"{content} [refs:{references}]"


def _render(
    candidates: list[_Candidate],
    overrides: dict[UUID, str],
) -> str:
    grouped: dict[str, list[_Candidate]] = {}
    for candidate in _render_order(candidates):
        grouped.setdefault(candidate.section, []).append(candidate)
    sections: list[str] = []
    for section in _SECTION_ORDER:
        rows = grouped.get(section)
        if not rows:
            continue
        lines = [section]
        lines.extend(
            f"- {overrides.get(candidate.record_id, candidate.content)}" for candidate in rows
        )
        sections.append("\n".join(lines))
    return "\n\n".join(sections)


def _render_order(candidates: list[_Candidate]) -> list[_Candidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            _SECTION_INDEX[candidate.section],
            _ranking_key(candidate),
        ),
    )


def _remove_lowest_value(candidates: list[_Candidate]) -> list[_Candidate]:
    optional = [candidate for candidate in candidates if not candidate.mandatory]
    removable = optional or candidates
    lowest = max(removable, key=_ranking_key)
    return [candidate for candidate in candidates if candidate.record_id != lowest.record_id]


def _representative_key(
    candidate: _Candidate,
) -> tuple[bool, tuple[int, int, int, int, float, str], int]:
    return (
        not candidate.mandatory,
        _ranking_key(candidate),
        _SECTION_INDEX[candidate.section],
    )


def _ranking_key(candidate: _Candidate) -> tuple[int, int, int, int, float, str]:
    return (
        -candidate.path_command_match,
        -candidate.overlap,
        -candidate.evidence_strength,
        -candidate.observed_at,
        -candidate.confidence,
        str(candidate.record_id),
    )


def _relevance(content: str, task_terms: frozenset[str]) -> tuple[int, int]:
    content_terms = _terms(content)
    matching_terms = task_terms & content_terms
    path_or_command = any(_is_path_term(term) or term in _COMMAND_TERMS for term in matching_terms)
    return len(matching_terms), int(path_or_command)


def _is_path_term(term: str) -> bool:
    return "/" in term or "\\" in term or term.startswith(".") or "." in term


def _terms(text: str) -> frozenset[str]:
    return frozenset(term.casefold() for term in _LOCAL_TERM.findall(text))


def _line(text: str) -> str:
    return _SPACE.sub(" ", text).strip()


def _fact_evidence_strength(evidence_type: str) -> int:
    if evidence_type == "user_approval":
        return 3
    if evidence_type:
        return 2
    return 1


def _timestamp(value: datetime) -> int:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return int(value.astimezone(timezone.utc).timestamp() * 1_000_000)
