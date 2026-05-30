"""WORKSTATE-REF-45 implementation note: compaction(operation=...) merge.

Asserts the merged ``compaction`` MCP tool dispatches to ``record``,
``get``, and ``get_latest`` operations with the same envelopes the
legacy ``compact_session``, ``get_compaction``, and
``get_latest_compaction`` tools produced, and that the legacy
registrations have been removed in favor of the consolidated entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from workstate_protocol import StructuredSummary

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def test_compaction_operation_record_returns_compaction_id(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    from workstate_handoff_mcp.api import compaction

    transcript_path = tmp_path / "transcript.md"
    transcript_path.write_text("Residual transcript for compaction.\n")

    result = compaction(
        {
            "operation": "record",
            "transcript_path": str(transcript_path),
            "task_ref": "WORKSTATE-REF-34",
            "harness": "codex",
            "session_id": "session-compact-1",
        }
    )

    assert isinstance(result, dict)
    assert result["compaction_id"] == "C-WORKSTATE-REF-34-0001"


def test_compaction_operation_record_accepts_cursor_harness_alias(
    isolated_runtime: RuntimeConfig, tmp_path: Path
) -> None:
    """WORKSTATE-REF-1-BR3-02: the public compaction API must accept the cursor alias.

    `compaction.compact_session` widens its `harness` parameter to
    `CompactionHarnessInput` (which includes "cursor") and normalizes to
    "vscode" via `_validate_harness`. The MCP-facing
    `CompactionRecordOp.harness` previously typed as the narrower
    `CompactionHarness` (no cursor), which made
    `compaction(operation="record", harness="cursor", ...)` raise a
    Pydantic validation error before normalization could run. Accepting
    the alias at the entry point is the documented compatibility
    contract.
    """
    from workstate_handoff_mcp.api import compaction, get_compaction

    transcript_path = tmp_path / "transcript.md"
    transcript_path.write_text("Residual transcript for cursor alias.\n")

    result = compaction(
        {
            "operation": "record",
            "transcript_path": str(transcript_path),
            "task_ref": "WORKSTATE-REF-60",
            "harness": "cursor",
            "session_id": "session-cursor-1",
        }
    )
    assert isinstance(result, dict)

    fetched = get_compaction(result["compaction_id"])
    assert isinstance(fetched, StructuredSummary)
    assert fetched.harness == "vscode", "The cursor alias must be canonicalized to vscode before storage."


def test_compaction_cli_choices_accept_cursor_alias() -> None:
    """WORKSTATE-REF-1-BR3-02: the CLI --harness flag must accept cursor too.

    The CLI registration must mirror the API's alias-acceptance so
    `workstate-handoff-mcp compaction --operation record --harness cursor ...`
    does not fail argparse validation before normalization.
    """
    from workstate_handoff_mcp.api import _build_tool_registry

    compaction_entry = next(entry for entry in _build_tool_registry() if entry.name == "compaction")
    harness_arg = next(arg for arg in compaction_entry.cli_args if arg.name == "--harness")
    assert "cursor" in (harness_arg.choices or ()), (
        "--harness CLI choices must include 'cursor' so the alias is "
        "accepted at the argparse layer before normalization."
    )


def test_compaction_operation_get_returns_structured_summary(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    from workstate_handoff_mcp.api import compaction

    transcript_path = tmp_path / "transcript.md"
    transcript_path.write_text("Residual transcript content.\n")
    record_receipt = compaction(
        {
            "operation": "record",
            "transcript_path": str(transcript_path),
            "task_ref": "WORKSTATE-REF-34",
            "harness": "codex",
            "session_id": "session-compact-1",
        }
    )
    compaction_id = record_receipt["compaction_id"]

    fetched = compaction({"operation": "get", "compaction_id": compaction_id})

    assert isinstance(fetched, StructuredSummary)
    assert fetched.compaction_id == compaction_id
    assert fetched.session_id == "session-compact-1"
    assert fetched.task_ref == "WORKSTATE-REF-34"


def test_compaction_operation_get_latest_returns_newest(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    from workstate_handoff_mcp.api import compaction

    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first\n")
    second.write_text("second\n")

    compaction(
        {
            "operation": "record",
            "transcript_path": str(first),
            "task_ref": "WORKSTATE-REF-34",
            "harness": "codex",
            "session_id": "session-compact-1",
        }
    )
    second_receipt = compaction(
        {
            "operation": "record",
            "transcript_path": str(second),
            "task_ref": "WORKSTATE-REF-34",
            "harness": "codex",
            "session_id": "session-compact-2",
        }
    )

    latest = compaction({"operation": "get_latest", "task_ref": "WORKSTATE-REF-34"})

    assert isinstance(latest, StructuredSummary)
    assert latest.compaction_id == second_receipt["compaction_id"]


def test_compaction_operation_get_latest_returns_none_when_absent(
    isolated_runtime: RuntimeConfig,
) -> None:
    from workstate_handoff_mcp.api import compaction

    assert compaction({"operation": "get_latest", "task_ref": "WORKSTATE-REF-34"}) is None


def test_registry_replaces_compaction_split_with_consolidated_tool() -> None:
    from workstate_handoff_mcp.api import _build_tool_registry

    registry = _build_tool_registry()
    names = {entry.name for entry in registry}

    assert "compaction" in names, "consolidated compaction tool must be registered"
    assert "compact_session" not in names, (
        "legacy compact_session must be removed in favor of compaction(operation='record')"
    )
    assert "get_compaction" not in names, (
        "legacy get_compaction must be removed in favor of compaction(operation='get')"
    )
    assert "get_latest_compaction" not in names, (
        "legacy get_latest_compaction must be removed in favor of compaction(operation='get_latest')"
    )


def test_expected_handoff_tool_count_decremented_for_slice_2() -> None:
    """implementation note dropped the count by exactly 2 (compact_session + get_compaction + get_latest_compaction -> compaction).

    The live invariant is asserted in the per-slice test file for the most recent
    slice, so this test only enforces a ceiling of 27 (the post-slice-2 value).
    """
    from workstate_handoff_mcp.invariants import EXPECTED_HANDOFF_TOOL_COUNT

    assert EXPECTED_HANDOFF_TOOL_COUNT <= 27
