from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes.models import (
    LightInspection,
    ProbeBudget,
    ProbeCapability,
    SourceProbeRequest,
    SourceProbeResult,
    StructureInspection,
)


class TrustedAnchor(StrEnum):
    FILESYSTEM_ROOT = "filesystem_root"
    HOME = "home"


class ExpectedPathType(StrEnum):
    DIRECTORY = "directory"
    EXECUTABLE_FILE = "executable_file"


@dataclass(frozen=True, slots=True)
class TrustedPath:
    anchor: TrustedAnchor
    components: tuple[str, ...]
    expected_type: ExpectedPathType

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, TrustedAnchor):
            raise TypeError("anchor must be a TrustedAnchor")
        if type(self.components) is not tuple:
            raise TypeError("components must be an exact tuple")
        if not isinstance(self.expected_type, ExpectedPathType):
            raise TypeError("expected_type must be an ExpectedPathType")
        if not self.components:
            raise ValueError("trusted path needs components")
        for component in self.components:
            _validate_component(component)


@dataclass(frozen=True, slots=True)
class RecognizedSchema:
    fingerprint: str
    session_identifier_fields: frozenset[str] = frozenset()
    model_identifier_fields: frozenset[str] = frozenset()
    bounded_count_query: str | None = None

    def __post_init__(self) -> None:
        if type(self.fingerprint) is not str:
            raise TypeError("fingerprint must be a string")
        _validate_frozen_string_set(
            self.session_identifier_fields,
            field_name="session_identifier_fields",
        )
        _validate_frozen_string_set(
            self.model_identifier_fields,
            field_name="model_identifier_fields",
        )
        if self.bounded_count_query is not None and type(self.bounded_count_query) is not str:
            raise TypeError("bounded_count_query must be a string or None")


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    source_agent: SourceAgent
    installation_markers: tuple[TrustedPath, ...]
    data_roots: tuple[TrustedPath, ...]
    capability: ProbeCapability
    recognized_schemas: tuple[RecognizedSchema, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.source_agent, SourceAgent):
            raise TypeError("source_agent must be a SourceAgent")
        _validate_exact_tuple(
            self.installation_markers,
            field_name="installation_markers",
            member_type=TrustedPath,
        )
        _validate_exact_tuple(
            self.data_roots,
            field_name="data_roots",
            member_type=TrustedPath,
        )
        if not isinstance(self.capability, ProbeCapability):
            raise TypeError("capability must be a ProbeCapability")
        _validate_exact_tuple(
            self.recognized_schemas,
            field_name="recognized_schemas",
            member_type=RecognizedSchema,
        )


class ProbeClock(Protocol):
    def now(self) -> datetime:
        raise NotImplementedError

    def monotonic(self) -> float:
        raise NotImplementedError


class SystemProbeClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


class ProbeFilesystem(Protocol):
    def inspect_light(
        self,
        descriptor: SourceDescriptor,
        *,
        budget: ProbeBudget,
        clock: ProbeClock,
        deadline: float,
    ) -> LightInspection:
        raise NotImplementedError

    def inspect_trae_structure(
        self,
        descriptor: SourceDescriptor,
        *,
        budget: ProbeBudget,
        clock: ProbeClock,
        deadline: float,
    ) -> StructureInspection:
        raise NotImplementedError


class SourceProbe(Protocol):
    descriptor: SourceDescriptor

    def probe(
        self,
        request: SourceProbeRequest,
        *,
        filesystem: ProbeFilesystem,
        budget: ProbeBudget,
        clock: ProbeClock,
        checked_at: datetime,
        deadline: float,
    ) -> SourceProbeResult:
        raise NotImplementedError


class InvalidProbeRequest(ValueError):
    pass


class ProbeBusyError(RuntimeError):
    pass


def _validate_component(component: str) -> None:
    if type(component) is not str:
        raise TypeError("trusted path components must be exact strings")
    encoded = component.encode("utf-8", errors="strict")
    invalid = (
        not component
        or component in {".", ".."}
        or "/" in component
        or "\x00" in component
        or len(encoded) > 255
        or any(ord(character) < 32 or ord(character) == 127 for character in component)
    )
    if invalid:
        raise ValueError("invalid trusted path component")


def _validate_frozen_string_set(value: object, *, field_name: str) -> None:
    if type(value) is not frozenset:
        raise TypeError(f"{field_name} must be an exact frozenset")
    if any(type(item) is not str for item in value):
        raise TypeError(f"{field_name} must contain only strings")


def _validate_exact_tuple(value: object, *, field_name: str, member_type: type[object]) -> None:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    if any(type(item) is not member_type for item in value):
        raise TypeError(f"{field_name} contains an invalid member")
