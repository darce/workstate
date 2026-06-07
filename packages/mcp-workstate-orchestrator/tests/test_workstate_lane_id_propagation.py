"""WORKSTATE_LANE_ID end-to-end propagation.

Cross-package contract test. Pins the chain:

    orchestrator daemon_start  →  worker subprocess env (implementation note.3)
                                ↓
              MCP resolver step 2 binding  (implementation note.2)
                                ↓
                        active task_ref

Without this test, the orchestrator and the resolver could diverge on
the env var name (e.g. one renamed it without the other catching up)
and the four-step Resolution Rule's step 2 would silently degrade to
a no-op in production. The contract here is the *literal env-var key*
shared by both sides — the test composes a real ``daemon_start``
spawn (Popen mocked) with a real
``shared_primitives.resolve_active_task_ref`` call so any rename on
either side surfaces immediately.

This is an in-process composition test, not a true subprocess
round-trip — that would require booting a worker daemon. The
in-process composition is sufficient because the contract under
test is "what daemon_start writes is what the resolver reads,"
which is preserved as long as both sides reference the same
``WORKSTATE_LANE_ID_ENV`` constant.
"""

from __future__ import annotations

import importlib.util
import os
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "src" / "workstate_orchestrator_mcp" / "orchestration"
WORKER_DAEMON_CTL_PATH = ORCHESTRATION_DIR / "worker_daemon_ctl.py"


def _load_worker_daemon_ctl():
    spec = importlib.util.spec_from_file_location("worker_daemon_ctl_e2e", WORKER_DAEMON_CTL_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module from {WORKER_DAEMON_CTL_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _configured_conn(tmp_path: Path) -> sqlite3.Connection:
    from workstate_handoff_mcp import RuntimeConfig, configure_runtime
    from workstate_handoff_mcp.shared_schema import _open_db_connection

    configure_runtime(RuntimeConfig.for_repo(tmp_path))
    return _open_db_connection()


def _insert_handoff_row(conn: sqlite3.Connection, *, task_ref: str, target_branch: str) -> None:
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


def _insert_lane_row(
    conn: sqlite3.Connection,
    *,
    task_ref: str,
    lane_id: str,
    worktree_path: str = "/tmp/lane-wt",
    branch: str = "feature/lane",
) -> None:
    conn.execute(
        """
        INSERT INTO worktree_lanes (
            task_ref, lane_id, worktree_path, branch, status
        ) VALUES (?, ?, ?, ?, 'active')
        """,
        (task_ref, lane_id, worktree_path, branch),
    )


def test_workstate_lane_id_propagates_from_daemon_start_to_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end: daemon_start emits WORKSTATE_LANE_ID into the
    spawned worker's env; the MCP resolver's step 2 reads the same
    env var and binds to the lane's task_ref. Two ambiguous
    workspace-root rows ensure step 2 must do real work — without
    propagation, the resolver would fail at step 4."""
    # 1. Capture the env that daemon_start would hand to the spawned
    #    worker (Popen mocked; we never actually fork).
    mod = _load_worker_daemon_ctl()
    proc = mock.Mock(pid=4567)
    captured_env: dict = {}

    def _capture_popen(*args, **kwargs):
        captured_env.update(kwargs["env"])
        return proc

    with (
        mock.patch.object(mod, "daemon_status", return_value={"process": None}),
        mock.patch.object(mod.subprocess, "Popen", side_effect=_capture_popen),
    ):
        mod.daemon_start(
            orchestrator_root=tmp_path / "orch_root",
            state_dir=tmp_path / "state",
            log_dir=tmp_path / "logs",
            task_ref="WORKSTATE-REF-54",
            lane_id="lane-frontend-7",
            worktree_path=tmp_path / "lane",
            session="WORKSTATE-54-lane-frontend-7",
            python_executable="/usr/bin/python3",
        )
    (tmp_path / "orch_root").mkdir(exist_ok=True)

    # 2. Confirm the orchestrator-side contract emitted the var.
    propagated = captured_env.get("WORKSTATE_LANE_ID")
    assert propagated == "lane-frontend-7", "daemon_start failed to inject WORKSTATE_LANE_ID into the subprocess env."

    # 3. Apply the captured env to the current process and drive the
    #    server-side resolver. Two ambiguous workspace-root rows force
    #    the resolver to use step 2; without propagation it would
    #    raise AmbiguousWorkspaceContextError at step 4.
    monkeypatch.delenv("WORKSTATE_LANE_ID", raising=False)
    monkeypatch.setenv("WORKSTATE_LANE_ID", propagated)

    from workstate_handoff_mcp.shared_primitives import resolve_active_task_ref

    conn = _configured_conn(tmp_path / "handoff_db")
    try:
        _insert_handoff_row(conn, task_ref="WORKSTATE-REF-A", target_branch="main")
        _insert_handoff_row(conn, task_ref="WORKSTATE-REF-54", target_branch="feature/WORKSTATE-54")
        _insert_lane_row(conn, task_ref="WORKSTATE-REF-54", lane_id="lane-frontend-7")
        conn.commit()

        resolved = resolve_active_task_ref(conn, task_ref=None)
        assert resolved == "WORKSTATE-REF-54", (
            "Server-side resolver did not bind to the propagated "
            "WORKSTATE_LANE_ID — the four-step Resolution Rule's "
            "step 2 chain is broken."
        )
    finally:
        conn.close()


def test_workstate_lane_id_env_var_name_matches_constant() -> None:
    """The orchestrator-side string literal ``"WORKSTATE_LANE_ID"`` and
    the server-side ``shared_primitives.WORKSTATE_LANE_ID_ENV`` constant
    must agree. Renaming one without the other would silently break
    step 2 in production. This test fails fast on drift."""
    from workstate_handoff_mcp.shared_primitives import WORKSTATE_LANE_ID_ENV

    mod = _load_worker_daemon_ctl()
    source = WORKER_DAEMON_CTL_PATH.read_text(encoding="utf-8")
    # The orchestrator hardcodes the env var name; verify it matches
    # the server-side constant verbatim.
    assert f'env["{WORKSTATE_LANE_ID_ENV}"] = lane_id' in source, (
        f"worker_daemon_ctl.daemon_start does not assign the canonical "
        f"env var name {WORKSTATE_LANE_ID_ENV!r}; the Resolution Rule "
        "step 2 chain is broken."
    )
    # Sanity: the loaded module exposes daemon_start (avoid the test
    # passing trivially against a stale file path).
    assert hasattr(mod, "daemon_start")
