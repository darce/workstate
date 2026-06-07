from __future__ import annotations

from pathlib import Path

from workstate_handoff_mcp import RuntimeConfig, configure_runtime, get_handoff_state, set_handoff_state


def _configure_runtime(tmp_path: Path) -> None:
    configure_runtime(RuntimeConfig.for_workspace(tmp_path))


def _active_task(tmp_path: Path, task_ref: str) -> dict:
    _configure_runtime(tmp_path)
    envelope = get_handoff_state(task_ref=task_ref)
    return envelope["data"]["active"]


def test_task_plan_path_is_not_inferred_from_focus_on_create(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)

    result = set_handoff_state(
        task_ref="PLAN-CONTRACT-1",
        objective="probe",
        focus="docs/plans/0004-task-plan-metadata-and-current-task-demotion.md",
    )

    assert result["ok"] is True, result
    active = _active_task(tmp_path, "PLAN-CONTRACT-1")
    assert active["focus"] == "docs/plans/0004-task-plan-metadata-and-current-task-demotion.md"
    assert active["task_plan_path"] is None
    assert active["task_plan_abs_path"] is None
    assert active["task_plan_exists"] is False
    assert active["task_plan_resolution"] is None


def test_task_plan_path_remains_none_until_explicitly_set(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)

    created = set_handoff_state(task_ref="PLAN-CONTRACT-2", objective="probe")
    assert created["ok"] is True, created

    updated = set_handoff_state(
        task_ref="PLAN-CONTRACT-2",
        expected_revision=created["data"]["active"]["revision"],
        focus="docs/plans/0004-task-plan-metadata-and-current-task-demotion.md",
    )

    assert updated["ok"] is True, updated
    active = _active_task(tmp_path, "PLAN-CONTRACT-2")
    assert active["focus"] == "docs/plans/0004-task-plan-metadata-and-current-task-demotion.md"
    assert active["task_plan_path"] is None


def test_task_plan_path_is_preserved_when_update_omits_it(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)

    created = set_handoff_state(
        task_ref="PLAN-CONTRACT-3",
        objective="probe",
        task_plan_path="docs/plans/0004-task-plan-metadata-and-current-task-demotion.md",
    )
    assert created["ok"] is True, created

    updated = set_handoff_state(
        task_ref="PLAN-CONTRACT-3",
        expected_revision=created["data"]["active"]["revision"],
        focus="implementation note",
    )

    assert updated["ok"] is True, updated
    active = _active_task(tmp_path, "PLAN-CONTRACT-3")
    assert active["focus"] == "implementation note"
    assert active["task_plan_path"] == "docs/plans/0004-task-plan-metadata-and-current-task-demotion.md"


def test_empty_task_plan_path_clears_existing_value(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)

    created = set_handoff_state(
        task_ref="PLAN-CONTRACT-4",
        objective="probe",
        task_plan_path="docs/plans/0004-task-plan-metadata-and-current-task-demotion.md",
    )
    assert created["ok"] is True, created

    cleared = set_handoff_state(
        task_ref="PLAN-CONTRACT-4",
        expected_revision=created["data"]["active"]["revision"],
        task_plan_path="",
    )

    assert cleared["ok"] is True, cleared
    active = _active_task(tmp_path, "PLAN-CONTRACT-4")
    assert active["task_plan_path"] is None
    assert active["task_plan_abs_path"] is None
    assert active["task_plan_exists"] is False
    assert active["task_plan_resolution"] is None
