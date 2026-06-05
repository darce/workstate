"""WS-TASKSTART regression: ``task-start`` must leave a live handoff_state row.

The reported bug: ``make task-start`` reported ``handoff_projection=synced``
(receipt ``ok:true``, branch + worktree + .venv created) but never persisted a
live ``handoff_state`` row in the canonical handoff DB. Root cause: the handler
parsed ``--objective`` but never forwarded it to ``projection.project_state_sync``,
so the ``set`` insert was rejected (objective required) — and because the handoff
CLI prints its envelope yet always exits 0, that rejection was misclassified as
``synced`` and the row silently never landed.

Unlike ``test_task_start_uv_sync.py`` (which stubs the handoff CLI with a bare
``exit 0`` and therefore could never catch a rejected insert), this test drives
``task-start`` against the *real* ``mcp-workstate-handoff`` CLI and then reads the
on-disk handoff DB, asserting the row is actually present.

CI NOTE: ``workstate-system`` intentionally declares no dependency on
``mcp-workstate-handoff`` (the inverse-dependency invariant — see
``pyproject.toml``), so these tests resolve the CLI via ``shutil.which`` and
``pytest.skip`` when it is absent rather than importing it. The trade-off is that
this is the ONLY test driving the real CLI: CI must provision the
``mcp-workstate-handoff`` console script on PATH (e.g. by syncing the sibling
package's venv) or this regression goes unguarded under a green skip.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"

# Fake ``uv`` that satisfies the preflight, materializes ``<venv>/bin/python`` on
# ``uv venv --seed`` (WORKSTATE-REF-07 root provisioning validates it exists), and treats
# ``sync`` / ``pip install`` as no-ops. Mirrors test_task_start_uv_sync.py.
_FAKE_UV_BODY = (
    "#!/usr/bin/env bash\n"
    'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
    'if [[ "$1" == "venv" ]]; then mkdir -p "$2/bin"; : > "$2/bin/python"; '
    'chmod +x "$2/bin/python"; fi\n'
    "exit 0\n"
)


def _write_executable(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def real_handoff_cli() -> str:
    cli = shutil.which("mcp-workstate-handoff")
    if cli is None:
        pytest.skip("mcp-workstate-handoff console script not on PATH")
    return cli


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    return repo


def _read_handoff_rows(repo: Path) -> list[sqlite3.Row]:
    db = repo / ".task-state" / "handoff.db"
    if not db.exists():
        return []
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            "SELECT task_ref, status, objective, target_branch, "
            "target_worktree_path FROM handoff_state"
        ).fetchall()
    finally:
        conn.close()


def test_task_start_persists_live_handoff_row(
    git_repo: Path, real_handoff_cli: str, tmp_path: Path
) -> None:
    """End-to-end: after ``task-start`` the canonical handoff DB holds a live
    ``in_progress`` row for the task_ref carrying the supplied objective.
    """
    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_BODY)

    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = real_handoff_cli
    env["WORKSTATE_LIFECYCLE_UV_BIN"] = str(fake_uv)
    # Keep the best-effort overlay-adopt step inert: this scratch repo is not a
    # materialized overlay, so it would skip anyway, but pin it off explicitly.
    env["WORKSTATE_ADOPT_CMD"] = ""

    proc = subprocess.run(
        [
            sys.executable, str(LIFECYCLE_PKG), "task-start",
            "--task", "WS-LIVEROW-01",
            "--objective", "persist a live handoff row",
            "--mode", "here",
            "--json",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["task_ref"] == "WS-LIVEROW-01"
    # The receipt's own projection claim must be honest: a real row landed.
    assert receipt["handoff_projection"] == "synced", receipt

    rows = _read_handoff_rows(git_repo)
    matching = [r for r in rows if r["task_ref"] == "WS-LIVEROW-01"]
    assert matching, (
        f"no live handoff_state row for WS-LIVEROW-01; rows={[dict(r) for r in rows]}"
    )
    row = matching[0]
    assert row["status"] == "in_progress", dict(row)
    assert row["objective"] == "persist a live handoff row", dict(row)
    assert row["target_branch"] == "feature/ws-liverow-01", dict(row)


def test_task_start_without_objective_still_persists_row(
    git_repo: Path, real_handoff_cli: str, tmp_path: Path
) -> None:
    """Realistic "forgot OBJECTIVE" case: the Makefile only forwards
    ``--objective`` when OBJECTIVE is set, so ``args.objective`` defaults to
    "". An empty objective is still a valid INSERT (the handoff server rejects
    only ``objective is None``), so a row must still land — creating a row with
    an empty objective beats leaving the task entirely unrecorded.
    """
    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_BODY)

    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = real_handoff_cli
    env["WORKSTATE_LIFECYCLE_UV_BIN"] = str(fake_uv)
    env["WORKSTATE_ADOPT_CMD"] = ""

    proc = subprocess.run(
        [
            sys.executable, str(LIFECYCLE_PKG), "task-start",
            "--task", "WS-LIVEROW-03",
            # deliberately no --objective
            "--mode", "here",
            "--json",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["handoff_projection"] == "synced", receipt

    rows = _read_handoff_rows(git_repo)
    matching = [r for r in rows if r["task_ref"] == "WS-LIVEROW-03"]
    assert matching, (
        f"no row for objective-less task-start; rows={[dict(r) for r in rows]}"
    )
    assert matching[0]["status"] == "in_progress", dict(matching[0])
    # Empty objective is the documented trade-off, not None.
    assert (matching[0]["objective"] or "") == "", dict(matching[0])


def test_task_start_projection_synced_implies_row_present(
    git_repo: Path, real_handoff_cli: str, tmp_path: Path
) -> None:
    """Guards the silent-no-op specifically: a ``synced`` projection receipt
    must never coexist with an absent row. Before the fix the insert was
    rejected (objective dropped) yet the receipt still said ``synced``.
    """
    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_BODY)

    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = real_handoff_cli
    env["WORKSTATE_LIFECYCLE_UV_BIN"] = str(fake_uv)
    env["WORKSTATE_ADOPT_CMD"] = ""

    proc = subprocess.run(
        [
            sys.executable, str(LIFECYCLE_PKG), "task-start",
            "--task", "WS-LIVEROW-02",
            "--objective", "synced must mean persisted",
            "--mode", "here",
            "--json",
        ],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    receipt = json.loads(proc.stdout)

    rows = _read_handoff_rows(git_repo)
    present = any(r["task_ref"] == "WS-LIVEROW-02" for r in rows)
    if receipt["handoff_projection"] == "synced":
        assert present, (
            "receipt claimed handoff_projection=synced but no row landed: "
            f"rows={[dict(r) for r in rows]}"
        )


def test_replay_drains_spooled_state_sync_into_live_row(
    git_repo: Path, real_handoff_cli: str, tmp_path: Path
) -> None:
    """End-to-end for the replay extension: a ``state_sync`` entry spooled by a
    task-start whose ``set`` could not land online is drained by
    ``project-events-replay`` into a real live handoff_state row, and the spool
    file is emptied. Drives the real handoff CLI (not a stub).
    """
    spool = git_repo / ".task-state" / "pending-workflow-events.jsonl"
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(
        json.dumps(
            {
                "kind": "state_sync",
                "task_ref": "WS-REPLAY-E2E-01",
                "target_branch": "feature/ws-replay-e2e-01",
                "target_worktree_path": str(git_repo),
                "task_plan_path": None,
                "objective": "drained from the spool",
                "status": "in_progress",
                "branch": "feature/ws-replay-e2e-01",
            }
        )
        + "\n"
    )

    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = real_handoff_cli

    proc = subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "project-events-replay", "--json"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    receipt = json.loads(proc.stdout)
    assert receipt["drained"] == 1, receipt
    assert receipt["pending_remaining"] == 0, receipt
    assert receipt["handoff_projection"] == "synced", receipt

    # The row actually landed in the canonical DB...
    rows = _read_handoff_rows(git_repo)
    matching = [r for r in rows if r["task_ref"] == "WS-REPLAY-E2E-01"]
    assert matching, (
        f"replay reported drained but no row landed; rows={[dict(r) for r in rows]}"
    )
    assert matching[0]["status"] == "in_progress", dict(matching[0])
    assert matching[0]["objective"] == "drained from the spool", dict(matching[0])
    # ...and the spool is emptied.
    assert not spool.exists() or spool.read_text() == ""
