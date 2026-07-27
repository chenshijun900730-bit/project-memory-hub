from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread
from typing import cast

import pytest

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes.base import (
    InvalidProbeRequest,
    ProbeBusyError,
    ProbeClock,
    ProbeFilesystem,
    SourceDescriptor,
    SourceProbe,
)
from project_memory_hub.probes.builtin import (
    OPTIONAL_PROBE_SOURCES,
    build_builtin_probes,
)
from project_memory_hub.probes.filesystem import PathSafetyPolicy
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    LightInspection,
    ModelStatus,
    ProbeBudget,
    ProbeMetrics,
    ProbeMode,
    ProbeWarningCode,
    SourceProbeRequest,
    SourceProbeResult,
    StructureInspection,
    StructureStatus,
)
from project_memory_hub.probes.service import (
    SourceProbeRegistry,
    SourceProbeService,
)
from project_memory_hub.probes import service as service_module


class RecordingClock(ProbeClock):
    def __init__(self) -> None:
        self.monotonic_value = 10.0
        self.monotonic_calls = 0
        self.now_calls = 0

    def now(self) -> datetime:
        self.now_calls += 1
        return datetime(2026, 7, 17, 8, 0, tzinfo=UTC)

    def monotonic(self) -> float:
        self.monotonic_calls += 1
        return self.monotonic_value


class RecordingFilesystem(ProbeFilesystem):
    def __init__(self) -> None:
        self.light_calls: list[SourceAgent] = []
        self.light_deadlines: list[float] = []
        self.structure_calls: list[SourceAgent] = []
        self.structure_deadlines: list[float] = []
        self.expire_light_on_call: int | None = None
        self.structure_result = StructureInspection(
            installation_status=InstallationStatus.DETECTED,
            data_status=DataStatus.READABLE,
            structure_status=StructureStatus.RECOGNIZED,
            metrics=ProbeMetrics(),
        )
        self.structure_error: BaseException | None = None
        self.entered: Event | None = None
        self.release: Event | None = None
        self.open_resource_count = 0

    def inspect_light(
        self,
        descriptor: SourceDescriptor,
        *,
        budget: ProbeBudget,
        clock: ProbeClock,
        deadline: float,
    ) -> LightInspection:
        del budget
        self.light_calls.append(descriptor.source_agent)
        self.light_deadlines.append(deadline)
        if self.expire_light_on_call == len(self.light_calls):
            assert isinstance(clock, RecordingClock)
            clock.monotonic_value = deadline
        if isinstance(clock, RecordingClock) and clock.monotonic_value >= deadline:
            return LightInspection(
                installation_status=InstallationStatus.NOT_DETECTED,
                data_status=DataStatus.MISSING,
                metrics=ProbeMetrics(),
                warning_codes=(ProbeWarningCode.PROBE_TIMEOUT,),
            )
        return LightInspection(
            installation_status=InstallationStatus.DETECTED,
            data_status=DataStatus.READABLE,
            metrics=ProbeMetrics(),
        )

    def inspect_trae_structure(
        self,
        descriptor: SourceDescriptor,
        *,
        budget: ProbeBudget,
        clock: ProbeClock,
        deadline: float,
    ) -> StructureInspection:
        del budget, clock
        self.structure_calls.append(descriptor.source_agent)
        self.structure_deadlines.append(deadline)
        self.open_resource_count = 1
        try:
            if self.entered is not None:
                self.entered.set()
            if self.release is not None and not self.release.wait(timeout=1):
                raise AssertionError("test did not release structure probe")
            if self.structure_error is not None:
                raise self.structure_error
            return self.structure_result
        finally:
            self.open_resource_count = 0


class ScriptedProbe(SourceProbe):
    def __init__(
        self,
        descriptor: SourceDescriptor,
        action: Callable[..., SourceProbeResult],
    ) -> None:
        self.descriptor = descriptor
        self.action = action
        self.deadlines: list[float] = []

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
        self.deadlines.append(deadline)
        return self.action(
            request=request,
            filesystem=filesystem,
            budget=budget,
            clock=clock,
            checked_at=checked_at,
            deadline=deadline,
        )


def _service(
    monkeypatch: pytest.MonkeyPatch,
    filesystem: RecordingFilesystem,
    *,
    probes: tuple[SourceProbe, ...] | None = None,
    clock: RecordingClock | None = None,
) -> SourceProbeService:
    monkeypatch.setattr(
        service_module,
        "SafeProbeFilesystem",
        lambda _policy: filesystem,
    )
    return SourceProbeService(
        SourceProbeRegistry(probes or build_builtin_probes()),
        PathSafetyPolicy(home=Path("/tmp")),
        ProbeBudget(),
        clock or RecordingClock(),
    )


