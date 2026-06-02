"""WORKSTATE-REF-02 implementation note — `RuntimeConfig.from_args` falls back to cwd when neither
`--workspace-root` nor `WORKSTATE_HANDOFF_WORKSPACE_ROOT` is set.

When the CLI is invoked from inside any git worktree (primary or linked) with
no explicit workspace pointer, the runtime should walk from `Path.cwd()` to
the primary worktree root for state storage and use the caller's cwd as the
git provenance root. This preserves the WORKSTATE-REF-01 split between
`state_workspace_root` (collapsed to primary) and `git_workspace_root`
(caller's actual worktree). When cwd is not inside any git repo, the
existing loud `RuntimeError` is retained.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from workstate_handoff_mcp.config import RuntimeConfig


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def git_repo_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    primary = tmp_path / "primary"
    primary.mkdir()
    _run_git(primary, "init", "-q", "-b", "main")
    _run_git(primary, "config", "user.email", "test@example.com")
    _run_git(primary, "config", "user.name", "Test User")
    _run_git(primary, "commit", "--allow-empty", "-m", "init", "-q")
    linked = tmp_path / "primary-feature"
    _run_git(primary, "branch", "feature/test")
    _run_git(primary, "worktree", "add", "-q", str(linked), "feature/test")
    return primary, linked


class _NoFlagArgs:
    """argparse.Namespace stand-in where every relevant attribute is unset.

    Mirrors the situation where the CLI is invoked with no `--workspace-root`
    and no path-override flags, and the environment is cleared.
    """

    workspace_root = None
    state_dir = None
    current_task_path = None
    dashboard_path = None
    exports_dir = None
    tool_profile = None


def test_from_args_cwd_fallback_resolves_primary_worktree(
    git_repo_with_linked_worktree: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`from_args` with no flags / env and cwd inside the primary worktree
    binds state to the primary root and git provenance to the same root."""
    primary, _linked = git_repo_with_linked_worktree
    monkeypatch.chdir(primary)

    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.from_args(_NoFlagArgs())

    assert runtime.state_workspace_root == primary.resolve()
    assert runtime.git_workspace_root == primary.resolve()
    assert runtime.db_path == primary.resolve() / ".task-state" / "handoff.db"


def test_from_args_cwd_fallback_collapses_linked_worktree_to_primary(
    git_repo_with_linked_worktree: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`from_args` with no flags / env and cwd inside a *linked* worktree
    still collapses state to the primary root while preserving the linked
    worktree as the git provenance root. This is the post-WORKSTATE-REF-01 split:
    one shared `.task-state/handoff.db` per physical repo, per-cwd git
    detection for write attribution."""
    primary, linked = git_repo_with_linked_worktree
    monkeypatch.chdir(linked)

    with mock.patch.dict(os.environ, {}, clear=True):
        runtime = RuntimeConfig.from_args(_NoFlagArgs())

    assert runtime.state_workspace_root == primary.resolve()
    assert runtime.git_workspace_root == linked.resolve()
    assert runtime.db_path == primary.resolve() / ".task-state" / "handoff.db"


def test_from_args_cwd_fallback_raises_outside_any_git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When neither `--workspace-root` nor `WORKSTATE_HANDOFF_WORKSPACE_ROOT` is
    set and cwd is not inside any git repo, retain the existing loud
    `RuntimeError`. No silent default-to-`$HOME` or to-cwd-as-workspace."""
    not_a_repo = tmp_path / "scratch"
    not_a_repo.mkdir()
    monkeypatch.chdir(not_a_repo)

    with mock.patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError) as excinfo:
            RuntimeConfig.from_args(_NoFlagArgs())

    message = str(excinfo.value)
    assert "WORKSTATE_HANDOFF_WORKSPACE_ROOT" in message or "could not resolve" in message
