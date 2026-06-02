"""WORKSTATE-REF-45 implementation note: integrity_check(kind=...) merge.

Asserts the merged ``integrity_check`` MCP tool dispatches to
``working_tree``, ``post_merge``, and ``close`` kinds with the same
envelopes the legacy ``working_tree_integrity_check``,
``post_merge_integrity_check``, and ``handoff_close_check`` tools
produced, and that the legacy registrations have been removed in favor
of the consolidated entry. This slice overrides ADR-005's
``handoff_close_check`` carve-out.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT", "1")
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION", "1")
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    seed = tmp_path / "a.txt"
    seed.write_text("one\n")
    _git("add", "a.txt", cwd=tmp_path)
    _git("commit", "-q", "-m", "initial", cwd=tmp_path)
    mcp_server.configure_runtime(RuntimeConfig.for_workspace(tmp_path))
    return tmp_path


def test_integrity_check_kind_working_tree_returns_legacy_envelope(repo: Path) -> None:
    from workstate_handoff_mcp.api import integrity_check

    (repo / "a.txt").write_text("two\n")
    envelope = integrity_check({"kind": "working_tree"})

    assert envelope["tool"] == "integrity_check"
    assert envelope["ok"] is False
    assert envelope["data"]["unexpected_dirty"] == ["a.txt"]


def test_integrity_check_kind_working_tree_accepts_expected_dirty(repo: Path) -> None:
    from workstate_handoff_mcp.api import integrity_check

    (repo / "a.txt").write_text("two\n")
    envelope = integrity_check({"kind": "working_tree", "expected_dirty": ["a.txt"]})

    assert envelope["tool"] == "integrity_check"
    assert envelope["ok"] is True
    assert envelope["data"]["unexpected_dirty"] == []


def test_integrity_check_kind_post_merge_clean(repo: Path) -> None:
    from workstate_handoff_mcp.api import integrity_check

    sha = _git("rev-parse", "HEAD", cwd=repo)
    envelope = integrity_check({"kind": "post_merge", "merged_sha": sha, "expected_changed_files": []})

    assert envelope["tool"] == "integrity_check"
    assert envelope["ok"] is True
    assert envelope["data"]["divergence"] == []


def test_integrity_check_kind_post_merge_detects_divergence(repo: Path) -> None:
    from workstate_handoff_mcp.api import integrity_check

    sha = _git("rev-parse", "HEAD", cwd=repo)
    (repo / "a.txt").write_text("drift\n")
    envelope = integrity_check({"kind": "post_merge", "merged_sha": sha, "expected_changed_files": []})

    assert envelope["tool"] == "integrity_check"
    assert envelope["ok"] is False
    assert envelope["data"]["divergence"] == ["a.txt"]


def test_integrity_check_kind_close_allows_no_active_task(tmp_path: Path) -> None:
    from workstate_handoff_mcp.api import integrity_check

    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)

    envelope = integrity_check({"kind": "close", "allow_no_active_task": True, "enforce": True})

    assert envelope["tool"] == "integrity_check"
    assert envelope["ok"] is True
    assert envelope["data"]["skipped"] is True
    assert envelope["data"]["ready_to_close"] is True


def test_registry_replaces_integrity_check_split_with_consolidated_tool() -> None:
    from workstate_handoff_mcp.api import _build_tool_registry

    registry = _build_tool_registry()
    names = {entry.name for entry in registry}

    assert "integrity_check" in names, "consolidated integrity_check tool must be registered"
    assert "working_tree_integrity_check" not in names, (
        "legacy working_tree_integrity_check must be removed in favor of integrity_check(kind='working_tree')"
    )
    assert "post_merge_integrity_check" not in names, (
        "legacy post_merge_integrity_check must be removed in favor of integrity_check(kind='post_merge')"
    )
    assert "handoff_close_check" not in names, (
        "legacy handoff_close_check must be removed in favor of integrity_check(kind='close')"
    )


def test_expected_handoff_tool_count_decremented_for_slice_4() -> None:
    """implementation note's count ceiling. The live invariant (== current) belongs to the latest slice's test."""
    from workstate_handoff_mcp.invariants import EXPECTED_HANDOFF_TOOL_COUNT

    assert EXPECTED_HANDOFF_TOOL_COUNT <= 24
