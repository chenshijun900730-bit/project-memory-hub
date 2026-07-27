# Project Memory Hub 0.2.0 安全来源只读探针设计

## 1. 背景

Project Memory Hub 0.1.2 默认只启用 Codex 和 ChatGPT。Trae、WorkBuddy、Zcode、
QoderWork 和 Claude Code 已作为不可用来源显示在本地控制台，但当前页面不能区分“软件未
安装”“数据目录不可访问”和“已安装但因为模型身份无法验证而禁止导入”。这会让用户误以为
这些软件没有被发现。

不同软件、不同精确 `model_id` 的行为记忆必须隔离。现有 Trae `session_memory` 数据不能
逐记录证明精确 `model_id`，因此本阶段只能增加来源发现和结构诊断，不能把任何新来源接入
行为记忆写入链。

## 2. 目标

1. 在控制台中检测 Trae、WorkBuddy、Zcode、QoderWork 和 Claude Code 是否存在，以及
   固定数据目录是否可读。
2. 允许用户显式触发 Trae 结构元数据检测，解释为什么它仍不能导入行为记忆。
3. 提供等价的稳定 CLI JSON 输出，便于诊断和自动化测试。
4. 从依赖结构上保证探针不能写行为记忆、checkpoint、receipt、来源引用或配置。
5. 不把任务标题、会话正文或项目名解析为领域字段，也不返回、缓存或猜测这些内容。
6. 保持 Codex、ChatGPT 的启用状态、reconcile 行为和现有控制台操作不变。

## 3. 非目标

- 不启用 Trae、WorkBuddy、Zcode、QoderWork 或 Claude Code 的行为导入。
- 不把不可验证的模型记为 `unknown`，也不从产品名称、字段内容或最近使用情况猜测模型。
- 不支持用户输入任意路径、在控制台添加自定义来源或动态加载第三方探针。
- 不在 reconcile、doctor、每日任务或后台线程中运行来源探针。
- 不恢复 `project_not_found` 隔离记录。
- 不修改数据库 schema、`enabled_sources` 或来源适配器注册表。
- 不把 `/Applications/Claude.app` 单独视为 Claude Code 已安装；Claude 桌面端和 Claude Code
  是不同来源形态。

## 4. 已锁定的产品决策

1. 使用独立 `SourceProbe` 层，不给现有 `SourceAdapter` 增加 dry-run 分支。
2. 控制台采用混合触发：页面打开时自动执行轻量检测；Trae 结构检测必须由用户点击。
3. Trae 结构检测采用严格元数据模式；包含任务正文的记录文件不解析。
4. WorkBuddy、Zcode、QoderWork 和 Claude Code 首版只做安装与目录可读性检测。
5. 探针结果只存在于当前请求，刷新页面后重新检测，不持久化。
6. 首版使用内置固定路径白名单；自定义探针属于后续独立规格。

## 5. 架构与依赖边界

新增 `project_memory_hub.probes` 包，内部职责分为五个模块：

- `models.py`：只读请求、结果、枚举和预算值。
- `base.py`：`SourceProbe` 协议和不可变 `SourceDescriptor`。
- `filesystem.py`：固定根目录验证、fd-relative 遍历、读取预算和竞态检查。
- `service.py`：`SourceProbeRegistry` 与 `SourceProbeService`，聚合并脱敏结果。
- `builtin.py`：五个内置来源描述符、通用安装探针和 Trae 结构探针。

探针继续使用现有 `SourceAgent` 作为稳定来源标识，因为五个可选来源已经存在于该枚举中；
这不改变 `_REGISTERED_SOURCES`、`AdapterRegistry` 或 `enabled_sources` 的含义。

`SourceProbeService` 的构造参数只允许是探针 registry、路径安全策略、预算和时钟。它不得接收
`ServiceContainer`、`Database`、任何 repository、`CaptureService`、`ReconcileService`、
adapter 或 checkpoint 组件。新增静态依赖测试禁止 `project_memory_hub.probes` 导入：

- `project_memory_hub.storage`
- `project_memory_hub.adapters`
- `project_memory_hub.services.capture`
- `project_memory_hub.services.reconcile`

完整 Web `ServiceContainer` 可以持有一个已经构建好的 `source_probes` 字段供控制台调用，但
探针对象本身仍没有反向访问 container 的能力。CLI 使用独立 `build_probe_container()`；该
builder 不调用 `RuntimePaths.ensure()`、不创建配置文件、不初始化 SQLite，也不获得运行库路径。

## 6. 领域契约

### 6.1 检测模式

