# Project Memory Hub 运维指南

[README](../README.md) · [简体中文](../README.zh-CN.md) ·
[Getting started](getting-started.md) · [Architecture](architecture.md) ·
[Security](security.md) · [Release preparation](releasing.md)

本文面向本机操作者。默认运行目录：

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
DB="$RUNTIME/memory.db"
```

如果使用全局 `--config /absolute/path/config.toml`，则该配置的父目录是运行目录。下文命令需要相应替换 `RUNTIME`，不要猜路径。

运行时是 local-first：不上传 Coze，不使用 Coze CLI，不把原始对话放入数据库或日志。运维过程也不应输出 `structured_payload_json`、对话正文、令牌、补丁全文或环境变量。

## 基线健康检查

```bash
memory-hub version
memory-hub doctor --format json
memory-hub setup --format json
memory-hub discover --dry-run --format json
```

`doctor` 是只读诊断。它检查运行目录权限、SQLite `quick_check`、schema migration 版本、FTS5、Codex sessions、ChatGPT import 目录、已启用适配器、retry 年龄、最后一次 reconcile、managed AGENTS、Graphify hooks 和 Codex automation。不要为了让所有路径“变绿”就扩大到整个主目录的权限。

从稳定本地仓库执行 `uv tool install .` 时，已安装版 `doctor` 只接受由 PEP 610
`direct_url.json`、`METADATA` 和 `RECORD` 共同绑定的安装来源证明；Codex automation 的
cwd 只能与该可信根精确比对，不能自行证明源码身份。来源指向 `.worktrees`、记录哈希不符、
路径身份变化或自动任务漂移时会失败关闭。普通索引 wheel 没有本地源码证明时，Graphify
和每日任务仍保持可选警告；若原先从临时 worktree 安装，应回到稳定仓库重新执行安装，
不要手改 dist-info 或 automation TOML 来消除诊断。

需要把 `quick_check`、为空的 `foreign_key_check` 和 migration 结果作为正式运维证据时，先停止 serve 和 reconcile 写入，避免活跃 WAL 让只读检查按安全边界失败。

## 首次配置状态

`memory-hub setup --format json` 不带配置选项时只读取安全汇总，不改写已有配置、访问令牌或数据库。显式配置示例：

```bash
memory-hub setup --project-root "$HOME/Documents" --source codex --source chatgpt --complete --format json
```

CLI 和本机 `/setup` 页面共用同一套校验，只允许 Codex、ChatGPT 成为导入来源，并按项目 + 来源 + 精确模型 ID 保持行为记忆隔离。Setup 不会执行 discovery、import 或 reconcile，不会调用可选来源探针，也不会旋转 access token。保存设置后，应重启当前 `serve` 进程，再继续项目发现或导入。
新配置尚未完成时，`serve` 稳定显示 `setup_required` 并跳过启动时 reconcile；完成向导后才恢复既有启动判定，仍不会隐式执行 discovery 或 import。

Setup 只保存期望的每日时间并只读检查任务状态；它不编辑 Codex automation TOML。真实创建或修复必须由授权的 Codex 宿主执行。重复完成相同配置是零写入操作；旧配置缺少完成字段时按已完成处理，不会在升级后被强制重跑向导。

## 只读来源探针

0.2.1 Public Beta 的默认 ingest 仍且仅限 Codex 与 ChatGPT。Trae、WorkBuddy、Zcode、QoderWork 和 Claude Code 没有 ingest adapter，只提供固定白名单内的只读探针：本地控制台只在 Trae 轻量结果显示根目录安全可读时启用结构检查按钮；CLI 可直接请求结构探针，探针会自行重新验证路径和文件身份。其余四项只检查安装标记与目录访问状态。探针结果不会进入行为记忆，也不会解锁 Enable 或 Import。

只在操作者明确请求时运行：

```bash
memory-hub source probe --all --format json
memory-hub source probe trae --structure --format json
```

第一条按固定顺序返回五个轻量结果；第二条只检查 Trae 的有界结构元数据。Trae structure 不能证明 `model_id`，所以正常结果仍必须是 `model_status="unverifiable"`、`ingestion_allowed=false`。生产环境的已审核 schema 指纹集合为空，结构结果不会是 `recognized`，也不会执行记录数量查询；不要为了得到更乐观的结果手工加入本机 schema 或放宽路径规则。

CLI probe 使用独立的零写入 container，不打开 Project Memory Hub 的运行时配置、数据库或访问令牌。全局 `--config` 对 `source probe` 无效：命令不会读取该参数指向的文件，也不能用它扩大探针根目录。探针不写配置、数据库、sidecar、checkpoint、session、cookie、日志或临时缓存；输出只包含有界聚合状态和稳定 warning code。

`reconcile`、`doctor` 和任何每日 reconcile 自动任务都不会调用来源探针。`doctor` 仍只检查既有运行时健康；来源探针只会由上面的显式 CLI 或本地 Sources 页面触发。来源探针本身不会触发 migration；0.2.1 当前运行 schema 为 v12，升级边界见下文 `0011_pending_capture_history.sql` 与 `0012_capture_correlation.sql`。

## 每日自动化与错过补跑

Project Memory Hub 0.2.1 Public Beta 不创建 Codex 桌面自动化、cron 或 LaunchAgent。因此，如果用户选择不配置每日任务，`doctor` 返回 `codex_automation_missing` 是可接受警告，不是数据库或适配器故障。`daily_reconcile_time` 仅表示主机自动化的期望时间和漂移诊断。

如果电脑所有者以后单独选择创建，建议的 Codex 桌面自动化是：

- 精确名称：`Project Memory Hub Daily Reconcile`
- 频率：每天一次
- 默认时间：`03:30`
- 时区：`Asia/Shanghai`
- 执行环境：local
- 项目：Project Memory Hub 的稳定仓库路径，不是 `.worktrees` 临时路径
- 自动任务调用：MCP 工具 `reconcile_if_due_v1`，参数固定为 `{}`
- 允许的报告：健康、计数、阻塞路径和待确认队列大小；不含对话内容

可选自动化必须由授权的 Codex 宿主通过 automation 工具创建或更新。Project Memory Hub 的 Web 进程只保存期望时间并只读检查状态；它不直接编辑 Codex automation TOML。Settings 或 Setup 中改时间后，在 host 自动化同步前状态会显示 `drifted`，这是正常的诚实状态。`doctor` 的 `codex_automation_current` 只证明当前任务元数据与期望配置一致，不等同于最近一次运行成功；运行结果仍以 Codex 自动任务记录和最新 reconcile 报告为准。

如果选择配置，这也只是尽力执行，不是系统守护进程。如果 Mac 睡眠、关机或 Codex 未运行，03:30 可能错过。不会因此丢掉已成功的检查点：

1. managed AGENTS 在下一个 Git 项目的实质任务前通过最小权限 MCP broker 调用
   `reconcile_if_due_v1`。
2. 控制台启动时如果已过 24 小时或存在 catch-up 标记，会在后台补跑。
3. 操作者也可手动执行：

```bash
memory-hub reconcile --if-due --format json
# 已确认需要立即补跑时：
memory-hub reconcile --force --format json
```

单实例锁会阻止并发重入。reconcile 按顺序处理项目发现和事实、安全 retry、Codex 增量、专用 inbox 内的 ChatGPT ZIP、待核验 capture、长期压缩和健康型 proposal。重复执行依靠内容指纹、import receipt 和检查点去重。

## macOS 权限诊断

先用产品诊断，不要先 `sudo` 或对整个主目录 `chmod -R`：

```bash
memory-hub doctor --format json
memory-hub discover --dry-run --format json
ls -ldeO@ "$RUNTIME" "$HOME/.codex" "$HOME/.codex/sessions"
stat -f '%Su %Sp %l %N' "$RUNTIME" "$RUNTIME/config.toml" "$DB"
```

常见分流：

- `blocked_permission` 项目：在“系统设置 -> 隐私与安全性”中，只向实际运行 `memory-hub` 的 Codex 或终端应用授予所需文件夹权限。先尝试“文件与文件夹”精确权限，只在必需时考虑“完全磁盘访问权限”。授权后重启对应应用。
- 运行目录权限失配：目录应归当前用户且为 `0700`，普通文件应为 `0600`。先用上面的 `stat` 确认文件归当前用户、是普通文件且 link count 为 1，再只修复精确运行目录和已核对的文件：

```bash
chmod 700 "$RUNTIME" "$RUNTIME/imports" "$RUNTIME/retries" "$RUNTIME/backups" "$RUNTIME/logs"
chmod 600 "$RUNTIME/config.toml" "$DB" "$RUNTIME/access-token"
```

- symlink、hardlink、非本用户文件或 group/world-writable 路径会被拒绝；不要通过放宽权限绕过，而应该改用归当前用户所有的规范真实路径。
- Codex sessions 不可读：只检查 `~/.codex/sessions` 的读取权限，不应将整个 `~/.codex` 暴露给无关进程。
- 可选项目不可读时，其它项目仍继续运行；保留诊断或从 Settings 移除该根，不要伪造扫描成功。

## Codex 最小权限读写边界

普通 `memory-hub recall` 是严格只读操作：它只加载已经存在的私有配置，把当前 schema 的稳定数据库文件复制到单一内存快照后查询。它不会创建运行目录、生成默认配置、修改权限、初始化数据库或执行 migration。配置或数据库缺失时先显式运行 `memory-hub init`；schema 不匹配时先由有写权限的操作者运行 reconcile 或升级流程。活跃 writer 留下非空 WAL/journal 时，recall 会暂时拒绝，而不是读取不稳定快照。

Codex 的正式写入边界是 stdio MCP broker `project_memory_hub.integration.mcp_broker`。它只声明两个工具：

- `capture_pending_v1`：提交结构化声明，返回 `status` 和独立的 `duplicate` 布尔值；调用者不能提供 verification。
- `reconcile_if_due_v1`：固定执行 `force=False` 的到期检查；调用者不能选择配置、数据库路径或强制运行。

broker 只接受 `source_agent=codex`。只有仍待核验的 active pending 队列计入容量：每项目最多 512 条、全局最多 10000 条。核验或过期时，记录会在同一写事务内移入不含 `structured_payload_json` 的历史表并从 active 表删除；历史表全局最多保留 50000 条，超限时按 `(finalized_at, pending_id)` 确定性删除最旧元数据。保留期内的精确 duplicate 在 active 容量已满时仍保持幂等，新的不同声明会返回稳定的 `capacity_exceeded`，不会继续扩张数据库。

托管 `AGENTS.md` 必须直接调用这两个 MCP 工具，不得从 Codex 任务内执行 CLI `capture` 或
`reconcile`，也不得把扩大沙箱权限作为回退。若 MCP 工具尚未加载，保留最终 capture marker
供后续可信适配器恢复。只有工具返回 `status=pending_verification` 且 `duplicate=false` 时，
才能声称新记录已进入待核验队列；`duplicate=true` 只应按原样报告，因为匹配记录可能已核验或
已过期，并不证明本次新建了 pending 记录。

不要用 `--add-dir`、完全磁盘访问权限或放宽整个 PMH 运行目录来替代该边界。安装稳定工具后，可由操作者一次性注册 broker：

```bash
PMH_LAUNCHER="$(realpath "$(command -v memory-hub)")"
PMH_PYTHON="$(dirname "$PMH_LAUNCHER")/python"
test -x "$PMH_PYTHON"
codex mcp add project-memory-hub -- \
  "$PMH_PYTHON" -m project_memory_hub.integration.mcp_broker
