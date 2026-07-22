# Project Memory Hub 0.2.0 Safe Source Probe Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Trae、WorkBuddy、Zcode、QoderWork 和 Claude Code 增加零持久化、只读、有界的来源检测，并在精确 `model_id` 无法证明时永久锁住行为记忆导入。

**Architecture:** 新增无 storage/adapter 依赖的 `project_memory_hub.probes` 包，由固定白名单描述符、安全 fd 相对文件系统层、只读结果模型和单槽结构检测服务组成。完整 Web 容器只持有该服务，CLI 使用独立的零写入 `ProbeContainer`；控制台把瞬时结果映射为白名单标签，既不注册新 adapter，也不修改 `enabled_sources`、数据库或配置。

**Tech Stack:** Python 3.11、Pydantic 2、Typer、FastAPI/Starlette、Jinja2、SQLite 只读 URI、pytest、Playwright、Ruff、mypy、Hatch wheel、Graphify

---

## 开始前先锁定的边界

- 实施依据是 `docs/superpowers/specs/2026-07-17-safe-source-probe-design.md`；发生歧义时以该规格为准。
- 只允许修改本计划列出的 tracked source/test/docs 文件。Graphify 可以重建被 Git 忽略的本地 `graphify-out/*`，但不得主动 stage/commit 新生成物；不要修改 `SourceAgent`、`AdapterRegistry`、`ReconcileService`、`enabled_sources`、storage、migration、checkpoint、receipt 或 capture 链。
- 生产 `recognized_schema_fingerprints` 必须保持空集合；测试可以依赖注入的非生产指纹验证未来分支。
- CLI 的 `--config` 对 probe 无效，不能被打开、解析或作为扫描根。
- Web 深度检测必须先在事件循环线程非阻塞取得 reservation，再创建工作线程。锁忙时不能调用 `asyncio.to_thread()`。
- 所有测试只使用临时 HOME、合成目录和合成 SQLite；不得复制真实用户数据。
- “零副作用”指不创建、不持久化、不修改 Project Memory Hub runtime/config/SQLite/lock/checkpoint/receipt/source reference/pending capture/behavior memory；只读打开第三方文件可能由底层文件系统更新 atime，不把 atime 作为产品写入。

## 文件职责图

| 文件 | 动作 | 单一职责 |
|---|---|---|
| `src/project_memory_hub/probes/__init__.py` | 新建 | 只导出稳定探针 API |
| `src/project_memory_hub/probes/models.py` | 新建 | 严格结果模型、枚举、预算和稳定警告码 |
| `src/project_memory_hub/probes/base.py` | 新建 | 固定路径描述符、协议、clock 与错误类型 |
| `src/project_memory_hub/probes/filesystem.py` | 新建 | no-follow 轻量检查、fd-relative Trae 遍历、fd-only SQLite schema 检查 |
| `src/project_memory_hub/probes/builtin.py` | 新建 | 五来源精确白名单和结果映射 |
| `src/project_memory_hub/probes/service.py` | 新建 | registry、来源局部化、稳定顺序和结构检测 reservation |
| `src/project_memory_hub/container.py` | 修改 | 完整容器注入探针；新增零写入 `ProbeContainer` builder |
| `src/project_memory_hub/cli.py` | 修改 | 新增 `source probe` 命令与稳定错误/输出边界 |
| `src/project_memory_hub/services/control.py` | 修改 | 把领域结果映射为无任意字符串的页面视图模型 |
| `src/project_memory_hub/web/routes.py` | 修改 | GET 轻量探针、Trae POST reservation 与请求内渲染 |
| `src/project_memory_hub/web/templates/sources.html` | 修改 | 保留 Codex/ChatGPT 控制并显示五来源探针状态 |
| `src/project_memory_hub/web/static/sources.js` | 新建 | POST 页面把历史地址替换为 `/sources`，刷新转回 GET |
| `src/project_memory_hub/web/static/app.css` | 修改 | 探针状态、禁用按钮和警告样式 |
| `tests/unit/probes/*.py` | 新建 | 类型、依赖、文件系统、SQLite、服务和并发单元测试 |
| `tests/integration/test_probe_container.py` | 新建 | probe container 的零写入与完整容器接缝 |
| `tests/integration/test_probe_cli.py` | 新建 | CLI 参数、输出、错误、隐私和零副作用 |
| `tests/integration/test_web_routes.py` | 修改 | Sources GET/POST、瞬时结果和控制权限 |
| `tests/integration/test_web_security.py` | 修改 | Host/bootstrap/origin/CSRF/body/field 限制先于探针 |
| `tests/e2e/test_dashboard.py` | 修改 | 浏览器 URL、刷新复位、隐私和零写入验收 |
| `scripts/verify_wheel.py` | 新建 | 在临时目录构建并检查 wheel 内容、版本与入口 |
| `scripts/verify_probe_zero_write.py` | 新建 | 真实 runtime 的文件、摘要与关键表零写入验收 |
| `README.md`、`docs/operations.md` | 修改 | 0.2.0 用户说明与运维边界 |
| `pyproject.toml`、`src/project_memory_hub/__init__.py` | 修改 | 版本升至 0.2.0，不新增依赖或 migration |

## Task 1: 锁定领域契约、预算与依赖边界

**Files:**

- Create: `src/project_memory_hub/probes/__init__.py`
- Create: `src/project_memory_hub/probes/models.py`
- Create: `src/project_memory_hub/probes/base.py`
- Create: `tests/unit/probes/test_models.py`
- Create: `tests/unit/probes/test_import_boundaries.py`

- [ ] **Step 1: 写结果、预算、路径组件和静态依赖的失败测试**

在 `tests/unit/probes/test_models.py` 固定这些断言：

```python
def test_probe_budget_has_exact_frozen_defaults() -> None:
    budget = ProbeBudget()
    assert asdict(budget) == {
        "max_depth": 4,
        "max_entries": 2_048,
        "max_candidate_files": 64,
        "max_sqlite_candidates": 4,
        "max_sqlite_file_bytes": 64 * 1024 * 1024,
        "max_sqlite_total_bytes": 128 * 1024 * 1024,
        "max_sqlite_vm_steps": 100_000,
        "max_header_bytes": 64,
        "max_total_header_bytes": 4 * 1024,
        "max_schema_identifiers": 2_048,
        "structure_timeout_seconds": 3.0,
        "light_max_targets_per_source": 16,
        "light_all_timeout_seconds": 2.0,
    }


@pytest.mark.parametrize("value", [True, False, 0, -1])
def test_probe_budget_rejects_bool_zero_and_negative_values(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        ProbeBudget(max_depth=value)  # type: ignore[arg-type]


def test_result_is_strict_sorted_utc_and_never_ingestable() -> None:
    result = SourceProbeResult(
        source_agent=SourceAgent.TRAE,
        mode=ProbeMode.STRUCTURE,
        installation_status=InstallationStatus.DETECTED,
        data_status=DataStatus.READABLE,
        capability=ProbeCapability.STRUCTURE_METADATA,
        structure_status=StructureStatus.UNSUPPORTED,
        model_status=ModelStatus.UNVERIFIABLE,
        ingestion_allowed=False,
        metrics=ProbeMetrics(),
        warning_codes=(
            ProbeWarningCode.SOURCE_MISSING,
            ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
            ProbeWarningCode.SOURCE_MISSING,
        ),
        checked_at=datetime(2026, 7, 17, 8, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    assert result.ingestion_allowed is False
    assert result.warning_codes == (
        ProbeWarningCode.MODEL_ID_UNVERIFIABLE,
        ProbeWarningCode.SOURCE_MISSING,
    )
    assert result.checked_at == datetime(2026, 7, 17, tzinfo=UTC)


@pytest.mark.parametrize("value", [True, 0, 1, "false"])
def test_result_rejects_non_false_ingestion_values(value: object) -> None:
    with pytest.raises(ValidationError):
        payload = _valid_result_dict()
        payload["ingestion_allowed"] = value
        SourceProbeResult.model_validate(payload)


@pytest.mark.parametrize("component", ["", ".", "..", "a/b", "a\x00b", "a\nb"])
def test_trusted_path_rejects_dynamic_components(component: str) -> None:
    with pytest.raises(ValueError):
        TrustedPath(
            anchor=TrustedAnchor.HOME,
            components=(component,),
            expected_type=ExpectedPathType.DIRECTORY,
        )
```

在 `tests/unit/probes/test_import_boundaries.py` 用 AST 遍历 `src/project_memory_hub/probes/*.py`，把相对导入解析成绝对模块后，精确拒绝 `project_memory_hub.storage`、`project_memory_hub.adapters`、`project_memory_hub.services.capture`、`project_memory_hub.services.reconcile` 前缀：

```python
FORBIDDEN = (
    "project_memory_hub.storage",
    "project_memory_hub.adapters",
    "project_memory_hub.services.capture",
    "project_memory_hub.services.reconcile",
)


def test_probe_package_has_no_forbidden_imports() -> None:
    probe_root = Path("src/project_memory_hub/probes")
    violations: list[str] = []
    for path in sorted(probe_root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                dotted = "." * node.level + (node.module or "")
                base = importlib.util.resolve_name(dotted, "project_memory_hub.probes")
                names = [base, *(f"{base}.{alias.name}" for alias in node.names)]
            else:
                names = []
            violations.extend(
                f"{path}:{name}"
                for name in names
                if name is not None and name.startswith(FORBIDDEN)
            )
    assert violations == []
```

把上面的 AST 名称规范化循环提取为 `_resolved_imports(tree)`，并用两条负例证明相对导入和 parent alias 不能绕过：

```python
@pytest.mark.parametrize(
    "document",
    [
        "from ..storage import database",
        "from project_memory_hub import storage",
    ],
)
def test_forbidden_import_resolver_catches_relative_and_alias_forms(document: str) -> None:
    imported = _resolved_imports(ast.parse(document))
    assert any(name.startswith("project_memory_hub.storage") for name in imported)
```

- [ ] **Step 2: 运行测试，确认从缺失的 probes 包开始失败**

Run: `uv run pytest tests/unit/probes/test_models.py tests/unit/probes/test_import_boundaries.py -q`

Expected: FAIL，首个原因是 `ModuleNotFoundError: No module named 'project_memory_hub.probes'`。

- [ ] **Step 3: 实现严格枚举、模型、预算与固定路径类型**

`models.py` 必须定义规格中的全部稳定值，且 Pydantic 模型使用 `ConfigDict(extra="forbid", frozen=True, strict=True)`：

