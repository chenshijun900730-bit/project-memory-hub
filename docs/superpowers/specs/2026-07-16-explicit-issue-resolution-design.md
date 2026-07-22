# Project Memory Hub 0.1.2 显式问题解决设计

## 1. 背景

Project Memory Hub 目前会把同一项目、同一来源软件、同一精确模型命名空间内的所有
`active` 行为记忆作为召回候选。`Outcome` 会进入“当前状态”，`Open issue` 会被视为
必须优先保留的未完成事项。

这种行为正确保留了历史分歧，但缺少“这个旧问题已经明确解决”的结构化表达。结果是：
用户后来取消每日自动任务后，旧的“每日任务仍待确认”仍可能继续出现在召回简报中，
占用 token，并让后续模型误以为它仍是待办事项。

本设计补充显式解决语义，不改变“行为记忆之间的分歧不强行合并”这一既有边界。

## 2. 目标

1. 允许已验证的任务捕获明确声明某条旧 `Open issue` 已解决。
2. 只在同一 `project_id + source_agent + exact model_id` 内生效。
3. 只做确定性的精确匹配，不用模型、关键词或时间猜测冲突关系。
4. 已解决记录退出正常召回，但保留原始记忆和解决审计记录。
5. 未验证的直接 capture 不得改变任何既有记忆生命周期。
6. 重复执行、中断恢复和适配器重放保持幂等。

## 3. 非目标

- 不实现“最新记忆自动覆盖旧记忆”。
- 不根据语义相似度、关键词重合或日期推断问题已解决。
- 不删除旧行为记忆。
- 不跨项目、跨软件或跨模型解决问题。
- 不在本切片接入 Trae、WorkBuddy、Zcode、QoderWork 或 Claude Code。
- 不创建或恢复每日 03:30 自动任务。

## 4. 用户可见语法

受管 Codex capture 标记、ChatGPT 显式标签提取器和结构化 capture JSON 新增可重复字段：

```text
Resolved issue: <此前 Open issue 的完整文本>
```

对应结构化字段为：

```json
{
  "resolved_open_issues": [
    "此前 Open issue 的完整文本"
  ]
}
```

规则：

- 文本先经过与 `Open issue` 相同的 UTF-8 上限、脱敏和空白规范化。
- 规范化后的文本必须与旧 `Open issue` 完全一致。
- 字段可重复；规范化后按首次出现顺序去重，计数也按去重后的声明计算。
- 同一 payload 的 `open_issues` 和 `resolved_open_issues` 规范化后不得有交集；存在交集时
  整个 capture 作为矛盾输入拒绝，不能先写入再解决。
- 空值、超限值和脱敏失败按现有 capture 安全规则拒绝。
- 旧 capture JSON 不含该字段时等价于空列表，保持向后兼容。
- ChatGPT 只有明确出现 `Resolved issue:` 标签时才产生解决声明；普通自然语言不得被推断为
  已解决。

## 5. 数据模型

新增 migration `0009_explicit_issue_resolution.sql` 和审计表
`memory_issue_resolutions`。每次解决尝试只保存必要元数据：

- `resolution_id`
- `project_id`
- `source_agent`
- `model_id`
- `target_content_hash`
- `target_memory_id`，未匹配时为 `NULL`
- `source_reference_id`
- `status`：`resolved` 或 `not_found`
- `resolved_at`

约束：

- `target_memory_id` 指向 `behavior_memories`，不复制问题正文。
- `status = resolved` 必须有非空 `target_memory_id`，`status = not_found` 必须为 `NULL`；
  migration 通过 `CHECK` 固定该关系。
- 使用两个 partial unique index 保证 SQLite 幂等，而不是依赖普通 UNIQUE 对 `NULL` 的处理：
  - `resolved` 索引覆盖完整隔离键、`source_reference_id`、`target_content_hash` 和
    `target_memory_id`；
  - `not_found` 索引覆盖完整隔离键、`source_reference_id` 和 `target_content_hash`。
- 完整隔离键是 `project_id + source_agent + exact model_id`，两个索引都必须包含它。
- migration 增加归属校验触发器：非空 `target_memory_id` 必须指向同项目、同来源、同精确模型
  且 `memory_kind = open_issue` 的记录；仅靠应用层检查不够。
- `model_id` 保存精确原值，禁止缩短、归一化或改写。
- 已匹配的目标行为记忆从 `active` 变为 `archived`；审计表用于区分“问题已解决”
  与用户手工归档。
- 不增加 `resolved` 生命周期值，避免重建既有 `behavior_memories` 表及其约束。

## 6. 数据流与事务边界

### 6.1 未验证 capture

直接调用 `memory-hub capture` 时，`resolved_open_issues` 只进入
`pending_captures.structured_payload_json`。此阶段：