codex mcp get project-memory-hub
```

保留原有全局审批策略，在 `~/.codex/config.toml` 生成的
`[mcp_servers.project-memory-hub]` 表中只放行并预先批准两个有界工具：

```toml
enabled_tools = ["capture_pending_v1", "reconcile_if_due_v1"]
default_tools_approval_mode = "prompt"

[mcp_servers.project-memory-hub.tools.capture_pending_v1]
approval_mode = "approve"

[mcp_servers.project-memory-hub.tools.reconcile_if_due_v1]
approval_mode = "approve"
```

注册后重启 Codex 客户端以加载新工具。继续使用“工作区写入”沙箱即可；不需要切换到“完全访问”。只有 `status=pending_verification` 且 `duplicate=false` 的成功回执表示本次新建了待核验记录；后续 Codex JSONL lifecycle、项目、精确模型、结构哈希和时间窗口全部匹配后，reconcile 才能把它提升为可信行为记忆。

## 静态 SQLite 只读核验

已停止所有 writer 的数据库和已完成的备份属于静态证据。核验它们时，先在当前 shell 定义下列门禁：

```bash
assert_static_sqlite() {
  local static_db="$1"
  local sidecar
  for sidecar in "${static_db}-wal" "${static_db}-journal"; do
    if [[ -s "$sidecar" ]]; then
      echo 'refusing immutable SQLite read: non-empty WAL/journal' >&2
      return 1
    fi
  done
}

