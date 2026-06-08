"""Round-trip tests for handoff schema models."""

from __future__ import annotations

import json
from pathlib import Path

from workstate_protocol import (
    ActiveTask,
    HandoffState,
    HandoffStatus,
    StructuredSummary,
    TaskPlanResolution,
    TurnRange,
)


def test_active_task_minimal_roundtrip() -> None:
    a = ActiveTask(task_ref="WORKSTATE-REF-17-14", objective="probe")
    assert a.status is HandoffStatus.in_progress
    assert a.task_plan_path is None
    assert a.task_plan() is None
    dumped = a.model_dump()
    assert dumped["task_ref"] == "WORKSTATE-REF-17-14"
    assert dumped["status"] == "in_progress"


def test_active_task_with_plan_metadata() -> None:
    a = ActiveTask(
        task_ref="WORKSTATE-REF-17-14",
        objective="probe",
        target_branch="feature/e17-14",
        target_worktree_path="/tmp/worktree",
        task_plan_path="docs/tasks/foo.md",
        task_plan_abs_path="/tmp/worktree/docs/tasks/foo.md",
        task_plan_exists=True,
        task_plan_resolution=TaskPlanResolution.worktree,
    )
    plan = a.task_plan()
    assert plan is not None
    assert plan.task_plan_path == "docs/tasks/foo.md"
    assert plan.task_plan_resolution is TaskPlanResolution.worktree
    target = a.target_worktree()
    assert target.target_branch == "feature/e17-14"


def test_active_task_allows_extra_fields_for_passthrough() -> None:
    # The handoff DB row has columns we don't model yet; they must
    # round-trip rather than blow up.
    raw = {
        "task_ref": "WORKSTATE-REF-17-14",
        "objective": "probe",
        "revision": 3,
        "updated_at": "2026-04-25T12:00:00Z",
        "updated_by": "claude-opus-4-7",
        "lane_id": "main",
    }
    a = ActiveTask.model_validate(raw)
    dumped = a.model_dump()
    assert dumped["lane_id"] == "main"
    assert dumped["updated_at"] == "2026-04-25T12:00:00Z"


def test_handoff_state_holds_multiple_active_tasks() -> None:
    s = HandoffState(
        active_tasks=[
            ActiveTask(task_ref="A-1", objective="alpha"),
            ActiveTask(task_ref="B-2", objective="beta"),
        ],
        active_task_ref="A-1",
    )
    assert s.active_tasks[0].task_ref == "A-1"
    assert s.active_task_ref == "A-1"


def test_from_identity_envelope_adapter() -> None:
    envelope = {
        "ok": True,
        "tool": "get_handoff_state",
        "task_ref": "X-1",
        "data": {
            "active": {
                "task_ref": "X-1",
                "objective": "probe",
                "status": "in_progress",
                "revision": 0,
                "task_plan_path": "docs/tasks/x.md",
                "task_plan_resolution": "worktree",
            },
            "limits": {},
        },
    }
    state = HandoffState.from_identity_envelope(envelope)
    assert state.active_task_ref == "X-1"
    assert state.active is not None
    assert state.active.task_plan_path == "docs/tasks/x.md"
    assert state.active_tasks == [state.active]


def test_from_identity_envelope_rejects_mismatched_task_ref() -> None:
    import pytest

    envelope = {
        "ok": True,
        "task_ref": "OUTER",
        "data": {"active": {"task_ref": "INNER", "objective": "x"}},
    }
    with pytest.raises(ValueError, match="envelope identity mismatch"):
        HandoffState.from_identity_envelope(envelope)


def test_from_identity_envelope_prefers_inner_when_outer_absent() -> None:
    envelope = {
        "ok": True,
        "data": {"active": {"task_ref": "INNER", "objective": "x"}},
    }
    state = HandoffState.from_identity_envelope(envelope)
    assert state.active_task_ref == "INNER"


def test_from_identity_envelope_handles_no_active() -> None:
    envelope = {"ok": True, "task_ref": None, "data": {"active": None}}
    state = HandoffState.from_identity_envelope(envelope)
    assert state.active is None
    assert state.active_tasks == []


def test_handoff_status_is_constrained() -> None:
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ActiveTask(task_ref="X", objective="y", status="not-a-status")


def test_turn_range_and_structured_summary_roundtrip() -> None:
    summary = StructuredSummary(
        compaction_id="C-WORKSTATE-REF-34-0001",
        session_id="session-123",
        harness="codex",
        task_ref="WORKSTATE-REF-34",
        turn_range=TurnRange(start_turn=1, end_turn=42),
        decisions=[{"decision_id": "scope_intake_WORKSTATE-34_trigger_choice", "slug": "trigger-choice"}],
        findings_fixed=["F-1"],
        findings_opened=["F-2"],
        tests_verified=["pytest tests/test_schema_migrations.py -q"],
        files_touched=["packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py"],
        prose_residual="Unstructured tail",
        created_at="2026-04-30T23:00:00Z",
    )

    dumped = summary.model_dump(mode="json")
    assert dumped["turn_range"] == {"start_turn": 1, "end_turn": 42}
    assert dumped["harness"] == "codex"
    restored = StructuredSummary.model_validate(dumped)
    assert restored.turn_range.end_turn == 42
    assert restored.decisions[0].decision_id == "scope_intake_WORKSTATE-34_trigger_choice"


def test_compaction_summary_schema_artifact_exists() -> None:
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "compaction-summary.json"
    schema = json.loads(schema_path.read_text())

    assert schema["title"] == "StructuredSummary"
    assert "turn_range" in schema["properties"]
