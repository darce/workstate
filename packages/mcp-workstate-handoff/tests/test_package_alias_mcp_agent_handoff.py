"""implementation note BR-06 — distribution-name alias package.

The wheel ships as ``mcp-workstate-handoff`` (PEP 503-normalised), but the
import path is the legacy ``workstate_handoff_mcp``. The branch review
called out that ``mcp_workstate_handoff`` does not exist as an importable
source package, leaving callers no way to import from a name that
matches the distribution.

Adding a thin alias package ``mcp_workstate_handoff`` that re-exports the
real public surface from ``workstate_handoff_mcp`` resolves the gap with
zero rename churn across the monorepo. The legacy import path keeps
working; new callers can write ``from mcp_workstate_handoff import api``.
"""

from __future__ import annotations


def test_mcp_workstate_handoff_alias_imports() -> None:
    import mcp_workstate_handoff  # noqa: F401

    assert mcp_workstate_handoff is not None


def test_mcp_workstate_handoff_api_module_aliased() -> None:
    from mcp_workstate_handoff import api as alias_api
    from workstate_handoff_mcp import api as real_api

    assert alias_api is real_api


def test_mcp_workstate_handoff_top_level_symbols_match() -> None:
    import mcp_workstate_handoff
    import workstate_handoff_mcp

    for symbol in ("BranchMismatchError", "PromptMetrics", "TokenUsage"):
        assert hasattr(mcp_workstate_handoff, symbol)
        assert getattr(mcp_workstate_handoff, symbol) is getattr(workstate_handoff_mcp, symbol)
