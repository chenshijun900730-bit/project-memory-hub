# 项目记忆中枢设计规格

- 日期：2026-07-12
- 状态：已完成对话设计评审，等待用户对书面规格复核
- 项目目录：~/Documents/example-project
- 工作名称：Project Memory Hub，中文名“项目记忆中枢”

## 1. 目标

构建一个完全在本机运行的项目记忆中枢，自动发现用户的代码项目，增量总结 Codex 和 ChatGPT 参与开发时留下的有效经验，并在后续任务开始时向当前模型提供不超过约 800 tokens 的相关简报。

系统需要形成以下闭环：

1. 项目开始前，按当前目录和任务目标召回相关上下文。
2. 一个可验证的工作单元完成后，只记录本次新增的决策、失败、验证和遗留问题。
3. 每日补偿扫描 Git 和任务记录，补录因中断或遗漏而没有主动提交的增量。
4. 项目长期没有变化时，将零散记录压缩为阶段复盘。
5. 系统根据运行数据提出自身改进建议，但未经用户批准不得修改自身代码、提示词或规则。

## 2. 已确认的产品决策

### 2.1 默认数据来源

- 默认启用 Codex 本地任务记录。
- 默认启用 ChatGPT 官方数据导出包。
- Trae、WorkBuddy、Zcode、QoderWork、Claude Code 作为默认关闭的可扩展适配器。
- 新的软件来源通过控制台开关和适配器接口添加，不修改核心存储和检索逻辑。

“来源适配器是否启用”只控制该软件的任务和行为记录。项目代码本身属于共享项目事实，仍由项目发现器按白名单扫描，不按创建软件区分。

### 2.2 项目扫描范围

首版项目根目录为：

- ${HOME}/Documents
- ${HOME}/Projects
- ${HOME}/Workspace

扫描器通过 Git 根目录及 package.json、pyproject.toml、Cargo.toml、go.mod、pom.xml、build.gradle 等项目标志识别项目。允许用户在控制台添加或关闭根目录。

默认排除：

- 系统和私人目录：Library、Downloads、Applications、.Trash、iCloud 归档、Obsidian 知识库。
- 隐藏工具目录：用户主目录下的隐藏目录默认排除，只有已启用适配器声明的精确路径可以读取。
- 依赖和环境：node_modules、vendor、.venv、venv、env、Pods。
- 缓存和产物：dist、build、target、.next、.nuxt、.turbo、.gradle、DerivedData、coverage、graphify-out 以及常见语言缓存。
- 敏感文件：.env 及其变体、证书、私钥、SSH 文件、credentials、secrets、token 等。

扫描器遇到无权限目录时必须保存“权限阻塞”状态并在控制台显示路径和修复方式，不得静默忽略。重复副本只标记为候选重复项，未经用户确认不得合并。

共享事实扫描采用有界输入，而不是把整个仓库发送给模型。默认读取 Git 元数据、根级清单、README、AGENTS、文档索引、包脚本、测试配置、文件树统计和当前分支状态。源码内容只在当前任务需要、文件发生相关变化或项目已经存在可用 Graphify 图时按需读取。

### 2.3 记忆隔离

系统包含两个逻辑存储层：

1. 共享项目事实：技术栈、目录结构、Git 状态、运行命令、验证命令和当前可观察状态。
2. 隔离行为记忆：某个软件及某个模型在任务中的决策方式、失败模式、操作习惯和复盘。

行为记忆以 source_agent 和 model_id 组成硬命名空间。查询必须同时匹配来源软件和模型。无法确定模型时写入该来源的 unknown 命名空间，绝不猜测或合并。

命名空间过滤必须在 SQL 查询和全文检索候选生成之前执行，不能先跨模型检索再在结果层过滤。写入端同样必须携带经过适配器验证的命名空间上下文。

一个行为结论只有在被文件、Git、测试结果或用户确认独立验证后，才能成为共享项目事实。跨模型行为经验只能进入待审批队列，用户明确批准后才能提升为共享规则。

