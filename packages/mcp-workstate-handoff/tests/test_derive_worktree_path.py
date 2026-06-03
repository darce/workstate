"""Derive ``target_worktree_path`` from ``git worktree list`` (WORKSTATE-REF-52 implementation note).

The plan's Goal: "No code path reads ``handoff_state.target_worktree_path``;
the value is resolved via ``git worktree list --porcelain`` keyed by the
*canonical* ``target_branch`` from implementation note."

Two RED gates here:

1. ``_canonical_worktree_for_task("feature/<task>")`` must return the
   absolute path of the worktree currently checked out at that branch,
   queried via ``git worktree list --porcelain``. The stored
   ``target_worktree_path`` column is irrelevant; deriving from
   ``target_branch`` is the new ground truth.

2. When the canonical ``target_branch`` has no matching worktree on
   disk, the helper raises ``WorktreeNotFoundError`` instead of
   returning ``None`` and silently letting the resolver fall through to
   wrong-cwd attribution. Operators see the failure; WORKSTATE-REF-44's
   wrong-cwd invariant cannot be re-broken by stale row data.

Both tests build a real git repo via ``subprocess`` so the helper
exercises actual ``git worktree list --porcelain`` output rather than
mocked stdout. That is the only way to catch quoting/parsing drift in
the porcelain reader.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _init_repo(path: Path) -> None:
    """Initialize ``path`` as a git repo with one commit on ``main``."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def _add_worktree(repo: Path, worktree_path: Path, branch: str) -> None:
    """Add a linked worktree at ``worktree_path`` on a new ``branch``."""
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(worktree_path)],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_canonical_worktree_for_task_returns_path_for_branch_with_worktree(tmp_path: Path) -> None:
    """RED: derivation must return the worktree's absolute path keyed by branch."""
    from workstate_handoff_mcp.shared_write_context import _canonical_worktree_for_task

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    wt = tmp_path / "wt-derived"
    _add_worktree(repo, wt, "feature/derived")

    resolved = _canonical_worktree_for_task("feature/derived", workspace_root=repo)

    assert resolved is not None
    assert Path(resolved).resolve() == wt.resolve(), (
        "_canonical_worktree_for_task must return the worktree path for the branch "
        "from `git worktree list --porcelain`, not None and not the stored column."
    )


def test_canonical_worktree_for_task_raises_when_branch_has_no_worktree(tmp_path: Path) -> None:
    """RED: missing worktree must fail loudly (operator-visible), not silently None.

    The plan: "The derivation function MUST fail loudly (return an
    explicit error, not silently fall through) when ``git worktree
    list --porcelain`` produces no match for the row's canonical
    ``target_branch``. Operators see the failure instead of getting
    wrong-cwd attribution."
    """
    from workstate_handoff_mcp.shared_write_context import (
        WorktreeNotFoundError,
        _canonical_worktree_for_task,
    )

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    # Note: we deliberately do NOT add a worktree for ``feature/never-exists``.

    with pytest.raises(WorktreeNotFoundError) as excinfo:
        _canonical_worktree_for_task("feature/never-exists", workspace_root=repo)

    msg = str(excinfo.value)
    assert "feature/never-exists" in msg, (
        "WorktreeNotFoundError message must name the branch so operators can act on it."
    )


def test_canonical_worktree_for_task_returns_none_when_branch_is_none() -> None:
    """A row with no ``target_branch`` cannot derive — caller short-circuits, no raise."""
    from workstate_handoff_mcp.shared_write_context import _canonical_worktree_for_task

    assert _canonical_worktree_for_task(None) is None
    assert _canonical_worktree_for_task("") is None


def test_canonical_worktree_for_task_resolves_main_to_primary_worktree(tmp_path: Path) -> None:
    """``main`` (the default branch) maps to the primary worktree, not a linked one."""
    from workstate_handoff_mcp.shared_write_context import _canonical_worktree_for_task

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    resolved = _canonical_worktree_for_task("main", workspace_root=repo)
    assert resolved is not None
    assert Path(resolved).resolve() == repo.resolve()


def test_resolver_uses_canonical_worktree_when_bypass_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Integration: with the bypass disabled, ``_resolve_write_actor`` derives
    the worktree path from ``target_branch`` instead of reading the row's
    stored column.

    WORKSTATE-REF-52 implementation note wires the helper into the production write-actor
    resolver. Today, ``_detect_git_write_context_at`` was called with the
    stored ``target_worktree_path``; after this slice it is called with
    the derived path from ``_canonical_worktree_for_task(target_branch)``.
    We assert the derived path was the one passed to the path-keyed git
    probe, which is observable via a monkeypatched probe that records
    its argument.
    """
    monkeypatch.delenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", raising=False)

    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    wt = tmp_path / "wt-resolver"
    _add_worktree(repo, wt, "feature/resolver-derives")

    from workstate_handoff_mcp import core as handoff_core
    from workstate_handoff_mcp import shared_write_context as swc

    # Force the workspace_root probe to use our tmp_path repo.
    monkeypatch.setattr(swc, "get_runtime_config", lambda: type("C", (), {"workspace_root": str(repo)})())

    captured: dict[str, str | None] = {}

    def fake_detect_at(path: str | None) -> tuple[str | None, str | None]:
        captured["path"] = path
        return "feature/resolver-derives", "0" * 40

    # Monkeypatch on the ``core`` module: ``_resolve_core_override`` is the
    # documented monkeypatch contract for write-context probes (see
    # _resolve_core_override in shared_write_context.py).
    monkeypatch.setattr(handoff_core, "_detect_git_write_context_at", fake_detect_at)
    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: (None, None))

    # Build a fake sqlite Row-like object via a real in-memory DB so the
    # resolver's ``active["target_branch"]`` access works the same way it
    # does in production.
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE handoff_state ("
        "id INTEGER PRIMARY KEY, task_ref TEXT, updated_by TEXT, "
        "updated_branch TEXT, updated_commit_sha TEXT, "
        "target_branch TEXT, target_worktree_path TEXT)"
    )
    conn.execute(
        "INSERT INTO handoff_state(task_ref, target_branch, target_worktree_path) "
        "VALUES('derive-task', 'feature/resolver-derives', NULL)"
    )

    swc._resolve_write_actor(conn, None, task_ref="derive-task")

    # The path-keyed probe must have been called with the derived path,
    # not with NULL (which is what the stored column held).
    assert captured.get("path") is not None, (
        "_detect_git_write_context_at must receive the derived worktree path; "
        "the stored target_worktree_path column was NULL."
    )
    assert Path(captured["path"]).resolve() == wt.resolve()
