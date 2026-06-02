"""Contract test: handoff DB rows must validate as workstate_protocol.ActiveTask.

This is the cross-repo wire-shape guarantee. If a handoff schema
migration adds a column whose values don't fit ActiveTask's typed
shape, this test fails — preventing silent drift between the handoff
runtime and any consumer (orchestrator, hooks, bootstrap manifests)
that imports ActiveTask.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

workstate_protocol = pytest.importorskip("workstate_protocol")


def _make_runtime(tmpdir: str):
    from workstate_handoff_mcp.config import RuntimeConfig
    from workstate_handoff_mcp.runtime import configure_runtime

    cfg = RuntimeConfig.for_workspace(tmpdir)
    configure_runtime(cfg)
    return cfg


def test_get_handoff_state_active_validates_as_protocol_active_task() -> None:
    from workstate_protocol import ActiveTask, TaskPlanResolution

    with tempfile.TemporaryDirectory() as d:
        plan_dir = pathlib.Path(d) / "docs" / "tasks"
        plan_dir.mkdir(parents=True)
        (plan_dir / "plan.md").write_text("# plan")
        _make_runtime(d)

        from workstate_handoff_mcp.handoff_state import get_handoff_state, set_handoff_state

        set_handoff_state(
            task_ref="CONTRACT-1",
            objective="probe",
            target_branch="feature/contract",
            target_worktree_path=d,
            task_plan_path="docs/tasks/plan.md",
        )
        envelope = get_handoff_state(task_ref="CONTRACT-1")
        active_dict = envelope["data"]["active"]

        # Round-trip the dict through the contract type.
        active = ActiveTask.model_validate(active_dict)
        assert active.task_ref == "CONTRACT-1"
        assert active.task_plan_path == "docs/tasks/plan.md"
        assert active.task_plan_exists is True
        assert active.task_plan_resolution is TaskPlanResolution.worktree

        # Re-serialize and confirm key parity (no fields lost).
        round_tripped = active.model_dump()
        for key in ("task_ref", "objective", "task_plan_path", "task_plan_abs_path"):
            assert round_tripped[key] == active_dict[key]


def test_active_task_without_plan_still_validates() -> None:
    from workstate_protocol import ActiveTask

    with tempfile.TemporaryDirectory() as d:
        _make_runtime(d)
        from workstate_handoff_mcp.handoff_state import get_handoff_state, set_handoff_state

        set_handoff_state(task_ref="CONTRACT-2", objective="no plan")
        envelope = get_handoff_state(task_ref="CONTRACT-2")
        ActiveTask.model_validate(envelope["data"]["active"])


def test_current_task_auto_regen_default_is_off() -> None:
    """Demotion guarantee: routine MCP writes do NOT regenerate
    CURRENT_TASK.json by default. DASHBOARD.txt is the always-current
    operator surface; CURRENT_TASK.json is on-demand only.
    """
    from workstate_handoff_mcp.config import RuntimeConfig

    cfg = RuntimeConfig.for_workspace("/tmp/probe")
    assert cfg.current_task_auto_regen is False


def test_current_task_auto_regen_env_var_opts_back_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """Legacy consumers can opt back in via env var."""
    from workstate_handoff_mcp.config import RuntimeConfig

    monkeypatch.setenv("WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN", "1")
    cfg = RuntimeConfig.for_workspace("/tmp/probe")
    assert cfg.current_task_auto_regen is True


def test_routine_writes_do_not_regenerate_current_task_json() -> None:
    """End-to-end demotion check: with the default config, set_handoff_state
    and update_task_status must not produce CURRENT_TASK.json.
    """
    with tempfile.TemporaryDirectory() as d:
        _make_runtime(d)
        from workstate_handoff_mcp.handoff_state import set_handoff_state
        from workstate_handoff_mcp.import_export import update_task_status
        from workstate_handoff_mcp.runtime import get_runtime_config

        cfg = get_runtime_config()
        assert not cfg.current_task_path.exists(), "fixture leak before write"

        r = set_handoff_state(task_ref="DEMOTE-1", objective="probe")
        assert r["ok"]
        assert not cfg.current_task_path.exists(), "set_handoff_state should not auto-regen"

        update_task_status(
            task_ref="DEMOTE-1",
            status="in_progress",
            expected_revision=r["data"]["active"]["revision"],
        )
        assert not cfg.current_task_path.exists(), "update_task_status should not auto-regen"


def test_get_handoff_state_envelope_shapes_to_handoff_state() -> None:
    """Top-level wire-shape contract: the MCP envelope round-trips
    through workstate_protocol.HandoffState via from_identity_envelope.

    Closes review finding P3 — Schema #1 is now enforced at the
    envelope level, not just on individual active rows.
    """
    from workstate_protocol import HandoffState

    with tempfile.TemporaryDirectory() as d:
        _make_runtime(d)
        from workstate_handoff_mcp.handoff_state import get_handoff_state, set_handoff_state

        set_handoff_state(
            task_ref="CONTRACT-3",
            objective="envelope-level contract",
            target_branch="feature/contract-3",
        )
        envelope = get_handoff_state(task_ref="CONTRACT-3", sections="identity")
        state = HandoffState.from_identity_envelope(envelope)
        assert state.active is not None
        assert state.active.task_ref == "CONTRACT-3"
        assert state.active_task_ref == "CONTRACT-3"
        assert state.active_tasks == [state.active]
