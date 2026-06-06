"""implementation note (implementation note / WS-ERRTEL-01): errors-record CLI + direct-SQLite path.

The harness hook writes through ``mcp-workstate-handoff errors-record``,
which uses a *direct* SQLite path instead of the configured runtime:

- primary DB resolved via ``git rev-parse --path-format=absolute
  --git-common-dir`` so linked worktrees write to the primary repo's
  ``.task-state/handoff.db``, never cwd-local state
- WAL + busy_timeout, one insert/update transaction
- ``PRAGMA user_version`` must equal the schema version this package
  expects; otherwise the redacted event is appended to
  ``.task-state/agent-errors-spool.jsonl`` for later replay
- same 10-minute dedup upsert as server self-capture
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.agent_errors import record_agent_error_direct, replay_agent_error_spool
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import HANDOFF_SCHEMA_VERSION, _get_db_connection


@pytest.fixture()
def seeded_repo(tmp_path: Path) -> Path:
    """A git repo with a bootstrapped .task-state/handoff.db at current schema."""

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=tmp_path / ".task-state",
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    with _get_db_connection():
        pass  # bootstrap schema
    return tmp_path


def _rows(db_path: Path) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM agent_errors ORDER BY id ASC").fetchall()
    finally:
        conn.close()


def _spool_lines(repo: Path) -> list[dict]:
    spool = repo / ".task-state" / "agent-errors-spool.jsonl"
    if not spool.exists():
        return []
    return [json.loads(line) for line in spool.read_text().splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Direct write path
# ---------------------------------------------------------------------------


def test_direct_write_inserts_row(seeded_repo: Path) -> None:
    result = record_agent_error_direct(
        error_class="install_drift",
        summary="ImportError: cannot import name 'list_handoff_rows' from 'workstate_handoff_mcp'",
        package_name="workstate_handoff_mcp",
        package_version="0.1.0",
        tool_name="Bash",
        harness="claude",
        cwd=seeded_repo,
    )
    assert result["ok"] is True
    assert result["mode"] == "db"

    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert len(rows) == 1
    row = rows[0]
    assert row["error_class"] == "install_drift"
    assert row["package_name"] == "workstate_handoff_mcp"
    assert row["package_version"] == "0.1.0"
    assert row["harness"] == "claude"
    assert row["repo_instance_id"]


def test_direct_write_resolves_primary_db_from_linked_worktree(seeded_repo: Path) -> None:
    subprocess.run(["git", "-C", str(seeded_repo), "commit", "--allow-empty", "-q", "-m", "seed"], check=True)
    worktree = seeded_repo.parent / "linked-wt"
    subprocess.run(
        ["git", "-C", str(seeded_repo), "worktree", "add", "-q", str(worktree), "-b", "wt-branch"],
        check=True,
    )

    result = record_agent_error_direct(
        error_class="cli_failure",
        summary="make task-start exited 2",
        cwd=worktree,
    )
    assert result["ok"] is True
    assert result["mode"] == "db"

    # Row landed in the PRIMARY repo DB; no cwd-local state was created.
    assert len(_rows(seeded_repo / ".task-state" / "handoff.db")) == 1
    assert not (worktree / ".task-state").exists()


def test_direct_write_dedup_increments_occurrence(seeded_repo: Path) -> None:
    for _ in range(2):
        record_agent_error_direct(error_class="cli_failure", summary="same failure", cwd=seeded_repo)
    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert len(rows) == 1
    assert rows[0]["occurrence_count"] == 2


def test_direct_write_redacts_secrets(seeded_repo: Path) -> None:
    record_agent_error_direct(
        error_class="cli_failure",
        summary="curl --token=supersecret123 failed",
        detail="env API_KEY=abcd1234 leaked",
        cwd=seeded_repo,
    )
    row = _rows(seeded_repo / ".task-state" / "handoff.db")[0]
    assert "supersecret123" not in row["summary"]
    assert "abcd1234" not in row["detail"]


def test_direct_write_rejects_bad_error_class(seeded_repo: Path) -> None:
    result = record_agent_error_direct(error_class="Not Valid!", summary="x", cwd=seeded_repo)
    assert result["ok"] is False
    assert _rows(seeded_repo / ".task-state" / "handoff.db") == []


# ---------------------------------------------------------------------------
# Schema-version guard -> spool
# ---------------------------------------------------------------------------


def test_stale_schema_spools_instead_of_writing(seeded_repo: Path) -> None:
    db_path = seeded_repo / ".task-state" / "handoff.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {HANDOFF_SCHEMA_VERSION + 1}")  # DB newer than package
    conn.close()

    result = record_agent_error_direct(
        error_class="install_drift",
        summary="stale package vs newer db",
        cwd=seeded_repo,
    )
    assert result["ok"] is True
    assert result["mode"] == "spool"

    lines = _spool_lines(seeded_repo)
    assert len(lines) == 1
    assert lines[0]["error_class"] == "install_drift"
    # No row written to the mismatched DB.
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    count = conn.execute("SELECT COUNT(*) AS n FROM agent_errors").fetchone()["n"]
    conn.close()
    assert count == 0


def test_missing_db_spools(seeded_repo: Path) -> None:
    (seeded_repo / ".task-state" / "handoff.db").unlink()
    result = record_agent_error_direct(error_class="other", summary="no db yet", cwd=seeded_repo)
    assert result["ok"] is True
    assert result["mode"] == "spool"
    assert len(_spool_lines(seeded_repo)) == 1


def test_spooled_event_is_redacted(seeded_repo: Path) -> None:
    (seeded_repo / ".task-state" / "handoff.db").unlink()
    record_agent_error_direct(
        error_class="other",
        summary="curl --token=supersecret123 failed",
        cwd=seeded_repo,
    )
    line = _spool_lines(seeded_repo)[0]
    assert "supersecret123" not in line["summary"]


def test_non_git_cwd_returns_not_ok(tmp_path: Path) -> None:
    plain_dir = tmp_path / "not-a-repo"
    plain_dir.mkdir()
    result = record_agent_error_direct(error_class="other", summary="x", cwd=plain_dir)
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# CLI subcommand
# ---------------------------------------------------------------------------


def test_errors_record_cli_subprocess(seeded_repo: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_handoff_mcp",
            "errors-record",
            "--error-class",
            "install_drift",
            "--summary",
            "ImportError: cannot import name 'list_handoff_rows' from 'workstate_handoff_mcp'",
            "--package-name",
            "workstate_handoff_mcp",
            "--tool-name",
            "Bash",
            "--harness",
            "claude",
            "--task-ref",
            "WS-ERRTEL-01",
        ],
        cwd=seeded_repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["mode"] == "db"

    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert len(rows) == 1
    assert rows[0]["error_class"] == "install_drift"
    assert rows[0]["task_ref"] == "WS-ERRTEL-01"


def test_errors_record_cli_invalid_class_exits_zero(seeded_repo: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_handoff_mcp",
            "errors-record",
            "--error-class",
            "Bad Class",
            "--summary",
            "x",
        ],
        cwd=seeded_repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    # Telemetry surface: never fails the caller; failure is in the JSON.
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is False
    assert _rows(seeded_repo / ".task-state" / "handoff.db") == []


# ---------------------------------------------------------------------------
# Spool replay (REV-B-001)
# ---------------------------------------------------------------------------


def test_replay_spool_drains_into_db(seeded_repo: Path) -> None:
    db_path = seeded_repo / ".task-state" / "handoff.db"
    db_path.unlink()
    record_agent_error_direct(error_class="install_drift", summary="spooled one", cwd=seeded_repo)
    record_agent_error_direct(error_class="cli_failure", summary="spooled two", cwd=seeded_repo)
    assert len(_spool_lines(seeded_repo)) == 2

    # Recreate the DB at the current schema, then replay.
    runtime = RuntimeConfig.for_workspace(
        seeded_repo,
        state_dir=seeded_repo / ".task-state",
        current_task_path=seeded_repo / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    with _get_db_connection():
        pass  # bootstrap schema

    result = replay_agent_error_spool(cwd=seeded_repo)
    assert result["ok"] is True
    assert result["replayed"] == 2
    assert result["remaining"] == 0
    # Drained spool is removed.
    assert not (seeded_repo / ".task-state" / "agent-errors-spool.jsonl").exists()
    rows = _rows(db_path)
    assert sorted(row["error_class"] for row in rows) == ["cli_failure", "install_drift"]


def test_replay_spool_keeps_malformed_lines(seeded_repo: Path) -> None:
    spool = seeded_repo / ".task-state" / "agent-errors-spool.jsonl"
    valid = json.dumps({"error_class": "other", "summary": "valid spooled event"})
    spool.write_text(
        valid + "\n" + "not json at all\n" + json.dumps({"error_class": "Bad Class", "summary": "x"}) + "\n"
    )

    result = replay_agent_error_spool(cwd=seeded_repo)
    assert result["ok"] is True
    assert result["replayed"] == 1
    assert result["remaining"] == 2
    kept = spool.read_text().splitlines()
    assert "not json at all" in kept
    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert len(rows) == 1
    assert rows[0]["summary"] == "valid spooled event"


def test_replay_spool_respects_schema_guard(seeded_repo: Path) -> None:
    spool = seeded_repo / ".task-state" / "agent-errors-spool.jsonl"
    spool.write_text(json.dumps({"error_class": "other", "summary": "still stale"}) + "\n")
    db_path = seeded_repo / ".task-state" / "handoff.db"
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {HANDOFF_SCHEMA_VERSION + 1}")
    conn.close()

    result = replay_agent_error_spool(cwd=seeded_repo)
    assert result["ok"] is False
    assert "schema_version_mismatch" in result["error"]
    # Spool left untouched for a future matching install.
    assert len(_spool_lines(seeded_repo)) == 1


def test_replay_spool_no_spool_is_ok(seeded_repo: Path) -> None:
    result = replay_agent_error_spool(cwd=seeded_repo)
    assert result["ok"] is True
    assert result["replayed"] == 0
    assert result["reason"] == "no_spool"


def test_errors_replay_spool_cli_subprocess(seeded_repo: Path) -> None:
    spool = seeded_repo / ".task-state" / "agent-errors-spool.jsonl"
    spool.write_text(json.dumps({"error_class": "env_misconfig", "summary": "from cli replay"}) + "\n")
    proc = subprocess.run(
        [sys.executable, "-m", "workstate_handoff_mcp", "errors-replay-spool"],
        cwd=seeded_repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["replayed"] == 1
    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert len(rows) == 1
    assert rows[0]["error_class"] == "env_misconfig"


def test_replay_spool_preserves_spooled_at_as_created_at(seeded_repo: Path) -> None:
    """Harvest first-seen provenance survives a delayed replay (REV-D-006)."""
    spool = seeded_repo / ".task-state" / "agent-errors-spool.jsonl"
    spool.write_text(
        json.dumps(
            {
                "error_class": "install_drift",
                "summary": "delayed replay provenance",
                "spooled_at": "2026-06-01 08:00:00",
            }
        )
        + "\n"
    )
    result = replay_agent_error_spool(cwd=seeded_repo)
    assert result["ok"] is True and result["replayed"] == 1
    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert rows[0]["created_at"] == "2026-06-01 08:00:00"
    assert rows[0]["last_seen_at"] >= rows[0]["created_at"]


def test_replay_spool_redacts_spooled_secrets(seeded_repo: Path) -> None:
    """A spool line edited/produced outside our writer is still redacted on replay (REV-D-005)."""
    spool = seeded_repo / ".task-state" / "agent-errors-spool.jsonl"
    spool.write_text(
        json.dumps(
            {
                "error_class": "other",
                "summary": "curl --token=replaysecret999 failed",
                "detail": "Authorization: Bearer replaysecret999",
            }
        )
        + "\n"
    )
    result = replay_agent_error_spool(cwd=seeded_repo)
    assert result["ok"] is True and result["replayed"] == 1
    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert "replaysecret999" not in rows[0]["summary"]
    assert "replaysecret999" not in (rows[0]["detail"] or "")


def test_replay_spool_path_override_targets_primary_db(seeded_repo: Path, tmp_path_factory) -> None:
    """--spool-path moves only the spool; the write still lands in the cwd's primary DB (REV-D-002)."""
    elsewhere = tmp_path_factory.mktemp("relocated")
    relocated = elsewhere / "relocated-spool.jsonl"
    relocated.write_text(json.dumps({"error_class": "other", "summary": "from relocated spool"}) + "\n")

    result = replay_agent_error_spool(cwd=seeded_repo, spool_path=relocated)
    assert result["ok"] is True and result["replayed"] == 1
    # Row landed in the seeded repo's primary DB, not next to the spool.
    rows = _rows(seeded_repo / ".task-state" / "handoff.db")
    assert len(rows) == 1 and rows[0]["summary"] == "from relocated spool"
    assert not (elsewhere / "handoff.db").exists()
    assert not relocated.exists()  # drained
