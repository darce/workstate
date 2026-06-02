"""WORKSTATE-REF-45 implementation note: archive(operation=...) merge.

Asserts the merged ``archive`` MCP tool dispatches to ``archive``, ``gc``,
and ``get`` operations with the same envelopes the legacy
``archive_task_state``, ``tasks_gc``, and ``get_archived_task`` tools
produced, and that the legacy registrations have been removed in favor
of the consolidated entry. This slice overrides ADR-005's
``archive_task_state`` carve-out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


def _parse(payload: str | dict) -> dict:
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        data = raw.get("data", {})
        scope = raw.get("scope", {})
        flat = {**raw, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return raw


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=tmp_path / ".task-state",
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return tmp_path


def test_archive_operation_archive_returns_legacy_envelope(workspace: Path) -> None:
    from workstate_handoff_mcp.api import archive

    _parse(
        mcp_server.set_handoff_state(
            task_ref="archive-merge-task",
            objective="exercise archive operation",
            status="in_progress",
        )
    )

    envelope = archive({"operation": "archive", "task_ref": "archive-merge-task"})

    assert envelope["tool"] == "archive"
    assert envelope["ok"] is True
    flat = _parse(envelope)
    assert flat["task_ref"] == "archive-merge-task"


def test_archive_operation_gc_dry_run_returns_envelope(workspace: Path) -> None:
    from workstate_handoff_mcp.api import archive

    envelope = archive({"operation": "gc"})

    assert envelope["tool"] == "archive"
    assert envelope["ok"] is True
    flat = _parse(envelope)
    assert flat["applied"] is False
    assert flat["archived"] == []
    assert flat["would_archive"] == []


def test_archive_operation_get_returns_archived_metadata(workspace: Path) -> None:
    from workstate_handoff_mcp.api import archive

    _parse(
        mcp_server.set_handoff_state(
            task_ref="archive-get-task",
            objective="round-trip via consolidated archive tool",
            status="in_progress",
        )
    )
    archive({"operation": "archive", "task_ref": "archive-get-task"})

    envelope = archive({"operation": "get", "task_ref": "archive-get-task"})

    assert envelope["tool"] == "archive"
    flat = _parse(envelope)
    assert flat["ok"] is True
    assert flat["archive"]["task_ref"] == "archive-get-task"


def test_archive_operation_get_returns_structured_error_when_missing(workspace: Path) -> None:
    from workstate_handoff_mcp.api import archive

    envelope = archive({"operation": "get", "task_ref": "never-archived"})

    assert envelope["tool"] == "archive"
    flat = _parse(envelope)
    assert flat["ok"] is False
    assert "No archived task found" in flat["error"]


def test_archive_operation_get_omits_snapshot_when_include_snapshot_false(workspace: Path) -> None:
    from workstate_handoff_mcp.api import archive

    _parse(
        mcp_server.set_handoff_state(
            task_ref="archive-meta-only",
            objective="metadata-only fetch",
            status="in_progress",
        )
    )
    archive({"operation": "archive", "task_ref": "archive-meta-only"})

    envelope = archive(
        {
            "operation": "get",
            "task_ref": "archive-meta-only",
            "include_snapshot": False,
        }
    )

    assert envelope["tool"] == "archive"
    flat = _parse(envelope)
    assert flat["ok"] is True
    assert "snapshot" not in flat
    assert flat["archive"]["task_ref"] == "archive-meta-only"


def test_registry_replaces_archive_split_with_consolidated_tool() -> None:
    from workstate_handoff_mcp.api import _build_tool_registry

    registry = _build_tool_registry()
    names = {entry.name for entry in registry}

    assert "archive" in names, "consolidated archive tool must be registered"
    assert "archive_task_state" not in names, (
        "legacy archive_task_state must be removed in favor of archive(operation='archive')"
    )
    assert "tasks_gc" not in names, "legacy tasks_gc must be removed in favor of archive(operation='gc')"
    assert "get_archived_task" not in names, (
        "legacy get_archived_task must be removed in favor of archive(operation='get')"
    )


def test_expected_handoff_tool_count_decremented_for_slice_5() -> None:
    from workstate_handoff_mcp.invariants import EXPECTED_HANDOFF_TOOL_COUNT

    assert EXPECTED_HANDOFF_TOOL_COUNT <= 22


def test_archive_workspace_summary_terminal_flush(workspace: Path) -> None:
    """WORKSTATE-REF-54-FU implementation note: archive must flush CURRENT_TASK.json unconditionally.

    After archive returns, the on-disk workspace-summary file must exist
    and must not reference the archived task. Mirrors decisions.py:758
    (close_check unconditional flush). Without the flush, legacy file
    consumers see a stale summary and trip `task_ref_ambiguous` on the
    next `make task-start`.
    """
    from workstate_handoff_mcp.api import archive

    _parse(
        mcp_server.set_handoff_state(
            task_ref="ARCHIVE-FLUSH-TARGET",
            objective="task to archive",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="ARCHIVE-FLUSH-NEIGHBOR",
            objective="unrelated co-resident task",
            status="in_progress",
        )
    )

    envelope = _parse(archive({"operation": "archive", "task_ref": "ARCHIVE-FLUSH-TARGET"}))
    assert envelope["ok"] is True

    current_task_path = workspace / "CURRENT_TASK.json"
    assert current_task_path.exists(), (
        "terminal archive must flush CURRENT_TASK.json unconditionally so "
        "legacy file readers see the post-archive state without a separate "
        "render_handoff call"
    )

    on_disk = json.loads(current_task_path.read_text(encoding="utf-8"))
    assert on_disk.get("schema_version") == 2
    # Neighbor task survives; archived task is absent from the summary.
    flattened_refs: list[str] = []
    if on_disk.get("shape") == "single":
        ref = on_disk.get("task_ref")
        if isinstance(ref, str):
            flattened_refs.append(ref)
    elif on_disk.get("shape") == "workspace_ambiguous":
        for entry in on_disk.get("tasks", []):
            ref = entry.get("task_ref") if isinstance(entry, dict) else None
            if isinstance(ref, str):
                flattened_refs.append(ref)
    assert "ARCHIVE-FLUSH-TARGET" not in flattened_refs, (
        "archived task must not appear in workspace-summary tasks[] post-archive — found stale entry"
    )
    assert "ARCHIVE-FLUSH-NEIGHBOR" in flattened_refs, "unrelated live task must survive the archive flush"
