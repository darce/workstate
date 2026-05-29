"""WORKSTATE-REF-54 implementation note sub-implementation note.1: server-side four-step Resolution Rule.

The four-step Resolution Rule (Resolving CTP-PR-03 in the WORKSTATE-REF-54
plan) is the shared spec implemented by both the server-side MCP
resolver and the client-side lifecycle CLI handlers:

    Step 1. Explicit ``task_ref`` argument.
    Step 2. ``AGENTIC_LANE_ID`` env var binding (sub-implementation note.2).
    Step 3. Unique active task for the canonical workspace root.
    Step 4. Multiple tasks share the canonical workspace root → fail
            with the structured ``AmbiguousWorkspaceContextError``
            (no "last writer wins" fallback).

This test module pins down steps 1, 3, 4 against the canonical
server-side entry point ``resolve_active_task_ref``. Sub-implementation note.2
adds the step 2 lane-binding test (separate commit).

The fixtures here populate ``handoff_state`` directly via SQL — the
goal is to exercise the resolver in isolation without coupling to
the writer side of the system.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import (
    RuntimeConfig,
    UnresolvedTaskContextError,
    configure_runtime,
)
from workstate_handoff_mcp.shared_primitives import resolve_active_task_ref
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
    status: str = "in_progress",
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


def _insert_lane(
    conn: sqlite3.Connection,
    *,
    task_ref: str,
    lane_id: str,
    worktree_path: str = "/tmp/lane-wt",
    branch: str = "feature/lane",
    status: str = "active",
) -> None:
    conn.execute(
        """
        INSERT INTO worktree_lanes (
            task_ref, lane_id, worktree_path, branch, status
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (task_ref, lane_id, worktree_path, branch, status),
    )


# ---------------------------------------------------------------------------
# Step 1: explicit task_ref short-circuits earlier steps
# ---------------------------------------------------------------------------


def test_step_1_explicit_task_ref_returns_unchanged(tmp_path: Path) -> None:
    """When the caller passes ``task_ref`` explicitly, it wins
    regardless of how many other rows are active. The resolver must
    not even consult workspace-path or lane bindings — explicit
    intent always overrides the inference rules."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()

        # Two ambiguous rows in the table — but the explicit caller
        # bypasses ambiguity entirely.
        assert resolve_active_task_ref(conn, task_ref="WORKSTATE-REF-54") == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_1_explicit_task_ref_does_not_require_row_to_exist(tmp_path: Path) -> None:
    """Step 1 returns the task_ref string verbatim. The resolver does
    not verify the row exists — the caller (write/read handler) is
    the one that decides whether existence matters. This preserves
    the existing ``_resolve_task_ref`` contract."""
    conn = _configured_conn(tmp_path)
    try:
        # No rows at all.
        assert resolve_active_task_ref(conn, task_ref="WORKSTATE-REF-54") == "WORKSTATE-REF-54"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 2: AGENTIC_LANE_ID env var binding (sub-implementation note.2)
# ---------------------------------------------------------------------------


def test_step_2_legacy_lane_id_env_binds_to_task_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``AGENTIC_LANE_ID`` is set and matches a row in
    ``worktree_lanes``, the resolver returns that lane's ``task_ref``
    even if multiple workspace-root candidates would otherwise be
    ambiguous. This is the binding the orchestrator uses to scope a
    spawned worker subprocess to its lane's task."""
    conn = _configured_conn(tmp_path)
    try:
        # Two ambiguous workspace-root candidates so the test fails
        # at step 4 if step 2 doesn't short-circuit.
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        _insert_lane(conn, task_ref="WORKSTATE-REF-54", lane_id="lane-7")
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "lane-7")

        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_2_canonical_workstate_lane_id_binds_to_task_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """implementation note B4: the canonical system-wide ``WORKSTATE_LANE_ID`` binds
    the resolver to its lane's ``task_ref`` exactly as the legacy
    ``AGENTIC_LANE_ID`` did (which remains honored via the alias fallback)."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        _insert_lane(conn, task_ref="WORKSTATE-REF-54", lane_id="lane-7")
        conn.commit()

        monkeypatch.delenv("AGENTIC_LANE_ID", raising=False)
        monkeypatch.setenv("WORKSTATE_LANE_ID", "lane-7")

        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_2_legacy_lane_id_unset_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``AGENTIC_LANE_ID`` is not set, step 2 must return None
    so step 3 (workspace-root resolution) runs. The single-row
    fixture below would never resolve via step 2, so the assert
    proves the resolver fell through."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        _insert_lane(conn, task_ref="WORKSTATE-REF-54", lane_id="lane-7")
        conn.commit()

        monkeypatch.delenv("AGENTIC_LANE_ID", raising=False)

        # Step 2 returns None → step 3 finds the single live row.
        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_2_legacy_lane_id_unknown_lane_falls_through(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``AGENTIC_LANE_ID`` is set but no ``worktree_lanes`` row
    matches, step 2 must fall through to step 3 (and beyond) instead
    of hard-failing. A stale env var must not prevent the
    workspace-root resolver from finding the right answer."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        # No matching lane row.
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "lane-stale")

        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_2_legacy_lane_id_empty_string_treated_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``AGENTIC_LANE_ID=''`` must behave like an unset env var.
    Subprocess env propagation often passes empty strings rather than
    omitting keys; treating them as unset prevents a spurious step-2
    miss from masking a legitimate step-3 resolution."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "")

        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_2_legacy_lane_id_excludes_closed_lanes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BR-WORKSTATWORKSTATE-REF-54-20260510-S4-01: when ``AGENTIC_LANE_ID`` matches a
    ``worktree_lanes`` row whose status is ``closed`` or ``archived``,
    step 2 must NOT bind to that lane. A worker process whose lane has
    been closed must fall through to implementation note rather than silently
    riding a stale binding into a now-non-current task.

    The fixture below has a single ``closed`` lane bound to a row that
    no longer exists in ``handoff_state`` (the typical post-close
    state). The resolver MUST ignore the closed lane and surface the
    legitimate workspace-resolution outcome — here, a step-4 unresolved
    error because no live row matches.
    """
    conn = _configured_conn(tmp_path)
    try:
        _insert_lane(
            conn,
            task_ref="WORKSTATE-REF-54",
            lane_id="lane-7",
            status="closed",
        )
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "lane-7")

        # Closed lane must not bind; with no live handoff_state row the
        # resolver falls through to implementation note and raises ValueError
        # (matches the existing no-live-row contract).
        with pytest.raises(ValueError):
            resolve_active_task_ref(conn, task_ref=None)
    finally:
        conn.close()


