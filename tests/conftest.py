"""Shared pytest configuration for Project Memory Hub."""

from __future__ import annotations

import sys

import pytest


_MACOS_SANDBOX_TESTS = frozenset(
    {
        "tests/e2e/test_memory_hub.py::test_local_memory_hub_e2e_is_bounded_private_and_namespace_safe",
        "tests/integration/test_proposal_container.py::test_real_cli_apply_dry_run_validates_without_changing_runtime_or_git",
        "tests/integration/test_proposal_container.py::test_valid_execution_config_builds_one_optional_applier",
        "tests/integration/test_proposal_git.py::test_apply_and_rollback_previews_validate_exact_state_without_any_write",
        "tests/integration/test_proposal_git.py::test_apply_commits_once_in_private_worktree_without_touching_user_tree",
        "tests/integration/test_proposal_git.py::test_apply_recovery_fails_closed_without_an_exact_commit",
        "tests/integration/test_proposal_git.py::test_apply_supports_configured_root_that_is_a_linked_worktree",
        "tests/integration/test_proposal_git.py::test_exact_commit_recovery_ignores_later_original_worktree_drift",
        "tests/integration/test_proposal_git.py::test_exact_commit_recovery_uses_only_recorded_git_objects",
        "tests/integration/test_proposal_git.py::test_exact_full_verification_argv_is_required_before_git_mutation",
        "tests/integration/test_proposal_git.py::test_final_db_crash_never_reapplies_after_proposal_ref_is_lost",
        "tests/integration/test_proposal_git.py::test_git_commit_db_crash_recovers_exact_commit_without_reapplying",
        "tests/integration/test_proposal_git.py::test_lock_contention_leaves_approved_proposal_and_refs_unchanged",
        "tests/integration/test_proposal_git.py::test_post_commit_cleanup_failure_remains_recoverable",
        "tests/integration/test_proposal_git.py::test_preflight_rejects_nested_special_ancestor_hidden_by_skip_worktree",
        "tests/integration/test_proposal_git.py::test_preflight_rejects_unavailable_execution_boundary_before_transition",
        "tests/integration/test_proposal_git.py::test_preflight_rejects_unsafe_repository_without_transition_or_mutation",
        "tests/integration/test_proposal_git.py::test_recovery_rejects_merge_commit_even_with_expected_tree_and_message",
        "tests/integration/test_proposal_git.py::test_repository_hook_cannot_read_outside_private_worktree",
        "tests/integration/test_proposal_git.py::test_repository_hook_is_preserved_and_deferred_without_execution",
        "tests/integration/test_proposal_git.py::test_rollback_only_marks_state_after_exact_ref_verification",
        "tests/integration/test_proposal_git.py::test_rollback_preview_rejects_ref_and_database_that_agree_on_wrong_commit",
        "tests/integration/test_proposal_git.py::test_rollback_ref_or_worktree_drift_fails_closed",
        "tests/integration/test_proposal_git.py::test_verification_executable_cannot_change_after_allowlist_configuration",
        "tests/integration/test_proposal_git.py::test_verification_failure_is_bounded_and_never_changes_user_tree",
        "tests/integration/test_proposal_git.py::test_verification_uses_fixed_process_boundary",
        "tests/integration/test_proposal_git.py::test_verifier_cannot_read_outside_private_worktree",
        "tests/integration/test_proposal_git.py::test_verifier_cannot_write_the_original_user_worktree",
        "tests/integration/test_web_proposals.py::test_applying_state_offers_recovery_without_rendering_git_metadata",
        "tests/integration/test_web_proposals.py::test_proposal_page_exposes_only_safe_review_projection_and_valid_actions",
        "tests/integration/test_web_proposals.py::test_ref_inconsistent_applied_record_is_inert_in_ui_and_direct_post",
        "tests/integration/test_web_proposals.py::test_web_apply_and_rollback_use_isolated_git_result",
        "tests/integration/test_web_proposals.py::test_web_apply_does_not_block_the_event_loop",
        "tests/integration/test_web_proposals.py::test_web_rollback_does_not_block_the_event_loop",
    }
)
_MACOS_F_GETPATH_TESTS = frozenset(
    {
        "tests/integration/test_web_security.py::test_multipart_spool_stays_private_and_is_closed_after_response",
    }
)
_MACOS_ONLY_TESTS = _MACOS_SANDBOX_TESTS | _MACOS_F_GETPATH_TESTS


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Keep portable Linux checks while naming the exact Darwin-only boundary."""
    for item in items:
        base_nodeid = item.nodeid.partition("[")[0]
        if base_nodeid not in _MACOS_ONLY_TESTS:
            continue
        item.add_marker(pytest.mark.macos_only)
        if sys.platform == "darwin":
            continue
        boundary = (
            "fcntl.F_GETPATH" if base_nodeid in _MACOS_F_GETPATH_TESTS else "/usr/bin/sandbox-exec"
        )
        item.add_marker(pytest.mark.skip(reason=f"requires macOS {boundary}"))
