"""implementation note implementation note — BR-04 published-registry surface.

The write-contract registry must be reachable through real MCP
surfaces, not just an in-process import. Two callers need it:

1. ``limits.write`` envelope — agents reading ``get_handoff_state``
   should see the registry in the same identity payload that already
   advertises slice-complete grammar.
2. A side-effect-free preflight tool ``validate_write`` — mirrors
   ``validate_decision_id`` so callers can preflight an arbitrary MCP
   write payload without bouncing off the mutating path first.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


@pytest.fixture()
def isolated_handoff(tmp_path: Path) -> Path:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.txt",
        current_task_auto_regen=True,
    )
    mcp_server.configure_runtime(runtime)
    return tmp_path


def _parse(payload: str | dict) -> dict:
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        data = raw.get("data", {}) or {}
        return {**raw, **data}
    return raw


def test_get_handoff_state_limits_write_publishes_registry(isolated_handoff: Path) -> None:
    """``limits.write.tools`` must include the registry export."""
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-DEMO",
        objective="demo",
        status="in_progress",
    )
    envelope = _parse(mcp_server.get_handoff_state(task_ref="WORKSTATE-REF-DEMO", sections="identity"))
    write_limits = envelope["limits"]["write"]
    tools = write_limits.get("tools")
    assert isinstance(tools, dict)
    assert "review_findings" in tools
    assert "review_runs" in tools
    review_findings_row = tools["review_findings"]
    assert "variants" in review_findings_row
    assert "record" in review_findings_row["variants"]
    record_required = set(review_findings_row["variants"]["record"]["required"])
    assert {"session", "finding_id", "severity", "file_path", "description"} <= record_required


def test_validate_write_preflight_tool_is_registered() -> None:
    """``validate_write`` must be a callable function on the api module."""
    assert hasattr(mcp_server, "validate_write")
    fn = mcp_server.validate_write
    assert callable(fn)


def test_validate_write_returns_envelope_for_valid_payload() -> None:
    envelope = _parse(
        mcp_server.validate_write(
            tool_name="review_findings",
            payload={
                "operation": "record",
                "session": "session-1",
                "finding_id": "WORKSTATE-demo-001",
                "severity": "high",
                "file_path": "packages/foo/bar.py",
                "description": "demo finding",
            },
        )
    )
    assert envelope["ok"] is True
    assert envelope.get("tool") == "validate"
    assert envelope["errors"] == []
    assert envelope["variant_selected"] == "record"


def test_validate_write_returns_envelope_for_invalid_payload() -> None:
    envelope = _parse(
        mcp_server.validate_write(
            tool_name="review_findings",
            payload={
                "operation": "record",
                "session": "session-1",
                "finding_id": "WORKSTATE-demo-001",
                "severity": "blocker",
                "file_path": "packages/foo/bar.py",
                "description": "demo finding",
            },
        )
    )
    assert envelope["ok"] is False
    assert any("severity" in err for err in envelope["errors"])


def test_validate_write_unknown_tool_returns_descriptive_error() -> None:
    envelope = _parse(
        mcp_server.validate_write(
            tool_name="totally_made_up_tool",
            payload={},
        )
    )
    assert envelope["ok"] is False
    assert any("registry" in err.lower() for err in envelope["errors"])
