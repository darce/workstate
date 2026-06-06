"""Env-var wiring for write-context safety guards (implementation note B4 / Slice E).

The four guard helpers in ``shared_write_context.py`` resolve their
bypass/enforce flags through ``workstate_protocol.resolve_env_alias`` so the
canonical ``WORKSTATE_HANDOFF_*`` names work.

Naming follows implementation note §2: ``WORKSTATE_HANDOFF_*`` (component-scoped), not a
bare ``WORKSTATE_*`` collapse.
"""

from __future__ import annotations

import warnings

import pytest

from workstate_handoff_mcp import shared_write_context as swc

# Canonical names the guard helpers read, cleared before each test so the
# package conftest's process-wide defaults do not leak in.
_GUARD_ENV = (
    "WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION",
    "WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION",
    "WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT",
    "WORKSTATE_HANDOFF_ENFORCE_BRANCH",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for name in _GUARD_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


def test_sha_validation_canonical_skip(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION", "1")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert swc._commit_sha_validation_enabled() is False


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
