"""implementation note (implementation note / WS-ERRTEL-01): MCP server self-capture of rejected writes.

The server already sees its own rejected writes; implementation note makes it record
them. Every ok:false envelope returned by a write tool (a tool with a
``write_contracts`` registry row) through the MCP wrapper also lands an
``agent_errors`` row classed ``mcp_write_rejected`` — without changing
the caller-visible response, with a 10-minute dedup window on
``(error_class, summary, task_ref)``, and with a never-fail guarantee.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Callable

import pytest

from workstate_handoff_mcp import agent_errors as agent_errors_module
from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import _get_db_connection


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> RuntimeConfig:
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=tmp_path / ".task-state",
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def _wrapped_tool(name: str) -> Callable[..., object]:
    """Resolve the MCP-registered wrapper for a tool, as FastMCP would call it."""

    entry = next(e for e in mcp_server._build_tool_registry() if e.name == name)
    return mcp_server._wrap_branch_mismatch_for_mcp(entry)


def _agent_error_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM agent_errors ORDER BY id ASC").fetchall()


def _rejected_close_slice(close_slice: Callable[..., object]) -> dict:
    """Drive a deterministic close_slice rejection (XML-in-rationale guard)."""

    result = close_slice(
        session="self-capture-test",
        decision="claude_slice_complete_WS-ERRTEL-01_selfcapture",
        rationale="## Changes\n<actor>oops</actor>\n## Verification\nx\n## Schema / Contract Changes\nx\n## Open Threads\nx",
        task_ref="WS-ERRTEL-01",
    )
    assert isinstance(result, dict)
    return result


# ---------------------------------------------------------------------------
# Core capture behavior
# ---------------------------------------------------------------------------


def test_rejected_close_slice_self_captures_one_row(isolated_runtime: RuntimeConfig) -> None:
    close_slice = _wrapped_tool("close_slice")
    result = _rejected_close_slice(close_slice)

    # Caller-visible response unchanged: still the original rejection shape.
    assert result["ok"] is False
    assert "rationale contains the XML-like tag" in result["data"]["error"]
    assert result["data"]["rejected_tag"] == "<actor>"

    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["error_class"] == "mcp_write_rejected"
    assert row["tool_name"] == "close_slice"
    assert row["task_ref"] == "WS-ERRTEL-01"
    assert row["harness"] == "mcp"
    assert row["occurrence_count"] == 1
    assert "XML-like tag" in row["summary"]


def test_successful_write_does_not_capture(isolated_runtime: RuntimeConfig) -> None:
    set_handoff_state = _wrapped_tool("set_handoff_state")
    result = set_handoff_state(task_ref="WS-ERRTEL-01", objective="self-capture test", status="in_progress")
    assert isinstance(result, dict) and result["ok"] is True

    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert rows == []


def test_non_write_tool_rejection_not_captured(isolated_runtime: RuntimeConfig) -> None:
    """ok:false from a tool without a write-contract registry row is not a write rejection."""

    mcp_server._maybe_capture_write_rejection(
        "get_handoff_state",
        {"ok": False, "tool": "get_handoff_state", "scope": {"task_ref": None}, "data": {"error": "nope"}},
    )
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert rows == []


# ---------------------------------------------------------------------------
# Dedup window
# ---------------------------------------------------------------------------


def test_duplicate_rejection_increments_occurrence_count(isolated_runtime: RuntimeConfig) -> None:
    close_slice = _wrapped_tool("close_slice")
    _rejected_close_slice(close_slice)
    _rejected_close_slice(close_slice)

    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["occurrence_count"] == 2
    assert row["last_seen_at"] >= row["created_at"]


def test_rejection_outside_window_inserts_new_row(isolated_runtime: RuntimeConfig) -> None:
    close_slice = _wrapped_tool("close_slice")
    _rejected_close_slice(close_slice)

    # Age the first capture past the 10-minute dedup window.
    with _get_db_connection() as conn:
        conn.execute("UPDATE agent_errors SET last_seen_at = datetime('now', '-11 minutes')")

    _rejected_close_slice(close_slice)

    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 2
    assert all(row["occurrence_count"] == 1 for row in rows)


def test_different_task_ref_is_separate_dedup_key(isolated_runtime: RuntimeConfig) -> None:
    agent_errors_module.capture_write_rejection(tool_name="close_slice", summary="same summary", task_ref="A-1")
    agent_errors_module.capture_write_rejection(tool_name="close_slice", summary="same summary", task_ref="B-2")
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Never-fail + recursion guard
# ---------------------------------------------------------------------------


def test_capture_failure_never_fails_original_response(
    isolated_runtime: RuntimeConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("self-capture exploded")

    monkeypatch.setattr(agent_errors_module, "_capture_write_rejection_unguarded", _boom)

    close_slice = _wrapped_tool("close_slice")
    result = _rejected_close_slice(close_slice)
    assert result["ok"] is False
    assert "rationale contains the XML-like tag" in result["data"]["error"]


def test_reentrant_capture_is_dropped(isolated_runtime: RuntimeConfig) -> None:
    calls: list[str] = []
    original = agent_errors_module._capture_write_rejection_unguarded

    def _recursing(**kwargs: object) -> None:
        calls.append(str(kwargs.get("summary")))
        # A capture that itself triggers capture must be dropped by the guard.
        agent_errors_module.capture_write_rejection(tool_name="inner", summary="recursive capture")
        original(**kwargs)

    agent_errors_module._capture_write_rejection_unguarded = _recursing  # type: ignore[assignment]
    try:
        agent_errors_module.capture_write_rejection(tool_name="outer", summary="outer capture")
    finally:
        agent_errors_module._capture_write_rejection_unguarded = original  # type: ignore[assignment]

    assert calls == ["outer capture"]
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    assert rows[0]["summary"] == "outer capture"


def test_rejected_error_event_write_is_captured_without_recursion(isolated_runtime: RuntimeConfig) -> None:
    """A rejected record_event(error) is itself a write rejection — captured once, no loop."""

    record_event = _wrapped_tool("record_event")
    result = record_event(
        event={
            "event_kind": "error",
            "error_class": "Not A Valid Class!",
            "summary": "bad class string",
        }
    )
    assert isinstance(result, dict) and result["ok"] is False

    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    assert rows[0]["error_class"] == "mcp_write_rejected"
    # Sub-op precision (REV-A-002): the wrapper resolves the event_kind
    # selector so implementation note clusters see the failing variant, not the
    # coarse multiplexed tool name.
    assert rows[0]["tool_name"] == "record_event.error"


# ---------------------------------------------------------------------------
# Read-only sub-operation scope (REV-A-001) + selector extraction (REV-A-002)
# ---------------------------------------------------------------------------


def test_read_only_sub_operation_rejection_not_captured(isolated_runtime: RuntimeConfig) -> None:
    """ok:false from a read sub-op of a multiplexed write tool is not a write rejection."""

    for tool_name, envelope_tool, sub_operation in (
        ("artifacts", "get_artifact", "get"),
        ("artifacts", "search_artifacts", "search"),
        ("review_findings", "list_review_findings", "list"),
        ("review_runs", "get_review_coverage", "coverage"),
        ("archive", "get_archived_task", "get"),
        ("terminal_guard_telemetry", "terminal_guard_telemetry", "list"),
        ("next_actions", "list_next_actions", "list"),
        ("compaction", "get_compaction", "get"),
        ("compaction", "get_latest_compaction", "get_latest"),
        ("compaction", "compaction", "status"),
    ):
        mcp_server._maybe_capture_write_rejection(
            tool_name,
            {"ok": False, "tool": envelope_tool, "scope": {"task_ref": None}, "data": {"error": "not found"}},
            sub_operation=sub_operation,
        )
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert rows == []


def test_read_only_tool_rejection_not_captured(isolated_runtime: RuntimeConfig) -> None:
    mcp_server._maybe_capture_write_rejection(
        "export_handoff_state",
        {"ok": False, "tool": "export_handoff_state", "scope": {"task_ref": None}, "data": {"error": "nope"}},
    )
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert rows == []


def test_write_sub_operation_rejection_still_captured(isolated_runtime: RuntimeConfig) -> None:
    """The read-only exclusion must not silence genuine write sub-op rejections."""

    mcp_server._maybe_capture_write_rejection(
        "review_findings",
        {
            "ok": False,
            "tool": "merge_review_findings",
            "scope": {"task_ref": "WS-ERRTEL-01"},
            "data": {"error": "source_task_refs must be non-empty"},
        },
        sub_operation="merge",
    )
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    # Envelope handler name preferred over the coarse registered name.
    assert rows[0]["tool_name"] == "merge_review_findings"
    assert rows[0]["task_ref"] == "WS-ERRTEL-01"


def test_extract_write_selector_resolves_payload_and_kwarg_shapes() -> None:
    extract = mcp_server._extract_write_selector
    assert extract("review_findings", (), {"review": {"operation": "list"}}) == "list"
    assert extract("record_event", (), {"event": {"event_kind": "error"}}) == "error"
    assert extract("artifacts", (), {"operation": "get"}) == "get"
    assert extract("archive", (), {"payload": {"operation": "get"}}) == "get"
    assert extract("close_slice", (), {"task_ref": "X-1"}) is None
    assert extract("not_a_registered_tool", (), {"operation": "get"}) is None


def test_extract_write_selector_resolves_basemodel_payloads() -> None:
    """FastMCP passes coerced Pydantic models at runtime — cover that branch (REV-C-003)."""
    from pydantic import BaseModel

    class _FakeOp(BaseModel):
        operation: str = "merge"

    class _FakeEvent(BaseModel):
        event_kind: str = "error"

    extract = mcp_server._extract_write_selector
    assert extract("review_findings", (_FakeOp(),), {}) == "merge"
    assert extract("review_findings", (), {"review": _FakeOp()}) == "merge"
    assert extract("record_event", (), {"event": _FakeEvent()}) == "error"


def test_compaction_write_rejection_is_captured() -> None:
    """The registered tool name is `compaction`; its write rejections must capture (REV-C-002)."""
    from workstate_handoff_mcp.write_contracts import READ_ONLY_OPERATIONS, get_write_contract

    contract = get_write_contract("compaction")
    assert contract is not None and contract.tool_name == "compaction"
    assert READ_ONLY_OPERATIONS["compaction"] == frozenset({"get", "get_latest", "status"})
    assert mcp_server._extract_write_selector("compaction", (), {"operation": "record"}) == "record"


def test_compaction_record_rejection_inserts_row(isolated_runtime: RuntimeConfig) -> None:
    mcp_server._maybe_capture_write_rejection(
        "compaction",
        {"ok": False, "tool": "compaction", "scope": {"task_ref": "X-1"}, "data": {"error": "bad transcript"}},
        sub_operation="record",
    )
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    assert rows[0]["tool_name"] == "compaction.record"
