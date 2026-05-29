"""WORKSTATE-REF-58 implementation note: FastMCP wire-parity test for envelope normalisation.

Proves that the registered tool objects installed by ``build_handoff_mcp(...)``
for ``list_handoff_rows`` and ``compaction`` expose a v2 dict envelope on the
MCP wire, where the prior shape errored with ``structured_content must be a
dict or None``. Adds synthetic stub-handler coverage for each non-dict
normalisation branch (``list``, ``str``, ``None``) plus the defensive
unsupported-type ``TypeError``.

The server fixture resolves the production ``FunctionTool`` instances from the
FastMCP registry and invokes their ``.fn`` callables, so a regression at the
``mcp.add_tool(...)`` registration site is visible to this test.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from workstate_handoff_mcp import core
from workstate_handoff_mcp.api import (
    ToolEntry,
    _wrap_branch_mismatch_for_mcp,
    build_handoff_mcp,
)
from workstate_handoff_mcp.config import RuntimeConfig


def _registered_tool_map(mcp: object) -> dict[str, Any]:
    tools = asyncio.run(mcp._list_tools())
    return {tool.name: tool for tool in tools}


@pytest.fixture()
def configured_server(tmp_path: Path):
    """Build the production FastMCP server against an isolated workspace."""

    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp = build_handoff_mcp(runtime)
    return mcp, _registered_tool_map(mcp)


def test_list_handoff_rows_wire_payload_is_dict_envelope(configured_server) -> None:
    _, registered_tools = configured_server

    result = registered_tools["list_handoff_rows"].fn()

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["schema_version"] == 2
    assert result["tool"] == "list_handoff_rows"
    assert isinstance(result["data"], dict)
    assert result["data"] == {"rows": []}


def test_compaction_get_latest_wire_payload_is_dict_envelope(configured_server) -> None:
    _, registered_tools = configured_server

    result = registered_tools["compaction"].fn(payload={"operation": "get_latest", "task_ref": "WORKSTATE-REF-58"})

    assert isinstance(result, dict)
    assert result["ok"] is True
    assert result["schema_version"] == 2
    assert result["tool"] == "compaction"
    assert result["data"] == {"result": None}


@pytest.mark.parametrize(
    ("native_result", "expected_data"),
    [
        (
            [{"task_ref": "WORKSTATE-REF-58"}, {"task_ref": "WORKSTATE-REF-7"}],
            {"rows": [{"task_ref": "WORKSTATE-REF-58"}, {"task_ref": "WORKSTATE-REF-7"}]},
        ),
        ("C-WORKSTATE-REF-58-0001", {"result": "C-WORKSTATE-REF-58-0001"}),
        (None, {"result": None}),
    ],
    ids=["list_branch", "str_branch", "none_branch"],
)
def test_synthetic_stub_handler_envelopes_non_dict_returns(native_result: Any, expected_data: dict[str, Any]) -> None:
    def handler() -> Any:
        return native_result

    entry = ToolEntry(name="synthetic_tool", handler=handler, description="synthetic")
    wrapped = _wrap_branch_mismatch_for_mcp(entry)

    assert wrapped() == core._envelope(ok=True, tool="synthetic_tool", data=expected_data)


def test_synthetic_stub_handler_rejects_unsupported_return_type() -> None:
    def handler() -> Any:
        return 3.14

    entry = ToolEntry(name="bad_tool", handler=handler, description="synthetic")
    wrapped = _wrap_branch_mismatch_for_mcp(entry)

    with pytest.raises(TypeError, match="bad_tool"):
        wrapped()


def test_configured_server_actually_registers_offenders(configured_server) -> None:
    _, registered_tools = configured_server

    assert "list_handoff_rows" in registered_tools
    assert "compaction" in registered_tools
    assert callable(registered_tools["list_handoff_rows"].fn)
    assert callable(registered_tools["compaction"].fn)
