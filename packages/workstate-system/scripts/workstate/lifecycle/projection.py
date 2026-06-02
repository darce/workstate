"""Projection adapter wrapper (implementation note implementation note).

Mirrors a lifecycle-runner decision into the handoff DB via
``mcp-workstate-handoff event record``. When the adapter is unavailable
the call must spool the payload to
``.task-state/pending-workflow-events.jsonl`` so a later
``make project-events-replay`` can retry it. Adapter failure never
blocks the primary git operation.

Status mapping:

* ``synced`` — the underlying CLI exited 0. The returned id is the
  one parsed out of the CLI's JSON response when present, otherwise
  the supplied ``decision_id`` (caller-id fallback matches the
  skill-broadcast wrapper).
* ``spooled`` — the CLI ran and rejected the payload (any non-zero
  returncode that is *not* a CLI-unreachable signal). The payload is
  appended to the pending-events spool so a later replay can retry,
  and the receipt surfaces the loud failure to the operator.
* ``pending`` — the CLI could not be invoked at all (missing binary
  or timeout). The payload is still spooled, but the receipt routes
  the case as a transient unreachability rather than a contract
  rejection.
* ``error`` — the caller passed a malformed payload (e.g. empty
  ``decision_id``); short-circuits before the adapter is touched.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import resolver

from handlers import _common

PENDING_EVENTS_REL = Path(".task-state") / "pending-workflow-events.jsonl"

# Returncodes that ``run_subprocess`` synthesises when the CLI cannot be
# invoked at all (missing binary -> 127, timeout -> 124). Anything else
# non-zero means the CLI ran and rejected the payload.
_CLI_UNREACHABLE_RETURNCODES = frozenset({124, 127})


def _classify_returncode(returncode: int) -> str:
    """Map a ``run_subprocess`` returncode to a projection status.

    Returns ``"synced"`` on success, ``"pending"`` when the CLI was
    unreachable, and ``"spooled"`` when the CLI ran but rejected the
    payload. WORKSTATE-REF-52 implementation note introduces the ``spooled`` split so
    operator-visible receipts distinguish loud rejections from
    transient unreachability.
    """
    if returncode == 0:
        return "synced"
    if returncode in _CLI_UNREACHABLE_RETURNCODES:
        return "pending"
    return "spooled"


def _spool(repo_root: Path, payload: dict[str, Any]) -> None:
    target = repo_root / PENDING_EVENTS_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=False) + "\n")


def project_decision(
    repo_root: Path,
    *,
    decision_id: str,
    rationale: str,
    session: str,
) -> tuple[str, str | None]:
    """Project a decision event into the handoff DB.

    Returns ``(status, returned_id)``. ``status`` is one of
    ``"synced"``, ``"spooled"``, ``"pending"``, or ``"error"``.
    ``returned_id`` is the decision id reported by the adapter (or the
    supplied ``decision_id`` when the adapter response is unparseable
    on a zero-exit invocation), and ``None`` when the call was spooled,
    pending, or rejected.
    """
    if not decision_id or not session:
        return "error", None

    proc = _common.run_subprocess(
        _common.handoff_command_argv(
            repo_root,
            "event",
            "--event-kind", "decision",
            "--session", session,
            "--decision", decision_id,
            "--rationale", rationale,
        )
    )
    status = _classify_returncode(proc.returncode)
    if status != "synced":
        _spool(
            repo_root,
            {
                "kind": "decision",
                "decision_id": decision_id,
                "rationale": rationale,
                "session": session,
            },
        )
        return status, None

    try:
        parsed = json.loads(proc.stdout)
    except (json.JSONDecodeError, ValueError):
        return "synced", decision_id

    candidate: Any = decision_id
    if isinstance(parsed, dict):
        data = parsed.get("data") if isinstance(parsed.get("data"), dict) else {}
        decision_obj = data.get("decision") if isinstance(data, dict) else None
        candidate = (
            (data.get("decision_id") if isinstance(data, dict) else None)
            or (decision_obj.get("id") if isinstance(decision_obj, dict) else None)
            or decision_id
        )
    return "synced", str(candidate)


def project_test_result(
    repo_root: Path,
    *,
    session: str,
    command: str,
    passed: bool,
    exit_code: int | None = None,
    result: str | None = None,
) -> str:
    """Project a test_result event into the handoff DB.

    Mirrors :func:`project_decision`'s shell-out pattern: shells out to
    ``mcp-workstate-handoff event --event-kind test_result``. On adapter
    failure, spools a ``test_result`` payload to the pending file so a
    later ``project-events-replay`` can drain it. Returns ``"synced"``,
    ``"spooled"`` (CLI ran and rejected), or ``"pending"`` (CLI
    unreachable).
    """
    if not session or not command:
        return "error"
    argv: list[str] = _common.handoff_command_argv(
        repo_root,
        "event",
        "--event-kind", "test_result",
        "--session", session,
        "--command", command,
    )
    # Pin verified-test provenance to the worktree HEAD that produced
    # the result, not the canonical workspace HEAD. Without these
    # overrides the handoff server attributes the row to whatever the
    # primary repo's branch/commit happens to be (e.g. main / a stale
    # tip), so linked-worktree slice-start runs land verified_tests
    # rows with the wrong (branch, commit_sha) tuple.
    worktree_branch = resolver.current_branch(repo_root)
    worktree_head = resolver.head_sha(repo_root)
    if worktree_branch:
        argv.extend(["--branch", worktree_branch])
    if worktree_head:
        argv.extend(["--commit-sha", worktree_head])
    if passed:
        argv.append("--passed")
    if exit_code is not None:
        argv.extend(["--exit-code", str(exit_code)])
    if result:
        argv.extend(["--result", result])

    proc = _common.run_subprocess(argv)
    status = _classify_returncode(proc.returncode)
    if status != "synced":
        spool_payload: dict[str, Any] = {
            "kind": "test_result",
            "session": session,
            "command": command,
            "passed": passed,
            "exit_code": exit_code,
            "result": result,
        }
        # Persist actor provenance alongside the payload so replay can
        # re-issue the event with matching --branch/--commit-sha and
        # avoid the drift the online path already guards against.
        if worktree_branch:
            spool_payload["branch"] = worktree_branch
        if worktree_head:
            spool_payload["commit_sha"] = worktree_head
        _spool(repo_root, spool_payload)
    return status


def project_state_sync(
    repo_root: Path,
    *,
    task_ref: str,
    target_branch: str,
    target_worktree_path: str,
    task_plan_path: str | None,
    status: str = "in_progress",
) -> str:
    """Project a handoff-state sync into the handoff DB.

    Shells out to ``mcp-workstate-handoff set`` with the git-derived task
    ref / branch / worktree path / plan path so the handoff state row
    follows the started task. Falls back to spooling a ``state_sync``
    payload to ``.task-state/pending-workflow-events.jsonl`` when the
    adapter is unavailable. Returns ``"synced"``, ``"spooled"`` (CLI
    ran and rejected), or ``"pending"`` (CLI unreachable).
    """
    argv = _common.handoff_command_argv(
        repo_root,
        "set",
        "--task-ref", task_ref,
        "--target-branch", target_branch,
        "--target-worktree-path", target_worktree_path,
        "--status", status,
    )
    if task_plan_path:
        argv.extend(["--task-plan-path", task_plan_path])
    if target_branch:
        argv.extend(["--branch", target_branch])
    target_head = resolver.head_sha(Path(target_worktree_path))
    if target_head:
        argv.extend(["--commit-sha", target_head])

    proc = _common.run_subprocess(argv)
    projection_status = _classify_returncode(proc.returncode)
    if projection_status != "synced":
        _spool(
            repo_root,
            {
                "kind": "state_sync",
                "task_ref": task_ref,
                "target_branch": target_branch,
                "target_worktree_path": target_worktree_path,
                "task_plan_path": task_plan_path,
                "status": status,
                "branch": target_branch,
                "commit_sha": target_head,
            },
        )
    return projection_status
