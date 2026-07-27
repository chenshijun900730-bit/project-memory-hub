from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime

from project_memory_hub.domain import SourceAgent
from project_memory_hub.probes.base import (
    InvalidProbeRequest,
    ProbeBusyError,
    ProbeClock,
    SourceProbe,
)
from project_memory_hub.probes.builtin import OPTIONAL_PROBE_SOURCES
from project_memory_hub.probes.filesystem import PathSafetyPolicy, SafeProbeFilesystem
from project_memory_hub.probes.models import (
    DataStatus,
    InstallationStatus,
    ModelStatus,
    ProbeBudget,
    ProbeMetrics,
    ProbeMode,
    ProbeWarningCode,
    SourceProbeRequest,
    SourceProbeResult,
    StructureStatus,
)


class SourceProbeRegistry:
    def __init__(self, probes: Iterable[SourceProbe]) -> None:
        self._probes = tuple(probes)
        sources = tuple(probe.descriptor.source_agent for probe in self._probes)
        if sources != OPTIONAL_PROBE_SOURCES or len(set(sources)) != len(sources):
            raise ValueError("probe registry must contain the five optional sources")
        self._by_source = dict(zip(sources, self._probes, strict=True))

    def get(self, source_agent: SourceAgent) -> SourceProbe:
        if not isinstance(source_agent, SourceAgent) or source_agent not in OPTIONAL_PROBE_SOURCES:
            raise InvalidProbeRequest("unsupported probe source")
        return self._by_source[source_agent]

    def all(self) -> tuple[SourceProbe, ...]:
        return self._probes


class StructureProbeLease:
    def __init__(
        self,
        run_probe: Callable[[], SourceProbeResult],
        reservation_lock: threading.Lock,
    ) -> None:
        self._run_probe = run_probe
        self._reservation_lock = reservation_lock
        self._state_lock = threading.Lock()
        self._state = "reserved"

    def run(self) -> SourceProbeResult:
        with self._state_lock:
            if self._state != "reserved":
                raise RuntimeError("structure lease is not runnable")
            self._state = "running"
        try:
            return self._run_probe()
        finally:
            try:
                with self._state_lock:
                    self._state = "closed"
            finally:
                self._reservation_lock.release()

    def close(self) -> None:
        with self._state_lock:
            if self._state != "reserved":
                return
            self._state = "closed"
        self._reservation_lock.release()


class SourceProbeService:
    def __init__(
        self,
        registry: SourceProbeRegistry,
        path_policy: PathSafetyPolicy,
        budget: ProbeBudget,
        clock: ProbeClock,
    ) -> None:
        self._registry = registry
        self._filesystem = SafeProbeFilesystem(path_policy)
        self._budget = budget
        self._clock = clock
        self._structure_lock = threading.Lock()

    def probe_all_light(self) -> tuple[SourceProbeResult, ...]:
        deadline = self._clock.monotonic() + self._budget.light_all_timeout_seconds
        return tuple(
            self._run_localized_probe(
                probe,
                SourceProbeRequest(
                    source_agent=probe.descriptor.source_agent,
                    mode=ProbeMode.LIGHT,
                ),
                deadline=deadline,
            )
            for probe in self._registry.all()
        )

    def probe_one(
        self,
        source_agent: SourceAgent,
        *,
        mode: ProbeMode = ProbeMode.LIGHT,
    ) -> SourceProbeResult:
        self._validate_request(source_agent, mode)
        if mode is ProbeMode.STRUCTURE:
            return self.reserve_structure(source_agent).run()
        probe = self._registry.get(source_agent)
        deadline = self._clock.monotonic() + self._budget.light_all_timeout_seconds
        return self._run_localized_probe(
            probe,
            SourceProbeRequest(source_agent=source_agent, mode=mode),
            deadline=deadline,
        )

    def reserve_structure(self, source_agent: SourceAgent) -> StructureProbeLease:
        self._validate_request(source_agent, ProbeMode.STRUCTURE)
        probe = self._registry.get(source_agent)
        if not self._structure_lock.acquire(blocking=False):
            raise ProbeBusyError("probe_busy")
        try:
            return StructureProbeLease(
                lambda: self._run_structure_probe(probe),
                self._structure_lock,
            )
        except BaseException:
            self._structure_lock.release()
            raise

    def _run_structure_probe(self, probe: SourceProbe) -> SourceProbeResult:
        deadline = self._clock.monotonic() + self._budget.structure_timeout_seconds
        return self._run_localized_probe(
            probe,
            SourceProbeRequest(
                source_agent=SourceAgent.TRAE,
                mode=ProbeMode.STRUCTURE,
            ),
            deadline=deadline,
        )

    def _run_localized_probe(
        self,
        probe: SourceProbe,
        request: SourceProbeRequest,
        *,
        deadline: float,
    ) -> SourceProbeResult:
        checked_at = self._clock.now()
        try:
            return probe.probe(
                request,
                filesystem=self._filesystem,
                budget=self._budget,
                clock=self._clock,
                checked_at=checked_at,
                deadline=deadline,
            )
        except Exception:
            return self._fallback_result(probe, request, checked_at=checked_at)

    @staticmethod
    def _fallback_result(
        probe: SourceProbe,
        request: SourceProbeRequest,
        *,
        checked_at: datetime,
    ) -> SourceProbeResult:
        structure_mode = request.mode is ProbeMode.STRUCTURE
        warnings: tuple[ProbeWarningCode, ...] = (ProbeWarningCode.PROBE_FAILED,)
        if structure_mode:
            warnings = (*warnings, ProbeWarningCode.MODEL_ID_UNVERIFIABLE)
        return SourceProbeResult(
            source_agent=request.source_agent,
            mode=request.mode,
            installation_status=InstallationStatus.NOT_DETECTED,
            data_status=DataStatus.MISSING,
            capability=probe.descriptor.capability,
            structure_status=StructureStatus.NOT_RUN,
            model_status=(ModelStatus.UNVERIFIABLE if structure_mode else ModelStatus.NOT_CHECKED),
            ingestion_allowed=False,
            metrics=ProbeMetrics(),
            warning_codes=warnings,
            checked_at=checked_at,
        )

    @staticmethod
    def _validate_request(source_agent: object, mode: object) -> None:
        if not isinstance(source_agent, SourceAgent) or source_agent not in OPTIONAL_PROBE_SOURCES:
            raise InvalidProbeRequest("unsupported probe source")
        if not isinstance(mode, ProbeMode):
            raise InvalidProbeRequest("unsupported probe mode")
        if mode is ProbeMode.STRUCTURE and source_agent is not SourceAgent.TRAE:
            raise InvalidProbeRequest("structure mode is Trae-only")
