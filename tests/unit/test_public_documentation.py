from __future__ import annotations

import importlib.util
import inspect
import re
from pathlib import Path
from types import ModuleType

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PUBLIC_DOCUMENTS = (
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    "docs/getting-started.md",
    "docs/architecture.md",
    "docs/security.md",
    "docs/releasing.md",
    "docs/operations.md",
)
PUBLIC_ASSETS = {
    "docs/assets/screenshots/overview.png",
    "docs/assets/screenshots/sources.png",
    "docs/assets/screenshots/memories.png",
    "docs/assets/diagrams/local-data-flow.svg",
    "docs/assets/diagrams/strict-model-isolation.svg",
    "docs/assets/diagrams/approval-gated-improvement.svg",
    "docs/assets/social-preview.png",
}
PUBLIC_ASSET_MANIFEST = "docs/assets/demo-manifest.json"


def _read(relative_path: str) -> str:
    path = PROJECT_ROOT / relative_path
    assert path.is_file(), f"missing public document: {relative_path}"
    return path.read_text(encoding="utf-8")


def _assert_in_order(document: str, markers: tuple[str, ...]) -> None:
    positions = [document.index(marker) for marker in markers]
    assert positions == sorted(positions)


def _commands(document: str) -> tuple[str, ...]:
    blocks = re.findall(r"```(?:bash|console)\n(.*?)```", document, flags=re.DOTALL)
    return tuple(
        line.strip()
        for block in blocks
        for line in block.splitlines()
        if line.strip().startswith(("memory-hub ", "uv tool "))
    )


