"""WORKSTATE-REF-54 implementation note: compatibility reader for workspace-summary
``CURRENT_TASK.json`` payloads.

Covers the contract that lets implementation note migrate readers (lifecycle CLI,
compact-session hook) one at a time while the live writer still emits
the legacy ``schema_version: 1`` shape:

- v1 ``active``-populated payload → ``shape="single"``.
- v1 with ``active: null`` → ``shape="none"``.
- v2 ``single`` / ``workspace_ambiguous`` / ``none`` payloads round-trip
  through ``shape`` accessors without lossy translation.
- Missing file → ``shape="none"`` (lifecycle readers must treat absence
  the same as "no active task").
- Corrupt JSON → typed error so the caller can decide between hard-stop
  vs. degrade-with-warning. No silent fallback to "none".

This test module is intentionally non-live: it exercises
``load_workspace_summary_compat`` directly and does NOT depend on the
MCP server, the per-task projection writer, or any live
``CURRENT_TASK.json`` produced by the running system. implementation note wires
this helper into the actual readers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# _common.py does ``import resolver`` (a flat import that resolves
# against the lifecycle package directory, not the agentic namespace
# package). Mirror the sys.path setup that test_resolver.py uses so
# the import chain succeeds without an editable install.
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_LIFECYCLE_PKG = _PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"
if str(_LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(_LIFECYCLE_PKG))

from workstate.lifecycle.handlers._common import (  # noqa: WORKSTATE-REF-402
    WorkspaceSummaryView,
    WorkspaceSummaryParseError,
    load_workspace_summary_compat,
)


def _write(path: Path, payload: dict) -> Path:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2))
    return path


def test_compat_reader_v1_active_populated_returns_single(tmp_path: Path) -> None:
    """Legacy v1 payload with a populated ``active`` block → ``single``.

    The v1 ``active`` block is field-equivalent to the
    ``task_projection_schema_version=1`` per-task payload (the renderer
    that writes v1 ``active`` is the same source-of-truth shape used by
    the per-task projection writer; see plan ``implementation note → Changes``), so
    the compat reader passes it through as ``view.active`` without
    content transformation.
    """
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 1,
            "surface": "current_task",
            "task_ref": "WORKSTATE-REF-54",
            "active": {
                "task_ref": "WORKSTATE-REF-54",
                "status": "in_progress",
                "objective": "ship the compat reader",
                "focus": "implementation note",
                "target_branch": "feature/WORKSTATE-54",
                "target_worktree_path": "/tmp/wt",
                "task_plan_path": "packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54.md",
                "revision": 7,
                "updated_at": "2026-05-10T04:00:00",
            },
            "blockers_open": [],
        },
    )

    view = load_workspace_summary_compat(path)

    assert isinstance(view, WorkspaceSummaryView)
    assert view.shape == "single"
    assert view.source_schema_version == 1
    assert view.task_ref == "WORKSTATE-REF-54"
    assert view.active is not None
    assert view.active["task_ref"] == "WORKSTATE-REF-54"
    assert view.active["status"] == "in_progress"
    assert view.active["objective"] == "ship the compat reader"
    assert view.tasks == []


def test_compat_reader_v1_active_null_returns_none(tmp_path: Path) -> None:
    """Legacy v1 payload with ``active: null`` → ``shape="none"``.

    A live workspace with no active task currently writes this payload
    today (see ``CURRENT_TASK.json`` at repo root with
    ``"active": null``); migrated readers must collapse it to the same
    ``none`` shape they get from a v2 payload, otherwise reader logic
    has to special-case the schema_version it received.
    """
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 1,
            "surface": "current_task",
            "task_ref": None,
            "active": None,
            "blockers_open": [],
        },
    )

    view = load_workspace_summary_compat(path)

    assert view.shape == "none"
    assert view.source_schema_version == 1
    assert view.task_ref is None
    assert view.active is None
    assert view.tasks == []


def test_compat_reader_v2_none_round_trips(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {"schema_version": 2, "shape": "none"},
    )

    view = load_workspace_summary_compat(path)

    assert view.shape == "none"
    assert view.source_schema_version == 2
    assert view.task_ref is None
    assert view.active is None
    assert view.tasks == []


def test_compat_reader_v2_single_round_trips(tmp_path: Path) -> None:
    active_payload = {
        "task_projection_schema_version": 1,
        "task_ref": "WORKSTATE-REF-54",
        "status": "in_progress",
        "objective": "ship the compat reader",
        "focus": "implementation note",
        "target_branch": "feature/WORKSTATE-54",
        "target_worktree_path": "/tmp/wt",
        "task_plan_path": "packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54.md",
        "revision": 7,
        "updated_at": "2026-05-10T04:00:00",
    }
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 2,
            "shape": "single",
            "task_ref": "WORKSTATE-REF-54",
            "active": active_payload,
        },
    )

    view = load_workspace_summary_compat(path)

    assert view.shape == "single"
    assert view.source_schema_version == 2
    assert view.task_ref == "WORKSTATE-REF-54"
    assert view.active == active_payload
    assert view.tasks == []


def test_compat_reader_v2_workspace_ambiguous_round_trips(tmp_path: Path) -> None:
    tasks_payload = [
        {
            "task_projection_schema_version": 1,
            "task_ref": "WORKSTATE-REF-54",
            "status": "in_progress",
            "objective": "task A",
            "focus": None,
            "target_branch": None,
            "target_worktree_path": None,
            "task_plan_path": None,
            "revision": 1,
            "updated_at": "2026-05-10T04:00:00",
        },
        {
            "task_projection_schema_version": 1,
            "task_ref": "WORKSTATE-REF-PLAN-20260510",
            "status": "in_progress",
            "objective": "task B",
            "focus": None,
            "target_branch": "main",
            "target_worktree_path": None,
            "task_plan_path": None,
            "revision": 1,
            "updated_at": "2026-05-10T04:00:00",
        },
    ]
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 2,
            "shape": "workspace_ambiguous",
            "tasks": tasks_payload,
        },
    )

    view = load_workspace_summary_compat(path)

    assert view.shape == "workspace_ambiguous"
    assert view.source_schema_version == 2
    assert view.task_ref is None
    assert view.active is None
    assert view.tasks == tasks_payload


def test_compat_reader_missing_file_returns_none(tmp_path: Path) -> None:
    """Reader callers must treat a missing ``CURRENT_TASK.json`` as
    ``shape="none"``. A bootstrapped workspace before any
    ``set_handoff_state`` call has no file at all; that is the same
    semantic as "no active task" and the reader must not blow up."""
    path = tmp_path / "does-not-exist.json"
    assert not path.exists()

    view = load_workspace_summary_compat(path)

    assert view.shape == "none"
    assert view.source_schema_version is None
    assert view.task_ref is None


def test_compat_reader_corrupt_json_raises_typed_error(tmp_path: Path) -> None:
    """Corrupt JSON must NOT silently degrade to ``none``. A
    half-written file is operator-actionable (e.g. disk-full crash
    mid-write); collapsing it to ``none`` would mask the corruption
    and let a reader pretend there is no active task when there is."""
    path = tmp_path / "CURRENT_TASK.json"
    path.write_text("{not valid json")

    with pytest.raises(WorkspaceSummaryParseError):
        load_workspace_summary_compat(path)


def test_compat_reader_v2_single_missing_active_raises(tmp_path: Path) -> None:
    """WORKSTATE-REF-54-BR-02: a v2 ``single`` payload with no ``active`` block
    (or a non-dict ``active``) is malformed — the shape claim says
    ``single`` but the data does not back it. The reader must NOT
    return a usable ``shape="single"`` view, otherwise downstream
    readers (``task_finish``, ``shell_out``) trust the top-level
    ``task_ref`` and resolve to a task with no active projection."""
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {"schema_version": 2, "shape": "single", "task_ref": "WORKSTATE-REF-54"},
    )
    with pytest.raises(WorkspaceSummaryParseError):
        load_workspace_summary_compat(path)


def test_compat_reader_v2_single_non_dict_active_raises(tmp_path: Path) -> None:
    """WORKSTATE-REF-54-BR-02: ``active`` must be a dict for ``shape='single'``."""
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 2,
            "shape": "single",
            "task_ref": "WORKSTATE-REF-54",
            "active": "not-a-dict",
        },
    )
    with pytest.raises(WorkspaceSummaryParseError):
        load_workspace_summary_compat(path)


def test_compat_reader_v2_single_empty_task_ref_raises(tmp_path: Path) -> None:
    """WORKSTATE-REF-54-BR-02: empty/missing top-level ``task_ref`` invalidates
    a ``single`` shape — the projection cannot identify the task."""
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 2,
            "shape": "single",
            "task_ref": "",
            "active": {"task_ref": "WORKSTATE-REF-54", "status": "in_progress"},
        },
    )
    with pytest.raises(WorkspaceSummaryParseError):
        load_workspace_summary_compat(path)


def test_compat_reader_v2_single_task_ref_mismatch_raises(tmp_path: Path) -> None:
    """WORKSTATE-REF-54-BR-02: the top-level ``task_ref`` and ``active.task_ref``
    must agree. A mismatch indicates a corrupt projection — readers
    must not pick one and silently proceed."""
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 2,
            "shape": "single",
            "task_ref": "WORKSTATE-REF-54",
            "active": {"task_ref": "WORKSTATE-REF-OTHER", "status": "in_progress"},
        },
    )
    with pytest.raises(WorkspaceSummaryParseError):
        load_workspace_summary_compat(path)


def test_compat_reader_v2_workspace_ambiguous_missing_tasks_raises(tmp_path: Path) -> None:
    """WORKSTATE-REF-54-BR-02: ``workspace_ambiguous`` without a ``tasks`` key
    is malformed — the ambiguity surface is the candidate list, and an
    empty filter is not equivalent to "no candidates declared"."""
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {"schema_version": 2, "shape": "workspace_ambiguous"},
    )
    with pytest.raises(WorkspaceSummaryParseError):
        load_workspace_summary_compat(path)


def test_compat_reader_v2_workspace_ambiguous_non_list_tasks_raises(tmp_path: Path) -> None:
    """WORKSTATE-REF-54-BR-02: ``tasks`` must be a list for ``workspace_ambiguous``."""
    path = _write(
        tmp_path / "CURRENT_TASK.json",
        {
            "schema_version": 2,
            "shape": "workspace_ambiguous",
            "tasks": {"not": "a list"},
        },
    )
    with pytest.raises(WorkspaceSummaryParseError):
        load_workspace_summary_compat(path)


def test_compat_reader_accepts_parsed_payload_directly(tmp_path: Path) -> None:
    """Per the plan: ``Inputs: a parsed CURRENT_TASK.json payload OR
    the file path``. Callers that already parsed JSON elsewhere (e.g.
    the lifecycle resolver receives a dict from a subprocess) must
    not have to round-trip through disk."""
    payload = {
        "schema_version": 2,
        "shape": "single",
        "task_ref": "WORKSTATE-REF-54",
        "active": {"task_ref": "WORKSTATE-REF-54", "status": "in_progress"},
    }

    view = load_workspace_summary_compat(payload)

    assert view.shape == "single"
    assert view.source_schema_version == 2
    assert view.task_ref == "WORKSTATE-REF-54"
