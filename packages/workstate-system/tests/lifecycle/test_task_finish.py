"""Contract tests for the mutating ``task-finish`` subcommand.

The handler wraps the canonical close sequence (status=done -> archive ->
dashboard -> remove worktree -> delete merged branch) in a single Make
target. The fake ``mcp-workstate-handoff`` shim dispatches on the third
positional argv (``set`` / ``archive`` / ``render-handoff``) so each
test can pin failure to one stage and assert the receipt's ``events`` /
``warnings`` / ``error`` / ``worktree_status`` / ``branch_status``
match. Identity (``target_branch`` + ``target_worktree_path``) is
read directly from the ``handoff_state`` row by the handler — tests
seed that row via ``_seed_handoff_state`` rather than going through the
fake CLI, so identity lookup is exercised against the real explicit
``task_ref`` filter.
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


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


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
    return repo


def _run_task_finish(
    cwd: Path,
    fake_cli: Path | None,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if fake_cli is not None:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    else:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = "/nonexistent/no-such-binary-xyz"
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "task-finish", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


# Fake CLI dispatches on $3 (the subcommand after `--workspace-root <path>`).
# Each subcommand can be made to fail by setting the matching env var to "1".
# Identity is no longer read via this shim — tests seed the handoff DB
# directly (see ``_seed_handoff_state``) so the handler exercises its
# explicit-task_ref sqlite lookup path.
_FAKE_CLI_BODY = """#!/usr/bin/env bash
# argv: --workspace-root <path> <subcommand> ...
sub="$3"
case "$sub" in
  set)
    if [ "${FAKE_FAIL_SET:-0}" = "1" ]; then echo "set failed" >&2; exit 1; fi
    exit 0
    ;;
  archive)
    if [ "${FAKE_FAIL_ARCHIVE:-0}" = "1" ]; then echo "archive failed" >&2; exit 1; fi
    exit 0
    ;;
  render-handoff)
    if [ "${FAKE_FAIL_RENDER:-0}" = "1" ]; then echo "render failed" >&2; exit 1; fi
    exit 0
    ;;
  *)
    echo "unsupported fake subcommand: $sub" >&2
    exit 1
    ;;