assert_static_foreign_keys() {
  local static_db="$1"
  local violations
  violations="$(sqlite3 "file:${static_db}?mode=ro&immutable=1" \
    'PRAGMA foreign_key_check;')" || return 1
  if [[ -n "$violations" ]]; then
    printf '%s\n' "$violations" >&2
    echo 'refusing SQLite evidence: foreign key violations found' >&2
    return 1
  fi
}
```

下文的静态命令都先调用这个函数，然后使用完整引用的 `file:...?mode=ro&immutable=1` URI。每次完整性核验都要同时运行 `quick_check` 和 `assert_static_foreign_keys`：前者只能输出 `ok`，后者会显式要求 `PRAGMA foreign_key_check` 输出为空。`immutable=1` 会忽略 WAL/journal，所以只有伴随文件不存在或为空时才能使用；如果门禁失败，不要删除、截断或绕过伴随文件，应查明 writer 或未 checkpoint 事务。后文的 checkpoints 和 retry 汇总是服务运行期间的实时查询，仍使用普通 `sqlite3 -readonly` 让 SQLite 正常跟随活跃 WAL。

## SQLite 在线备份

不要在数据库可能正在 WAL 写入时直接 `cp memory.db`。使用 SQLite backup API 对应的 `.backup` 命令。下面的备份名只由时间戳组成，并在写入前将 umask 收紧：

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
DB="$RUNTIME/memory.db"
BACKUP_DIR="$RUNTIME/backups"
BACKUP_NAME="memory-$(date +%Y%m%d-%H%M%S).db"

(
  umask 077
  cd "$BACKUP_DIR" || exit 1
  sqlite3 "$DB" ".backup '$BACKUP_NAME'"
  chmod 600 "$BACKUP_NAME"
  assert_static_sqlite "$BACKUP_NAME" || exit 1
  sqlite3 "file:${BACKUP_NAME}?mode=ro&immutable=1" 'PRAGMA quick_check;'
  assert_static_foreign_keys "$BACKUP_NAME" || exit 1
)
```