```python
class ProbeMode(StrEnum):
    LIGHT = "light"
    STRUCTURE = "structure"


class InstallationStatus(StrEnum):
    DETECTED = "detected"
    NOT_DETECTED = "not_detected"


class DataStatus(StrEnum):
    READABLE = "readable"
    BLOCKED = "blocked"
    MISSING = "missing"
    REJECTED = "rejected"


class ProbeCapability(StrEnum):
    PRESENCE_AND_ACCESS = "presence_and_access"
    STRUCTURE_METADATA = "structure_metadata"


class StructureStatus(StrEnum):
    NOT_RUN = "not_run"
    RECOGNIZED = "recognized"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class ModelStatus(StrEnum):
    NOT_CHECKED = "not_checked"
    UNVERIFIABLE = "unverifiable"


class ProbeWarningCode(StrEnum):
    SOURCE_MISSING = "source_missing"
    PERMISSION_BLOCKED = "permission_blocked"
    SYMLINK_REJECTED = "symlink_rejected"
    UNSAFE_FILE_TYPE = "unsafe_file_type"
    UNSUPPORTED_FORMAT = "unsupported_format"
    MALFORMED_METADATA = "malformed_metadata"
    INVALID_UTF8 = "invalid_utf8"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROBE_TIMEOUT = "probe_timeout"
    SOURCE_CHANGED = "source_changed"
    MODEL_ID_UNVERIFIABLE = "model_id_unverifiable"
    PROBE_BUSY = "probe_busy"
    PROBE_FAILED = "probe_failed"


@dataclass(frozen=True, slots=True)
class ProbeBudget:
    max_depth: int = 4
    max_entries: int = 2_048
    max_candidate_files: int = 64
    max_sqlite_candidates: int = 4
    max_sqlite_file_bytes: int = 64 * 1024 * 1024
    max_sqlite_total_bytes: int = 128 * 1024 * 1024
    max_sqlite_vm_steps: int = 100_000
    max_header_bytes: int = 64
    max_total_header_bytes: int = 4 * 1024
    max_schema_identifiers: int = 2_048
    structure_timeout_seconds: float = 3.0
    light_max_targets_per_source: int = 16
    light_all_timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        for name in (
            "max_depth",
            "max_entries",
            "max_candidate_files",
            "max_sqlite_candidates",
            "max_sqlite_file_bytes",
            "max_sqlite_total_bytes",
            "max_sqlite_vm_steps",
            "max_header_bytes",
            "max_total_header_bytes",
            "max_schema_identifiers",
            "light_max_targets_per_source",
        ):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError("probe budget integers must be positive")
        for name in ("structure_timeout_seconds", "light_all_timeout_seconds"):
            if type(getattr(self, name)) is not float or getattr(self, name) <= 0:
                raise ValueError("probe budget timeouts must be positive floats")


class SourceProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_agent: SourceAgent
    mode: ProbeMode = ProbeMode.LIGHT


class ProbeMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    checked_installation_marker_count: int = Field(default=0, ge=0, le=2**31 - 1)
    detected_installation_marker_count: int = Field(default=0, ge=0, le=2**31 - 1)
    checked_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    readable_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    blocked_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    missing_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    rejected_data_root_count: int = Field(default=0, ge=0, le=2**31 - 1)
    metadata_file_count: int = Field(default=0, ge=0, le=2**31 - 1)
    sqlite_candidate_count: int = Field(default=0, ge=0, le=2**31 - 1)
    schema_object_count: int = Field(default=0, ge=0, le=2**31 - 1)
    bounded_record_count: int | None = Field(default=None, ge=0, le=2**31 - 1)
    has_session_identifier: bool = False
    has_model_identifier_field: bool = False


class SourceProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    source_agent: SourceAgent
    mode: ProbeMode
    installation_status: InstallationStatus
    data_status: DataStatus
    capability: ProbeCapability
    structure_status: StructureStatus
    model_status: ModelStatus
    ingestion_allowed: Literal[False] = False
    metrics: ProbeMetrics
    warning_codes: tuple[ProbeWarningCode, ...] = ()
    checked_at: datetime

    @field_validator("warning_codes")
    @classmethod
    def normalize_warnings(
        cls, value: tuple[ProbeWarningCode, ...]
    ) -> tuple[ProbeWarningCode, ...]:
        return tuple(sorted(set(value), key=lambda item: item.value))

    @field_validator("checked_at")
    @classmethod
    def require_aware_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("checked_at must be timezone-aware")
        return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class LightInspection:
    installation_status: InstallationStatus
    data_status: DataStatus
    metrics: ProbeMetrics
    warning_codes: tuple[ProbeWarningCode, ...] = ()


@dataclass(frozen=True, slots=True)
class StructureInspection:
    installation_status: InstallationStatus
    data_status: DataStatus
    structure_status: StructureStatus
    metrics: ProbeMetrics
    warning_codes: tuple[ProbeWarningCode, ...] = ()
```

`base.py` 定义 `TrustedAnchor`、`ExpectedPathType`、冻结的 `TrustedPath`、`RecognizedSchema`、`SourceDescriptor`、`ProbeClock`、结构化 `ProbeFilesystem` protocol、`SourceProbe`、`InvalidProbeRequest` 和 `ProbeBusyError`。这样 Task 1 不导入尚未创建的 `filesystem.py`，也不产生循环依赖；Task 2 的 `SafeProbeFilesystem` 以结构类型满足该 protocol。系统 clock 只封装 `datetime.now(UTC)` 与 `time.monotonic()`：

```python
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


@dataclass(frozen=True, slots=True)
class SourceDescriptor:
    source_agent: SourceAgent
    installation_markers: tuple[TrustedPath, ...]
    data_roots: tuple[TrustedPath, ...]
    capability: ProbeCapability
    recognized_schemas: tuple[RecognizedSchema, ...] = ()


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


class InvalidProbeRequest(ValueError):
    pass


class ProbeBusyError(RuntimeError):
    pass
```

`ProbeFilesystem` 与 `SourceProbe` 的签名固定为：

```python
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
```

`TrustedPath.__post_init__` 对每个组件执行以下精确检查：

```python
def _validate_component(component: str) -> None:
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
```

`ProbeBudget.__post_init__` 对每个数字字段拒绝 `bool`、零和负数。模型测试另断言任何 metric 超过 `2**31 - 1` 都被拒绝；count query 超出该范围时省略值并返回 `budget_exceeded`，不能截断成伪造计数。`__init__.py` 只重导出外部需要的类型，不执行路径展开或文件访问。

- [ ] **Step 4: 运行模型与依赖测试，确认通过**

Run: `uv run pytest tests/unit/probes/test_models.py tests/unit/probes/test_import_boundaries.py -q`

Expected: PASS；静态依赖违规列表为空。

- [ ] **Step 5: 格式化、静态检查并提交契约**

Run: `uv run ruff format src/project_memory_hub/probes tests/unit/probes`

Run: `uv run ruff check src/project_memory_hub/probes tests/unit/probes && uv run mypy src/project_memory_hub/probes`

Expected: 两条命令均 exit 0。

```bash
git add src/project_memory_hub/probes tests/unit/probes
git commit -m "feat(probe): 锁定安全探针领域契约"
```

## Task 2: 实现五来源白名单与轻量 no-follow 检测

**Files:**

- Create: `src/project_memory_hub/probes/filesystem.py`
- Create: `src/project_memory_hub/probes/builtin.py`
- Create: `tests/unit/probes/test_builtin.py`
- Create: `tests/unit/probes/test_filesystem_light.py`

- [ ] **Step 1: 写精确白名单、排除项、聚合和“绝不枚举内容”的失败测试**

在 `test_builtin.py` 用完整 tuple 断言固定路径；至少包含以下负断言：

```python
def test_builtin_descriptors_exclude_unapproved_paths() -> None:
    descriptors = builtin_descriptors()
    serialized = repr(descriptors)
    assert "Workbuddy" not in serialized
    assert "Qoder.app" not in serialized
    assert "Application Support/Qoder" not in serialized
    assert "/.qoder" not in serialized
    assert "Claude.app" not in serialized
    assert "PATH" not in serialized
    assert descriptors[0].source_agent is SourceAgent.TRAE
    assert descriptors[0].recognized_schemas == ()
```

完整期望必须逐项覆盖规格 7.1–7.5 的 11 个安装标记和 14 个数据根；顺序固定为 Trae、WorkBuddy、Zcode、QoderWork、Claude Code。

在 `test_filesystem_light.py` 用真实临时目录，并把任何枚举/内容读取 monkeypatch 成立即失败：

```python
def test_light_probe_never_enumerates_or_reads_root_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path.resolve()
    (home / "safe-root").mkdir()
    descriptor = SourceDescriptor(
        source_agent=SourceAgent.WORKBUDDY,
        installation_markers=(),
        data_roots=(
            TrustedPath(
                TrustedAnchor.HOME,
                ("safe-root",),
                ExpectedPathType.DIRECTORY,
            ),
        ),
        capability=ProbeCapability.PRESENCE_AND_ACCESS,
    )

    def unexpected(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("light probe accessed directory contents")

    monkeypatch.setattr(filesystem_module.os, "scandir", unexpected)
    monkeypatch.setattr(filesystem_module.os, "pread", unexpected)
    clock = SystemProbeClock()
    inspection = SafeProbeFilesystem(PathSafetyPolicy(home=home)).inspect_light(
        descriptor,
        budget=ProbeBudget(),
        clock=clock,
        deadline=clock.monotonic() + 2.0,
    )
    assert inspection.data_status is DataStatus.READABLE


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        (("readable", "blocked", "rejected", "missing"), DataStatus.READABLE),
        (("blocked", "rejected", "missing"), DataStatus.BLOCKED),
        (("rejected", "missing"), DataStatus.REJECTED),
        (("missing",), DataStatus.MISSING),
    ],
)
def test_light_data_status_has_fixed_precedence(
    states: tuple[str, ...], expected: DataStatus
) -> None:
    assert aggregate_data_status(states) is expected


@pytest.mark.parametrize(
    ("marker_hits", "expected"),
    [
        ((False, False), InstallationStatus.NOT_DETECTED),
        ((True, False), InstallationStatus.DETECTED),
        ((False, True), InstallationStatus.DETECTED),
        ((True, True), InstallationStatus.DETECTED),
    ],
)
def test_installation_status_is_detected_when_any_marker_is_safe(
    marker_hits: tuple[bool, ...], expected: InstallationStatus
) -> None:
    assert aggregate_installation_status(marker_hits) is expected
```

把其余轻量行为拆成以下独立测试动作；每个函数只锁一个原因，失败后单独运行同名 node id：

- [ ] `test_light_probe_rejects_symlink_in_any_path_component`：参数化 anchor/intermediate/leaf symlink，均断言 rejected + `symlink_rejected`。
- [ ] `test_light_probe_rejects_fifo_socket_and_non_executable_cli_marker`：参数化 FIFO、Unix socket、无 execute bit 普通文件，断言 `unsafe_file_type`。
- [ ] `test_light_probe_maps_eacces_to_blocked`：monkeypatch `os.open` 对目标 leaf 抛 `EACCES`，断言 blocked + `permission_blocked`。
- [ ] `test_light_probe_rejects_preview_open_and_after_open_identity_changes`：分别替换 dev/inode/type/size/mtime，断言 `source_changed`。
- [ ] `test_bad_root_warning_does_not_hide_another_readable_root`：同一 descriptor 一个 symlink root、一个安全 root，断言 data readable 且保留 symlink warning。
- [ ] `test_light_probe_closes_every_descriptor_on_success_and_failure`：记录 open fd 集合，在正常与异常出口均断言为空。
- [ ] `test_light_probe_enforces_sixteen_targets_per_source`：第 17 个固定目标不调用 `os.stat/open`，结果含 `budget_exceeded`。
- [ ] `test_probe_all_light_shares_two_second_deadline`：Fake monotonic 在第二来源越界，后续来源返回 `probe_timeout` 且不增加 deadline。

