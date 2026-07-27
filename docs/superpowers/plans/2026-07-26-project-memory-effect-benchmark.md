# Project Memory Hub 公开效果基准实施计划

> **For agentic workers:** REQUIRED SUB-SKILLS: use `executing-plans` and
> `test-driven-development`. Every production behavior change must be preceded by a focused failing
> test. Work in an isolated `codex/` branch or worktree.

**Goal:** 建立并执行一个可公开复现、隐私安全的 Project Memory Hub 配对 A/B 基准，真实测量
全新 Codex 任务的成功率、完整 token、首次通过耗时和命名空间污染，而不把召回压缩率冒充
真实任务收益。

**Architecture:** 新增仓库级 benchmark 核心模块与独立脚本，不修改 `memory-hub` 产品 CLI
或 Web 路由。运行器在临时目录生成两个合成 Git fixture 和冻结的只读 PMH 记忆快照，预先
生成 treatment brief，再以 `codex exec --ephemeral --ignore-user-config --json` 启动隔离
任务。macOS 上只使用一层严格外部 Seatbelt；Codex 的旁路参数仅用于避免不支持的第二次
Seatbelt apply，模型子进程仍被外层限制为工作区内读写且不能联网。原始事件只保存在被忽略
的私有目录；公开结果只包含白名单数字、状态和 SHA-256。

**Tech stack:** Python 3.11/3.12、stdlib `argparse`/`subprocess`/`unittest`、现有 PMH
container/recall、Codex CLI JSON events、pytest、Ruff、strict mypy、Graphify、现有公开树
隐私审计。

**Approved direction:** 用户于 2026-07-26 选择 GitHub 公开可信 A/B 实验。先完成单对端到端
试跑，再执行预注册批次；一轮 16 次只能标注 pilot，正式公开结果默认三次重复共 48 次，
且统计单位仍是八个任务。公开 artifact 必须显式选择 `single-pair-pilot`、`full-pilot` 或
`public-v1`，不得依赖文件名推断；每个 scope 的 task/replicate/condition 键集合必须完整且
精确。

## 任务依赖

| Task | 交付 | 依赖 |
|---|---|---|
| 1 | 协议、任务 manifest、结果 schema 与隐私边界 | 无 |
| 2 | 有界 Codex token/event 解析与配对统计核心 | Task 1 |
| 3 | 两个合成 fixture 与冻结 PMH recall 快照 | Tasks 1–2 |
| 4 | Codex 执行器、验证器、私有证据与断点恢复 | Tasks 2–3 |
| 5 | 单对端到端 pilot 与独立复核 | Task 4 |
| 6 | 八任务预注册批次与统计报告 | Task 5 |
| 7 | 双语文档、隐私/发布质量门与 GitHub 首页入口 | Task 6 |

## Task 1: 锁定公开协议、任务和结果契约

**Files:**

- Create: `docs/superpowers/specs/2026-07-26-project-memory-effect-benchmark-design.md`
- Create: `docs/superpowers/plans/2026-07-26-project-memory-effect-benchmark.md`
- Create: `benchmarks/project-memory-effect/manifest.json`
- Create: `benchmarks/project-memory-effect/result.schema.json`
- Create: `benchmarks/project-memory-effect/README.md`
- Create: `tests/unit/test_benchmark_contracts.py`

- [ ] 先写失败契约测试，固定八个任务、两个 fixture、AB/BA 平衡、同提交/模型/验收边界、
  pilot 与 public-v1 重复数、只读记忆、零污染和公开字段 allowlist。
- [ ] 运行 RED，确认 manifest/schema/README 尚不存在或缺少必需字段。
- [ ] 最小添加 manifest、schema 和协议，禁止原始 JSONL、会话 ID、cwd、提示词/回答正文、
  PMH 数据库或本机路径进入公开结果。
- [ ] 运行 focused GREEN、Ruff 和 `git diff --check`。

