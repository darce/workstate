"""WORKSTATE-REF-33: read paths surface structured candidates on ambiguity.

Closes COLDSTART-H-02. Verifies that:

A. ``_resolve_workspace_handoff_row`` raises
   ``AmbiguousWorkspaceContextError`` (a ``UnresolvedTaskContextError``
   subclass, which is itself a ``ValueError`` subclass, so existing
   write-path catches still match) with a populated ``candidates``
   list when 2+ main-branch tasks coexist with null
   ``target_worktree_path``.

B. ``get_handoff_state(task_ref=None)`` surfaces those candidates in
   the error envelope under ``data.candidates`` with a ``resolution``
   hint, instead of returning a bare error string.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import (
    RuntimeConfig,
    UnresolvedTaskContextError,
    configure_runtime,
    get_handoff_state,
)
from workstate_handoff_mcp.shared_primitives import _resolve_workspace_handoff_row
from workstate_handoff_mcp.shared_write_context import AmbiguousWorkspaceContextError


def _configured_conn(tmp_path: Path) -> sqlite3.Connection:
    configure_runtime(RuntimeConfig.for_repo(tmp_path))
    from workstate_handoff_mcp.shared_schema import _open_db_connection

    return _open_db_connection()


def _insert_row(
    conn: sqlite3.Connection,
    *,
    task_ref: str,
    target_worktree_path: str | None = None,
    target_branch: str | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO handoff_state (
            task_ref, objective, focus, status, target_branch,
            target_worktree_path, revision, updated_at, updated_by,
            updated_branch, updated_commit_sha
        ) VALUES (?, ?, ?, 'in_progress', ?, ?, 0,
                  datetime('now'), 'tester', 'main', 'abc123')
        """,
        (task_ref, f"obj-{task_ref}", f"focus-{task_ref}", target_branch, target_worktree_path),
    )


def test_resolver_raises_structured_error_with_candidates(tmp_path: Path) -> None:
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()

        with pytest.raises(AmbiguousWorkspaceContextError) as excinfo:
            _resolve_workspace_handoff_row(conn)

        assert isinstance(excinfo.value, UnresolvedTaskContextError)
        assert isinstance(excinfo.value, ValueError)
        task_refs = sorted(c["task_ref"] for c in excinfo.value.candidates)
        assert task_refs == ["WORKSTATE-REF-A", "WORKSTATE-REF-B"]
        sample = next(c for c in excinfo.value.candidates if c["task_ref"] == "WORKSTATE-REF-A")
        assert sample["target_branch"] == "main"
        assert sample["target_worktree_path"] is None
        assert sample["status"] == "in_progress"
    finally:
        conn.close()


def test_get_handoff_state_surfaces_candidates_on_ambiguity(tmp_path: Path) -> None:
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()
    finally:
        conn.close()

    envelope = get_handoff_state()
    assert envelope["ok"] is False
    data = envelope["data"]
    assert "Ambiguous active task" in data["error"]
    assert "candidates" in data
    task_refs = sorted(c["task_ref"] for c in data["candidates"])
    assert task_refs == ["WORKSTATE-REF-A", "WORKSTATE-REF-B"]
    assert "resolution" in data
    assert "task_ref" in data["resolution"]


def test_cwd_disambiguates_maint_at_root_from_feature_in_linked_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: a WORKSTATE-REF row pinned to the primary worktree must not
    collide with a feature row pinned to a linked worktree when the
    caller's cwd is inside the linked worktree.

    RuntimeConfig.for_repo collapses every linked worktree to the primary
    root, so _workspace_root() is the primary path even when cwd points
    at the linked feature worktree. The resolver tiers candidates so the
    cwd match wins before the workspace_root fallback applies.
    """
    primary_root = tmp_path / "primary"
    feature_root = tmp_path / "feature-worktree"
    primary_root.mkdir()
    feature_root.mkdir()

    conn = _configured_conn(primary_root)
    try:
        _insert_row(
            conn,
            task_ref="WORKSTATE-REF-scan",
            target_branch="main",
            target_worktree_path=str(primary_root),
        )
        _insert_row(
            conn,
            task_ref="pds-pipeline-stability-26",
            target_branch="feature/pds",
            target_worktree_path=str(feature_root),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.chdir(feature_root)
    conn = _configured_conn(primary_root)
    try:
        row = _resolve_workspace_handoff_row(conn)
    finally:
        conn.close()

    assert row is not None
    assert row["task_ref"] == "pds-pipeline-stability-26"
