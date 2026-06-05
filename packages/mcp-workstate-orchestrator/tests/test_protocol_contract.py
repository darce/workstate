"""Cross-server contract test.

Verifies that ``mcp-workstate-orchestrator`` and ``mcp-workstate-handoff``
agree on the wire shape of ``get_handoff_state``'s active row, by
running both packages in-process and validating the response through
``workstate_protocol.ActiveTask`` via the orchestrator's helper.

This is the missing contract test from review finding P1 #3 — when
the two servers disagree on the handoff shape, this test fails before
runtime.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

workstate_protocol = pytest.importorskip("workstate_protocol")
pytest.importorskip("workstate_handoff_mcp")
pytest.importorskip("workstate_orchestrator_mcp")


def _setup_handoff(tmpdir: str) -> None:
    from workstate_handoff_mcp.config import RuntimeConfig
    from workstate_handoff_mcp.runtime import configure_runtime

    configure_runtime(RuntimeConfig.for_workspace(tmpdir))


def test_orchestrator_validates_handoff_active_row_through_protocol() -> None:
    from workstate_orchestrator_mcp.orchestration import handoff_read_shapes

    with tempfile.TemporaryDirectory() as d:
        plan_dir = pathlib.Path(d) / "docs" / "tasks"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan.md").write_text("# plan")
        _setup_handoff(d)

        from workstate_handoff_mcp.handoff_state import get_handoff_state, set_handoff_state

        set_handoff_state(
            task_ref="ORCH-1",
            objective="cross-server contract probe",
            target_branch="feature/orch",
            target_worktree_path=d,
            task_plan_path="docs/tasks/plan.md",
        )

        envelope = get_handoff_state(task_ref="ORCH-1")
        active = handoff_read_shapes.validate_active_task(envelope)
        assert active is not None, "orchestrator helper failed to validate handoff active row"
        assert active.task_ref == "ORCH-1"
        assert active.task_plan_path == "docs/tasks/plan.md"
        assert active.task_plan_exists is True


def test_validate_active_task_returns_none_on_missing_active() -> None:
    from workstate_orchestrator_mcp.orchestration import handoff_read_shapes

    assert handoff_read_shapes.validate_active_task({"data": {"active": None}}) is None
    assert handoff_read_shapes.validate_active_task({"data": {}}) is None
    assert handoff_read_shapes.validate_active_task({}) is None


def test_read_handoff_state_calls_validator_and_returns_envelope() -> None:
    """The chokepoint wrapper validates every consumed envelope, not
    just the one in _resolve_task_ref.
    """
    from workstate_orchestrator_mcp.orchestration import handoff_read_shapes

    with tempfile.TemporaryDirectory() as d:
        _setup_handoff(d)
        from workstate_handoff_mcp.handoff_state import set_handoff_state

        set_handoff_state(task_ref="WRAP-1", objective="probe wrapper")

        # Patch validate_active_task to record call counts.
        called: list[dict] = []
        original = handoff_read_shapes.validate_active_task
        handoff_read_shapes.validate_active_task = lambda env: called.append(env) or None
        try:
            envelope = handoff_read_shapes.read_handoff_state(task_ref="WRAP-1")
        finally:
            handoff_read_shapes.validate_active_task = original

        assert len(called) == 1
        assert envelope["data"]["active"]["task_ref"] == "WRAP-1"


def test_validate_active_task_does_not_raise_on_drift() -> None:
    from workstate_orchestrator_mcp.orchestration import handoff_read_shapes

    # Missing required `task_ref` would fail Pydantic validation; the
    # helper must log + return None rather than propagate.
    result = handoff_read_shapes.validate_active_task({"data": {"active": {"objective": "no task_ref"}}})
    assert result is None