### 2.4 自我迭代边界

- 系统可以自动更新项目事实、任务增量、索引、压缩结果和健康指标。
- 系统可以生成扫描规则、提示模板、适配器或程序代码的改进建议和补丁。
- 改进补丁必须包含原因、影响范围、测试结果和回滚版本。
- 未经用户在控制台批准，补丁不得应用。
- 补丁测试失败时不得启用，系统继续运行上一稳定版本。

## 3. 非目标

首版明确不做以下内容：

- 不扫描整个用户主目录。
- 不复制和长期保存原始 Codex 或 ChatGPT 对话。
- 不直接写入或修改 Codex 内部记忆数据库。
- 不自动修改几十个被扫描项目的文件或 Git 历史。
- 不在不同模型之间自动共享行为经验。
- 不自动读取 Trae、WorkBuddy、Zcode、QoderWork 或 Claude Code 的任务记录。
- 不使用向量数据库或外部嵌入服务。
- 不要求额外的模型 API Key。
- 不允许无人审批的自我修改。

## 4. 总体架构

系统由九个边界清晰的组件组成：

1. Project Registry：发现项目、生成稳定项目标识、维护路径和重复候选。
2. Source Adapters：读取 Codex 会话和 ChatGPT 官方导出，后续承载其它软件适配器。
3. Normalizer and Redactor：统一事件结构、识别密钥和隐私字段、执行脱敏和丢弃。
4. Shared Fact Store：保存确定性或已验证的项目事实。
5. Isolated Behavior Store：按来源软件和模型保存行为记忆。
6. Retrieval and Brief Builder：检索、排序、去重并组装约 800-token 简报。
7. Capture and Reconciler：处理任务结束增量、每日补偿扫描和长期压缩。
8. CLI and Local Dashboard：为 Codex 自动调用和用户控制提供接口。
9. Improvement Proposal Engine：分析运行健康并生成待审批升级建议。

核心依赖方向为：

来源适配器 -> 规范化和脱敏 -> 共享事实或隔离行为记忆 -> 检索器 -> Codex 或 CLI

控制台管理来源、项目、记忆和升级建议，但不能绕过命名空间访问控制。

## 5. 数据模型

### 5.1 projects

保存：

- project_id
- canonical_path
- path_device
- path_inode
- display_name
- git_root
- git_remote_fingerprint
- manifest_fingerprint
- discovery_status
- permission_status
- last_observed_change
- inactivity_state

project_id 是首次发现时生成的本地 UUID，规范化真实路径作为当前定位信息。path_device 和 path_inode 是内部目录身份校验值，用来防止 macOS Data 卷或大小写别名把同一个物理目录注册为多个项目，也用来在已登记路径被原位替换时拒绝检索。无法逐级安全打开的 `/.vol/...` 虚拟路径直接拒绝，用户应选择规范路径。旧数据库升级后该身份先保持未信任，首次可写发现只能在唯一匹配时补齐；只要仍有启用项目未完成身份回填，reconcile 必须保持待处理，用户可重新关联或禁用失效项目。项目移动后，Git 远程地址和清单指纹只能提出“重新关联”建议，必须经用户确认后才能把新路径关联到原 project_id。

项目注册表维护单调 generation。ChatGPT 导入开始时完整验证一次启用项目，处理中用 generation 做常数级快照校验，并只重验当前会话实际命中的目录。禁用的内层项目必须遮蔽启用的外层项目，即使内层证据分数较低，或项目重新关联后 display_name 仍是旧名称；绝对路径、引号路径和相对路径均不得回退写入外层项目。项目注册发生漂移时返回 `reconcile_required`，包括格式错误和未完成会话在内的所有导入 receipt 均不得落库。reconcile 写入成功状态前必须在同一事务内复核 generation 和启用项目身份，不能清除执行期间新产生的 catch-up 标记。

### 5.2 project_facts

保存：

- fact_id
- project_id
- category
- normalized_content
- evidence_type
- evidence_reference
- observed_at
- confidence
- supersedes_fact_id
- stale_at

