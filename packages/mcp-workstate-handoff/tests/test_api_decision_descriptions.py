from __future__ import annotations

from typing import get_args, get_type_hints

from workstate_handoff_mcp import api


def _annotated_field_description(func: object, param_name: str) -> str:
    annotation = get_type_hints(func, include_extras=True)[param_name]
    return get_args(annotation)[1].description


def test_decision_field_descriptions_publish_slice_complete_canonical_form() -> None:
    generic_description = api.DECISION_ID_DESCRIPTION
    slice_description = api.SLICE_COMPLETE_DECISION_ID_DESCRIPTION

    assert "<author_tag>_slice_complete_<work_ref>_<slug>" in generic_description
    assert "codex_slice_complete_plan0005_render_budget_benchmark" in generic_description
    assert "<author_tag>_slice_complete_<work_ref>_<slug>" in slice_description
    assert "codex_slice_complete_plan0005_render_budget_benchmark" in slice_description

    assert api.RecordDecisionEvent.model_fields["decision"].description == generic_description
    assert _annotated_field_description(api.record_decision, "decision") == generic_description
    assert _annotated_field_description(api.close_slice, "decision") == slice_description