- `light`：只对固定安装标记和数据根目录执行非递归检查。
- `structure`：只允许 `SourceAgent.TRAE`，在用户显式操作后执行有界结构元数据检测。

CLI 的 source 位置参数和 `--all` 必须二选一：`probe trae` 默认是 Trae `light`，
`probe --all` 固定是五来源 `light`。同时提供 source 与 `--all`、两者都不提供、非 Trae
source 使用 `--structure` 或 `--all --structure` 时都返回 `invalid_input`，不按参数顺序选择
或退化为其它模式。

### 6.2 单一来源结果

每个 `SourceProbeResult` 至少包含：

- `source_agent`
- `mode`
- `installation_status`：`detected` 或 `not_detected`
- `data_status`：`readable`、`blocked`、`missing` 或 `rejected`
- `capability`：`presence_and_access` 或 `structure_metadata`
- `structure_status`：`not_run`、`recognized`、`partial` 或 `unsupported`
- `model_status`：`not_checked` 或 `unverifiable`
- `ingestion_allowed`：类型和值都固定为 `false`
- `metrics`：只允许有界非负整数和已定义布尔能力位
- `warning_codes`：去重、排序后的稳定错误码
- `checked_at`：UTC ISO 8601 时间

`metrics` 不返回原始表名或字段名，而是把识别结果映射为稳定能力位，例如
`has_session_identifier`、`has_model_identifier_field`、`metadata_file_count` 和
`bounded_record_count`。这样即使第三方文件使用异常 schema 标识符，也不会把任意字符串带入
CLI 或页面。只有 schema 与代码中已审核的只读签名完全匹配时才允许查询
`bounded_record_count`；不得对任意第三方表动态执行 `COUNT(*)`。计数查询超时或无法保证
只读一致性时省略对应值，并返回稳定警告码。

`structure_status` 的含义固定为：轻量模式是 `not_run`；结构模式至少完整识别一个受支持候选
且没有截断时是 `recognized`；识别了受支持候选但另有预算、竞态或格式警告时是 `partial`；
没有受支持候选时是 `unsupported`。

`ingestion_allowed` 不是根据其它字段计算的临时结论，而是本版本契约中的常量 `false`。
发现疑似模型字段、未来格式或完整模型名称都不能改变它。

`capability` 表示该内置探针在本版本支持的最高能力，不表示本次已经执行结构读取：Trae 在
`light` 和 `structure` 结果中都为 `structure_metadata`，另外四个来源始终为
`presence_and_access`；本次实际执行程度只由 `mode` 和 `structure_status` 表达。

### 6.3 多根目录聚合

一个来源可能同时存在多个产品版本或数据根。聚合规则固定为：

1. 任一安装标记存在即为 `detected`。
2. 任一允许的数据根安全可读即为 `readable`。
3. 没有可读根但至少一个根被权限阻止时为 `blocked`。
4. 没有可读或被阻止的根、但发现符号链接或不安全文件类型时为 `rejected`。
5. 其余为 `missing`。

单个不安全根产生警告，但不能遮蔽另一个安全根的检测结果。结果只返回聚合状态和计数，不
返回实际命中的绝对路径。

## 7. 固定来源白名单

路径在运行时从受信任的 `Path.home()` 展开，结果只用于本地访问，不进入输出。首版允许：

### 7.1 Trae

安装标记：

- `/Applications/Trae.app`
- `/Applications/Trae CN.app`
- `/Applications/TRAE SOLO.app`
- `/Applications/TRAE SOLO CN.app`

数据根：

- `~/Library/Application Support/Trae`
- `~/Library/Application Support/Trae CN`
- `~/Library/Application Support/TRAE SOLO`
- `~/Library/Application Support/TRAE SOLO CN`
- `~/.trae`
- `~/.trae-cn`
- `~/.trae-aicc`

现阶段没有证据证明 `session_memory` 的精确相对路径、扩展名或 schema，因此规格不虚构固定
文件名或已识别格式。
结构模式只把“任一相对路径组件或叶节点 stem 精确等于 `session_memory`”的条目视为候选，并
继续受深度、条目和文件预算限制。候选首先只读有界 magic header：SQLite 才进入只读 schema
检查；JSON、JSONL、日志和未知格式首版只报告格式能力不足，不解析记录。0.2.0 的生产
`recognized_schema_fingerprints` 固定为空集合，因此真实 Trae SQLite 只生成不对外展示的结构
指纹并返回 `unsupported`；它不能被标为 `recognized`。以后若确认权威 schema 或独立无正文
元数据文件，必须通过代码变更增加精确指纹、相对路径和允许字段，不能在运行时自动放宽。

