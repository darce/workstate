"""Tests for the canonical branch-naming rule.

implementation note implementation note: ``workstate_protocol.branch_naming`` exposes
``TASK_REF_RE``, ``derive_task_ref_candidates``,
``format_suggested_branch_name``, and ``__protocol_version__`` as the
single source of truth for branch-naming validation across every gate.
``workstate_handoff_mcp`` re-exports these without duplication.
"""

from __future__ import annotations

import re

import pytest

from workstate_protocol import branch_naming
from workstate_protocol.branch_naming import (
    TASK_REF_RE,
    derive_task_ref_candidates,
    format_suggested_branch_name,
    select_task_ref_candidate,
)


POSITIVE_CORPUS = (
    "feature/WORKSTATE-35",
    "feature/WORKSTATE-37-branch-naming-enforcement",
    "feature/maint-dirty-br-01",
    "feature/maint-archive-stale-20260502",
    "feature/plan-0006",
    "feature/e17-10",
)


NEGATIVE_CORPUS = (
    "main",
    "master",
    "fix/foo",
    "chore/bar",
    "wip-thing",
    "feature/foo",  # single-segment
    "feature/foo-bar",  # no digit
    "feature/no-digits-here",
    "hotfix/x",
    "release/v1",
    "feature/WORKSTATE-REF-37",  # uppercase
    "feature/-x",  # leading hyphen
    "feature/123-foo",  # leading digit segment
    "feature/",  # empty task ref
    "FEATURE/WORKSTATE-37",  # uppercase prefix
    "",
)


@pytest.mark.parametrize("name", POSITIVE_CORPUS)
def test_task_ref_re_accepts_conforming_names(name: str) -> None:
    assert TASK_REF_RE.match(name) is not None


@pytest.mark.parametrize("name", NEGATIVE_CORPUS)
def test_task_ref_re_rejects_non_conforming_names(name: str) -> None:
    assert TASK_REF_RE.match(name) is None


def test_task_ref_re_is_compiled_regex() -> None:
    assert isinstance(TASK_REF_RE, re.Pattern)


def test_protocol_version_marker_is_string() -> None:
    assert isinstance(branch_naming.__protocol_version__, str)
    assert branch_naming.__protocol_version__ != ""


@pytest.mark.parametrize(
    "branch,expected",
    [
        # Walk every digit-bearing prefix from longest to shortest. ``WORKSTATE`` is
        # dropped because it has no digit.
        ("feature/WORKSTATE-37-branch-naming-enforcement", [
            "WORKSTATE-37-branch-naming-enforcement",
            "WORKSTATE-37-branch-naming",
            "WORKSTATE-37-branch",
            "WORKSTATE-37",
        ]),
        ("feature/WORKSTATE-37", ["WORKSTATE-37"]),
        # Only the full ref carries a digit; shorter prefixes are digit-less.
        ("feature/maint-dirty-br-01", ["maint-dirty-br-01"]),
        ("feature/plan-0006", ["plan-0006"]),
        # Both ``e17-10`` and ``e17`` carry a digit; both are returned.
        ("feature/e17-10", ["e17-10", "e17"]),
        # non-conforming → empty
        ("feature/foo", []),
        ("feature/foo-bar", []),
        ("fix/foo", []),
        ("main", []),
        ("", []),
    ],
)
def test_derive_task_ref_candidates(branch: str, expected: list[str]) -> None:
    assert derive_task_ref_candidates(branch) == expected


def test_derive_returns_lowercase_for_conforming() -> None:
    """Callers uppercase before intersecting against the live task table.
    The derivation MUST stay lowercase so that intersection logic owns the
    case conversion rather than guessing it here."""
    candidates = derive_task_ref_candidates(
        "feature/WORKSTATE-37-branch-naming-enforcement"
    )
    assert all(c == c.lower() for c in candidates)


@pytest.mark.parametrize(
    "task_ref,slug,expected",
    [
        ("WORKSTATE-REF-37", None, "feature/WORKSTATE-37"),
        ("WORKSTATE-37", None, "feature/WORKSTATE-37"),
        ("WORKSTATE-REF-37", "branch-naming-enforcement",
         "feature/WORKSTATE-37-branch-naming-enforcement"),
        ("WORKSTATE-REF-DIRTY-BR-01", None, "feature/maint-dirty-br-01"),
        ("PLAN-0006", "rollout", "feature/plan-0006-rollout"),
    ],
)
def test_format_suggested_branch_name(
    task_ref: str, slug: str | None, expected: str
) -> None:
    suggestion = format_suggested_branch_name(task_ref, slug=slug)
    assert suggestion == expected
    assert TASK_REF_RE.match(suggestion) is not None


def test_format_suggested_branch_name_returns_none_without_task_ref() -> None:
    """Callers (e.g. the post-checkout warn helper) pass None when no
    active task is registered. The formatter returns None so the warn
    path can fall back to a generic message instead of crashing."""
    assert format_suggested_branch_name(None) is None
    assert format_suggested_branch_name("") is None