`quick_check` 必须只输出 `ok`，`foreign_key_check` 必须为空。备份文件只保存在 `0700` 的本地备份目录，不上传云盘或 Coze。

## SQLite 恢复

恢复前必须停止 `memory-hub serve`、reconcile 和 Codex 每日自动化。先核对要使用的备份文件名；下面只接受由字母、数字、点、下划线和连字号组成的 basename：

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
DB="$RUNTIME/memory.db"
BACKUP_DIR="$RUNTIME/backups"
BACKUP_NAME="memory-YYYYMMDD-HHMMSS.db"  # 人工替换

case "$BACKUP_NAME" in
  ''|*[!A-Za-z0-9._-]*) echo 'unsafe backup name' >&2; return 1 2>/dev/null || exit 1 ;;
esac

assert_static_sqlite "$BACKUP_DIR/$BACKUP_NAME" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${BACKUP_DIR}/${BACKUP_NAME}?mode=ro&immutable=1" 'PRAGMA quick_check;'
assert_static_foreign_keys "$BACKUP_DIR/$BACKUP_NAME" || { return 1 2>/dev/null || exit 1; }
RESTORED="$RUNTIME/.memory.restore.db"
test ! -e "$RESTORED" || { echo 'restore target exists' >&2; return 1 2>/dev/null || exit 1; }