def test_step_2_legacy_lane_id_excludes_closed_lane_falls_through_to_step_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BR-WORKSTATWORKSTATE-REF-54-20260510-S4-01: when a closed lane matches
    ``AGENTIC_LANE_ID`` AND a live row exists for a different task, the
    closed-lane filter must let step 3 find the live row instead of
    silently binding the worker to the closed lane's now-stale task.
    """
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        _insert_lane(
            conn,
            task_ref="WORKSTATE-REF-OLD",
            lane_id="lane-stale",
            status="closed",
        )
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "lane-stale")

        # Closed lane must not bind to WORKSTATE-REF-OLD; step 3 finds the
        # unique live row WORKSTATE-REF-54.
        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_2_legacy_lane_id_excludes_lane_without_live_handoff_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-54-BR-01: an active ``worktree_lanes`` row whose
    ``task_ref`` no longer has a live ``handoff_state`` row (status in
    ``LIVE_ACTIVE_STATUSES``) must NOT bind step 2. ``archive_task_state``
    leaves ``worktree_lanes`` rows untouched unless ``prune_working_rows``
    is set, and ``task-finish`` only warns about open lanes — so a stale
    env-bound worker could otherwise short-circuit resolution to an
    archived/deleted task. The lane binding must require a live
    ``handoff_state`` row, falling through to implementation note otherwise.
    """
    conn = _configured_conn(tmp_path)
    try:
        # Active lane, but no handoff_state row at all → archived/deleted.
        _insert_lane(
            conn,
            task_ref="ARCHIVED-TASK",
            lane_id="lane-7",
            status="active",
        )
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "lane-7")

        # Without a live handoff_state row, step 2 must not bind. Step
        # 3/4 then raises ValueError because no live row exists.
        with pytest.raises(ValueError):
            resolve_active_task_ref(conn, task_ref=None)
    finally:
        conn.close()