项目事实必须有可追踪证据。当前文件或 Git 事实与旧结论冲突时，新事实生效，旧事实标记为过期而不是直接删除。

### 5.3 behavior_memories

保存：

- memory_id
- project_id
- source_agent
- model_id
- task_fingerprint
- memory_kind
- normalized_content
- source_reference_id
- extractor_type
- created_at
- confidence
- lifecycle_state

memory_kind 至少支持 decision、failed_attempt、verified_method、preference、risk、open_issue 和 retrospective。

### 5.4 source_refs

只保存定位和完整性信息：

- source_reference_id
- source_agent
- source_record_id
- source_path
- content_hash
- source_timestamp
- parser_version

数据库不保存原始对话正文。原内容仍位于 Codex 会话目录或用户提供的 ChatGPT 导出包中。

### 5.5 checkpoints

为每个适配器保存最后成功处理的位置、文件指纹和解析器版本。检查点只在数据库事务成功后推进。

### 5.6 improvement_proposals

保存建议原因、建议类型、补丁、验证命令、验证输出摘要、风险等级、目标版本、审批状态和回滚版本。

## 6. 来源适配器

### 6.1 Codex 适配器

默认读取 ${HOME}/.codex/sessions 下的本地 JSONL 会话记录。适配器按会话工作目录匹配项目，只处理白名单项目范围内的任务。

优先使用 Codex 在任务结束前主动提交的结构化增量。会话解析器只承担补录和来源核验，不把整段会话复制到数据库。

Codex 行为记录必须保留会话中的实际模型标识。无法提取时进入 codex/unknown 命名空间。

主动 capture 声明的模型必须与当前会话可验证的模型元数据一致。两者冲突时不得写入声明的模型命名空间，而应进入待确认状态。

### 6.2 ChatGPT 适配器

ChatGPT 首版只支持官方数据导出 ZIP。用户通过控制台选择文件，或把 ZIP 放入应用专用导入目录。系统不自动监控整个 Downloads 目录。

导入器支持 conversations.json 以及官方导出可能出现的编号会话 JSON 文件。处理流程为：

1. 校验 ZIP 结构和内容大小。
2. 拒绝路径穿越、异常压缩比和超出配置上限的归档成员。
3. 临时流式解析，不将完整会话正文写入数据库。
4. 只选择具有代码开发特征的会话。
5. 使用绝对路径、仓库名、远程地址、明确项目名等证据匹配项目。
6. 无法可靠匹配的会话进入人工确认队列。
7. 使用确定性提取器生成结构化任务事件，不让 Codex 重新解释 ChatGPT 的行为。
8. 确定性提取只保存对话中明确出现且可归因的决策、命令、结果和失败，不推断模型心理、人格或隐含偏好。
9. 保存摘要、来源引用、模型标识和内容指纹。

通过文件选择器导入时，系统只读原文件且不复制。用户主动放入应用专用导入目录的 ZIP 在成功导入后保留原状，并在控制台提示用户自行删除；系统不自动删除原始导出。

如果导出中没有模型标识，数据进入 chatgpt/unknown 命名空间。

### 6.3 后续适配器接口

每个新适配器必须实现：

- discover_sources
- health_check
- read_incremental
- normalize
- checkpoint
- source_capabilities

新适配器默认关闭。启用前必须通过跨模型隔离、脱敏、重复导入、损坏输入和中断恢复测试。

## 7. 任务生命周期

### 7.1 任务开始召回

全局 Codex 指令在进入 Git 项目并开始实质工作前调用：

    memory-hub recall --stdin-json --format prompt

标准输入对象包含当前项目路径、任务目标、来源软件和当前模型。任务文本不得放入命令行参数，避免出现在进程列表或终端历史中。普通 recall 在检索前必须用 `CODEX_THREAD_ID`、cwd 和有界本地会话元数据二次核对精确 Codex 命名空间，不信任 stdin 自报值。人工跨命名空间查看只允许通过 owner-only `--manual` 通道，使用 stdin 中的本地访问令牌；受管 Codex 指令明确禁止将其作为失败回退。

