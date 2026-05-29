"""Tests for the Tier-4 env-var alias shim.

implementation note Slice C / B4: ``workstate_protocol.env_aliases`` resolves the
canonical ``WORKSTATE_*`` env-var name first, then falls back to any legacy
alias (``AGENT_HANDOFF_*`` / ``AGENT_ORCHESTRATOR_*`` / ``AGENTIC_*`` /
``MCP_AGENT_HANDOFF_*``) for one release, emitting exactly one
``DeprecationWarning`` per legacy name that is actually read. Exports always
set the new name only; this shim is read-side compatibility during cutover.
"""

from __future__ import annotations

import warnings

import pytest

from workstate_protocol import resolve_env_alias
from workstate_protocol.env_aliases import reset_alias_warnings


@pytest.fixture(autouse=True)
def _clean_warn_state():
    reset_alias_warnings()
    yield
    reset_alias_warnings()


def test_canonical_name_wins_without_warning():
    env = {"WORKSTATE_STATE_DIR": "/new", "AGENT_HANDOFF_STATE_DIR": "/old"}
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        value = resolve_env_alias(
            "WORKSTATE_STATE_DIR", "AGENT_HANDOFF_STATE_DIR", env=env
        )
    assert value == "/new"


def test_legacy_fallback_emits_one_deprecation_warning():
    env = {"AGENT_HANDOFF_STATE_DIR": "/old"}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        value = resolve_env_alias(
            "WORKSTATE_STATE_DIR", "AGENT_HANDOFF_STATE_DIR", env=env
        )
    assert value == "/old"
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1
    msg = str(dep[0].message)
    # The warning must name both the deprecated and the canonical replacement.
    assert "AGENT_HANDOFF_STATE_DIR" in msg
    assert "WORKSTATE_STATE_DIR" in msg


def test_warn_once_per_legacy_name():
    env = {"AGENT_HANDOFF_STATE_DIR": "/old"}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        first = resolve_env_alias(
            "WORKSTATE_STATE_DIR", "AGENT_HANDOFF_STATE_DIR", env=env
        )
        second = resolve_env_alias(
            "WORKSTATE_STATE_DIR", "AGENT_HANDOFF_STATE_DIR", env=env
        )
    assert first == second == "/old"
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    assert len(dep) == 1


def test_first_present_legacy_wins_among_multiple_aliases():
    env = {"AGENTIC_LANE_ID": "lane-2"}
    value = resolve_env_alias(
        "WORKSTATE_LANE_ID", "AGENT_HANDOFF_LANE_ID", "AGENTIC_LANE_ID", env=env
    )
    assert value == "lane-2"


def test_blank_canonical_falls_back_to_legacy():
    env = {"WORKSTATE_STATE_DIR": "   ", "AGENT_HANDOFF_STATE_DIR": "/old"}
    value = resolve_env_alias("WORKSTATE_STATE_DIR", "AGENT_HANDOFF_STATE_DIR", env=env)
    assert value == "/old"


def test_unset_returns_default_without_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert (
            resolve_env_alias("WORKSTATE_STATE_DIR", "AGENT_HANDOFF_STATE_DIR", env={})
            is None
        )
        assert (
            resolve_env_alias(
                "WORKSTATE_STATE_DIR",
                "AGENT_HANDOFF_STATE_DIR",
                env={},
                default="/fallback",
            )
            == "/fallback"
        )
