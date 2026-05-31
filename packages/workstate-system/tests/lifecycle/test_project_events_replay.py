"""implementation note.2 contract tests for ``project-events-replay``.

Drains ``.task-state/pending-workflow-events.jsonl`` (written by
:func:`projection.project_test_result` and friends when the handoff
adapter is offline) by replaying each entry through the canonical
handoff CLI in original order, then rewriting the file with only
undrained entries.

Receipt extras (over the required base):

* ``drained``: ``int`` — number of entries successfully replayed.
* ``pending_remaining``: ``int`` — entries left in the spool after the
  replay run.
* ``replay_results``: ``list[{"kind": str, "status": "synced" | "pending"}]``
  — per-entry outcome in original order.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
PENDING_REL = Path(".task-state") / "pending-workflow-events.jsonl"

REQUIRED_FIELDS = (
    "ok",
    "command",
    "task_ref",
    "branch",
    "worktree_path",
    "head",
    "handoff_projection",
    "events",
)
REPLAY_EXTRA_FIELDS = ("drained", "pending_remaining", "replay_results")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


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
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature/WORKSTATE-21"],
        check=True,
    )
    return repo


def _run_replay(
    cwd: Path,
    fake_cli: Path | None,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = (
        str(fake_cli) if fake_cli is not None else "/nonexistent/no-such-binary-xyz"
    )
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "project-events-replay", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _split_argv_blocks(log_path: Path) -> list[list[str]]:
    blocks: list[list[str]] = []
    current: list[str] = []
    if not log_path.exists():
        return blocks
    for line in log_path.read_text().splitlines():
        if line == "---":
            blocks.append(current)
            current = []
        else:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def test_replay_drains_single_test_result(
    git_repo: Path, tmp_path: Path
) -> None:
    """One spooled test_result entry → CLI invoked once → spool emptied."""
    spool = git_repo / PENDING_REL
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(
        json.dumps(
            {
                "kind": "test_result",
                "session": "claude_offline_session",
                "command": "pytest -q",
                "passed": False,
                "exit_code": 1,
                "result": None,
            }
        )
        + "\n"
    )
    fake_cli = tmp_path / "fake-mcp"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )

    proc = _run_replay(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    for field in (*REQUIRED_FIELDS, *REPLAY_EXTRA_FIELDS):
        assert field in receipt, f"missing field {field!r}: {receipt!r}"
    assert receipt["ok"] is True
    assert receipt["command"] == "project-events-replay"
    assert receipt["drained"] == 1
    assert receipt["pending_remaining"] == 0
    assert receipt["replay_results"] == [
        {"kind": "test_result", "status": "synced"}
    ]
    assert "events_replayed" in receipt["events"]
    assert receipt["handoff_projection"] == "synced"
    # Spool emptied (file removed or zero-length).
    assert not spool.exists() or spool.read_text() == ""

    blocks = _split_argv_blocks(argv_log)
    event_blocks = [b for b in blocks if "event" in b]
    assert len(event_blocks) == 1, blocks
    args = event_blocks[0]
    assert "--event-kind" in args
    assert args[args.index("--event-kind") + 1] == "test_result"
    assert "--session" in args
    assert args[args.index("--session") + 1] == "claude_offline_session"
    assert "--command" in args
    assert args[args.index("--command") + 1] == "pytest -q"
    assert "--exit-code" in args
    assert args[args.index("--exit-code") + 1] == "1"
    # passed=False → no --passed flag (store_true).
    assert "--passed" not in args


def test_replay_forwards_branch_and_commit_overrides(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note.2.1: a spooled test_result entry that carries the
    worktree's branch + commit_sha must be replayed with matching
    --branch / --commit-sha actor overrides, so verified_tests rows
    keep the same provenance they would have had online. Regression
    for finding 152 (codex_WORKSTATE40_slice4_2_replay_drops_test_provenance).
    """
    spool = git_repo / PENDING_REL
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(
        json.dumps(
            {
                "kind": "test_result",
                "session": "claude_offline_session",
                "command": "pytest -q",
                "passed": True,
                "exit_code": 0,
                "result": None,
                "branch": "feature/WORKSTATE-99",
                "commit_sha": "deadbeef" * 5,
            }
        )
        + "\n"
    )
    fake_cli = tmp_path / "fake-mcp"
    argv_log = tmp_path / "argv.log"
    _write_fake_cli(
        fake_cli,
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\n'
        f'echo "---" >> {argv_log}\nexit 0\n',
    )

    proc = _run_replay(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["drained"] == 1
    assert receipt["pending_remaining"] == 0

    blocks = _split_argv_blocks(argv_log)
    event_blocks = [b for b in blocks if "event" in b]
    assert len(event_blocks) == 1, blocks
    args = event_blocks[0]
    assert "--branch" in args
    assert args[args.index("--branch") + 1] == "feature/WORKSTATE-99"
    assert "--commit-sha" in args
    assert args[args.index("--commit-sha") + 1] == "deadbeef" * 5


def test_replay_no_spool_returns_zero_drained(
    git_repo: Path, tmp_path: Path
) -> None:
    """Missing spool file is the steady-state: replay reports zero work."""
    fake_cli = tmp_path / "fake-mcp"
    _write_fake_cli(fake_cli, '#!/usr/bin/env bash\nexit 0\n')

    proc = _run_replay(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == "project-events-replay"
    assert receipt["drained"] == 0
    assert receipt["pending_remaining"] == 0
    assert receipt["replay_results"] == []
    assert receipt["handoff_projection"] == "synced"


def test_replay_keeps_undrained_entries_when_cli_fails(
    git_repo: Path, tmp_path: Path
) -> None:
    """Adapter rejected (exit 1) → entry stays in spool, receipt reports
    ``spooled`` (WORKSTATE-REF-52 implementation note split: rejection is loud, unreachability
    is the transient ``pending`` case pinned in the missing-CLI test).
    """
    spool = git_repo / PENDING_REL
    spool.parent.mkdir(parents=True, exist_ok=True)
    spool.write_text(
        json.dumps(
            {
                "kind": "test_result",
                "session": "claude_offline_session",
                "command": "pytest -q",
                "passed": True,
                "exit_code": 0,
                "result": None,
            }
        )
        + "\n"
    )
    fake_cli = tmp_path / "fake-mcp"
    _write_fake_cli(fake_cli, '#!/usr/bin/env bash\nexit 1\n')

    proc = _run_replay(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["drained"] == 0
    assert receipt["pending_remaining"] == 1
    assert receipt["replay_results"] == [
        {"kind": "test_result", "status": "spooled"}
    ]
    assert receipt["handoff_projection"] == "spooled"
    # Spool retains the original line.
    remaining = [json.loads(line) for line in spool.read_text().splitlines() if line]
    assert len(remaining) == 1
    assert remaining[0]["session"] == "claude_offline_session"
