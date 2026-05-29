"""implementation note implementation note — development-workflow.md mentions the WORKSTATE-REF-39
session-compaction surfaces.

Operators reach for ``development-workflow.md`` when a hook fires or
they need to know how to disable / throttle the compaction. This test
locks in that the three env vars, the bootstrap scope flag, and the
manual ``make compact-now`` target each appear in that file so the
operator-facing reference does not drift away from the implementation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

DOC_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "agentic"
    / "rules"
    / "development-workflow.md"
)


@pytest.fixture(scope="module")
def doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_doc_has_session_compaction_section(doc_text: str) -> None:
    assert "## Session Compaction" in doc_text, (
        "development-workflow.md must include a top-level "
        "'Session Compaction' section per implementation note implementation note."
    )


@pytest.mark.parametrize(
    "marker",
    [
        # Canonical AGENT_HANDOFF_COMPACTION_* names (consolidated to match
        # the dominant AGENT_HANDOFF_* prefix; see
        # docs/agentic/environment-variables.md and
        # BR-WORKSTATE-REF-34-COMPENV-02).
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
        "--install-claude-stop-hook",
        "--install-claude-stop-hook-local",
        "compact-session.py",
    ],
)
def test_doc_mentions_compaction_surfaces(doc_text: str, marker: str) -> None:
    assert marker in doc_text, (
        f"development-workflow.md must mention {marker!r} in the "
        "Session Compaction section (implementation note implementation note + "
        "BR-WORKSTATE-REF-34-COMPENV-02)."
    )
