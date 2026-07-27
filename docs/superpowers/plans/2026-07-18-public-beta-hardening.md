# Project Memory Hub 0.2.1 Public Beta Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `executing-plans` to implement this
> plan task by task. Production behavior changes must follow strict red-green-refactor.
> Documentation, workflow, and generated-asset changes use contract tests before files are
> added. Do not start a later task while the focused checks for the current task are red.

**Goal:** 在不改变记忆引擎、隔离边界和审批语义的前提下，把 Project Memory Hub
准备成可信的 `0.2.1` Public Beta：首次使用更清楚、公开资料完整、兼容性声明有证据、
演示素材不含私人数据、GitHub 自动化只生成草稿发布、公开快照不携带私人历史。

**Architecture:** 保留现有引擎为唯一数据路径，在其外侧增加四层发布外壳：展示层、
文档与合成演示层、构建兼容验证层、GitHub 治理与草稿发布层。公开截图只连接隔离的
合成 runtime；公开快照从已审计 tree 创建无父提交，既不重写本地 `main`，也不创建
remote、push、tag 或 PyPI 发布。

**Tech Stack:** Python 3.11/3.12、Typer、FastAPI/Starlette、Jinja2、原生 JavaScript、
SQLite、Playwright/Chromium、pytest、branch coverage、Ruff、strict mypy、Hatchling、
uv、GitHub Actions、CodeQL、Dependabot、Graphify。

**Approved design:**
`docs/superpowers/specs/2026-07-18-public-beta-hardening-design.md`

---

## 开始前锁定的边界

- 当前实施分支是 `codex/public-beta-hardening`；保留本地 `main` 和既有私人历史。
- 每个 Task 是一个独立 verified work unit；开始和结束时遵循仓库 AGENTS 中的
  Project Memory Hub reconcile/context/recall/capture 流程，namespace 必须使用 resolver
  返回的精确值。记忆工具不可用时继续交付并如实披露，不能手猜 model ID。
- 产品版本使用 `0.2.1`，Beta 是成熟度标签和 classifier，不使用 PEP 440 预发布后缀。
- 不新增 migration；最新 schema migration 仍为 `0010_codex_deferred_records.sql`。
- 不改变 memory kind、lifecycle、compaction、recall 排序、800-token 上限或 token 计算。
- 不改变 `project_id + source_agent + model_id` 的严格行为记忆隔离，也不枚举其他模型。
- 不改变 Codex/ChatGPT 导入语义；Trae、WorkBuddy、Zcode、QoderWork、Claude Code
  仍是只读探针，永远没有 Enable 或 Import 动作。
- 不改变 CLI JSON 字段、稳定错误码、退出码和命令语义。
- 不改变 Web 路由、HTTP 状态、Host/loopback、token、session、CSRF、CSP 和请求体限制。
- 不放宽 promotion/self-improvement 审批，不自动 apply、merge、push 或外部上传。
- macOS 是唯一正式支持平台；Linux 只能标为 experimental；Windows 明确 unsupported；
  Chromium 是本次唯一有 E2E 证据的浏览器。
- 所有测试、截图和演示数据只使用临时目录与固定虚构内容，禁止读取默认用户 runtime。
- 计划内可以创建本地独立公开快照分支和 worktree，但不能创建 GitHub 仓库、remote、
  tag、Release、公开可见性变更或 PyPI 凭据。
- 若新增展示与既有冻结契约冲突，撤回或缩小展示改动，不能修改测试去认可行为漂移。

## 当前证据基线

- 版本仍为 `0.2.0`，`scripts/verify_wheel.py` 仍硬编码该版本。
- 根目录 `uv.lock` 存在但被 `.gitignore` 排除，尚未形成公开可复现基线。
- 已有 Python branch coverage 门禁为 85%，Web i18n 有静态和 Chromium 数字覆盖测试。
- 当前 Web 控制台已有中英切换；本计划扩展文案和结构，不重建 i18n 架构。
- 当前 Sources 是 12 列表格，Projects 是完整服务端渲染卡片，Memories 需要手填 model ID。
- 当前仓库没有 LICENSE、GitHub workflows、治理模板或已配置 remote。
- 规划前证据确认 Python 3.11/3.12 的 wheel 安装烟测可用；3.13 尚无完整通过证据。

## 任务依赖与并行边界

| Task | 交付 | 依赖 | 可并行性 |
|---|---|---|---|
| 1 | 冻结契约、版本、许可证、元数据、lock、双语文档与治理 | 无 | 阻塞全部后续发布工作 |
| 2 | 隔离 demo runtime、seed、隐私扫描和临时资产生成管线 | Task 1 | 必须先独立通过，不能提交当前 UI 截图 |
| 3 | CLI 首跑、Web 错误壳、Overview/Nav/Memories | Task 2 | 可与纯文案复核并行；完成后锁住共享 i18n/CSS |
| 4 | Sources 分组与 Projects 客户端渐进展示 | Task 3 | 必须在 Task 3 后，避免 i18n/CSS 冲突 |
| 5 | 用已验证 demo 管线生成并提交最终公开资产 | Task 4 | 只重生成最终 UI，禁止访问 Projects |
| 6 | 构建烟测、macOS CI、Linux experimental、草稿 Release | Tasks 1–5 | 只创建配置，不执行远程发布 |
| 7 | 公开 tree 审计与单根提交快照 | Tasks 1–6 | 最后执行；源 commit 和工作树必须固定且干净 |

## 文件责任图

| 范围 | 主要文件 | 责任 |
|---|---|---|
| 公共包契约 | `pyproject.toml`、`uv.lock`、`scripts/verify_wheel.py` | 版本、元数据、锁、wheel 内容 |
| 公开入口 | `README.md`、`README.zh-CN.md`、`docs/*.md` | 价值、安装、边界、架构、运维 |
| 治理 | `LICENSE`、`SECURITY.md`、`CONTRIBUTING.md`、`.github/*` | 许可证、贡献、安全和模板 |
| CLI 展示 | `src/project_memory_hub/cli.py` | text-only 安全提示和 init 下一步 |
| Web 展示 | `web/presentation.py`、`web/errors.py`、模板、CSS、JS | 纯展示选择、错误壳、渐进披露 |
| 合成演示 | `src/project_memory_hub/demo/*`、`scripts/generate_demo_assets.py` | 隔离 seed、渲染和资产隐私 |
| 发布验证 | `scripts/verify_release_artifacts.py`、workflows | 构建、安装、兼容和草稿发布 |
| 公开快照 | `scripts/audit_public_tree.py`、`scripts/prepare_public_snapshot.py` | tree 脱敏和无父提交快照 |

---

## Task 1: 锁定核心契约并建立完整 0.2.1 Public foundation

**Files:**