(
  umask 077
  cd "$BACKUP_DIR" || exit 1
  sqlite3 "$RESTORED" ".restore '$BACKUP_NAME'"
)
assert_static_sqlite "$RESTORED" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${RESTORED}?mode=ro&immutable=1" 'PRAGMA quick_check;'
assert_static_foreign_keys "$RESTORED" || { return 1 2>/dev/null || exit 1; }
chmod 600 "$RESTORED"

RECOVERY="$BACKUP_DIR/pre-restore-$(date +%Y%m%d-%H%M%S)"
mkdir -m 700 "$RECOVERY"
for file in "$DB" "$DB-wal" "$DB-shm" "$DB-journal"; do
  [[ ! -e "$file" ]] || mv -- "$file" "$RECOVERY/"
done
mv -- "$RESTORED" "$DB"
chmod 600 "$DB"
memory-hub doctor --format json
```

两次 `quick_check` 都必须是 `ok`，两次 `foreign_key_check` 都必须为空。恢复后如果 doctor 报告 schema 版本不匹配，不要继续写入，请按“Schema migration 失败”流程处理。确认恢复稳定前保留 `pre-restore-*` 目录。

## 适配器格式漂移与检查点恢复

Codex JSONL 或 ChatGPT 官方导出格式变化时，安全行为是隔离失效 lifecycle，保留上一份有效索引：

- 来源级读取失败、未完成尾行或写入事务失败不推进 checkpoint，不覆盖上一份有效索引。
- 已由换行符完整界定的单条损坏、超限或未知记录会产生不含原文的警告，清空当前 lifecycle 并推进该条位置，避免对同一坏记录无限重试。失效后的后续记录不会被采信，直到出现新的有效 `session_meta`。
- ChatGPT 先用 `import chatgpt ... --dry-run --format json` 测新导出；Codex 用 `reconcile --if-due --format json` 观察稳定健康代码。
- 保留原 JSONL/export ZIP 不变，更新到支持该格式的适配器后重跑；幂等指纹和 import receipt 防止已成功记录重复写入。
- 不要手工改 `cursor_json`、`scope` 或 `parser_version`，不要删除整张 checkpoint 表。如需回到旧状态，使用已验证 SQLite 备份。

只查看无内容的汇总元数据：

```bash
sqlite3 -readonly "$DB" \
  'SELECT adapter, parser_version, COUNT(*) AS scopes, MIN(updated_at), MAX(updated_at)
   FROM checkpoints GROUP BY adapter, parser_version ORDER BY adapter, parser_version;'
```

这条查询故意不输出 `scope` 或 `cursor_json`，因为它们可能含本机路径。如果适配器仍不可用，保留现场、停止该来源并继续服务其它来源；不要为了“清零”直接删除检查点。

## Retry 队列

retry 只保存经过脱敏和边界检查的结构化 capture，不保存异常 repr、stdout/stderr、环境变量或原对话。直接 capture 的 `pending_verification` 不等于 retry 失败；前者是等待适配器核验模型来源。

先用 doctor，再只看计数和时间，不要查询 `payload_json`：

```bash
memory-hub doctor --format json
sqlite3 -readonly "$DB" \
  'SELECT COUNT(*) AS queued, MIN(created_at) AS oldest, MAX(attempts) AS max_attempts
   FROM retry_items;'