def _load_link_verifier() -> ModuleType:
    path = PROJECT_ROOT / "scripts/verify_document_links.py"
    assert path.is_file(), "document link verifier must exist"
    spec = importlib.util.spec_from_file_location("verify_document_links_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_public_document_inventory_exists() -> None:
    assert all((PROJECT_ROOT / path).is_file() for path in PUBLIC_DOCUMENTS)


def test_english_readme_leads_with_value_and_a_five_minute_path() -> None:
    readme = _read("README.md")

    _assert_in_order(
        readme,
        (
            "[简体中文](README.zh-CN.md)",
            "Public Beta",
            "# Project Memory Hub",
            "Local-first",
            "Strict isolation",
            "Verified memory",
            "Approval-gated change",
            "## Five-minute quick start",
        ),
    )
    assert "Local-first, model-isolated project memory" in readme
    assert "uv tool install ." in readme
    assert "memory-hub doctor --format json" in readme
    assert "--editable" not in readme
    assert "--force" not in readme


def test_first_run_setup_is_documented_without_unsafe_automation_claims() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    getting_started = _read("docs/getting-started.md")
    operations = _read("docs/operations.md")

    for document in (english, chinese, getting_started):
        _assert_in_order(
            document,
            (
                "memory-hub init --format json",
                "memory-hub setup",
                "memory-hub discover --dry-run --format json",
            ),
        )
        assert "Codex" in document and "ChatGPT" in document
    assert "project + source + exact model ID" in english
    assert "项目 + 来源 + 精确模型 ID" in chinese
    assert "does not run discovery, import, or reconcile" in getting_started
    assert "不会执行 discovery、import 或 reconcile" in operations
    for document in (english, chinese, getting_started, operations):
        normalized = " ".join(document.split())
        assert "authorized Codex host" in normalized or "授权的 Codex 宿主" in normalized
        assert "automation TOML" in document


def test_daily_automation_documents_the_bounded_mcp_call() -> None:
    operations = _read("docs/operations.md")

    assert "自动任务调用：MCP 工具 `reconcile_if_due_v1`，参数固定为 `{}`" in operations


def test_installed_doctor_identity_boundary_is_documented() -> None:
    security = _read("docs/security.md")
    operations = _read("docs/operations.md")

    assert "PEP 610" in security
    assert "automation cwd is never accepted as source provenance" in security
    assert "安装来源证明" in operations
    assert "`.worktrees`" in operations


def test_chinese_readme_is_a_complete_command_equivalent_mirror() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")

    _assert_in_order(
        chinese,
        (
            "[English](README.md)",
            "Public Beta",
            "# Project Memory Hub",
            "本机优先",
            "严格隔离",
            "可验证记忆",
            "审批后变更",
            "## 五分钟快速开始",
        ),
    )
    assert len(chinese) >= len(english) * 0.55
    assert _commands(chinese) == _commands(english)
    assert PUBLIC_ASSETS <= {target for target in re.findall(r"\((docs/assets/[^)]+)\)", chinese)}
    assert PUBLIC_ASSETS <= {target for target in re.findall(r"\((docs/assets/[^)]+)\)", english)}


@pytest.mark.parametrize("relative_path", ("README.md", "README.zh-CN.md"))
def test_readme_uses_final_public_assets_by_role(relative_path: str) -> None:
    readme = _read(relative_path)
    embedded = set(re.findall(r"!\[[^\]]*\]\((docs/assets/[^)]+)\)", readme))
    linked = set(re.findall(r"(?<!!)\[[^\]]*\]\((docs/assets/[^)]+)\)", readme))

    assert embedded == {
        "docs/assets/screenshots/overview.png",
        "docs/assets/diagrams/local-data-flow.svg",
        "docs/assets/diagrams/strict-model-isolation.svg",
        "docs/assets/diagrams/approval-gated-improvement.svg",
    }
    assert {
        "docs/assets/screenshots/sources.png",
        "docs/assets/screenshots/memories.png",
    } <= linked
    assert "docs/assets/social-preview.png" not in embedded


def test_final_public_asset_bundle_exists() -> None:
    assert all((PROJECT_ROOT / path).is_file() for path in PUBLIC_ASSETS)
    assert (PROJECT_ROOT / PUBLIC_ASSET_MANIFEST).is_file()


@pytest.mark.parametrize("relative_path", ("README.md", "README.zh-CN.md"))
def test_readme_support_and_source_matrices_are_honest(relative_path: str) -> None:
    readme = _read(relative_path)

    for marker in (
        "Codex",
        "ChatGPT",
        "Trae",
        "WorkBuddy",
        "Zcode",
        "QoderWork",
        "Claude Code",
        "macOS",
        "Linux",
        "Windows",
        "Python 3.11",
        "Python 3.12",
        "Chromium",
    ):
        assert marker in readme
    for status in ("Supported", "Experimental", "Unsupported", "Verified", "Read-only probe"):
        assert status in readme
    assert "ChatGPT real-time" not in readme
    assert "ChatGPT 实时" not in readme
    assert "automatic merge" not in readme.casefold()
    assert "自动合并" not in readme
    assert "automatically edits" not in readme.casefold()
    assert "自动改码" not in readme
    assert "PyPI" not in readme


def test_focused_documents_keep_public_details_out_of_the_readme() -> None:
    getting_started = _read("docs/getting-started.md")
    architecture = _read("docs/architecture.md")
    security = _read("docs/security.md")
    releasing = _read("docs/releasing.md")
    operations = _read("docs/operations.md")

    assert "## Success criteria" in getting_started
    assert "project_id + source_agent + model_id" in architecture
    assert "800" in architecture
    assert "Capture marker contract" in architecture
    assert "Approval-gated" in architecture
    assert "## Threat model" in security
    assert "CSRF" in security and "loopback" in security
    assert "## Remote actions require a maintainer" in releasing
    assert "No PyPI upload" in releasing
    assert "[Getting started](getting-started.md)" in operations
    assert "docs/superpowers" not in _read("README.md")
    assert "docs/superpowers" not in _read("README.zh-CN.md")


@pytest.mark.parametrize(
    "relative_path",
    (
        "README.md",
        "README.zh-CN.md",
        "docs/getting-started.md",
        "docs/operations.md",
    ),
)
def test_codex_connection_docs_require_the_narrow_mcp_broker(
    relative_path: str,
) -> None:
    document = _read(relative_path)

    assert 'realpath "$(command -v memory-hub)"' in document
    assert "codex mcp add project-memory-hub" in document
    assert "project_memory_hub.integration.mcp_broker" in document
    assert 'enabled_tools = ["capture_pending_v1", "reconcile_if_due_v1"]' in document
    assert "mcp_servers.project-memory-hub.tools.capture_pending_v1" in document
    assert "mcp_servers.project-memory-hub.tools.reconcile_if_due_v1" in document
    assert document.count('approval_mode = "approve"') >= 2
    assert "workspace-write" in document or "工作区写入" in document


def test_v11_pending_history_contract_is_public_and_consistent() -> None:
    operations = _read("docs/operations.md")
    architecture = _read("docs/architecture.md")
    releasing = _read("docs/releasing.md")
    changelog = _read("CHANGELOG.md")

    assert "当前 schema 仍为 v10" not in operations
    assert "0011_pending_capture_history.sql" in operations
    assert "0012_capture_correlation.sql" in operations
    assert "created_at` 作为保守排序代理" in operations
    assert "source record ID 在整个批次内必须全局唯一" in operations
    assert "ambiguous_source` 并且整批零写入" in operations
    assert "pending_capture_history" in architecture
    assert "50,000-row" in architecture
    assert "`0001` through `0012`" in releasing
    assert "Schema v11" in changelog
    assert "Schema v12" in changelog
    assert "payload-free terminal" in changelog
    assert "structured_payload_json" in changelog


def test_operations_static_sqlite_evidence_uses_quiescent_immutable_reads() -> None:
    operations = _read("docs/operations.md")
    releasing = _read("docs/releasing.md")

    assert "assert_static_sqlite()" in operations
    assert "assert_static_foreign_keys()" in operations
    assert 'for sidecar in "${static_db}-wal" "${static_db}-journal"' in operations
    assert "immutable=1` 会忽略 WAL/journal" in operations
    assert "'PRAGMA foreign_key_check;'" in operations
    assert 'if [[ -n "$violations" ]]' in operations
    for static_target in (
        '"$BACKUP_NAME"',
        '"$BACKUP_DIR/$BACKUP_NAME"',
        '"$RESTORED"',
        '"$BACKUP"',
    ):
        assert f"assert_static_foreign_keys {static_target}" in operations
    assert operations.count('assert_static_foreign_keys "$DB"') >= 3
    assert operations.count("foreign_key_check` 必须") >= 3
    for forbidden in (
        'sqlite3 -readonly "$BACKUP_NAME"',
        'sqlite3 -readonly "$BACKUP_DIR/$BACKUP_NAME"',
        'sqlite3 -readonly "$RESTORED"',
        'sqlite3 -readonly "$BACKUP"',
    ):
        assert forbidden not in operations
    assert operations.count("mode=ro&immutable=1") >= 12
    assert operations.count('assert_static_sqlite "$DB"') >= 3

    # Live operational summaries must keep ordinary read-only SQLite semantics so
    # SQLite can consistently follow an active WAL instead of ignoring it.
    assert operations.count('sqlite3 -readonly "$DB"') >= 2
    assert "FROM checkpoints GROUP BY adapter" in operations
    assert "FROM retry_items;" in operations

    assert "WAL/journal gate passes" in releasing
    assert "`foreign_key_check` is" in releasing


def test_release_docs_describe_reproducible_draft_only_publication() -> None:
    releasing = _read("docs/releasing.md")

    for marker in (
        "scripts/verify_release_artifacts.py",
        "scripts/create_checksums.py",
        "scripts/verify_workflows.py",
        "SHA256SUMS",
        "gh release create",
        "--draft",
        "v*",
        "secret scanning",
        "push protection",
        "zero unresolved alerts",
    ):
        assert marker in releasing
    fenced_commands = re.findall(r"```(?:bash|console)\n(.*?)```", releasing, flags=re.DOTALL)
    assert all("twine upload" not in command for command in fenced_commands)
    assert "PyPI secret" not in releasing


def test_readmes_limit_compatibility_claims_to_local_artifact_evidence() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")

    assert "clean built-wheel smoke" in english
    assert "Hosted CI is not claimed" in english
    assert "全新环境安装已构建 wheel" in chinese
    assert "不声称 GitHub 托管 CI 已通过" in chinese
    for readme in (english, chinese):
        assert "badge.svg" not in readme


def test_candidate_status_batch_discovery_and_unknown_model_limits_are_explicit() -> None:
    english = _read("README.md")
    chinese = _read("README.zh-CN.md")
    getting_started = _read("docs/getting-started.md")
    architecture = _read("docs/architecture.md")
    security = _read("docs/security.md")

    assert "0.2.1 · Public Beta candidate · Unreleased" in english
    assert "0.2.1 · Public Beta 候选版 · 尚未发布" in chinese
    assert "registers every candidate returned by that discovery run" in english
    assert "会注册该次发现返回的全部候选" in chinese
    assert "registers every candidate returned by that run" in getting_started
    for document in (english, chinese, architecture, security):
        assert "source_agent=chatgpt + model_id=unknown" in document
    assert "share that fallback namespace" in english
    assert "共享这个回退命名空间" in chinese


def test_document_link_verifier_accepts_safe_links_and_ignores_code(tmp_path: Path) -> None:
    verifier = _load_link_verifier()
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/target.md").write_text("# Target\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[Target](docs/target.md)\n\n```bash\n[ignored](/private/path)\n```\n",
        encoding="utf-8",
    )

    verifier.verify_document_links(
        tmp_path,
        documents=(Path("README.md"),),
        allowed_missing=frozenset(),
    )