### 7.2 WorkBuddy

安装标记为 `/Applications/WorkBuddy.app`；数据根为
`~/Library/Application Support/WorkBuddy` 和 `~/.workbuddy`。用户项目目录
`~/Workbuddy` 不在白名单内，避免把普通项目树当成应用数据递归检查。

### 7.3 Zcode

安装标记为 `/Applications/ZCode.app`；数据根为
`~/Library/Application Support/ZCode` 和 `~/.zcode`。

### 7.4 QoderWork

安装标记为 `/Applications/QoderWork.app`；数据根为
`~/Library/Application Support/QoderWork` 和 `~/.qoderwork`。Qoder 和 QoderWork 视为
两个产品，本阶段不扫描 `/Applications/Qoder.app`、`~/Library/Application Support/Qoder`
或 `~/.qoder`。

### 7.5 Claude Code

安装标记只允许
`~/.local/bin/claude`、`~/.claude/local/claude`、`/opt/homebrew/bin/claude` 和
`/usr/local/bin/claude`；数据根仅为 `~/.claude`。不遍历或信任任意 PATH 目录；固定标记仍要
验证目标是普通可执行文件且没有不安全符号链接链。

白名单的新增、删除或扩大必须经过代码变更和测试，不能由配置文件静默扩展。

## 8. 触发方式与数据流

### 8.1 控制台轻量检测

`GET /sources` 保留现有 Codex、ChatGPT 启用控制，并对五个不可用来源调用
`SourceProbeService.probe_all_light()`。轻量模式只执行以下步骤：

1. 对有限安装标记和数据根执行 `lstat`。
2. 拒绝符号链接和非普通目录或文件。
3. 用 no-follow 目录描述符确认可读性后立即关闭。
4. 返回聚合结果并渲染页面。

轻量检测不枚举数据根内容，不由 reconcile 或 doctor 复用。单一来源检测失败只影响对应卡片，
不能使整个 Sources 页面返回 500。

### 8.2 Trae 手动结构检测

Sources 页面只为 Trae 提供带 CSRF token 的“进一步检测”表单。服务器通过
`POST /sources/trae/probe` 接收空操作表单，继续使用现有 loopback、bootstrap token、Host
校验、CSRF 和表单大小限制。路由通过线程卸载执行有界同步探针，然后直接重新渲染 Sources
页面；结果不写 session、cookie、数据库或临时缓存，刷新后恢复轻量状态。

`SourceProbeService` 为结构模式持有每进程单槽、非阻塞 `threading.Lock`。锁已占用时不再
创建工作线程，Web 返回 HTTP 409 并显示 `probe_busy`，CLI 返回同名稳定错误。轻量检测不
获取该锁。路由不使用无法终止线程的外层 `asyncio.wait_for`；截止时间由探针在每次枚举、读取
和 SQLite VM progress callback 中主动检查，超时后在工作线程内关闭连接和全部 fd 再返回。

结构探针按以下顺序运行：

1. 重新执行全部根目录和文件身份检查，不能信任页面上一次轻量结果。
2. fd-relative 遍历时只选择第 7.1 节定义的 `session_memory` 候选规则；其它条目只计入遍历
   预算，不打开文件内容。
3. SQLite 候选先通过目录 fd 使用 `O_RDONLY | O_NOFOLLOW | O_CLOEXEC` 打开并 `fstat`。
   存在同名 `-wal`、`-shm` 或 `-journal` 伴随文件时返回 `source_changed`，不读取可能不一致的
   live snapshot。只有平台能证明 `/dev/fd/<fd>` 与已打开 fd 的 device/inode 相同，才允许用
   `mode=ro&immutable=1` 打开；否则返回 `unsupported_format`，绝不回退到原路径重开。
4. SQLite 连接关闭扩展加载，并设置 `query_only=ON`、`trusted_schema=OFF`、`busy_timeout=0`
   和 VM progress deadline。只允许 `sqlite_schema`、`table_list` 与 `table_xinfo` 元数据读取；
   禁止读取应用表。表和字段元组经过有界规范化、排序后只在内存中计算 SHA-256 指纹，原始
   标识符和指纹都不进入结果。0.2.0 的生产指纹白名单为空，因此不执行记录数量查询。
5. 首版没有已确认的独立元数据 JSON 路径，因此 JSON、JSONL、行式会话、日志和聊天记录都
   不进入内容解析器。未来只有路径和 JSON Pointer 同时写入内置描述符并通过隐私测试后，
   才能读取对应无正文元数据字段。
