from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

import pytest

from project_memory_hub import container as container_module
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.domain import SourceAgent
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.probes.base import ProbeClock, SystemProbeClock
from project_memory_hub.probes.builtin import OPTIONAL_PROBE_SOURCES
from project_memory_hub.probes.service import SourceProbeService
from project_memory_hub.storage.database import Database


class RecordingClock(ProbeClock):
    def now(self) -> datetime:
        return datetime(2026, 7, 18, tzinfo=UTC)

    def monotonic(self) -> float:
        return 10.0


class UnusedClock(ProbeClock):
    def now(self) -> datetime:
        return _unexpected_call()

    def monotonic(self) -> float:
        return _unexpected_call()


class FspathBomb:
    def __fspath__(self) -> str:
        raise AssertionError("ignored config_path was inspected")


def _unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("zero-write probe builder touched runtime state")


def _build_probe_container(**kwargs: object) -> object:
    builder = getattr(container_module, "build_probe_container")
    return builder(**kwargs)


def test_build_probe_container_does_not_touch_runtime_or_probe_on_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "must-not-exist"
    ignored_config = tmp_path / "ignored" / "~" / "config.toml"
    probe_home = tmp_path / "probe-home"
    clock = RecordingClock()
    monkeypatch.setenv("PROJECT_MEMORY_HUB_HOME", str(runtime))
    monkeypatch.setattr(RuntimePaths, "for_root", _unexpected_call)
    monkeypatch.setattr(RuntimePaths, "ensure", _unexpected_call)
    monkeypatch.setattr(ConfigManager, "load", _unexpected_call)
    monkeypatch.setattr(ConfigManager, "save", _unexpected_call)
    monkeypatch.setattr(Database, "initialize", _unexpected_call)
    monkeypatch.setattr(Path, "expanduser", _unexpected_call)
    monkeypatch.setattr(
        container_module,
        "_create_default_config_if_absent",
        _unexpected_call,
    )
    monkeypatch.setattr(
        container_module,
        "_tighten_config_permissions",
        _unexpected_call,
    )
    monkeypatch.setattr(SourceProbeService, "probe_all_light", _unexpected_call)
    monkeypatch.setattr(SourceProbeService, "probe_one", _unexpected_call)
    monkeypatch.setattr(SourceProbeService, "reserve_structure", _unexpected_call)

    container = _build_probe_container(
        config_path=ignored_config,
        home=probe_home,
        clock=clock,
    )
    container.close()
    container.close()

    assert not runtime.exists()
    assert not ignored_config.parent.exists()
    assert not probe_home.exists()
    assert tuple(field.name for field in dataclasses.fields(container)) == ("source_probes",)


def test_probe_container_preserves_injected_home_and_clock() -> None:
    probe_home = Path("/private/nonexistent/project-memory-probe-home")
    clock = RecordingClock()

    container = _build_probe_container(home=probe_home, clock=clock)
    try:
        service = container.source_probes
        assert isinstance(service, SourceProbeService)
        assert service._clock is clock
        assert service._filesystem._policy.home == probe_home
    finally:
        container.close()


def test_probe_builder_never_inspects_config_or_expands_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe_home = tmp_path / "literal-home"
    with monkeypatch.context() as path_guards:
        path_guards.setattr(Path, "expanduser", _unexpected_call)
        path_guards.setattr(Path, "resolve", _unexpected_call)
        path_guards.setattr(Path, "exists", _unexpected_call)
        container = _build_probe_container(
            config_path=FspathBomb(),
            home=probe_home,
            clock=UnusedClock(),
        )

    try:
        assert container.source_probes._filesystem._policy.home == probe_home
        assert not probe_home.exists()
    finally:
        container.close()


def test_probe_builder_rejects_relative_home_without_runtime_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tmp_path / "must-not-exist"
    monkeypatch.setenv("PROJECT_MEMORY_HUB_HOME", str(runtime))
    monkeypatch.setattr(RuntimePaths, "for_root", _unexpected_call)
    monkeypatch.setattr(RuntimePaths, "ensure", _unexpected_call)

    with pytest.raises(ValueError, match="absolute"):
        _build_probe_container(
            config_path=FspathBomb(),
            home=Path("relative-home"),
            clock=UnusedClock(),
        )

    assert not runtime.exists()


def test_probe_container_defaults_to_a_system_clock(
    tmp_path: Path,
) -> None:
    container = _build_probe_container(home=tmp_path)
    try:
        assert isinstance(container.source_probes._clock, SystemProbeClock)
    finally:
        container.close()


def test_probe_graph_has_no_runtime_reverse_dependencies(
    tmp_path: Path,
) -> None:
    container = _build_probe_container(home=tmp_path, clock=RecordingClock())
    try:
        service = container.source_probes
        forbidden = ("container", "database", "config", "repository")
        assert all(
            token not in field_name.casefold()
            for field_name in vars(service)
            for token in forbidden
        )
        assert (
            tuple(probe.descriptor.source_agent for probe in service._registry.all())
            == OPTIONAL_PROBE_SOURCES
        )
        assert all(
            tuple(field.name for field in dataclasses.fields(probe)) == ("descriptor",)
            for probe in service._registry.all()
        )
    finally:
        container.close()


def test_full_container_validates_probe_home_before_writing_runtime(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "runtime" / "config.toml"

    with pytest.raises(ValueError, match="absolute"):
        container_module.build_container(
            config_path,
            probe_home=Path("relative-home"),
        )

    assert not config_path.parent.exists()


def test_full_container_exposes_probes_without_registering_optional_adapters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "runtime" / "config.toml"
    probe_home = tmp_path / "unexpanded-probe-home"
    ConfigManager(config_path).save(
        AppConfig(
            project_roots=(tmp_path / "projects",),
            enabled_sources=(
                SourceAgent.CODEX,
                SourceAgent.CHATGPT,
                *OPTIONAL_PROBE_SOURCES,
            ),
            inactive_days=21,
            max_recall_tokens=800,
            daily_reconcile_time="03:30",
        )
    )
    monkeypatch.setattr(SourceProbeService, "probe_all_light", _unexpected_call)
    monkeypatch.setattr(SourceProbeService, "probe_one", _unexpected_call)
    monkeypatch.setattr(SourceProbeService, "reserve_structure", _unexpected_call)

    container = container_module.build_container(
        config_path,
        probe_home=probe_home,
    )
    try:
        assert isinstance(container.source_probes, SourceProbeService)
        assert container.source_probes._filesystem._policy.home == probe_home
        assert set(OPTIONAL_PROBE_SOURCES) <= set(container.config.enabled_sources)
        assert container.adapter_registry._enabled_sources == frozenset(
            {SourceAgent.CODEX, SourceAgent.CHATGPT}
        )
        assert {adapter.source_agent for adapter in container.adapter_registry._adapters} <= {
            SourceAgent.CODEX,
            SourceAgent.CHATGPT,
        }
        assert not (set(OPTIONAL_PROBE_SOURCES) & container.adapter_registry._enabled_sources)
    finally:
        container.close()


def test_safe_probe_release_keeps_schema_version_twelve(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime" / "config.toml"

    with container_module.build_container(config_path, probe_home=tmp_path) as container:
        with container.database.connect(readonly=True) as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )

    assert versions == tuple(range(1, 13))