召回器依次执行：

1. 以当前目录找到最长前缀匹配的项目根。
2. 检索共享项目事实。
3. 使用硬过滤只检索当前来源和当前模型的行为记忆。
4. 按任务相关性、时效性、置信度和验证强度排序。
5. 去除重复或已被替代的信息。
6. 按优先级组装简报。

简报优先级为：

1. 当前项目状态和未完成事项。
2. 与本任务直接相关的已验证命令和失败模式。
3. 近期关键决策。
4. 可复用工作习惯。
5. 低优先级背景。

目标上限为约 800 tokens。已知模型使用本地可用的对应 tokenizer；无法获得 tokenizer 时使用保守字符估算并预留安全余量。token 计算不得在运行时联网下载编码文件。

### 7.2 工作单元完成捕获

一个“工作单元完成”指 Codex 完成一个明确目标并完成相称验证，例如实现功能、修复缺陷、完成安装或给出证据支持的诊断。它不是每一条对话消息，也不是等待整个项目永久停止。

Codex 在最终交付前通过标准输入提交 JSON：

    memory-hub capture \
      --cwd <项目路径> \
      --source codex \
      --model <当前模型> \
      --stdin-json

结构化增量包含：

- objective
- outcome
- decisions
- failed_attempts
- verified_commands
- changed_paths
- open_issues
- reusable_lessons

系统验证路径、命令和来源后保存增量。相同 task_fingerprint 和内容指纹不会重复写入。

如果记忆服务不可用，Codex 的工作结果仍可正常交付。增量进入权限受限的本地待重试队列，由每日补偿任务处理。

### 7.3 每日补偿

Codex 桌面自动化每天在本项目中运行一次 reconcile 命令。具体时间可在控制台修改。

每日运行是本机在线状态下的尽力执行，不假设电脑睡眠或 Codex 退出时仍能按点运行。doctor 和控制台展示最后成功时间；错过的周期在下次 Codex 项目任务或控制台启动时补跑。

补偿流程：

1. 获取单实例锁。
2. 比较项目 Git HEAD、工作树摘要和上次扫描指纹。
3. 读取 Codex 适配器的新会话范围。
4. 导入应用专用目录中新出现的 ChatGPT ZIP。
5. 只处理有变化的项目和任务。
6. 在同一事务中写入记录并推进检查点。
7. 输出健康摘要和待人工处理项目。

每日补偿不是全量重新总结。

### 7.4 长期压缩

项目连续 21 天没有有效 Git、文件或任务变化时进入不活跃状态。该阈值可在控制台修改。

压缩器将旧任务增量整理为阶段复盘，保留关键决策、已验证方法、失败模式、风险和未完成事项。原始增量进入冷状态，不删除，需要时仍可回查。

## 8. Codex 集成

实现阶段在现有 ${HOME}/.codex/AGENTS.md 中追加一个有边界的 Project Memory Hub 区块，并保留用户已有规则。

该区块要求：

- 进入项目并开始实质工作前运行 recall。
- 完成可验证工作单元后，在最终回复前运行 capture。
- recall 失败不得阻断任务，但必须在最终回复中简短说明。
- capture 失败写入待重试队列，不吞掉用户交付结果。
- 不在非项目聊天或简单问答中调用项目记忆。

每日补偿通过 Codex 桌面本地自动化执行。系统不依赖未公开的硬性任务结束钩子，而是用主动 capture 加每日 reconcile 双保险。

## 9. CLI 合同

首版命令为：

