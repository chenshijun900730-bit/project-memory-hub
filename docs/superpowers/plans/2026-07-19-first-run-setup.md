# Project Memory Hub 首次配置向导实施计划

> **For agentic workers:** REQUIRED SUB-SKILLS: use `executing-plans` and
> `test-driven-development`. Every production behavior change must be preceded by a focused
> failing test. Do not start a later task while the current focused checks are red.

**Goal:** 在不改变记忆引擎、来源权限、模型隔离或 Codex 自动任务所有权的前提下，
新增可恢复、幂等的 CLI/Web 首次配置向导，让新用户不用编辑 TOML 即可完成本地根目录、
Codex/ChatGPT 来源和每日 reconcile 期望时间配置，并清楚知道下一步由谁授权执行。

**Architecture:** 新增共享 `SetupService` 作为唯一向导状态与写入入口。CLI 和 Web 只做
输入解析与展示。完成态和设置写入同一份配置文件；新生成配置标记为未完成，旧配置缺少
标记时按已完成处理。Web 沿用现有 loopback/token/session/CSRF/CSP 边界；自动任务只读
检查，真实创建继续交给 Codex 宿主。向导不会隐式执行 discovery、import、reconcile、
行为记忆写入或可选来源探针。

**Tech stack:** Python 3.11/3.12、Typer、FastAPI/Starlette、Jinja2、原生 JavaScript、
SQLite、Playwright/Chromium、pytest、Ruff、strict mypy、Graphify。

**Approved direction:** 用户于 2026-07-19 明确同意推荐方案：统一 Web `/setup` 与 CLI
`memory-hub setup`，Codex/ChatGPT 默认启用、模型记忆严格隔离、可恢复且不要求手改 TOML；
每日任务仍需要 Codex 宿主授权。

---

## 锁定边界

- 只允许 Codex、ChatGPT 进入 `enabled_sources`；Trae、WorkBuddy、Zcode、QoderWork、
  Claude Code 等仍是只读探针，向导不提供 Enable/Import。
- 向导不接收 `model_id`，不猜测 namespace，不改变
  `project_id + source_agent + model_id` 的行为记忆隔离。
- Web/CLI 不编辑 `~/.codex/automations`。只保存 `daily_reconcile_time` 并返回
  `current`、`authorization_required`、`drifted` 或 `unavailable` 等诚实状态。
- 无显式修改参数的 CLI `setup` 是只读检查；重复运行不得改写已有配置、token 或数据库。
- 配置提交不得丢失 `codex_project_id`、改进仓库或验证命令。
- 新配置显式 `setup_completed = false`；旧配置缺字段时按 `true` 加载，避免升级后强制向导。
- 完成态与设置必须在同一次原子配置写入中提交；并发过期写入不得覆盖较新配置。
- Setup 表单使用独立的小请求体限制、URL-encoded 精确字段 allowlist、固定 303 跳转；
  不接受上传、JSON、开放重定向字段或 token。
- 向导只显示安全汇总，不回显 access token、异常、私有会话内容或不必要的绝对路径。
- 不自动创建 remote、push、tag、Release 或 PyPI 发布。

## 任务依赖

| Task | 交付 | 依赖 |
|---|---|---|
| 1 | 配置完成态、并发保护与安全原子保存 | 无 |
| 2 | 共享 SetupService 状态机 | Task 1 |
| 3 | CLI `memory-hub setup` | Task 2 |
| 4 | Web `/setup` 安全路由和服务端向导 | Tasks 1–2 |
| 5 | 中英文、响应式与浏览器流程 | Task 4 |
| 6 | 文档、完整验证、独立复核与提交 | Tasks 1–5 |

---

## Task 1: 建立向后兼容且并发安全的配置完成态

**Files:**

- Modify: `src/project_memory_hub/config.py`
- Modify: `src/project_memory_hub/services/control.py`
- Modify: `src/project_memory_hub/container.py`
- Modify/Create: `tests/unit/test_config.py`
- Modify: `tests/integration/test_proposal_container.py`
- Create: `tests/integration/test_config_concurrency.py`

- [x] 先写失败测试，固定新默认配置 incomplete、旧配置缺字段按 complete、序列化往返、
  隐藏字段不丢失、相同写入幂等、过期并发写入冲突、symlink/hardlink/权限异常和写失败回滚。
- [x] 运行 focused RED，确认失败来自完成态与 CAS 尚不存在。
- [x] 最小实现 `setup_completed` 和安全保存接口；保存继续使用私有临时文件、fsync、
  原子替换，并在替换前复核父目录、目标身份和 expected revision。
- [x] 让 Settings/source 保存完整保留所有非表单字段，并把冲突映射成稳定的输入错误。
- [x] 运行 focused GREEN、Ruff、mypy 和 `git diff --check`。

## Task 2: 新增共享 SetupService

**Files:**

- Create: `src/project_memory_hub/services/setup.py`
- Modify: `src/project_memory_hub/container.py`
- Create: `tests/unit/services/test_setup.py`
- Create: `tests/integration/test_setup_service.py`