memory-hub reconcile --force --format json
```

重试成功的项会在同一事务内进入待模型核验状态并从 retry 删除。如果队列一天以上仍存在，doctor 会警告；超过七天会失败。先修复权限、项目注册或适配器问题，不要用 SQL 批量删队列来隐藏故障。

## Proposal 分支与中断清理

```bash
memory-hub proposal list --format json
git -C /absolute/path/to/project-memory-hub worktree list --porcelain
git -C /absolute/path/to/project-memory-hub branch --list 'codex/memory-hub-proposal-*'
```

已批准 proposal 在干净的基线上创建 `codex/memory-hub-proposal-<uuid>`，在运行目录内的 `0700` 临时 worktree 应用补丁、执行白名单验证并提交。正常结束会清理临时 worktree，但保留分支给用户复审。系统永不合并、push、删除用户分支或 amend 既有提交。

中断处理：

1. proposal 状态为 `applying` 时，先从控制台点击 Recover，或在审批边界下重新执行 `memory-hub proposal apply PROPOSAL_UUID`。它只清理服务自己的私有 worktree，不删任意目录。
2. 如果状态为 `applied`，在人工复审前保留分支。`proposal rollback` 会校验精确 ref 并标记回滚，不会悄悄改主分支或删分支。
3. 用户完成合并后，可人工执行 `git branch -d <exact-branch>`。如决定丢弃未合并分支，只在核对精确分支名和 commit 后才使用 `git branch -D <exact-branch>`。
4. 不要手动 `rm -rf` 一个注册中的 worktree；先完成 Recover，或由熟悉 Git worktree 管理边界的操作者查明元数据。

## 历史升级：0.1.2 与 schema v10

0.1.2 的 migration `0009_explicit_issue_resolution.sql` 新增显式问题解决审计，`0010_codex_deferred_records.sql` 新增不含 capture 内容的 Codex 丢失项目 locator 隔离表。升级前先停止 `memory-hub serve`、reconcile 和任何可选的 Codex 每日任务，再使用 SQLite backup API 生成一份已验证的 v9 或更早现场备份：

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
DB="$RUNTIME/memory.db"
BACKUP="$RUNTIME/backups/pre-0.1.2-$(date +%Y%m%d-%H%M%S).db"

umask 077
sqlite3 "$DB" ".backup '$BACKUP'"
chmod 600 "$BACKUP"
assert_static_sqlite "$BACKUP" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${BACKUP}?mode=ro&immutable=1" 'PRAGMA quick_check;'
assert_static_foreign_keys "$BACKUP" || { return 1 2>/dev/null || exit 1; }
```

`quick_check` 必须只输出 `ok`，`foreign_key_check` 必须为空。然后确认当前可执行文件是 0.1.2，再由同一版本执行迁移：

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
DB="$RUNTIME/memory.db"

