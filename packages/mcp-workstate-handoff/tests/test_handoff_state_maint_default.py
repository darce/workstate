"""Regression tests for WORKSTATE-REF-* target_worktree_path on insert.

After WORKSTATE-REF-51 implementation note, WORKSTATE-REF-* tasks no longer auto-default
``target_worktree_path`` to the workspace root on insert. The original
WORKSTATE-REF-17-11 resolver-disambiguation motivation is now satisfied by the
explicit ``--commit-sha`` / ``--branch`` actor channel that callers
(notably ``make slice-commit``) use to drive
``handoff_state.updated_commit_sha`` directly without leaning on the
resolver's stored-row task_git fallback. ``task-start`` continues to
write the linked worktree path explicitly when a worktree is created.

WORKSTATE-REF-52 implementation note adds the symbol-existence tripwire below — the
behavioral assertions already pin the *result*, but only a static
source check can pin the *cause* (the helper itself). implementation note made
the resolver derive ``target_worktree_path`` from ``target_branch``
via ``git worktree list``, but writes to the column still happen and
some read sites (dashboard, plan_cli, get_handoff_state) still read
it. A re-introduced default-write helper would still pollute those
reads, so the static guard remains valuable even after implementation note.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from workstate_handoff_mcp import RuntimeConfig, configure_runtime, set_handoff_state


def _setup(tmp_path: Path) -> None:
    configure_runtime(RuntimeConfig.for_repo(tmp_path))


def test_set_handoff_state_persists_null_target_worktree_path_for_maint(tmp_path: Path) -> None:
    """A WORKSTATE-REF-* task on main inserts with ``target_worktree_path`` NULL.

    Before WORKSTATE-REF-51 the row would be backfilled to the workspace root by
    ``_default_maint_target_worktree_path``; that defaulting was the root
    cause of slice-commit projecting the wrong HEAD into
    ``updated_commit_sha`` (the row pointed at the primary tree, not the
    linked worktree). The defaulting is now removed; callers that need a
    concrete path pass it explicitly (e.g. ``task-start``).
    """
    _setup(tmp_path)
    result = set_handoff_state(
        task_ref="WORKSTATE-REF-e2e",
        objective="null target after WORKSTATE-REF-51",
        status="in_progress",
        target_branch="main",
    )
    assert result["ok"] is True, result

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT target_worktree_path FROM handoff_state WHERE task_ref = ?",
            ("WORKSTATE-REF-e2e",),
        ).fetchone()
    assert row is not None
    assert row["target_worktree_path"] is None


def test_set_handoff_state_non_maint_leaves_path_null(tmp_path: Path) -> None:
    _setup(tmp_path)
    result = set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="no-default",
        status="in_progress",
        target_branch="main",
    )
    assert result["ok"] is True, result

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT target_worktree_path FROM handoff_state WHERE task_ref = ?",
            ("WORKSTATE-REF-99",),
        ).fetchone()
    assert row is not None
    assert row["target_worktree_path"] is None


def test_default_maint_target_worktree_path_helper_does_not_exist() -> None:
    """Static tripwire: the deleted helper must never re-appear in ``handoff_state``.

    WORKSTATE-REF-51 implementation note deleted ``_default_maint_target_worktree_path`` after the
    behavioral tests above pinned the *result* (NULL on WORKSTATE-REF-* insert). But
    the result-only assertions cannot tell the difference between "the helper
    is gone" and "a future refactor re-introduces a helper that happens to
    return None for these specific inputs." implementation note made the resolver and
    warning collector derive ``target_worktree_path`` via ``git worktree
    list``, but writes to the column still happen and read sites in
    ``dashboard_rendering``, ``plan_cli``, and ``handoff_state.get_handoff_state``
    still read it; a re-introduced default helper would still pollute those
    reads. A source-level grep is the only check that pins the *cause*.
    """
    from workstate_handoff_mcp import handoff_state

    source = inspect.getsource(handoff_state)
    assert "_default_maint_target_worktree_path" not in source, (
        "The WORKSTATE-REF-51 default-write helper must not be re-introduced. "
        "If you need a default for WORKSTATE-REF-* target_worktree_path, derive it "
        "from target_branch via shared_write_context._canonical_worktree_for_task "
        "or pass it explicitly from the caller (see task-start)."
    )


def test_set_handoff_state_explicit_target_worktree_path_persists(tmp_path: Path) -> None:
    """Explicit caller-supplied paths are written verbatim regardless of task_ref shape."""
    _setup(tmp_path)
    explicit = "/opt/somewhere/else"
    result = set_handoff_state(
        task_ref="WORKSTATE-REF-explicit",
        objective="explicit path persists",
        status="in_progress",
        target_branch="main",
        target_worktree_path=explicit,
    )
    assert result["ok"] is True, result

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        row = conn.execute(
            "SELECT target_worktree_path FROM handoff_state WHERE task_ref = ?",
            ("WORKSTATE-REF-explicit",),
        ).fetchone()
    assert row is not None
    assert row["target_worktree_path"] == explicit