- Create: `LICENSE`
- Create: `tests/unit/test_public_beta_contracts.py`
- Create: `tests/unit/test_public_package_contracts.py`
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/project_memory_hub/__init__.py`
- Modify: `scripts/verify_wheel.py`
- Modify: `tests/unit/test_release_verifiers.py`

- [ ] **Step 1: 先写冻结契约与公共包失败测试**

  `test_public_beta_contracts.py` 固定这些不变量：

  - migration 文件集合和最新版本仍停在 `0010`；
  - recall ceiling 仍为 800；
  - strict namespace 测试继续从 SQL 查询前隔离 source/model；
  - Codex/ChatGPT 是仅有可注册 ingestion sources；五个 optional source 的
    `ingestion_allowed` 永远为 false；
  - CLI JSON/exit、Web route/status/security header 的现有测试组必须原样通过。

  `test_public_package_contracts.py` 先断言：

  - `pyproject.toml` 和 `__version__` 都是 `0.2.1`；
  - metadata 含 Apache-2.0 SPDX、Beta classifier、英文 README、非个人作者；
  - 只列已验证 Python 3.11/3.12，不出现 Windows classifier；
  - `uv.lock` 已跟踪且 `uv lock --check` 可通过；
  - LICENSE 是完整 Apache License 2.0 正文；
  - wheel verifier 从项目 metadata 取得 expected version，不含字符串 `0.2.0`。

- [ ] **Step 2: 运行 RED 并确认只因发布基础尚未实现而失败**

  Run:

  ```bash
  uv run pytest \
    tests/unit/test_public_beta_contracts.py \
    tests/unit/test_public_package_contracts.py \
    tests/unit/test_release_verifiers.py -q
  ```

  Expected: FAIL，原因是版本仍为 `0.2.0`、LICENSE/metadata 缺失、lock 未跟踪、
  wheel verifier 仍硬编码；冻结的引擎契约断言必须保持通过。

- [ ] **Step 3: 最小实现许可证、metadata、版本与 lock**

  - 从 Apache 官方文本写入完整 `LICENSE`，不得缩写或自行改写条款。
  - `pyproject.toml` 增加 description、`readme = "README.md"`、SPDX license、
    neutral contributor author、keywords、Beta/macOS/Python classifiers。
  - 0.2.1 首先声明 `requires-python = ">=3.11,<3.13"`；只有后续干净 artifact
    matrix 真实证明新版本后，才能在独立变更中放宽。
  - test extra 明确加入 `twine`、`PyYAML` 和 `Pillow` 的有界版本，并由 package
    contract 锁定；不把这些发布/YAML/图片工具塞入普通用户 runtime dependencies。
  - 从 `.gitignore` 移除 `uv.lock`，重新锁定并提交它。
  - `scripts/verify_wheel.py` 使用 `tomllib` 读取项目名/版本并检查 wheel、METADATA、
    entry point、模板和静态资源；禁止用 glob 中的版本字符串作为可信来源。
  - 不增加 project URL；remote 尚不存在时不写占位或猜测地址。

- [ ] **Step 4: GREEN，验证构建物和冻结契约**

  Run:

  ```bash
  uv lock --check
  uv sync --locked --extra test
  uv run pytest \
    tests/unit/test_public_beta_contracts.py \
    tests/unit/test_public_package_contracts.py \
    tests/unit/test_release_verifiers.py \
    tests/unit/storage/test_namespace_isolation.py \
    tests/unit/services/test_recall.py \
    tests/integration/test_probe_cli.py \
    tests/integration/test_web_security.py -q
  uv run python scripts/verify_wheel.py
  dist_dir="$(mktemp -d)"
  uv build --wheel --sdist --out-dir "$dist_dir"
  uv run twine check "$dist_dir"/*
  ```

  Expected: PASS；wheel/sdist 都标记 0.2.1，无 migration、namespace、source、JSON、
  HTTP 或安全边界漂移。

- [ ] **Step 5: 静态质量 checkpoint，继续完成同一 Public foundation Task**

  Run:

  ```bash
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy src/project_memory_hub
  git diff --check
  graphify hook status
  ```

  Expected: PASS。此 checkpoint 不提交；继续完成下面的 Public foundation Part B，
  避免对外留下 metadata 已指向但文档/治理尚不完整的中间 commit。

---

### Task 1 Part B: 建立英中公开入口和仓库治理

**Files:**

- Rewrite: `README.md`
- Create: `README.zh-CN.md`
- Create: `docs/getting-started.md`
- Create: `docs/architecture.md`
- Create: `docs/security.md`
- Create: `docs/releasing.md`
- Modify: `docs/operations.md`
- Create: `SECURITY.md`
- Create: `CONTRIBUTING.md`
- Create: `CODE_OF_CONDUCT.md`
- Create: `CHANGELOG.md`
- Create: `.github/ISSUE_TEMPLATE/bug_report.yml`
- Create: `.github/ISSUE_TEMPLATE/feature_request.yml`
- Create: `.github/ISSUE_TEMPLATE/config.yml`
- Create: `.github/pull_request_template.md`
- Create: `tests/unit/test_public_documentation.py`
- Create: `tests/unit/test_public_repository_contracts.py`
- Create: `scripts/verify_document_links.py`

- [ ] **Step 1: 先写文档信息架构和治理契约的 RED 测试**

  测试必须断言：

  - 英文 `README.md` 首屏依次出现中文入口、Beta、价值句、四个核心收益和五分钟路径；
  - `README.zh-CN.md` 是完整镜像，不只是摘要，命令和产品边界与英文一致；
  - 两份 README 都含 source/platform/browser matrix，明确 macOS supported、Linux
    experimental、Windows unsupported、Chromium verified；
  - Quick Start 使用普通本地包安装，不把 editable/force 安装写成用户默认路径；
  - 不声称 ChatGPT 实时同步、optional source 导入、自动改码、自动 merge/push 或 PyPI；
  - 所有相对链接存在，不含占位 repository URL；
  - SECURITY/CONTRIBUTING/Issue/PR 模板要求删除路径、token、会话正文、数据库内容、
    私人截图和模型凭据；
  - CHANGELOG 标记 0.2.1 Beta，但不声称 GitHub Release 或 PyPI 已发布；
  - `SECURITY.md` 使用 GitHub 私密漏洞报告流程，不要求公开个人邮箱。

- [ ] **Step 2: 运行 RED**

  Run:

  ```bash
  uv run pytest \
    tests/unit/test_public_documentation.py \
    tests/unit/test_public_repository_contracts.py -q
  ```

  Expected: FAIL，原因是中文 README、治理文件和聚焦文档尚不存在；不能因放宽断言而绿。

- [ ] **Step 3: 写公开入口和聚焦文档**

  `README.md` 与 `README.zh-CN.md` 使用相同顺序：

  1. 语言切换与 Public Beta 标签；
  2. 一句话价值与 Overview 图片的最终固定相对路径（文件由 Task 5 生成）；
  3. local-first、strict isolation、verified memory、approval-gated change；
  4. 五分钟安装、每一步命令和成功判据；
  5. 来源、平台、Python、浏览器矩阵；
  6. 隐私、不适用场景和已知限制；
  7. 架构、模型隔离、审批流程图的资产引用位置；
  8. getting-started、operations、security、contributing、design 链接。

  在 Task 5 资产生成前，README/docs 引用与 asset manifest contract 已使用最终确定的
  7 个相对文件名；本任务的 link verifier 只允许这 7 个精确路径暂时不存在，不能允许
  任意 broken link。Task 5 生成后删除该精确路径例外。

  把 capture marker、locator 上限、resolution 状态机、备份恢复、probe 内部细节放进
  focused docs；README 只保留用户做决定所需的信息。

- [ ] **Step 4: 写治理文件和隐私贡献规则**

  - Apache-2.0 许可证引用、贡献许可说明和提交规则保持一致，不额外引入冲突许可证。
  - Code of Conduct 使用公开标准文本并采用仓库内 GitHub 报告渠道，不写个人联系方式。
  - Issue forms 使用结构化字段；不开放空白 issue；PR 模板列出真实运行过的验证。
  - `docs/releasing.md` 明确本地准备、远程人工动作和回滚边界；此时不包含真实 URL。

- [ ] **Step 5: GREEN、更新 Graphify 并提交**

  Run:

  ```bash
  uv run pytest \
    tests/unit/test_public_documentation.py \
    tests/unit/test_public_repository_contracts.py -q
  uv run python scripts/verify_document_links.py
  graphify update .
  git diff --check
  ```

  Commit:

  ```bash
  git add .gitignore LICENSE pyproject.toml uv.lock \
    src/project_memory_hub/__init__.py scripts/verify_wheel.py \
    tests/unit/test_public_beta_contracts.py \
    tests/unit/test_public_package_contracts.py \
    tests/unit/test_release_verifiers.py \
    README.md README.zh-CN.md SECURITY.md CONTRIBUTING.md \
    CODE_OF_CONDUCT.md CHANGELOG.md \
    docs/getting-started.md docs/architecture.md docs/security.md \
    docs/releasing.md docs/operations.md \
    .github/ISSUE_TEMPLATE/bug_report.yml \
    .github/ISSUE_TEMPLATE/feature_request.yml \
    .github/ISSUE_TEMPLATE/config.yml \
    .github/pull_request_template.md scripts/verify_document_links.py \
    tests/unit/test_public_documentation.py \
    tests/unit/test_public_repository_contracts.py
  git diff --cached --name-only
  git diff --cached --check
  git commit -m "build(发布): 建立公开 Beta 基础"
  ```

---

## Task 2: 先建立失败关闭的合成演示与隐私验证管线

**Files:**

- Create: `src/project_memory_hub/demo/__init__.py`
- Create: `src/project_memory_hub/demo/runtime.py`
- Create: `src/project_memory_hub/demo/seed.py`
- Create: `src/project_memory_hub/demo/privacy.py`
- Create: `scripts/generate_demo_assets.py`
- Create: `scripts/verify_public_assets.py`
- Create: `tests/unit/demo/test_runtime.py`
- Create: `tests/unit/demo/test_seed.py`
- Create: `tests/unit/demo/test_privacy.py`
- Create: `tests/e2e/test_demo_assets.py`
- Modify: `src/project_memory_hub/container.py`
- Modify: `src/project_memory_hub/discovery/policy.py`
- Modify: `scripts/verify_wheel.py`
- Modify: `tests/unit/test_release_verifiers.py`

本 Task 只证明“能在隔离 runtime 中安全生成临时资产”。输出写入 `mktemp`，不创建或
提交 `docs/assets`。最终 UI 截图必须等 Task 3–4 完成后由 Task 5 重生成。

- [ ] **Step 1: Runtime guard RED——真实或含糊目标全部失败关闭**

  测试覆盖：

  - default runtime、其 parent/child alias、任一路径组件 symlink、已有 database、未知
    非空目录全部拒绝；
  - runtime 必须是显式外部临时目录，不能位于仓库、默认 HOME 数据目录或 output 内；
  - output 只接受新空目录，或带匹配 generator marker 且仅含生成文件 allowlist 的目录；
  - 失败前后默认 database、外部文件 hash 和 Git working tree 完全不变；
  - cleanup 只删除本次创建且带固定 demo marker 的目录，不递归删除调用者已有目录。

  Run:

  ```bash
  uv run pytest tests/unit/demo/test_runtime.py -q
  ```

  Expected: FAIL，demo package 尚不存在。

- [ ] **Step 2: Runtime guard GREEN 与固定 seed RED/GREEN**

  - `runtime.py` 使用 resolved fd/stat 校验显式目录，拒绝 symlink 和默认 RuntimePaths。
  - `seed.py` 通过现有 migration/container/repository 路径建立临时 store；只在固定展示
    时间/ID 必需时使用小型受测 seed adapter，不增加正式 schema 或第二条记忆路径。
  - inventory 使用固定虚构项目、固定 UTC 时间、固定 synthetic UUID allowlist、
    Codex/ChatGPT 两个精确 model namespace、一个待审批 proposal 和 `DEMO DATA` 标识。
  - 测试证明两个 model namespace 不能互查，五个 optional source 仍不可导入。

  Run:

  ```bash
  uv run pytest tests/unit/demo/test_runtime.py tests/unit/demo/test_seed.py -q
  ```

  Expected: PASS。

- [ ] **Step 3: Privacy scanner RED/GREEN**

  RED fixtures 覆盖 home prefix、token-like string、私有短语、非 allowlist UUID、
  HTML/SVG 可见文字、PNG text chunk、WebP EXIF/XMP、超大文件和损坏 metadata。

  GREEN 必须：

  - 有总文件数、单文件 bytes 和解码长度上限，超限/损坏时 fail closed；
  - 扫描 seed、最终 DOM、SVG、manifest、PNG/WebP metadata；
  - 只允许固定 synthetic UUID；额外私人词从仓库外 denylist 读取，不写入报告或 Git；
  - 不声称 OCR。截图文字安全由“隔离 seed + 截图前 DOM 扫描 + DOM hash 绑定”证明，
    图片生成后再验证 metadata 和尺寸。

  Run:

  ```bash
  uv run pytest tests/unit/demo/test_privacy.py -q
  ```

  Expected: PASS；恶意 fixture 全部拒绝，固定 inventory 通过。

- [ ] **Step 4: 临时 E2E 资产管线 RED/GREEN**

  - Playwright 只启动临时配置/runtime，运行前后记录默认 runtime hash。
  - 只访问 Overview、Sources、Memories；route recorder 一旦看到 Projects 立即失败。
  - 截图前注入固定、可见、可访问的 `DEMO DATA` overlay，然后扫描最终 DOM。
  - 固定 viewport、locale、timezone、reduced motion、动画、UTC clock 和 seed IDs。
  - 生成器能输出三张 screenshot、三张 SVG diagram、一个 1280×640 social preview 和
    manifest；本 Task 只写入临时目录。
  - SVG/manifest byte-stable；raster 用固定尺寸、无 metadata、DOM hash 和 seed/version
    manifest 保证语义确定性，不跨 macOS 字体版本强求 PNG byte hash 相同。

  Run:

  ```bash
  uv run pytest tests/e2e/test_demo_assets.py -q
  demo_root="$(mktemp -d)"
  uv run python scripts/generate_demo_assets.py \
    --runtime-dir "$demo_root/runtime" \
    --output-dir "$demo_root/assets"
  uv run python scripts/verify_public_assets.py "$demo_root/assets"
  ```

  Expected: PASS；临时资产完整，DOM/metadata/尺寸/route receipt 通过，默认 runtime 不变。

- [ ] **Step 5: 质量、wheel 接缝与提交**

  - demo 模块可以随 wheel 分发，但不增加公开 CLI 命令；Playwright/Pillow 只在 test extra。
  - wheel verifier 固定 demo package 文件，不把临时 output 或测试资产打进 wheel。

  Run:

  ```bash
  uv run pytest tests/unit/demo tests/e2e/test_demo_assets.py \
    tests/unit/test_release_verifiers.py -q
  uv run ruff format --check src/project_memory_hub/demo scripts tests/unit/demo \
    tests/e2e/test_demo_assets.py
  uv run ruff check src/project_memory_hub/demo scripts tests/unit/demo \
    tests/e2e/test_demo_assets.py
  uv run mypy src/project_memory_hub/demo
  uv run python scripts/verify_wheel.py
  git diff --check
  ```

  Commit:

  ```bash
  git add \
    src/project_memory_hub/demo/__init__.py \
    src/project_memory_hub/demo/runtime.py \
    src/project_memory_hub/demo/seed.py \
    src/project_memory_hub/demo/privacy.py \
    src/project_memory_hub/container.py \
    src/project_memory_hub/discovery/policy.py \
    scripts/generate_demo_assets.py \
    scripts/verify_public_assets.py \
    scripts/verify_wheel.py \
    tests/unit/demo/test_runtime.py \
    tests/unit/demo/test_seed.py \
    tests/unit/demo/test_privacy.py \
    tests/e2e/test_demo_assets.py \
    tests/unit/test_release_verifiers.py
  git diff --cached --name-only
  git diff --cached --check
  git commit -m "feat(演示): 建立隔离脱敏生成管线"
  ```

---

## Task 3: 改善 CLI 首跑与 Web 基础引导，不改变协议

**Files:**

- Create: `src/project_memory_hub/web/presentation.py`
- Create: `src/project_memory_hub/web/errors.py`
- Create: `src/project_memory_hub/web/templates/error.html`
- Create: `src/project_memory_hub/web/templates/_empty_state.html`
- Modify: `src/project_memory_hub/cli.py`
- Modify: `src/project_memory_hub/security/web.py`
- Modify: `src/project_memory_hub/web/app.py`
- Modify: `src/project_memory_hub/web/routes.py`
- Modify: `src/project_memory_hub/web/templates/base.html`
- Modify: `src/project_memory_hub/web/templates/overview.html`
- Modify: `src/project_memory_hub/web/templates/memories.html`
- Modify: `src/project_memory_hub/web/templates/proposals.html`
- Modify: `src/project_memory_hub/web/static/app.css`
- Modify: `src/project_memory_hub/web/static/i18n.js`
- Modify: `scripts/verify_wheel.py`
- Create: `tests/unit/test_web_presentation.py`
- Create: `tests/integration/test_cli_public_beta_ux.py`
- Create: `tests/integration/test_web_public_beta_ux.py`
- Modify: `tests/unit/test_web_i18n.py`
- Modify: `tests/unit/test_release_verifiers.py`
- Modify: `tests/integration/test_cli_core.py`
- Modify: `tests/integration/test_cli_display_coverage.py`
- Modify: `tests/integration/test_web_app_fallback_coverage.py`
- Modify: `tests/integration/test_web_routes.py`
- Modify: `tests/integration/test_web_security.py`
- Modify: `tests/e2e/test_dashboard.py`

- [ ] **Step 1: 先运行冻结测试，记录未改前契约**

  Run:

  ```bash
  uv run pytest \
    tests/integration/test_cli_core.py \
    tests/integration/test_cli_display_coverage.py \
    tests/integration/test_web_routes.py \
    tests/integration/test_web_security.py \
    tests/unit/storage/test_namespace_isolation.py -q
  ```

  Expected: PASS。记录 JSON、exit、路由、状态、安全头、表单和 namespace 基线；后续测试
  只能新增 text/HTML 展示期望。

- [ ] **Step 2: CLI RED——安全 text 错误和 init 下一步**

  新测试覆盖：

  - text error 同时显示 `error: <code>`、allowlisted 安全 message、该 code 的安全 hint；
  - 任意异常、路径、token、用户输入不能通过 message/hint 回显；未知 code 使用固定 fallback；
  - JSON payload 和退出码与基线逐字节/逐值一致；
  - text `init` 依次给出 discovery dry-run、discovery apply、AGENTS integration、doctor；
  - JSON `init --format json` 仍精确返回 `{"status":"initialized"}`。

  Run:

  ```bash
  uv run pytest tests/integration/test_cli_public_beta_ux.py -q
  ```

  Expected: FAIL，现有 text error 只显示 code，text init 只显示状态。

- [ ] **Step 3: CLI GREEN——只增加 text renderer**

  - 为稳定 error code 建立小型 allowlist message/hint 映射；未知内容回退通用安全文字。
  - `_emit_error()` 的 JSON 分支和所有 exit 映射不动；text 分支不打印任意 exception text。
  - `init` 使用专属 text renderer；不改变初始化顺序、文件、token 或 JSON response。

  Run:

  ```bash
  uv run pytest \
    tests/integration/test_cli_public_beta_ux.py \
    tests/integration/test_cli_core.py \
    tests/integration/test_cli_display_coverage.py \
    tests/integration/test_cli_integration.py \
    tests/integration/test_cli_proposals.py -q
  ```

  Expected: PASS。

- [ ] **Step 4: Web RED——错误壳、导航、Next safe step、精确 model 指导**

  新测试固定：

  - 400/401/403/404/409/413/422/500 保持原状态码和安全头，但 body 是固定英中 HTML；
  - 错误壳同时含 Overview 链接和 allowlisted 文案，不含 exception、request body、path、
    token、query 或用户输入；无需 locale cookie，也不执行 inline script；
  - 当前导航项唯一具有 `aria-current="page"`；
  - `Next safe step` 纯函数只消费现有 Overview/startup 状态：
    - 0 项目：`memory-hub discover --dry-run --format json`；
    - 有项目但 0 facts：从注册项目目录执行 `memory-hub scan --cwd "$PWD" --dry-run --format json`；
    - permission error 或 degraded：`memory-hub doctor --format json`；
    - 其余健康状态：`memory-hub reconcile --if-due --format json`；
  - 每一步有原因和成功判据，页面绝不执行这些命令或额外 host probe；
  - Memories 显示 `memory-hub codex-context --cwd "$PWD" --format json`，不枚举、猜测、
    预填或查询其他 model ID。
  - Memories 和 Proposals 的每个 `.empty` 状态都说明“为何为空、一个有界下一步、什么
    结果代表成功”；命令只作为文本，不由 Web 执行。
  - `Sources` 固定渲染七个 source record、`Imports` 是主动上传表单，二者没有 collection
    empty branch；静态 inventory 断言它们不存在未结构化 `.empty` markup。

  Run:

  ```bash
  uv run pytest \
    tests/unit/test_web_presentation.py \
    tests/integration/test_web_public_beta_ux.py -q
  ```

  Expected: FAIL，现有错误是纯英文文本，导航/引导/精确模型帮助尚不存在。

- [ ] **Step 5: Web GREEN——实现无副作用展示层**

  - `presentation.py` 只含冻结 dataclass/enum 和纯选择函数，不导入 storage、adapter、
    reconcile 或 subprocess。
  - `errors.py` 只接收 allowlisted status code，生成固定英中 shell；security middleware 与
    FastAPI handlers 共用它，只改 body/content-type，不改拒绝顺序、状态或安全头。
  - route 只把已有 snapshot 和 startup status 传给模板，不运行 doctor/reconcile 命令。
  - base template 根据现有 title/route 标记 active nav；Memories/Proposals 只增加静态
    指导，不增加读取或 mutation。
  - `_empty_state.html` macro 固定 reason、one bounded next step、success condition 三段；
    Task 3 先迁移 Memories/Proposals，Task 4 再迁移 Projects 并启用全模板 inventory gate。
  - `i18n.js` 只新增静态键；不能修改表单 value/name/action、model ID、路径或确认口令。
  - 把新模板/模块加入 wheel verifier。

  Run:

  ```bash
  uv run pytest \
    tests/unit/test_web_presentation.py \
    tests/unit/test_web_i18n.py \
    tests/integration/test_web_public_beta_ux.py \
    tests/integration/test_web_app_fallback_coverage.py \
    tests/integration/test_web_routes.py \
    tests/integration/test_web_security.py -q
  node --check src/project_memory_hub/web/static/i18n.js
  uv run pytest tests/e2e/test_dashboard.py -q
  ```

  Expected: PASS；数据库/config/route/form 快照前后完全一致。

- [ ] **Step 6: 质量检查和提交**

  Run:

  ```bash
  uv run ruff format --check src tests scripts
  uv run ruff check src tests scripts
  uv run mypy src/project_memory_hub
  uv run python scripts/verify_wheel.py
  git diff --check
  ```

  Commit:

  ```bash
  git add \
    src/project_memory_hub/cli.py \
    src/project_memory_hub/security/web.py \
    src/project_memory_hub/web/presentation.py \
    src/project_memory_hub/web/errors.py \
    src/project_memory_hub/web/app.py \
    src/project_memory_hub/web/routes.py \
    src/project_memory_hub/web/templates/error.html \
    src/project_memory_hub/web/templates/_empty_state.html \
    src/project_memory_hub/web/templates/base.html \
    src/project_memory_hub/web/templates/overview.html \
    src/project_memory_hub/web/templates/memories.html \
    src/project_memory_hub/web/templates/proposals.html \
    src/project_memory_hub/web/static/app.css \
    src/project_memory_hub/web/static/i18n.js \
    scripts/verify_wheel.py \
    tests/unit/test_web_presentation.py \
    tests/unit/test_web_i18n.py \
    tests/unit/test_release_verifiers.py \
    tests/integration/test_cli_public_beta_ux.py \
    tests/integration/test_web_public_beta_ux.py \
    tests/integration/test_cli_core.py \
    tests/integration/test_cli_display_coverage.py \
    tests/integration/test_web_app_fallback_coverage.py \
    tests/integration/test_web_routes.py \
    tests/integration/test_web_security.py \
    tests/e2e/test_dashboard.py
  git diff --cached --name-only
  git diff --cached --check
  git commit -m "feat(体验): 增加安全首跑与页面引导"
  ```

---

## Task 4: 重排 Sources 并为 Projects 增加纯客户端渐进展示

**Files:**

- Modify: `src/project_memory_hub/web/presentation.py`
- Modify: `src/project_memory_hub/web/templates/sources.html`
- Modify: `src/project_memory_hub/web/templates/projects.html`
- Modify: `src/project_memory_hub/web/static/app.css`
- Modify: `src/project_memory_hub/web/static/i18n.js`
- Create: `src/project_memory_hub/web/static/projects.js`
- Modify: `scripts/verify_wheel.py`
- Create: `tests/unit/test_web_projects_js.py`
- Create: `tests/integration/test_web_collection_ux.py`
- Modify: `tests/unit/test_web_i18n.py`
- Modify: `tests/unit/test_release_verifiers.py`
- Modify: `tests/integration/test_web_routes.py`
- Modify: `tests/integration/test_web_security.py`
- Modify: `tests/e2e/test_dashboard.py`

- [ ] **Step 1: Sources RED——两组诚实能力卡**

  测试要求：

  - 页面有且仅有 `Ingestion sources` 与 `Read-only probes` 两组；
  - Codex/ChatGPT 显示注册、运行和允许动作；五个 optional tool 显示安装、访问、
    model verification、probe capability 和 `Behavior import: Locked`；
  - warning code 只在 `<details>` 内展开；默认视图不再是 12 列横向表格；
  - Trae 仍只有原 `Further check` POST；optional tool 永远没有 Enable/Import；
  - 所有原 form action/method/name/value、CSRF 和深度探针瞬时语义保持不变。

  Run:

  ```bash
  uv run pytest tests/integration/test_web_collection_ux.py -k sources -q
  ```

  Expected: FAIL，现有页面仍为 12 列表格。

- [ ] **Step 2: Sources GREEN——只重排已有 view model**

  - 用 `presentation.py` 对现有 `SourceControlRecord` 做展示分组，不创建新能力来源。
  - 模板保留所有服务端 action，技术 warning 放入 `<details>`，移动端使用纵向 card。
  - 可用性和 ingestion 状态必须来自现有 `available`/`probe` 字段，不能用标签文字反推。

  Run:

  ```bash
  uv run pytest \
    tests/integration/test_web_collection_ux.py -k sources \
    tests/integration/test_web_routes.py \
    tests/integration/test_web_security.py -q
  ```

  Expected: PASS。

- [ ] **Step 3: Projects RED——本地搜索、过滤、计数和路径披露**

  测试要求：

  - 搜索只匹配已渲染的 display name/安全 ID；status filter 只操作 DOM；
  - 页面显示结果数，首批固定显示 12 张卡，`Show more` 每次再显示 12 张；
  - 默认不显示完整 canonical path，用户展开 `<details>` 后才看到；
  - 无 JavaScript 时所有卡片、路径和操作仍可访问；
  - `projects.js` 不包含 fetch、XHR、WebSocket、storage 或外部 URL；
  - JS 前后 form action/method/name/value、CSRF、数据库和配置快照完全一致；
  - 不增加 route、API、server-side query、mutation 或跨页面 model 信息。
  - Projects 的三个 `.empty` 状态都解释原因、一个有界下一步和成功判据；健康的
    “无问题”状态明确允许不采取动作。
  - 全模板 inventory 逐个检查所有 `.empty` 只能来自 `_empty_state.html` macro，并在
    Chromium 空 runtime 中验证 reason/next/success；新增未迁移空状态会直接失败。

  Run:

  ```bash
  uv run pytest \
    tests/unit/test_web_projects_js.py \
    tests/integration/test_web_collection_ux.py -k projects -q
  ```

  Expected: FAIL，搜索、过滤、分页和路径折叠尚不存在。

- [ ] **Step 4: Projects GREEN——渐进增强且 no-JS 完整可用**

  - template 只增加受控 `data-*`、filter controls、`details` 和外部 defer script。
  - JS 初始化成功后才添加 enhancement class 并隐藏超出首批的卡；初始化失败时全量可见。
  - 所有可见文字进入英中词典；品牌、ID、路径、时间、机器码不翻译。
  - 把 `projects.js` 加入 wheel verifier、CSP 静态脚本契约和 JS 数字/分支 E2E 范围。

  Run:

  ```bash
  node --check src/project_memory_hub/web/static/i18n.js
  node --check src/project_memory_hub/web/static/projects.js
  node --check src/project_memory_hub/web/static/sources.js
  uv run pytest \
    tests/unit/test_web_projects_js.py \
    tests/unit/test_web_i18n.py \
    tests/unit/test_release_verifiers.py \
    tests/integration/test_web_collection_ux.py \
    tests/integration/test_web_routes.py \
    tests/integration/test_web_security.py \
    tests/e2e/test_dashboard.py -q
  ```

  Expected: PASS；Chromium 验证过滤、显示更多、路径展开、语言切换和表单不变。

- [ ] **Step 5: 质量检查与提交**

  Run:

  ```bash
  uv run ruff format --check src tests scripts
  uv run ruff check src tests scripts
  uv run mypy src/project_memory_hub
  uv run python scripts/verify_wheel.py
  git diff --check
  ```

  Commit:

  ```bash
  git add \
    src/project_memory_hub/web/presentation.py \
    src/project_memory_hub/web/templates/sources.html \
    src/project_memory_hub/web/templates/projects.html \
    src/project_memory_hub/web/static/app.css \
    src/project_memory_hub/web/static/i18n.js \
    src/project_memory_hub/web/static/projects.js \
    scripts/verify_wheel.py \
    tests/unit/test_web_projects_js.py \
    tests/unit/test_web_i18n.py \
    tests/unit/test_release_verifiers.py \
    tests/integration/test_web_collection_ux.py \
    tests/integration/test_web_routes.py \
    tests/integration/test_web_security.py \
    tests/e2e/test_dashboard.py
  git diff --cached --name-only
  git diff --cached --check
  git commit -m "feat(控制台): 改善来源与项目浏览体验"
  ```

---

## Task 5: 用已验证管线生成最终公开资产

**Files:**

- Create: `docs/assets/screenshots/overview.png`
- Create: `docs/assets/screenshots/sources.png`
- Create: `docs/assets/screenshots/memories.png`
- Create: `docs/assets/diagrams/local-data-flow.svg`
- Create: `docs/assets/diagrams/strict-model-isolation.svg`
- Create: `docs/assets/diagrams/approval-gated-improvement.svg`
- Create: `docs/assets/social-preview.png`
- Create: `docs/assets/demo-manifest.json`
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `scripts/verify_document_links.py`
- Modify: `tests/e2e/test_demo_assets.py`
- Modify: `tests/unit/test_public_documentation.py`

- [ ] **Step 1: 先写“最终 UI 已进入合成资产”的 RED 契约**

  新断言要求：

  - Overview screenshot 包含 Next safe step 和当前导航态；
  - Sources screenshot 显示 ingestion/read-only 两组且 optional sources 全部 Locked；
  - Memories screenshot 显示 exact `codex-context` 指导和一个已选择的虚构 model ID；seed
    中第二个 model namespace 必须存在但不得在该页面被枚举或渲染；
  - route receipt 只含 `/`、`/sources`、`/memories`，任何 `/projects` 都失败；
  - 每张截图有可见 `DEMO DATA`，social preview 精确为 1280×640；
  - manifest 的 7 个固定资产必须存在；两份 README 显示 Overview hero 和三张架构图、
    链接 Sources/Memories gallery，social preview 单独验证而不要求嵌入正文；
    link verifier 不再允许缺失例外。

  Run:

  ```bash
  uv run pytest tests/e2e/test_demo_assets.py \
    tests/unit/test_public_documentation.py -q
  ```

  Expected: FAIL，Task 2 只验证了临时管线，最终资产尚未提交。

- [ ] **Step 2: 从最终 UI 重生成资产，不手工编辑截图**

  - Playwright 只启动临时配置和 runtime；启动前/后分别记录 default runtime hash。
  - 只访问 Overview、Sources、Memories，测试禁止导航到 Projects。
  - 截图前注入固定、可见、可访问的 `DEMO DATA` overlay，然后扫描最终 DOM。
  - 固定 viewport、locale、timezone、reduced motion、动画、UTC clock 和 seed IDs。
  - 三张截图使用固定尺寸；三张 SVG 使用现有纸张/墨色/安全绿/警告红视觉语言；
    social preview 精确为 1280×640。
  - SVG/manifest 必须 byte-stable；raster 以固定尺寸、无 metadata、DOM hash、seed/version
    manifest 保证语义确定性，不跨不同 macOS 字体版本强求 PNG byte hash 相同。

  Run:

  ```bash
  demo_root="$(mktemp -d)"
  uv run python scripts/generate_demo_assets.py \
    --runtime-dir "$demo_root/runtime" \
    --output-dir docs/assets
  uv run python scripts/verify_public_assets.py docs/assets
  uv run pytest tests/e2e/test_demo_assets.py -q
  ```

  Expected: PASS，生成 3 PNG screenshot、3 SVG diagram、1 个 1280×640 PNG 和 manifest；
  资产中无真实路径、项目名、token、live UUID、会话内容或私人 metadata。

- [ ] **Step 3: 接入 README、移除精确资产缺失例外并提交**

  - 两份 README 使用真实相对路径展示 Overview hero、三个架构图，并链接更多截图。
  - `verify_document_links.py` 删除 Task 1 的精确资产缺失例外，任一缺图/错名立即失败。
  - 生成工具仍是开发工具，不新增公开 CLI 命令或 runtime dependency。

  Run:

  ```bash
  uv run pytest tests/unit/demo tests/e2e/test_demo_assets.py \
    tests/unit/test_public_documentation.py -q
  uv run python scripts/verify_document_links.py
  uv run python scripts/verify_public_assets.py docs/assets
  graphify update .
  git diff --check
  ```

  Commit:

  ```bash
  git add \
    scripts/verify_document_links.py \
    docs/assets/screenshots/overview.png \
    docs/assets/screenshots/sources.png \
    docs/assets/screenshots/memories.png \
    docs/assets/diagrams/local-data-flow.svg \
    docs/assets/diagrams/strict-model-isolation.svg \
    docs/assets/diagrams/approval-gated-improvement.svg \
    docs/assets/social-preview.png \
    docs/assets/demo-manifest.json \
    README.md README.zh-CN.md \
    tests/e2e/test_demo_assets.py \
    tests/unit/test_public_documentation.py
  git diff --cached --name-only
  git diff --cached --check
  git commit -m "feat(演示): 生成最终脱敏公开素材"
  ```

---

## Task 6: 建立可复现 CI、兼容证据和仅草稿发布

**Files:**

- Create: `scripts/verify_release_artifacts.py`
- Create: `scripts/smoke_install_artifact.py`
- Create: `scripts/create_checksums.py`
- Create: `scripts/verify_workflows.py`
- Create: `tests/unit/test_release_artifacts.py`
- Create: `tests/unit/test_release_checksums.py`
- Create: `tests/unit/test_github_workflows.py`
- Create: `.github/workflows/ci.yml`
- Create: `.github/workflows/linux-experimental.yml`
- Create: `.github/workflows/codeql.yml`
- Create: `.github/workflows/release-draft.yml`
- Create: `.github/dependabot.yml`
- Create: `.github/secret_scanning.yml`
- Modify: `docs/releasing.md`
- Modify: `README.md`
- Modify: `README.zh-CN.md`

- [ ] **Step 1: Artifact verifier RED/GREEN**

  测试要求 verifier：

  - 只接受一个 wheel、一个 sdist，项目名/版本/entry point/METADATA 必须一致；
  - 拒绝重复、缺失、错误版本、缺模板/静态文件、异常归档路径或额外 release artifact；
  - 分别用 Python 3.11 和 3.12 创建全新临时 venv 并安装 wheel，显式隔离
    HOME/config/data/cache 后执行：
    `memory-hub --help`、`memory-hub version`、`memory-hub init --format json`、
    `memory-hub doctor --format json` 和 loopback serve startup smoke；
  - smoke 前后默认用户 runtime 与仓库都不变；临时进程按时退出并清理。
  - verifier 只接受 `uv python find --system` 返回且 resolve 后位于仓库/`.venv` 之外的
    绝对解释器；再用 `uv venv --python <absolute-path>` 创建环境，并只调用新 venv 的绝对
    Python/`memory-hub` 路径，不能复用项目 `.venv` 或 ambient `python`。

  Run RED, implement, then GREEN:

  ```bash
  uv run pytest tests/unit/test_release_artifacts.py -q
  release_dist="$(mktemp -d)"
  uv build --wheel --sdist --out-dir "$release_dist"
  uv run twine check "$release_dist"/*
  python_311="$(uv python find --system 3.11)"
  python_312="$(uv python find --system 3.12)"
  uv run python scripts/verify_release_artifacts.py \
    --dist "$release_dist" \
    --smoke-python "$python_311" \
    --smoke-python "$python_312"
  ```

- [ ] **Step 2: Checksum 和 workflow policy RED/GREEN**

  测试要求：

  - SHA256SUMS 稳定排序，只覆盖已验证 wheel/sdist，重复运行 byte-identical；
  - tag 中版本必须与 `pyproject.toml` 一致；
  - macOS blocking jobs 使用 locked sync，执行 Ruff、strict mypy、branch coverage 85%、
    JS checks、Chromium E2E、build、metadata、3.11/3.12 clean artifact smoke、demo privacy；
  - macOS 在 E2E 前显式执行 `uv run playwright install chromium`；Linux experimental
    使用 `uv run playwright install --with-deps chromium`，不能依赖 runner 预装缓存；
  - Linux workflow/job 名明确含 experimental，并使用 `continue-on-error`，README 不展示
    supported 徽章；没有 Windows job；
  - CodeQL 分析 Python 和 JavaScript；Dependabot 仅对 pip 和 GitHub Actions 提交 PR；
  - `.github/secret_scanning.yml` 默认 `paths-ignore: []`，不排除任何内容；若真实 GitHub
    扫描以后确认某个 synthetic fixture 是 false positive，只能加入精确文件路径，同时在
    public allowlist 记录该文件完整 SHA-256 和理由，禁止目录或 wildcard exclusion；
  - actions 使用最小 permissions，并固定完整 commit SHA；实施时从对应官方 action 仓库
    核对 SHA，不凭记忆填写；
  - workflow 不含 `remote add`、push、PyPI、OIDC publish、`twine upload` 或仓库可见性变更；
  - release workflow 只有 `v*` tag 触发，所有验证通过后才调用 `gh release create --draft`。

  Run RED, implement, then GREEN:

  ```bash
  uv run pytest \
    tests/unit/test_release_checksums.py \
    tests/unit/test_github_workflows.py -q
  uv run python scripts/verify_workflows.py .github/workflows \
    --secret-scanning .github/secret_scanning.yml
  release_dist="$(mktemp -d)"
  uv build --wheel --sdist --out-dir "$release_dist"
  uv run python scripts/create_checksums.py "$release_dist"
  (cd "$release_dist" && shasum -a 256 -c SHA256SUMS)
  ```

- [ ] **Step 3: 本地镜像 CI 全量执行**

  Run:

  ```bash
  release_dist="$(mktemp -d)"
  uv build --wheel --sdist --out-dir "$release_dist"
  uv lock --check
  uv sync --locked --extra test
  uv run playwright install chromium
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy src/project_memory_hub
  uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85
  node --check src/project_memory_hub/web/static/i18n.js
  node --check src/project_memory_hub/web/static/projects.js
  node --check src/project_memory_hub/web/static/sources.js
  uv run pytest tests/e2e -q
  uv run python scripts/verify_wheel.py
  uv run python scripts/verify_public_assets.py docs/assets
  uv run python scripts/verify_document_links.py
  uv run python scripts/verify_workflows.py .github/workflows \
    --secret-scanning .github/secret_scanning.yml
  python_311="$(uv python find --system 3.11)"
  python_312="$(uv python find --system 3.12)"
  uv run python scripts/verify_release_artifacts.py \
    --dist "$release_dist" \
    --smoke-python "$python_311" \
    --smoke-python "$python_312"
  ```

  Expected: 全部 PASS。未 push 前只能证明 workflow 结构和本地等价命令；GitHub hosted
  runner 的真实结果必须在未来 push 后如实记录，当前不能宣称远程 CI 已绿。

- [ ] **Step 4: 更新发布说明并提交**

  `docs/releasing.md` 明确 tag、draft Release、checksum、人工检查与失败回滚；README 只展示
  已在本地/CI 有证据的徽章和兼容声明。工作流不保存或要求 PyPI secret。发布后的人工
  checklist 要求仓库管理员启用 GitHub secret scanning 和 push protection、确认 0 个未处理
  alert；这些是有 remote 后的网页端设置，本计划不伪装成可由仓库文件自动启用。

  Run:

  ```bash
  graphify update .
  git diff --check
  ```

  Commit:

  ```bash
  git add \
    .github/workflows/ci.yml \
    .github/workflows/linux-experimental.yml \
    .github/workflows/codeql.yml \
    .github/workflows/release-draft.yml \
    .github/dependabot.yml \
    .github/secret_scanning.yml \
    scripts/verify_release_artifacts.py \
    scripts/smoke_install_artifact.py \
    scripts/create_checksums.py \
    scripts/verify_workflows.py \
    tests/unit/test_release_artifacts.py \
    tests/unit/test_release_checksums.py \
    tests/unit/test_github_workflows.py \
    README.md README.zh-CN.md docs/releasing.md
  git diff --cached --name-only
  git diff --cached --check
  git commit -m "ci(发布): 增加兼容验证与草稿发布"
  ```

---

## Task 7: 审计公开 tree 并准备无私人历史的单根快照

**Files:**

- Create: `config/public-release-allowlist.toml`
- Create: `scripts/audit_public_tree.py`
- Create: `scripts/prepare_public_snapshot.py`
- Create: `tests/unit/test_public_privacy_audit.py`
- Create: `tests/integration/test_public_snapshot.py`
- Modify: `tests/unit/security/test_redaction.py`
- Modify: `docs/releasing.md`
- Modify: `docs/superpowers/specs/2026-07-12-codex-memory-hub-design.md`
- Modify: `docs/superpowers/plans/2026-07-12-project-memory-hub.md`
- Modify: `docs/superpowers/plans/2026-07-16-explicit-issue-resolution.md`
- Modify: `docs/superpowers/plans/2026-07-17-safe-source-probe.md`
- Modify: `docs/superpowers/plans/2026-07-18-public-beta-hardening.md`

- [ ] **Step 1: Public tree auditor RED/GREEN**

  测试要求：

  - 发布 receipt 只能来自已解析的完整 commit/tree Git object，通过
    `git --no-replace-objects ls-tree -rz` 与 `git cat-file` 读取；working-tree 诊断不得
    签发 receipt，也不能跟随 symlink；
  - 拒绝真实 HOME prefix、个人用户名、token/credential、session body、database dump、
    私人项目名、未知 UUID、图片 metadata 和禁止词；
  - 测试中的假路径/假 token 只能用“精确文件路径 + 完整文件 SHA-256”放行；文件任意
    一字节变化后豁免失效，禁止忽略整个 `tests/`；
  - 额外私人项目词通过仓库外 UTF-8 denylist 提供；该文件本身不能被 Git 跟踪；
  - audit 报告只输出安全相对路径（路径本身违规时输出不透明 digest ID）、
    规则 code 和计数，不回显匹配到的私人内容；
  - PNG/WebP 复用 Task 2 metadata scanner；截图 DOM hash 必须与 manifest 匹配。

  Run RED, implement, then GREEN with synthetic fixture repositories；此时不扫描真实 tracked
  tree，因为旧文档中的已知私人路径要在 Step 2 定向清理：

  ```bash
  uv run pytest tests/unit/test_public_privacy_audit.py -q
  ```

- [ ] **Step 2: 定向清理当前 tracked tree**

  - 把旧设计/计划里的真实 home、用户名、工作区和私人项目名替换成 `${HOME}`、
    `~/Documents/example-project` 或明确的虚构路径；不删除仍有价值的设计决策。
  - 测试用安全攻击向量保持功能，但加入精确 digest allowlist；任何测试文件变化都需复审。
  - 禁止通过宽泛 regex、目录忽略、二进制全跳过或删除测试来让扫描通过。
  - 实施者在仓库外准备权限为 0600 的 UTF-8 私人词清单，并通过
    `PMH_PRIVATE_TERMS_FILE` 指向它；清单至少覆盖本机用户名、当前工作区名和已知私人项目名，
    不把内容打印到日志、argv 之外的报告或 Git tracked 文件。
  - auditor 用 `lstat` 和 resolved path 验证 denylist 是仓库外、未跟踪、非 symlink、
    owner-only 0600 regular file，且大小有界、UTF-8 有效、去空白后非空；否则 fail closed。

  Run:

  ```bash
  test -s "${PMH_PRIVATE_TERMS_FILE:?set a non-empty untracked private terms file}"
  uv run pytest tests/unit/test_public_privacy_audit.py -q
  uv run python scripts/audit_public_tree.py \
    --mode tree \
    --forbidden-file "$PMH_PRIVATE_TERMS_FILE"
  uv run python scripts/verify_public_assets.py docs/assets
  git diff --check
  ```

  Expected: PASS；报告不含私人值，当前 tracked tree 可公开。

- [ ] **Step 3: Snapshot builder RED/GREEN**

  在临时 Git repo 中构造多提交私人历史，测试 builder：

  - 要求源 commit 为完整 OID 且等于当前 `HEAD`、source index/tracked/未忽略
    untracked 全部 clean、不存在 assume-unchanged/skip-worktree，privacy receipt 匹配
    同一 tree hash；
  - builder 必须使用同一仓库外 denylist 和精确 allowlist 重新审计该 tree，
    不把可编辑 receipt 单独当作授权；
  - 使用 `git commit-tree` 从已审计 tree 创建无 parent root commit；
  - author/committer 使用固定非个人身份
    `Project Memory Hub Maintainers <noreply@project-memory-hub.invalid>`；
  - 新分支名固定 `codex/public-beta-0.2.1`，独立 worktree 不位于源 checkout 内；
  - 公开分支只有一个 commit，tree 与已审计 source 完全相同；
  - script 源码和子进程 allowlist 不含 remote、push、tag、GitHub CLI、Release 或 merge；
  - 任一失败不改变源 branch/HEAD/index，并移除只由本次创建的 ref/worktree。

  Run RED, implement, then GREEN:

  ```bash
  uv run pytest tests/integration/test_public_snapshot.py -q
  ```

- [ ] **Step 4: 提交审计工具和脱敏改动**

  Run:

  ```bash
  graphify update .
  git diff --check
  ```

  Commit:

  ```bash
  git add config/public-release-allowlist.toml \
    scripts/audit_public_tree.py scripts/prepare_public_snapshot.py \
    tests/unit/test_public_privacy_audit.py \
    tests/unit/security/test_redaction.py \
    tests/integration/test_public_snapshot.py docs/releasing.md \
    docs/superpowers/specs/2026-07-12-codex-memory-hub-design.md \
    docs/superpowers/plans/2026-07-12-project-memory-hub.md \
    docs/superpowers/plans/2026-07-16-explicit-issue-resolution.md \
    docs/superpowers/plans/2026-07-17-safe-source-probe.md \
    docs/superpowers/plans/2026-07-18-public-beta-hardening.md
  git diff --cached --name-only
  git diff --cached --check
  git commit -m "chore(发布): 准备脱敏公开快照"
  ```

- [ ] **Step 5: 最终全量验收后创建本地单根快照**

  必须先满足：当前分支 clean、Task 1–7 focused tests 全绿、全量质量门禁通过、clean
  artifact smoke 中的 doctor 健康、privacy receipt 的 tree hash 等于 `HEAD^{tree}`。

  Run:

  ```bash
  release_dist="$(mktemp -d)"
  uv build --wheel --sdist --out-dir "$release_dist"
  test -s "${PMH_PRIVATE_TERMS_FILE:?set a non-empty untracked private terms file}"
  uv lock --check
  uv sync --locked --extra test
  uv run ruff format --check .
  uv run ruff check .
  uv run mypy src/project_memory_hub
  uv run playwright install chromium
  uv run pytest --cov=project_memory_hub --cov-branch --cov-fail-under=85
  uv run pytest tests/e2e -q
  node --check src/project_memory_hub/web/static/i18n.js
  node --check src/project_memory_hub/web/static/projects.js
  node --check src/project_memory_hub/web/static/sources.js
  uv run python scripts/verify_wheel.py
  uv run python scripts/verify_document_links.py
  uv run python scripts/verify_public_assets.py docs/assets
  uv run python scripts/verify_workflows.py .github/workflows \
    --secret-scanning .github/secret_scanning.yml
  python_311="$(uv python find --system 3.11)"
  python_312="$(uv python find --system 3.12)"
  uv run python scripts/verify_release_artifacts.py \
    --dist "$release_dist" \
    --smoke-python "$python_311" \
    --smoke-python "$python_312"
  uv run python scripts/audit_public_tree.py \
    --mode tree \
    --forbidden-file "$PMH_PRIVATE_TERMS_FILE" \
    --receipt "$release_dist/public-tree-receipt.json"
  graphify update .
  graphify hook status
  test -z "$(git status --porcelain=v1)"

  # Only after every command above succeeds, create the local snapshot.
  snapshot_root="$(mktemp -d)"
  uv run python scripts/prepare_public_snapshot.py \
    --source "$(git rev-parse HEAD)" \
    --receipt "$release_dist/public-tree-receipt.json" \
    --branch codex/public-beta-0.2.1 \
    --worktree "$snapshot_root/worktree" \
    --forbidden-file "$PMH_PRIVATE_TERMS_FILE" \
    --allowlist config/public-release-allowlist.toml
  test "$(git rev-list --count codex/public-beta-0.2.1)" -eq 1
  test -z "$(git show -s --format='%P' codex/public-beta-0.2.1)"
  test "$(git show -s --format='%an <%ae>' codex/public-beta-0.2.1)" = \
    'Project Memory Hub Maintainers <noreply@project-memory-hub.invalid>'
  test "$(git rev-parse HEAD^{tree})" = \
    "$(git rev-parse codex/public-beta-0.2.1^{tree})"
  git diff --exit-code HEAD codex/public-beta-0.2.1 --
  uv run python scripts/audit_public_tree.py \
    --mode snapshot \
    --ref codex/public-beta-0.2.1 \
    --forbidden-file "$PMH_PRIVATE_TERMS_FILE"
  ```

  Expected: 全部验证 PASS，创建 snapshot 前的 `git status --porcelain=v1` 无输出，并且：

  - commit count 为 1；parent 行为空；
  - author 是固定非个人身份；
  - source 与 snapshot tree 完全相同；
  - snapshot privacy audit PASS；
  - 当前分支、`main` 和私人历史未变；没有 remote、push、tag 或 Release。

  本地全局安装的 doctor 属于单独的 operator health evidence，不绑定公开 build：若存在稳定
  安装，实施者在快照完成后只读记录 `command -v memory-hub`、`memory-hub version` 和
  `memory-hub doctor --format json`；若未安装或版本不同，如实报告，不据此改写已验证 artifact。

---

## 完整验收映射

| 规格验收项 | 计划证据 |
|---|---|
| 双语 README 可独立安装 | Task 1 文档契约 + Task 6 clean artifact smoke |
| 0.2.1 Beta、无 migration | Task 1 package/frozen contracts |
| CLI JSON/exit 不变、text 更可行动 | Task 3 byte/value contract + focused CLI tests |
| 错误页不泄露、状态/安全头不变 | Task 3 middleware/app integration tests |
| Overview 每个状态给一个安全下一步 | Task 3 presentation pure-function tests |
| Sources 区分 ingestion/probe 且不增权限 | Task 4 route/form/capability tests |
| Projects 可搜索/过滤/渐进披露且无新 API | Task 4 static contract + Chromium E2E |
| Memories 精确 model 指导不跨模型 | Task 3 namespace/HTML tests |
| 所有真实 empty branch 都有原因/下一步/成功判据 | Tasks 3、4 macro inventory + E2E |
| 截图只来自合成 runtime | Task 2 runtime guard + Task 5 DOM/metadata receipt |
| macOS 支持、Linux 实验、Windows 不支持 | Tasks 1、6 metadata/docs/workflow tests |
| CI、CodeQL、Dependabot、secret policy、draft Release | Task 6 workflow/config policy tests |
| 公开 tree 无私人路径/标识 | Task 7 tracked-tree audit |
| 私人历史不公开 | Task 7 single-root snapshot integration test |
| 无隐式 remote/push/PyPI | Tasks 6、7 forbidden-action contract |

## 明确留给未来人工执行的动作

本计划结束时仍不会自动执行：

- 创建或选择 GitHub repository；
- 添加 remote 或填写未经确认的 repository/Issues/docs URL；
- push 分支或 tag；
- 创建真实 draft/final GitHub Release；
- 修改 repository public/private、ruleset、required checks、private vulnerability reporting
  或 secret-scanning 设置；
- 上传 GitHub social preview；
- 配置 GitHub/PyPI secret、OIDC 或发布到 PyPI；
- merge 公开快照到本地 `main`；
- 改写、删除或强推既有私人 Git 历史。

这些动作需要真实 GitHub 仓库身份、最终公开前复核和单独授权；准备好代码与本地快照不等于
已经公开发布。