memory-hub version
memory-hub init --format json
assert_static_sqlite "$DB" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  'SELECT MIN(version), MAX(version), COUNT(*) FROM schema_migrations;'
sqlite3 "file:${DB}?mode=ro&immutable=1" 'PRAGMA quick_check;'
assert_static_foreign_keys "$DB" || { return 1 2>/dev/null || exit 1; }
memory-hub doctor --format json
```

对从新建数据库逐版升级的 0.1.2 运行时，迁移范围应为 `1|10|10`，`quick_check` 仍应为 `ok`，`foreign_key_check` 仍应为空。如果主机没有配置每日任务，doctor 可以为总体 `warn` 且包含 `codex_automation_missing`；这是本版本接受的状态，不需要为了消除警告而创建自动化。

只用脱敏计数核对解决数据，不查询记忆内容或原声明：

```bash
assert_static_sqlite "$DB" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  'SELECT status, COUNT(*) FROM memory_issue_resolutions GROUP BY status ORDER BY status;'
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  "SELECT lifecycle_state, COUNT(*) FROM behavior_memories WHERE memory_kind='open_issue' GROUP BY lifecycle_state ORDER BY lifecycle_state;"
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  'SELECT state, reason_code, COUNT(*) FROM codex_deferred_records GROUP BY state, reason_code ORDER BY state, reason_code;'
```

`memory_issue_resolutions` 只保存目标哈希、归属 ID、状态和时间，不保存 `Resolved issue:` 声明正文。`resolved` 审计数与 archived `open_issue` 数不必相等，因为手动 Archive 也会产生已归档条目；控制台会将两者分别显示为 `Resolved` 和 `Archived`。持久化 `last_reconcile_report.stage_metrics` 可包含 `resolved_count`、`already_resolved_count` 和 `unmatched_resolution_count` 等计数，但不包含声明文本；公共 `memory-hub reconcile --format json` 顶层输出只是运行摘要，没有这三个顶层字段。

`codex_deferred_records` 只保存来源定位与尝试元数据，不含 cwd、project/model ID、objective、outcome、changed paths 或 capture payload。pending 上限是每个 scope 256 条、全局 10000 条；超限会连同正常 capture、receipt 和 checkpoint 一起回滚。隔离记录本身没有 import receipt；不要手工推进 checkpoint、删除隔离行来隐藏 warning，或把旧 cwd 映射到猜测的新路径。日常 reconcile 只报告 `deferred_count`，不会把它设成 catch-up backlog。

事件处置时，操作者可以通过 `memory-hub deferred recover --stdin-json --format json` 显式给出一个已注册的精确目标项目；命令先重放并核验源文件身份、prefix hash、结构内容和 namespace，默认仅预览，只有输入中的 `apply=true` 才在单一事务中写入并把 locator 标为 recovered。

历史 pending 的恢复同样只用于取证后的事件处置。`memory-hub pending recover --stdin-json --format json` 要求逐条提供 pending ID、真实 JSONL scope、真实 source record ID 和期望结构哈希；它会再次核验项目、模型、完整 capture block、结构哈希和 24 小时时间窗。source record ID 在整个批次内必须全局唯一，不会因 scope 不同而变成两份可信证明；同一 session:turn 被映射两次时返回 `ambiguous_source` 并且整批零写入。缺 marker、aborted lifecycle、来源缺失、模型或项目不一致的条目必须继续保留 pending，不能批量强制放行。

如果 migration v9 或 v10 失败，保持所有写进程停止，保留脱敏错误码，并按下文流程检查原库与备份。需要回退 schema 时，必须恢复上面 `quick_check` 为 `ok` 且 `foreign_key_check` 为空的 SQLite 备份；仅重新安装旧版 Python 代码不会回退数据库 schema，反而可能让旧代码向新 schema 写入不兼容数据，因此不安全。

## 历史升级：0.2.1 与 schema v11/v12

`0011_pending_capture_history.sql` 把 active pending 队列与终态审计分开。迁移只让仍为 `pending` 的行继续在 `pending_captures` 中保存待核验结构正文；`verified`、`expired` 或 `rejected` 行只把项目、namespace、来源标识、结构哈希和时间等元数据迁入 `pending_capture_history`，不复制 `structured_payload_json`。旧版 `pending_confirmation:*` 状态项同时删除，后续过期审计以有界历史表为准。

`0012_capture_correlation.sql` 在 trusted `source_refs` 中单独保存已实际核验的本地 pending correlation，不再假设它等于适配器的 `session:turn` source record。迁移只从 retained verified history 中回填唯一、无冲突的关联；歧义关联保持为空，不猜测补写。以后只有新 trusted source 实际匹配 pending，或显式取证恢复提供精确 pending ID 时，才会在同一事务内绑定；不同任务即使结构哈希相同也不会被合并。

升级前停止所有 writer，并按“SQLite 在线备份”生成一份 `quick_check` 为 `ok` 且 `foreign_key_check` 为空的 v10 备份。然后用目标版本显式执行迁移并核对 schema：

```bash
RUNTIME="$HOME/Library/Application Support/Project Memory Hub"
DB="$RUNTIME/memory.db"