- [x] 先写失败测试固定 `inspect()` 快照、下一步状态、来源 allowlist、根目录校验、
  完成幂等性、automation 零写入和配置重启恢复。
- [x] 运行 RED，确认只因共享服务不存在而失败。
- [x] 实现 `SetupSnapshot`、`SetupRequest`、`SetupResult` 和 `SetupService`：
  读取 overview/config/automation 状态，显式应用本地设置，完成向导；不执行 discovery、
  import、reconcile、probe、token rotation 或 namespace 推断。
- [x] 配置改变后从磁盘重新读取快照，不继续使用容器启动时的旧 `config`。
- [x] 运行 focused GREEN、Ruff、mypy。

## Task 3: 增加 CLI `memory-hub setup`

**Files:**

- Modify: `src/project_memory_hub/cli.py`
- Modify: `tests/integration/test_cli_core.py`
- Create: `tests/integration/test_cli_setup.py`

- [x] 先写 CLI RED：无参数只读；显式 root/source/time 保存；`--complete` 幂等；非法、
  重复或可选来源失败且零写；JSON/text 不泄露 token/私有路径；两次状态稳定。
- [x] 实现命令和简洁文本下一步。`init` 的首个下一步改为 `memory-hub setup`，但保留
  discovery、集成和 doctor 的显式命令。
- [x] 自动任务缺失时只输出 `authorization_required` 和宿主交接说明，不声称已启用。
- [x] 运行 CLI focused GREEN 和帮助/稳定错误码契约。

## Task 4: 增加安全的 Web `/setup`

**Files:**

- Modify: `src/project_memory_hub/security/web.py`
- Modify: `src/project_memory_hub/web/routes.py`
- Create: `src/project_memory_hub/web/templates/setup.html`
- Modify: `src/project_memory_hub/web/templates/base.html`
- Modify: `src/project_memory_hub/web/templates/overview.html`
- Modify: `src/project_memory_hub/web/templates/settings.html`
- Create: `tests/integration/test_web_setup.py`
- Modify: `tests/integration/test_web_security.py`

- [x] 先写 Web RED：认证、Host/Origin、CSRF、Content-Type、256 KiB setup body 上限、
  精确字段和固定 redirect；失败必须发生在服务写入前。
- [x] 实现 GET `/setup`、POST `/setup/configure`、POST `/setup/complete`，全部位于现有
  CSRF router 内，服务端渲染且不新增 API/fetch/内联脚本。
- [x] 配置与完成操作复用共享 SetupService，并在同一份带 revision 的完整表单中原子提交；重跑向导不覆盖设置。
- [x] 页面显示模型隔离、自动检查摘要、项目发现/首次记忆的显式下一步和 Codex 自动任务
  授权边界；不展示 token 或原始异常。
- [x] Overview 对未完成的新配置显示 CTA，Settings 提供“重新打开向导”；旧用户不强制跳转。
- [x] 运行 focused GREEN 和 Web 安全契约。

## Task 5: 完成双语、响应式与真实浏览器体验

**Files:**

- Modify: `src/project_memory_hub/web/static/i18n.js`
- Modify: `src/project_memory_hub/web/static/app.css`
- Modify: `tests/unit/test_web_i18n.py`
- Modify: `tests/integration/test_web_routes.py`
- Modify: `tests/e2e/test_dashboard.py`

- [x] 先写静态和 Chromium RED：所有 setup key 英中齐全、语言切换可持久化、默认勾选
  Codex/ChatGPT、可选来源无启用控件、保存/关闭/重开恢复、完成进入 Overview、手机宽度可读。
- [x] 增加服务端 stepper、状态卡、固定操作区与响应式单列布局；沿用现有视觉和 focus 规范。
- [x] i18n 继续仅更新 `textContent`/aria/title，不写 `innerHTML`、input value 或远程资源。
- [x] 运行 i18n、静态契约和 Chromium E2E GREEN，保存必要截图供人工复核但不提交私人数据。

## Task 6: 文档、全量验证和提交

**Files:**

- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/getting-started.md`
- Modify: `docs/operations.md`
- Modify: `CHANGELOG.md`
- Modify: relevant documentation/release contract tests

- [x] 先写文档契约 RED，要求 Quick Start 使用 `memory-hub setup`，并明确自动任务宿主授权、
  来源 allowlist、模型隔离、无隐式扫描/导入。
- [x] 更新英中文档和变更日志，不声称未实际发布或未验证的平台能力。
- [x] 运行 focused tests、完整 pytest+branch coverage、JS 语法/静态契约、Chromium E2E、Ruff、
  strict mypy、wheel/sdist smoke、`git diff --check`。
- [x] 运行独立实现审查与安全复核；修复后重复相关验证。
- [x] 因仓库已有 `graphify-out/graph.json` 且文档有变更，按当前 CLI 运行 `graphify update .`；确认
  `graphify hook status`。
- [x] 审计 diff 与工作树，仅暂存本功能文件，以中文规范提交；不 push/tag/release。
