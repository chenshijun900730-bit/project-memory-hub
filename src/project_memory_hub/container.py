from __future__ import annotations

import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from project_memory_hub.adapters.base import IngestionService
from project_memory_hub.adapters.chatgpt import (
    ChatGPTExportAdapter,
    ExplicitTaskExtractor,
    ProjectMatcher,
)
from project_memory_hub.adapters.codex import CodexAdapter
from project_memory_hub.adapters.registry import AdapterRegistry
from project_memory_hub.config import AppConfig, ConfigManager
from project_memory_hub.discovery.policy import DiscoveryPolicy
from project_memory_hub.discovery.scanner import ProjectScanner
from project_memory_hub.domain import CaptureResult, DiscoveryResult, SourceAgent
from project_memory_hub.improvement.analyzer import ImprovementAnalyzer
from project_memory_hub.improvement.git_apply import (
    GitProposalApplier,
    GitProposalError,
)
from project_memory_hub.improvement.service import ProposalService
from project_memory_hub.integration.agents import AgentsIntegration
from project_memory_hub.integration.automation import (
    DEFAULT_LOCAL_TIME,
    DEFAULT_TIMEZONE,
    AutomationInspector,
    DesiredAutomation,
    InstallationIdentity,
)
from project_memory_hub.integration.doctor import DoctorService, inspect_graphify_hooks
from project_memory_hub.paths import RuntimePaths
from project_memory_hub.probes.base import ProbeClock, SystemProbeClock
from project_memory_hub.probes.builtin import build_builtin_probes
from project_memory_hub.probes.filesystem import PathSafetyPolicy
from project_memory_hub.probes.models import ProbeBudget
from project_memory_hub.probes.service import SourceProbeRegistry, SourceProbeService
from project_memory_hub.security.redaction import Redactor
from project_memory_hub.services.capture import CaptureService
from project_memory_hub.services.compaction import CompactionService
from project_memory_hub.services.locking import ProcessLock
from project_memory_hub.services.project_facts import ProjectFactService
from project_memory_hub.services.recall import RecallService
from project_memory_hub.services.reconcile import (
    DiscoveryStageResult,
    InboxRejectedError,
    ReconcileService,
)
from project_memory_hub.services.retry_queue import RetryQueue
from project_memory_hub.services.tokens import TokenCounterRegistry
from project_memory_hub.storage.checkpoints import CheckpointRepository
from project_memory_hub.storage.database import Database, ReadonlyDatabaseSnapshot
from project_memory_hub.storage.discovery import DiscoveryFindingRepository
from project_memory_hub.storage.facts import FactRepository
from project_memory_hub.storage.memories import MemoryRepository
from project_memory_hub.storage.projects import ProjectRepository
from project_memory_hub.storage.promotions import PromotionRepository
from project_memory_hub.storage.proposals import ProposalRepository
from project_memory_hub.storage.resolutions import IssueResolutionRepository


