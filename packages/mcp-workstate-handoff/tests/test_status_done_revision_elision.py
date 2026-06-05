"""WORKSTATE-REF-41 implementation note: ``update_task_status`` revision elision for ``status='done'``.

Closes the cold-start ergonomics regression where ``update_task_status(status='done')``
unconditionally required ``expected_revision``, forcing operators to issue an
extra ``get_handoff_state(sections='identity')`` call before every task-finish.

The fix infers ``expected_revision`` for ``status='done'`` only, by opening
``BEGIN IMMEDIATE`` and reading the current revision inside the same
transaction. Other transitions (``in_progress``, ``review``, ``blocked``)
continue to require an explicit ``expected_revision`` because they are
mid-lifecycle mutations where stale-write protection is more valuable than
ergonomic shorthand.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp import handoff_state
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


def test_update_task_status_done_succeeds_without_expected_revision(workspace: Path) -> None:
    """Bare update_task_status(status='done') succeeds — server infers the revision."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="elide-done-task",
            objective="Cold-start ergonomics: status=done elides expected_revision",
            status="in_progress",
        )
    )

    updated = _parse(mcp_server.update_task_status(task_ref="elide-done-task", status="done"))

    assert updated["ok"] is True
    assert updated["updated_scope"] == "active"
    assert updated["active"]["status"] == "done"
    assert updated["active"]["revision"] == 1


def test_update_task_status_done_elision_after_intermediate_revision_bumps(workspace: Path) -> None:
    """Elision reads the current revision at update time, not the create-time revision."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="bump-then-elide",
            objective="Several intermediate updates then bare done",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="bump-then-elide",
            status="review",
            expected_revision=0,
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="bump-then-elide",
            status="blocked",
            expected_revision=1,
        )
    )

    updated = _parse(mcp_server.update_task_status(task_ref="bump-then-elide", status="done"))

    assert updated["ok"] is True
    assert updated["active"]["status"] == "done"
    assert updated["active"]["revision"] == 3


def test_update_task_status_in_progress_still_requires_expected_revision(workspace: Path) -> None:
    """Asymmetric: in_progress (mid-lifecycle) still rejects the bare call."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="midlife-task",
            objective="Mid-lifecycle transition still demands stale-write protection",
            status="in_progress",
        )
    )

    rejected = _parse(mcp_server.update_task_status(task_ref="midlife-task", status="review"))

    assert rejected["ok"] is False
    assert "expected_revision" in (rejected.get("error") or "")


def test_update_task_status_blocked_still_requires_expected_revision(workspace: Path) -> None:
    """Asymmetric: blocked also remains a mid-lifecycle mutation."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="midlife-blocked",
            objective="blocked still requires expected_revision",
            status="in_progress",
        )
    )

    rejected = _parse(mcp_server.update_task_status(task_ref="midlife-blocked", status="blocked"))

    assert rejected["ok"] is False
    assert "expected_revision" in (rejected.get("error") or "")


def test_update_task_status_done_with_explicit_expected_revision_still_works(workspace: Path) -> None:
    """Existing callers that pass expected_revision keep working unchanged."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="explicit-revision-task",
            objective="Explicit expected_revision must stay supported",
            status="in_progress",
        )
    )

    updated = _parse(
        mcp_server.update_task_status(
            task_ref="explicit-revision-task",
            status="done",
            expected_revision=0,
        )
    )

    assert updated["ok"] is True
    assert updated["active"]["status"] == "done"
    assert updated["active"]["revision"] == 1


def test_update_task_status_done_explicit_stale_revision_rejected(workspace: Path) -> None:
    """If the caller passes a stale explicit expected_revision, the call still fails.

    The elision path only kicks in when expected_revision is None; an explicit
    wrong value preserves stale-write detection.
    """
    _parse(
        mcp_server.set_handoff_state(
            task_ref="stale-explicit",
            objective="Explicit stale expected_revision must still reject",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="stale-explicit",
            status="review",
            expected_revision=0,
        )
    )

    rejected = _parse(
        mcp_server.update_task_status(
            task_ref="stale-explicit",
            status="done",
            expected_revision=0,
        )
    )

    assert rejected["ok"] is False


