from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    tracked = repo / "tracked.txt"
    tracked.write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "-m", "init", "-q",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature/WORKSTATE-21"],
        check=True,
    )
    return repo


def _run_slice_commit(cwd: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    # WORKSTATE-REF-SLICE-COMMIT-PROJECTION-20260509: pin the projection CLI to a
    # nonexistent binary so the post-commit projection deterministically
    # falls through to the offline path. Without this, the real
    # ``mcp-workstate-handoff`` on PATH would init a ``.task-state/`` dir
    # inside the tmp git fixture and break the working-tree-clean
    # invariant these tests assert. Tests that exercise the projection
    # itself (synced path, argv shape) live in ``test_slice_commit_projection.py``.
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = "/nonexistent/no-such-binary-xyz"
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "slice-commit", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_slice_commit_stages_tracked_changes_and_creates_commit(git_repo: Path) -> None:
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")

    head_before = _git(git_repo, "rev-parse", "HEAD")
    proc = _run_slice_commit(git_repo, "--msg", "feat(workstate-system): test slice commit", "--json")

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == "slice-commit"
    assert receipt["task_ref"] == "WORKSTATE-REF-21"
    assert receipt["branch"] == "feature/WORKSTATE-21"
    assert receipt["commit_message"] == "feat(workstate-system): test slice commit"
    assert receipt["handoff_projection"] == "pending"
    assert "staged_tracked" in receipt["events"]
    assert "commit_created" in receipt["events"]
    assert receipt["previous_head"] == head_before
    assert receipt["dirty_summary"]["untracked"] == 0
    assert receipt["included_untracked"] is False
    assert len(receipt["commit_sha"]) == 40
    assert _git(git_repo, "status", "--short") == ""
    committed = set(_git(git_repo, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert {"tracked.txt"}.issubset(committed)


def test_slice_commit_refuses_untracked_files_without_opt_in(git_repo: Path) -> None:
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")
    created = git_repo / "created.txt"
    created.write_text("new\n", encoding="utf-8")

    proc = _run_slice_commit(git_repo, "--msg", "feat(workstate-system): test slice commit", "--json")

    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "untracked_files_present"
    assert receipt["dirty_summary"]["untracked"] == 1
    assert receipt["untracked_paths"] == ["created.txt"]
    assert receipt["included_untracked"] is False
    assert "?? created.txt" in _git(git_repo, "status", "--short")


def test_slice_commit_can_include_untracked_files_with_explicit_flag(git_repo: Path) -> None:
    tracked = git_repo / "tracked.txt"
    tracked.write_text("seed\nchanged\n", encoding="utf-8")
    created = git_repo / "created.txt"
    created.write_text("new\n", encoding="utf-8")

    proc = _run_slice_commit(
        git_repo,
        "--msg",
        "feat(workstate-system): include untracked",
        "--include-untracked",
        "--json",
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["dirty_summary"]["untracked"] == 1
    assert receipt["included_untracked"] is True
    assert "staged_all" in receipt["events"]
    committed = set(_git(git_repo, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert {"tracked.txt", "created.txt"}.issubset(committed)


def test_slice_commit_requires_msg(git_repo: Path) -> None:
    proc = _run_slice_commit(git_repo, "--json")

    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["command"] == "slice-commit"
    assert receipt["error"] == "msg_required"