@pytest.mark.parametrize(
    "target",
    (
        "/private/absolute.md",
        "docs/%2e%2e/%2e%2e/private.md",
        "file:///private/path",
        "javascript:alert(1)",
    ),
)
def test_document_link_verifier_rejects_unsafe_targets(tmp_path: Path, target: str) -> None:
    verifier = _load_link_verifier()
    (tmp_path / "README.md").write_text(f"[unsafe]({target})\n", encoding="utf-8")

    with pytest.raises(verifier.DocumentLinkError):
        verifier.verify_document_links(
            tmp_path,
            documents=(Path("README.md"),),
            allowed_missing=frozenset(),
        )


def test_document_link_verifier_rejects_symlink_and_case_aliases(tmp_path: Path) -> None:
    verifier = _load_link_verifier()
    (tmp_path / "Real.md").write_text("safe\n", encoding="utf-8")
    (tmp_path / "alias.md").symlink_to(tmp_path / "Real.md")

    for target in ("alias.md", "real.md"):
        (tmp_path / "README.md").write_text(f"[unsafe]({target})\n", encoding="utf-8")
        with pytest.raises(verifier.DocumentLinkError):
            verifier.verify_document_links(
                tmp_path,
                documents=(Path("README.md"),),
                allowed_missing=frozenset(),
            )


