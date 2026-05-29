"""Tier-4 env-var alias wiring for git write-context detection.

implementation note Slice C / B4. ``_detect_git_write_context`` resolves its env
overrides through ``workstate_protocol.resolve_env_alias`` so the canonical
``WORKSTATE_DEFAULT_BRANCH`` / ``WORKSTATE_DEFAULT_COMMIT_SHA`` names work,
while the legacy ``AGENT_HANDOFF_DEFAULT_*`` names keep working for one
release with a one-time deprecation warning. The env override path is the
no-git fallback (git rev-parse wins when a repo is present), so these tests
force git failure to exercise the env branch.
"""

from __future__ import annotations

import subprocess
import warnings

import pytest
from workstate_protocol.env_aliases import reset_alias_warnings

from workstate_handoff_mcp import shared_write_context as swc

_TIER4_ENV = (
    "WORKSTATE_HANDOFF_DEFAULT_BRANCH",
    "AGENT_HANDOFF_DEFAULT_BRANCH",
    "WORKSTATE_HANDOFF_DEFAULT_COMMIT_SHA",
    "AGENT_HANDOFF_DEFAULT_COMMIT_SHA",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    reset_alias_warnings()
    for name in _TIER4_ENV:
        monkeypatch.delenv(name, raising=False)
    # Force git failure so the env-override branch is exercised.
    monkeypatch.setattr(
        swc,
        "_run_cmd",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=1, stdout="", stderr=""),
    )
    yield
    reset_alias_warnings()


def test_canonical_workstate_names_resolve(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_DEFAULT_BRANCH", "feature/new")
    monkeypatch.setenv("WORKSTATE_HANDOFF_DEFAULT_COMMIT_SHA", "abc123")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        branch, commit_sha = swc._detect_git_write_context()
    assert branch == "feature/new"
    assert commit_sha == "abc123"


def test_legacy_names_still_resolve_with_deprecation_warning(monkeypatch):
    monkeypatch.setenv("AGENT_HANDOFF_DEFAULT_BRANCH", "feature/legacy")
    monkeypatch.setenv("AGENT_HANDOFF_DEFAULT_COMMIT_SHA", "def456")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", DeprecationWarning)
        branch, commit_sha = swc._detect_git_write_context()
    assert branch == "feature/legacy"
    assert commit_sha == "def456"
    dep = [w for w in caught if issubclass(w.category, DeprecationWarning)]
    deprecated = {str(w.message).split()[2] for w in dep}
    assert "AGENT_HANDOFF_DEFAULT_BRANCH" in deprecated
    assert "AGENT_HANDOFF_DEFAULT_COMMIT_SHA" in deprecated


def test_canonical_wins_over_legacy(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_DEFAULT_BRANCH", "feature/new")
    monkeypatch.setenv("AGENT_HANDOFF_DEFAULT_BRANCH", "feature/legacy")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        branch, _ = swc._detect_git_write_context()
    assert branch == "feature/new"
