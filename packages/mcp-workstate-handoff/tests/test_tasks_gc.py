"""WORKSTATE-REF-41 implementation note: ``tasks_gc`` janitor for stale WORKSTATE-REF-PLANNING-REVIEW rows.

The bulk archiver complements ``archive_task_state(cascade_maint_review=True)``
by cleaning up rows whose parent task was archived without the cascade flag.
Default is dry-run; ``apply=True`` mutates and writes one cascade_archive
decision per archived row.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.runtime import get_runtime_config


def _parse(payload: str | dict) -> dict:
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        data = raw.get("data", {})
        scope = raw.get("scope", {})
        flat = {**raw, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return raw


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=tmp_path / ".task-state",
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return tmp_path


def _set_row(task_ref: str, **kwargs: object) -> dict:
    defaults: dict[str, object] = {"objective": f"Objective for {task_ref}", "status": "in_progress"}
    defaults.update(kwargs)
    return _parse(mcp_server.set_handoff_state(task_ref=task_ref, **defaults))


def _seed_archived_parent_with_done_planning_row(parent_ref: str, child_ref: str) -> None:
    _set_row(parent_ref, objective=f"Parent {parent_ref}")
    _parse(mcp_server.archive_task_state(task_ref=parent_ref))
    _set_row(child_ref, objective=f"Planning review for {parent_ref}")
    _parse(mcp_server.update_task_status(task_ref=child_ref, status="done"))


def _decision_rows_for(task_ref: str) -> list[sqlite3.Row]:
    db_path = get_runtime_config().db_path
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT decision, rationale, session FROM decisions WHERE task_ref = ?",
            (task_ref,),
        ).fetchall()


def test_tasks_gc_dry_run_lists_eligible_rows_without_mutating(workspace: Path) -> None:
    _seed_archived_parent_with_done_planning_row("WORKSTATE-REF-200", "WORKSTATE-REF-PLANNING-REVIEW-TASK-200-A")

    result = _parse(mcp_server.tasks_gc())

    assert result["ok"] is True
    assert result.get("applied") is False
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-200-A" in (result.get("would_archive") or [])

    # Row is still live.
    archived_check = _parse(mcp_server.get_archived_task(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-200-A"))
    assert archived_check["ok"] is False


def test_tasks_gc_apply_archives_eligible_rows(workspace: Path) -> None:
    _seed_archived_parent_with_done_planning_row("WORKSTATE-REF-201", "WORKSTATE-REF-PLANNING-REVIEW-TASK-201-A")

    result = _parse(mcp_server.tasks_gc(apply=True))

    assert result["ok"] is True
    assert result.get("applied") is True
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-201-A" in (result.get("archived") or [])

    archived_check = _parse(mcp_server.get_archived_task(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-201-A"))
    assert archived_check["ok"] is True


def test_tasks_gc_apply_records_cascade_archive_decision_per_row(workspace: Path) -> None:
    _seed_archived_parent_with_done_planning_row("WORKSTATE-REF-202", "WORKSTATE-REF-PLANNING-REVIEW-TASK-202-A")
    _seed_archived_parent_with_done_planning_row("WORKSTATE-REF-203", "WORKSTATE-REF-PLANNING-REVIEW-TASK-203-A")

    _parse(mcp_server.tasks_gc(apply=True))

    rows_a = _decision_rows_for("WORKSTATE-REF-202")
    cascades_a = [r for r in rows_a if "cascade_archive" in str(r["decision"]).lower()]
    assert len(cascades_a) == 1
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-202-A" in str(cascades_a[0]["rationale"])

    rows_b = _decision_rows_for("WORKSTATE-REF-203")
    cascades_b = [r for r in rows_b if "cascade_archive" in str(r["decision"]).lower()]
    assert len(cascades_b) == 1
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-203-A" in str(cascades_b[0]["rationale"])


def test_tasks_gc_skips_rows_whose_parent_is_still_live(workspace: Path) -> None:
    _set_row("WORKSTATE-REF-204", objective="Live parent, not archived")
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-204-A",
        objective="Planning review for WORKSTATE-REF-204",
    )
    _parse(mcp_server.update_task_status(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-204-A", status="done"))

    result = _parse(mcp_server.tasks_gc(apply=True))

    assert result["ok"] is True
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-204-A" not in (result.get("archived") or [])


def test_tasks_gc_skips_non_done_planning_review_rows(workspace: Path) -> None:
    _set_row("WORKSTATE-REF-205", objective="Parent")
    _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-205"))
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-205-A",
        objective="Planning review for WORKSTATE-REF-205, still in_progress",
    )

    result = _parse(mcp_server.tasks_gc(apply=True))

    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-205-A" not in (result.get("archived") or [])


def test_tasks_gc_apply_is_idempotent(workspace: Path) -> None:
    _seed_archived_parent_with_done_planning_row("WORKSTATE-REF-206", "WORKSTATE-REF-PLANNING-REVIEW-TASK-206-A")

    first = _parse(mcp_server.tasks_gc(apply=True))
    second = _parse(mcp_server.tasks_gc(apply=True))

    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-206-A" in (first.get("archived") or [])
    assert (second.get("archived") or []) == []