6. 把结构映射为布尔能力位和有界计数，不返回原始标识符、指纹或任何记录值。
7. 无论结果如何都返回 `model_status=unverifiable` 和 `ingestion_allowed=false`。

### 8.3 CLI

新增 Typer 子命令组：

```text
memory-hub source probe --all --format json
memory-hub source probe trae --structure --format json
```

第一条对五个来源执行轻量检测；第二条执行 Trae 手动结构检测。文本输出只显示与页面相同的
聚合状态，JSON 输出使用领域契约的稳定字段。正常完成时即使某个来源缺失或被权限阻止也
返回退出码 0；请求不合法、探针基础设施失败或输出编码失败分别使用既有 CLI 稳定错误约定。
全局 `--config` 对该命令没有作用；probe container 不打开该路径，也不把它解释为额外扫描根。

## 9. 页面表现

现有 Sources 表格保留 `Implementation`、`Desired state`、`Running process` 和 Codex、
ChatGPT 的启停操作。五个不可用来源新增：

- `Detected`：`Detected` 或 `Not detected`
- `Probe health`：`Readable`、`Permission blocked`、`Missing` 或 `Rejected`
- `Model identity`：轻量模式显示 `Not checked`；Trae 结构结果显示 `Unverifiable`
- `Behavior import`：始终显示 `Locked`

Trae 在根目录安全可读时显示“进一步检测”按钮；根缺失、被阻止或拒绝时按钮禁用并显示稳定
原因。其它四个来源显示“安装与目录访问检测”，没有启用或导入按钮。页面不展示绝对路径、文件名、
项目名、任务标题、正文、原始 schema 标识符或异常原文。

## 10. 文件系统安全与读取预算

### 10.1 路径安全

- 从固定信任锚点逐组件执行 `lstat`，拒绝现存符号链接组件。
- 打开目录和文件时使用平台支持的 `O_NOFOLLOW`、`O_DIRECTORY` 和只读标志。
- 深层枚举以已打开目录 fd 为基准，不能拼接后重新解析任意绝对路径。
- 打开前后比较 device、inode、类型、大小和修改时间；变化时返回 `source_changed`。
- 目录项名称必须通过严格 UTF-8、有界长度和控制字符检查。
- 不请求扩大到整个主目录、`~/Library` 或任意项目根的权限。

### 10.2 默认预算

单次 Trae 结构检测的硬上限为：

- 最大目录深度：4
- 最大目录项：2,048
- 最大候选元数据文件：64
- 最大 SQLite 候选：4
- 单个 SQLite 文件大小：64 MiB
- SQLite 候选文件大小总和：128 MiB
- 单次 SQLite 元数据查询 VM steps：100,000
- 单个候选 magic header 最多读取：64 B
- 所有候选 header 总读取量：4 KiB
- SQLite schema 标识符总数：2,048
- 单个来源墙钟时间：3 秒

轻量模式每个来源最多检查 16 个固定标记或根，全部五个来源总墙钟预算为 2 秒。预算耗尽时
立即关闭所有描述符，返回已有安全聚合结果和 `budget_exceeded`，不自动增加预算重试。
深度、条目、文件、字节或 schema 数量耗尽使用 `budget_exceeded`；墙钟时间耗尽只使用
`probe_timeout`，两者不同时报告同一个原因。

## 11. 错误处理与隐私输出

允许返回的警告码固定为：

- `source_missing`
- `permission_blocked`
- `symlink_rejected`
- `unsafe_file_type`
- `unsupported_format`
- `malformed_metadata`
- `invalid_utf8`
- `budget_exceeded`
- `probe_timeout`
- `source_changed`
- `model_id_unverifiable`
- `probe_busy`
- `probe_failed`

实现可以在内部记录异常类型用于测试，但用户输出不得包含异常 `repr`、系统错误正文、绝对路径、
环境变量或第三方文件内容。未知异常统一映射为 `probe_failed`；该码只表示探针基础设施失败，
不暗示来源数据损坏。

所有失败保持来源局部化。Trae 结构检测失败不能改变之后的轻量结果，任一可选来源失败不能
阻断 Codex、ChatGPT 的控制、导入、capture、recall 或 reconcile。

## 12. 不变量与零副作用证明

1. `SourceProbeService` 没有数据库或配置写入依赖。
2. 探针从不注册到 `AdapterRegistry`，从不被 `ReconcileService` 调用。
3. 探针不创建 runtime root、config、SQLite、lock、checkpoint、receipt、source reference、
   pending capture 或 behavior memory。
