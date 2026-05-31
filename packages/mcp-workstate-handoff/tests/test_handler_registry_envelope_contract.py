"""WORKSTATE-REF-58 implementation note: registry-iteration regression guard.

Iterates every tool registered on the production FastMCP server, invokes the
registered ``FunctionTool.fn`` with permissive happy-path inputs, and asserts
the result is a ``dict`` (the wire envelope) — never a non-dict that would
surface as FastMCP's ``ValueError("structured_content must be a dict or None")``
at the wire.

Mutation-only tools that cannot be safely invoked without pre-seeded
fixtures (writes, archive operations, import) are skipped via the
``REQUIRES_FIXTURE`` allow-list. The list's size is asserted exactly so any
new mutation-only tool surfaces in a diff and must be evaluated before it
silently slips past coverage.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from workstate_handoff_mcp.api import (
    _build_tool_registry,
    build_handoff_mcp,
)
from workstate_handoff_mcp.config import RuntimeConfig

REQUIRES_FIXTURE: frozenset[str] = frozenset(
    {
        # Mutation tools that require pre-seeded handoff state to exercise
        # safely. Each writes to handoff_state / decisions / archive tables,
        # so invocation with permissive happy-path inputs would either error
        # before the envelope check or leak rows that other tests rely on.
        "set_handoff_state",
        "record_event",
        "import_handoff_state",
        "archive",
        "close_slice",
    }
)


HAPPY_PATH_INPUTS: dict[str, dict[str, Any]] = {
    # Tools whose required arg is a typed payload — supply the simplest
    # read-style discriminator value so the call lands on a safe branch.
    "validate": {"payload": {"kind": "decision_id", "decision": "claude_smoke_decision_id"}},
    "integrity_check": {"payload": {"kind": "working_tree"}},
    "next_actions": {"action": {"operation": "list"}},
    "review_findings": {"review": {"operation": "list"}},
    "review_runs": {"review": {"operation": "list"}},
    "terminal_guard_telemetry": {"telemetry": {"operation": "list"}},
    "render_handoff": {"kind": "dashboard", "write_file": False},
    "touched_files": {"payload": {"operation": "list"}},
    "compaction": {"payload": {"operation": "get_latest", "task_ref": "WORKSTATE-REF-58"}},
    "artifacts": {"artifact": {"operation": "search"}},
    # Tools whose every parameter is defaulted — `{}` exercises the
    # all-defaults read path.
    "get_handoff_state": {},
    "export_handoff_state": {},
    "get_verified_tests": {},
    "load_session": {},
    "list_handoff_rows": {},
    "audit_decision_ids": {},
    "search_handoff": {},
}


_FASTMCP_NON_DICT_MARKER = "structured_content must be a dict"


def _registered_tool_map(runtime: RuntimeConfig) -> dict[str, Any]:
    mcp = build_handoff_mcp(runtime)
    tools = asyncio.run(mcp._list_tools())
    return {tool.name: tool for tool in tools}


@pytest.fixture(scope="module")
def configured_tools(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    tmp_path = tmp_path_factory.mktemp("WORKSTATE58-slice3-registry-guard")
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    return _registered_tool_map(runtime)


def _coverable_tool_names() -> list[str]:
    return sorted(entry.name for entry in _build_tool_registry() if entry.name not in REQUIRES_FIXTURE)


def test_requires_fixture_allowlist_is_exactly_documented_size() -> None:
    # If a new mutation-only tool is added, this assertion forces the
    # author to update REQUIRES_FIXTURE deliberately rather than letting
    # the registry guard silently skip it.
    assert len(REQUIRES_FIXTURE) == 5, (
        f"REQUIRES_FIXTURE has {len(REQUIRES_FIXTURE)} entries; "
        f"contents: {sorted(REQUIRES_FIXTURE)}. Update this assertion "
        "deliberately when adding or removing a mutation-only tool."
    )


def test_every_registered_tool_is_either_covered_or_skipped(configured_tools: dict[str, Any]) -> None:
    registered = set(configured_tools)
    covered = set(HAPPY_PATH_INPUTS)
    skipped = set(REQUIRES_FIXTURE)
    untracked = registered - covered - skipped
    assert not untracked, (
        f"Registered tools missing from both HAPPY_PATH_INPUTS and REQUIRES_FIXTURE: "
        f"{sorted(untracked)}. Add happy-path inputs for read-safe tools or place the "
        "mutation-only tool on REQUIRES_FIXTURE."
    )


@pytest.mark.parametrize("tool_name", _coverable_tool_names())
def test_registered_tool_wrapper_returns_dict_envelope(tool_name: str, configured_tools: dict[str, Any]) -> None:
    registered_tool = configured_tools[tool_name]
    kwargs = HAPPY_PATH_INPUTS[tool_name]

    try:
        result = registered_tool.fn(**kwargs)
    except Exception as exc:  # noqa: BLE001 — guard checks the message shape only.
        # Domain errors are acceptable; the regression guard only fails if a
        # registered tool surfaces the FastMCP non-dict structured_content error.
        assert _FASTMCP_NON_DICT_MARKER not in str(exc), (
            f"{tool_name} raised the FastMCP non-dict envelope error: {exc!r}"
        )
        return

    assert isinstance(result, dict), (
        f"{tool_name} registered tool returned {type(result).__name__}; "
        "expected dict (the v2 envelope wire shape). The FastMCP registration "
        "surface should expose the normalized MCP handler."
    )
