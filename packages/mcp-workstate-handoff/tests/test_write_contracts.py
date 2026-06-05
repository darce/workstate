"""implementation note implementation note — write-contract registry."""

from __future__ import annotations


def test_registry_has_top_level_rows_for_known_mcp_write_tools() -> None:
    from workstate_handoff_mcp.write_contracts import REGISTRY, get_write_contract

    expected_top_level = {
        "set_handoff_state",
        "record_event",
        "review_runs",
        "review_findings",
        "next_actions",
        "close_slice",
        "archive",
        "update_task_status",
        "record_file_touch",
        "artifacts",
        "compact_session",
        "import_handoff_state",
        "export_handoff_state",
    }
    assert expected_top_level <= set(REGISTRY)
    contract = get_write_contract("close_slice")
    assert contract is not None
    assert contract.tool_name == "close_slice"
    assert isinstance(contract.required, list)


def test_record_event_carries_typed_subgrammar_variants() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("record_event")
    assert contract is not None
    variants = contract.variants
    assert {"decision", "test_result", "blocker"} <= set(variants)
    decision_variant = variants["decision"]
    assert "decision" in decision_variant.field_grammars


def test_validate_write_rejects_missing_required_field() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write("close_slice", {})
    assert result["ok"] is False
    assert any("required" in err.lower() or "missing" in err.lower() for err in result["errors"])


def test_validate_write_accepts_minimum_valid_payload() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "close_slice",
        {
            "task_ref": "WORKSTATE-REF-EXAMPLE",
            "author_tag": "claude",
            "work_ref": "plan0010",
            "slug": "demo",
            "rationale": "## Changes\n- a\n\n## Verification\n- b\n\n## Schema / Contract Changes\n- none\n\n## Open Threads\n- none",
            "session": "session-1",
            "expected_revision": 1,
        },
    )
    assert result["ok"] is True, result


def test_validate_write_unknown_tool_reports_missing_registry_row() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write("totally_made_up_tool", {})
    assert result["ok"] is False
    assert any("registry" in err.lower() or "unknown" in err.lower() for err in result["errors"])


def test_record_event_decision_accepts_optional_plan_revision() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    contract = get_write_contract("record_event")
    assert contract is not None
    decision_variant = contract.variants["decision"]
    assert "plan_revision" in decision_variant.optional
    assert "plan_revision" in decision_variant.field_grammars


def test_validate_write_rejects_malformed_plan_revision() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "record_event",
        {
            "event": {
                "event_kind": "decision",
                "decision": "claude_demo_decision",
                "rationale": "demo",
                "plan_revision": "not-a-revision-name",
            }
        },
    )
    assert result["ok"] is False
    assert any("plan_revision" in err for err in result["errors"])


def test_validate_write_accepts_valid_plan_revision() -> None:
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "record_event",
        {
            "event": {
                "event_kind": "decision",
                "decision": "claude_demo_decision",
                "rationale": "demo",
                "plan_revision": "0010-frictionless-receipts-r2.md",
            }
        },
    )
    assert result["ok"] is True, result


# ---------------------------------------------------------------------------
# F7: Align slice-complete decision-id grammar with documented valid_examples
# ---------------------------------------------------------------------------

_CANONICAL_VALID_EXAMPLES = [
    "codex_slice_complete_plan0005_render_budget_benchmark",
    "copilot_slice_complete_WORKSTATE-DASHBOARD-AUTOREGEN_hook_removed_followup",
]


def test_record_event_decision_accepts_every_canonical_valid_example() -> None:
    """Every valid_examples entry from slice_complete_decision_id must pass record_event grammar.

    F7: the old decision regex ^[A-Za-z][A-Za-z0-9_]*$ rejects hyphens, so
    copilot_slice_complete_WORKSTATE-DASHBOARD-AUTOREGEN_hook_removed_followup was
    silently refused by the write-contract gate.
    """
    from workstate_handoff_mcp.write_contracts import validate_write

    for decision_id in _CANONICAL_VALID_EXAMPLES:
        result = validate_write(
            "record_event",
            {
                "event": {
                    "event_kind": "decision",
                    "decision": decision_id,
                    "rationale": "placeholder rationale for grammar contract test",
                    "session": "contract-test",
                }
            },
        )
        assert result["ok"] is True, f"record_event rejected canonical decision id {decision_id!r}: {result}"


def test_close_slice_work_ref_accepts_hyphenated_task_ref_segments() -> None:
    """close_slice work_ref must accept uppercase-hyphenated values like WORKSTATE-REF-DASHBOARD-AUTOREGEN.

    F7: the old work_ref regex ^[a-z0-9][a-z0-9_]*$ rejects hyphens and uppercase,
    making task-ref-style work_refs (WORKSTATE-REF-*, WORKSTATE-REF-*) unwritable through close_slice.
    """
    from workstate_handoff_mcp.write_contracts import validate_write

    result = validate_write(
        "close_slice",
        {
            "task_ref": "WORKSTATE-REF-50",
            "author_tag": "copilot",
            "work_ref": "WORKSTATE-REF-DASHBOARD-AUTOREGEN",
            "slug": "hook_removed_followup",
            "rationale": (
                "## Changes\n- a\n\n## Verification\n- b\n\n"
                "## Schema / Contract Changes\n- none\n\n## Open Threads\n- none"
            ),
            "session": "contract-test",
            "expected_revision": 1,
        },
    )
    assert result["ok"] is True, f"close_slice rejected hyphenated work_ref: {result}"