- memory-hub init：创建私有运行目录和数据库。
- memory-hub discover：发现项目并报告重复及权限问题。
- memory-hub scan：增量提取项目事实。
- memory-hub recall：生成任务简报。
- memory-hub recall --manual：电脑所有者使用 stdin 本地令牌包装显式查看指定命名空间；不是模型自动回退路径。
- memory-hub capture：保存工作单元增量。
- memory-hub reconcile：执行每日补偿。
- memory-hub import chatgpt：导入官方 ChatGPT 导出。
- memory-hub compact：压缩长期不活跃项目。
- memory-hub doctor：检查权限、数据库、适配器、自动化和 AGENTS 集成。
- memory-hub serve：仅在本机回环地址启动控制台。
- memory-hub proposal list：列出升级建议。
- memory-hub proposal approve：由用户明确批准建议。
- memory-hub proposal apply：只在干净工作树中为已批准建议创建命名为 codex/memory-hub-proposal-ID 的隔离分支并应用补丁，不直接修改主分支。
- memory-hub proposal rollback：回滚升级。

所有需要机器解析的命令支持 JSON 输出。写操作支持 dry-run，其中 proposal apply 还必须校验审批状态。

## 10. 本地控制台

控制台只绑定 127.0.0.1，不监听局域网。每次安装生成本地访问令牌，验证 Host 和 Origin；浏览器写操作使用 CSRF 防护，不开放宽松 CORS。

控制台包含：

### 10.1 总览

- 项目数量和健康状态
- 最后一次发现、补偿和压缩时间
- 各来源及模型的记忆数量
- 当前平均召回大小
- 权限错误、解析错误和待确认数量

### 10.2 来源

- Codex：默认启用
- ChatGPT：默认启用，展示最后导出时间
- Trae、WorkBuddy、Zcode、QoderWork、Claude Code：默认关闭
- 新适配器的安装状态、能力、健康和版本

### 10.3 项目

- 根目录白名单
- 项目启用状态
- 权限问题
- 重复候选
- 最近变化和不活跃状态

### 10.4 记忆

- 共享事实
- 按来源和模型隔离的行为记忆
- 来源和置信度
- 纠错、归档和删除
- 跨模型共享审批

### 10.5 自我改进

- 建议原因
- 代码或配置差异
- 风险等级
- 测试命令和结果
- 批准、拒绝和回滚

## 11. 安全和隐私

运行目录使用：

    ~/Library/Application Support/Project Memory Hub

目录权限为 0700，数据库、导入记录、待重试队列和备份文件权限为 0600。

核心安全规则：

- 默认不联网。
- 不保存 API Key、登录 Cookie 或访问令牌。
- 不读取未启用适配器的隐藏目录。
- 文件名和内容双重检测敏感数据。
- 敏感内容在进入日志和数据库前丢弃或脱敏。
- 原始对话只做流式读取，不复制进数据库。
- 日志使用轮转和脱敏，不记录会话正文。
- 数据库写入使用事务和外键。
- 已应用 migration 必须是当前程序已知版本的连续前缀；未来版本、断档或乱序历史一律在执行后续 migration 前拒绝。
- 项目根目录不得覆盖整个用户主目录；项目定位同时校验路径文本和文件系统目录身份。
- capture、fact scan 和 receipt 在各自写事务内前后重验项目身份；recall 在读取前后重验，失败时只返回空结果。
- 删除、跨模型共享和升级应用需要显式用户操作。
- 控制台和 CLI 共享同一权限检查层，不能通过界面绕过命名空间隔离。

## 12. 故障处理

### 12.1 权限不足

项目保留在注册表中，状态设为 blocked_permission，控制台显示精确路径、受影响能力和 macOS 修复建议。其它项目继续运行。

### 12.2 输入格式变化

来源级读取失败、未完成尾行或写入事务失败时，检查点不推进，上一有效索引继续服务。对已由换行符完整界定的单条损坏、超限或未知记录，适配器为避免在同一输入上永久卡死，只记录不含原文的警告，清空当前 lifecycle 状态并推进该条位置；同一失效 lifecycle 中的后续记录不会被采信，只有新的有效 `session_meta` 才能恢复。上述异常均使适配器健康状态标记为 degraded。

### 12.3 项目匹配不确定

低置信度匹配进入人工确认队列，不写入任何项目行为命名空间。

### 12.4 中断和重复执行