def test_step_2_legacy_lane_id_excludes_lane_for_done_task_falls_through_to_step_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-54-BR-01: when an active lane row binds to a task whose
    ``handoff_state`` row is ``done`` (post-finish, pre-archive), step 2
    must fall through so step 3 finds the unique live row for a
    different task. Without the live-row join, the env-bound worker
    would silently keep resolving to the finished task."""
    conn = _configured_conn(tmp_path)
    try:
        # The lane's task is finished but not archived.
        _insert_row(
            conn,
            task_ref="DONE-TASK",
            target_branch="main",
            status="done",
        )
        _insert_lane(
            conn,
            task_ref="DONE-TASK",
            lane_id="lane-stale",
            status="active",
        )
        # A different task is genuinely live.
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "lane-stale")

        # Lane row is active but its task_ref is not live → step 2 falls
        # through; step 3 finds the unique live WORKSTATE-REF-54 row.
        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_2_runs_after_step_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 1 (explicit task_ref) must short-circuit BEFORE step 2.
    The fixture below has a lane binding that would resolve to
    ``WORKSTATE-REF-54`` via step 2, but the explicit caller names
    ``WORKSTATE-REF-OTHER`` — explicit intent always wins."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_lane(conn, task_ref="WORKSTATE-REF-54", lane_id="lane-7")
        conn.commit()

        monkeypatch.setenv("AGENTIC_LANE_ID", "lane-7")

        assert resolve_active_task_ref(conn, task_ref="WORKSTATE-REF-OTHER") == "WORKSTATE-REF-OTHER"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 3: unique active task for the canonical workspace root
# ---------------------------------------------------------------------------


def test_step_3_single_active_row_resolves(tmp_path: Path) -> None:
    """When exactly one row is live and matches the canonical
    workspace root (here: only one row exists, so the workspace-root
    tier collapses to it), the resolver returns its task_ref."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        conn.commit()

        assert resolve_active_task_ref(conn, task_ref=None) == "WORKSTATE-REF-54"
    finally:
        conn.close()


def test_step_3_done_status_excluded_from_resolution(tmp_path: Path) -> None:
    """Only ``LIVE_ACTIVE_STATUSES`` rows participate in step 3.
    A ``status=done`` row must not resolve, even if it would otherwise
    be the unique active row, because ``done`` is archive-eligible
    only (mirrors ``LIVE_ACTIVE_STATUSES`` in shared_primitives)."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(
            conn,
            task_ref="WORKSTATE-REF-54",
            target_branch="feature/WORKSTATE-54",
            status="done",
        )
        conn.commit()

        with pytest.raises(ValueError):
            resolve_active_task_ref(conn, task_ref=None)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step 4: multi-row ambiguity raises the structured error
# ---------------------------------------------------------------------------


def test_step_4_multi_row_ambiguity_raises_structured_error(tmp_path: Path) -> None:
    """Two coexisting active rows with no workspace-path
    discrimination must raise ``AmbiguousWorkspaceContextError``
    (a ``UnresolvedTaskContextError`` and ``ValueError`` subclass)
    with a populated ``candidates`` list. There is NO "last writer
    wins" fallback — the resolver fails closed and lets the caller
    surface the candidates."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()

        with pytest.raises(AmbiguousWorkspaceContextError) as excinfo:
            resolve_active_task_ref(conn, task_ref=None)

        assert isinstance(excinfo.value, UnresolvedTaskContextError)
        assert isinstance(excinfo.value, ValueError)
        task_refs = sorted(c["task_ref"] for c in excinfo.value.candidates)
        assert task_refs == ["WORKSTATE-REF-A", "WORKSTATE-REF-B"]
    finally:
        conn.close()


def test_step_4_no_active_rows_raises_unresolved(tmp_path: Path) -> None:
    """When no live rows exist at all, the resolver must raise a
    ``ValueError`` (the caller catches and reports "no active task").
    This is distinct from the step-4 ambiguity case and must NOT
    be conflated with it."""
    conn = _configured_conn(tmp_path)
    try:
        # No rows inserted.
        with pytest.raises(ValueError):
            resolve_active_task_ref(conn, task_ref=None)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Step ordering — explicit task_ref must short-circuit before any
# DB consultation (would otherwise raise on ambiguity).
# ---------------------------------------------------------------------------


def test_step_ordering_step_1_runs_before_step_3(tmp_path: Path) -> None:
    """Step 1 must short-circuit before the resolver consults the
    DB for the workspace-root tier. If step 1 leaked into step 3,
    the ambiguous fixture below would raise even though the caller
    named the task explicitly."""
    conn = _configured_conn(tmp_path)
    try:
        _insert_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_row(conn, task_ref="WORKSTATE-REF-B", target_branch="main")
        conn.commit()

        # Step 1 wins; ambiguity at implementation note is never reached.
        assert resolve_active_task_ref(conn, task_ref="WORKSTATE-REF-A") == "WORKSTATE-REF-A"
    finally:
        conn.close()