def test_update_task_status_done_elision_under_concurrent_writer(workspace: Path) -> None:
    """BEGIN IMMEDIATE serializes against an interleaved writer.

    The plan calls out: "the test explicitly exercises the BEGIN IMMEDIATE
    lock boundary rather than relying on default SQLite transaction timing."
    We open a separate connection that holds an EXCLUSIVE lock for a brief
    window while the main thread issues update_task_status(done) without
    expected_revision. The implementation must serialize correctly and the
    final state must reflect the bumped revision.
    """
    _parse(
        mcp_server.set_handoff_state(
            task_ref="concurrent-done",
            objective="Concurrent write boundary",
            status="in_progress",
        )
    )

    # Bump the revision via an explicit second update so the in-memory
    # "create-time revision" of 0 would be stale for any naive elision.
    _parse(
        mcp_server.set_handoff_state(
            task_ref="concurrent-done",
            status="review",
            expected_revision=0,
        )
    )

    state = mcp_server.get_runtime_config()
    db_path = Path(state.state_dir) / "handoff.db"
    assert db_path.exists()

    blocker_done = threading.Event()
    main_proceed = threading.Event()

    def _hold_immediate_lock() -> None:
        conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=10.0)
        try:
            conn.execute("BEGIN IMMEDIATE")
            main_proceed.set()
            blocker_done.wait(timeout=2.0)
            conn.execute("COMMIT")
        finally:
            conn.close()

    blocker = threading.Thread(target=_hold_immediate_lock)
    blocker.start()
    try:
        assert main_proceed.wait(timeout=2.0), "blocker thread never acquired lock"
        # Release blocker shortly so the main update can proceed.
        threading.Timer(0.2, blocker_done.set).start()
        updated = _parse(mcp_server.update_task_status(task_ref="concurrent-done", status="done"))
    finally:
        blocker_done.set()
        blocker.join(timeout=5.0)

    assert updated["ok"] is True
    assert updated["active"]["status"] == "done"
    # revision should be 2 (insert=0 -> review=1 -> done=2)
    assert updated["active"]["revision"] == 2


def test_update_task_status_done_keeps_lock_until_write_completes(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The inferred-revision read and the status write stay under one write lock.

    Regression for WORKSTATE-REF-41-BR-01: older code committed immediately after the
    revision read, then delegated the write on a fresh connection. A competing
    writer could slip in between and bump the revision, turning the ergonomic
    bare call back into a stale-write conflict. The fixed path keeps the lock
    held until the write completes.
    """
    _parse(
        mcp_server.set_handoff_state(
            task_ref="locked-done",
            objective="Keep the inferred revision lock through the write",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="locked-done",
            status="review",
            expected_revision=0,
        )
    )

    state = mcp_server.get_runtime_config()
    db_path = Path(state.state_dir) / "handoff.db"
    assert db_path.exists()

    writer_start = threading.Event()
    writer_attempted = threading.Event()
    writer_result: dict[str, object] = {}

    def _competing_writer() -> None:
        assert writer_start.wait(timeout=2.0), "main thread never signaled competing writer"
        conn = sqlite3.connect(str(db_path), isolation_level=None, timeout=0.05)
        try:
            writer_attempted.set()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as exc:
                writer_result["error"] = str(exc)
                return
            conn.execute(
                "UPDATE handoff_state SET revision = revision + 1 WHERE task_ref = ?",
                ("locked-done",),
            )
            conn.execute("COMMIT")
            writer_result["updated"] = True
        finally:
            conn.close()

    writer = threading.Thread(target=_competing_writer)
    writer.start()

    original = handoff_state._set_handoff_state_with_conn

    def _wrapped_set_handoff_state_with_conn(conn, **kwargs):  # type: ignore[no-untyped-def]
        writer_start.set()
        assert writer_attempted.wait(timeout=1.0), "competing writer never attempted its write"
        time.sleep(0.1)
        return original(conn, **kwargs)

    monkeypatch.setattr(handoff_state, "_set_handoff_state_with_conn", _wrapped_set_handoff_state_with_conn)

    updated = _parse(mcp_server.update_task_status(task_ref="locked-done", status="done"))
    writer.join(timeout=5.0)

    assert updated["ok"] is True
    assert updated["active"]["status"] == "done"
    assert updated["active"]["revision"] == 2
    assert writer_result.get("updated") is not True
    assert "locked" in str(writer_result.get("error", "")).lower()