def test_select_task_ref_candidate_no_registry_returns_shortest_prefix() -> None:
    """When ``known_task_refs`` is empty/``None``, the selector degrades to
    the shortest digit-bearing prefix (today's behavior). This locks the
    no-context fallback so degraded environments without a registry keep
    resolving correctly.
    """
    assert (
        select_task_ref_candidate(
            "feature/WORKSTATE-63-fu-tighten-compaction-defaults", known_task_refs=None
        )
        == "WORKSTATE-REF-63"
    )
    assert (
        select_task_ref_candidate(
            "feature/WORKSTATE-63-fu-tighten-compaction-defaults", known_task_refs=()
        )
        == "WORKSTATE-REF-63"
    )
    assert (
        select_task_ref_candidate(
            "feature/WORKSTATE-37-branch-naming-enforcement", known_task_refs=set()
        )
        == "WORKSTATE-REF-37"
    )
    assert (
        select_task_ref_candidate("feature/maint-dirty-br-01", known_task_refs=None)
        == "WORKSTATE-REF-DIRTY-BR-01"
    )


def test_select_task_ref_candidate_longest_registered_wins() -> None:
    """With both the base and a follow-up registered, the longer registered
    candidate wins. This is the WORKSTATE-REF-63 vs WORKSTATE-REF-63-FU-... case that
    motivated the selector.
    """
    branch = "feature/WORKSTATE-63-fu-tighten-compaction-defaults"
    known = {"WORKSTATE-REF-63", "WORKSTATE-REF-63-FU-TIGHTEN-COMTASKCTION-DEFAULTS"}
    assert (
        select_task_ref_candidate(branch, known_task_refs=known)
        == "WORKSTATE-REF-63-FU-TIGHTEN-COMTASKCTION-DEFAULTS"
    )


def test_select_task_ref_candidate_only_base_registered_picks_base() -> None:
    """When only the base ref is registered, no longer candidate intersects
    and the base wins (it is itself a registered candidate)."""
    branch = "feature/WORKSTATE-63-fu-tighten-compaction-defaults"
    assert (
        select_task_ref_candidate(branch, known_task_refs={"WORKSTATE-REF-63"})
        == "WORKSTATE-REF-63"
    )


def test_select_task_ref_candidate_no_intersection_returns_none() -> None:
    """If ``known_task_refs`` is non-empty but no candidate intersects, the
    selector returns ``None`` rather than naming a candidate absent from a
    populated registry (WORKSTATE65-BR-02 invariant: "no resolver path should
    return a candidate absent from a non-empty registry"). The no-context
    shortest-prefix fallback applies only when the registry is genuinely
    empty / unavailable, not when it answered with unrelated rows."""
    branch = "feature/WORKSTATE-63-fu-tighten-compaction-defaults"
    assert (
        select_task_ref_candidate(branch, known_task_refs={"WORKSTATE-REF-02", "WORKSTATE-REF-99"})
        is None
    )


def test_select_task_ref_candidate_case_insensitive_intersection() -> None:
    """``derive_task_ref_candidates`` returns lowercase; registry refs are
    canonically uppercase. The selector normalizes both sides so a
    lowercase or mixed-case registry still resolves correctly."""
    branch = "feature/WORKSTATE-63-fu-example"
    assert (
        select_task_ref_candidate(branch, known_task_refs={"WORKSTATE-63-fu-example"})
        == "WORKSTATE-REF-63-FU-EXAMPLE"
    )
    assert (
        select_task_ref_candidate(branch, known_task_refs={"WORKSTATE-63-Fu-Example"})
        == "WORKSTATE-REF-63-FU-EXAMPLE"
    )


def test_select_task_ref_candidate_non_conforming_branch_returns_none() -> None:
    """Branches that do not match ``TASK_REF_RE`` produce no candidates and
    the selector returns ``None`` regardless of registry state."""
    assert select_task_ref_candidate("fix/foo") is None
    assert select_task_ref_candidate("feature/no-digits-here") is None
    assert select_task_ref_candidate("main") is None
    assert select_task_ref_candidate("") is None
    assert (
        select_task_ref_candidate("fix/foo", known_task_refs={"WORKSTATE-REF-1"}) is None
    )


def test_select_task_ref_candidate_single_segment_unchanged() -> None:
    """Single-segment refs like ``WORKSTATE-REF-DIRTY-BR-01`` (one task ref, no
    follow-up suffix possible) resolve identically with or without
    registry context."""
    branch = "feature/maint-dirty-br-01"
    assert (
        select_task_ref_candidate(branch, known_task_refs={"WORKSTATE-REF-DIRTY-BR-01"})
        == "WORKSTATE-REF-DIRTY-BR-01"
    )
    assert (
        select_task_ref_candidate(branch, known_task_refs=None)
        == "WORKSTATE-REF-DIRTY-BR-01"
    )


def test_select_task_ref_candidate_in_module_all() -> None:
    """The selector must be in ``branch_naming.__all__`` so the public
    surface advertises it alongside the parser."""
    assert "select_task_ref_candidate" in branch_naming.__all__


def test_handoff_re_export_is_canonical_object() -> None:
    """``workstate_handoff_mcp`` MUST re-export the same compiled regex
    object — not a literal copy. A second compiled pattern would let
    grammar tweaks drift across packages.
    """
    from workstate_handoff_mcp import (
        TASK_REF_RE as handoff_re,
        derive_task_ref_candidates as handoff_derive,
        format_suggested_branch_name as handoff_format,
    )

    assert handoff_re is TASK_REF_RE
    assert handoff_derive is derive_task_ref_candidates
    assert handoff_format is format_suggested_branch_name