def _successful_result(
    request: SourceProbeRequest,
    *,
    checked_at: datetime,
    descriptor: SourceDescriptor,
) -> SourceProbeResult:
    return SourceProbeResult(
        source_agent=request.source_agent,
        mode=request.mode,
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        capability=descriptor.capability,
        structure_status=StructureStatus.NOT_RUN,
        model_status=ModelStatus.NOT_CHECKED,
        ingestion_allowed=False,
        metrics=ProbeMetrics(),
        checked_at=checked_at,
    )


def _scripted_registry(
    *,
    failing_source: SourceAgent | None = None,
    error: BaseException | None = None,
) -> tuple[ScriptedProbe, ...]:
    probes: list[ScriptedProbe] = []
    for builtin in build_builtin_probes():
        descriptor = builtin.descriptor

        def action(
            *,
            request: SourceProbeRequest,
            checked_at: datetime,
            _descriptor: SourceDescriptor = descriptor,
            **_kwargs: object,
        ) -> SourceProbeResult:
            if request.source_agent is failing_source and error is not None:
                raise error
            return _successful_result(
                request,
                checked_at=checked_at,
                descriptor=_descriptor,
            )

        probes.append(ScriptedProbe(descriptor, action))
    return tuple(probes)


def test_registry_has_exact_five_source_order_and_rejects_duplicates() -> None:
    probes = build_builtin_probes()
    registry = SourceProbeRegistry(probes)

    assert tuple(probe.descriptor.source_agent for probe in registry.all()) == (
        OPTIONAL_PROBE_SOURCES
    )
    assert registry.get(SourceAgent.TRAE) is probes[0]

    with pytest.raises(ValueError, match="five optional sources"):
        SourceProbeRegistry((probes[0], *probes[:-1]))
    with pytest.raises(ValueError, match="five optional sources"):
        SourceProbeRegistry((probes[1], probes[0], *probes[2:]))


def test_trae_capability_is_structure_metadata_in_both_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    service = _service(monkeypatch, filesystem)

    light = service.probe_one(SourceAgent.TRAE)
    structure = service.probe_one(SourceAgent.TRAE, mode=ProbeMode.STRUCTURE)

    assert light.capability == structure.capability
    assert structure.structure_status is StructureStatus.RECOGNIZED
    assert structure.model_status is ModelStatus.UNVERIFIABLE
    assert ProbeWarningCode.MODEL_ID_UNVERIFIABLE in structure.warning_codes
    assert structure.ingestion_allowed is False