@dataclass(slots=True)
class ServiceContainer:
    paths: RuntimePaths
    config_manager: ConfigManager
    config: AppConfig
    database: Database
    projects: ProjectRepository
    facts: FactRepository
    memories: MemoryRepository
    issue_resolutions: IssueResolutionRepository
    promotions: PromotionRepository
    proposals: ProposalRepository
    improvement_analyzer: ImprovementAnalyzer
    proposal_applier: GitProposalApplier | None
    proposal_service: ProposalService
    discovery_findings: DiscoveryFindingRepository
    redactor: Redactor
    project_scanner: ProjectScanner
    project_facts: ProjectFactService
    capture: CaptureService
    token_counters: TokenCounterRegistry
    recall: RecallService
    checkpoints: CheckpointRepository
    retry_queue: RetryQueue
    process_lock: ProcessLock
    codex_adapter: CodexAdapter
    chatgpt_adapter: ChatGPTExportAdapter
    adapter_registry: AdapterRegistry
    source_probes: SourceProbeService
    reconcile: ReconcileService
    compaction: CompactionService
    _closed: bool = field(default=False, init=False, repr=False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

    def __enter__(self) -> ServiceContainer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class McpCaptureContainer:
    capture: CaptureService

    def close(self) -> None:
        return None

    def __enter__(self) -> McpCaptureContainer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class ProbeContainer:
    source_probes: SourceProbeService

    def close(self) -> None:
        return None


@dataclass(slots=True)
class ReadonlyChatGPTContainer:
    chatgpt_adapter: ChatGPTExportAdapter
    source_enabled: bool
    database: ReadonlyDatabaseSnapshot

    def close(self) -> None:
        self.database.close()


@dataclass(slots=True)
class ReadonlyCompactionContainer:
    compaction: CompactionService
    database: ReadonlyDatabaseSnapshot

    def close(self) -> None:
        self.database.close()


@dataclass(slots=True)
class ReadonlySetupContainer:
    config_manager: ConfigManager
    database: ReadonlyDatabaseSnapshot

    def close(self) -> None:
        self.database.close()


@dataclass(slots=True)
class ReadonlyProposalContainer:
    paths: RuntimePaths
    proposals: ProposalRepository
    proposal_applier: GitProposalApplier | None
    proposal_service: ProposalService
    database: ReadonlyDatabaseSnapshot

    def close(self) -> None:
        self.database.close()

    def __enter__(self) -> ReadonlyProposalContainer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class ReadonlyRecallContainer:
    paths: RuntimePaths
    recall: RecallService
    database: ReadonlyDatabaseSnapshot

    def close(self) -> None:
        self.database.close()

    def __enter__(self) -> ReadonlyRecallContainer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


@dataclass(slots=True)
class DoctorContainer:
    doctor: DoctorService

    def close(self) -> None:
        pass

    def __enter__(self) -> DoctorContainer:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()


def runtime_paths_for_config(config_path: Path | None = None) -> RuntimePaths:
    if config_path is None:
        return RuntimePaths.for_root()
    selected_config_path = Path(config_path).expanduser().absolute()
    _reject_existing_symlink_components(selected_config_path)
    return RuntimePaths.for_root(selected_config_path.parent)


def _build_source_probe_service(*, home: Path, clock: ProbeClock) -> SourceProbeService:
    return SourceProbeService(
        SourceProbeRegistry(build_builtin_probes()),
        PathSafetyPolicy(home=home),
        ProbeBudget(),
        clock,
    )


def build_probe_container(
    config_path: Path | None = None,
    *,
    home: Path | None = None,
    clock: ProbeClock | None = None,
) -> ProbeContainer:
    del config_path
    selected_home = home if home is not None else Path.home()
    return ProbeContainer(
        source_probes=_build_source_probe_service(
            home=selected_home,
            clock=clock if clock is not None else SystemProbeClock(),
        )
    )


def build_mcp_capture_container(
    config_path: Path | None = None,
) -> McpCaptureContainer:
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
        paths = RuntimePaths.for_root(selected_config_path.parent)
    _validate_existing_private_file(selected_config_path)
    _validate_existing_private_file(paths.database)
    ConfigManager(selected_config_path).load()
    database = Database(paths.database)
    database.require_current_schema_readonly()
    projects = ProjectRepository(database)
    redactor = Redactor()
    return McpCaptureContainer(
        capture=CaptureService(
            database,
            projects,
            MemoryRepository(database),
            redactor,
            issue_resolutions=IssueResolutionRepository(),
        )
    )


def build_mcp_reconcile_container(
    config_path: Path | None = None,
) -> ServiceContainer:
    return _build_service_container(config_path, prepare_runtime=False)


def build_container(
    config_path: Path | None = None,
    *,
    probe_home: Path | None = None,
    codex_sessions_root: Path | None = None,
    discovery_home: Path | None = None,
) -> ServiceContainer:
    return _build_service_container(
        config_path,
        probe_home=probe_home,
        codex_sessions_root=codex_sessions_root,
        discovery_home=discovery_home,
        prepare_runtime=True,
    )


def _build_service_container(
    config_path: Path | None = None,
    *,
    probe_home: Path | None = None,
    codex_sessions_root: Path | None = None,
    discovery_home: Path | None = None,
    prepare_runtime: bool,
) -> ServiceContainer:
    source_probes = _build_source_probe_service(
        home=probe_home if probe_home is not None else Path.home(),
        clock=SystemProbeClock(),
    )
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
        paths = RuntimePaths.for_root(selected_config_path.parent)

    manager = ConfigManager(selected_config_path)
    if prepare_runtime:
        paths.ensure()
        _create_default_config_if_absent(manager)
        _tighten_config_permissions(selected_config_path)
    else:
        _validate_existing_private_file(selected_config_path)
        _validate_existing_private_file(paths.database)
    config = manager.load()

    if paths.database.is_symlink():
        raise PermissionError("database must not be a symlink")
    database = Database(paths.database)
    if prepare_runtime:
        database.initialize()
    else:
        database.require_current_schema_readonly()
    projects = ProjectRepository(database)
    facts = FactRepository(database)
    memories = MemoryRepository(database)
    issue_resolutions = IssueResolutionRepository()
    redactor = Redactor()
    promotions = PromotionRepository(database, memories, facts, redactor)
    proposals = ProposalRepository(database, redactor)
    improvement_analyzer = ImprovementAnalyzer()
    proposal_applier = _build_proposal_applier(
        config,
        paths,
        repair_runtime_permissions=prepare_runtime,
    )
    proposal_service = ProposalService(
        proposals,
        proposal_applier,
        ProcessLock(paths.root / "proposal-apply.lock"),
    )
    discovery_findings = DiscoveryFindingRepository(database)
    project_scanner = ProjectScanner(DiscoveryPolicy.from_config(config, home=discovery_home))
    project_facts = ProjectFactService(facts, redactor, projects=projects)
    capture = CaptureService(
        database,
        projects,
        memories,
        redactor,
        issue_resolutions=issue_resolutions,
    )
    token_counters = TokenCounterRegistry()
    recall = RecallService(
        projects,
        facts,
        memories,
        token_counters,
        max_recall_tokens=config.max_recall_tokens,
    )
    checkpoints = CheckpointRepository(database)
    retry_queue = RetryQueue(database, projects, redactor)
    compaction = CompactionService(
        database,
        memories,
        redactor,
        inactive_days=config.inactive_days,
    )
    process_lock = ProcessLock(paths.root / "reconcile.lock")
    codex_adapter = CodexAdapter(
        (
            Path.home() / ".codex" / "sessions"
            if codex_sessions_root is None
            else Path(codex_sessions_root)
        ),
        redactor,
    )
    chatgpt_adapter = ChatGPTExportAdapter(
        matcher=ProjectMatcher(database),
        extractor=ExplicitTaskExtractor(redactor),
        capture=capture,
        checkpoints=checkpoints,
        redactor=redactor,
        database=database,
    )
    adapter_registry = AdapterRegistry(
        adapters=(codex_adapter,),
        enabled_sources=tuple(
            source
            for source in config.enabled_sources
            if source in {SourceAgent.CODEX, SourceAgent.CHATGPT}
        ),
    )
    ingestion = IngestionService(capture, checkpoints, database, projects)

    def discover_projects() -> DiscoveryStageResult:
        result = project_scanner.discover()
        permission_failures = sum(issue.code == "blocked_permission" for issue in result.issues)
        duplicate_candidates = _duplicate_candidate_count(result)
        discovery_findings.sync(result)
        registered = []
        failures = len(result.issues)
        with projects.discovery_batch():
            for candidate in result.candidates:
                try:
                    registered.append(projects.register(candidate))
                except Exception:
                    failures += 1
        return DiscoveryStageResult(
            tuple(registered),
            failure_count=min(failures, 2**31 - 1),
            permission_failure_count=min(permission_failures, 2**31 - 1),
            duplicate_candidate_count=duplicate_candidates,
        )

    def ingest_codex() -> SimpleNamespace:
        results: list[CaptureResult] = []
        failures = 0
        warnings = 0
        deferred_count = 0
        resolved_count = 0
        already_resolved_count = 0
        unmatched_resolution_count = 0
        for scope in codex_adapter.discover_scopes():
            try:
                result = ingestion.ingest(codex_adapter, scope)
                results.extend(result.capture_results)
                warnings += result.warning_count
                deferred_count += result.deferred_count
                resolved_count += result.resolved_count
                already_resolved_count += result.already_resolved_count
                unmatched_resolution_count += result.unmatched_resolution_count
            except Exception:
                failures += 1
        return SimpleNamespace(
            capture_results=tuple(results),
            failure_count=failures,
            warning_count=warnings,
            deferred_count=deferred_count,
            resolved_count=resolved_count,
            already_resolved_count=already_resolved_count,
            unmatched_resolution_count=unmatched_resolution_count,
        )

    chatgpt_inbox = paths.imports / "chatgpt"

    def inbox_archives() -> tuple[Path, ...]:
        try:
            metadata = chatgpt_inbox.lstat()
        except FileNotFoundError:
            return ()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise InboxRejectedError("inbox_rejected")
        owner_mode = stat.S_IMODE(metadata.st_mode)
        if owner_mode & 0o500 != 0o500:
            raise InboxRejectedError("inbox_rejected")
        flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(chatgpt_inbox, flags)
        except OSError as error:
            raise InboxRejectedError("inbox_rejected") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISDIR(opened.st_mode):
                raise InboxRejectedError("inbox_rejected")
            with os.scandir(descriptor) as entries:
                archives = tuple(
                    chatgpt_inbox / entry.name
                    for entry in entries
                    if Path(entry.name).suffix.casefold() == ".zip"
                    and entry.is_file(follow_symlinks=False)
                )
        except (InboxRejectedError, OSError) as error:
            if isinstance(error, InboxRejectedError):
                raise
            raise InboxRejectedError("inbox_rejected") from error
        finally:
            os.close(descriptor)
        return tuple(
            sorted(
                archives,
                key=lambda item: item.name,
            )
        )

    codex_runs = (ingest_codex,) if SourceAgent.CODEX in config.enabled_sources else ()
    chatgpt_import = (
        chatgpt_adapter.import_zip if SourceAgent.CHATGPT in config.enabled_sources else None
    )
    reconcile = ReconcileService(
        database,
        process_lock,
        discover=discover_projects,
        scan_fact=project_facts.scan,
        retry_queue=retry_queue,
        retry_capture=capture,
        codex_runs=codex_runs,
        chatgpt_import=chatgpt_import,
        chatgpt_inbox=inbox_archives if chatgpt_import is not None else None,
        compact=compaction.compact_newly_inactive,
        improvement_analyzer=improvement_analyzer.analyze,
        improvement_draft_sink=proposals.create,
    )
    return ServiceContainer(
        paths=paths,
        config_manager=manager,
        config=config,
        database=database,
        projects=projects,
        facts=facts,
        memories=memories,
        issue_resolutions=issue_resolutions,
        promotions=promotions,
        proposals=proposals,
        improvement_analyzer=improvement_analyzer,
        proposal_applier=proposal_applier,
        proposal_service=proposal_service,
        discovery_findings=discovery_findings,
        redactor=redactor,
        project_scanner=project_scanner,
        project_facts=project_facts,
        capture=capture,
        token_counters=token_counters,
        recall=recall,
        checkpoints=checkpoints,
        retry_queue=retry_queue,
        process_lock=process_lock,
        codex_adapter=codex_adapter,
        chatgpt_adapter=chatgpt_adapter,
        adapter_registry=adapter_registry,
        source_probes=source_probes,
        reconcile=reconcile,
        compaction=compaction,
    )


def build_readonly_chatgpt_container(
    config_path: Path | None = None,
) -> ReadonlyChatGPTContainer:
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
        paths = RuntimePaths.for_root(selected_config_path.parent)
    _validate_existing_private_file(selected_config_path)
    _validate_existing_private_file(paths.database)
    config_manager = ConfigManager(selected_config_path)
    config = config_manager.load()
    database = ReadonlyDatabaseSnapshot(paths.database)
    projects = ProjectRepository(database)
    memories = MemoryRepository(database)
    redactor = Redactor()
    capture = CaptureService(
        database,
        projects,
        memories,
        redactor,
        issue_resolutions=IssueResolutionRepository(),
    )
    checkpoints = CheckpointRepository(database)
    adapter = ChatGPTExportAdapter(
        matcher=ProjectMatcher(database),
        extractor=ExplicitTaskExtractor(redactor),
        capture=capture,
        checkpoints=checkpoints,
        redactor=redactor,
        database=database,
    )
    return ReadonlyChatGPTContainer(
        adapter,
        SourceAgent.CHATGPT in config.enabled_sources,
        database,
    )


def build_readonly_compaction_container(
    config_path: Path | None = None,
) -> ReadonlyCompactionContainer:
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
        paths = RuntimePaths.for_root(selected_config_path.parent)
    _validate_existing_private_file(selected_config_path)
    _validate_existing_private_file(paths.database)
    config = ConfigManager(selected_config_path).load()
    database = ReadonlyDatabaseSnapshot(paths.database)
    memories = MemoryRepository(database)
    compaction = CompactionService(
        database,
        memories,
        Redactor(),
        inactive_days=config.inactive_days,
    )
    return ReadonlyCompactionContainer(compaction, database)


def build_readonly_setup_container(
    config_path: Path | None = None,
) -> ReadonlySetupContainer:
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
        paths = RuntimePaths.for_root(selected_config_path.parent)
    _validate_existing_private_file(selected_config_path)
    _validate_existing_private_file(paths.database)
    return ReadonlySetupContainer(
        config_manager=ConfigManager(selected_config_path),
        database=ReadonlyDatabaseSnapshot(paths.database),
    )


def build_readonly_proposal_container(
    config_path: Path | None = None,
) -> ReadonlyProposalContainer:
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
        paths = RuntimePaths.for_root(selected_config_path.parent)
    _validate_existing_private_file(selected_config_path)
    _validate_existing_private_file(paths.database)
    config = ConfigManager(selected_config_path).load()
    database = ReadonlyDatabaseSnapshot(paths.database)
    proposals = ProposalRepository(database, Redactor())
    proposal_applier = _build_proposal_applier(
        config,
        paths,
        repair_runtime_permissions=False,
    )
    proposal_service = ProposalService(
        proposals,
        proposal_applier,
        ProcessLock(paths.root / "proposal-apply.lock"),
    )
    return ReadonlyProposalContainer(
        paths=paths,
        proposals=proposals,
        proposal_applier=proposal_applier,
        proposal_service=proposal_service,
        database=database,
    )


def build_readonly_recall_container(
    config_path: Path | None = None,
) -> ReadonlyRecallContainer:
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
        paths = RuntimePaths.for_root(selected_config_path.parent)
    _validate_existing_private_file(selected_config_path)
    _validate_existing_private_file(paths.database)
    config = ConfigManager(selected_config_path).load()
    database = ReadonlyDatabaseSnapshot(paths.database, migrate=False)
    recall = RecallService(
        ProjectRepository(database),
        FactRepository(database),
        MemoryRepository(database),
        TokenCounterRegistry(),
        max_recall_tokens=config.max_recall_tokens,
    )
    return ReadonlyRecallContainer(
        paths=paths,
        recall=recall,
        database=database,
    )


def build_doctor_container(config_path: Path | None = None) -> DoctorContainer:
    if config_path is None:
        paths = RuntimePaths.for_root()
        selected_config_path = paths.root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        paths = RuntimePaths.for_root(selected_config_path.parent)

    config = _load_optional_doctor_config(selected_config_path)
    codex_root = Path.home() / ".codex"
    repository_root = (paths.root / ".doctor-repository-unavailable").absolute()
    agents_status = _unavailable_agents_status
    automation_status = _unavailable_automation_status
    graphify_status: Callable[[Path], str] = _unavailable_graphify_status
    codex_sessions_optional = False

    try:
        identity = InstallationIdentity.discover()
    except Exception:
        identity = None
    launcher: Path | None = None
    identity_root: Path | None = None
    repository_identity: tuple[int, int] | None = None
    installed_distribution = False
    if identity is None:
        try:
            installed_distribution = InstallationIdentity.is_installed_distribution()
        except Exception:
            installed_distribution = False
        if installed_distribution:
            agents_status = _missing_agents_status
            automation_status = _missing_automation_status
            graphify_status = _missing_graphify_status
            codex_sessions_optional = True
        try:
            discovered_launcher = InstallationIdentity.discover_launcher()
        except Exception:
            discovered_launcher = None
        if discovered_launcher is not None:
            launcher_candidate = Path(discovered_launcher)
            if launcher_candidate.is_absolute():
                launcher = launcher_candidate
    else:
        launcher_candidate = Path(identity.launcher)
        identity_root_candidate = Path(identity.repository_root)
        if launcher_candidate.is_absolute() and identity_root_candidate.is_absolute():
            launcher = launcher_candidate
            identity_root = identity_root_candidate
            repository_root = identity_root_candidate
            repository_identity = (
                identity.repository_device,
                identity.repository_inode,
            )

    automation_inspector: AutomationInspector | None = None
    if launcher is not None:
        try:
            automation_inspector = AutomationInspector(codex_root / "automations")
        except Exception:
            automation_inspector = None

    if installed_distribution and launcher is not None and identity_root is None:
        try:
            installed_source = InstallationIdentity.resolve_installed_source(
                launcher=launcher,
            )
        except Exception:
            installed_source = None
        if installed_source is None or installed_source.status == "invalid":
            automation_status = _unavailable_automation_status
            graphify_status = _unavailable_graphify_status
        elif installed_source.status == "trusted" and installed_source.identity is not None:
            recovered_identity = installed_source.identity
            recovered_root = Path(recovered_identity.repository_root)
            if recovered_root.is_absolute():
                identity_root = recovered_root
                repository_root = recovered_root
                repository_identity = (
                    recovered_identity.repository_device,
                    recovered_identity.repository_inode,
                )

    if launcher is not None:
        try:
            agents_integration = AgentsIntegration(launcher)
        except Exception:
            pass
        else:

            def agents_status() -> str:
                return agents_integration.inspect(codex_root / "AGENTS.md").status

    if launcher is not None and identity_root is not None:

        def graphify_status(repository_root: Path) -> str:
            return inspect_graphify_hooks(
                repository_root,
                expected_repository_identity=repository_identity,
            )

        try:
            desired = DesiredAutomation.daily_reconcile(
                timezone=DEFAULT_TIMEZONE,
                local_time=(
                    config.daily_reconcile_time if config is not None else DEFAULT_LOCAL_TIME
                ),
                repository_root=identity_root,
                launcher=launcher,
                project_id=(config.codex_project_id if config is not None else None),
            )
        except Exception:
            automation_status = _unavailable_automation_status
        else:
            if automation_inspector is None:
                automation_status = _unavailable_automation_status
            else:

                def automation_status() -> str:
                    return automation_inspector.inspect(desired).status

    return DoctorContainer(
        doctor=DoctorService(
            paths=paths,
            config_path=selected_config_path,
            config=config,
            codex_sessions_path=codex_root / "sessions",
            repository_root=repository_root,
            agents_status=agents_status,
            automation_status=automation_status,
            graphify_status=graphify_status,
            codex_sessions_optional=codex_sessions_optional,
        )
    )


def configured_source_enabled(
    config_path: Path | None,
    source_agent: SourceAgent,
) -> bool | None:
    if config_path is None:
        selected_config_path = RuntimePaths.for_root().root / "config.toml"
    else:
        selected_config_path = Path(config_path).expanduser().absolute()
        _reject_existing_symlink_components(selected_config_path)
    if not selected_config_path.exists():
        return None
    _validate_existing_private_file(selected_config_path)
    config = ConfigManager(selected_config_path).load()
    return SourceAgent(source_agent) in config.enabled_sources


def _load_optional_doctor_config(config_path: Path) -> AppConfig | None:
    try:
        config_path.lstat()
    except OSError:
        return None
    try:
        _validate_existing_private_file(config_path)
        return ConfigManager(config_path).load()
    except (KeyError, OSError, RuntimeError, TypeError, UnicodeError, ValueError):
        return None


def _unavailable_agents_status() -> str:
    return "malformed"


def _unavailable_automation_status() -> str:
    return "drifted"


def _unavailable_graphify_status(_repository_root: Path) -> str:
    return "unavailable"


def _missing_agents_status() -> str:
    return "missing"


def _missing_automation_status() -> str:
    return "missing"


def _missing_graphify_status(_repository_root: Path) -> str:
    return "missing"


def _build_proposal_applier(
    config: AppConfig,
    paths: RuntimePaths,
    *,
    repair_runtime_permissions: bool = True,
) -> GitProposalApplier | None:
    if config.improvement_repository_root is None or not config.improvement_verification_commands:
        return None
    try:
        return GitProposalApplier(
            config.improvement_repository_root,
            paths.root,
            allowed_verification_argv=config.improvement_verification_commands,
            repair_runtime_permissions=repair_runtime_permissions,
        )
    except (GitProposalError, OSError, ValueError):
        return None


def _duplicate_candidate_count(result: DiscoveryResult) -> int:
    grouped: dict[tuple[str, str], set[str]] = {}
    for candidate in result.candidates:
        candidate_path = str(candidate.canonical_path)
        for kind, fingerprint in (
            ("git_remote", candidate.git_remote_fingerprint),
            ("manifest", candidate.manifest_fingerprint),
        ):
            if fingerprint is None:
                continue
            grouped.setdefault((kind, fingerprint), set()).add(candidate_path)
    count = sum(len(paths) > 1 for paths in grouped.values())
    return min(count, 2**31 - 1)


def _create_default_config_if_absent(manager: ConfigManager) -> None:
    if manager.path.exists():
        return
    temporary_path = manager.path.with_name(f".{manager.path.name}.{uuid4().hex}.candidate")
    try:
        ConfigManager(temporary_path).save(AppConfig.defaults(Path.home()))
        try:
            os.link(temporary_path, manager.path, follow_symlinks=False)
        except FileExistsError:
            pass
    finally:
        temporary_path.unlink(missing_ok=True)


def _tighten_config_permissions(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise PermissionError("config must be a regular file")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & stat.S_IRUSR == 0:
            raise PermissionError("config owner cannot read file")
        private_mode = mode & 0o600
        if private_mode != mode:
            os.fchmod(descriptor, private_mode)
    finally:
        os.close(descriptor)


def _reject_existing_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for component in path.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode):
            raise PermissionError("explicit runtime path must not contain symlinks")


def _validate_existing_private_file(path: Path) -> None:
    _reject_existing_symlink_components(path.absolute())
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise PermissionError("private runtime file rejected") from None
    try:
        metadata = os.fstat(descriptor)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or mode & 0o077
            or mode & stat.S_IRUSR == 0
        ):
            raise PermissionError("private runtime file rejected")
    finally:
        os.close(descriptor)