4. 探针不更改 `enabled_sources`，也不让控制台出现可选来源启用按钮。
5. `ingestion_allowed` 在类型、运行时结果和页面中恒为 `false`。
6. `model_id_unverifiable` 不能被警告抑制、用户操作或字段存在性提升为可导入。
7. 临时结果只存在于调用栈和响应对象；请求完成后没有持久化探针缓存。

## 13. 测试策略

实现遵循红—绿—重构，至少覆盖：

1. 五个来源的安装标记存在、完全缺失、数据根可读、权限拒绝和不安全类型。
2. 多根目录按固定优先级聚合，单一坏根不遮蔽安全可读根。
3. 轻量模式不枚举目录内容；页面 GET 不触发 Trae 结构探针。
4. Trae POST 和 CLI `--structure` 才触发结构模式，且每次重新验证文件身份。
5. WorkBuddy、Zcode、QoderWork、Claude Code 的结构请求失败关闭为 `invalid_input`。
6. 损坏、超限或带 WAL/SHM/journal 的 SQLite，未知格式、无效 UTF-8、异常 schema 标识符和
   超长目录项只产生稳定码。
7. 符号链接、目录替换、文件替换、inode 漂移和 Preview-to-open 竞态被拒绝。
8. 深度、条目数、文件数、字节数、schema 数量和墙钟预算分别可独立触发。
9. 行式 JSON、日志和会话正文文件不会被内容解析器打开。
10. 结果不包含任务标题、正文、项目名、绝对路径、原始字段名、异常原文或模型猜测。
11. Trae 即使存在模型字段也固定返回 `unverifiable` 和 `ingestion_allowed=false`。
12. CLI 使用专用 probe container；对不存在的 runtime root 执行后，目录仍不存在。
13. 对已有运行目录执行前后文件清单、数据库 SHA-256、关键表行数和配置字节对比，证明零写入。
14. 不产生 checkpoint、receipt、source reference、pending capture 或 behavior memory。
15. Sources 页面保留 Codex、ChatGPT 原有状态和启停表单；五个可选来源没有启用表单。
16. Trae 深层表单继承 bootstrap token、Host、CSRF、表单大小和并发限制。
17. `project_memory_hub.probes` 的静态导入边界测试阻止接入 storage、adapter、capture 和
    reconcile。
18. 全量测试、覆盖率、Ruff、格式检查、mypy、wheel 内容和 Graphify 更新通过。

还必须验证同一进程并发提交两个结构探针时只有一个进入工作线程，另一个稳定返回
`probe_busy`；超时测试必须证明工作线程在响应前已经关闭连接和全部 fd。SQLite 专项测试使用
合成数据库覆盖 fd 身份不符、路径替换、伴随 WAL/SHM/journal、64 MiB 单文件上限、128 MiB
总量上限、VM steps 和空生产指纹白名单。生产白名单为空时任何合成 schema 都只能返回
`unsupported`；测试注入的非生产指纹只能用于验证未来 recognized 分支，不得进入内置描述符。

测试使用注入的临时 home 和完全合成的元数据 fixture，不复制或提交真实用户数据。真实机器
验收只验证聚合状态与能力位，不把应用数据保存到测试报告。

## 14. 发布与真实机器验收

- 目标版本为 `0.2.0`；本功能是新用户能力，但不改变现有命令默认行为。
- 不增加 migration，安装前后数据库 schema 版本保持 10。
- 先执行 CLI 五来源轻量探针，确认 JSON 不含绝对路径或内容。
- 打开本地控制台，确认 Codex、ChatGPT 控制保持不变，五个可选来源显示检测状态。
- 由用户操作 Trae“进一步检测”，确认页面显示 `model_id_unverifiable`、导入锁定且无正文。
- 对真实数据库和配置做前后校验，确认没有写入。
- 运行完整测试、doctor 和 Graphify；每日自动任务缺失继续是用户选择产生的非阻断提醒。

## 15. 后续扩展门槛

某个来源只有在能逐记录提供权威、精确、可竞态保护的 `model_id` 证明后，才可以进入新的
行为导入设计。届时必须单独评审 adapter、checkpoint、receipt、项目匹配、幂等和跨模型隔离，
不能因为探针显示 `recognized` 就复用或绕过本设计的 `ingestion_allowed=false`。

用户自定义来源也必须另做规格，包含路径授权、探针代码来源、签名或审核、资源预算和撤销机制；
首版固定白名单不能演变成读取任意路径的通用入口。