@pytest.mark.parametrize(
    "source_agent",
    (
        SourceAgent.WORKBUDDY,
        SourceAgent.ZCODE,
        SourceAgent.QODERWORK,
        SourceAgent.CLAUDE_CODE,
        SourceAgent.CODEX,
        SourceAgent.CHATGPT,
    ),
)
def test_other_sources_reject_structure_before_filesystem_access(
    source_agent: SourceAgent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    service = _service(monkeypatch, filesystem)

    with pytest.raises(InvalidProbeRequest):
        service.probe_one(source_agent, mode=ProbeMode.STRUCTURE)

    assert filesystem.light_calls == []
    assert filesystem.structure_calls == []
    lease = service.reserve_structure(SourceAgent.TRAE)
    lease.close()


def test_invalid_runtime_types_are_rejected_before_filesystem_or_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    service = _service(monkeypatch, filesystem)

    with pytest.raises(InvalidProbeRequest):
        service.probe_one(cast(SourceAgent, "trae"))
    with pytest.raises(InvalidProbeRequest):
        service.probe_one(SourceAgent.TRAE, mode=cast(ProbeMode, "structure"))

    assert filesystem.light_calls == []
    assert filesystem.structure_calls == []
    lease = service.reserve_structure(SourceAgent.TRAE)
    lease.close()


def test_probe_all_light_localizes_one_source_failure_and_hides_private_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "/Users/private/secret.sqlite schema chat body"
    probes = _scripted_registry(
        failing_source=SourceAgent.WORKBUDDY,
        error=RuntimeError(secret),
    )
    service = _service(monkeypatch, RecordingFilesystem(), probes=probes)

    results = service.probe_all_light()

    assert tuple(result.source_agent for result in results) == OPTIONAL_PROBE_SOURCES
    failed = results[1]
    assert failed.installation_status is InstallationStatus.NOT_DETECTED
    assert failed.data_status is DataStatus.MISSING
    assert failed.structure_status is StructureStatus.NOT_RUN
    assert failed.warning_codes == (ProbeWarningCode.PROBE_FAILED,)
    assert all(result.warning_codes == () for index, result in enumerate(results) if index != 1)
    assert secret not in json.dumps(
        [result.model_dump(mode="json") for result in results],
        ensure_ascii=False,
    )


def test_probe_all_light_shares_one_absolute_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordingClock()
    probes = _scripted_registry()
    service = _service(
        monkeypatch,
        RecordingFilesystem(),
        probes=probes,
        clock=clock,
    )

    service.probe_all_light()

    assert clock.monotonic_calls == 1
    assert [probe.deadlines for probe in probes] == [[12.0]] * 5


def test_probe_all_light_keeps_expired_deadline_for_remaining_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordingClock()
    filesystem = RecordingFilesystem()
    filesystem.expire_light_on_call = 2
    service = _service(monkeypatch, filesystem, clock=clock)

    results = service.probe_all_light()

    assert filesystem.light_deadlines == [12.0] * 5
    assert clock.monotonic_calls == 1
    assert results[0].warning_codes == ()
    assert all(result.warning_codes == (ProbeWarningCode.PROBE_TIMEOUT,) for result in results[1:])


def test_model_field_never_unlocks_ingestion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.structure_result = StructureInspection(
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        structure_status=StructureStatus.RECOGNIZED,
        metrics=ProbeMetrics(
            has_session_identifier=True,
            has_model_identifier_field=True,
        ),
    )
    service = _service(monkeypatch, filesystem)

    result = service.probe_one(SourceAgent.TRAE, mode=ProbeMode.STRUCTURE)

    assert result.metrics.has_model_identifier_field is True
    assert result.model_status is ModelStatus.UNVERIFIABLE
    assert result.ingestion_allowed is False
    assert ProbeWarningCode.MODEL_ID_UNVERIFIABLE in result.warning_codes


@pytest.mark.parametrize(
    ("status", "warnings"),
    (
        (StructureStatus.UNSUPPORTED, (ProbeWarningCode.UNSUPPORTED_FORMAT,)),
        (StructureStatus.RECOGNIZED, ()),
        (StructureStatus.PARTIAL, (ProbeWarningCode.MALFORMED_METADATA,)),
    ),
)
def test_structure_status_matrix_is_preserved_and_model_warning_is_added(
    status: StructureStatus,
    warnings: tuple[ProbeWarningCode, ...],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.structure_result = StructureInspection(
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        structure_status=status,
        metrics=ProbeMetrics(),
        warning_codes=warnings,
    )
    service = _service(monkeypatch, filesystem)

    result = service.probe_one(SourceAgent.TRAE, mode=ProbeMode.STRUCTURE)

    assert result.structure_status is status
    assert set(result.warning_codes) == {
        *warnings,
        ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
    }


def test_light_probe_does_not_acquire_structure_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    service = _service(monkeypatch, filesystem)
    lease = service.reserve_structure(SourceAgent.TRAE)
    try:
        result = service.probe_one(SourceAgent.TRAE)
    finally:
        lease.close()

    assert result.mode is ProbeMode.LIGHT
    assert filesystem.light_calls == [SourceAgent.TRAE]
    assert filesystem.structure_calls == []


def test_reservation_is_busy_before_any_probe_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    service = _service(monkeypatch, filesystem)

    first = service.reserve_structure(SourceAgent.TRAE)
    try:
        assert filesystem.structure_calls == []
        with pytest.raises(ProbeBusyError, match="probe_busy"):
            service.reserve_structure(SourceAgent.TRAE)
        assert filesystem.structure_calls == []
    finally:
        first.close()


def test_structure_deadline_starts_when_reserved_lease_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = RecordingClock()
    filesystem = RecordingFilesystem()
    service = _service(monkeypatch, filesystem, clock=clock)

    lease = service.reserve_structure(SourceAgent.TRAE)
    assert clock.monotonic_calls == 0
    clock.monotonic_value = 25.0
    lease.run()

    assert clock.monotonic_calls == 1
    assert filesystem.structure_deadlines == [28.0]


def test_two_structure_calls_start_only_one_body_and_busy_is_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.entered = Event()
    filesystem.release = Event()
    service = _service(monkeypatch, filesystem)
    lease = service.reserve_structure(SourceAgent.TRAE)
    result: list[SourceProbeResult] = []
    errors: list[BaseException] = []

    def run_lease() -> None:
        try:
            result.append(lease.run())
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run_lease)
    thread.start()
    assert filesystem.entered.wait(timeout=1)
    try:
        with pytest.raises(ProbeBusyError) as error:
            service.reserve_structure(SourceAgent.TRAE)
        assert str(error.value) == "probe_busy"
        assert filesystem.structure_calls == [SourceAgent.TRAE]
    finally:
        filesystem.release.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert errors == []
    assert len(result) == 1
    next_lease = service.reserve_structure(SourceAgent.TRAE)
    next_lease.close()


def test_two_probe_one_structure_calls_cannot_bypass_reservation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.entered = Event()
    filesystem.release = Event()
    service = _service(monkeypatch, filesystem)
    results: list[SourceProbeResult] = []
    errors: list[BaseException] = []

    def run_first() -> None:
        try:
            results.append(service.probe_one(SourceAgent.TRAE, mode=ProbeMode.STRUCTURE))
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run_first)
    thread.start()
    assert filesystem.entered.wait(timeout=1)
    try:
        with pytest.raises(ProbeBusyError, match="probe_busy"):
            service.probe_one(SourceAgent.TRAE, mode=ProbeMode.STRUCTURE)
        assert filesystem.structure_calls == [SourceAgent.TRAE]
    finally:
        filesystem.release.set()
        thread.join(timeout=1)

    assert not thread.is_alive()
    assert errors == []
    assert len(results) == 1


