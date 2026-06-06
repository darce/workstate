"""Env-var config wiring for RuntimeConfig (implementation note Slice D / B4).

``config.py`` resolves its operational env config through
``workstate_protocol.resolve_env_alias`` so the canonical
``WORKSTATE_HANDOFF_*`` names work. Exports and generated config set only the
new names.

Naming follows implementation note §2: ``WORKSTATE_HANDOFF_*`` (component-scoped), not a
bare ``WORKSTATE_*`` collapse.
"""

from __future__ import annotations

import warnings

import pytest

from workstate_handoff_mcp.config import RuntimeConfig

# Canonical names config.py reads, so every test starts from a clean
# environment regardless of the caller's shell.
_CONFIG_ENV = (
    "WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN",
    "WORKSTATE_HANDOFF_FINDING_LIFECYCLE_STATES",
    "WORKSTATE_HANDOFF_WORKSPACE_ROOT",
    "WORKSTATE_HANDOFF_STATE_DIR",
    "WORKSTATE_HANDOFF_CURRENT_TASK_PATH",
    "WORKSTATE_HANDOFF_DASHBOARD_PATH",
    "WORKSTATE_HANDOFF_EXPORTS_DIR",
    "WORKSTATE_HANDOFF_TOOL_PROFILE",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for name in _CONFIG_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


def test_auto_regen_canonical_name(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        cfg = RuntimeConfig.for_workspace("/tmp/ws")
    assert cfg.current_task_auto_regen is True


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