- [ ] **Step 2: 运行轻量测试，确认缺少白名单和文件系统实现**

Run: `uv run pytest tests/unit/probes/test_builtin.py tests/unit/probes/test_filesystem_light.py -q`

Expected: FAIL，原因是 `builtin_descriptors` 或 `SafeProbeFilesystem` 尚未定义。

- [ ] **Step 3: 实现白名单描述符和受信锚点解析**

`builtin_descriptors()` 只能通过 `TrustedAnchor.FILESYSTEM_ROOT` 和 `TrustedAnchor.HOME` + 组件 tuple 表示路径；不要保存展开后的绝对路径。Claude Code marker 使用 `ExpectedPathType.EXECUTABLE_FILE`，其余 app marker/data root 使用目录类型。生产 Trae 描述符必须是：

```python
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
)
```

其余四个 descriptor 也必须直接写成代码常量，不能从配置或 PATH 扩展：

```python
WORKBUDDY_DESCRIPTOR = SourceDescriptor(
    source_agent=SourceAgent.WORKBUDDY,
    installation_markers=(_root("Applications", "WorkBuddy.app"),),
    data_roots=(
        _home("Library", "Application Support", "WorkBuddy"),
        _home(".workbuddy"),
    ),
    capability=ProbeCapability.PRESENCE_AND_ACCESS,
)
ZCODE_DESCRIPTOR = SourceDescriptor(
    source_agent=SourceAgent.ZCODE,
    installation_markers=(_root("Applications", "ZCode.app"),),
    data_roots=(
        _home("Library", "Application Support", "ZCode"),
        _home(".zcode"),
    ),
    capability=ProbeCapability.PRESENCE_AND_ACCESS,
)
QODERWORK_DESCRIPTOR = SourceDescriptor(
    source_agent=SourceAgent.QODERWORK,
    installation_markers=(_root("Applications", "QoderWork.app"),),
    data_roots=(
        _home("Library", "Application Support", "QoderWork"),
        _home(".qoderwork"),
    ),
    capability=ProbeCapability.PRESENCE_AND_ACCESS,
)
CLAUDE_CODE_DESCRIPTOR = SourceDescriptor(
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
```

`_root/_home` 只生成 directory `TrustedPath`，`_root_executable/_home_executable` 只生成 executable-file `TrustedPath`。轻量检查对 executable marker 要求 `stat.S_ISREG(mode)` 且 `mode & 0o111 != 0`；不能调用 PATH 搜索或跟随 Homebrew symlink。

- [ ] **Step 4: 实现 no-follow 轻量检查和稳定聚合**

`PathSafetyPolicy` 是只含绝对 `home: Path` 的冻结 dataclass；构造只做词法验证，不 `exists/lstat/open`。`SafeProbeFilesystem(policy)` 不在构造时访问文件系统；单元测试通过 monkeypatch 本模块的 `os.open/stat/fstat/scandir/pread/close` 记录 syscall：

```python
class _ProbeFilesystemError(RuntimeError):
    def __init__(self, code: ProbeWarningCode) -> None:
        super().__init__(code.value)
        self.code = code


@dataclass(frozen=True, slots=True)
class PathSafetyPolicy:
    home: Path

    def __post_init__(self) -> None:
        selected = Path(self.home)
        if not selected.is_absolute():
            raise ValueError("probe home must be absolute")
        object.__setattr__(self, "home", selected)


class SafeProbeFilesystem:
    def __init__(self, policy: PathSafetyPolicy) -> None:
        self._policy = policy
```

`inspect_light()` 对每个固定 marker/root 逐组件打开并立即关闭。构造 flags 前先确认平台实际提供 `O_NOFOLLOW`、`O_CLOEXEC`，目录还必须提供 `O_DIRECTORY`；缺少任一安全能力就返回 rejected/`unsupported_format`，不能把缺失 flag 当成 0 后继续。每一层使用以下旗标和身份检查，绝不能跟随 symlink：

```python
def _identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        stat.S_IFMT(value.st_mode),
        value.st_size,
        value.st_mtime_ns,
    )


def _open_verified_component(
    component: str,
    *,
    parent_fd: int,
    expected_directory: bool,
) -> int:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if expected_directory:
        flags |= os.O_DIRECTORY
    before = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISLNK(before.st_mode):
        raise _ProbeFilesystemError(ProbeWarningCode.SYMLINK_REJECTED)
    fd = os.open(component, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(fd)
        after = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
        if _identity(before) != _identity(opened) or _identity(opened) != _identity(after):
            raise _ProbeFilesystemError(ProbeWarningCode.SOURCE_CHANGED)
        return fd
    except BaseException:
        os.close(fd)
        raise
```

`_ProbeFilesystemError` 是只携带 `ProbeWarningCode` 的私有异常，不保存 path、component 或原异常正文。调用方在一个 `ExitStack` 中逐组件调用 `_open_verified_component()`，每次返回后立刻 `stack.callback(os.close, fd)`，再把 fd 作为下一层 parent；所以任一后续步骤失败都会关闭整条 fd 链。把 `ENOENT/ENOTDIR` 映射为 missing，`EACCES/EPERM` 映射为 blocked，`ELOOP` 和 symlink 映射为 rejected + `symlink_rejected`，非预期类型映射为 rejected + `unsafe_file_type`；未知异常只在对应来源结果中变成 `probe_failed`。安装状态独立按 `any(safe_marker_hits)` 聚合，任一安全 app/executable marker 存在就是 detected，全部未命中才是 not_detected；数据根可读不能反推软件已安装，安装 marker 存在也不能反推数据 readable。输出 inspection 只能含枚举、受限整数、布尔和 warning code，不能含 `Path`、组件名或异常对象。

- [ ] **Step 5: 跑轻量测试、回归领域测试并提交**

Run: `uv run pytest tests/unit/probes/test_builtin.py tests/unit/probes/test_filesystem_light.py tests/unit/probes/test_models.py -q`

Expected: PASS；fake syscall 记录中没有 `scandir`、`pread` 或目录内容访问。

```bash
git add src/project_memory_hub/probes tests/unit/probes
git commit -m "feat(probe): 增加固定来源轻量检测"
```

## Task 3: 实现 Trae 有界 fd-relative 候选遍历

**Files:**

- Modify: `src/project_memory_hub/probes/filesystem.py`
- Create: `tests/unit/probes/test_filesystem_structure.py`

- [ ] **Step 1: 写候选规则、预算、竞态、UTF-8 和关闭资源的失败测试**

测试必须用 synthetic syscall/临时树证明候选规则是“任一相对组件精确等于 `session_memory`，或叶节点 stem 精确等于 `session_memory`”：

```python
@pytest.mark.parametrize(
    ("components", "expected"),
    [
        (("session_memory", "cache.db"), True),
        (("nested", "session_memory.sqlite"), True),
        (("nested", "Session_Memory.sqlite"), False),
        (("nested", "session_memory_backup.sqlite"), False),
        (("nested", "my_session_memory.sqlite"), False),
    ],
)
def test_session_memory_candidate_rule(
    components: tuple[str, ...], expected: bool
) -> None:
    assert is_session_memory_candidate(components) is expected
```

把其余结构遍历行为拆成以下独立测试动作：

- [ ] `test_structure_walk_does_not_open_non_candidates`：记录 open/pread，非候选列表均为零。
- [ ] `test_json_jsonl_log_and_unknown_candidates_receive_header_only`：四类文件各只调用一次 64 B `pread`，不进入 parser/SQLite。
- [ ] `test_candidate_header_reads_never_exceed_per_file_and_total_budget`：参数化 65 个候选，断言每个 <=64 B、总和 <=4096 B。
- [ ] `test_depth_budget_stops_before_opening_depth_five`：深度 5 tree 只到 depth 4，并返回 `budget_exceeded`。
- [ ] `test_entry_budget_never_requests_entry_2049`：记录 iterator `next` 次数精确 <=2048。
- [ ] `test_candidate_budget_never_opens_candidate_65`：记录候选 open 次数精确 <=64。
- [ ] `test_structure_deadline_returns_only_probe_timeout`：fake monotonic 越界后没有 `budget_exceeded`。
- [ ] `test_structure_walk_rejects_symlink_and_preview_to_open_swaps`：参数化 symlink、directory swap、file swap，分别断言稳定 warning。
- [ ] `test_structure_walk_rejects_invalid_utf8_control_and_oversized_names`：参数化 surrogate/control/>255 B name，绝不打开内容。
- [ ] `test_structure_walk_closes_all_fds_after_timeout_and_partial_failure`：open fd tracker 在两类出口均为空。

- [ ] **Step 2: 运行结构遍历测试，确认红灯**

Run: `uv run pytest tests/unit/probes/test_filesystem_structure.py -q`

Expected: FAIL，原因是 `inspect_trae_structure` 与候选函数不存在。

- [ ] **Step 3: 实现先消费预算再处理的有界 DFS**

实现使用已打开 data-root fd 作为唯一遍历锚点。不要 `list(scandir)`，也不要读取预算外哨兵；达到条目上限后保守标记 `budget_exceeded` 并停止，不请求下一个 DirEntry：

```python
@dataclass(frozen=True, slots=True)
class Candidate:
    parent_fd: int
    leaf: str
    relative_components: tuple[str, ...]
    preview_identity: tuple[int, int, int, int, int]


remaining_entries = budget.max_entries - counters.entries
for entry in itertools.islice(iterator, remaining_entries):
    counters.entries += 1
    _check_deadline(clock, deadline)
    name = _strict_entry_name(entry.name)
    child = (*relative_components, name)
    preview = entry.stat(follow_symlinks=False)
    if stat.S_ISLNK(preview.st_mode):
        warnings.add(ProbeWarningCode.SYMLINK_REJECTED)
        continue
    if stat.S_ISDIR(preview.st_mode) and depth < budget.max_depth:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if not stat.S_ISDIR(before.st_mode):
            warnings.add(ProbeWarningCode.SOURCE_CHANGED)
            continue
        child_fd = os.open(name, directory_flags, dir_fd=directory_fd)
        try:
            opened = os.fstat(child_fd)
            after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except BaseException:
            os.close(child_fd)
            raise
        if not (
            _identity(preview)
            == _identity(before)
            == _identity(opened)
            == _identity(after)
        ):
            os.close(child_fd)
            warnings.add(ProbeWarningCode.SOURCE_CHANGED)
            continue
        pending.append((child_fd, child, depth + 1))
    elif stat.S_ISDIR(preview.st_mode):
        warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
        continue
    elif stat.S_ISREG(preview.st_mode) and is_session_memory_candidate(child):
        candidates.append(
            Candidate(
                parent_fd=os.dup(directory_fd),
                leaf=name,
                relative_components=child,
                preview_identity=_identity(preview),
            )
        )
if counters.entries == budget.max_entries:
    warnings.add(ProbeWarningCode.BUDGET_EXCEEDED)
```

