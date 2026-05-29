"""Repo-hygiene invariants for on-demand handoff renders.

CURRENT_TASK.json (since WORKSTATE-REF-44) and DASHBOARD.txt (since WORKSTATE-REF-51)
are renders of the handoff DB at a moment in time. Both are gitignored
and regenerated on demand via ``render_handoff(...)`` or atomically by
lifecycle paths (``close_slice``, ``archive_task_state``). Tracking
either in git would duplicate state already persisted in the DB and
force every task close to commit a refresh — a workflow that does not
scale across agents/harnesses without per-environment permission rules.

These tests guard the convention: the files MUST be ignored and MUST
NOT be tracked. Reintroducing either to the index is the regression
they catch.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ON_DEMAND_RENDER_PATHS = ("DASHBOARD.txt", "CURRENT_TASK.json")


def _repo_root(start: Path) -> Path:
    for candidate in (start, *start.parents):
        if (candidate / ".git").exists() and (candidate / "docs").is_dir() and (candidate / "packages").is_dir():
            return candidate
    raise AssertionError(f"could not resolve repo root from {start}")


REPO_ROOT = _repo_root(Path(__file__).resolve())


def test_on_demand_renders_are_gitignored() -> None:
    for relpath in ON_DEMAND_RENDER_PATHS:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", relpath],
            check=False,
        )
        assert proc.returncode == 0, (
            f"{relpath} is not gitignored at repo root — see "
            "WORKSTATE-REF-44 (CURRENT_TASK.json) / WORKSTATE-REF-51 (DASHBOARD.txt)."
        )


def test_on_demand_renders_are_not_tracked() -> None:
    for relpath in ON_DEMAND_RENDER_PATHS:
        proc = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", relpath],
            check=True,
            capture_output=True,
            text=True,
        )
        assert proc.stdout.strip() == "", (
            f"{relpath} is tracked in git but must be a render-on-demand "
            "artifact. Run `git rm --cached {relpath}` and confirm it is "
            "gitignored."
        )