esac
"""


def _seed_handoff_state(
    repo: Path,
    task_ref: str,
    *,
    target_branch: str = "",
    target_worktree_path: str = "",
) -> None:
    """Seed a row in ``handoff_state`` keyed by exact ``task_ref``.

    The schema mirrors the live agent-handoff DB closely enough for
    `_read_handoff_identity` to project ``target_branch`` and
    ``target_worktree_path``. Other columns are unused by the handler
    so we keep the table narrow.
    """
    import sqlite3
    state_dir = repo / ".task-state"
    state_dir.mkdir(exist_ok=True)
    db_path = state_dir / "handoff.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS handoff_state ("
            " task_ref TEXT PRIMARY KEY,"
            " target_branch TEXT,"
            " target_worktree_path TEXT)"
        )
        conn.execute(
            "INSERT OR REPLACE INTO handoff_state(task_ref, target_branch, target_worktree_path) VALUES (?, ?, ?)",
            (task_ref, target_branch, target_worktree_path),
        )
        conn.commit()


def test_task_finish_blank_task_ref_with_no_current_task_errors(
    git_repo: Path, tmp_path: Path
) -> None:
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(git_repo, fake_cli, "--task", "", "--json")
    assert proc.returncode != 0
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "task_ref_required"
    assert receipt["task_ref"] is None
    assert receipt["events"] == []


def test_task_finish_happy_path_skipped_unset_when_identity_lacks_path(
    git_repo: Path, tmp_path: Path
) -> None:
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == "task-finish"
    assert receipt["task_ref"] == "WORKSTATE-REF-99"
    assert receipt["worktree_status"] == "skipped_unset"
    assert receipt["target_worktree_path"] == ""
    assert receipt["open_lanes"] == []
    assert receipt["events"] == ["status_done_set", "archived", "dashboard_rendered"]
    assert receipt["warnings"] == []


def test_task_finish_skipped_primary_when_target_resolves_to_repo_root(
    git_repo: Path, tmp_path: Path
) -> None:
    _seed_handoff_state(
        git_repo, "WORKSTATE-REF-99", target_worktree_path=str(git_repo)
    )
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["worktree_status"] == "skipped_primary"
    # The primary repo path itself must remain present after a finish run.
    assert git_repo.exists()


def test_task_finish_set_status_failure_short_circuits_before_archive(
    git_repo: Path, tmp_path: Path
) -> None:
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    env_override = os.environ.copy()
    env_override["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    env_override["FAKE_FAIL_SET"] = "1"
    proc = subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "task-finish",
         "--task", "WORKSTATE-REF-99", "--json"],
        cwd=git_repo, capture_output=True, text=True, check=False, env=env_override,
    )
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "set_status_done_failed"
    assert receipt["events"] == []
    assert "stderr_summary" in receipt


def test_task_finish_archive_failure_records_status_event_then_errors(
    git_repo: Path, tmp_path: Path
) -> None:
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    env_override = os.environ.copy()
    env_override["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    env_override["FAKE_FAIL_ARCHIVE"] = "1"
    proc = subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "task-finish",
         "--task", "WORKSTATE-REF-99", "--json"],
        cwd=git_repo, capture_output=True, text=True, check=False, env=env_override,
    )
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "archive_failed"
    assert receipt["events"] == ["status_done_set"]


def test_task_finish_lane_close_skipped_warning_when_open_lane_in_db(
    git_repo: Path, tmp_path: Path
) -> None:
    """Open lane rows surface a `lane_close_skipped` warning but do not block close.

    `_open_lanes_for_task` reads `worktree_lanes` directly from the
    handoff DB because there is no orchestrator CLI for lane close.
    Pre-seed a row so the warning path is exercised.
    """
    import sqlite3
    state_dir = git_repo / ".task-state"
    state_dir.mkdir()
    db_path = state_dir / "handoff.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE worktree_lanes ("
            " lane_id TEXT PRIMARY KEY,"
            " task_ref TEXT NOT NULL,"
            " status TEXT)"
        )
        conn.execute(
            "INSERT INTO worktree_lanes(lane_id, task_ref, status) VALUES (?, ?, ?)",
            ("lane-1", "WORKSTATE-REF-99", "open"),
        )
        conn.commit()

    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["open_lanes"] == ["lane-1"]
    assert any("lane_close_skipped" in w for w in receipt["warnings"])


def test_task_finish_skipped_missing_when_target_path_does_not_exist(
    git_repo: Path, tmp_path: Path
) -> None:
    _seed_handoff_state(
        git_repo, "WORKSTATE-REF-99",
        target_worktree_path=str(tmp_path / "nope" / "missing-worktree"),
    )
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["worktree_status"] == "skipped_missing"


def _seed_merged_feature_branch(repo: Path, branch: str) -> None:
    """Create a feature branch whose tip is reachable from main (merged).

    `git branch -d` only refuses when the branch has commits not in HEAD,
    so a branch that points at main's tip is trivially merged.
    """
    _git(repo, "branch", branch)


def _seed_unmerged_feature_branch(repo: Path, branch: str) -> None:
    """Create a feature branch with a commit that main does not contain."""
    _git(repo, "checkout", "-q", "-b", branch)
    _git(
        repo,
        "-c", "user.email=t@t",
        "-c", "user.name=t",
        "commit", "--allow-empty", "-m", "feature work", "-q",
    )
    _git(repo, "checkout", "-q", "main")


def test_task_finish_deletes_merged_feature_branch(
    git_repo: Path, tmp_path: Path
) -> None:
    """After worktree removal, a fully-merged target_branch is deleted with `git branch -d`."""
    branch = "feature/example"
    _seed_merged_feature_branch(git_repo, branch)
    assert branch in _git(git_repo, "branch", "--list", branch)
    _seed_handoff_state(git_repo, "WORKSTATE-REF-99", target_branch=branch)

    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["target_branch"] == branch
    assert receipt["branch_status"] == "deleted"
    assert "feature_branch_deleted" in receipt["events"]
    assert _git(git_repo, "branch", "--list", branch) == ""


def test_task_finish_skips_unmerged_feature_branch(
    git_repo: Path, tmp_path: Path
) -> None:
    """`git branch -d` refuses unmerged branches; receipt reports skipped_unmerged and branch survives."""
    branch = "feature/wip"
    _seed_unmerged_feature_branch(git_repo, branch)
    _seed_handoff_state(git_repo, "WORKSTATE-REF-99", target_branch=branch)

    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["branch_status"] == "skipped_unmerged"
    assert "feature_branch_deleted" not in receipt["events"]
    assert branch in _git(git_repo, "branch", "--list", branch)


def test_task_finish_skips_missing_feature_branch(
    git_repo: Path, tmp_path: Path
) -> None:
    """Identity names a branch that does not exist locally — no-op, no warning."""
    _seed_handoff_state(
        git_repo, "WORKSTATE-REF-99", target_branch="feature/never-existed"
    )
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["branch_status"] == "skipped_missing"


def test_task_finish_skips_unset_target_branch(
    git_repo: Path, tmp_path: Path
) -> None:
    """When identity has no target_branch, branch_status is skipped_unset."""
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["branch_status"] == "skipped_unset"


def test_task_finish_refuses_to_delete_main_branch(
    git_repo: Path, tmp_path: Path
) -> None:
    """target_branch == HEAD branch must never be deleted."""
    _seed_handoff_state(git_repo, "WORKSTATE-REF-99", target_branch="main")
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["branch_status"] == "skipped_primary"
    assert "main" in _git(git_repo, "branch", "--list", "main")


def test_task_finish_identity_lookup_uses_explicit_task_ref(
    git_repo: Path, tmp_path: Path
) -> None:
    """Identity lookup must filter the handoff DB by explicit ``task_ref``.

    Two rows live in ``handoff_state``: WORKSTATE-REF-99 (the requested task) and
    WORKSTATE-REF-OTHER (a distractor). The handler is invoked for WORKSTATE-REF-99 — the
    receipt's ``target_branch`` / ``target_worktree_path`` must reflect
    WORKSTATE-REF-99's row, never the distractor's, regardless of cwd-active
    resolution semantics.
    """
    requested_branch = "feature/explicit-target"
    distractor_branch = "feature/cwd-resolved"
    _seed_handoff_state(
        git_repo, "WORKSTATE-REF-99",
        target_branch=requested_branch,
        target_worktree_path=str(tmp_path / "explicit-wt"),
    )
    _seed_handoff_state(
        git_repo, "WORKSTATE-REF-OTHER",
        target_branch=distractor_branch,
        target_worktree_path=str(tmp_path / "distractor-wt"),
    )
    _seed_merged_feature_branch(git_repo, requested_branch)

    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["target_branch"] == requested_branch
    assert receipt["target_worktree_path"] == str(tmp_path / "explicit-wt")
    assert receipt["branch_status"] == "deleted"
    # The distractor must remain untouched — never resolved by cwd fallback.
    assert distractor_branch not in _git(git_repo, "branch", "--list", distractor_branch)


def test_task_finish_identity_returns_empty_when_db_absent(
    git_repo: Path, tmp_path: Path
) -> None:
    """Missing ``.task-state/handoff.db`` collapses to skip semantics."""
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _FAKE_CLI_BODY)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["target_branch"] == ""
    assert receipt["target_worktree_path"] == ""
    assert receipt["branch_status"] == "skipped_unset"
    assert receipt["worktree_status"] == "skipped_unset"


def test_task_finish_runs_checklist_sync_before_archive(
    git_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-70 implementation note.

    The final full-plan checklist sweep must run BEFORE the archive
    call so the sync's plan-path lookup (which goes through ``handoff
    state``) still sees the active row. After archive the row moves to
    ``task_archives`` and the active-state lookup returns nothing —
    sync degrades to ``plan_unresolved`` and never sweeps.

    Verifies ordering by routing the fake CLI through a recording shim:
    every CLI invocation logs the subcommand to a file, and the test
    asserts the first ``state`` (sync's lookup) call precedes the
    ``archive`` call.
    """
    call_log = tmp_path / "calls.log"
    body = (
        "#!/usr/bin/env bash\n"
        f"echo \"$3\" >> {call_log}\n"
        "sub=\"$3\"\n"
        "case \"$sub\" in\n"
        "  set|archive|render-handoff) exit 0 ;;\n"
        # ``state`` is what sync_task_plan_checklist's plan-path lookup
        # hits; return an empty active block so sync degrades gracefully
        # but the call is still recorded for ordering verification.
        "  state) echo '{\"data\": {\"active\": {}}}'; exit 0 ;;\n"
        "  get-verified-tests) echo '{\"data\": {\"tests\": []}}'; exit 0 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    fake_cli = tmp_path / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, body)
    proc = _run_task_finish(
        git_repo, fake_cli, "--task", "WORKSTATE-REF-99", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    log = call_log.read_text().splitlines() if call_log.exists() else []
    assert "archive" in log, log
    assert "state" in log, (
        f"sync subprocess must call ``state`` for plan-path lookup; got: {log}"
    )
    state_idx = log.index("state")
    archive_idx = log.index("archive")
    assert state_idx < archive_idx, (
        f"checklist sync state lookup must precede archive; got: {log}"
    )