其中 `directory_flags` 必须是 `O_RDONLY|O_DIRECTORY|O_NOFOLLOW|O_CLOEXEC`。`Candidate` 是私有冻结 dataclass，持有自己的 duplicated parent fd、经验证 leaf、仅供候选判定的相对组件和 DirEntry preview identity；header/SQLite 打开前后必须比较 preview→before lstat→fstat→after lstat 四方身份。后续只能用 `candidate.parent_fd + candidate.leaf` 打开，不能把路径重新解析为绝对路径；所有 pending/candidate fd 用 `ExitStack` 或显式 `try/finally` 关闭。

- [ ] **Step 4: 实现 header 预算、格式分类和竞态复核**

候选文件以 parent dirfd + leaf 打开，前后比较 dev/inode/type/size/mtime；只调用 `os.pread(fd, min(64, remaining_header_budget), 0)`。SQLite magic 精确匹配 `b"SQLite format 3\x00"`；JSON、JSONL、日志及未知格式只增加候选计数并返回 `unsupported_format`，不调用 JSON parser、不读取第二块内容。

- [ ] **Step 5: 跑结构遍历测试和全探针回归并提交**

Run: `uv run pytest tests/unit/probes/test_filesystem_structure.py tests/unit/probes/test_filesystem_light.py tests/unit/probes/test_builtin.py -q`

Expected: PASS；非候选读取次数为零，非 SQLite 候选除一次有界 header 外没有第二次内容读取。

```bash
git add src/project_memory_hub/probes/filesystem.py tests/unit/probes/test_filesystem_structure.py
git commit -m "feat(probe): 增加 Trae 有界结构遍历"
```

## Task 4: 实现 fd-only SQLite schema 元数据检查

**Files:**

- Modify: `src/project_memory_hub/probes/filesystem.py`
- Create: `tests/unit/probes/test_sqlite_metadata.py`

- [ ] **Step 1: 写只读 URI、sidecar、预算、查询白名单和生产空指纹测试**

测试用合成 SQLite，并通过 monkeypatch `sqlite3.connect` 记录 URI：

```python
def test_sqlite_connects_only_through_verified_dev_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "session_memory.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE synthetic(id INTEGER PRIMARY KEY)")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    preview = os.stat(database.name, dir_fd=parent_fd, follow_symlinks=False)
    candidate = Candidate(
        parent_fd=parent_fd,
        leaf=database.name,
        relative_components=(database.name,),
        preview_identity=_identity(preview),
    )
    real_connect = sqlite3.connect
    calls: list[tuple[str, bool]] = []

    def tracking_connect(name: str, *args: object, **kwargs: object) -> sqlite3.Connection:
        calls.append((name, kwargs.get("uri") is True))
        return real_connect(name, *args, **kwargs)

    monkeypatch.setattr(filesystem_module.sqlite3, "connect", tracking_connect)
    clock = SystemProbeClock()
    try:
        result = SqliteMetadataInspector(ProbeBudget(), clock).inspect(
            candidate,
            recognized_schemas=(),
            deadline=clock.monotonic() + 3.0,
        )
    finally:
        os.close(parent_fd)
    assert result.structure_status is StructureStatus.UNSUPPORTED
    assert len(calls) == 1
    assert re.fullmatch(r"file:/dev/fd/[0-9]+\?mode=ro&immutable=1", calls[0][0])
    assert calls[0][1] is True
    assert str(tmp_path) not in calls[0][0]
```

把其余 SQLite 行为拆成以下独立测试动作：

- [ ] `test_sqlite_identity_mismatch_never_calls_connect_or_fallback`：`/dev/fd` dev/inode 不同，connect 调用列表为空且原路径未出现。
- [ ] `test_sqlite_rejects_sidecars_before_and_after_read`：参数化 wal/shm/journal 与出现时机，均为 `source_changed`。
- [ ] `test_damaged_sqlite_is_malformed_without_exception_text`：损坏 magic 后 connect error 只映射 `malformed_metadata`。
- [ ] `test_fifth_sqlite_candidate_is_never_opened`：共享 counters 已为 4 时 open/connect 都为零，warning 为 `budget_exceeded`。
- [ ] `test_sqlite_single_and_total_file_byte_limits`：参数化 64 MiB+1 与共享 128 MiB+1 sparse stat，均不 connect。
- [ ] `test_sqlite_disables_extensions_and_sets_readonly_pragmas`：记录调用，精确包含 load_extension false、query_only on、trusted_schema off、busy_timeout 0。
- [ ] `test_sqlite_queries_only_allowlisted_metadata`：statement recorder 只接受 sqlite_schema/pragma_table_list/pragma_table_xinfo(?)。
- [ ] `test_sqlite_never_reads_application_tables_with_empty_fingerprints`：SQL 列表不含 synthetic table 的 SELECT/COUNT。
- [ ] `test_sqlite_vm_steps_and_deadline_interrupt_independently`：两组 fake clock/callback 分别只产生 `budget_exceeded` 与 `probe_timeout`。
- [ ] `test_sqlite_schema_identifier_budget_streams_at_2048`：cursor 第 2049 行不进入 normalized list，连接随即关闭。
- [ ] `test_sqlite_invalid_identifiers_are_never_returned`：surrogate/control/oversized schema 值只产生稳定 warning。
- [ ] `test_empty_production_fingerprint_set_keeps_every_schema_unsupported`：任意合成 schema 均 unsupported。
- [ ] `test_injected_reviewed_fingerprint_reaches_recognized_and_partial`：无降级 warning 为 recognized，invalid_utf8/source_changed 为 partial。
- [ ] `test_count_query_failures_omit_bounded_record_count`：timeout/VM/identity change 均断言 count is None 与对应 warning。
- [ ] `test_sqlite_connection_and_all_fds_close_before_return`：成功、malformed、timeout 三出口 tracker 均为空。

- [ ] **Step 2: 运行 SQLite 专项测试，确认红灯**

Run: `uv run pytest tests/unit/probes/test_sqlite_metadata.py -q`

Expected: FAIL，原因是 `SqliteMetadataInspector` 尚未定义。

`filesystem.py` 中固定 inspector 接口；内部 `_StructureCounters` 持有跨候选的文件数、总字节、VM steps 和 schema identifier 计数：

```python
@dataclass(slots=True)
class _StructureCounters:
    sqlite_candidates: int = 0
    sqlite_total_bytes: int = 0
    sqlite_vm_steps: int = 0
    schema_identifiers: int = 0


class SqliteMetadataInspector:
    def __init__(self, budget: ProbeBudget, clock: ProbeClock) -> None:
        self._budget = budget
        self._clock = clock

    def inspect(
        self,
        candidate: Candidate,
        *,
        recognized_schemas: tuple[RecognizedSchema, ...],
        deadline: float,
        counters: _StructureCounters | None = None,
    ) -> StructureInspection:
        selected_counters = counters if counters is not None else _StructureCounters()
        return self._inspect_verified_candidate(
            candidate,
            recognized_schemas=recognized_schemas,
            deadline=deadline,
            counters=selected_counters,
        )
```

`_inspect_verified_candidate()` 由 Step 3–4 的 sidecar、fd URI、PRAGMA、streaming schema 和 fingerprint 代码组成；所有出口返回只含稳定枚举/metrics/warnings 的 `StructureInspection`。

- [ ] **Step 3: 实现 sidecar/身份复核与唯一 SQLite 打开路径**

在候选 parent dirfd 中对 leaf、`leaf-wal`、`leaf-shm`、`leaf-journal` 使用 `stat(..., follow_symlinks=False)`；sidecar 存在即停止。验证 `/dev/fd/<fd>` 与候选 fd 的 `(st_dev, st_ino)` 完全一致后才调用：

```python
uri = f"file:/dev/fd/{candidate_fd}?mode=ro&immutable=1"
connection = sqlite3.connect(uri, uri=True, timeout=0.0)
connection.enable_load_extension(False)
connection.execute("PRAGMA query_only = ON")
connection.execute("PRAGMA trusted_schema = OFF")
connection.execute("PRAGMA busy_timeout = 0")
```

如果 `/dev/fd` 不存在、身份不符或 SQLite 无法从该 fd 打开，返回 `unsupported_format`；不得调用 `sqlite3.connect` 的原始路径 fallback。

- [ ] **Step 4: 实现 schema 查询白名单、VM deadline 和内部指纹**

查询语句固定在代码常量中；表名传入 table-valued pragma 的绑定参数，不能插值。两个 cursor 都逐行迭代，每一行在进入内存 tuple 前先检查 deadline 和 `max_schema_identifiers`，绝不 `fetchall()`：

```python
schema_rows: list[tuple[object, ...]] = []
table_cursor = connection.execute(
    "SELECT schema, name, type, ncol, wr, strict FROM pragma_table_list "
    "WHERE schema = 'main' ORDER BY name"
)
for table_row in table_cursor:
    _check_deadline(clock, deadline)
    _consume_schema_identifiers(table_row, counters, budget.max_schema_identifiers)
    schema_rows.append(_normalize_schema_row(table_row))
    table_name = table_row[1]
    column_cursor = connection.execute(
        "SELECT p.cid, p.name, p.type, p.\"notnull\", p.dflt_value, p.pk, p.hidden "
        "FROM pragma_table_xinfo(?) AS p ORDER BY p.cid",
        (table_name,),
    )
    for column_row in column_cursor:
        _check_deadline(clock, deadline)
        _consume_schema_identifiers(
            column_row, counters, budget.max_schema_identifiers
        )
        schema_rows.append(_normalize_schema_row(column_row))
```

`_consume_schema_identifiers` 在加入任何值前计数，达到 2,048 立即中断并映射 `budget_exceeded`；异常 UTF-8、控制字符或超长 identifier 映射 `invalid_utf8`/`malformed_metadata`，不带原值。progress callback 每批增加 VM step 计数并检查 monotonic deadline；任一达到上限时中断。规范化 tuple 仅在内存排序后计算 SHA-256，原始 identifier 和 fingerprint 不进入 `StructureInspection`、日志或异常。只有注入的 `RecognizedSchema` 精确匹配才设置能力位并执行该对象预审的固定 count query；生产 descriptor 的 tuple 为空，所以 0.2.0 不执行应用表 COUNT。

`structure_status` 只按固定矩阵计算：没有受支持 fingerprint 是 unsupported；至少一个受支持 fingerprint 且没有降级警告是 recognized；至少一个受支持 fingerprint 但同时出现 `permission_blocked`、`symlink_rejected`、`unsafe_file_type`、`unsupported_format`、`malformed_metadata`、`invalid_utf8`、`budget_exceeded`、`probe_timeout`、`source_changed` 或 `probe_failed` 中任一个是 partial。`model_id_unverifiable` 是结构结果恒有的隔离结论，不单独把 recognized 降为 partial；count query 失败不撤销 schema 识别，但必须省略 count 并使状态为 partial。

- [ ] **Step 5: 跑 SQLite、遍历和隐私回归并提交**

Run: `uv run pytest tests/unit/probes/test_sqlite_metadata.py tests/unit/probes/test_filesystem_structure.py -q`

Expected: PASS；connect URI 只有已验证 `/dev/fd`，应用表查询列表为空。

```bash
git add src/project_memory_hub/probes/filesystem.py tests/unit/probes/test_sqlite_metadata.py
git commit -m "feat(probe): 限制 SQLite 只读元数据检查"
```

## Task 5: 组装内置探针、结果局部化与结构 reservation

**Files:**

