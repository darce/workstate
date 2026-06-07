"""Tests for canonical Workstate env-var reads."""

from __future__ import annotations

import pytest

from workstate_protocol import resolve_env_alias
from workstate_protocol import env_aliases


def test_canonical_name_wins():
    env = {"WORKSTATE_STATE_DIR": "/new", "AGENT_HANDOFF_STATE_DIR": "/old"}
    value = resolve_env_alias("WORKSTATE_STATE_DIR", env=env)
    assert value == "/new"


def test_legacy_name_is_ignored():
    env = {"AGENT_HANDOFF_STATE_DIR": "/old"}
    assert resolve_env_alias("WORKSTATE_STATE_DIR", env=env) is None


def test_legacy_args_are_not_supported():
    with pytest.raises(TypeError):
        resolve_env_alias("WORKSTATE_STATE_DIR", "AGENT_HANDOFF_STATE_DIR", env={})


def test_warn_once_reset_hook_is_removed():
    assert not hasattr(env_aliases, "reset_alias_warnings")


def test_blank_canonical_returns_default_not_legacy():
    env = {"WORKSTATE_STATE_DIR": "   ", "AGENT_HANDOFF_STATE_DIR": "/old"}
    value = resolve_env_alias("WORKSTATE_STATE_DIR", env=env, default="/fallback")
    assert value == "/fallback"


def test_unset_returns_default():
    assert resolve_env_alias("WORKSTATE_STATE_DIR", env={}) is None
    assert resolve_env_alias("WORKSTATE_STATE_DIR", env={}, default="/fallback") == "/fallback"