## Task 2: 实现有界 token 解析和配对统计

**Files:**

- Create: `src/project_memory_hub/benchmark.py`
- Create: `tests/unit/test_benchmark.py`

- [ ] 先写 parser RED：只读显式单个 JSONL；取最后累计值而非求和；支持 Codex exec 和持久
  event 形态；保留 null；拒绝负数、bool、float、超大整数、重复 key、计数倒退、非 UTF-8、
  超大文件/单行、符号链接和读取中替换。
- [ ] 先写 analysis RED：每 task/replicate 恰好 baseline+pmh；固定 commit/model/task/
  validator/timeout；失败不得删除；仅双方成功比较；计算中位配对变化；reasoning 不重复相加。
- [ ] 确认 RED 只因 benchmark API 不存在而失败。
- [ ] 最小实现不可变 domain、受限 parser、配对校验、确定性 JSON 编码和统计摘要。
- [ ] 运行 focused GREEN、Ruff、strict mypy 与 `git diff --check`。

## Task 3: 生成合成 fixture 和冻结 PMH recall

**Files:**

- Create: `src/project_memory_hub/benchmark_fixtures.py`
- Create: `tests/unit/test_benchmark_fixtures.py`
- Create: `tests/integration/test_benchmark_recall.py`

- [ ] 先写 fixture RED：两个 repo、每个四任务、确定性内容/提交、可恢复仓库证据、任务路径
  allowlist、验证器不得被 treatment 修改。
- [ ] 先写 recall RED：目标记忆经过 verified capture seam；pending=0；brief≤800；foreign
  project/source/model canary 均不出现；冻结前后数据库/计数不变。
- [ ] 最小实现 fixture builder 和仅在临时 runtime 中运行的 recall seeder。
- [ ] 运行 focused GREEN，并复跑现有 recall/namespace/capture 测试。

## Task 4: 实现独立 Codex 运行器与安全断点

**Files:**

- Create: `scripts/run_public_benchmark.py`
- Create: `tests/integration/test_public_benchmark.py`
- Modify: `.gitignore`

- [ ] 先写 runner RED：stdin 传 prompt；固定 `--ephemeral --ignore-user-config --json`；
  `macos-external-seatbelt-v2` 单层外部沙箱；每次运行复制并隔离唯一 Codex 启动器，精确父进程
  独占认证目录和网络，模型子进程只能写预注册文件的数据且不能创建同级文件或写
  `.git/.codex`；只有精确 Codex 父进程可 fork，子进程只能执行固定工具集且不得运行解释器、
  编译器或任意 workspace 程序；预先设置资源上限并在所有退出路径清理进程组；模型
  workspace 隔离复制到从未授权给模型的只读验证路径；变更检查使用流式独立文件快照而非
  Git 元数据；fixture 在运行前必须与记录 commit 的 blob 逐文件一致（包括拒绝 ignored/
  untracked 文件），模型 workspace 只从已验证 commit tree 导出且不包含 `.git`、本地 Git
  配置或 hooks；验证器用独立临时目录、最小 Seatbelt 和父进程逐例 oracle，不向导入模型代码
  的 worker 传递可信成功通道；固定任务 AST 门先拒绝导入期副作用，worker 解析目标后再生成
  128-bit challenge，父进程只接受无重复键的 canonical 新 challenge/observation；无 shell
  拼接；固定平衡顺序策略；私有原始证据；断点恢复与 changed-path 检查。官方 manifest
  必须以一次稳定读取匹配固定预注册 digest；state、公开
  行和配对身份绑定 manifest、harness commit/content identity 与实际模型 sandbox profile
  digest；
  真实 `--execute` 还必须用 `python3 -B` 从已审阅的干净 clone 或 detached worktree
  启动，并证明 `scripts/`、`src/` 全量文件集以及 manifest/schema、`pyproject.toml`、
  `uv.lock` 与记录 `HEAD` 的 blob 一致，拒绝额外文件、缓存、符号链接与 dirty 内容；
  公开验真按每行 `harness_commit` 重算 identity，execution envelope 绑定固定
  `/usr/bin/git` 的路径与内容 hash。进程内自检只用于发现意外漂移，不宣称证明 Python
  导入前已被恶意替换的 launcher；checkpoint 还绑定 validation tree、validator
  transcript 和 raw events，并能将
  partial receipt 安全隔离后重跑。