- Modify: `src/project_memory_hub/probes/builtin.py`
- Create: `src/project_memory_hub/probes/service.py`
- Modify: `src/project_memory_hub/probes/__init__.py`
- Create: `tests/unit/probes/test_service.py`

- [ ] **Step 1: 写稳定顺序、能力、局部失败和无阻塞并发测试**

并发测试必须用 `threading.Event`，不得依赖 sleep：

```python
def test_second_structure_reservation_is_busy_before_worker_creation(
    service: SourceProbeService,
) -> None:
    first = service.reserve_structure(SourceAgent.TRAE)
    with pytest.raises(ProbeBusyError) as error:
        service.reserve_structure(SourceAgent.TRAE)
    assert str(error.value) == "probe_busy"
    first.close()


def test_lease_releases_only_after_probe_resources_close(
    service_with_blocking_probe: tuple[SourceProbeService, BlockingProbe],
) -> None:
    service, probe = service_with_blocking_probe
    lease = service.reserve_structure(SourceAgent.TRAE)
    thread = Thread(target=lease.run)
    thread.start()
    probe.entered.wait(timeout=1)
    with pytest.raises(ProbeBusyError):
        service.reserve_structure(SourceAgent.TRAE)
    probe.release.set()
    thread.join(timeout=1)
    assert probe.open_resource_count == 0
    next_lease = service.reserve_structure(SourceAgent.TRAE)
    next_lease.close()
```

把其余 service/result 行为拆成以下独立测试动作：

- [ ] `test_registry_has_exact_five_source_order_and_rejects_duplicates`：精确比较 `OPTIONAL_PROBE_SOURCES`。
- [ ] `test_trae_capability_is_structure_metadata_in_both_modes`：light/structure 两结果 capability 相同。
- [ ] `test_other_sources_reject_structure_before_filesystem_access`：参数化四来源，filesystem call count 为零。
- [ ] `test_probe_all_light_localizes_one_source_failure`：第二来源抛异常，只第二结果是 not_detected/missing/probe_failed，其余正常。
- [ ] `test_probe_results_never_contain_private_strings`：model_dump/json 中不含注入 path/name/schema/exception text。
- [ ] `test_model_field_never_unlocks_ingestion`：has_model_identifier_field true 时仍 unverifiable + warning + false。
- [ ] `test_structure_status_matrix`：参数化 no fingerprint/clean fingerprint/degrading warnings，依次 unsupported/recognized/partial。
- [ ] `test_light_probe_does_not_acquire_structure_lock`：预占结构锁后 light 仍立即完成。
- [ ] `test_two_structure_calls_start_only_one_probe_body`：Event/Barrier 证明第二次 body call count 为零。
- [ ] `test_probe_busy_is_nonblocking_and_stable`：第二次 reservation 立即 `ProbeBusyError("probe_busy")`。
- [ ] `test_timeout_releases_lock_after_resources_close`：tracker 先归零，随后新 reservation 才成功。

- [ ] **Step 2: 运行 service 测试，确认红灯**

Run: `uv run pytest tests/unit/probes/test_service.py -q`

Expected: FAIL，原因是 registry、service 或 reservation lease 不存在。

- [ ] **Step 3: 实现 registry、轻量聚合和来源局部化**

`SourceProbeRegistry` 构造时拒绝重复来源，并保留内置 tuple 顺序。`probe_all_light()` 计算一个共享的 `clock.monotonic() + 2.0` deadline，逐来源调用；未知异常映射为该来源固定 `probe_failed` 结果，继续后续来源。

公开 service API 固定为：

```python
class SourceProbeRegistry:
    def __init__(self, probes: Iterable[SourceProbe]) -> None:
        self._probes = tuple(probes)
        sources = tuple(probe.descriptor.source_agent for probe in self._probes)
        if sources != OPTIONAL_PROBE_SOURCES or len(set(sources)) != len(sources):
            raise ValueError("probe registry must contain the five optional sources")
        self._by_source = dict(zip(sources, self._probes, strict=True))

    def get(self, source_agent: SourceAgent) -> SourceProbe:
        return self._by_source[source_agent]

    def all(self) -> tuple[SourceProbe, ...]:
        return self._probes


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
        return self._probe_all_light_with_shared_deadline()

    def probe_one(
        self,
        source_agent: SourceAgent,
        *,
        mode: ProbeMode = ProbeMode.LIGHT,
    ) -> SourceProbeResult:
        return self._probe_validated(source_agent, mode)

    def reserve_structure(self, source_agent: SourceAgent) -> StructureProbeLease:
        return self._reserve_validated(source_agent)
```

构造函数在建立 `_by_source` 时拒绝重复、Codex/ChatGPT 或顺序不等于 `OPTIONAL_PROBE_SOURCES` 的 registry；上面私有方法分别实现本步骤已经锁定的 deadline、局部 fallback 和模式校验，不新增持久化依赖。

在 `builtin.py` 固定并导出来源顺序，且由 descriptor 生成无状态探针：

```python
OPTIONAL_PROBE_SOURCES: Final = (
    SourceAgent.TRAE,
    SourceAgent.WORKBUDDY,
    SourceAgent.ZCODE,
    SourceAgent.QODERWORK,
    SourceAgent.CLAUDE_CODE,
)


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
            inspection = filesystem.inspect_trae_structure(
                self.descriptor,
                budget=budget,
                clock=clock,
                deadline=deadline,
            )
            warnings = (*inspection.warning_codes, ProbeWarningCode.MODEL_ID_UNVERIFIABLE)
            return SourceProbeResult(
                source_agent=request.source_agent,
                mode=request.mode,
                installation_status=inspection.installation_status,
                data_status=inspection.data_status,
                capability=self.descriptor.capability,
                structure_status=inspection.structure_status,
                model_status=ModelStatus.UNVERIFIABLE,
                ingestion_allowed=False,
                metrics=inspection.metrics,
                warning_codes=warnings,
                checked_at=checked_at,
            )
        inspection = filesystem.inspect_light(
            self.descriptor,
            budget=budget,
            clock=clock,
            deadline=deadline,
        )
        return SourceProbeResult(
            source_agent=request.source_agent,
            mode=request.mode,
            installation_status=inspection.installation_status,
            data_status=inspection.data_status,
            capability=self.descriptor.capability,
            structure_status=StructureStatus.NOT_RUN,
            model_status=ModelStatus.NOT_CHECKED,
            ingestion_allowed=False,
            metrics=inspection.metrics,
            warning_codes=inspection.warning_codes,
            checked_at=checked_at,
        )


def build_builtin_probes() -> tuple[SourceProbe, ...]:
    return tuple(BuiltinSourceProbe(descriptor) for descriptor in builtin_descriptors())
```

局部未知异常的 fallback 固定为 `installation_status=NOT_DETECTED`、`data_status=MISSING`、`structure_status=NOT_RUN`、`warning_codes=(PROBE_FAILED,)`，避免异常字符串进入结果；其它正常缺失结果还应使用 `source_missing`。

结构模式映射固定为：

```python
model_status = ModelStatus.UNVERIFIABLE
warnings.add(ProbeWarningCode.MODEL_ID_UNVERIFIABLE)
return SourceProbeResult(
    source_agent=SourceAgent.TRAE,
    mode=ProbeMode.STRUCTURE,
    installation_status=inspection.installation_status,
    data_status=inspection.data_status,
    capability=ProbeCapability.STRUCTURE_METADATA,
    structure_status=inspection.structure_status,
    model_status=model_status,
    ingestion_allowed=False,
    metrics=inspection.metrics,
    warning_codes=tuple(warnings),
    checked_at=checked_at,
)
```

- [ ] **Step 4: 实现先 reservation 后 worker 的一次性 lease**

`reserve_structure()` 用 `threading.Lock.acquire(blocking=False)`；失败立即抛 `ProbeBusyError("probe_busy")`。`StructureProbeLease.run()` 只能执行一次并在最外层 finally 释放，`close()` 只在尚未开始时幂等释放：

```python
class StructureProbeLease:
    def run(self) -> SourceProbeResult:
        with self._state_lock:
            if self._state != "reserved":
                raise RuntimeError("structure lease is not runnable")
            self._state = "running"
        try:
            return self._run_probe()
        finally:
            with self._state_lock:
                self._state = "closed"
            self._reservation_lock.release()

    def close(self) -> None:
        with self._state_lock:
            if self._state != "reserved":
                return
            self._state = "closed"
        self._reservation_lock.release()
```

同步 CLI 的 `probe_one(..., mode=STRUCTURE)` 可以执行 `reserve_structure(source).run()`；Web 必须显式先 reserve，再把 `lease.run` 传给 `to_thread`。

- [ ] **Step 5: 跑 service、filesystem 与模型回归并提交**

Run: `uv run pytest tests/unit/probes -q`

Expected: PASS；并发断言无需时间等待重试即可稳定通过。

```bash
git add src/project_memory_hub/probes tests/unit/probes
git commit -m "feat(probe): 增加来源聚合与并发闸门"
```

## Task 6: 装配独立零写入 ProbeContainer

**Files:**

- Modify: `src/project_memory_hub/container.py:63-104,166-383`
- Create: `tests/integration/test_probe_container.py`

- [ ] **Step 1: 写专用容器零副作用和完整容器接缝测试**

核心测试应把写路径替换成一旦调用就失败：

```python
def _unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("zero-write probe builder touched runtime state")


def test_build_probe_container_does_not_touch_runtime_or_probe_on_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "must-not-exist"
    monkeypatch.setenv("PROJECT_MEMORY_HUB_HOME", str(runtime))
    monkeypatch.setattr(RuntimePaths, "ensure", _unexpected_call)
    monkeypatch.setattr(ConfigManager, "load", _unexpected_call)
    monkeypatch.setattr(ConfigManager, "save", _unexpected_call)
    monkeypatch.setattr(Database, "initialize", _unexpected_call)

    container = build_probe_container(config_path=tmp_path / "ignored.toml", home=tmp_path)
    container.close()
    container.close()

    assert not runtime.exists()
    assert tuple(field.name for field in dataclasses.fields(container)) == ("source_probes",)
```

另测：构造期间不调用 probe；注入 home 不展开任意 config 路径；完整 `build_container(..., probe_home=tmp_path)` 暴露同一类 `SourceProbeService`；完整 adapter registry 仍只有 Codex/ChatGPT；探针对象没有 container/database/config/repository 反向字段。

- [ ] **Step 2: 运行容器测试，确认 ServiceContainer 缺少探针字段**

Run: `uv run pytest tests/integration/test_probe_container.py -q`

Expected: FAIL，原因是 `ProbeContainer`/`build_probe_container` 未定义或 `ServiceContainer` 没有 `source_probes`。

- [ ] **Step 3: 实现共享纯构造 helper 与两个容器入口**

在 `container.py` 增加：

```python
@dataclass(slots=True)
class ProbeContainer:
    source_probes: SourceProbeService

    def close(self) -> None:
        return None


def _build_source_probe_service(*, home: Path, clock: ProbeClock) -> SourceProbeService:
    probes = build_builtin_probes()
    return SourceProbeService(
        SourceProbeRegistry(probes),
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
```

