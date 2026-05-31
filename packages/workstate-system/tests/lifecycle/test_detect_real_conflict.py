"""WORKSTATE-REF-66 implementation note — `_detect_real_conflict` helper unit tests.

The helper is the pure-function replacement for the
``workspace_ambiguous`` veto at ``task_start.py:291-306``. It returns
``None`` for the non-conflict case (explicit fresh task whose target
branch + worktree path are unclaimed) and a ``_RealConflict`` for the
four real-conflict kinds:

* ``same_task_elsewhere`` — the requested task_ref is already live in
  a different worktree (policy conflict).
* ``branch_collision`` — the requested target_branch is attached to a
  different worktree (resource collision).
* ``worktree_path_collision`` — the derived worktree path already
  exists and is attached to a different task (resource collision).
* ``mode_here_implementation_conflict`` — ``MODE=here`` against a
  primary checkout currently attached to a different implementation
  (non-``main``-target) task (policy conflict).

These tests target the helper directly (not the CLI), so they
sys.path-prepend the lifecycle package to import
``handlers.task_start`` per the WORKSTATE-REF-66 plan (PR-04 fix).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
if str(LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG))

from handlers import task_start  # noqa: WORKSTATE-REF-402


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def primary_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "primary"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-m",
            "init",
            "-q",
        ],
        check=True,
    )
    return repo


def _live_task(
    task_ref: str,
    *,
    target_branch: str,
    target_worktree_path: str = "",
) -> dict[str, object]:
    return {
        "task_ref": task_ref,
        "status": "in_progress",
        "target_branch": target_branch,
        "target_worktree_path": target_worktree_path,
    }


# ---------------------------------------------------------------------------
# (a) Non-conflict — explicit fresh task, unclaimed target
# ---------------------------------------------------------------------------


def test_explicit_fresh_task_with_unclaimed_target_returns_none(
    primary_repo: Path,
) -> None:
    """The motivating WORKSTATE-REF-66 case: workspace lists two live siblings but
    the requested task_ref + target_branch + worktree path are all unclaimed.
    """
    live_tasks = [
        _live_task("WORKSTATE-REF-54-FU", target_branch="feature/WORKSTATE-54-fu"),
        _live_task("WORKSTATE-REF-99", target_branch="feature/WORKSTATE-99"),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is None


# ---------------------------------------------------------------------------
# (b) same_task_elsewhere — policy conflict
# ---------------------------------------------------------------------------


def test_same_task_already_live_elsewhere_refuses(primary_repo: Path) -> None:
    live_tasks = [
        _live_task(
            "WORKSTATE-REF-66",
            target_branch="feature/WORKSTATE-66",
            target_worktree_path=str(primary_repo.parent / "other-worktree"),
        ),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is not None
    assert result.kind == "same_task_elsewhere"
    assert result.category == "policy"
    assert result.conflicting_task_ref == "WORKSTATE-REF-66"


# ---------------------------------------------------------------------------
# (c) branch_collision — resource collision
# ---------------------------------------------------------------------------


def test_existing_worktree_with_no_live_owner_is_claim_candidate(
    primary_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-05 implementation note: the requested task's own branch already has a
    linked worktree but no live row owns it. This is the May-21 incident
    shape — the worktree is valid and should be claimed, not refused as a
    generic collision.
    """
    other_worktree = tmp_path / "other-worktree"
    _git(
        primary_repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "feature/WORKSTATE-66",
        str(other_worktree),
    )

    live_tasks = [
        _live_task("WORKSTATE-REF-99", target_branch="feature/WORKSTATE-99"),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is not None
    assert result.kind == "claim_existing_worktree"
    assert result.category == "recoverable"
    assert result.conflicting_branch == "feature/WORKSTATE-66"
    assert result.conflicting_path == str(other_worktree)


def test_existing_worktree_owned_by_other_live_row_still_branch_collision(
    primary_repo: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-05 implementation note: a live row for a *different* task already owns the
    worktree path the request would claim. That is an unsafe collision,
    not a claim candidate — it must keep hard-blocking.
    """
    other_worktree = tmp_path / "other-worktree"
    _git(
        primary_repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "feature/WORKSTATE-66",
        str(other_worktree),
    )

    live_tasks = [
        _live_task(
            "WORKSTATE-REF-77",
            target_branch="feature/WORKSTATE-77",
            target_worktree_path=str(other_worktree),
        ),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is not None
    assert result.kind == "branch_collision"
    assert result.category == "collision"
    assert result.conflicting_task_ref == "WORKSTATE-REF-77"


def test_branch_collision_consults_git_branch_list_even_without_worktree(
    primary_repo: Path,
) -> None:
    """PR-05: branch_collision detection must consult both
    `_find_linked_worktree_for_branch` AND `git branch --list <branch>` so
    a dangling local branch (no worktree attached) still trips the guard.
    """
    _git(primary_repo, "branch", "feature/WORKSTATE-66")

    live_tasks: list[dict[str, object]] = []
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is not None
    assert result.kind == "branch_collision"
    assert result.category == "collision"


# ---------------------------------------------------------------------------
# (d) worktree_path_collision — resource collision
# ---------------------------------------------------------------------------


def test_worktree_path_already_claimed_refuses(
    primary_repo: Path, tmp_path: Path
) -> None:
    """The derived worktree path (sibling-of-primary convention) is
    already attached to a different task's worktree."""
    derived = task_start._derive_worktree_path(primary_repo, "WORKSTATE-REF-66")
    _git(
        primary_repo, "worktree", "add", "-q", "-b", "feature/some-other", str(derived)
    )

    live_tasks: list[dict[str, object]] = []
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is not None
    assert result.kind == "worktree_path_collision"
    assert result.category == "collision"
    assert result.conflicting_path == str(derived)


# ---------------------------------------------------------------------------
# (e) mode_here_implementation_conflict — policy conflict
# ---------------------------------------------------------------------------


def test_mode_here_against_implementation_primary_refuses(primary_repo: Path) -> None:
    """MODE=here, primary checkout currently on a different implementation
    task's branch (non-`main` target). Worktree-singleton-class refusal.
    """
    _git(primary_repo, "checkout", "-q", "-b", "feature/WORKSTATE-99")

    live_tasks = [
        _live_task(
            "WORKSTATE-REF-99",
            target_branch="feature/WORKSTATE-99",
            target_worktree_path=str(primary_repo),
        ),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="here",
        live_tasks=live_tasks,
    )
    assert result is not None
    assert result.kind == "mode_here_implementation_conflict"
    assert result.category == "policy"
    assert result.conflicting_task_ref == "WORKSTATE-REF-99"


def test_mode_here_against_planning_primary_allows(primary_repo: Path) -> None:
    """MODE=here while primary is on `main` with a planning/maintenance
    task active (target_branch == "main"). WORKSTATE-REF-54 OQ2 case 4 parity:
    planning does not displace, so no conflict.
    """
    live_tasks = [
        _live_task(
            "WORKSTATE-REF-DEMO-01",
            target_branch="main",
            target_worktree_path=str(primary_repo),
        ),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="here",
        live_tasks=live_tasks,
    )
    assert result is None


# ---------------------------------------------------------------------------
# (f) Legitimate reuse — same task already has its own linked worktree
# (WORKSTATE-REF-66-BR-01 regression: `_detect_real_conflict` must NOT fire
# `same_task_elsewhere` when the listed live row's
# `target_worktree_path` matches the existing linked worktree for
# `target_branch`. That state is the canonical "resume in own
# worktree" case; refusing it kills the downstream
# `_find_linked_worktree_for_branch` reuse path in task_start.py.)
# ---------------------------------------------------------------------------


def test_same_task_with_matching_existing_worktree_allows_reuse(
    primary_repo: Path, tmp_path: Path
) -> None:
    """workspace_ambiguous summary lists WORKSTATE-REF-66 with target_worktree_path
    set to an existing linked worktree for `feature/WORKSTATE-66`. This is a
    legitimate resume — the helper must return None so the caller's
    downstream reuse path can pick up the existing worktree.
    """
    reuse_worktree = tmp_path / "primary-WORKSTATE-66"
    _git(
        primary_repo,
        "worktree",
        "add",
        "-q",
        "-b",
        "feature/WORKSTATE-66",
        str(reuse_worktree),
    )

    live_tasks = [
        _live_task(
            "WORKSTATE-REF-66",
            target_branch="feature/WORKSTATE-66",
            target_worktree_path=str(reuse_worktree),
        ),
        _live_task("WORKSTATE-REF-99", target_branch="feature/WORKSTATE-99"),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is None


def test_mode_worktree_ignores_primary_head_attachment(primary_repo: Path) -> None:
    """MODE=worktree never refuses on primary-HEAD state — its conflicts
    are entirely about the target worktree path / branch claim (per the
    MODE=here decision table footnote in the plan)."""
    _git(primary_repo, "checkout", "-q", "-b", "feature/WORKSTATE-99")

    live_tasks = [
        _live_task(
            "WORKSTATE-REF-99",
            target_branch="feature/WORKSTATE-99",
            target_worktree_path=str(primary_repo),
        ),
    ]
    result = task_start._detect_real_conflict(
        primary_repo,
        primary=primary_repo,
        task_ref="WORKSTATE-REF-66",
        target_branch="feature/WORKSTATE-66",
        mode="worktree",
        live_tasks=live_tasks,
    )
    assert result is None
