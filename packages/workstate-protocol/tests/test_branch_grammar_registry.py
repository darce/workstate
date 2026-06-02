"""implementation note implementation note — branch-grammar registry."""

from __future__ import annotations

import pytest


def test_registry_includes_documented_exception_kinds() -> None:
    from workstate_protocol.branch_naming import BRANCH_GRAMMAR_REGISTRY

    kinds = {entry.kind for entry in BRANCH_GRAMMAR_REGISTRY}
    expected = {"feature", "release", "hotfix", "maint", "revert"}
    assert expected <= kinds


@pytest.mark.parametrize(
    "branch,expected_kind",
    [
        ("feature/WORKSTATE-37-branch-naming-enforcement", "feature"),
        ("release/0.9.1", "release"),
        ("hotfix/WORKSTATE-99-fix-bug", "hotfix"),
        ("maint/cleanup-stale-rows", "maint"),
        ("revert/WORKSTATE-12-bad-merge", "revert"),
    ],
)
def test_classify_branch_returns_correct_kind(branch: str, expected_kind: str) -> None:
    from workstate_protocol.branch_naming import classify_branch

    classification = classify_branch(branch)
    assert classification is not None
    assert classification.kind == expected_kind


@pytest.mark.parametrize(
    "branch",
    [
        "feature/WORKSTATE-37-branch-naming-enforcement",
        "release/0.9.1",
        "hotfix/WORKSTATE-99-fix-bug",
        "maint/cleanup-stale-rows",
        "revert/WORKSTATE-12-bad-merge",
    ],
)
def test_is_allowed_branch_passes_for_documented_patterns(branch: str) -> None:
    from workstate_protocol.branch_naming import is_allowed_branch

    assert is_allowed_branch(branch, mode="post_checkout_warn")


def test_is_allowed_branch_fails_closed_for_unknown_pattern() -> None:
    from workstate_protocol.branch_naming import classify_branch, is_allowed_branch

    assert classify_branch("experiment/some-prototype") is None
    assert not is_allowed_branch("experiment/some-prototype", mode="post_checkout_warn")


def test_main_is_classified() -> None:
    from workstate_protocol.branch_naming import classify_branch

    assert classify_branch("main") is not None
