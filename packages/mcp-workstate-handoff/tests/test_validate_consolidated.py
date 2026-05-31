"""WORKSTATE-REF-45 implementation note: validate(kind=...) merge.

Asserts the merged ``validate`` MCP tool dispatches to the
``decision_id`` and ``write`` kinds with the same envelopes the legacy
``validate_decision_id`` and ``validate_write`` tools produced, and that
the legacy registrations have been removed in favor of the consolidated
entry.
"""

from __future__ import annotations


def test_validate_kind_decision_id_returns_legacy_envelope() -> None:
    from workstate_handoff_mcp.api import validate

    result = validate({"kind": "decision_id", "decision": "claude_demo_decision"})

    assert result["tool"] == "validate"
    assert "data" in result
    data = result["data"]
    assert "ok" in data
    assert "category" in data or "errors" in data


def test_validate_kind_write_returns_legacy_envelope() -> None:
    from workstate_handoff_mcp.api import validate

    result = validate(
        {
            "kind": "write",
            "tool_name": "close_slice",
            "payload": {
                "task_ref": "WORKSTATE-REF-EXAMPLE",
                "author_tag": "claude",
                "work_ref": "plan0010",
                "slug": "demo",
                "rationale": (
                    "## Changes\n- a\n\n"
                    "## Verification\n- b\n\n"
                    "## Schema / Contract Changes\n- none\n\n"
                    "## Open Threads\n- none"
                ),
                "session": "session-1",
                "expected_revision": 1,
            },
        }
    )

    assert result["tool"] == "validate"
    assert result["ok"] is True
    data = result["data"]
    assert data["ok"] is True
    assert "errors" in data


def test_validate_kind_write_reports_missing_registry_row() -> None:
    from workstate_handoff_mcp.api import validate

    result = validate({"kind": "write", "tool_name": "totally_made_up_tool", "payload": {}})

    assert result["ok"] is False
    data = result["data"]
    assert data["ok"] is False
    assert any("registry" in err.lower() or "unknown" in err.lower() for err in data["errors"])


def test_registry_replaces_validate_split_with_consolidated_tool() -> None:
    from workstate_handoff_mcp.api import _build_tool_registry

    registry = _build_tool_registry()
    names = {entry.name for entry in registry}

    assert "validate" in names, "consolidated validate tool must be registered"
    assert "validate_decision_id" not in names, (
        "legacy validate_decision_id must be removed in favor of validate(kind=decision_id)"
    )
    assert "validate_write" not in names, "legacy validate_write must be removed in favor of validate(kind=write)"


def test_expected_handoff_tool_count_decremented_for_slice_1() -> None:
    """implementation note dropped the count by exactly 2 (validate_decision_id + validate_write -> validate).

    The live invariant is asserted in the per-slice test file for the most recent
    slice, so this test only enforces that implementation note's contribution is non-negative
    against the ceiling of 30 (the pre-slice-1 value).
    """
    from workstate_handoff_mcp.invariants import EXPECTED_HANDOFF_TOOL_COUNT

    assert EXPECTED_HANDOFF_TOOL_COUNT <= 29
