"""WORKSTATE-REF-04 implementation note: explicit ``task_ref`` precedence over workspace ambiguity.

``_resolve_workspace_handoff_row`` must honor an explicit ``task_ref``
before any workspace-row ambiguity is evaluated. When the caller names a
task, that is the operator's disambiguation and the helper returns the
named row (or ``None`` when it does not exist — honor-then-warn, the warn
channel lives in the caller layer) instead of raising
``AmbiguousWorkspaceContextError``.

The no-``task_ref`` path is unchanged: multiple live rows that cannot be
resolved by cwd/workspace-root tiering still raise loudly with the full
candidate list.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import RuntimeConfig, configure_runtime
from workstate_handoff_mcp.shared_primitives import _resolve_workspace_handoff_row
from workstate_handoff_mcp.shared_write_context import AmbiguousWorkspaceContextError


def _configured_conn(tmp_path: Path) -> sqlite3.Connection:
    configure_runtime(RuntimeConfig.for_repo(tmp_path))
    from workstate_handoff_mcp.shared_schema import _open_db_connection

    return _open_db_connection()


def _insert_row(conn: sqlite3.Connection, *, task_ref: str, target_branch: str | None = None) -> None:
    conn.execute(
        """
        INSERT INTO handoff_state (
            task_ref, objective, focus, status, target_branch,
            target_worktree_path, revision, updated_at, updated_by,
            updated_branch, updated_commit_sha
        ) VALUES (?, ?, ?, 'in_progress', ?, NULL, 0,
                  datetime('now'), 'tester', 'main', 'abc123')
        """,
        (task_ref, f"obj-{task_ref}", f"focus-{task_ref}", target_branch),
    )


def test_explicit_task_ref_honored_over_workspace_ambiguity(tmp_path: Path) -> None:
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()

        row = _resolve_workspace_handoff_row(conn, task_ref="WORKSTATE-REF-B")

        assert row is not None
        assert row["task_ref"] == "WORKSTATE-REF-B"
    finally:
        conn.close()


def test_no_task_ref_still_raises_with_candidates(tmp_path: Path) -> None:
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()

        with pytest.raises(AmbiguousWorkspaceContextError) as excinfo:
            _resolve_workspace_handoff_row(conn)

        task_refs = sorted(c["task_ref"] for c in excinfo.value.candidates)
        assert task_refs == ["WORKSTATE-REF-A", "WORKSTATE-REF-B"]
    finally:
        conn.close()


def test_explicit_task_ref_absent_returns_none_without_raising(tmp_path: Path) -> None:
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()

        # Honor-then-warn: a named ref that matches no live row resolves to
        # None (the named intent is honored, not overridden by ambiguity
        # inference). The warning is emitted by the caller layer.
        assert _resolve_workspace_handoff_row(conn, task_ref="DOES-NOT-EXIST") is None
    finally:
        conn.close()
