"""Unit tests for WORKSTATE-REF-71 implementation note named read profiles.

Coverage:
* Each profile expands to the documented sections / detail / row limits.
* Explicit caller arguments override the profile's defaults.
* Omitted public defaults (``None`` sentinels) do not masquerade as
  explicit overrides — the profile values win.
* ``get_handoff_state`` and ``load_session`` integrations attach
  ``data.read_shape`` only when a profile is requested.
* Unknown profile names produce a structured envelope error rather than
  a Python exception or silent fallback.
* Zero-limit add-on sentinels (``open_findings_limit=0`` /
  ``top_n_touched_files=0``) record an omission instead of getting
  clamped to one row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp import core as handoff_core
from workstate_handoff_mcp import read_profiles
from workstate_handoff_mcp.config import RuntimeConfig

# --- isolated handoff fixture (mirrors test_handoff_state.py) --------------


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
    return {
        "tmp_path": tmp_path,
        "state_dir": state_dir,
        "current_task_path": current_task_path,
        "dashboard_path": dashboard_path,
    }


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


# --- pure unit tests on the resolver --------------------------------------


@pytest.mark.parametrize("profile_name", read_profiles.VALID_PROFILE_NAMES)
def test_each_profile_resolves_to_its_documented_shape(profile_name: str) -> None:
    profile = read_profiles.get_profile(profile_name)
    assert profile is not None
    shape = read_profiles.resolve_state_shape(
        read_profile=profile_name,
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    assert shape.applied_profile == profile_name
    assert shape.requested_profile == profile_name
    assert shape.sections == profile.state.sections
    assert shape.detail == profile.state.detail
    assert shape.top_n_blockers == profile.state.top_n_blockers
    assert shape.top_n_actions == profile.state.top_n_actions
    assert shape.top_n_decisions == profile.state.top_n_decisions
    assert shape.top_n_slices == profile.state.top_n_slices
    assert shape.top_n_tests == profile.state.top_n_tests
    assert shape.top_n_findings == profile.state.top_n_findings
    assert shape.overrides == ()


def test_no_profile_resolves_to_full_debug_baseline() -> None:
    shape = read_profiles.resolve_state_shape(
        read_profile=None,
        sections=None,
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=None,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    assert shape.requested_profile is None
    assert shape.applied_profile is None
    assert shape.sections is None
    assert shape.detail == "full"
    assert shape.top_n_blockers == read_profiles.FULL_DEBUG.state.top_n_blockers
    assert shape.top_n_findings == read_profiles.FULL_DEBUG.state.top_n_findings


def test_explicit_overrides_beat_profile_defaults() -> None:
    shape = read_profiles.resolve_state_shape(
        read_profile="hot_summary",
        sections="decisions_recent",
        detail=None,
        top_n_blockers=None,
        top_n_actions=None,
        top_n_decisions=42,
        top_n_slices=None,
        top_n_tests=None,
        top_n_findings=None,
    )
    assert shape.applied_profile == "hot_summary"
    # Caller override wins for the two explicit fields.
    assert shape.sections == "decisions_recent"
    assert shape.top_n_decisions == 42
    # Other fields stay on the hot_summary defaults.
    assert shape.detail == "summary"
    assert shape.top_n_blockers == read_profiles.HOT_SUMMARY.state.top_n_blockers
    # Overrides are reported by name.
    assert set(shape.overrides) == {"sections", "top_n_decisions"}


def test_unknown_profile_raises_known_error() -> None:
    with pytest.raises(read_profiles.UnknownProfileError) as excinfo:
        read_profiles.get_profile("not_a_real_profile")
    assert excinfo.value.name == "not_a_real_profile"


def test_session_add_on_resolves_zero_limit_sentinels() -> None:
    add_on = read_profiles.resolve_session_add_on_shape(
        read_profile="identity",
        open_findings_limit=None,
        open_findings_detail=None,
        top_n_touched_files=None,
    )
    # identity profile asks to omit both additive sections.
    assert add_on.open_findings_limit == 0
    assert add_on.top_n_touched_files == 0
    assert add_on.overrides == ()


# --- integration tests through get_handoff_state ---------------------------


def _seed_task(task_ref: str) -> None:
    _parse(
        mcp_server.set_handoff_state(
            task_ref=task_ref,
            objective="seed for profile tests",
            status="in_progress",
        )
    )
    for idx in range(6):
        _parse(mcp_server.record_decision(session="s1", decision=f"dec_{idx}"))
    for idx in range(7):
        _parse(
            mcp_server.record_test_result(
                session="s1",
                command=f"pytest -k t{idx}",
                passed=True,
                result="ok",
            )
        )


def test_get_handoff_state_default_call_unchanged(isolated_handoff: dict) -> None:
    """Existing default behavior must not regress."""
    _seed_task("PROFILE-DEFAULT")
    state = _parse(mcp_server.get_handoff_state(task_ref="PROFILE-DEFAULT"))
    assert state["ok"] is True
    # full_debug baseline: 3 decisions, 3 tests (default handoff limits).
    assert len(state["decisions_recent"]) == 3
    assert len(state["tests_recent"]) == 3
    # read_shape is only attached when a profile was requested.
    assert "read_shape" not in state


def test_get_handoff_state_identity_profile_returns_identity_shape(isolated_handoff: dict) -> None:
    _seed_task("PROFILE-IDENTITY")
    state = _parse(mcp_server.get_handoff_state(task_ref="PROFILE-IDENTITY", read_profile="identity"))
    assert state["ok"] is True
    # identity = active + limits only (existing sections="identity" behavior).
    assert "decisions_recent" not in state
    assert "tests_recent" not in state
    assert state["read_shape"]["applied_profile"] == "identity"


def test_get_handoff_state_hot_summary_caps_rows(isolated_handoff: dict) -> None:
    _seed_task("PROFILE-HOT")
    state = _parse(mcp_server.get_handoff_state(task_ref="PROFILE-HOT", read_profile="hot_summary"))
    assert state["ok"] is True
    # hot_summary caps decisions to 3 and tests to 3, summary detail.
    assert len(state["decisions_recent"]) == 3
    assert len(state["tests_recent"]) == 3
    shape = state["read_shape"]
    assert shape["applied_profile"] == "hot_summary"
    assert shape["detail"] == "summary"
    assert shape["limits"]["decisions"] == 3


def test_get_handoff_state_explicit_arg_overrides_profile_default(isolated_handoff: dict) -> None:
    """``top_n_decisions=5`` overrides hot_summary's default of 3."""
    _seed_task("PROFILE-OVERRIDE")
    state = _parse(
        mcp_server.get_handoff_state(
            task_ref="PROFILE-OVERRIDE",
            read_profile="hot_summary",
            top_n_decisions=5,
        )
    )
    assert state["ok"] is True
    assert len(state["decisions_recent"]) == 5
    assert state["read_shape"]["limits"]["decisions"] == 5
    assert "top_n_decisions" in state["read_shape"]["overrides"]


