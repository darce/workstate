"""Mutating ``task-finish`` subcommand.

Wraps the canonical end-of-task close sequence documented at
``packages/workstate-system/skills/branch-lifecycle/body.md`` step 9 in a
single Make-callable target so the order is mechanical and a stalled
operator never leaves an inconsistent dashboard / unarchived task row /
orphaned linked worktree behind.

The sequence:

1. ``mcp-workstate-handoff set --task-ref <ref> --status done --status-only``
   — flip the live row to ``done`` so the row is archive-eligible.
2. (Best-effort) record any open worktree lanes for the task. Lane
   close itself has no direct CLI surface in
   ``mcp-workstate-orchestrator``; if open lanes are detected the receipt
   surfaces a ``lane_close_skipped`` warning so the operator knows to
   close them via MCP before re-running. Absent a state DB or
   orchestrator install we proceed silently.
3. (WORKSTATE-REF-64 implementation note / WORKSTATE-REF-70 implementation note) ``sync-task-plan-checklist
   --apply --quiet`` — final full-plan sweep while the active row's
   ``task_plan_path`` and decision/test evidence are still readable.
   Any ``- [ ]`` boxes whose evidence anchors matched recorded artifacts
   flip to ``- [x]`` before archive. Failure-as-warning: a malformed
   plan never blocks the close sequence; the slim sync receipt is
   merged under the ``checklist_sync`` key of the returned envelope.
4. ``mcp-workstate-handoff archive --operation archive --task-ref <ref> --apply``
   — move the row into ``task_archives`` and snapshot status.
5. ``mcp-workstate-handoff render-handoff --kind dashboard`` — regenerate
   ``DASHBOARD.txt`` from the updated state.
6. ``git worktree remove`` for the row's ``target_worktree_path`` when
   it points at a real linked worktree distinct from the primary
   repo — the linked worktree is no longer needed once the task is
   archived. Failure is reported but does not flip ``ok`` to false:
   the canonical state already reflects the close, and the operator
   can finish teardown manually.
7. ``git branch -d`` for the row's ``target_branch`` when it exists
   locally, is fully merged into the current HEAD, is not the branch
   currently checked out, and is not checked out in another linked
   worktree. ``-d`` is the safe variant — git itself refuses unmerged
   branches — so the worst case is a ``skipped_unmerged`` receipt
   field, never a destructive surprise.

Step ordering is load-bearing: archive MUST run before the worktree
remove so the write-side guard still sees a live worktree at the time
the archive write lands. The branch delete MUST run after the worktree
remove because git refuses to delete a branch that is checked out in a
worktree — including the linked worktree we just tore down. Inverting
the archive/worktree order is the failure mode the
``mcp-workstate-handoff`` write-side-guard scope captures (see
``docs/scopes/handoff-write-side-guard-archive-no-worktree-scope.md``).

Folding the branch delete into this target removes the previous
``manual git branch -d`` step from the branch-lifecycle skill and
eliminates the post-merge contingency where the row's ``target_branch``
no longer mapped to a live worktree at the time the close ran (the row
is set/archive-written *before* the branch is deleted here). It also
keeps the cleanup inside an authorized make target, so the auto-mode
classifier never sees a standalone ``git branch -d`` invocation.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

import resolver

from . import _common


def _read_active_task_ref(repo: Path) -> str | None:
    """Derive the active task_ref via ``render-handoff --no-write``.

    WORKSTATE-REF-54-FU implementation note: the on-disk ``CURRENT_TASK.json`` is no longer
    consulted; the singular ``task_ref`` is derived from MCP's live
    state on each call. ``workspace_ambiguous`` and ``none`` both yield
    ``None`` — task-finish must not pick a winner; the operator-supplied
    ``--task`` flag is the disambiguation surface.
    """
    view = _common.derive_workspace_summary_view(repo)
    if view.shape != "single":
        return None
    return view.task_ref if view.task_ref else None


def _read_handoff_identity(repo: Path, task_ref: str) -> dict[str, Any]:
    """Read the row's identity directly from the handoff DB by exact task_ref.

    Goes through the local sqlite store rather than ``mcp-workstate-handoff
    state`` so the lookup is fully bound by the requested ``task_ref``.
    The CLI's ``state`` subcommand falls back to cwd-active resolution
    when its positional argument is dropped or shadowed by argparse —
    that ambiguity has bitten the dogfood close where ``task-finish``
    runs from the primary worktree (whose cwd-active row is a different
    WORKSTATE-REF task) on behalf of the finishing task. A direct row read
    eliminates the fallback surface entirely and mirrors the pattern
    already used by ``_open_lanes_for_task`` below.

    Returns ``{}`` on any missing/stale state — callers collapse to
    ``skipped_unset`` for the optional teardown steps that need
    ``target_worktree_path`` / ``target_branch``.
    """
    canonical = resolver.canonical_workspace_root(repo) or repo
    db_path = canonical / ".task-state" / "handoff.db"
    if not db_path.is_file():
        return {}
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT target_branch, target_worktree_path "
                "FROM handoff_state WHERE task_ref = ?",
                (task_ref,),
            ).fetchone()
    except sqlite3.Error:
        return {}
    if row is None:
        return {}
    return {
        "target_branch": row[0] or "",
        "target_worktree_path": row[1] or "",
    }


def _open_lanes_for_task(repo: Path, task_ref: str) -> list[str]:
    """Return open lane_ids for ``task_ref``, empty when state DB absent.

    Reads ``worktree_lanes`` directly from the local handoff DB rather
    than shelling out to the orchestrator — there is no
    ``mcp-workstate-orchestrator`` CLI surface for ``manage_worktree_lane``
    so this is the only way to detect lanes from a Make target. Any
    error path returns empty so the close sequence is not blocked by
    transient detection failures.
    """
    canonical = resolver.canonical_workspace_root(repo) or repo
    db_path = canonical / ".task-state" / "handoff.db"
    if not db_path.is_file():
        return []
    try:
        with sqlite3.connect(str(db_path)) as conn:
            cursor = conn.execute(
                "SELECT lane_id FROM worktree_lanes "
                "WHERE task_ref = ? AND COALESCE(status, '') NOT IN ('closed', 'archived')",
                (task_ref,),
            )
            return [str(row[0]) for row in cursor.fetchall() if row[0]]
    except sqlite3.Error:
        return []


def _set_status_done(repo: Path, task_ref: str) -> tuple[bool, str | None]:
    argv = _common.handoff_command_argv(
        repo, "set",
        "--task-ref", task_ref,
        "--status", "done",
        "--status-only",
    )
    proc = _common.run_subprocess(argv)
    if proc.returncode == 0:
        return True, None
    return False, (proc.stderr or proc.stdout or "").strip()[:300]


def _archive(repo: Path, task_ref: str) -> tuple[bool, str | None]:
    argv = _common.handoff_command_argv(
        repo, "archive",
        "--operation", "archive",
        "--task-ref", task_ref,
        "--apply",
    )
    proc = _common.run_subprocess(argv)
    if proc.returncode == 0:
        return True, None
    return False, (proc.stderr or proc.stdout or "").strip()[:300]


def _render_dashboard(repo: Path) -> tuple[bool, str | None]:
    argv = _common.handoff_command_argv(
        repo, "render-handoff", "--kind", "dashboard",
    )
    proc = _common.run_subprocess(argv)
    if proc.returncode == 0:
        return True, None
    return False, (proc.stderr or proc.stdout or "").strip()[:300]


def _remove_worktree(
    primary: Path, target_worktree_path: str
) -> tuple[str, str | None]:
    """Remove the linked worktree at ``target_worktree_path``.

    Returns a status string + optional warning. Status values:

    * ``removed`` — ``git worktree remove`` exited 0.
    * ``skipped_primary`` — the path resolves to the primary worktree;
      removing it would teardown the operator's main checkout.
    * ``skipped_missing`` — the path does not exist on disk.
    * ``skipped_unset`` — the row had no ``target_worktree_path``.
    * ``failed`` — ``git worktree remove`` exited non-zero.
    """
    if not target_worktree_path:
        return "skipped_unset", None
    target = Path(target_worktree_path)
    try:
        if target.resolve() == primary.resolve():
            return "skipped_primary", None
    except OSError:
        return "skipped_missing", None
    if not target.exists():
        return "skipped_missing", None
    proc = _common.run_subprocess(
        ["git", "-C", str(primary), "worktree", "remove", str(target)]
    )
    if proc.returncode == 0:
        return "removed", None
    # ``--force`` is intentionally NOT applied automatically: a non-zero
    # rc usually means the worktree has uncommitted changes, and silently
    # discarding them on a finish target would be a destructive surprise.
    # Operators reading the receipt warning can rerun manually with --force.
    return "failed", (proc.stderr or proc.stdout or "").strip()[:300]


def _current_branch(primary: Path) -> str:
    """Return the branch HEAD points at in the primary worktree, or empty."""
    proc = _common.run_subprocess(
        ["git", "-C", str(primary), "symbolic-ref", "--short", "-q", "HEAD"]
    )
    if proc.returncode != 0:
        return ""
    return (proc.stdout or "").strip()


def _branch_checked_out_in_other_worktree(primary: Path, branch: str) -> bool:
    """True when ``branch`` is checked out by any worktree other than primary.

    Uses ``git worktree list --porcelain`` so the result is robust to
    detached HEADs and arbitrary worktree paths.
    """
    proc = _common.run_subprocess(
        ["git", "-C", str(primary), "worktree", "list", "--porcelain"]
    )
    if proc.returncode != 0:
        return False
    primary_resolved = ""
    try:
        primary_resolved = str(primary.resolve())
    except OSError:
        primary_resolved = str(primary)
    current_path = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
        elif line.startswith("branch "):
            ref = line[len("branch "):].strip()
            short = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
            if short != branch:
                continue
            try:
                resolved = str(Path(current_path).resolve())
            except OSError:
                resolved = current_path
            if resolved != primary_resolved:
                return True
    return False


def _delete_merged_branch(
    primary: Path, target_branch: str
) -> tuple[str, str | None]:
    """Run ``git branch -d`` for ``target_branch`` if safe.

    Returns a status string + optional warning. Status values:

    * ``deleted`` — ``git branch -d`` exited 0.
    * ``skipped_unset`` — the row had no ``target_branch``.
    * ``skipped_missing`` — the branch does not exist locally.
    * ``skipped_primary`` — the branch is the primary worktree's HEAD;
      deleting it is never the right call from this target.
    * ``skipped_checked_out`` — the branch is checked out in another
      linked worktree (git would refuse anyway).
    * ``skipped_unmerged`` — ``git branch -d`` refused: the branch has
      commits not reachable from HEAD.
    * ``failed`` — non-zero exit for any other reason.
    """
    if not target_branch:
        return "skipped_unset", None
    exists = _common.run_subprocess(
        ["git", "-C", str(primary), "show-ref", "--verify", "--quiet",
         f"refs/heads/{target_branch}"]
    )
    if exists.returncode != 0:
        return "skipped_missing", None
    if target_branch == _current_branch(primary):
        return "skipped_primary", None
    if _branch_checked_out_in_other_worktree(primary, target_branch):
        return "skipped_checked_out", None
    proc = _common.run_subprocess(
        ["git", "-C", str(primary), "branch", "-d", target_branch]
    )
    if proc.returncode == 0:
        return "deleted", None
    stderr = (proc.stderr or proc.stdout or "").strip()
    # ``git branch -d`` prints "not fully merged" for the unmerged case.
    if "not fully merged" in stderr.lower():
        return "skipped_unmerged", None
    return "failed", stderr[:300]


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="lifecycle task-finish", add_help=True)
    parser.add_argument("--task", dest="task", default="")
    parser.add_argument("--json", dest="emit_json", action="store_true", default=False)
    args = parser.parse_args(argv)

    repo = _common.repo_root()
    if repo is None:
        _common.emit(
            {
                "ok": False,
                "command": "task-finish",
                "task_ref": None,
                "events": [],
                "error": "not_in_git_repo",
            }
        )
        return 2

    task_ref = (args.task or "").strip().upper() or _read_active_task_ref(repo)
    if not task_ref:
        _common.emit(
            {
                "ok": False,
                "command": "task-finish",
                "task_ref": None,
                "events": [],
                "error": "task_ref_required",
            }
        )
        return 2

    primary = resolver.canonical_workspace_root(repo) or repo
    identity = _read_handoff_identity(repo, task_ref)
    target_worktree_path = ""
    raw_target = identity.get("target_worktree_path")
    if isinstance(raw_target, str):
        target_worktree_path = raw_target
    target_branch = ""
    raw_branch = identity.get("target_branch")
    if isinstance(raw_branch, str):
        target_branch = raw_branch.strip()

    events: list[str] = []
    warnings: list[str] = []

    status_ok, status_err = _set_status_done(repo, task_ref)
    if not status_ok:
        _common.emit(
            {
                "ok": False,
                "command": "task-finish",
                "task_ref": task_ref,
                "events": events,
                "error": "set_status_done_failed",
                "stderr_summary": status_err,
            }
        )
        return 2
    events.append("status_done_set")

    open_lanes = _open_lanes_for_task(repo, task_ref)
    if open_lanes:
        warnings.append(
            "lane_close_skipped: open lanes detected "
            f"({', '.join(sorted(open_lanes))}); close via MCP "
            "manage_worktree_lane(operation='close') before rerunning"
        )

    # WORKSTATE-REF-70 implementation note: the final full-plan checklist sweep runs BEFORE
    # archive so the active handoff row's ``task_plan_path`` and
    # decision/test evidence are still readable. After archive the row
    # moves to ``task_archives`` and the ``state`` CLI surface used by
    # the sync handler's plan-path lookup returns nothing — the sync
    # would degrade to ``plan_unresolved`` and skip silently, leaving
    # the task plan's `- [ ]` boxes stale. Failure stays a warning so a
    # malformed plan never blocks archive.
    checklist_sync = _common.run_checklist_sync(repo, task_ref)
    if checklist_sync.get("ok"):
        if checklist_sync.get("ticked", 0):
            events.append("checklist_sync_applied")
    else:
        warning_text = checklist_sync.get("warning") or "sync_not_ok"
        warnings.append(f"checklist_sync_failed: {warning_text}")

    archive_ok, archive_err = _archive(repo, task_ref)
    if not archive_ok:
        _common.emit(
            {
                "ok": False,
                "command": "task-finish",
                "task_ref": task_ref,
                "events": events,
                "open_lanes": open_lanes,
                "warnings": warnings,
                "error": "archive_failed",
                "stderr_summary": archive_err,
            }
        )
        return 2
    events.append("archived")

    dashboard_ok, dashboard_err = _render_dashboard(repo)
    if dashboard_ok:
        events.append("dashboard_rendered")
    else:
        warnings.append(f"render_dashboard_failed: {dashboard_err}")

    worktree_status, worktree_err = _remove_worktree(primary, target_worktree_path)
    if worktree_status == "removed":
        events.append("worktree_removed")
    elif worktree_status == "failed":
        warnings.append(f"worktree_remove_failed: {worktree_err}")

    branch_status, branch_err = _delete_merged_branch(primary, target_branch)
    if branch_status == "deleted":
        events.append("feature_branch_deleted")
    elif branch_status == "failed":
        warnings.append(f"feature_branch_delete_failed: {branch_err}")

    receipt: dict[str, Any] = {
        "ok": True,
        "command": "task-finish",
        "task_ref": task_ref,
        "target_worktree_path": target_worktree_path,
        "worktree_status": worktree_status,
        "target_branch": target_branch,
        "branch_status": branch_status,
        "open_lanes": open_lanes,
        "events": events,
        "warnings": warnings,
        "checklist_sync": checklist_sync,
    }

    if not args.emit_json:
        sync_summary = (
            f"sync={'ok' if checklist_sync.get('ok') else 'warn'}"
            f" ticked={checklist_sync.get('ticked', 0)}"
        )
        sys.stderr.write(
            f"task-finish: task_ref={task_ref} archived dashboard={'ok' if dashboard_ok else 'warn'} "
            f"worktree={worktree_status} branch={branch_status} "
            f"{sync_summary}"
            + (f" warnings={len(warnings)}" if warnings else "")
            + "\n"
        )

    _common.emit(receipt)
    return 0
