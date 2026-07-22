from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final, cast

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes.base import (
    ExpectedPathType,
    InvalidProbeRequest,
    ProbeClock,
    ProbeFilesystem,
    SourceProbe,
    SourceDescriptor,
    TrustedAnchor,
    TrustedPath,
)
from project_memory_hub.probes.models import (
    ModelStatus,
    ProbeBudget,
    ProbeCapability,
    ProbeMode,
    ProbeWarningCode,
    SourceProbeRequest,
    SourceProbeResult,
    StructureStatus,
)


def _root(*components: str) -> TrustedPath:
    return TrustedPath(
        TrustedAnchor.FILESYSTEM_ROOT,
        components,
        ExpectedPathType.DIRECTORY,
    )


def _home(*components: str) -> TrustedPath:
    return TrustedPath(TrustedAnchor.HOME, components, ExpectedPathType.DIRECTORY)


def _root_executable(*components: str) -> TrustedPath:
    return TrustedPath(
        TrustedAnchor.FILESYSTEM_ROOT,
        components,
        ExpectedPathType.EXECUTABLE_FILE,
    )


def _home_executable(*components: str) -> TrustedPath:
    return TrustedPath(
        TrustedAnchor.HOME,
        components,
        ExpectedPathType.EXECUTABLE_FILE,
    )


TRAE_DESCRIPTOR: Final = SourceDescriptor(
    source_agent=SourceAgent.TRAE,
    installation_markers=(
        _root("Applications", "Trae.app"),
        _root("Applications", "Trae CN.app"),
        _root("Applications", "TRAE SOLO.app"),
        _root("Applications", "TRAE SOLO CN.app"),
    ),
    data_roots=(
        _home("Library", "Application Support", "Trae"),
        _home("Library", "Application Support", "Trae CN"),
        _home("Library", "Application Support", "TRAE SOLO"),
        _home("Library", "Application Support", "TRAE SOLO CN"),
        _home(".trae"),
        _home(".trae-cn"),
        _home(".trae-aicc"),
    ),
    capability=ProbeCapability.STRUCTURE_METADATA,
    recognized_schemas=(),
)

WORKBUDDY_DESCRIPTOR: Final = SourceDescriptor(
    source_agent=SourceAgent.WORKBUDDY,
    installation_markers=(_root("Applications", "WorkBuddy.app"),),
    data_roots=(
        _home("Library", "Application Support", "WorkBuddy"),
        _home(".workbuddy"),
    ),
    capability=ProbeCapability.PRESENCE_AND_ACCESS,
)

ZCODE_DESCRIPTOR: Final = SourceDescriptor(
    source_agent=SourceAgent.ZCODE,
    installation_markers=(_root("Applications", "ZCode.app"),),
    data_roots=(
        _home("Library", "Application Support", "ZCode"),
        _home(".zcode"),
    ),
    capability=ProbeCapability.PRESENCE_AND_ACCESS,
)

QODERWORK_DESCRIPTOR: Final = SourceDescriptor(
    source_agent=SourceAgent.QODERWORK,
    installation_markers=(_root("Applications", "QoderWork.app"),),
    data_roots=(
        _home("Library", "Application Support", "QoderWork"),
        _home(".qoderwork"),
    ),
    capability=ProbeCapability.PRESENCE_AND_ACCESS,
)

CLAUDE_CODE_DESCRIPTOR: Final = SourceDescriptor(
    source_agent=SourceAgent.CLAUDE_CODE,
    installation_markers=(
        _home_executable(".local", "bin", "claude"),
        _home_executable(".claude", "local", "claude"),
        _root_executable("opt", "homebrew", "bin", "claude"),
        _root_executable("usr", "local", "bin", "claude"),
    ),
    data_roots=(_home(".claude"),),
    capability=ProbeCapability.PRESENCE_AND_ACCESS,
)

OPTIONAL_PROBE_SOURCES: Final = (
    SourceAgent.TRAE,
    SourceAgent.WORKBUDDY,
    SourceAgent.ZCODE,
    SourceAgent.QODERWORK,
    SourceAgent.CLAUDE_CODE,
)

_BUILTIN_DESCRIPTORS: Final = (
    TRAE_DESCRIPTOR,
    WORKBUDDY_DESCRIPTOR,
    ZCODE_DESCRIPTOR,
    QODERWORK_DESCRIPTOR,
    CLAUDE_CODE_DESCRIPTOR,
)


def builtin_descriptors() -> tuple[SourceDescriptor, ...]:
    return _BUILTIN_DESCRIPTORS


@dataclass(frozen=True, slots=True)
class BuiltinSourceProbe:
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
        if request.source_agent is not self.descriptor.source_agent:
            raise InvalidProbeRequest("probe source does not match descriptor")
        if request.mode is ProbeMode.STRUCTURE:
            if request.source_agent is not SourceAgent.TRAE:
                raise InvalidProbeRequest("structure mode is Trae-only")
            structure_inspection = filesystem.inspect_trae_structure(
                self.descriptor,
                budget=budget,
                clock=clock,
                deadline=deadline,
            )
            return SourceProbeResult(
                source_agent=request.source_agent,
                mode=request.mode,
                installation_status=structure_inspection.installation_status,
                data_status=structure_inspection.data_status,
                capability=self.descriptor.capability,
                structure_status=structure_inspection.structure_status,
                model_status=ModelStatus.UNVERIFIABLE,
                ingestion_allowed=False,
                metrics=structure_inspection.metrics,
                warning_codes=(
                    *structure_inspection.warning_codes,
                    ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
                ),
                checked_at=checked_at,
            )
        if request.mode is not ProbeMode.LIGHT:
            raise InvalidProbeRequest("unsupported probe mode")
        light_inspection = filesystem.inspect_light(
            self.descriptor,
            budget=budget,
            clock=clock,
            deadline=deadline,
        )
        return SourceProbeResult(
            source_agent=request.source_agent,
            mode=request.mode,
            installation_status=light_inspection.installation_status,
            data_status=light_inspection.data_status,
            capability=self.descriptor.capability,
            structure_status=StructureStatus.NOT_RUN,
            model_status=ModelStatus.NOT_CHECKED,
            ingestion_allowed=False,
            metrics=light_inspection.metrics,
            warning_codes=light_inspection.warning_codes,
            checked_at=checked_at,
        )


def build_builtin_probes() -> tuple[SourceProbe, ...]:
    return tuple(
        cast(SourceProbe, BuiltinSourceProbe(descriptor)) for descriptor in builtin_descriptors()
    )