memory-hub version
memory-hub init --format json
assert_static_sqlite "$DB" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  'SELECT MIN(version), MAX(version), COUNT(*) FROM schema_migrations;'
sqlite3 "file:${DB}?mode=ro&immutable=1" 'PRAGMA quick_check;'
assert_static_foreign_keys "$DB" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  'SELECT verification_state, COUNT(*) FROM pending_captures GROUP BY verification_state;'
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  'SELECT final_state, COUNT(*) FROM pending_capture_history GROUP BY final_state ORDER BY final_state;'
```

迁移范围应为 `1|12|12`，`quick_check` 必须仍为 `ok`，`foreign_key_check` 必须仍为空，而 `pending_captures` 只能包含 `pending`。v10 无法证明历史 verified 行对应的可信 `source_reference_id`，因此迁移后的该字段允许为空，不得猜测补写；新核验记录会保存真实引用。旧 expired 行以可验证的 `expires_at` 作为 `finalized_at`；旧 verified/rejected 行没有可信终结时间，因此使用严格有效的 `created_at` 作为保守排序代理。历史全局硬上限为 50000 条：迁移和后续写入都只保留按 `(finalized_at, pending_id)` 排序的最新元数据，active pending 不受这项历史淘汰影响。

如果 migration v11 或 v12 失败，保持 writer 停止并按下文流程检查。回退必须恢复升级前已验证的 SQLite 备份；安装旧代码本身不能安全回退 schema。

## Schema migration 失败

每个 migration 在独立的 SQLite exclusive transaction 中运行。失败的 migration 回滚，不会伪造已应用版本。

1. 停止 serve、reconcile 和每日自动化，不要反复用不同版本写同一数据库。
2. 运行 `assert_static_sqlite "$DB"`；门禁通过后，用引用的静态 URI 执行
   `sqlite3 "file:${DB}?mode=ro&immutable=1" 'PRAGMA quick_check;'`，并执行
   `assert_static_foreign_keys "$DB"`。只有前者是 `ok` 且后者确认 `foreign_key_check` 为空时，才用上文 `.backup` 创建升级前现场备份。
3. 只查看 schema 元数据：

```bash
assert_static_sqlite "$DB" || { return 1 2>/dev/null || exit 1; }
sqlite3 "file:${DB}?mode=ro&immutable=1" \
  'SELECT version, applied_at FROM schema_migrations ORDER BY version;'
```

4. 确认 `memory-hub version` 与目标代码匹配，然后用同一版本重新执行 `memory-hub init --format json`。已完成的 migration 会跳过，未完成的会重试。
5. 如果仍失败，不要手工 `INSERT/DELETE schema_migrations`，不要用旧二进制写新 schema。恢复已验证备份，保留只含稳定错误代码的本地日志，等待修复后再升级。

如果 `quick_check` 不是 `ok` 或 `foreign_key_check` 不为空，不要先跑 migration；直接从已验证备份恢复。

## 安全卸载检查表

- 如果曾另行创建，在 Codex 自动化界面禁用 `Project Memory Hub Daily Reconcile`。
- 执行 `memory-hub integrate agents remove --dry-run`，核对后执行真实 remove。
- 停止 serve/reconcile，按需用 SQLite backup API 备份。
- 卸载 uv tool，再删除精确运行目录。
- 不对任何已发现项目运行 `rm`、Git 写操作或“清理”命令。

managed block 移除与运行数据删除是两个独立步骤：前者不动 runtime，后者不动项目仓库。
