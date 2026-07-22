from __future__ import annotations

import re
from pathlib import Path

import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[2]
GOVERNANCE_FILES = (
    "SECURITY.md",
    "CONTRIBUTING.md",
    "CODE_OF_CONDUCT.md",
    "CHANGELOG.md",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/pull_request_template.md",
)
PRIVATE_MATERIAL = (
    "absolute paths",
    "access tokens",
    "session transcripts",
    "database contents",
    "private screenshots",
    "model credentials",
)


def _read(relative_path: str) -> str:
    path = PROJECT_ROOT / relative_path
    assert path.is_file(), f"missing governance file: {relative_path}"
    return path.read_text(encoding="utf-8")


def _yaml(relative_path: str) -> dict[str, object]:
    document = yaml.safe_load(_read(relative_path))
    assert type(document) is dict
    return document


def test_public_repository_governance_inventory_exists() -> None:
    assert all((PROJECT_ROOT / path).is_file() for path in GOVERNANCE_FILES)


def test_security_policy_uses_conditional_private_reporting_without_personal_contact() -> None:
    security = _read("SECURITY.md")

    assert "## Supported versions" in security
    assert "private vulnerability reporting" in security.casefold()
    assert "If the repository shows a **Report a vulnerability** button" in security
    assert "Do not open a public issue" in security
    assert "not guaranteed" in security.casefold()
    assert not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", security)


def test_contributing_contract_freezes_privacy_and_verified_reporting() -> None:
    contributing = _read("CONTRIBUTING.md")

    assert "uv sync --locked --extra test" in contributing
    assert "Apache-2.0" in contributing
    assert "project_id + source_agent + model_id" in contributing
    assert "Tests actually run" in contributing
    assert "Generated assets" in contributing
    for item in PRIVATE_MATERIAL:
        assert item in contributing


def test_code_of_conduct_has_no_placeholder_or_fictional_private_channel() -> None:
    conduct = _read("CODE_OF_CONDUCT.md")

    assert "Contributor Covenant" in conduct
    assert "version 2.1" in conduct.casefold()
    assert "GitHub Report Abuse" in conduct
    assert "Do not post incident details in a public issue" in conduct
    assert "[INSERT" not in conduct
    assert not re.search(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", conduct)


def test_changelog_marks_beta_candidate_without_claiming_publication() -> None:
    changelog = _read("CHANGELOG.md")

    assert "## [0.2.1] - Unreleased" in changelog
    assert "Public Beta candidate" in changelog
    assert "No GitHub Release or PyPI publication is claimed" in changelog
    assert "github.com/" not in changelog


def test_issue_forms_are_structured_private_and_do_not_invent_repository_metadata() -> None:
    for relative_path in (
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
    ):
        document = _yaml(relative_path)
        assert document.get("name")
        assert document.get("description")
        assert document.get("labels", []) == []
        assert document.get("assignees", []) == []
        body = document["body"]
        assert type(body) is list
        serialized = yaml.safe_dump(document, sort_keys=True)
        for item in PRIVATE_MATERIAL:
            assert item in serialized
        assert "security vulnerabilities" in serialized.casefold()
        assert "type: upload" not in serialized
        ids = [item.get("id") for item in body if type(item) is dict and item.get("id")]
        assert len(ids) == len(set(ids))
        assert "privacy_confirmation" in ids
        assert "conduct_confirmation" in ids

    config = _yaml(".github/ISSUE_TEMPLATE/config.yml")
    assert config == {"blank_issues_enabled": False}


def test_pull_request_template_demands_honest_verification_and_privacy_review() -> None:
    template = _read(".github/pull_request_template.md")

    for heading in (
        "## Summary",
        "## Motivation",
        "## Changes",
        "## Privacy and security",
        "## Verification actually run",
        "## Verification not run",
        "## Compatibility",
        "## Documentation and changelog",
        "## Risks and rollback",
    ):
        assert heading in template
    assert "[ ]" in template
    assert "[x]" not in template.casefold()
    for item in PRIVATE_MATERIAL:
        assert item in template


def test_governance_files_contain_no_private_paths_or_repository_placeholders() -> None:
    combined = "\n".join(_read(path) for path in GOVERNANCE_FILES)

    assert "/Users/" not in combined
    assert "file://" not in combined
    assert "<repository-url>" not in combined
    assert "github.com/OWNER" not in combined
    assert "github.com/your-" not in combined.casefold()
