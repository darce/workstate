"""Tests for ``workstate_handoff_mcp.scripts.backfill_plan_paths`` (implementation note implementation note).

The backfill script walks active handoff rows, skips rows that already
have ``task_plan_path`` set, globs ``docs/**/*<task-id>*.md`` for the
remainder, and registers the unique match via ``register_plan_path``.
Multi-match and zero-match rows are reported and left untouched so the
operator can disambiguate (or pass an explicit override).

This module covers four behaviours:

1. Happy path: missing rows whose globs return a unique match get
   populated; the round-trip is observable via ``get_handoff_state``.
2. Idempotency: re-running on a fully-populated DB is a no-op.
3. Multi-match refusal: rows with multiple plan candidates are left
   unset with a hint naming the candidates.
4. Operator override (``--task TASK=path``) wins over the glob.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


@pytest.fixture()
def workspace_repo(tmp_path: Path) -> Path:
    _git("init", "--initial-branch=main", cwd=tmp_path)
    _git("config", "user.email", "backfill@test", cwd=tmp_path)
    _git("config", "user.name", "Backfill Test", cwd=tmp_path)
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    return tmp_path


def _seed_active_row(task_ref: str, branch: str, plan_path: str | None = None) -> None:
    kwargs: dict = {
        "task_ref": task_ref,
        "objective": f"backfill test {task_ref}",
        "status": "in_progress",
        "target_branch": branch,
    }
    if plan_path is not None:
        kwargs["task_plan_path"] = plan_path
    mcp_server.set_handoff_state(**kwargs)


def _write_plan(repo: Path, rel_path: str) -> None:
    abs_path = repo / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(f"# {Path(rel_path).name}\n")


def _active_plan_path(task_ref: str) -> str | None:
    envelope = mcp_server.get_handoff_state(task_ref=task_ref, sections="identity")
    active = envelope.get("data", {}).get("active") or {}
    raw = active.get("task_plan_path")
    return raw if isinstance(raw, str) and raw.strip() else None


def test_backfill_populates_missing_plan_paths(workspace_repo: Path) -> None:
    from workstate_handoff_mcp.scripts.backfill_plan_paths import main

    _write_plan(workspace_repo, "docs/plans/0099-WORKSTATE-99-demo.md")
    _write_plan(workspace_repo, "docs/plans/0100-WORKSTATE-100-other.md")
    _seed_active_row("WORKSTATE-REF-99", "feature/WORKSTATE-99-demo")
    _seed_active_row("WORKSTATE-REF-100", "feature/WORKSTATE-100-other")

    rc = main([])
    assert rc == 0
    assert _active_plan_path("WORKSTATE-REF-99") == "docs/plans/0099-WORKSTATE-99-demo.md"
    assert _active_plan_path("WORKSTATE-REF-100") == "docs/plans/0100-WORKSTATE-100-other.md"


def test_backfill_is_idempotent_on_populated_rows(workspace_repo: Path) -> None:
    from workstate_handoff_mcp.scripts.backfill_plan_paths import main

    _write_plan(workspace_repo, "docs/plans/0099-WORKSTATE-99-demo.md")
    _seed_active_row("WORKSTATE-REF-99", "feature/WORKSTATE-99-demo", plan_path="docs/plans/0099-WORKSTATE-99-demo.md")

    rc_first = main([])
    rc_second = main([])
    assert rc_first == 0
    assert rc_second == 0
    assert _active_plan_path("WORKSTATE-REF-99") == "docs/plans/0099-WORKSTATE-99-demo.md"


def test_backfill_skips_multi_match_rows(workspace_repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from workstate_handoff_mcp.scripts.backfill_plan_paths import main

    _write_plan(workspace_repo, "docs/plans/0099-WORKSTATE-99-demo.md")
    _write_plan(workspace_repo, "docs/tasks/WORKSTATE-99-followup.md")
    _seed_active_row("WORKSTATE-REF-99", "feature/WORKSTATE-99-demo")

    rc = main([])
    assert rc == 0  # multi-match is a skip, not a hard failure
    assert _active_plan_path("WORKSTATE-REF-99") is None
    err = capsys.readouterr().err
    assert "WORKSTATE-REF-99" in err
    assert "docs/plans/0099-WORKSTATE-99-demo.md" in err
    assert "docs/tasks/WORKSTATE-99-followup.md" in err


def test_backfill_operator_override_wins_over_glob(workspace_repo: Path) -> None:
    from workstate_handoff_mcp.scripts.backfill_plan_paths import main

    # Two candidates would normally cause a multi-match skip; --task override
    # forces a specific path so the operator can drive disambiguation.
    _write_plan(workspace_repo, "docs/plans/0099-WORKSTATE-99-demo.md")
    _write_plan(workspace_repo, "docs/tasks/WORKSTATE-99-followup.md")
    _seed_active_row("WORKSTATE-REF-99", "feature/WORKSTATE-99-demo")

    rc = main(["--task", "WORKSTATE-REF-99=docs/tasks/WORKSTATE-99-followup.md"])
    assert rc == 0
    assert _active_plan_path("WORKSTATE-REF-99") == "docs/tasks/WORKSTATE-99-followup.md"