给 `ServiceContainer` 增加 `source_probes` 字段；`build_container(config_path, *, probe_home=None)` 在已有 runtime/config/database 初始化不变的前提下构造探针服务，但不自动调用任何 probe。

- [ ] **Step 4: 跑容器测试和既有 adapter/doctor 回归**

Run: `uv run pytest tests/integration/test_probe_container.py tests/integration/test_doctor.py tests/integration/test_codex_adapter.py tests/integration/test_chatgpt_adapter.py -q`

Expected: PASS；probe-only 测试的 runtime 路径仍不存在。

- [ ] **Step 5: 提交容器接缝**

```bash
git add src/project_memory_hub/container.py tests/integration/test_probe_container.py
git commit -m "feat(probe): 装配零写入来源探针容器"
```

## Task 7: 增加 `memory-hub source probe` CLI

**Files:**

- Modify: `src/project_memory_hub/cli.py:61-96,387-516,1074-1130,1321-1352`
- Create: `tests/integration/test_probe_cli.py`

- [ ] **Step 1: 写参数矩阵、稳定 JSON、错误边界和零写入失败测试**

参数化非法输入并确认在 builder 调用前返回 exit 4：

```python
def _unexpected_call(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("invalid probe input constructed a container")


@pytest.mark.parametrize(
    "args",
    [
        ["source", "probe", "--format", "json"],
        ["source", "probe", "trae", "--all", "--format", "json"],
        ["source", "probe", "--all", "--structure", "--format", "json"],
        ["source", "probe", "workbuddy", "--structure", "--format", "json"],
        ["source", "probe", "codex", "--format", "json"],
        ["source", "probe", "chatgpt", "--format", "json"],
        ["source", "probe", "not-a-source", "--format", "json"],
    ],
)
def test_probe_rejects_invalid_combinations_before_build(
    args: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "build_probe_container", _unexpected_call)
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 4
    assert json.loads(result.stdout)["error"]["code"] == "invalid_input"
    assert "not-a-source" not in result.stdout
```

成功 JSON 统一为 `{"status":"ok","results":[...]}`，单来源也用长度 1 的 results；`--all` 顺序固定五来源。文本模式逐来源显示与页面相同的 Source、Detected、Probe health、Model identity、Structure、Behavior import 和稳定 warnings，不得只输出 `ok`。再测缺失/blocked/rejected/unsupported 都 exit 0；单来源未知异常变成正常 `probe_failed` result；busy 是顶层 `probe_busy` exit 2；基础设施/JSON/text 编码失败是脱敏 `operation_failed` exit 1；两种输出都不含绝对路径、异常、schema、正文；container 成功/失败都 close；不存在 runtime 执行后仍不存在；已有 config bytes、DB SHA-256、关键表行数和文件清单前后相同。

- [ ] **Step 2: 运行 CLI 测试，确认命令尚不存在**

Run: `uv run pytest tests/integration/test_probe_cli.py -q`

Expected: FAIL，根帮助中没有 `source` 或命令返回 no such command。

- [ ] **Step 3: 注册子组并在构造容器前校验所有参数**

```python
source_app = typer.Typer(no_args_is_help=True)
app.add_typer(source_app, name="source", help="Inspect optional local sources.")


@dataclass(frozen=True, slots=True)
class ProbeCliRequest:
    source: SourceAgent | None
    all_sources: bool
    mode: ProbeMode


def _probe_request(source: str | None, all_sources: bool, structure: bool) -> ProbeCliRequest:
    if (source is None) == (not all_sources):
        raise _CliFailure("invalid_input", "Invalid source probe request.", 4)
    if all_sources and structure:
        raise _CliFailure("invalid_input", "Invalid source probe request.", 4)
    if source is None:
        return ProbeCliRequest(source=None, all_sources=True, mode=ProbeMode.LIGHT)
    try:
        selected = SourceAgent(source)
    except ValueError:
        raise _CliFailure("invalid_input", "Invalid source probe request.", 4) from None
    if selected not in OPTIONAL_PROBE_SOURCES or (
        structure and selected is not SourceAgent.TRAE
    ):
        raise _CliFailure("invalid_input", "Invalid source probe request.", 4)
    return ProbeCliRequest(
        source=selected,
        all_sources=False,
        mode=ProbeMode.STRUCTURE if structure else ProbeMode.LIGHT,
    )
```

source 用 `str | None` 手工校验，避免 Typer help 把 Codex/ChatGPT 展示成可探测来源。command 先 `_validate_format()`，再用窄 try 调 `_probe_request()`；捕获 `_CliFailure` 后立即 `_emit_error()` + `typer.Exit`，所以非法请求不会进入 `_run()` 或创建 container。

完整命令主体固定为：

```python
@source_app.command("probe")
def source_probe_command(
    ctx: typer.Context,
    source: str | None = typer.Argument(None),
    all_sources: bool = typer.Option(False, "--all"),
    structure: bool = typer.Option(False, "--structure"),
    output_format: str = typer.Option("text", "--format"),
) -> None:
    """Inspect fixed optional sources without writing Project Memory Hub state."""
    _validate_format(output_format)
    try:
        request = _probe_request(source, all_sources, structure)
    except _CliFailure as error:
        _emit_error(output_format, error.code, error.message)
        raise typer.Exit(error.exit_code) from None

    def operation(container: ProbeContainer) -> dict[str, Any]:
        try:
            results = (
                container.source_probes.probe_all_light()
                if request.all_sources
                else (
                    container.source_probes.probe_one(
                        cast(SourceAgent, request.source),
                        mode=request.mode,
                    ),
                )
            )
        except ProbeBusyError:
            raise _CliFailure("probe_busy", "Source probe is busy.", 2) from None
        return {
            "results": [result.model_dump(mode="json") for result in results],
            "status": "ok",
        }

    _run(
        ctx,
        output_format,
        operation,
        builder=_build_cli_probe_container,
        text_renderer=_probe_text,
    )
```

`cast(SourceAgent, request.source)` 只位于已经通过 XOR 校验的单来源分支；它不改变运行时值，也不允许 Codex/ChatGPT 进入。

- [ ] **Step 4: 用专用 builder 执行并封闭输出编码异常**

CLI wrapper 必须显式丢弃全局 config：

```python
def _build_cli_probe_container(_config_path: Path | None) -> ProbeContainer:
    return build_probe_container()
```

把 `_run` 泛化为 `_ContainerT = TypeVar("_ContainerT")`，其 `operation` 是 `Callable[[_ContainerT], dict[str, Any]]`、显式 builder 是 `Callable[[Path | None], _ContainerT]`；默认 builder 分支用受限 `cast(_ContainerT, build_container(...))`，让 `ProbeContainer` operation 在 mypy strict 下成立。调用 `_run(..., builder=_build_cli_probe_container)`，不能依赖默认 builder。

把“序列化”和“写 stdout”分成两阶段：主 try 内用纯 `_render_response()` 生成字符串但不输出，随后 finally 关闭 container，只有 close 成功后才调用一次 `typer.echo(rendered)`。这样 close 失败时只输出现有稳定 error JSON，不会先输出 success 再输出 error：

```python
def _render_response(
    output_format: str,
    response: dict[str, Any],
    text_renderer: Callable[[dict[str, Any]], str] | None,
) -> str:
    if output_format == "json":
        return json.dumps(
            response,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    if text_renderer is not None:
        return text_renderer(response)
    return str(response.get("status", "ok"))
```

probe command 必须显式传入专用白名单 renderer：

```python
def _probe_text(response: dict[str, Any]) -> str:
    lines: list[str] = []
    for item in response["results"]:
        warnings = ",".join(item["warning_codes"]) or "none"
        lines.append(
            " | ".join(
                (
                    _PROBE_SOURCE_LABELS[item["source_agent"]],
                    f"Detected: {_INSTALLATION_LABELS[item['installation_status']]}",
                    f"Probe health: {_DATA_LABELS[item['data_status']]}",
                    f"Model identity: {_MODEL_LABELS[item['model_status']]}",
                    f"Structure: {_STRUCTURE_LABELS[item['structure_status']]}",
                    "Behavior import: Locked",
                    f"Warnings: {warnings}",
                )
            )
        )
    return "\n".join(lines)
```

五个 mapping 的 key 只包含领域 enum 的 `.value`，value 与控制台标签完全相同；调用 `_run(..., text_renderer=_probe_text, builder=_build_cli_probe_container)`。CLI 测试对一个 readable Trae 和一个 missing WorkBuddy 断言整行文本，并再次注入路径/schema/异常正文确认都不出现。

`_render_response()` 的 `TypeError`、`ValueError`、`UnicodeError` 在主 try 内转为 `_CliFailure("operation_failed", "Operation failed.", 1)`；finally 因 pending error 不重复输出。close 成功后的单次 `typer.echo()` 若发生上述异常，再输出 ASCII 稳定 `operation_failed` 并 exit 1。`ProbeBusyError` 映射 `probe_busy`/exit 2；其它来源级失败必须已由 service 局部化。

- [ ] **Step 5: 跑 CLI、version 零副作用与静态检查并提交**

Run: `uv run pytest tests/integration/test_probe_cli.py tests/integration/test_cli_core.py tests/integration/test_cli_proposals.py -q`

Expected: PASS；全局 `--config` 指向不存在路径后该路径仍不存在；现有 container-close failure 只输出一个 error document，proposal 命令输出没有回归。

Run: `uv run ruff check src/project_memory_hub/cli.py tests/integration/test_probe_cli.py && uv run mypy src/project_memory_hub`

Expected: exit 0。

```bash
git add src/project_memory_hub/cli.py tests/integration/test_probe_cli.py
git commit -m "feat(cli): 增加安全来源探针命令"
```

## Task 8: 在控制台 GET 中展示瞬时轻量结果

**Files:**

- Modify: `src/project_memory_hub/services/control.py:38-79,175-239`
- Modify: `src/project_memory_hub/web/routes.py:31-52,357-365`
- Modify: `src/project_memory_hub/web/templates/sources.html`
- Modify: `src/project_memory_hub/web/static/app.css:68-103`
- Modify: `tests/integration/test_web_routes.py`

- [ ] **Step 1: 写 GET、视图白名单和原控制权限不变的失败测试**

把 GET/视图行为拆成以下独立测试动作：

- [ ] `test_sources_get_runs_only_light_probes`：light call count 1，reserve/structure count 0。
- [ ] `test_sources_get_localizes_probe_failure`：一个 probe_failed 时 HTTP 200 且其它四行正常。
- [ ] `test_registered_sources_preserve_enable_disable_controls`：Codex/ChatGPT 原 action/desired/runtime 文本不变。
- [ ] `test_optional_sources_never_render_enable_or_import_controls`：五行均 Locked 且无 enable/import action。
- [ ] `test_trae_button_requires_readable_data_root`：参数化 readable/blocked/missing/rejected，只有 readable button enabled。
- [ ] `test_optional_source_labels_use_enum_whitelists`：每个 label/class 与固定 mapping 精确相等。
- [ ] `test_sources_page_never_renders_private_probe_metadata`：注入 path/schema/exception/body sentinel 均不在 HTML。

先修改 `tests/integration/test_web_routes.py::_container()`：为每个 tmp_path 创建空的 `probe-home`，并调用 `build_container(config_path, probe_home=probe_home)`。所有 GET/POST 测试必须通过该 helper，禁止默认 `Path.home()` 检测真实电脑。