def test_document_link_verifier_rejects_external_images_and_unused_exceptions(
    tmp_path: Path,
) -> None:
    verifier = _load_link_verifier()
    (tmp_path / "README.md").write_text(
        "![external](https://example.invalid/image.png)\n",
        encoding="utf-8",
    )
    with pytest.raises(verifier.DocumentLinkError):
        verifier.verify_document_links(
            tmp_path,
            documents=(Path("README.md"),),
            allowed_missing=frozenset(),
        )

    (tmp_path / "README.md").write_text("No links.\n", encoding="utf-8")
    with pytest.raises(verifier.DocumentLinkError, match="unused"):
        verifier.verify_document_links(
            tmp_path,
            documents=(Path("README.md"),),
            allowed_missing=frozenset({"docs/assets/missing.png"}),
        )


def test_repository_public_documents_have_no_broken_links() -> None:
    verifier = _load_link_verifier()

    assert verifier.TEMPORARY_MISSING_ASSETS == frozenset()
    assert (
        inspect.signature(verifier.verify_document_links).parameters["allowed_missing"].default
        == frozenset()
    )

    verifier.verify_document_links(
        PROJECT_ROOT,
        documents=tuple(Path(path) for path in PUBLIC_DOCUMENTS),
        allowed_missing=frozenset(),
    )