- 不查询目标问题；
- 不写解决审计；
- 不改变任何行为记忆生命周期。

### 6.2 适配器验证后的 capture 与事务所有权

新增 connection-scoped verified capture API。现有公开 `CaptureService.capture()` 在单独调用时
仍可自己创建事务；进入适配器 ingestion 时不得嵌套创建事务：

- Codex `IngestionService` 对一个有界 `AdapterBatch` 持有外层写事务，在同一 connection 上
  调用 verified capture，并用 connection-scoped checkpoint API 写入 checkpoint，最后一次提交。
- ChatGPT adapter 对每个 conversation 持有一个写事务，在同一 connection 上调用 verified
  capture、写入 receipt，再一次提交。
- connection-scoped capture 不允许自行 commit、rollback 或打开第二个写连接。
- Codex batch 可能包含多个项目。事务必须记录本批次触碰过的每个 `ProjectRecord` 和事务开始
  时的注册表 generation；写 checkpoint 前逐一复验所有已触碰项目的物理身份，并比较全局
  generation 快照。任意一个项目漂移都会回滚整个 batch，不能只复验最后一条记录的项目。
- ChatGPT 每个 conversation 只允许命中一个项目，但仍执行同样的 generation 快照和该项目
  事务末复验。

Codex 或 ChatGPT 适配器提供可信 `NamespaceVerification` 后，同一外层写事务依次：

1. 重新验证项目物理身份和注册表 generation。
2. 验证来源软件、精确模型和 `source_record_id` 与适配器证明一致。
3. 检查本次来源引用是否已完整处理；完全重放直接返回 duplicate，所有解决计数为零。
4. 写入本次任务的新行为记忆。
5. 对每条规范化后的 `resolved_open_issues` 查询同项目、同精确命名空间、
   `memory_kind = open_issue` 且 `lifecycle_state = active` 的候选。先用内容哈希缩小范围，
   再比较完整 `normalized_content`，不能只凭哈希归档。
6. 目标必须属于不同的 `source_reference_id`，且目标来源时间不得晚于本次
   `verification.verified_at`；历史导入的旧解决声明不能归档后来产生的问题。
7. 将所有符合条件的完全匹配旧问题标记为 `archived`，并逐目标写入 `resolved` 审计。
8. 没有活动匹配时，查询相同命名空间和内容哈希的成功解决审计，并联表读取其目标
   `behavior_memories.normalized_content` 做完整文本复核。只有全文仍完全一致时才不新增审计行，
   并把本条声明计入 `already_resolved_count`；否则新增一条 `not_found` 审计，但不修改记忆。
9. 写入 Codex checkpoint 或 ChatGPT receipt。
10. 最后再次验证项目身份和注册表 generation，再提交事务。

任何身份漂移、命名空间不一致、数据库异常或中断都会回滚本次新记忆、生命周期变化和
解决审计、receipt 和 checkpoint。生命周期更新后、receipt/checkpoint 前发生中断也必须回滚。

### 6.3 多条完全相同问题

同一精确命名空间可能因为不同任务指纹保留多条文本完全相同的 `Open issue`。一次明确
解决会归档所有活动的完全匹配项，因为它们表达的是同一个显式文本问题；每个目标都有
独立 `resolved` 审计行。`resolved_count` 按本次新归档的目标记忆数计数；
`already_resolved_count` 和 `unmatched_resolution_count` 按去重后的解决声明数计数。

## 7. 召回行为

RecallService 不增加语义冲突判断，也不改变排序算法：

- `active` 问题继续按现有优先级召回；
- 显式解决后变为 `archived` 的问题自然退出普通 search；
- 未匹配、未验证或跨命名空间的解决声明不会影响召回；
- 其它相互矛盾的 Outcome、Decision、Preference 仍按现有规则共同保留。

这样只移除具有明确、可审计解决证据的旧问题，不把“最近”误当作“正确”。

## 8. API 与界面反馈

### 8.1 领域对象

- `CapturePayload.resolved_open_issues: list[str] = []`
- `NormalizedTaskRecord.resolved_open_issues: tuple[str, ...] = ()`
- `CaptureResult` 增加默认值为零的 `resolved_count`、`already_resolved_count` 和
  `unmatched_resolution_count`
- `CaptureResult.status` 增加 `resolved` 和 `partial`：没有新增行为记忆、但确实归档了
  旧问题时返回 `resolved`；任何新处理的解决声明未匹配时都返回 `partial`，即使本次没有
  其它新增行为记忆。`duplicate` 只表示没有新增行为记忆、没有新的生命周期变化且没有
  新的未匹配项；它可以同时带有非零 `already_resolved_count`，表示请求的目标此前已解决。

