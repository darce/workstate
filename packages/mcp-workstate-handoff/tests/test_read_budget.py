"""Unit + integration tests for WORKSTATE-REF-71 implementation note budget planner.

Coverage:
* Policy resolution: warn / auto_summary / fail defaults from request-presence.
* Pure planner: under-budget pass-through, warn pass-through, fail rejection,
  auto_summary reduction order (detail -> limits -> optional omissions).
* Required sections (``open_items``) never auto_summary-omitted.
* ``get_handoff_state`` integration: ``data.read_budget`` attaches when a
  budget is supplied or reductions applied; ``fail`` returns ``ok=false``
  with structured ``retry_with``.
* ``load_session`` compound budget covers state + add-ons.
* Unknown ``budget_policy`` produces a structured envelope error.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp import read_budget, read_profiles
from workstate_handoff_mcp.config import RuntimeConfig

# --- isolated handoff fixture (mirror of test_read_profiles.py) ------------


@pytest.fixture()
def isolated_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    current_task_path = tmp_path / "CURRENT_TASK.json"
    dashboard_path = tmp_path / "DASHBOARD.txt"
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=current_task_path,
        dashboard_path=dashboard_path,
        current_task_auto_regen=True,
    )
    mcp_server.configure_runtime(runtime)
    monkeypatch.chdir(tmp_path)
    return {"tmp_path": tmp_path, "state_dir": state_dir}


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


def _seed_task(task_ref: str, *, decisions: int = 20, tests: int = 5) -> None:
    _parse(
        mcp_server.set_handoff_state(
            task_ref=task_ref,
            objective="seed for budget planner tests",
            status="in_progress",
        )
    )
    for idx in range(decisions):
        long_rationale = "x" * 600  # mimic slice-complete rationale weight
        _parse(
            mcp_server.record_decision(
                session="s1",
                decision=f"dec_{idx}",
                rationale=long_rationale,
            )
        )
    for idx in range(tests):
        _parse(
            mcp_server.record_test_result(
                session="s1",
                command=f"pytest -k t{idx}",
                passed=True,
                result="ok",
            )
        )


# --- pure unit tests on policy resolution and the planner ------------------


def test_resolve_policy_defaults_match_request_presence() -> None:
    # No budget -> warn (advisory only).
    assert read_budget.resolve_policy(response_budget_bytes=None, budget_policy=None) == "warn"
    # Budget supplied without policy -> auto_summary (effective default).
    assert read_budget.resolve_policy(response_budget_bytes=4000, budget_policy=None) == "auto_summary"
    # Explicit policy is preserved.
    assert read_budget.resolve_policy(response_budget_bytes=None, budget_policy="warn") == "warn"
    assert read_budget.resolve_policy(response_budget_bytes=4000, budget_policy="fail") == "fail"


def test_resolve_policy_rejects_unknown_value() -> None:
    with pytest.raises(read_budget.UnknownBudgetPolicyError) as exc:
        read_budget.resolve_policy(response_budget_bytes=4000, budget_policy="not_a_policy")
    assert exc.value.policy == "not_a_policy"


def test_plan_state_under_budget_is_a_noop() -> None:
    shape = read_profiles.resolve_state_shape(
        read_profile="hot_summary",
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    final, plan = read_budget.plan_state_read(shape=shape, response_budget_bytes=200_000, budget_policy="auto_summary")
    assert plan.applied_reductions == []
    assert plan.omitted_sections == []
    assert plan.over_budget_after is False
    assert final == shape


def test_plan_state_warn_passes_through_over_budget() -> None:
    shape = read_profiles.resolve_state_shape(
        read_profile="full_debug",
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    final, plan = read_budget.plan_state_read(shape=shape, response_budget_bytes=500, budget_policy="warn")
    assert final == shape  # warn never reduces
    assert plan.over_budget_after is True
    assert plan.applied_reductions == []
    assert plan.retry_with is not None
    assert plan.retry_with.get("budget_policy") == "auto_summary"


def test_plan_state_fail_flags_fail_now_with_retry_hint() -> None:
    shape = read_profiles.resolve_state_shape(
        read_profile="full_debug",
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    final, plan = read_budget.plan_state_read(shape=shape, response_budget_bytes=500, budget_policy="fail")
    assert plan.fail_now is True
    assert final == shape  # never materialised — shape returned unchanged
    assert plan.retry_with is not None
    assert plan.retry_with.get("read_profile") == "hot_summary"
    assert plan.retry_with.get("response_budget_bytes") == 500


def test_plan_state_auto_summary_reduces_in_priority_order() -> None:
    shape = read_profiles.resolve_state_shape(
        read_profile="full_debug",
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    initial = read_budget.estimate_state_bytes(shape)
    final, plan = read_budget.plan_state_read(
        shape=shape, response_budget_bytes=initial // 2, budget_policy="auto_summary"
    )
    # First reduction is detail_to_summary for a full_debug profile.
    assert plan.applied_reductions[0] == "detail_to_summary"
    # Final estimate should be at or under target (or, if not, plan flags it).
    if plan.over_budget_after:
        assert plan.retry_with == {"budget_policy": "fail"}
    else:
        assert read_budget.estimate_state_bytes(final, omitted=plan.omitted_sections) <= initial // 2


def test_plan_state_open_items_protects_required_sections() -> None:
    shape = read_profiles.resolve_state_shape(
        read_profile="open_items",
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    # Choose a tight budget that forces omissions; required sections must
    # never be omitted by ``auto_summary``.
    final, plan = read_budget.plan_state_read(shape=shape, response_budget_bytes=3000, budget_policy="auto_summary")
    required = {"blockers_open", "actions_pending", "findings_open"}
    assert required.isdisjoint(set(plan.omitted_sections))


def test_estimate_session_add_on_bytes_respects_zero_limits() -> None:
    add_on = read_profiles.resolve_session_add_on_shape(
        read_profile="identity",
        open_findings_limit=None,
        open_findings_detail=None,
        top_n_touched_files=None,
    )
    assert read_budget.estimate_session_add_on_bytes(add_on) == 0


def test_plan_session_does_not_report_noop_open_findings_reduction() -> None:
    """Omitted open_findings must not report a detail reduction."""
    state_shape = read_profiles.resolve_state_shape(
        read_profile="full_debug",
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    add_on = read_profiles.resolve_session_add_on_shape(
        read_profile="hot_summary",
        open_findings_limit=0,
        open_findings_detail="full",
        top_n_touched_files=0,
    )

    _, _, plan = read_budget.plan_session_read(
        state_shape=state_shape,
        add_on=add_on,
        response_budget_bytes=1,
        budget_policy="auto_summary",
    )

    assert "open_findings" in plan.omitted_sections
    assert "session_open_findings_detail_to_summary" not in plan.applied_reductions


# --- integration tests through get_handoff_state ---------------------------


def test_get_handoff_state_unbudgeted_call_omits_read_budget(isolated_handoff: dict) -> None:
    _seed_task("BUDGET-UNBUDGETED")
    state = _parse(mcp_server.get_handoff_state(task_ref="BUDGET-UNBUDGETED"))
    assert state["ok"] is True
    # No budget supplied and no reductions applied -> data.read_budget absent.
    assert "read_budget" not in state


def test_get_handoff_state_auto_summary_reduces_and_attaches_budget(isolated_handoff: dict) -> None:
    _seed_task("BUDGET-AUTO-SUMMARY")
    state = _parse(
        mcp_server.get_handoff_state(
            task_ref="BUDGET-AUTO-SUMMARY",
            read_profile="full_debug",
            response_budget_bytes=4000,
            # default policy with budget = auto_summary
        )
    )
    assert state["ok"] is True
    budget = state["read_budget"]
    assert budget["policy"] == "auto_summary"
    assert budget["requested_bytes"] == 4000
    # Something must have been reduced for this shape against this budget.
    assert budget["applied_reductions"]
    # detail forced to summary on the response shape echo.
    assert state["read_shape"]["detail"] == "summary"


def test_get_handoff_state_fail_policy_rejects_with_retry_hint(isolated_handoff: dict) -> None:
    _seed_task("BUDGET-FAIL")
    raw = mcp_server.get_handoff_state(
        task_ref="BUDGET-FAIL",
        read_profile="full_debug",
        response_budget_bytes=500,
        budget_policy="fail",
    )
    parsed = _parse(raw)
    assert parsed["ok"] is False
    budget = parsed.get("read_budget") or parsed["data"]["read_budget"]
    assert budget["policy"] == "fail"
    retry = budget["retry_with"]
    assert retry["response_budget_bytes"] == 500
    assert retry["budget_policy"] == "auto_summary"
    assert retry.get("read_profile") == "hot_summary"


def test_get_handoff_state_unknown_budget_policy_returns_envelope_error(isolated_handoff: dict) -> None:
    _seed_task("BUDGET-BAD-POLICY")
    raw = mcp_server.get_handoff_state(
        task_ref="BUDGET-BAD-POLICY",
        response_budget_bytes=2000,
        budget_policy="bogus",
    )
    parsed = _parse(raw)
    assert parsed["ok"] is False
    err = parsed.get("error") or parsed["data"]["error"]
    assert "bogus" in err
    valid = parsed.get("valid_policies") or parsed["data"].get("valid_policies")
    assert "auto_summary" in valid
    assert "fail" in valid


def test_get_handoff_state_warn_policy_does_not_reduce(isolated_handoff: dict) -> None:
    _seed_task("BUDGET-WARN")
    state = _parse(
        mcp_server.get_handoff_state(
            task_ref="BUDGET-WARN",
            read_profile="full_debug",
            response_budget_bytes=500,
            budget_policy="warn",
        )
    )
    assert state["ok"] is True
    budget = state["read_budget"]
    assert budget["policy"] == "warn"
    assert budget["applied_reductions"] == []
    # All decisions still present; nothing was reduced.
    assert len(state["decisions_recent"]) >= 1


# --- integration tests through load_session --------------------------------


def test_load_session_compound_budget_attaches_read_budget(isolated_handoff: dict) -> None:
    _seed_task("BUDGET-SESSION")
    session = _parse(
        mcp_server.load_session(
            task_ref="BUDGET-SESSION",
            read_profile="full_debug",
            response_budget_bytes=4000,
        )
    )
    assert session["ok"] is True
    budget = session["read_budget"]
    assert budget["policy"] == "auto_summary"
    assert budget["requested_bytes"] == 4000
    # Compound estimate should reflect state + add-ons (>= state-only estimate).
    assert budget["estimated_initial_bytes"] >= 2500


def test_load_session_fail_policy_rejects_with_retry_hint(isolated_handoff: dict) -> None:
    _seed_task("BUDGET-SESSION-FAIL")
    raw = mcp_server.load_session(
        task_ref="BUDGET-SESSION-FAIL",
        read_profile="full_debug",
        response_budget_bytes=400,
        budget_policy="fail",
    )
    parsed = _parse(raw)
    assert parsed["ok"] is False
    budget = parsed.get("read_budget") or parsed["data"]["read_budget"]
    assert budget["policy"] == "fail"
    assert budget["retry_with"]["budget_policy"] == "auto_summary"


def test_load_session_records_compound_zero_limit_omissions(isolated_handoff: dict) -> None:
    """``identity`` profile with budget still records add-on omissions."""
    _seed_task("BUDGET-SESSION-IDENTITY")
    session = _parse(
        mcp_server.load_session(
            task_ref="BUDGET-SESSION-IDENTITY",
            read_profile="identity",
            response_budget_bytes=8000,
        )
    )
    assert session["ok"] is True
    budget = session["read_budget"]
    assert "open_findings" in budget["omitted_sections"]
    assert "touched_files" in budget["omitted_sections"]
