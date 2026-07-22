from __future__ import annotations

import re
from pathlib import Path


_I18N_MARKER = re.compile(r'data-i18n(?:-count|-title|-aria-label)?="([^"]+)"')


def test_every_static_template_translation_key_is_cataloged() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    templates = repository_root / "src/project_memory_hub/web/templates"
    script = (repository_root / "src/project_memory_hub/web/static/i18n.js").read_text(
        encoding="utf-8"
    )

    keys: set[str] = set()
    for template in templates.glob("*.html"):
        text = template.read_text(encoding="utf-8")
        assert "data-i18n-value" not in text
        keys.update(
            key for key in _I18N_MARKER.findall(text) if "{{" not in key and "{%" not in key
        )

    missing = sorted(key for key in keys if f'"{key}"' not in script)
    assert missing == []

    semantic_keys = sorted(key for key in keys if "." in key)
    one_sided = sorted(key for key in semantic_keys if script.count(f'"{key}"') < 2)
    assert one_sided == []


def test_i18n_script_uses_text_only_local_translation() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script = (repository_root / "src/project_memory_hub/web/static/i18n.js").read_text(
        encoding="utf-8"
    )

    assert "textContent" in script
    assert "innerHTML" not in script
    assert ".value =" not in script
    assert 'setAttribute("value"' not in script
    assert "fetch(" not in script
    assert "XMLHttpRequest" not in script
    assert "http://" not in script
    assert "https://" not in script


def test_setup_translation_contract_is_complete_in_both_catalogs() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script = (repository_root / "src/project_memory_hub/web/static/i18n.js").read_text(
        encoding="utf-8"
    )
    keys = {
        "nav.setup",
        "page.setup",
        "index.setup",
        "document.setup",
        "setup.lede",
        "setup.isolation",
        "setup.status_heading",
        "setup.roots_ready",
        "setup.projects_found",
        "setup.first_memory",
        "setup.automation",
        "setup.automation_note",
        "setup.optional_sources_note",
        "setup.save",
        "setup.complete",
        "setup.callout_heading",
        "setup.callout_body",
        "setup.open",
        "setup.reopen",
        "setup.completed_notice",
        "setup.next_step_heading",
        "setup.next_step.configure",
        "setup.next_step.discover",
        "setup.next_step.first_memory",
        "setup.next_step.authorize_automation",
        "setup.next_step.ready",
        "setup.automation.current",
        "setup.automation.authorization_required",
        "setup.automation.drifted",
        "setup.automation.unavailable",
    }

    assert all(script.count(f'"{key}"') == 2 for key in keys)
    assert 'setup_required: "需要先完成配置"' in script


def test_public_beta_dynamic_presentation_keys_exist_once_per_catalog() -> None:
    repository_root = Path(__file__).resolve().parents[2]
    script = (repository_root / "src/project_memory_hub/web/static/i18n.js").read_text(
        encoding="utf-8"
    )
    dynamic_keys = {
        *(
            f"overview.next_step.{kind}.{field}"
            for kind in ("discover", "scan", "doctor", "reconcile")
            for field in ("reason", "success")
        ),
        "memories.no_shared_facts_success",
        "memories.choose_exact_source_model_success",
        "memories.choose_registered_project_source_model",
        "memories.choose_registered_project_source_model_success",
        "memories.choose_project_source_model_success",
        "memories.none_recorded_success",
        "proposals.no_promotions_success",
        "proposals.none_recorded_success",
        "projects.no_discovery_issues_success",
        "projects.duplicates_not_recorded_success",
        "projects.none_recorded_success",
    }

    assert all(script.count(f'"{key}"') == 2 for key in dynamic_keys)
