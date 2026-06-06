"""Contract: close writes tolerate a row whose target_branch has no live worktree.

WORKSTATE-REF-WSG-ARCHIVE-NO-WORKTREE. The write-side guard
(``WorktreeNotFoundError`` from ``shared_write_context.py``) is correct on
active writes but too strict during the *close* transition. When a task's
linked worktree has already been deleted (off-canonical close), both the
direct ``archive`` write and the ``status='done'`` close write resolve a
write actor that derives the worktree from the row's canonical
``target_branch`` via ``_canonical_worktree_for_task`` — and abort with
``WorktreeNotFoundError`` before the row can be retired.

These tests reproduce both off-canonical close scenarios against a real
``tmp_path`` git repo with worktree derivation ENABLED (the package
conftest disables derivation by default for synthetic fixtures; close
recovery is exactly the production path that must derive). The row is
seeded while the bypass is still on, then derivation is enabled before the
close attempt so the guard fires the way it does in production.

implementation note lands these red: today both close writes raise. implementation note makes them
green and pins that the archive snapshot preserves the pre-clear branch for
forensic readers.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig

GHOST_BRANCH = "feature/wsg-ghost-worktree"
TASK_REF = "WORKSTATE-REF-WSG-CONTRACT-OFFCANONICAL"


def _parse(payload: str | dict) -> dict:
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        data = raw.get("data", {})
        scope = raw.get("scope", {})
        flat = {**raw, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return raw


def _init_repo(path: Path) -> None:
    """Initialize ``path`` as a git repo with one commit on ``main``."""
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


@pytest.fixture()
def off_canonical_task(tmp_path: Path) -> tuple[str, str]:
    """Seed a handoff row whose ``target_branch`` maps to no live worktree.

    Returns ``(task_ref, ghost_branch)``. The git repo (``main`` only) has
    no worktree for ``ghost_branch``, so production worktree derivation
    raises ``WorktreeNotFoundError`` for this row — the off-canonical close
    scenario. The row is created with the bypass still active (default in
    conftest) so seeding does not itself trip the guard.
    """
    _init_repo(tmp_path)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=tmp_path / ".task-state",
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref=TASK_REF,
            objective="off-canonical close: feature worktree already deleted",
            status="in_progress",
            target_branch=GHOST_BRANCH,
            target_worktree_path=str(tmp_path / "deleted-worktree"),
        )
    )
    assert created["ok"] is True
    return TASK_REF, GHOST_BRANCH


def test_direct_archive_succeeds_when_target_branch_has_no_worktree(
    off_canonical_task: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct ``archive`` must retire a stale-pointer row instead of raising.

    RED today: ``_resolve_write_actor`` derives the worktree from
    ``target_branch`` and raises ``WorktreeNotFoundError`` before the
    archive write lands. Post-Slice-2 the close clears the stale pointer and
    the archive succeeds.
    """
    from workstate_handoff_mcp import core

    task_ref, _ghost_branch = off_canonical_task
    monkeypatch.delenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", raising=False)

    envelope = core.archive_task_state(task_ref=task_ref)

    assert envelope["ok"] is True, (
        f"off-canonical archive must succeed without recreating a tombstone worktree; got: {envelope!r}"
    )


def test_archive_snapshot_preserves_pre_clear_branch(
    off_canonical_task: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The archived snapshot must retain the pre-clear ``target_branch``.

    Greenfield: no ``task_archives`` row carries a live pointer to a deleted
    branch, but the pre-clear value is preserved inside the snapshot for
    forensic readers. RED today (archive raises before snapshotting can be
    verified end-to-end).
    """
    from workstate_handoff_mcp import core

    task_ref, ghost_branch = off_canonical_task
    monkeypatch.delenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", raising=False)

    archive_envelope = core.archive_task_state(task_ref=task_ref)
    assert archive_envelope["ok"] is True

    got = _parse(mcp_server.archive({"operation": "get", "task_ref": task_ref, "include_snapshot": True}))
    assert got["ok"] is True
    snapshot = got.get("snapshot")
    assert snapshot is not None, "archived snapshot must be retrievable for forensic reads"
    active = snapshot.get("active") or {}
    assert active.get("target_branch") == ghost_branch, (
        "archive snapshot must preserve the pre-clear target_branch so the "
        "deleted-worktree forensic trail survives the close"
    )


def test_status_done_close_succeeds_when_target_branch_has_no_worktree(
    off_canonical_task: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``set_handoff_state(status='done', status_only=True)`` must complete.

    This is the write that ``make task-finish`` issues first (``_set_status_done``
    before ``_archive``). RED today: the status-only close resolves a write
    actor that derives the missing worktree and raises before archive can
    clear the stale pointer.
    """
    task_ref, _ghost_branch = off_canonical_task
    monkeypatch.delenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", raising=False)

    envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref=task_ref,
            status="done",
            status_only=True,
        )
    )

    assert envelope["ok"] is True, (
        "status-done close must complete on a row whose worktree was already "
        f"deleted (the first write make task-finish issues); got: {envelope!r}"
    )
    assert envelope["status"] == "done"


def test_worktree_not_found_message_routes_operator_to_task_finish(tmp_path: Path) -> None:
    """implementation note: the active-path guard message must name ``make task-finish``.

    A non-close active write whose worktree is gone still raises
    ``WorktreeNotFoundError`` (that guard is intentionally preserved), but the
    remediation string must route the operator at the canonical close path
    instead of relying on them to remember it. The existing three causes
    (worktree deleted, stale ``target_branch``, ``git worktree add`` never run)
    must survive so audit/search of the error string keeps matching.
    """
    from workstate_handoff_mcp.shared_write_context import (
        WorktreeNotFoundError,
        _canonical_worktree_for_task,
    )

    _init_repo(tmp_path)

    with pytest.raises(WorktreeNotFoundError) as excinfo:
        _canonical_worktree_for_task("feature/never-backed", workspace_root=tmp_path)

    msg = str(excinfo.value)
    assert "make task-finish" in msg, (
        "WorktreeNotFoundError must name `make task-finish` so an operator who "
        f"slipped off the canonical close path is told how to recover; got: {msg!r}"
    )
    assert "git worktree add" in msg, (
        "the existing `git worktree add` remediation must be preserved alongside the new task-finish routing"
    )
