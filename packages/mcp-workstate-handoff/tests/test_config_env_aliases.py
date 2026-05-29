"""Tier-4 env-var alias wiring for RuntimeConfig (implementation note Slice D / B4).

``config.py`` resolves its operational env config through
``workstate_protocol.resolve_env_alias`` so the canonical
``WORKSTATE_HANDOFF_*`` names work, while the legacy ``AGENT_HANDOFF_*`` names
keep working for one release with a one-time deprecation warning. Exports and
generated config set only the new names; this module is read-side
compatibility during the cutover.

Naming follows implementation note §2: ``AGENT_HANDOFF_*`` → ``WORKSTATE_HANDOFF_*``
(component-scoped), not a bare ``WORKSTATE_*`` collapse.
"""

from __future__ import annotations

import warnings

import pytest
from workstate_protocol.env_aliases import reset_alias_warnings

from workstate_handoff_mcp.config import RuntimeConfig

# Canonical + legacy names config.py reads, so every test starts from a clean
# environment regardless of the caller's shell.
_CONFIG_ENV = (
    "WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN",
    "AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN",
    "WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES",
    "AGENT_HANDOFF_FINDING_LIFECYCLE_STATES",
    "WORKSTATE_HANDOFF_WORKSPACE_ROOT",
    "AGENT_HANDOFF_WORKSPACE_ROOT",
    "WORKSTATE_HANDOFF_STATE_DIR",
    "AGENT_HANDOFF_STATE_DIR",
    "WORKSTATE_HANDOFF_CURRENT_TASK_PATH",
    "AGENT_HANDOFF_CURRENT_TASK_PATH",
    "WORKSTATE_HANDOFF_DASHBOARD_PATH",
    "AGENT_HANDOFF_DASHBOARD_PATH",
    "WORKSTATE_HANDOFF_EXPORTS_DIR",
    "AGENT_HANDOFF_EXPORTS_DIR",
    "WORKSTATE_HANDOFF_TOOL_PROFILE",
    "AGENT_HANDOFF_TOOL_PROFILE",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    reset_alias_warnings()
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    reset_alias_warnings()


def _dep_messages(caught) -> str:
    return " ".join(str(w.message) for w in caught if issubclass(w.category, DeprecationWarning))


def test_auto_regen_canonical_name(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = RuntimeConfig.for_workspace("/tmp/ws")
    assert cfg.current_task_auto_regen is True


def test_auto_regen_legacy_name_warns(monkeypatch):
    monkeypatch.setenv("AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN", "1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        cfg = RuntimeConfig.for_workspace("/tmp/ws")
    assert cfg.current_task_auto_regen is True
    assert "WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN" in _dep_messages(caught)


def test_lifecycle_flag_canonical_off(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES", "0")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = RuntimeConfig.for_workspace("/tmp/ws")
    assert cfg.finding_lifecycle_states_enabled is False


def test_from_args_canonical_path_names(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKSTATE_HANDOFF_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("WORKSTATE_HANDOFF_STATE_DIR", str(tmp_path / "st"))
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = RuntimeConfig.from_args(object())
    assert cfg.workspace_root == tmp_path.resolve()
    assert cfg.state_dir == (tmp_path / "st").resolve()


def test_from_args_legacy_path_names_warn(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HANDOFF_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_HANDOFF_STATE_DIR", str(tmp_path / "st"))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        cfg = RuntimeConfig.from_args(object())
    assert cfg.workspace_root == tmp_path.resolve()
    assert cfg.state_dir == (tmp_path / "st").resolve()
    messages = _dep_messages(caught)
    assert "WORKSTATE_HANDOFF_WORKSPACE_ROOT" in messages
    assert "WORKSTATE_HANDOFF_STATE_DIR" in messages