SQLite 事务、WAL、内容指纹和检查点保证幂等。进程中断后从最后成功检查点继续。

### 12.5 事实冲突

当前文件、Git 和重新执行的验证结果优先。旧事实保留来源但标记为 stale。行为记忆之间的分歧不强行合并。

### 12.6 token 预算溢出

简报生成器按固定优先级裁剪。任何裁剪都先删除低置信度和低相关性背景，不能删除当前未完成事项和直接相关的已验证命令。

### 12.7 自我升级失败

失败建议保持未应用状态。批准后的补丁在 codex/memory-hub-proposal-ID 隔离分支中应用和测试，不直接写主分支。已应用版本保留回滚点；健康检查失败时停止升级并恢复上一稳定版本。

## 13. 技术选型

- Python 3.11 或更高版本
- sqlite3 和 SQLite FTS5
- FastAPI
- Typer
- Pydantic
- 服务端渲染页面和轻量交互，不引入大型前端框架
- pytest
- Playwright

首版不引入外部向量数据库、外部嵌入 API 或常驻系统级守护进程。CLI 由 Codex 指令和桌面自动化调用；控制台按需启动。

如果项目已有 graphify-out/graph.json，可将其作为只读项目事实来源。系统不为无图项目自动启动完整 Graphify 构建。

## 14. 测试策略

核心边界采用先测试后实现。

### 14.1 单元测试

- 项目发现和排除规则
- 路径规范化和重复候选
- 密钥及隐私字段脱敏
- 模型命名空间访问控制
- 内容指纹和幂等
- token 预算裁剪
- SQL 级命名空间过滤先于全文检索
- 项目事实过期
- ChatGPT 项目匹配置信度
- 自我升级审批状态机

### 14.2 集成测试

使用合成夹具覆盖：

- Codex JSONL 会话
- ChatGPT 官方导出 ZIP
- 损坏或版本变化的输入
- 无权限项目
- 重复仓库
- Git 提交和未提交变化
- 中断后的检查点恢复
- 跨模型读写攻击
- 待重试队列

### 14.3 端到端测试

验证：

1. discover 找到测试项目。
2. scan 创建共享事实。
3. recall 只返回当前模型允许的数据。
4. capture 写入增量。
5. reconcile 不重复写入。
6. compact 产生阶段复盘并保留冷记录。
7. 控制台可以处理权限、匹配和升级审批。
8. 重启后数据库和检查点保持一致。
9. token 基准报告同时给出候选上下文大小、最终简报大小和缩减比例。

最后选择至少三个真实项目进行本机只读烟雾验证。真实项目内容不得复制进测试仓库。

## 15. 验收标准

- 召回目标不超过约 800 tokens；已知 tokenizer 时以真实计数为准，未知时使用保守估算。
- 在标注夹具中，相比拼接候选上下文，最终简报至少减少 80% tokens，同时保留全部当前状态、直接相关的已验证命令和未完成事项。
- Codex 查询返回零条 ChatGPT 私有行为记忆，反向同样成立。
- 重复导入同一会话、任务或 ZIP 不增加重复记录。
- 敏感夹具在数据库和日志中均不存在。
- 权限、解析和匹配错误全部可见，不静默丢失。
- 强制终止进程后可以从最后成功检查点恢复。
- 没有项目变化时，日常增量检查目标在 30 秒内完成。
- 本地 recall 目标在 1 秒内完成。
- 未经批准的自我升级无法应用。
- 升级验证失败时旧版本仍可工作。
- 项目仓库不会因扫描和召回产生未预期修改。

## 16. 分阶段交付边界

实现计划将按以下产品边界拆分：

1. 数据库、项目发现、脱敏和命名空间隔离。
2. Codex 适配器、capture、recall 和每日补偿。
3. ChatGPT 官方导出适配器。
4. 本地网页控制台。
5. 长期压缩、自我改进建议、审批和回滚。
6. Codex 全局指令与桌面自动化安装、doctor 和真实项目验证。

向量检索和其它软件适配器在首版完成并经过使用验证后另行设计。
