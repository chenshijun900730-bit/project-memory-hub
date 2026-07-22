"use strict";

(() => {
  const STORAGE_KEY = "pmh-language";
  const DEFAULT_LANGUAGE = "en";
  const SUPPORTED_LANGUAGES = new Set(["en", "zh-CN"]);

  const catalogs = {
    en: {
      "brand.name": "Project Memory Hub",
      "a11y.skip_to_content": "Skip to content",
      "a11y.primary_navigation": "Primary navigation",
      "masthead.eyebrow": "Local operations ledger",
      "masthead.loopback_only": "Loopback only",
      "language.selector": "Language",
      "language.chinese": "中文",
      "language.english": "English",
      "nav.overview": "Overview",
      "nav.setup": "Setup",
      "nav.sources": "Sources",
      "nav.projects": "Projects",
      "nav.memories": "Memories",
      "nav.imports": "Imports",
      "nav.proposals": "Proposals",
      "nav.settings": "Settings",
      "page.overview": "Overview",
      "page.setup": "Setup",
      "page.sources": "Sources",
      "page.projects": "Projects",
      "page.memories": "Memories",
      "page.imports": "Imports",
      "page.proposals": "Proposals",
      "page.settings": "Settings",
      "index.overview": "PMH / OVERVIEW",
      "index.setup": "PMH / SETUP",
      "index.sources": "PMH / SOURCES",
      "index.projects": "PMH / PROJECTS",
      "index.memories": "PMH / MEMORIES",
      "index.imports": "PMH / IMPORTS",
      "index.proposals": "PMH / PROPOSALS",
      "index.settings": "PMH / SETTINGS",
      "document.overview": "Overview · Project Memory Hub",
      "document.setup": "Setup · Project Memory Hub",
      "document.sources": "Sources · Project Memory Hub",
      "document.projects": "Projects · Project Memory Hub",
      "document.memories": "Memories · Project Memory Hub",
      "document.imports": "Imports · Project Memory Hub",
      "document.proposals": "Proposals · Project Memory Hub",
      "document.settings": "Settings · Project Memory Hub",
      "footer.privacy": "Private by default. Raw conversations and access tokens are never shown here.",
      "sources.lede": "Only registered adapters can be enabled. Optional tools remain sealed until their implementation and isolation tests exist.",
      "sources.intro": "Only registered adapters can be enabled. Optional tools remain sealed until their implementation and isolation tests exist.",
      "sources.restart_notice": "Saved desired source state. Restart required before the running process changes; desired and runtime states are shown separately.",
      "sources.ingestion_sources": "Ingestion sources",
      "sources.read_only_probes": "Read-only probes",
      "sources.source": "Source",
      "sources.implementation": "Implementation",
      "sources.desired_state": "Desired state",
      "sources.running_process": "Running process",
      "sources.detected": "Detected",
      "sources.probe_health": "Probe health",
      "sources.model_identity": "Model identity",
      "sources.capability": "Capability",
      "sources.structure": "Structure",
      "sources.behavior_import": "Behavior import",
      "sources.warnings": "Warnings",
      "sources.action": "Action",
      "sources.columns.source": "Source",
      "sources.columns.implementation": "Implementation",
      "sources.columns.desired_state": "Desired state",
      "sources.columns.running_process": "Running process",
      "sources.columns.detected": "Detected",
      "sources.columns.probe_health": "Probe health",
      "sources.columns.model_identity": "Model identity",
      "sources.columns.capability": "Capability",
      "sources.columns.structure": "Structure",
      "sources.columns.behavior_import": "Behavior import",
      "sources.columns.warnings": "Warnings",
      "sources.columns.action": "Action",
      "sources.available": "Available",
      "sources.unavailable": "Unavailable",
      "sources.locked": "Locked",
      "sources.none": "none",
      "sources.not_applicable": "Not applicable",
      "sources.registered_adapter": "Registered adapter",
      "sources.enable": "Enable",
      "sources.disable": "Disable",
      "sources.further_check": "Further check",
      "sources.no_control_available": "No control available",
      "sources.probe_warnings": "Probe warnings",
      "common.observed": "Observed",
      "common.source": "Source",
      "common.confidence": "Confidence",
      "common.namespace": "Namespace",
      "common.created": "Created",
      "common.updated": "Updated",
      "common.status": "Status",
      "common.bytes": "bytes",
      "empty.next_safe_step": "Next safe step",
      "empty.success_condition": "Success condition:",
      "overview.lede": "A truthful view of what the local store knows—and what it does not.",
      "overview.reconcile_status": "Reconcile startup status:",
      "overview.stored_totals": "Stored totals",
      "overview.project_count": { one: "{count} project", other: "{count} projects" },
      "overview.shared_facts": "shared facts",
      "overview.behavior_count": {
        one: "{count} behavior memory",
        other: "{count} behavior memories",
      },
      "overview.permission_error_count": {
        one: "{count} permission error",
        other: "{count} permission errors",
      },
      "overview.recorded_operations": "Recorded operations",
      "overview.last_reconcile_success": "Last reconcile success",
      "overview.last_discovery": "Last discovery",
      "overview.last_compaction": "Last compaction",
      "overview.recall_size": "Recall size",
      "overview.pending_confirmations": "Pending confirmations",
      "overview.next_safe_step_eyebrow": "Guided first run",
      "overview.next_safe_step": "Next safe step",
      "overview.next_step.discover.reason": "No project is registered yet, so preview discovery before changing the store.",
      "overview.next_step.discover.success": "The preview lists only the project candidates you expect to review.",
      "overview.next_step.scan.reason": "A project is registered, but no shared fact has been recorded yet. Run this preview from that registered project directory.",
      "overview.next_step.scan.success": "The dry run reports reviewable facts without changing the store.",
      "overview.next_step.doctor.reason": "A permission problem or degraded startup needs diagnosis before more ingestion.",
      "overview.next_step.doctor.success": "Doctor reports no unexplained failure, or names one bounded repair.",
      "overview.next_step.reconcile.reason": "The local store is ready for its next due maintenance pass.",
      "overview.next_step.reconcile.success": "Reconcile reports success or skipped without a permission error.",
      "projects.persisted_findings": "Persisted discovery findings",
      "projects.no_discovery_issues": "No active discovery issues are recorded. This is healthy; no action required.",
      "projects.no_discovery_issues_success": "Doctor reports no unexplained discovery or permission failure.",
      "projects.duplicate_groups": "Duplicate candidate groups",
      "projects.duplicates_not_recorded": "Duplicate candidates: not recorded. This is healthy; no action required.",
      "projects.duplicates_not_recorded_success": "The preview shows no unexpected duplicate candidate group.",
      "projects.registered_projects": "Registered projects",
      "projects.search_label": "Search by project name or ID",
      "projects.status_filter": "Status filter",
      "projects.status_all": "All statuses",
      "projects.status_enabled": "Enabled",
      "projects.status_disabled": "Disabled",
      "projects.status_permission": "Permission issue",
      "projects.status_inactive": "Inactive",
      "projects.showing": "Showing",
      "projects.of": "of",
      "projects.result_label": "projects",
      "projects.show_affected_path": "Show affected path",
      "projects.show_candidate_paths": "Show candidate paths",
      "projects.show_full_path": "Show full path",
      "projects.no_filter_results": "No projects match the current filters.",
      "projects.show_more": "Show more",
      "projects.permission": "Permission",
      "projects.discovery": "Discovery",
      "projects.inactivity": "Inactivity",
      "projects.last_change": "Last change",
      "projects.verified_new_path": "Verified new path",
      "projects.none_recorded": "No projects are recorded yet.",
      "projects.none_recorded_success": "The preview lists only the project candidates you expect to review.",
      "projects.remediation.missing_root": "Choose an existing project root in discovery settings.",
      "projects.remediation.blocked_permission": "On macOS, grant Files and Folders or Full Disk Access in System Settings, then retry discovery.",
      "projects.remediation.scan_error": "Check that the path is readable and retry discovery.",
      "memories.project": "Project",
      "memories.choose_project": "Choose a project",
      "memories.behavior_source": "Source for behavior memory",
      "memories.exact_model": "Exact model ID for behavior memory",
      "memories.exact_model_guidance_eyebrow": "Exact namespace",
      "memories.exact_model_guidance_title": "Resolve the current Codex model ID",
      "memories.exact_model_guidance": "Run this command in the current project. Copy only the exact source_agent and model_id returned for this task; this page never guesses or lists other model namespaces.",
      "memories.load": "Load project memory",
      "memories.shared_facts": "Shared project facts",
      "memories.no_shared_facts": "No active shared facts are recorded for this project. Run the next step from that registered project directory.",
      "memories.no_shared_facts_success": "The dry run reports reviewable facts without changing the store.",
      "memories.choose_exact_source_model": "Choose an exact source and model before behavior rows are queried.",
      "memories.choose_exact_source_model_success": "The result identifies one exact source_agent and model_id for this task.",
      "memories.choose_registered_project_source_model": "Choose a project, source, and model before behavior rows are queried. A project is already registered; resolve this task's exact namespace below.",
      "memories.choose_registered_project_source_model_success": "The result identifies one exact source_agent and model_id for this task.",
      "memories.choose_project_source_model": "Choose a project, source, and model before behavior rows are queried.",
      "memories.choose_project_source_model_success": "The preview lists only the project candidates you expect to review.",
      "memories.type_delete": "Type DELETE",
      "memories.proposed_rule": "Proposed shared rule",
      "memories.request_approval": "Request approval",
      "memories.unsafe_actions": "Unsafe namespace metadata; memory actions unavailable.",
      "memories.none_recorded": "No non-deleted memories are recorded in this exact namespace.",
      "memories.none_recorded_success": "Reconcile reports success or skipped; this namespace remains isolated if it is still empty.",
      "imports.official_only": "Official export only",
      "imports.description": "The selected file is streamed through a private temporary file, validated, and removed after every outcome.",
      "imports.official_zip": "Official export ZIP",
      "imports.dry_run_first": "Dry run first",
      "imports.inspect": "Inspect locally",
      "imports.dry_run_matches": "Dry-run matches: {count}.",
      "imports.imported_matches": "Imported matches: {count}.",
      "imports.confirmation_required": "Confirmation required: {count}.",
      "imports.privacy_notice": "Client filenames, archive member names, and conversation text are never rendered on this page.",
      "proposals.promotion_lede": "Review model-private rules before explicitly promoting them to shared project facts.",
      "proposals.awaiting_approval": "Awaiting approval",
      "proposals.memory_promotions": "Memory promotions",
      "proposals.requested": "Requested",
      "proposals.approve_shared_rule": "Approve shared rule",
      "proposals.unsafe_promotion": "Unsafe promotion metadata; approval unavailable.",
      "proposals.no_promotions": "No memory promotions are awaiting approval.",
      "proposals.no_promotions_success": "The command returns the exact namespace needed before requesting a promotion from Memories.",
      "proposals.improvement_lede": "Review bounded proposal metadata, then explicitly approve, reject, apply, recover, or roll back an eligible local change.",
      "proposals.local_improvements": "Local improvements",
      "proposals.improvement_proposals": "Improvement proposals",
      "proposals.risk": "Risk",
      "proposals.origin": "Origin",
      "proposals.patch": "Patch",
      "proposals.no_patch": "no executable patch",
      "proposals.verification": "Verification",
      "proposals.no_verification": "No configured verification",
      "proposals.verification_summary": "Verification summary",
      "proposals.apply": "Apply in isolated branch",
      "proposals.recover": "Recover interrupted apply",
      "proposals.review_unavailable": "Review metadata was redacted or truncated; proposal actions are unavailable.",
      "proposals.no_action": "No proposal action is available in the current state.",
      "proposals.none_recorded": "No improvement proposal metadata is recorded.",
      "proposals.none_recorded_success": "The command reports status ok and the same empty proposal list.",
      "settings.saved_restart": "Saved safely. Restart required before these settings affect this running process.",
      "settings.desired_automation": "Desired automation",
      "settings.automation_note": "Automation changes require the authorized Codex host interface. The web process never edits Codex automation TOML.",
      "settings.project_roots": "Project roots",
      "settings.one_directory": "One absolute existing directory per line",
      "settings.roots_note": "Add a line to include a root; remove its line to stop scanning it after restart. Maximum 32 roots.",
      "settings.registered_sources": "Registered sources",
      "settings.recall_budget": "Recall token budget",
      "settings.inactive_days": "Inactive days",
      "settings.daily_time": "Desired daily time",
      "settings.save": "Save private config",
      "setup.saved": "Saved safely. Restart Project Memory Hub before the new settings affect this running process.",
      "setup.lede": "Configure the local memory boundary without editing TOML.",
      "setup.isolation": "Codex and ChatGPT are registered sources. Behavior memory remains isolated by project, source, and exact model ID.",
      "setup.status_heading": "Current readiness",
      "setup.roots_ready": "Project roots ready",
      "setup.projects_found": "Projects found",
      "setup.first_memory": "Shared facts",
      "setup.automation": "Daily automation",
      "setup.automation_note": "This page saves the desired time and checks status only. Create or update the task through the authorized Codex host.",
      "setup.optional_sources_note": "Other detected coding tools remain read-only probes and cannot be enabled here.",
      "setup.save": "Save and continue",
      "setup.complete": "Finish local setup",
      "setup.callout_heading": "Finish first-run setup",
      "setup.callout_body": "Review project roots, registered sources, model isolation, and the desired daily time.",
      "setup.open": "Open setup",
      "setup.reopen": "Review guided setup",
      "setup.completed_notice": "Local setup is complete. Restart before changed settings affect this process, then preview discovery.",
      "setup.next_step_heading": "Next safe step",
      "setup.next_step.configure": "Review and save the local settings below, then finish setup.",
      "setup.next_step.discover": "Preview project discovery with memory-hub discover --dry-run --format json.",
      "setup.next_step.first_memory": "Preview the first shared-facts scan from a registered project.",
      "setup.next_step.authorize_automation": "Ask Codex to create or repair the exact daily reconcile task.",
      "setup.next_step.ready": "Setup is ready; continue normal local operation.",
      "setup.automation.current": "Current",
      "setup.automation.authorization_required": "Codex authorization required",
      "setup.automation.drifted": "Needs repair in Codex",
      "setup.automation.unavailable": "Stable installation unavailable",
    },
    "zh-CN": {
      "brand.name": "Project Memory Hub",
      "a11y.skip_to_content": "跳到主要内容",
      "a11y.primary_navigation": "主导航",
      "masthead.eyebrow": "本地运行台账",
      "masthead.loopback_only": "仅限本机回环访问",
      "language.selector": "语言选择",
      "language.chinese": "中文",
      "language.english": "English",
      "nav.overview": "总览",
      "nav.setup": "配置向导",
      "nav.sources": "来源",
      "nav.projects": "项目",
      "nav.memories": "记忆",
      "nav.imports": "导入",
      "nav.proposals": "提案",
      "nav.settings": "设置",
      "page.overview": "总览",
      "page.setup": "配置向导",
      "page.sources": "来源",
      "page.projects": "项目",
      "page.memories": "记忆",
      "page.imports": "导入",
      "page.proposals": "提案",
      "page.settings": "设置",
      "index.overview": "PMH / 总览",
      "index.setup": "PMH / 配置向导",
      "index.sources": "PMH / 来源",
      "index.projects": "PMH / 项目",
      "index.memories": "PMH / 记忆",
      "index.imports": "PMH / 导入",
      "index.proposals": "PMH / 提案",
      "index.settings": "PMH / 设置",
      "document.overview": "总览 · Project Memory Hub",
      "document.setup": "配置向导 · Project Memory Hub",
      "document.sources": "来源 · Project Memory Hub",
      "document.projects": "项目 · Project Memory Hub",
      "document.memories": "记忆 · Project Memory Hub",
      "document.imports": "导入 · Project Memory Hub",
      "document.proposals": "提案 · Project Memory Hub",
      "document.settings": "设置 · Project Memory Hub",
      "footer.privacy": "默认保持私密。此处绝不会显示原始对话和访问令牌。",
      "sources.lede": "只有已注册的适配器才能启用。可选工具在完成实现与隔离测试前会保持封存。",
      "sources.intro": "只有已注册的适配器才能启用。可选工具在完成实现与隔离测试前会保持封存。",
      "sources.restart_notice": "已保存期望的来源状态。运行中的进程需重启后才会变更；期望状态和实际运行状态会分开显示。",
      "sources.ingestion_sources": "导入来源",
      "sources.read_only_probes": "只读探针",
      "sources.source": "来源",
      "sources.implementation": "实现状态",
      "sources.desired_state": "期望状态",
      "sources.running_process": "运行进程",
      "sources.detected": "检测结果",
      "sources.probe_health": "探针状态",
      "sources.model_identity": "模型身份",
      "sources.capability": "探测能力",
      "sources.structure": "结构",
      "sources.behavior_import": "行为导入",
      "sources.warnings": "警告",
      "sources.action": "操作",
      "sources.columns.source": "来源",
      "sources.columns.implementation": "实现状态",
      "sources.columns.desired_state": "期望状态",
      "sources.columns.running_process": "运行进程",
      "sources.columns.detected": "检测结果",
      "sources.columns.probe_health": "探针状态",
      "sources.columns.model_identity": "模型身份",
      "sources.columns.capability": "探测能力",
      "sources.columns.structure": "结构",
      "sources.columns.behavior_import": "行为导入",
      "sources.columns.warnings": "警告",
      "sources.columns.action": "操作",
      "sources.available": "可用",
      "sources.unavailable": "不可用",
      "sources.locked": "已锁定",
      "sources.none": "无",
      "sources.not_applicable": "不适用",
      "sources.registered_adapter": "已注册适配器",
      "sources.enable": "启用",
      "sources.disable": "停用",
      "sources.further_check": "进一步检测",
      "sources.no_control_available": "无可用操作",
      "sources.probe_warnings": "探针警告",
      "common.observed": "记录于",
      "common.source": "来源",
      "common.confidence": "置信度",
      "common.namespace": "命名空间",
      "common.created": "创建时间",
      "common.updated": "更新时间",
      "common.status": "状态",
      "common.bytes": "字节",
      "empty.next_safe_step": "下一个安全步骤",
      "empty.success_condition": "成功判据：",
      "overview.lede": "如实呈现本地存储知道什么，以及它还不知道什么。",
      "overview.reconcile_status": "启动协调状态：",
      "overview.stored_totals": "已存总数",
      "overview.project_count": "{count} 个项目",
      "overview.shared_facts": "共享事实",
      "overview.behavior_count": "{count} 条行为记忆",
      "overview.permission_error_count": "{count} 个权限错误",
      "overview.recorded_operations": "已记录的操作",
      "overview.last_reconcile_success": "最近协调成功",
      "overview.last_discovery": "最近发现",
      "overview.last_compaction": "最近压缩",
      "overview.recall_size": "回忆大小",
      "overview.pending_confirmations": "待确认项",
      "overview.next_safe_step_eyebrow": "首次运行引导",
      "overview.next_safe_step": "下一个安全步骤",
      "overview.next_step.discover.reason": "尚未注册项目，请先预览发现结果，不要立即改动存储。",
      "overview.next_step.discover.success": "预览只列出你预期审查的项目候选。",
      "overview.next_step.scan.reason": "项目已注册，但尚未记录共享事实。请在该已注册项目目录中运行此预览命令。",
      "overview.next_step.scan.success": "试运行报告可审查的事实，且不改动存储。",
      "overview.next_step.doctor.reason": "存在权限问题或启动已降级，继续导入前需要先诊断。",
      "overview.next_step.doctor.success": "Doctor 未报告无法解释的失败，或只指出一个有界的修复步骤。",
      "overview.next_step.reconcile.reason": "本地存储已可执行下一次到期维护。",
      "overview.next_step.reconcile.success": "Reconcile 返回 success 或 skipped，且没有权限错误。",
      "projects.persisted_findings": "已持久化的发现结果",
      "projects.no_discovery_issues": "当前没有已记录的发现问题。这是健康状态，无需操作。",
      "projects.no_discovery_issues_success": "Doctor 未报告无法解释的发现或权限故障。",
      "projects.duplicate_groups": "重复候选组",
      "projects.duplicates_not_recorded": "重复候选：未记录。这是健康状态，无需操作。",
      "projects.duplicates_not_recorded_success": "预览未显示意外的重复候选组。",
      "projects.registered_projects": "已注册项目",
      "projects.search_label": "按项目名称或 ID 搜索",
      "projects.status_filter": "状态筛选",
      "projects.status_all": "全部状态",
      "projects.status_enabled": "已启用",
      "projects.status_disabled": "已停用",
      "projects.status_permission": "权限问题",
      "projects.status_inactive": "非活跃",
      "projects.showing": "当前显示",
      "projects.of": "/",
      "projects.result_label": "个项目",
      "projects.show_affected_path": "显示受影响路径",
      "projects.show_candidate_paths": "显示候选路径",
      "projects.show_full_path": "显示完整路径",
      "projects.no_filter_results": "没有项目匹配当前筛选条件。",
      "projects.show_more": "显示更多",
      "projects.permission": "权限",
      "projects.discovery": "发现状态",
      "projects.inactivity": "活跃状态",
      "projects.last_change": "最近变更",
      "projects.verified_new_path": "已验证的新路径",
      "projects.none_recorded": "尚未记录任何项目。",
      "projects.none_recorded_success": "预览只列出你预期审查的项目候选。",
      "projects.remediation.missing_root": "请在发现设置中选择现有的项目根目录。",
      "projects.remediation.blocked_permission": "请在 macOS 系统设置中授予“文件与文件夹”或“完全磁盘访问权限”，然后重试发现。",
      "projects.remediation.scan_error": "请确认路径可读取，然后重试发现。",
      "memories.project": "项目",
      "memories.choose_project": "选择项目",
      "memories.behavior_source": "行为记忆来源",
      "memories.exact_model": "行为记忆的精确模型 ID",
      "memories.exact_model_guidance_eyebrow": "精确命名空间",
      "memories.exact_model_guidance_title": "解析当前 Codex 模型 ID",
      "memories.exact_model_guidance": "请在当前项目中运行该命令。只复制此任务返回的精确 source_agent 和 model_id；本页不会猜测或列出其他模型命名空间。",
      "memories.load": "加载项目记忆",
      "memories.shared_facts": "共享项目事实",
      "memories.no_shared_facts": "此项目当前没有已记录的有效共享事实。请在该已注册项目目录中运行下一步。",
      "memories.no_shared_facts_success": "试运行报告可审查的事实，且不改动存储。",
      "memories.choose_exact_source_model": "查询行为记录前，请选择精确的来源和模型。",
      "memories.choose_exact_source_model_success": "结果为此任务识别出一组精确的 source_agent 和 model_id。",
      "memories.choose_registered_project_source_model": "查询行为记录前，请选择项目、来源和模型。已有注册项目；请使用下方命令解析此任务的精确命名空间。",
      "memories.choose_registered_project_source_model_success": "结果为此任务识别出一组精确的 source_agent 和 model_id。",
      "memories.choose_project_source_model": "查询行为记录前，请选择项目、来源和模型。",
      "memories.choose_project_source_model_success": "预览只列出你预期审查的项目候选。",
      "memories.type_delete": "输入 DELETE",
      "memories.proposed_rule": "拟议共享规则",
      "memories.request_approval": "请求批准",
      "memories.unsafe_actions": "命名空间元数据不安全；记忆操作不可用。",
      "memories.none_recorded": "此精确命名空间中没有已记录的未删除记忆。",
      "memories.none_recorded_success": "Reconcile 返回 success 或 skipped；如果仍为空，该命名空间依然保持隔离。",
      "imports.official_only": "仅限官方导出",
      "imports.description": "所选文件会通过私有临时文件流式处理、完成校验，并在每种处理结果后删除。",
      "imports.official_zip": "官方导出 ZIP",
      "imports.dry_run_first": "先试运行",
      "imports.inspect": "在本机检查",
      "imports.dry_run_matches": "试运行匹配：{count} 条。",
      "imports.imported_matches": "已导入匹配：{count} 条。",
      "imports.confirmation_required": "需要确认：{count} 项。",
      "imports.privacy_notice": "此页面绝不会显示客户端文件名、归档成员名称或对话文本。",
      "proposals.promotion_lede": "先审查模型私有规则，再明确决定是否将其提升为共享项目事实。",
      "proposals.awaiting_approval": "等待批准",
      "proposals.memory_promotions": "记忆提升",
      "proposals.requested": "请求时间",
      "proposals.approve_shared_rule": "批准共享规则",
      "proposals.unsafe_promotion": "提升元数据不安全；批准操作不可用。",
      "proposals.no_promotions": "当前没有等待批准的记忆提升。",
      "proposals.no_promotions_success": "命令返回从“记忆”页请求提升前所需的精确命名空间。",
      "proposals.improvement_lede": "审查受限的提案元数据，然后明确批准、拒绝、应用、恢复或回滚符合条件的本地变更。",
      "proposals.local_improvements": "本地改进",
      "proposals.improvement_proposals": "改进提案",
      "proposals.risk": "风险",
      "proposals.origin": "来源",
      "proposals.patch": "补丁",
      "proposals.no_patch": "无可执行补丁",
      "proposals.verification": "验证",
      "proposals.no_verification": "未配置验证",
      "proposals.verification_summary": "验证摘要",
      "proposals.apply": "在隔离分支中应用",
      "proposals.recover": "恢复中断的应用",
      "proposals.review_unavailable": "审查元数据已脱敏或截断；提案操作不可用。",
      "proposals.no_action": "当前状态下没有可用的提案操作。",
      "proposals.none_recorded": "尚未记录任何改进提案元数据。",
      "proposals.none_recorded_success": "命令返回 status ok，且提案列表仍如实为空。",
      "settings.saved_restart": "已安全保存。需要重启后，这些设置才会影响当前运行进程。",
      "settings.desired_automation": "期望的自动任务",
      "settings.automation_note": "自动任务变更需要经过授权的 Codex 宿主界面。网页进程绝不会编辑 Codex 自动任务 TOML。",
      "settings.project_roots": "项目根目录",
      "settings.one_directory": "每行填写一个现有的绝对目录",
      "settings.roots_note": "新增一行即可纳入根目录；移除该行后，重启时会停止扫描。最多 32 个根目录。",
      "settings.registered_sources": "已注册来源",
      "settings.recall_budget": "回忆 Token 预算",
      "settings.inactive_days": "非活跃天数",
      "settings.daily_time": "期望每日执行时间",
      "settings.save": "保存私有配置",
      "setup.saved": "已安全保存。重启 Project Memory Hub 后，新设置才会影响当前运行进程。",
      "setup.lede": "无需编辑 TOML，即可配置本机记忆边界。",
      "setup.isolation": "Codex 和 ChatGPT 是已注册来源；行为记忆继续按项目、来源和精确模型 ID 严格隔离。",
      "setup.status_heading": "当前就绪状态",
      "setup.roots_ready": "可用项目根目录",
      "setup.projects_found": "已发现项目",
      "setup.first_memory": "共享事实",
      "setup.automation": "每日自动任务",
      "setup.automation_note": "此页面只保存期望时间并检查状态。创建或更新任务仍需通过已授权的 Codex 宿主。",
      "setup.optional_sources_note": "检测到的其他编程工具仍是只读探针，不能在这里启用。",
      "setup.save": "保存并继续",
      "setup.complete": "完成本机配置",
      "setup.callout_heading": "完成首次配置",
      "setup.callout_body": "检查项目根目录、已注册来源、模型隔离和期望的每日执行时间。",
      "setup.open": "打开配置向导",
      "setup.reopen": "重新查看配置向导",
      "setup.completed_notice": "本机配置已完成。若刚修改设置，请先重启当前进程，再预览项目发现。",
      "setup.next_step_heading": "下一个安全步骤",
      "setup.next_step.configure": "检查并保存下方本机设置，然后完成配置。",
      "setup.next_step.discover": "运行 memory-hub discover --dry-run --format json 预览项目发现。",
      "setup.next_step.first_memory": "从已注册项目中预览首次共享事实扫描。",
      "setup.next_step.authorize_automation": "请 Codex 创建或修复精确的每日 reconcile 任务。",
      "setup.next_step.ready": "配置已就绪，可以继续正常的本机操作。",
      "setup.automation.current": "当前已匹配",
      "setup.automation.authorization_required": "需要 Codex 授权",
      "setup.automation.drifted": "需要在 Codex 中修复",
      "setup.automation.unavailable": "稳定安装身份不可用",
    },
  };

  const dynamicChinese = {
    Available: "可用",
    Unavailable: "不可用",
    Enabled: "已启用",
    Disabled: "已停用",
    "Desired: Enabled": "期望：已启用",
    "Desired: Disabled": "期望：已停用",
    "Desired: Unavailable": "期望：不可用",
    "Runtime: Enabled": "运行中：已启用",
    "Runtime: Disabled": "运行中：已停用",
    "Runtime: Unavailable": "运行中：不可用",
    Detected: "已检测到",
    "Not detected": "未检测到",
    Readable: "可读取",
    "Permission blocked": "权限受阻",
    Missing: "缺失",
    Rejected: "已拒绝",
    "Not checked": "未检查",
    Unverifiable: "无法验证",
    "Presence and access check": "存在性与访问检查",
    "Structure metadata check": "结构元数据检查",
    "Not run": "未运行",
    Recognized: "已识别",
    Partial: "部分识别",
    Unsupported: "不支持",
    "Probe busy": "探针正忙",
    Locked: "已锁定",
    "Not applicable": "不适用",
    "Registered adapter": "已注册适配器",
    "No control available": "无可用操作",
    Enable: "启用",
    Disable: "停用",
    "Further check": "进一步检测",
    "Probe warnings": "探针警告",
    none: "无",
    "not recorded": "未记录",
    not_checked: "未检查",
    not_due: "当前无需执行",
    setup_required: "需要先完成配置",
    running: "运行中",
    degraded: "已降级",
    complete: "已完成",
    active: "活跃",
    inactive: "非活跃",
    ok: "正常",
    blocked_permission: "权限受阻",
    Active: "活跃",
    Cold: "冷存储",
    Archived: "已归档",
    Deleted: "已删除",
    Resolved: "已解决",
    decision: "决策",
    failed_attempt: "失败尝试",
    verified_method: "已验证方法",
    preference: "偏好",
    risk: "风险",
    open_issue: "待解决问题",
    reusable_lesson: "可复用经验",
    outcome: "结果",
    retrospective: "复盘",
    low: "低",
    medium: "中",
    high: "高",
    draft: "草稿",
    approved: "已批准",
    applying: "应用中",
    applied: "已应用",
    rejected: "已拒绝",
    failed: "失败",
    rolled_back: "已回滚",
    local_cli: "本地 CLI",
    codex_task: "Codex 任务",
    control_panel: "控制台",
    analyzer: "分析器",
    legacy: "旧版",
    current: "当前",
    missing: "缺失",
    duplicate: "重复",
    disabled: "已停用",
    drifted: "已偏移",
    "Archive": "归档",
    "Delete": "删除",
    "Relink": "重新关联",
    "Approve": "批准",
    "Reject": "拒绝",
    "Rollback": "回滚",
  };

  let activeLanguage = DEFAULT_LANGUAGE;

  const hasOwn = (object, key) => Object.prototype.hasOwnProperty.call(object, key);

  function selectCountVariant(value, count) {
    if (typeof value === "string") {
      return value;
    }
    if (value === null || typeof value !== "object") {
      return null;
    }
    const numericCount = Number(count);
    if (Number.isFinite(numericCount) && numericCount === 1 && typeof value.one === "string") {
      return value.one;
    }
    return typeof value.other === "string" ? value.other : value.one;
  }

  function translate(key, language, count = null) {
    const catalog = catalogs[language];
    let value = hasOwn(catalog, key) ? catalog[key] : null;

    if (value === null && hasOwn(dynamicChinese, key)) {
      value = language === "zh-CN" ? dynamicChinese[key] : key;
    }

    if (value === null) {
      const configuredCheck = /^Configured check ([1-9][0-9]*)$/.exec(key);
      if (configuredCheck !== null) {
        value =
          language === "zh-CN"
            ? `已配置检查 ${configuredCheck[1]}`
            : key;
      }
    }

    const selected = selectCountVariant(value, count);
    if (typeof selected !== "string") {
      return null;
    }
    if (count === null) {
      return selected;
    }
    return selected.replaceAll("{count}", String(count));
  }

  function setTranslatedText(element, attribute, language) {
    const key = element.getAttribute(attribute);
    if (!key) {
      return;
    }
    const translated = translate(key, language);
    if (translated !== null) {
      element.textContent = translated;
    }
  }

  function setTranslatedAttribute(element, marker, target, language) {
    const key = element.getAttribute(marker);
    if (!key) {
      return;
    }
    const translated = translate(key, language);
    if (translated !== null) {
      element.setAttribute(target, translated);
    }
  }

  function applyTranslations(language) {
    document.documentElement.setAttribute("lang", language);

    document.querySelectorAll("[data-i18n]").forEach((element) => {
      setTranslatedText(element, "data-i18n", language);
    });

    document.querySelectorAll("[data-i18n-count]").forEach((element) => {
      const key = element.getAttribute("data-i18n-count");
      const count = element.getAttribute("data-count");
      if (!key || count === null) {
        return;
      }
      const translated = translate(key, language, count);
      if (translated !== null) {
        element.textContent = translated;
      }
    });

    document.querySelectorAll("[data-i18n-title]").forEach((element) => {
      setTranslatedAttribute(element, "data-i18n-title", "title", language);
    });

    document.querySelectorAll("[data-i18n-aria-label]").forEach((element) => {
      setTranslatedAttribute(element, "data-i18n-aria-label", "aria-label", language);
    });

    document.querySelectorAll("[data-language-option]").forEach((button) => {
      button.setAttribute(
        "aria-pressed",
        String(button.getAttribute("data-language-option") === language),
      );
    });
  }

  function readStoredLanguage() {
    try {
      const stored = window.localStorage.getItem(STORAGE_KEY);
      return SUPPORTED_LANGUAGES.has(stored) ? stored : DEFAULT_LANGUAGE;
    } catch (_error) {
      return DEFAULT_LANGUAGE;
    }
  }

  function storeLanguage(language) {
    try {
      window.localStorage.setItem(STORAGE_KEY, language);
    } catch (_error) {
      // Storage can be unavailable in hardened or private browser contexts.
    }
  }

  function changeLanguage(language, persist) {
    if (!SUPPORTED_LANGUAGES.has(language)) {
      return;
    }
    activeLanguage = language;
    applyTranslations(activeLanguage);
    if (persist) {
      storeLanguage(activeLanguage);
    }
  }

  document.querySelectorAll("[data-language-option]").forEach((button) => {
    button.addEventListener("click", () => {
      changeLanguage(button.getAttribute("data-language-option"), true);
    });
  });

  changeLanguage(readStoredLanguage(), false);
})();