def test_lease_releases_only_after_resources_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.entered = Event()
    filesystem.release = Event()
    service = _service(monkeypatch, filesystem)
    lease = service.reserve_structure(SourceAgent.TRAE)
    errors: list[BaseException] = []

    def run_lease() -> None:
        try:
            lease.run()
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run_lease)
    thread.start()
    assert filesystem.entered.wait(timeout=1)
    assert filesystem.open_resource_count == 1
    with pytest.raises(ProbeBusyError):
        service.reserve_structure(SourceAgent.TRAE)

    filesystem.release.set()
    thread.join(timeout=1)

    assert not thread.is_alive()
    assert errors == []
    assert filesystem.open_resource_count == 0
    next_lease = service.reserve_structure(SourceAgent.TRAE)
    next_lease.close()


def test_timeout_result_closes_resources_before_releasing_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.structure_result = StructureInspection(
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        structure_status=StructureStatus.PARTIAL,
        metrics=ProbeMetrics(),
        warning_codes=(ProbeWarningCode.PROBE_TIMEOUT,),
    )
    service = _service(monkeypatch, filesystem)

    result = service.reserve_structure(SourceAgent.TRAE).run()

    assert result.warning_codes == (
        ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
        ProbeWarningCode.PROBE_TIMEOUT,
    )
    assert filesystem.open_resource_count == 0
    next_lease = service.reserve_structure(SourceAgent.TRAE)
    next_lease.close()


def test_structure_exception_is_localized_without_weakening_model_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.structure_error = RuntimeError("/private/schema/chat body")
    service = _service(monkeypatch, filesystem)

    result = service.probe_one(SourceAgent.TRAE, mode=ProbeMode.STRUCTURE)

    assert result.structure_status is StructureStatus.NOT_RUN
    assert result.model_status is ModelStatus.UNVERIFIABLE
    assert result.ingestion_allowed is False
    assert result.warning_codes == (
        ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
        ProbeWarningCode.PROBE_FAILED,
    )
    assert "/private" not in result.model_dump_json()
    next_lease = service.reserve_structure(SourceAgent.TRAE)
    next_lease.close()


def test_base_exception_propagates_but_structure_lease_still_releases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.structure_error = KeyboardInterrupt("private")
    service = _service(monkeypatch, filesystem)
    lease = service.reserve_structure(SourceAgent.TRAE)

    with pytest.raises(KeyboardInterrupt):
        lease.run()

    assert filesystem.open_resource_count == 0
    next_lease = service.reserve_structure(SourceAgent.TRAE)
    next_lease.close()
    with pytest.raises(RuntimeError, match="not runnable"):
        lease.run()


def test_light_probe_propagates_base_exception_instead_of_localizing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probes = _scripted_registry(
        failing_source=SourceAgent.WORKBUDDY,
        error=KeyboardInterrupt("private"),
    )
    service = _service(monkeypatch, RecordingFilesystem(), probes=probes)

    with pytest.raises(KeyboardInterrupt):
        service.probe_all_light()


def test_close_is_idempotent_and_never_releases_running_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    filesystem = RecordingFilesystem()
    filesystem.entered = Event()
    filesystem.release = Event()
    service = _service(monkeypatch, filesystem)
    lease = service.reserve_structure(SourceAgent.TRAE)
    errors: list[BaseException] = []

    def run_lease() -> None:
        try:
            lease.run()
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run_lease)
    thread.start()
    assert filesystem.entered.wait(timeout=1)

    lease.close()
    lease.close()
    with pytest.raises(ProbeBusyError):
        service.reserve_structure(SourceAgent.TRAE)

    filesystem.release.set()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert errors == []
    lease.close()
    next_lease = service.reserve_structure(SourceAgent.TRAE)
    next_lease.close()