def test_get_handoff_state_omitted_defaults_do_not_clobber_profile(isolated_handoff: dict) -> None:
    """Request-presence guard: omitting ``detail`` lets the profile choose.

    Pre-WORKSTATE-REF-71 the public signature used ``detail: str = "full"``. With
    a profile-aware resolver, ``detail`` must default to ``None`` so an
    omitted argument means "not supplied" rather than "explicit full".
    """
    _seed_task("PROFILE-PRESENCE")
    state = _parse(
        mcp_server.get_handoff_state(
            task_ref="PROFILE-PRESENCE",
            read_profile="hot_summary",
            # Note: caller does NOT pass detail. The profile says
            # detail="summary"; the public default must not override it.
        )
    )
    assert state["ok"] is True
    assert state["read_shape"]["detail"] == "summary"
    # Decisions are summary-truncated only when their rationale is long;
    # the key invariant is that the resolver did not collapse to "full".
    assert state["read_shape"]["applied_profile"] == "hot_summary"


def test_get_handoff_state_unknown_profile_returns_envelope_error(isolated_handoff: dict) -> None:
    _seed_task("PROFILE-UNKNOWN")
    raw = mcp_server.get_handoff_state(task_ref="PROFILE-UNKNOWN", read_profile="bogus")
    parsed = _parse(raw)
    assert parsed["ok"] is False
    err = parsed["data"]["error"] if "data" in parsed else parsed.get("error", "")
    # _parse merges data into top level too.
    err_text = parsed.get("error") or err
    assert "bogus" in err_text
    valid = parsed.get("valid_profiles") or parsed["data"].get("valid_profiles")
    assert "identity" in valid
    assert "full_debug" in valid


# --- integration tests through load_session --------------------------------


def test_load_session_default_call_unchanged(isolated_handoff: dict) -> None:
    _seed_task("PROFILE-SESSION-DEFAULT")
    session = _parse(mcp_server.load_session(task_ref="PROFILE-SESSION-DEFAULT"))
    assert session["ok"] is True
    assert "state" in session
    assert "open_findings" in session
    assert "touched_files" in session
    # read_shape attached only when a profile is requested.
    assert "read_shape" not in session


def test_load_session_identity_profile_omits_addons(isolated_handoff: dict) -> None:
    _seed_task("PROFILE-SESSION-IDENTITY")
    session = _parse(mcp_server.load_session(task_ref="PROFILE-SESSION-IDENTITY", read_profile="identity"))
    assert session["ok"] is True
    # identity profile sets open_findings_limit=0 and top_n_touched_files=0.
    assert session["open_findings"] == []
    assert session["touched_files"] == []
    shape = session["read_shape"]
    assert shape["state"]["applied_profile"] == "identity"
    session_shape = shape["session"]
    assert session_shape["open_findings_limit"] == 0
    assert session_shape["top_n_touched_files"] == 0
    assert "open_findings" in session_shape["omitted_sections"]
    assert "touched_files" in session_shape["omitted_sections"]


def test_load_session_zero_limit_overrides_omits_section(isolated_handoff: dict) -> None:
    """Caller-supplied zero limit must omit the section, not clamp to 1."""
    _seed_task("PROFILE-SESSION-ZERO")
    session = _parse(
        mcp_server.load_session(
            task_ref="PROFILE-SESSION-ZERO",
            read_profile="hot_summary",
            top_n_touched_files=0,
        )
    )
    assert session["ok"] is True
    assert session["touched_files"] == []
    session_shape = session["read_shape"]["session"]
    assert session_shape["top_n_touched_files"] == 0
    assert "touched_files" in session_shape["omitted_sections"]
    assert "top_n_touched_files" in session_shape["overrides"]


def test_load_session_unknown_profile_returns_envelope_error(isolated_handoff: dict) -> None:
    parsed = _parse(mcp_server.load_session(read_profile="not_real"))
    assert parsed["ok"] is False
    err = parsed.get("error") or parsed["data"].get("error", "")
    assert "not_real" in err
