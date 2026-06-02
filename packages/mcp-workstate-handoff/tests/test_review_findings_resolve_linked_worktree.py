"""WORKSTATE-REF-01 implementation note: end-to-end resolve invoked from a linked worktree.

The scenario this test pins:

  - A primary worktree owns the handoff DB at ``<primary>/.task-state``.
  - A linked worktree (``feature/test``) carries the bug commit and a later
    descendant commit that fixes it.
  - An MCP/CLI caller invokes ``mcp-workstate-handoff review-findings --operation
    resolve ...`` with ``--workspace-root <linked>``.

  Pre-WORKSTATE-REF-01, the runtime collapsed *both* the state coordinate and the
  git-context coordinate to the primary worktree, so resolve never saw the
  linked worktree's HEAD and the descendant commit looked like ``unknown``
  / ``same`` — the resolve outcome was ``blocked_by_context`` and the
  feedback loop required Python escape hatches (``monkeypatch.setattr`` of
  ``_detect_git_write_context`` / ``_classify_commit_relation``) just to
  prove the fix.

  After WORKSTATE-REF-01, ``RuntimeConfig`` carries a two-coordinate split:
  ``state_workspace_root`` collapses to the primary so the DB stays shared,
  while ``git_workspace_root`` preserves the linked worktree so commit
  detection, ancestry, and cleanliness read the operator's branch.

  This test exercises the whole loop via the public CLI surface — no
  monkeypatching of git-context helpers. If the slice regresses, the
  receipt will report ``outcome != "fixed"`` and the linked-worktree
  HEAD/branch will not appear in the verified commit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

from workstate_handoff_mcp import api, cli


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _rev_parse(cwd: Path, ref: str = "HEAD") -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), "rev-parse", ref],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def primary_and_linked_with_bug_and_fix(tmp_path: Path) -> dict:
    """Build a primary worktree + linked worktree with one bug commit and
    one descendant fix commit on the linked branch.

    Returns:
        {
            "primary": Path,
            "linked": Path,
            "bug_sha": str,   # commit where the finding was anchored
            "fix_sha": str,   # descendant commit that resolves the finding
        }
    """
    primary = tmp_path / "primary"
    primary.mkdir()
    _run_git(primary, "init", "-q", "-b", "main")
    _run_git(primary, "config", "user.email", "test@example.com")
    _run_git(primary, "config", "user.name", "Test User")
    # Ignore the handoff DB dir so the root checkout stays honestly clean:
    # the WORKSTATE-REF-09 caller-outside tests need a dirty/clean signal that comes
    # from intentional edits, not from the .task-state/ that seeding writes
    # into the primary worktree.
    (primary / ".gitignore").write_text(".task-state/\n")
    _run_git(primary, "add", ".gitignore")
    _run_git(primary, "commit", "-m", "ignore handoff state dir", "-q")

    linked = tmp_path / "primary-feature"
    _run_git(primary, "branch", "feature/test")
    _run_git(primary, "worktree", "add", "-q", str(linked), "feature/test")

    (linked / "module.py").write_text("def bug():\n    return 1\n")
    _run_git(linked, "add", "module.py")
    _run_git(linked, "commit", "-m", "introduce defect", "-q")
    bug_sha = _rev_parse(linked)

    (linked / "module.py").write_text("def fix():\n    return 2\n")
    _run_git(linked, "add", "module.py")
    _run_git(linked, "commit", "-m", "resolve defect", "-q")
    fix_sha = _rev_parse(linked)

    return {"primary": primary, "linked": linked, "bug_sha": bug_sha, "fix_sha": fix_sha}


def _seed_open_finding_in_primary_db(primary: Path, bug_sha: str) -> None:
    """Pre-record the open finding in the primary worktree's DB.

    Reset runtime after seeding so the CLI run starts with a fresh runtime
    (and re-resolves the linked-worktree git coordinate from --workspace-root).
    """
    api.configure_runtime(api.RuntimeConfig.for_workspace(primary))
    try:
        api.set_handoff_state(task_ref="WORKSTATE-test", objective="linked resolve", status="in_progress")
        api.record_review_finding(
            session="reviewer",
            finding_id="WORKSTATE-REF-RESOLVE-001",
            severity="medium",
            file_path="module.py",
            description="defect introduced on feature/test",
            task_ref="WORKSTATE-test",
            actor={"agent": "reviewer", "branch": "feature/test", "commit_sha": bug_sha},
        )
    finally:
        api.reset_runtime_config()


def _invoke_resolve_cli_from_linked(linked: Path, capsys) -> dict:
    """Drive the CLI without auto-injecting --state-dir.

    The test_cli helper injects ``--state-dir <workspace>/.task-state`` to
    appease ConsumerRootResolutionError, but doing so here would defeat the
    point of the slice: we want from_args() to collapse state to the primary
    on its own, using the linked-worktree path as the git coordinate. The
    linked path *is* inside a git repo, so the resolution error guard does
    not trip.
    """
    argv = [
        "mcp-workstate-handoff",
        "--workspace-root",
        str(linked),
        "review-findings",
        "--operation",
        "resolve",
        "--task-ref",
        "WORKSTATE-test",
        "--resolve-finding-id",
        "WORKSTATE-REF-RESOLVE-001",
        "--session",
        "WORKSTATE-resolve",
        "--resolution-notes",
        "Verified the descendant commit removes the defect on feature/test.",
    ]
    original_argv = sys.argv
    sys.argv = argv
    try:
        with mock.patch.dict(os.environ, {}, clear=True):
            cli.main()
    finally:
        sys.argv = original_argv
        api.reset_runtime_config()
    raw = capsys.readouterr().out
    payload = json.loads(raw)
    if isinstance(payload, dict) and payload.get("schema_version") == 2:
        payload = {**payload, **payload.get("data", {})}
    return payload


def test_resolve_from_linked_worktree_marks_descendant_fix(primary_and_linked_with_bug_and_fix: dict, capsys) -> None:
    """End-to-end: resolve from a linked worktree must see the linked
    HEAD as the descendant of the anchored finding commit, without any
    monkeypatching of git-context helpers."""
    primary = primary_and_linked_with_bug_and_fix["primary"]
    linked = primary_and_linked_with_bug_and_fix["linked"]
    bug_sha = primary_and_linked_with_bug_and_fix["bug_sha"]
    fix_sha = primary_and_linked_with_bug_and_fix["fix_sha"]

    _seed_open_finding_in_primary_db(primary, bug_sha)

    payload = _invoke_resolve_cli_from_linked(linked, capsys)

    assert payload["ok"] is True, payload
    assert payload["receipt"]["counts"]["fixed"] == 1, payload["receipt"]
    result_row = payload["receipt"]["results"][0]
    assert result_row["finding_id"] == "WORKSTATE-REF-RESOLVE-001"
    assert result_row["outcome"] == "fixed", result_row
    assert result_row["verified_commit_sha"] == fix_sha, result_row


# ---------------------------------------------------------------------------
# WORKSTATE-REF-09: caller OUTSIDE the worktree (the long-lived-MCP-server steady state).
#
# The fixture above runs the caller *inside* the linked worktree, so
# git_workspace_root == worktree. WORKSTATE-REF-09 fixes the case where the process
# checkout (git_workspace_root) is a *different* directory than the task
# worktree — the steady state for a server launched in root ``main``. The
# helpers below keep the caller's runtime pointed at the (separately dirtied)
# root checkout while the real linked worktree carries the task branch, and
# leave worktree derivation enabled so resolve must derive the task worktree
# from the row's ``target_branch``.
# ---------------------------------------------------------------------------

_TASK_BRANCH = "feature/test"
_FINDING_ID = "WORKSTATE-REF-RESOLVE-OUTSIDE-001"
_TASK_REF = "WORKSTATE-test-outside"
_MISSING_WORKTREE_BRANCH = "feature/missing-worktree"
_MISSING_WORKTREE_FINDING_ID = "WORKSTATE-REF-RESOLVE-MISSING-WORKTREE-001"
_MISSING_WORKTREE_TASK_REF = "WORKSTATE-test-missing-worktree"


def _receipt_of(envelope: dict) -> dict:
    """Pull the resolve receipt out of an API envelope (schema v2 nests it)."""
    data = envelope.get("data", envelope)
    return data["receipt"]


def _seed_outside_finding(primary: Path, finding_commit_sha: str) -> None:
    """Seed an open finding on a row whose ``target_branch`` is the task
    branch, with runtime pointed at the root checkout (caller outside)."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(primary))
    try:
        api.set_handoff_state(
            task_ref=_TASK_REF,
            objective="resolve from outside the worktree",
            status="in_progress",
            target_branch=_TASK_BRANCH,
        )
        api.record_review_finding(
            session="reviewer",
            finding_id=_FINDING_ID,
            severity="medium",
            file_path="module.py",
            description="defect introduced on the task branch",
            task_ref=_TASK_REF,
            actor={"agent": "reviewer", "branch": _TASK_BRANCH, "commit_sha": finding_commit_sha},
        )
    finally:
        api.reset_runtime_config()


