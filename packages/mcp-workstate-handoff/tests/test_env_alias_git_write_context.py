"""Env-var wiring for git write-context detection.

implementation note Slice C / B4. ``_detect_git_write_context`` resolves its env
overrides through ``workstate_protocol.resolve_env_alias`` so the canonical
``WORKSTATE_HANDOFF_DEFAULT_BRANCH`` / ``WORKSTATE_HANDOFF_DEFAULT_COMMIT_SHA``
names work. The env override path is the no-git fallback (git rev-parse wins
when a repo is present), so these tests force git failure to exercise the env
branch.
"""

from __future__ import annotations

import subprocess
import warnings

import pytest

from workstate_handoff_mcp import shared_write_context as swc

_TIER4_ENV = (
    "WORKSTATE_HANDOFF_DEFAULT_BRANCH",
    "WORKSTATE_HANDOFF_DEFAULT_COMMIT_SHA",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    for name in _TIER4_ENV:
        monkeypatch.delenv(name, raising=False)
    # Force git failure so the env-override branch is exercised.
    monkeypatch.setattr(
        swc,
        "_run_cmd",
        lambda *a, **k: subprocess.CompletedProcess(args=a, returncode=1, stdout="", stderr=""),
    )
    yield


def test_canonical_workstate_names_resolve(monkeypatch):
    monkeypatch.setenv("WORKSTATE_HANDOFF_DEFAULT_BRANCH", "feature/new")
    monkeypatch.setenv("WORKSTATE_HANDOFF_DEFAULT_COMMIT_SHA", "abc123")
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        branch, commit_sha = swc._detect_git_write_context()
    assert branch == "feature/new"
    assert commit_sha == "abc123"