```python
def test_optional_sources_never_render_enable_or_import_controls(tmp_path: Path) -> None:
    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            _app, client, _csrf = await _client(container)
            async with client:
                return await client.get("/sources")

    response = asyncio.run(scenario())
    assert response.status_code == 200
    for source in ("trae", "workbuddy", "zcode", "qoderwork", "claude_code"):
        match = re.search(
            rf'<tr data-source="{source}">(.*?)</tr>', response.text, flags=re.DOTALL
        )
        assert match is not None
        row = match.group(1)
        assert f'action="/sources/{source}/enable"' not in row
        assert "Import" not in row
        assert "Locked" in row
```

- [ ] **Step 2: 运行 Sources GET 测试，确认页面没有探针字段**

Run: `uv run pytest tests/integration/test_web_routes.py -k 'sources_get or optional_sources' -q`

Expected: FAIL，页面缺少 Detected/Probe health/Model identity/Behavior import。

- [ ] **Step 3: 增加纯白名单视图模型映射**

新增冻结 `SourceProbeControlRecord`，所有 label 只通过 enum-keyed 常量表获得：

```python
_DATA_LABELS = {
    DataStatus.READABLE: "Readable",
    DataStatus.BLOCKED: "Permission blocked",
    DataStatus.MISSING: "Missing",
    DataStatus.REJECTED: "Rejected",
}


@dataclass(frozen=True, slots=True)
class SourceProbeControlRecord:
    detected_label: str
    detected_class: Literal["detected", "not-detected"]
    health_label: str
    health_class: Literal["readable", "blocked", "missing", "rejected", "probe-busy"]
    model_label: str
    model_class: Literal["not-checked", "unverifiable"]
    capability_label: str
    structure_label: str
    warning_codes: tuple[str, ...]
    behavior_import_locked: bool
    behavior_class: Literal["locked"]
    can_run_structure: bool
```

`SourceControlRecord` 增加 `probe: SourceProbeControlRecord | None`。`ControlPanelService.sources(probe_results, *, probe_error=None)` 只映射传入结果：Codex/ChatGPT probe 为 None；可选来源永远 locked；只有 Trae + readable 为 can_run_structure。label 和 class 都由 enum/固定 error-keyed 常量表产生，模板直接使用 `health_class` 等稳定 key；不要从 label `|lower` 推导 CSS，也不要把异常、路径或 schema 字符串直接传给模板。

- [ ] **Step 4: GET 在线程中执行轻量检测并渲染扩展表格**

路由使用 `await asyncio.to_thread(container.source_probes.probe_all_light)`；模板保留原五列的语义，并为可选来源增加 Detected、Probe health、Model identity、Behavior import 和稳定 warning code。其它四来源只显示 `Presence and access check`。CSS 使用 `.status.readable/.blocked/.rejected/.locked`，不要对带空格 label 使用 `|lower` 生成 class。

- [ ] **Step 5: 跑 Sources GET、控制回归并提交**

Run: `uv run pytest tests/integration/test_web_routes.py -k 'sources or source' -q`

Expected: PASS；Codex/ChatGPT 原启停断言不变，五可选来源无控制表单。

```bash
git add src/project_memory_hub/services/control.py src/project_memory_hub/web/routes.py src/project_memory_hub/web/templates/sources.html src/project_memory_hub/web/static/app.css tests/integration/test_web_routes.py
git commit -m "feat(web): 展示可选来源探针状态"
```

## Task 9: 增加 Trae POST、并发 409、刷新复位与 Web 安全验收

**Files:**

- Modify: `src/project_memory_hub/web/routes.py:44-70,357-365`
- Modify: `src/project_memory_hub/web/templates/sources.html`
- Create: `src/project_memory_hub/web/static/sources.js`
- Modify: `tests/integration/test_web_routes.py`
- Modify: `tests/integration/test_web_security.py`
- Modify: `tests/e2e/test_dashboard.py`

- [ ] **Step 1: 写 POST 请求内结果、先 reserve、409 和关闭 lease 的失败测试**

精确锁定调用顺序：

```python
class BusyProbeService:
    def __init__(self) -> None:
        self.structure_calls = 0

    def reserve_structure(self, _source: SourceAgent) -> NoReturn:
        raise ProbeBusyError("probe_busy")

    def probe_all_light(self) -> NoReturn:
        raise AssertionError("busy route must not run a light probe")


def test_trae_probe_busy_returns_409_without_starting_worker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probes = BusyProbeService()

    def unexpected_to_thread(*_args: object, **_kwargs: object) -> NoReturn:
        raise AssertionError("busy route created a worker")

    monkeypatch.setattr(asyncio, "to_thread", unexpected_to_thread)

    async def scenario() -> httpx.Response:
        with _container(tmp_path) as container:
            container.source_probes = probes  # type: ignore[assignment]
            _app, client, csrf = await _client(container)
            async with client:
                return await client.post(
                    "/sources/trae/probe",
                    headers=_unsafe(csrf),
                    data={"csrf_token": csrf},
                )

    response = asyncio.run(scenario())
    assert response.status_code == 409
    assert "probe_busy" in response.text
    assert probes.structure_calls == 0
```

把其余 POST 行为拆成以下独立测试动作：

- [ ] `test_trae_probe_post_renders_structure_result_without_redirect`：HTTP 200 body 为 structure，Location 缺失。
- [ ] `test_trae_probe_reserves_before_to_thread`：调用日志精确为 reserve→worker start→worker close。
- [ ] `test_trae_probe_response_waits_for_lease_close`：发送 response 前 lease state 已 closed。
- [ ] `test_trae_probe_scheduler_failure_closes_unstarted_lease`：create_task 抛错后 reservation 可再次取得。
- [ ] `test_trae_probe_cancellation_waits_for_running_worker`：取消 route 后先等 fd/connection tracker 归零，再收到 CancelledError。
- [ ] `test_trae_probe_followup_get_returns_light`：POST structure 后 GET model label 恢复 Not checked。
- [ ] `test_trae_probe_post_writes_no_session_cookie_database_or_cache`：Set-Cookie 缺失且持久化快照相等。

- [ ] **Step 2: 写安全边界和浏览器刷新失败测试**

把安全边界拆成以下独立参数化测试动作，所有拒绝 case 都断言 reserve/lease call count 为零：

- [ ] `test_trae_probe_requires_bootstrap_loopback_host_origin_and_csrf`：参数化五类边界，断言现有稳定 401/403。
- [ ] `test_trae_probe_applies_body_and_field_limits_before_reservation`：超 body、超 field count、重复 csrf、额外 field 均为 400/413。
- [ ] `test_trae_probe_form_contains_exactly_one_csrf_field`：解析 template form 后 field names 精确为单个 csrf_token。

在 Playwright 新增 synthetic `probe_home`，点击后断言 URL 是 `/sources`、显示 `Unverifiable`、`model_id_unverifiable`、`Locked`；reload 后恢复 `Not checked`；前后 DB digest/config bytes/关键表行数不变；页面不含合成正文、绝对路径或 schema identifier。

具体修改 `tests/e2e/test_dashboard.py` 的 `_SERVER_SCRIPT`：第三个 argv 读取为 `Path` 并传给 `build_container(config_path, probe_home=probe_home)`；`_uvicorn_subprocess` 创建独立 `probe-home` 并把它作为第三个子进程参数。浏览器测试只在该目录构造 `~/.trae/session_memory` fixture，禁止继承真实 HOME。

- [ ] **Step 3: 运行 POST、安全和 E2E 测试，确认红灯**

Run: `uv run pytest tests/integration/test_web_routes.py -k 'trae_probe' -q`

Run: `uv run pytest tests/integration/test_web_security.py -k 'trae_probe' -q`

Run: `uv run pytest tests/e2e/test_dashboard.py -k 'source_probe or bootstrap_sources' -q`

Expected: 三组均 FAIL；POST 路由和 external script 尚不存在。

- [ ] **Step 4: 实现有限空表单、非阻塞 reservation 和请求内渲染**

先 `form = await limited_form(request)`，再要求 field 集合精确等于 `{"csrf_token"}` 且单值；之后才 reserve。锁忙时用 Sources 模板返回 409，不抛给纯文本 HTTPException handler。扩展 `_render(..., response_status=200)` 把 status 传给 `TemplateResponse`。

```python
def _render(
    request: Request,
    name: str,
    *,
    response_status: int = 200,
    **context: Any,
) -> Response:
    return _TEMPLATES.TemplateResponse(
        request=request,
        name=name,
        context={**context, "csrf_token": request.state.pmh_csrf},
        status_code=response_status,
    )
```

```python
lease = container.source_probes.reserve_structure(SourceAgent.TRAE)
try:
    worker = asyncio.create_task(asyncio.to_thread(lease.run))
except BaseException:
    lease.close()
    raise
try:
    structure_result = await asyncio.shield(worker)
except asyncio.CancelledError:
    try:
        await worker
    except (Exception, asyncio.CancelledError):
        pass
    raise
except BaseException:
    lease.close()
    raise
light_results = await asyncio.to_thread(container.source_probes.probe_all_light)
results = tuple(
    structure_result if item.source_agent is SourceAgent.TRAE else item
    for item in light_results
)
return _render(
    request,
    "sources.html",
    title="Sources",
    sources=control.sources(results),
    probe_request_complete=True,
)
```

不要使用 `asyncio.wait_for`。`shield + await worker` 只负责在 route cancellation 时等待内部 3 秒 deadline 和资源关闭，不延长探针预算。`ProbeBusyError` 分支不能运行任何探针或调用任何 `asyncio.to_thread()`；它用纯视图映射 `control.sources((), probe_error="probe_busy")` 渲染 409，Codex/ChatGPT 控制仍来自现有 config，五可选来源显示 Not checked/Locked，Trae 显示稳定 `probe_busy`。这既不缓存上次结果，也不为 busy 请求创建轻量或结构 worker。

- [ ] **Step 5: 用同源外部脚本把 POST 历史替换成 GET 地址**

只在 `probe_request_complete` 或 busy 响应时加载 `/static/sources.js`。脚本不使用 storage/cookie/fetch，只把浏览器历史改为 canonical GET 地址：

```javascript
"use strict";

if (window.location.pathname === "/sources/trae/probe") {
  window.history.replaceState(null, "", "/sources");
}
```

现有 CSP `script-src 'self'` 允许该文件，不增加 inline script 或 CSP 例外。这样 POST 响应仍直接渲染，刷新会 GET `/sources` 并恢复 light。

- [ ] **Step 6: 跑 Web 全量、安全、E2E 和零写入测试并提交**

Run: `uv run pytest tests/integration/test_web_routes.py tests/integration/test_web_security.py -q`

Run: `uv run pytest tests/e2e/test_dashboard.py -k 'source_probe or bootstrap_sources' -q`

Expected: PASS；busy 不创建 worker，reload 回到 Not checked，持久化快照不变。

```bash
git add src/project_memory_hub/web/routes.py src/project_memory_hub/web/templates/sources.html src/project_memory_hub/web/static/sources.js tests/integration/test_web_routes.py tests/integration/test_web_security.py tests/e2e/test_dashboard.py
git commit -m "feat(web): 增加 Trae 安全结构检测"
```

