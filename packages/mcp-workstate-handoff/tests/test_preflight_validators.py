"""implementation note implementation note — preflight validators (side-effect-free)."""

from __future__ import annotations

import pytest


def test_preflight_module_exports_both_validators() -> None:
    from workstate_handoff_mcp import preflight

    assert callable(getattr(preflight, "validate_review_ready", None))
    assert callable(getattr(preflight, "validate_finding_resolution", None))


def test_validate_review_ready_returns_envelope_shape() -> None:
    from workstate_handoff_mcp import preflight

    result = preflight.validate_review_ready(task_ref="WORKSTATE-REF-NONEXISTENT-TASK-XYZ")

    assert isinstance(result, dict)
    assert "ok" in result
    assert "blockers" in result
    assert "boundary_state" in result
    assert isinstance(result["blockers"], list)
    assert isinstance(result["boundary_state"], dict)


def test_validate_review_ready_blocker_kind_is_in_canonical_enum() -> None:
    from workstate_handoff_mcp import preflight

    canonical_kinds = {
        "open_findings",
        "contract_co_change_missing",
        "dirty_protected_paths",
        "behind_main",
        "descendant_commit_required",
        "unknown_task",
    }
    result = preflight.validate_review_ready(task_ref="WORKSTATE-REF-NONEXISTENT-TASK-XYZ")

    assert result["ok"] is False, "missing task should not be ready"
    assert len(result["blockers"]) >= 1
    for blocker in result["blockers"]:
        assert {"kind", "detail", "suggested"} <= set(blocker)
        assert blocker["kind"] in canonical_kinds


def test_validate_finding_resolution_returns_envelope_shape() -> None:
    from workstate_handoff_mcp import preflight

    result = preflight.validate_finding_resolution(finding_id_or_db_id="does-not-exist")

    assert isinstance(result, dict)
    assert "ok" in result
    assert result["ok"] is False
    assert "error" in result
    assert "suggested" in result


@pytest.mark.parametrize(
    "validator_name,kwargs",
    [
        ("validate_review_ready", {"task_ref": "WORKSTATE-REF-NONEXISTENT-TASK-XYZ"}),
        ("validate_finding_resolution", {"finding_id_or_db_id": "missing-finding"}),
    ],
)
def test_validators_are_side_effect_free(validator_name: str, kwargs: dict[str, object]) -> None:
    """Validators must not mutate handoff state."""
    from workstate_handoff_mcp import preflight

    validator = getattr(preflight, validator_name)
    # Call twice; payload should be deterministic for the same inputs.
    first = validator(**kwargs)
    second = validator(**kwargs)
    assert first == second