### 8.2 CLI 和 reconcile

- JSON 输出只返回计数和稳定状态码，不返回问题正文。
- `not_found` 不阻断其它合法记忆写入，但使对应 reconcile 阶段产生一个不含原文的
  `resolution_not_found` 警告。
- `inserted` 继续表示至少写入一条新行为记忆且没有未匹配解决项；同一次 capture 同时
  解决旧问题时通过计数字段表达，不另改状态。
- 状态优先级固定为：未验证时 `pending_verification`；否则有未匹配项时 `partial`；否则有
  新行为记忆时 `inserted`；否则有新归档目标时 `resolved`；其余为 `duplicate`。这样即使
  outcome 等行为内容已重复、capture 实际只处理解决声明，结果也没有歧义。
- 重复导入相同来源记录不得重复增加计数或审计行。
- Codex `IngestionResult`、ChatGPT `ImportReport` 和 reconcile stage metrics 必须显式聚合
  CaptureResult 的三个解决计数；只有新写入的 `not_found` 才增加一次警告，重放不重复告警。

### 8.3 本地控制台

- 已有 Memories 页面仍显示归档记录。
- 存在成功解决审计时，生命周期显示为 `Resolved`，否则保持 `Archived`。
- 页面只在用户已经选择精确项目、来源和模型后显示该状态。
- 本切片不增加模糊搜索、批量自动解决或跨命名空间控制。

## 9. 安全与隐私不变量

1. 解决目标始终先按项目和精确命名空间过滤，再比较内容哈希。
2. 哈希命中后仍必须比较完整规范化文本；不能仅凭哈希或 `memory_id` 修改记录。
3. 未验证 pending capture 永远没有解决副作用。
4. 解决文本经过现有 capture 脱敏，审计表不保存正文。
5. 项目路径在事务前后都必须通过物理身份检查。
6. 外层 adapter 事务统一提交 receipt、checkpoint、解决审计和生命周期更新。
7. 普通 recall 和控制台查询不能借解决关系读取其它模型的内容。

## 10. 测试策略

实现严格遵循红—绿—重构，至少覆盖：

1. 未验证 capture 携带解决声明时，旧问题保持 `active`。
2. 适配器验证后，目标旧 `Open issue` 变为 `archived` 且不再召回；同命名空间其它
   `active` 记忆不受影响。
3. 同文本但不同项目、来源软件或模型的问题不受影响。
4. 空白规范化后一致的文本可以匹配；相似但不完全一致的文本不得匹配。
5. 同命名空间内多条完全相同问题一次全部解决。
6. 同一 payload 的重复解决声明只处理和计数一次；与新 `Open issue` 矛盾时整体拒绝。
7. 本次 capture 新写入的同文问题不会被自身解决；旧来源记录也不能解决来源时间更晚的问题。
8. 没有匹配时产生脱敏警告，不影响其它新记忆。
9. 重放相同 Codex/ChatGPT 来源记录不会重复审计、计数或警告。
10. 项目路径替换、relink 或 generation 并发变化时整个事务回滚。
11. 生命周期更新后、Codex checkpoint 或 ChatGPT receipt 写入前的故障会回滚全部变化。
12. checkpoint/receipt 写入冲突时不得留下已归档问题。
13. 多项目 Codex batch 在最后一个项目写完后替换较早项目目录时，事务末全项目复验会
    回滚整个 batch 和 checkpoint。
14. `already_resolved` 必须通过目标行为记忆全文复核，不能只依赖历史审计中的哈希。
15. v8 到 v9 迁移、未来版本和断档迁移继续失败关闭；两个 partial unique index 对
    `NULL` 目标保持幂等。
16. 控制台只在精确命名空间下区分 `Resolved` 与 `Archived`。
17. 全量测试、覆盖率、Ruff、mypy、Graphify 和 wheel 内容验证通过。

## 11. 发布与真实运行库升级

- 目标版本为 `0.1.2`。
- 安装前确认没有旧版 serve/reconcile 进程。
- 对真实 SQLite 库执行 `quick_check` 并通过 SQLite backup API 创建 0600 备份。
- 安装 0.1.2 后运行 migration v9、强制一次 reconcile，再运行 doctor。
- 不创建每日自动任务；`codex_automation_missing` 继续作为用户选择产生的非阻断提醒。
- 真实库升级失败时恢复备份，不通过简单降级程序版本回滚新 schema。

## 12. Trae 后续边界

本机 Trae 的结构化 `session_memory` 文件不包含可逐记录验证的精确 `model_id`。因此
0.1.2 不启用 Trae 导入。后续可以单独设计只读来源探针，安全报告
`model_id_unverifiable`，但在找到权威且可竞态保护的模型映射前不得写入行为记忆。
