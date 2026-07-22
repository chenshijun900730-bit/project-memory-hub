from __future__ import annotations

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes.base import (
    ExpectedPathType,
    SourceDescriptor,
    TrustedAnchor,
    TrustedPath,
)
from project_memory_hub.probes.builtin import (
    OPTIONAL_PROBE_SOURCES,
    builtin_descriptors,
)
from project_memory_hub.probes.models import ProbeCapability


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


def test_builtin_descriptors_are_the_exact_fixed_whitelist() -> None:
    assert builtin_descriptors() == (
        SourceDescriptor(
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
        ),
        SourceDescriptor(
            source_agent=SourceAgent.WORKBUDDY,
            installation_markers=(_root("Applications", "WorkBuddy.app"),),
            data_roots=(
                _home("Library", "Application Support", "WorkBuddy"),
                _home(".workbuddy"),
            ),
            capability=ProbeCapability.PRESENCE_AND_ACCESS,
        ),
        SourceDescriptor(
            source_agent=SourceAgent.ZCODE,
            installation_markers=(_root("Applications", "ZCode.app"),),
            data_roots=(
                _home("Library", "Application Support", "ZCode"),
                _home(".zcode"),
            ),
            capability=ProbeCapability.PRESENCE_AND_ACCESS,
        ),
        SourceDescriptor(
            source_agent=SourceAgent.QODERWORK,
            installation_markers=(_root("Applications", "QoderWork.app"),),
            data_roots=(
                _home("Library", "Application Support", "QoderWork"),
                _home(".qoderwork"),
            ),
            capability=ProbeCapability.PRESENCE_AND_ACCESS,
        ),
        SourceDescriptor(
            source_agent=SourceAgent.CLAUDE_CODE,
            installation_markers=(
                _home_executable(".local", "bin", "claude"),
                _home_executable(".claude", "local", "claude"),
                _root_executable("opt", "homebrew", "bin", "claude"),
                _root_executable("usr", "local", "bin", "claude"),
            ),
            data_roots=(_home(".claude"),),
            capability=ProbeCapability.PRESENCE_AND_ACCESS,
        ),
    )


def test_builtin_descriptors_lock_counts_and_source_order() -> None:
    descriptors = builtin_descriptors()

    assert OPTIONAL_PROBE_SOURCES == (
        SourceAgent.TRAE,
        SourceAgent.WORKBUDDY,
        SourceAgent.ZCODE,
        SourceAgent.QODERWORK,
        SourceAgent.CLAUDE_CODE,
    )
    assert tuple(item.source_agent for item in descriptors) == OPTIONAL_PROBE_SOURCES
    assert sum(len(item.installation_markers) for item in descriptors) == 11
    assert sum(len(item.data_roots) for item in descriptors) == 14


def test_builtin_descriptors_exclude_unapproved_paths() -> None:
    descriptors = builtin_descriptors()
    serialized = repr(descriptors)

    assert "Workbuddy" not in serialized
    assert "Qoder.app" not in serialized
    assert "Application Support', 'Qoder'" not in serialized
    assert "('.qoder',)" not in serialized
    assert "Claude.app" not in serialized
    assert "PATH" not in serialized
    assert descriptors[0].source_agent is SourceAgent.TRAE
    assert descriptors[0].recognized_schemas == ()