- [ ] 测试认证/模型错误、quota 错误、截断 JSONL、超时、Codex 非零退出、validator 失败、
  treatment 测试篡改、PMH 快照变化和重复 run-id。
- [ ] 最小实现 `prepare`、`run`、`summarize` 与 `verify-public` 子命令；默认不执行模型调用，
  真实运行必须显式 `--execute`。
- [ ] 运行 focused GREEN、Ruff、strict mypy、脚本帮助与 `git diff --check`。

## Task 5: 运行单对端到端 pilot

**Files:**

- Private ignored: `benchmark-runs/**`
- Generated after success: `benchmarks/project-memory-effect/results/pilot-v1.jsonl`
- Generated after success: `benchmarks/project-memory-effect/results/pilot-v1-summary.json`

- [ ] 从干净已提交 benchmark harness 运行一个 task 的 baseline 与 pmh 条件。
- [ ] 核对 exact model、Codex version、prompt/task/workspace hash、validator、token 字段、耗时、
  changed paths、PMH brief、零 foreign canary 与写保护。
- [ ] 对 raw event 做只读人工抽查，但公开输出不得包含正文、路径、session/thread ID。
- [ ] 让独立子代理复核 pilot 证据和结果 schema；修复后重跑。
- [ ] pilot 只证明 harness，不能写入正式产品效果 headline。

## Task 6: 运行八任务预注册批次并生成统计

**Files:**

- Generated: `benchmarks/project-memory-effect/results/public-v1.jsonl`
- Generated: `benchmarks/project-memory-effect/results/public-v1-summary.json`
- Create: `docs/benchmark.md`

- [ ] 冻结 harness commit、fixture hash、model、reasoning、Codex version、随机 seed、超时和
  排除规则；运行前保存 preregistration receipt。
- [ ] 按 AB/BA 交错顺序执行八任务；正式 public-v1 每条件三次，若只完成一次必须标注
  `pilot`，不得伪装正式结果。
- [ ] 保留失败、超时和 invalid；先报告成功率，只在双方成功配对比较 token/耗时。
- [ ] 生成每任务表、中位配对变化、区间、可比 pair 数、九次隔离 canary 汇总和写保护证明。
- [ ] 独立复核统计与原始匿名行一一对应。

## Task 7: 完成公开文档、隐私与发布门禁

**Files:**

- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/benchmark.md`
- Modify: `docs/releasing.md`
- Modify: `tests/unit/test_public_documentation.py`
- Modify: `tests/unit/test_public_repository_contracts.py`
- Modify: `CHANGELOG.md` only after a completed public result exists

- [ ] 先写文档/仓库契约 RED，要求首页链接、准确 claim boundary、复现命令、完整公开结果文件，
  且禁止 raw JSONL/绝对路径/session ID/提示词正文。
- [ ] 更新双语首页与 benchmark 报告；不得宣称普遍节省、账单节省或未运行的双模型实验。
- [ ] 运行 focused tests、全量 pytest+branch coverage、Ruff、strict mypy、文档链接、公开树
  审计、wheel/sdist 验证和 `git diff --check`。
- [ ] 因仓库已有 Graphify 图且修改文档，回到持有图文件的主工作区运行 `graphify update .`；
  确认 `graphify hook status`。
- [ ] 审计 diff，仅暂存本基准文件，按中文规范提交；按既有私有源仓库→净化公开快照流程发布，
  不把私有历史、原始运行证据或本机标识推到 GitHub。
