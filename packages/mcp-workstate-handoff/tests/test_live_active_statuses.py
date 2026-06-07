"""WORKSTATE-REF-41 implementation note: LIVE_ACTIVE_STATUSES narrowing.

Closes the empirical regression where ``archive_task_state(active_cleared=True)``
was silently reversed by the next ``render_handoff(kind='current_task')`` call:
the renderer re-promoted the next stale ``status=done`` row into
``CURRENT_TASK.json.active``, and ``make task-start`` then failed with
``task_ref_ambiguous``.

The fix narrows what counts as "active" for resolver/renderer purposes from
"non-archived" to ``status in LIVE_ACTIVE_STATUSES``.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import RuntimeConfig, configure_runtime
from workstate_handoff_mcp.shared_primitives import (
    LIVE_ACTIVE_STATUSES,
    _resolve_workspace_handoff_row,
)
from workstate_handoff_mcp.shared_write_context import AmbiguousWorkspaceContextError


def _configured_conn(tmp_path: Path) -> sqlite3.Connection:
    configure_runtime(RuntimeConfig.for_repo(tmp_path))
    from workstate_handoff_mcp.shared_schema import _open_db_connection

    return _open_db_connection()


def _insert_row(
    conn: sqlite3.Connection,
    *,
    task_ref: str,
    status: str = "in_progress",
    target_worktree_path: str | None = None,
    target_branch: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO handoff_state (
            task_ref, objective, focus, status, target_branch,
            target_worktree_path, revision, updated_at, updated_by,
            updated_branch, updated_commit_sha
        ) VALUES (?, ?, ?, ?, ?, ?, 0,
                  datetime('now'), 'tester', 'main', 'abc123')
        """,
        (task_ref, f"obj-{task_ref}", f"focus-{task_ref}", status, target_branch, target_worktree_path),
    )


def test_live_active_statuses_is_narrow_subset_of_handoff_active_statuses() -> None:
    from workstate_handoff_mcp.shared_primitives import HANDOFF_ACTIVE_STATUSES

    assert tuple(LIVE_ACTIVE_STATUSES) == ("in_progress", "review", "blocked")
    assert set(LIVE_ACTIVE_STATUSES).issubset(HANDOFF_ACTIVE_STATUSES)
    assert "done" not in LIVE_ACTIVE_STATUSES


def test_resolver_skips_done_rows_when_no_live_row_matches(tmp_path: Path) -> None:
    """A workspace with only ``status=done`` rows resolves to None, not a stale row."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="STALE-A", status="done", target_branch="main")
        _insert_row(conn, task_ref="STALE-B", status="done", target_branch="main")
        conn.commit()

        result = _resolve_workspace_handoff_row(conn)
        assert result is None
    finally:
        conn.close()


def test_resolver_returns_live_row_even_when_done_rows_exist(tmp_path: Path) -> None:
    """A live ``in_progress`` row wins over coexisting ``status=done`` rows."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="STALE-DONE", status="done", target_branch="main")
        _insert_row(conn, task_ref="LIVE-WORK", status="in_progress", target_branch="main")
        conn.commit()

        result = _resolve_workspace_handoff_row(conn)
        assert result is not None
        assert result["task_ref"] == "LIVE-WORK"
    finally:
        conn.close()


def test_resolver_still_rejects_ambiguous_live_rows(tmp_path: Path) -> None:
    """Two live rows on the same workspace path still raise ambiguity."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="LIVE-A", status="in_progress", target_branch="main")
        _insert_row(conn, task_ref="LIVE-B", status="review", target_branch="main")
        conn.commit()

        with pytest.raises(AmbiguousWorkspaceContextError):
            _resolve_workspace_handoff_row(conn)
    finally:
        conn.close()


def test_list_handoff_rows_returns_all_non_archived_rows(tmp_path: Path) -> None:
    """list_handoff_rows() (no filter) enumerates every live row regardless of status."""
    from workstate_handoff_mcp import api as mcp_server

    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="LIVE", status="in_progress", target_branch="main")
        _insert_row(conn, task_ref="DONE", status="done", target_branch="main")
        conn.commit()
    finally:
        conn.close()

    rows = mcp_server.list_handoff_rows()
    refs = sorted(row["task_ref"] for row in rows)
    assert refs == ["DONE", "LIVE"]
    sample = next(row for row in rows if row["task_ref"] == "LIVE")
    assert {
        "task_ref",
        "status",
        "target_branch",
        "target_worktree_path",
        "task_plan_path",
        "updated_at",
        "revision",
    }.issubset(sample.keys())


def test_list_handoff_rows_status_filter_excludes_done(tmp_path: Path) -> None:
    """list_handoff_rows(status_filter=['in_progress']) excludes status=done rows."""
    from workstate_handoff_mcp import api as mcp_server

    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="LIVE", status="in_progress", target_branch="main")
        _insert_row(conn, task_ref="DONE", status="done", target_branch="main")
        conn.commit()
    finally:
        conn.close()

    rows = mcp_server.list_handoff_rows(status_filter=["in_progress"])
    refs = [row["task_ref"] for row in rows]
    assert refs == ["LIVE"]