def _resolve_outside(primary: Path) -> dict:
    """Run resolve via the MCP/API surface with runtime on the root checkout."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(primary))
    try:
        return api.resolve_review_findings(
            task_ref=_TASK_REF,
            finding_ids=[_FINDING_ID],
            session="WORKSTATE-resolve-outside",
            resolution_notes="Verified the task worktree carries the fix on its clean branch.",
        )
    finally:
        api.reset_runtime_config()


def _seed_missing_worktree_finding(primary: Path, finding_commit_sha: str) -> None:
    """Seed a finding whose target_branch has no linked worktree on disk."""
    api.configure_runtime(api.RuntimeConfig.for_workspace(primary))
    try:
        api.set_handoff_state(
            task_ref=_MISSING_WORKTREE_TASK_REF,
            objective="resolve with missing task worktree fallback",
            status="in_progress",
            target_branch=_MISSING_WORKTREE_BRANCH,
        )
        api.record_review_finding(
            session="reviewer",
            finding_id=_MISSING_WORKTREE_FINDING_ID,
            severity="medium",
            file_path="module.py",
            description="defect on a task whose worktree was removed",
            task_ref=_MISSING_WORKTREE_TASK_REF,
            actor={"agent": "reviewer", "branch": _MISSING_WORKTREE_BRANCH, "commit_sha": finding_commit_sha},
        )
    finally:
        api.reset_runtime_config()


def _resolve_missing_worktree(primary: Path) -> dict:
    api.configure_runtime(api.RuntimeConfig.for_workspace(primary))
    try:
        return api.resolve_review_findings(
            task_ref=_MISSING_WORKTREE_TASK_REF,
            finding_ids=[_MISSING_WORKTREE_FINDING_ID],
            session="WORKSTATE-resolve-missing-worktree",
            resolution_notes="Verified the missing worktree path falls back to the process checkout.",
        )
    finally:
        api.reset_runtime_config()


def test_resolve_dirty_root_clean_worktree_not_pending(primary_and_linked_with_bug_and_fix: dict, monkeypatch) -> None:
    """implementation note repro: a dirty root checkout must not yield a false
    ``pending_uncommitted`` when the task worktree is clean. Cleanliness is
    decided against the derived worktree, not the process checkout."""
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "0")
    primary = primary_and_linked_with_bug_and_fix["primary"]
    fix_sha = primary_and_linked_with_bug_and_fix["fix_sha"]

    # Dirty the ROOT checkout with an unrelated untracked file; the linked
    # worktree stays clean.
    (primary / "unrelated-root-junk.txt").write_text("dirty root\n")

    _seed_outside_finding(primary, fix_sha)
    envelope = _resolve_outside(primary)

    assert envelope["ok"] is True, envelope
    receipt = _receipt_of(envelope)
    assert receipt["has_uncommitted_changes"] is False, receipt
    row = receipt["results"][0]
    assert row["outcome"] != "pending_uncommitted", row


def test_resolve_dirty_worktree_still_refuses(primary_and_linked_with_bug_and_fix: dict, monkeypatch) -> None:
    """implementation note refusal: when the task worktree itself is dirty, resolve must
    still report ``pending_uncommitted`` and leave the finding open."""
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "0")
    primary = primary_and_linked_with_bug_and_fix["primary"]
    linked = primary_and_linked_with_bug_and_fix["linked"]
    fix_sha = primary_and_linked_with_bug_and_fix["fix_sha"]

    # The worktree itself is dirty; the root checkout is clean.
    (linked / "uncommitted-in-worktree.txt").write_text("dirty worktree\n")

    _seed_outside_finding(primary, fix_sha)
    envelope = _resolve_outside(primary)

    assert envelope["ok"] is True, envelope
    receipt = _receipt_of(envelope)
    assert receipt["has_uncommitted_changes"] is True, receipt
    row = receipt["results"][0]
    assert row["outcome"] == "pending_uncommitted", row


def test_resolve_outside_marks_fixed_with_task_branch_provenance(
    primary_and_linked_with_bug_and_fix: dict, monkeypatch
) -> None:
    """implementation note: dirty root + clean worktree + a finding anchored at the bug
    commit -> resolve marks the finding ``fixed`` using the worktree HEAD as
    the workspace commit, and the resolution provenance is the task
    branch/commit, never root ``main``."""
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "0")
    primary = primary_and_linked_with_bug_and_fix["primary"]
    bug_sha = primary_and_linked_with_bug_and_fix["bug_sha"]
    fix_sha = primary_and_linked_with_bug_and_fix["fix_sha"]
    root_main_head = _rev_parse(primary)  # the wrong anchor resolve must not use

    # Dirty the ROOT checkout; the linked worktree carries the fix and is clean.
    (primary / "unrelated-root-junk.txt").write_text("dirty root\n")

    # Finding anchored at the bug commit; the worktree HEAD (fix_sha) is its
    # descendant, so the workspace anchor is distinct from the finding anchor.
    _seed_outside_finding(primary, bug_sha)
    envelope = _resolve_outside(primary)

    assert envelope["ok"] is True, envelope
    receipt = _receipt_of(envelope)
    assert receipt["counts"]["fixed"] == 1, receipt
    # Provenance: the task branch + worktree HEAD, not root main, not the
    # finding's own anchor commit.
    assert receipt["workspace_branch"] == _TASK_BRANCH, receipt
    assert receipt["workspace_commit_sha"] == fix_sha, receipt
    assert receipt["workspace_commit_sha"] != root_main_head, receipt
    row = receipt["results"][0]
    assert row["outcome"] == "fixed", row
    assert row["verified_commit_sha"] == fix_sha, row


def test_resolve_missing_task_worktree_falls_back_to_process_checkout(
    primary_and_linked_with_bug_and_fix: dict, monkeypatch
) -> None:
    """Graceful fallback: resolve must not fail before it can classify a
    finding when target_branch has no matching linked worktree. Archived or
    torn-down task rows fall back to the process checkout instead."""
    primary = primary_and_linked_with_bug_and_fix["primary"]
    root_main_head = _rev_parse(primary)

    # Seed with the test-suite bypass because the row intentionally points at
    # a target_branch that has no worktree; then re-enable derivation for the
    # resolve under test.
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "1")
    _seed_missing_worktree_finding(primary, root_main_head)
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "0")

    envelope = _resolve_missing_worktree(primary)

    assert envelope["ok"] is True, envelope
    receipt = _receipt_of(envelope)
    assert receipt["workspace_branch"] == "main", receipt
    assert receipt["workspace_commit_sha"] == root_main_head, receipt
    assert receipt["counts"]["fixed"] == 1, receipt
    row = receipt["results"][0]
    assert row["outcome"] == "fixed", row
    assert row["verified_commit_sha"] == root_main_head, row
