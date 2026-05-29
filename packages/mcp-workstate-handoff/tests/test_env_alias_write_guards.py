"""Tier-4 env-var alias wiring for write-context safety guards (implementation note B4 / Slice E).

The four guard helpers in ``shared_write_context.py`` resolve their
bypass/enforce flags through ``workstate_protocol.resolve_env_alias`` so the
canonical ``WORKSTATE_HANDOFF_*`` names work, while the legacy
``AGENT_HANDOFF_*`` names keep working for one release with a one-time
deprecation warning. The package conftest and most tests still set the legacy
names; that path stays valid through the fallback during the cutover.

Naming follows implementation note §2: ``AGENT_HANDOFF_*`` → ``WORKSTATE_HANDOFF_*``
(component-scoped), not a bare ``WORKSTATE_*`` collapse.
"""

from __future__ import annotations

import warnings

import pytest
from workstate_protocol.env_aliases import reset_alias_warnings

from workstate_handoff_mcp import shared_write_context as swc

# Canonical + legacy names the guard helpers read, cleared before each test so
# the package conftest's process-wide legacy defaults do not leak in.
_GUARD_ENV = (
    "WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION",
    "AGENT_HANDOFF_SKIP_SHA_VALIDATION",
    "WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION",
    "AGENT_HANDOFF_SKIP_WORKTREE_DERIVATION",
    "WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT",
    "AGENT_HANDOFF_SKIP_BRANCH_ENFORCEMENT",
    "WORKSTATE_HANDOFF_ENFORCE_BRANCH",
    "AGENT_HANDOFF_ENFORCE_BRANCH",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    reset_alias_warnings()
    for name in _GUARD_ENV:
        monkeypatch.delenv(name, raising=False)
    yield
    reset_alias_warnings()


def _dep_messages(caught) -> str:
    return " ".join(str(w.message) for w in caught if issubclass(w.category, DeprecationWarning))


def test_sha_validation_canonical_skip(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert swc._commit_sha_validation_enabled() is False


def test_sha_validation_legacy_skip_warns(monkeypatch):
    monkeypatch.setenv("AGENT_HANDOFF_SKIP_SHA_VALIDATION", "1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        assert swc._commit_sha_validation_enabled() is False
    assert "WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION" in _dep_messages(caught)


def test_worktree_derivation_canonical_skip(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert swc._worktree_derivation_enabled() is False


def test_branch_enforcement_canonical_enable(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_ENFORCE_BRANCH", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert swc._branch_enforcement_enabled() is True


def test_branch_enforcement_canonical_skip_overrides_enable(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_ENFORCE_BRANCH", "1")
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert swc._branch_enforcement_enabled() is False


def test_branch_enforcement_legacy_enable_warns(monkeypatch):
    monkeypatch.setenv("AGENT_HANDOFF_ENFORCE_BRANCH", "1")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        assert swc._branch_enforcement_enabled() is True
    assert "WORKSTATE_HANDOFF_ENFORCE_BRANCH" in _dep_messages(caught)
