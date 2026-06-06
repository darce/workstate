"""WORKSTATE-REF-41 implementation note: ``archive_task_state(cascade_maint_review=True)``.

Plan-driven cascade: when an WORKSTATE-REF task is archived with the explicit opt-in,
non-archived ``WORKSTATE-REF-PLANNING-REVIEW-*`` rows whose ``objective`` or
``task_plan_path`` references the archiving WORKSTATE-REF task are archived in the
same transaction. A single ``cascade_archive`` decision records the
side-effect for audit.

Default behavior (no flag, or ``cascade_maint_review=False``) preserves
existing non-cascading semantics so legacy callers and tests are unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


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


def test_default_archive_does_not_cascade_maint_planning_review_rows(workspace: Path) -> None:
    """Existing callers see no behavior change without the explicit opt-in."""
    _set_row("WORKSTATE-REF-99", objective="Parent task")
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-99-20260505",
        objective="Planning review for WORKSTATE-REF-99",
    )

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-99"))
    assert result["ok"] is True
    assert result.get("cascade_archived") in (None, [], 0)

    archived_check = _parse(mcp_server.get_archived_task(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-99-20260505"))
    assert archived_check["ok"] is False  # not archived by default


def test_cascade_archives_maint_rows_referencing_parent_via_objective(workspace: Path) -> None:
    """cascade_maint_review=True archives WORKSTATE-REF-PLANNING-REVIEW-* rows whose objective mentions the parent."""
    _set_row("WORKSTATE-REF-100", objective="Parent task being archived")
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-100-20260505",
        objective="Planning review for WORKSTATE-REF-100 plan revision",
    )
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-OTHER-20260505",
        objective="Planning review for WORKSTATE-REF-OTHER, unrelated",
    )

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-100", cascade_maint_review=True))

    assert result["ok"] is True
    cascade = result.get("cascade_archived") or []
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-100-20260505" in cascade
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-OTHER-20260505" not in cascade

    cascaded_archive = _parse(mcp_server.get_archived_task(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-100-20260505"))
    assert cascaded_archive["ok"] is True

    unrelated = _parse(mcp_server.get_archived_task(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-OTHER-20260505"))
    assert unrelated["ok"] is False


def test_cascade_archives_maint_rows_referencing_parent_via_task_plan_path(workspace: Path) -> None:
    """task_plan_path matching also triggers cascade."""
    _set_row("WORKSTATE-REF-101", objective="Parent task")
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-101-20260505",
        objective="Planning review",
        task_plan_path="packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-101-something-task-plan.md",
    )

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-101", cascade_maint_review=True))

    assert result["ok"] is True
    cascade = result.get("cascade_archived") or []
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-101-20260505" in cascade


def test_cascade_skips_non_maint_rows(workspace: Path) -> None:
    """Cascade only matches WORKSTATE-REF-PLANNING-REVIEW-* prefix."""
    _set_row("WORKSTATE-REF-102", objective="Parent task")
    _set_row(
        "feature-branch-WORKSTATE-REF-102-followup",
        objective="Working row that mentions WORKSTATE-REF-102 but isn't WORKSTATE-REF-PLANNING-REVIEW",
    )

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-102", cascade_maint_review=True))

    assert result["ok"] is True
    cascade = result.get("cascade_archived") or []
    assert "feature-branch-WORKSTATE-REF-102-followup" not in cascade


def test_cascade_skips_already_archived_maint_rows(workspace: Path) -> None:
    """A WORKSTATE-REF-PLANNING-REVIEW row already archived is not double-archived."""
    _set_row("WORKSTATE-REF-103", objective="Parent task")
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-103-PRE",
        objective="Planning review for WORKSTATE-REF-103, already archived",
    )
    _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-103-PRE"))

    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-103-LATE",
        objective="Planning review for WORKSTATE-REF-103, still live",
    )

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-103", cascade_maint_review=True))

    cascade = result.get("cascade_archived") or []
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-103-LATE" in cascade
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-103-PRE" not in cascade


def test_cascade_records_a_decision_listing_archived_task_refs(workspace: Path) -> None:
    """A single cascade_archive decision is recorded as a side effect."""
    _set_row("WORKSTATE-REF-104", objective="Parent task")
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-104-20260505",
        objective="Planning review for WORKSTATE-REF-104",
    )

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-104", cascade_maint_review=True))
    assert result["ok"] is True
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-104-20260505" in (result.get("cascade_archived") or [])

    # Read the decision row directly so we can assert on the full rationale.
    import sqlite3

    from workstate_handoff_mcp.runtime import get_runtime_config

    db_path = get_runtime_config().db_path
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT decision, rationale, session, task_ref FROM decisions WHERE task_ref = ?",
            ("WORKSTATE-REF-104",),
        ).fetchall()

    cascade_decisions = [r for r in rows if "cascade_archive" in str(r["decision"]).lower()]
    assert len(cascade_decisions) == 1, f"expected exactly one cascade_archive decision, got {len(cascade_decisions)}"
    decision_row = cascade_decisions[0]
    assert decision_row["session"] == "archive_cascade"
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-104-20260505" in str(decision_row["rationale"])


def test_cascade_does_not_match_overlapping_task_ref_prefix(workspace: Path) -> None:
    """BR-WORKSTATE41-r6-01: archiving WORKSTATE-REF-10 must not cascade into WORKSTATE-REF-100 rows.

    Substring matching against objective/task_plan_path silently archived
    sibling planning-review rows whose parent ref shared a prefix with the
    archiving task. Boundary-anchoring the parent ref in the post-filter
    keeps the longer-suffix rows live.
    """
    _set_row("WORKSTATE-REF-10", objective="Parent being archived")
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-10-20260506",
        objective="Planning review for WORKSTATE-REF-10",
    )
    _set_row(
        "WORKSTATE-REF-PLANNING-REVIEW-TASK-100-20260506",
        objective="Planning review for WORKSTATE-REF-100, unrelated parent",
        task_plan_path="packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-100-something-task-plan.md",
    )

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-10", cascade_maint_review=True))

    assert result["ok"] is True
    cascade = result.get("cascade_archived") or []
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-10-20260506" in cascade
    assert "WORKSTATE-REF-PLANNING-REVIEW-TASK-100-20260506" not in cascade

    # WORKSTATE-REF-100 row stays live and is NOT in the archive.
    archived_check = _parse(mcp_server.get_archived_task(task_ref="WORKSTATE-REF-PLANNING-REVIEW-TASK-100-20260506"))
    assert archived_check["ok"] is False


def test_cascade_with_zero_matches_is_noop(workspace: Path) -> None:
    """If no WORKSTATE-REF-PLANNING-REVIEW row references the parent, cascade is a clean no-op."""
    _set_row("WORKSTATE-REF-105", objective="Parent with no planning rows")

    result = _parse(mcp_server.archive_task_state(task_ref="WORKSTATE-REF-105", cascade_maint_review=True))

    assert result["ok"] is True
    assert (result.get("cascade_archived") or []) == []