## Task 10: 发布 0.2.0、文档化边界并完成发布门禁

**Files:**

- Create: `scripts/verify_wheel.py`
- Create: `scripts/verify_probe_zero_write.py`
- Modify: `pyproject.toml:1-3`
- Modify: `src/project_memory_hub/__init__.py:1`
- Modify: `tests/integration/test_cli_core.py:278-285`
- Modify: `tests/integration/test_probe_container.py`
- Modify: `README.md:38-52`
- Modify: `docs/operations.md:14-28,194-240`

- [ ] **Step 1: 先把版本测试改为 0.2.0 并增加 wheel 内容断言**

版本测试继续断言 `memory-hub version` 不创建 runtime。在 `test_probe_container.py` 增加真实临时数据库断言，证明本功能没有新增 migration：

```python
def test_safe_probe_release_keeps_schema_version_ten(tmp_path: Path) -> None:
    config_path = tmp_path / "runtime" / "config.toml"
    with build_container(config_path, probe_home=tmp_path) as container:
        with container.database.connect(readonly=True) as connection:
            versions = tuple(
                row[0]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            )
    assert versions == tuple(range(1, 11))
```

新建 `scripts/verify_wheel.py`，在临时目录构建 wheel，不删除已有 `dist/`：

```python
from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="pmh-wheel-") as directory:
        output = Path(directory)
        subprocess.run(
            ["uv", "build", "--wheel", "--out-dir", str(output)],
            check=True,
        )
        wheels = tuple(output.glob("project_memory_hub-0.2.0-*.whl"))
        if len(wheels) != 1:
            raise SystemExit("expected exactly one 0.2.0 wheel")
        with zipfile.ZipFile(wheels[0]) as archive:
            wheel_names = set(archive.namelist())
            metadata_name = next(
                name for name in wheel_names if name.endswith(".dist-info/METADATA")
            )
            entry_points_name = next(
                name for name in wheel_names if name.endswith(".dist-info/entry_points.txt")
            )
            metadata = archive.read(metadata_name).decode("utf-8", errors="strict")
            entry_points = archive.read(entry_points_name).decode("utf-8", errors="strict")
        verify_wheel(wheel_names, metadata, entry_points)
```

`verify_wheel()` 在同一文件内实现为纯函数，并精确断言：

```python
def verify_wheel(wheel_names: set[str], metadata: str, entry_points: str) -> None:
    required = {
        "project_memory_hub/probes/__init__.py",
        "project_memory_hub/probes/models.py",
        "project_memory_hub/probes/base.py",
        "project_memory_hub/probes/filesystem.py",
        "project_memory_hub/probes/service.py",
        "project_memory_hub/probes/builtin.py",
        "project_memory_hub/storage/migrations/0010_codex_deferred_records.sql",
    }
    missing = sorted(required - wheel_names)
    if missing:
        raise SystemExit(f"wheel files missing: {missing}")
    if "Version: 0.2.0" not in metadata:
        raise SystemExit("wheel metadata version mismatch")
    if "memory-hub = project_memory_hub.cli:app" not in entry_points:
        raise SystemExit("console entry point mismatch")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 运行版本测试，确认仍报告 0.1.2**

Run: `uv run pytest tests/integration/test_cli_core.py::test_version_is_side_effect_free -q`

Expected: FAIL，实际版本是 0.1.2。

- [ ] **Step 3: 升级版本并更新 README/operations**

只把当前 package 版本改为 0.2.0；保留历史“0.1.2 schema v10 升级”段落，不做全局替换。README/operations 必须写清：默认 ingest 仍只有 Codex/ChatGPT；五来源只有只读探针；Trae structure 仍不能证明 model_id；两条 CLI 示例；probe 不由 reconcile/doctor/每日任务调用；不读 `--config`；不落盘；schema 保持 10；生产指纹为空。

- [ ] **Step 4: 运行完整自动化质量门**

Run: `uv run pytest -q`

Expected: PASS。

Run: `uv run pytest --cov=project_memory_hub --cov-report=term-missing --cov-fail-under=85 -q`

Expected: PASS，coverage >= 85%。

Run: `uv run ruff format --check . && uv run ruff check . && uv run mypy src/project_memory_hub`

Expected: 全部 exit 0。

- [ ] **Step 5: 在临时目录构建 wheel 并验证版本、内容、入口和 schema 不变**

Run: `uv run python scripts/verify_wheel.py`

Expected: PASS；临时 wheel 中六个 probes 模块、console entry point 和 storage migration 0010 均存在，版本是 0.2.0；脚本退出后不留下构建目录。

Run: `uv run memory-hub doctor --format json`

Expected: 输出可解析 JSON；每日自动任务缺失仍是用户选择产生的非阻断提醒，不为本功能新增自动任务。

- [ ] **Step 6: 更新 Graphify、确认 hook 并提交发布改动**

Run: `graphify update .`

Expected: 更新 `graphify-out/graph.json`，新 probes、CLI、控制台和文档关系可查询。

Run: `graphify hook status`

Expected: `post-commit: installed` 且 `post-checkout: installed`。

```bash
git add scripts/verify_wheel.py pyproject.toml src/project_memory_hub/__init__.py README.md docs/operations.md tests/integration/test_cli_core.py tests/integration/test_probe_container.py
git commit -m "chore(release): 发布安全来源探针 0.2.0"
```

- [ ] **Step 7: 做真实机器只读验收，不保存机器数据**

新增 `scripts/verify_probe_zero_write.py`。它只在内存保存相对文件清单与摘要，不打印路径、摘要或行内容：

```python
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from project_memory_hub.paths import RuntimePaths


_TABLES = (
    "project_facts",
    "source_refs",
    "behavior_memories",
    "pending_captures",
    "checkpoints",
    "import_receipts_v2",
    "codex_deferred_records",
)


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _inventory(root: Path) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    entries: list[str] = []
    for current, directories, files in os.walk(root, followlinks=False):
        directories.sort()
        files.sort()
        base = Path(current)
        entries.extend(str((base / name).relative_to(root)) for name in directories)
        entries.extend(str((base / name).relative_to(root)) for name in files)
    return tuple(entries)


def _row_counts(database: Path) -> tuple[tuple[str, int], ...]:
    if not database.is_file():
        return ()
    connection = sqlite3.connect(
        f"{database.resolve().as_uri()}?mode=ro&immutable=1", uri=True
    )
    try:
        present = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
        }
        return tuple(
            (table, connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in _TABLES
            if table in present
        )
    finally:
        connection.close()


def _snapshot(paths: RuntimePaths) -> tuple[object, ...]:
    config = paths.root / "config.toml"
    database_files = (
        paths.database,
        Path(f"{paths.database}-wal"),
        Path(f"{paths.database}-shm"),
        Path(f"{paths.database}-journal"),
    )
    return (
        _inventory(paths.root),
        _digest(config),
        tuple(_digest(path) for path in database_files),
        _row_counts(paths.database),
    )


def _probe(*arguments: str) -> None:
    completed = subprocess.run(
        ["uv", "run", "memory-hub", "source", "probe", *arguments, "--format", "json"],
        check=False,
        capture_output=True,
    )
    if completed.returncode != 0:
        raise SystemExit("probe did not complete; ensure no structure probe is active")
    json.loads(completed.stdout)


def main() -> None:
    paths = RuntimePaths.for_root()
    before = _snapshot(paths)
    _probe("--all")
    _probe("trae", "--structure")
    after = _snapshot(paths)
    if after != before:
        raise SystemExit("source probe changed Project Memory Hub runtime")
    print("source probe zero-write verification passed")


if __name__ == "__main__":
    main()
```

只在没有 reconcile、capture 或其它 PMH 写任务运行时执行，避免把外部并发写入误判为 probe 写入。

Run: `uv run memory-hub source probe --all --format json`

Expected: exit 0，恰好五个稳定结果；输出不含 `/Users/`、任务标题、正文、项目名、原始 schema、fingerprint 或异常原文。

Run: `uv run memory-hub source probe trae --structure --format json`

Expected: exit 0 或稳定 `probe_busy`；正常结果必须 `model_status="unverifiable"`、`ingestion_allowed=false`，生产空指纹不能返回 recognized。

Run: `uv run python scripts/verify_probe_zero_write.py`

Expected: 精确输出 `source probe zero-write verification passed`；真实 runtime 的相对文件清单、config bytes、数据库/sidecar SHA-256 和七张关键表行数全部不变。

打开本地控制台后人工确认 Codex/ChatGPT 控制仍可用，五可选来源没有 Enable/Import，Trae 结构结果刷新后恢复 light。验收只记录聚合状态和命令 exit，不复制真实路径或应用数据到仓库。

Run: `graphify update .`

Expected: 新验收脚本进入本地代码图，命令 exit 0。

```bash
git add scripts/verify_probe_zero_write.py
git commit -m "test(probe): 增加真实零写入验收"
```

## 最终自审清单

- [ ] 规格第 1–4 节：目标/非目标/产品决策分别落在 Task 2、5、7、8、9、10。
- [ ] 规格第 5 节：Task 1 静态导入测试和 Task 6 两容器证明依赖边界。
- [ ] 规格第 6–7 节：Task 1 严格模型、Task 2 精确白名单、Task 5 固定能力/导入锁。
- [ ] 规格第 8–9 节：Task 7 CLI、Task 8 GET、Task 9 POST/刷新/页面。
- [ ] 规格第 10–11 节：Task 2–4 独立预算、竞态和稳定码测试。
- [ ] 规格第 12–13 节：Task 1 import、Task 6/7/9 零写入、全量测试与并发证明。
- [ ] 规格第 14–15 节：Task 10 版本、wheel、doctor、Graphify 和真实机器验收。
- [ ] 逐项核对 `SourceProbeResult`、`ProbeMetrics`、`SourceProbeService.reserve_structure()`、`StructureProbeLease.run()/close()`、`build_probe_container()` 的名称和参数在所有任务中一致。
- [ ] 运行 `git diff --check`，Expected: 无输出。

## 已知平台边界与关闭规则

- `/dev/fd/<fd>` 行为有平台差异。无法证明身份或 SQLite 无法从 fd 打开时安全返回 `unsupported_format`；不能回退原路径。这不阻断 0.2.0，因为生产指纹本来就是空集合。
- 普通同步 `openat/scandir/sqlite3.connect` 没有可移植的强制取消。实现必须在每次调用前后及 SQLite VM callback 检查 3 秒 deadline，并在响应前关闭全部资源；异常 FUSE/网络挂载里的单个内核调用无法由 Python 绝对中断，真实验收不得扩大到任意挂载点。
- `/opt/homebrew/bin/claude` 常见形态可能是 symlink；批准规格要求逐组件拒绝 symlink，所以真实结果可能是 Rejected。不要为了提高命中率跟随链接。
- `invalid_input` 是 CLI/Web 输入层错误，不是来源 warning；`probe_busy` 是 reservation 错误，不伪装为正常来源结果。
- 权限测试通过 syscall 注入 `EACCES`，不依赖当前用户下不稳定的 chmod 结果；大文件上限使用 sparse/synthetic stat，避免真实 128 MiB 写入。
