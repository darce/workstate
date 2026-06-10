"""internal — development-workflow.md mentions the internal
session-compaction surfaces.

Operators reach for ``development-workflow.md`` when a hook fires or
they need to know how to disable / throttle the compaction. This test
locks in that the three env vars, the bootstrap scope flag, and the
manual ``make compact-now`` target each appear in that file so the
operator-facing reference does not drift away from the implementation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "workstate"
    / "rules"
    / "development-workflow.md"
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_doc_has_session_compaction_section(doc_text: str) -> None:
    assert "## Session Compaction" in doc_text, (
        "development-workflow.md must include a top-level "
        "'Session Compaction' section per internal."
    )


@pytest.mark.parametrize(
    "marker",
    [
        # Canonical AGENT_HANDOFF_COMPACTION_* names (consolidated to match
        # the dominant AGENT_HANDOFF_* prefix; see
        # docs/workstate/environment-variables.md and
        # BR-internal).
        "AGENT_HANDOFF_COMPACTION_DISABLED",
        "AGENT_HANDOFF_COMPACTION_MIN_NEW_TURNS",
        "AGENT_HANDOFF_COMPACTION_MIN_NEW_TOKENS",
        # Legacy WORKSTATE_* aliases must remain documented as one-release
        # back-compat so operators discover both names from one page.
        "WORKSTATE_COMPACTION_DISABLED",
        "WORKSTATE_COMPACTION_MIN_NEW_TURNS",
        "WORKSTATE_COMPACTION_MIN_NEW_TOKENS",
        "deprecated",
        "make compact-now",
        "--install-claude-stop-hook-local",
        "capture-agent-errors",
        "managed by default",
        "compact-session.py",
    ],
)
def test_doc_mentions_compaction_surfaces(doc_text: str, marker: str) -> None:
    assert marker in doc_text, (
        f"development-workflow.md must mention {marker!r} in the "
        "Session Compaction section (internal + "
        "BR-internal)."
    )


# ---------------------------------------------------------------------------
# internal — the re-injection section ("enabled vs wired vs
# reinjected" triad) is the operator-facing reference for the SessionStart
# hook; lock its surfaces so the prose cannot drift from the implementation.
# ---------------------------------------------------------------------------


def test_doc_has_reinjection_triad_section(doc_text: str) -> None:
    assert "### Enabled vs wired vs reinjected" in doc_text, (
        "development-workflow.md must keep the 'Enabled vs wired vs "
        "reinjected' re-injection section (internal)."
    )


@pytest.mark.parametrize(
    "removed_flag",
    [
        "--install-claude-stop-hook",
        "--install-claude-reinject-hook",
        "--install-claude-error-hook",
        "--install-claude-error-hook-local",
    ],
)
def test_doc_does_not_mention_removed_claude_hook_flags(
    doc_text: str, removed_flag: str
) -> None:
    """Shared / redundant Claude hook install flags were removed in implementation note."""
    assert not re.search(re.escape(removed_flag) + r"(?!-local)", doc_text), (
        f"development-workflow.md must not mention removed flag {removed_flag!r}"
    )


@pytest.mark.parametrize(
    "marker",
    [
        "--install-claude-reinject-hook-local",
        "WORKSTATE_REINJECT_SOURCES",
        "WORKSTATE_REINJECT_BUDGET_CHARS",
        "`1500`",
        "`compact,resume`",
        "reinject-context",
        "reinject skipped:",
        "latest_compaction_id",
    ],
)
def test_doc_mentions_reinjection_surfaces(doc_text: str, marker: str) -> None:
    assert marker in doc_text, (
        f"development-workflow.md must mention {marker!r} in the "
        "re-injection section (internal)."
    )
