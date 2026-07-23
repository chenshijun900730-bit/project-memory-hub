from __future__ import annotations

import inspect
from types import ModuleType
from typing import Any

from tests import conftest as test_policy
from tests.e2e import test_memory_hub
from tests.integration import (
    test_proposal_container,
    test_proposal_git,
    test_web_proposals,
    test_web_security,
)


def _test_names(module: ModuleType) -> frozenset[str]:
    return frozenset(
        name
        for name, value in inspect.getmembers(module, inspect.isfunction)
        if name.startswith("test_") and value.__module__ == module.__name__
    )


def _classified_names(path: str, *, f_getpath: bool = False) -> frozenset[str]:
    selected = test_policy._MACOS_F_GETPATH_TESTS if f_getpath else test_policy._MACOS_SANDBOX_TESTS
    prefix = f"{path}::"
    return frozenset(
        nodeid.removeprefix(prefix) for nodeid in selected if nodeid.startswith(prefix)
    )


class _CollectedItem:
    def __init__(self, nodeid: str) -> None:
        self.nodeid = nodeid
        self.markers: list[Any] = []

    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


def test_macos_capability_policy_keeps_portable_proposal_tests_on_linux() -> None:
    portable = {
        "test_bounded_process_input_cannot_block_past_timeout",
        "test_cleanup_never_follows_a_swapped_worktree_symlink",
        "test_cleanup_rejects_symlink_swap_between_check_and_git_remove",
        "test_verification_allowlist_rejects_relative_or_interpreter_code",
    }
    classified = _classified_names("tests/integration/test_proposal_git.py")

    assert classified == _test_names(test_proposal_git) - portable
    assert classified.isdisjoint(portable)


def test_macos_capability_policy_names_only_the_exact_cross_platform_boundaries() -> None:
    assert _classified_names("tests/integration/test_proposal_container.py") == {
        "test_real_cli_apply_dry_run_validates_without_changing_runtime_or_git",
        "test_valid_execution_config_builds_one_optional_applier",
    }
    assert _classified_names("tests/integration/test_web_proposals.py") == {
        "test_applying_state_offers_recovery_without_rendering_git_metadata",
        "test_proposal_page_exposes_only_safe_review_projection_and_valid_actions",
        "test_ref_inconsistent_applied_record_is_inert_in_ui_and_direct_post",
        "test_web_apply_and_rollback_use_isolated_git_result",
        "test_web_apply_does_not_block_the_event_loop",
        "test_web_rollback_does_not_block_the_event_loop",
    }
    assert _classified_names("tests/e2e/test_memory_hub.py") == {
        "test_local_memory_hub_e2e_is_bounded_private_and_namespace_safe",
    }
    assert _classified_names(
        "tests/integration/test_web_security.py",
        f_getpath=True,
    ) == {"test_multipart_spool_stays_private_and_is_closed_after_response"}
    assert not any(
        "test_automation.py" in nodeid or "test_doctor.py" in nodeid
        for nodeid in test_policy._MACOS_ONLY_TESTS
    )
    assert _test_names(test_proposal_container)
    assert _test_names(test_web_proposals)
    assert _test_names(test_memory_hub)
    assert _test_names(test_web_security)


def test_non_darwin_collection_marks_and_skips_only_classified_items(monkeypatch) -> None:
    classified = _CollectedItem(
        "tests/integration/test_proposal_git.py::"
        "test_apply_commits_once_in_private_worktree_without_touching_user_tree"
    )
    portable = _CollectedItem(
        "tests/integration/test_proposal_git.py::"
        "test_bounded_process_input_cannot_block_past_timeout"
    )
    monkeypatch.setattr(test_policy.sys, "platform", "linux")

    test_policy.pytest_collection_modifyitems([classified, portable])

    assert [marker.name for marker in classified.markers] == ["macos_only", "skip"]
    assert "sandbox-exec" in classified.markers[1].kwargs["reason"]
    assert portable.markers == []
