"""WORKSTATE-REF-54 implementation note → WORKSTATE-REF-66 refinement — task-start ambiguity guard.

Pinned discriminator (CTP-WORKSTATE-REF-T-01): a task is treated as
*planning/maintenance* iff ``target_branch == "main"``; otherwise it is
*implementation* and subject to the (worktree-scoped, post-WORKSTATE-REF-66)
singleton invariant.

OQ2 cases (post-WORKSTATE-REF-66 refinement, see
``docs/tasks/WORKSTATE-REF-66-task-start-identity-resolution-task-plan.md``):

1. ``workspace_ambiguous`` + requested ``task_ref`` listed → allow
   (resume).
2. ``workspace_ambiguous`` + requested ``task_ref`` NOT listed → **case
   split by real-conflict detection** (WORKSTATE-REF-66 supersedes the prior
   WORKSTATE-REF-54 implementation note "uniformly refuse" decision). The guard now invokes
   ``_detect_real_conflict`` against the workspace summary plus the
   filesystem/git state:

   - (a) explicit fresh task with unclaimed target branch + worktree
     path → **allow** (the WORKSTATE-REF-66 motivating case; an operator
     starting a new task in a sibling worktree is the dominant intent).
   - (b) same task already live in another worktree
     (``same_task_elsewhere``, policy) → refuse.
   - (c) target branch already attached to a different worktree, or
     present locally without a worktree (``branch_collision``,
     collision) → refuse.
   - (d) derived worktree path already exists / claimed
     (``worktree_path_collision``, collision) → refuse.
   - (e) ``MODE=here`` against a primary checkout attached to a
     different implementation task
     (``mode_here_implementation_conflict``, policy) → refuse.

3. ``single`` + active is implementation + request is for a different
   implementation task → refuse (preserves worktree-singleton; the v1
   variant of this case is covered by
   ``tests/lifecycle/test_task_start.py::test_task_start_ambiguity_hard_stops_before_mutating_git``).
4. ``single`` + active is planning/maintenance + request is for an
   implementation task → allow (the new feature-branch worktree is a
   sibling and never displaces the on-main planning row).

WORKSTATE-REF-66 introduced ``_detect_real_conflict`` as the pure-function
replacement for the pre-WORKSTATE-REF-66 ``workspace_ambiguous`` veto, plus the
``conflict_kind`` / ``conflict_category`` additive receipt fields.
Helper-level unit tests live next door at
``tests/lifecycle/test_detect_real_conflict.py``.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "--allow-empty",
            "-m",
            "init",
            "-q",
        ],
        check=True,
    )
    return repo


@pytest.fixture
def fake_cli_dir(tmp_path: Path) -> Path:
    return tmp_path / "fake-cli"


def _run_task_start(
    cwd: Path,
    fake_cli: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "task-start", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _per_task_payload(
    task_ref: str,
    *,
    target_branch: str,
    status: str = "in_progress",
    target_worktree_path: str = "",
) -> dict:
    return {
        "task_projection_schema_version": 1,
        "task_ref": task_ref,
        "status": status,
        "objective": f"objective for {task_ref}",
        "focus": "",
        "target_branch": target_branch,
        "target_worktree_path": target_worktree_path,
        "task_plan_path": None,
        "revision": 1,
        "updated_at": "2026-05-10T00:00:00Z",
    }


def _install_summary_cli(fake_cli: Path, summary: dict | None) -> None:
    """Install a fake ``mcp-workstate-handoff`` binary that emits the given v2
    workspace summary as the ``render-handoff`` envelope's
    ``current_task_json`` field.

    WORKSTATE-REF-54-FU flipped the lifecycle handlers to derive the workspace
    summary from ``mcp-workstate-handoff render-handoff --no-write`` rather
    than the on-disk ``CURRENT_TASK.json``. These integration tests
    therefore need the fake CLI to *answer* render-handoff probes with a
    realistic envelope; writing CURRENT_TASK.json on its own is a no-op
    against the live derive-on-read path.

    Pass ``summary=None`` to emit ``shape="none"`` (an empty
    ``current_task_json`` field, which fail-opens to ``_NONE_VIEW``).
    """
    envelope_payload = "" if summary is None else json.dumps(summary)
    envelope = json.dumps({"data": {"current_task_json": envelope_payload}})
    safe = envelope.replace("'", "'\\''")
    # ``handoff_command_argv`` prepends ``--workspace-root <path>`` before the
    # subcommand, so the fake CLI scans every argv slot for ``render-handoff``
    # rather than pinning on $1.
    body = (
        "#!/usr/bin/env bash\n"
        'for arg in "$@"; do\n'
        '  if [[ "$arg" == "render-handoff" ]]; then\n'
        f"    printf '%s' '{safe}'\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "exit 0\n"
    )
    fake_cli.write_text(body)
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _workspace_ambiguous_summary(tasks: list[dict]) -> dict:
    return {
        "schema_version": 2,
        "shape": "workspace_ambiguous",
        "tasks": tasks,
    }


def _v2_single_summary(task_ref: str, target_branch: str, **kwargs) -> dict:
    active = _per_task_payload(task_ref, target_branch=target_branch, **kwargs)
    return {
        "schema_version": 2,
        "shape": "single",
        "task_ref": task_ref,
        "active": active,
    }


# ---------------------------------------------------------------------------
# OQ2 Case 1: workspace_ambiguous + listed → allow
# ---------------------------------------------------------------------------


def test_oq2_case1_ambiguous_listed_allows_resume(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """``workspace_ambiguous`` listing the requested task_ref → allow.

    Two planning/maintenance tasks share the workspace on ``main``; the
    operator runs ``task-start --task <one-of-them>`` to switch to its
    feature branch. The guard must not refuse — the task is already
    known to this workspace and ``_detect_real_conflict`` finds nothing
    claiming the target branch or derived worktree path.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload("WORKSTATE-REF-50", target_branch="main"),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt.get("conflict_kind") is None
    assert receipt.get("conflict_category") is None
    assert "ambiguity_resolved" not in receipt["events"]
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-50"


# ---------------------------------------------------------------------------
# OQ2 Case 2a: workspace_ambiguous + not listed, no real conflict → allow
# (WORKSTATE-REF-66 inversion of the pre-WORKSTATE-REF-66 "uniformly refuse" decision)
# ---------------------------------------------------------------------------


def test_oq2_case2a_explicit_fresh_in_ambiguous_workspace_allows(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """``workspace_ambiguous`` without the requested task_ref AND no
    real conflict (target branch + derived path are unclaimed) → allow.

    The WORKSTATE-REF-66 motivating case: the workspace already runs a couple of
    planning/maintenance rows on ``main`` and the operator explicitly
    starts a fresh implementation task in a sibling worktree. The
    pre-WORKSTATE-REF-66 veto refused this; the WORKSTATE-REF-66 claim-aware guard
    allows it because nothing on disk or in the live-row set actually
    collides with the request.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload("WORKSTATE-REF-98", target_branch="main"),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt.get("conflict_kind") is None
    assert receipt.get("conflict_category") is None
    assert "ambiguity_resolved" not in receipt["events"]
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-50"


# ---------------------------------------------------------------------------
# OQ2 Case 2b: workspace_ambiguous + same_task_elsewhere → refuse (policy)
# ---------------------------------------------------------------------------


def test_oq2_case2b_same_task_elsewhere_refuses(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """Requested task is already live in a different worktree → refuse
    with ``conflict_kind="same_task_elsewhere"`` (policy conflict).

    Even though the listing includes WORKSTATE-REF-50, its live row points at a
    sibling worktree path, so starting it again here would clone the
    same identity across two worktrees.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    elsewhere = str(tmp_path / "elsewhere-WORKSTATE-50")
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload(
                    "WORKSTATE-REF-50",
                    target_branch="feature/WORKSTATE-50",
                    target_worktree_path=elsewhere,
                ),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt.get("error") == "task_ref_ambiguous"
    assert receipt.get("conflict_kind") == "same_task_elsewhere"
    assert receipt.get("conflict_category") == "policy"
    assert "ambiguity_resolved" in receipt["events"]
    assert _git(git_repo, "branch", "--show-current") == "main"
    assert _git(git_repo, "branch", "--list", "feature/WORKSTATE-50") == ""


# ---------------------------------------------------------------------------
# OQ2 Case 2c: workspace_ambiguous + branch_collision → refuse (collision)
# ---------------------------------------------------------------------------


def test_oq2_case2c_branch_collision_refuses(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """Target branch's worktree is owned by a *different* live task →
    refuse with ``conflict_kind="branch_collision"`` (collision).

    WORKSTATE-REF-05 implementation note narrows ``branch_collision`` to the unsafe case: the
    requested task's branch already has a worktree AND a live row for
    another task claims that exact path. (The unowned variant is now a
    claim candidate — see ``test_oq2_existing_unowned_worktree_*``.)
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    other_wt = tmp_path / "other-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/WORKSTATE-50",
            str(other_wt),
        ],
        check=True,
    )
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload(
                    "WORKSTATE-REF-77",
                    target_branch="feature/WORKSTATE-77",
                    target_worktree_path=str(other_wt),
                ),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt.get("error") == "task_ref_ambiguous"
    assert receipt.get("conflict_kind") == "branch_collision"
    assert receipt.get("conflict_category") == "collision"
    assert "ambiguity_resolved" in receipt["events"]
    assert _git(git_repo, "branch", "--show-current") == "main"


def test_oq2_existing_unowned_worktree_emits_claim_recovery(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-05 implementation note: the requested task's own branch already has a
    linked worktree with no live owning row. Instead of telling the
    operator to delete it, the receipt surfaces a ``claim_existing_worktree``
    recovery with a supported claim command and HEAD/branch/worktree
    evidence — and does not mutate git.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    existing_wt = tmp_path / "existing-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/WORKSTATE-50",
            str(existing_wt),
        ],
        check=True,
    )
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload("WORKSTATE-REF-98", target_branch="main"),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt.get("recovery_kind") == "claim_existing_worktree"
    assert receipt.get("conflict_kind") == "claim_existing_worktree"
    assert receipt.get("conflict_category") == "recoverable"
    assert receipt.get("worktree_path") == str(existing_wt)
    assert len(receipt.get("head") or "") == 40
    commands = [c["command"] for c in receipt.get("safe_next_commands") or []]
    assert any("MODE=claim" in c and "WORKSTATE-REF-50" in c for c in commands), commands
    # No git mutation: the existing worktree is untouched and no canonical
    # sibling was created.
    assert not (git_repo.parent / f"{git_repo.name}-WORKSTATE-50").exists()
    assert _git(git_repo, "branch", "--show-current") == "main"


def test_oq2_claim_mode_binds_existing_worktree(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-05 implementation note: ``MODE=claim`` binds a pre-existing unowned
    worktree to the task row through the normal projection path, exits
    zero, and leaves the worktree intact (reused, not recreated).
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    existing_wt = tmp_path / "existing-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/WORKSTATE-50",
            str(existing_wt),
        ],
        check=True,
    )
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload("WORKSTATE-REF-98", target_branch="main"),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "claim",
        "--json",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["mode"] == "claim"
    assert receipt["created_branch"] is False
    assert receipt["reused_worktree"] is True
    assert receipt["recovery_kind"] == "claim_existing_worktree"
    assert receipt["worktree_path"] == str(existing_wt)
    assert len(receipt["head"]) == 40
    assert "claimed_existing_worktree" in receipt["events"]
    # Worktree left intact; no canonical sibling created.
    assert existing_wt.is_dir()
    assert _git(existing_wt, "branch", "--show-current") == "feature/WORKSTATE-50"
    assert not (git_repo.parent / f"{git_repo.name}-WORKSTATE-50").exists()


def test_oq2_claim_mode_owned_by_other_live_row_still_blocks(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-05 implementation note: even in ``MODE=claim``, a worktree owned by a
    different live task hard-blocks with ``branch_collision`` and no
    mutation. Claim is for unowned worktrees only.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    other_wt = tmp_path / "other-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/WORKSTATE-50",
            str(other_wt),
        ],
        check=True,
    )
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload(
                    "WORKSTATE-REF-77",
                    target_branch="feature/WORKSTATE-77",
                    target_worktree_path=str(other_wt),
                ),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "claim",
        "--json",
    )
    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt.get("conflict_kind") == "branch_collision"
    assert receipt.get("error") == "task_ref_ambiguous"


# ---------------------------------------------------------------------------
# OQ2 Case 2d: workspace_ambiguous + worktree_path_collision → refuse
# ---------------------------------------------------------------------------


def test_oq2_case2d_worktree_path_collision_refuses(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """Derived sibling worktree path already exists → refuse with
    ``conflict_kind="worktree_path_collision"`` (collision)."""
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    # Sibling-of-primary derive convention: <primary>-<task-ref-lower>.
    derived = git_repo.parent / f"{git_repo.name}-WORKSTATE-50"
    derived.mkdir()
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload("WORKSTATE-REF-98", target_branch="main"),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt.get("error") == "task_ref_ambiguous"
    assert receipt.get("conflict_kind") == "worktree_path_collision"
    assert receipt.get("conflict_category") == "collision"
    assert "ambiguity_resolved" in receipt["events"]
    assert _git(git_repo, "branch", "--list", "feature/WORKSTATE-50") == ""


# ---------------------------------------------------------------------------
# OQ2 Case 2e: workspace_ambiguous + mode_here_implementation_conflict → refuse
# ---------------------------------------------------------------------------


def test_oq2_case2e_mode_here_implementation_conflict_refuses(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """MODE=here against a primary attached to a different live
    implementation task → refuse with
    ``conflict_kind="mode_here_implementation_conflict"`` (policy)."""
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-99"],
        check=True,
    )
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload(
                    "WORKSTATE-REF-99",
                    target_branch="feature/WORKSTATE-99",
                    target_worktree_path=str(git_repo),
                ),
                _per_task_payload("WORKSTATE-REF-TASK-01", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt.get("error") == "task_ref_ambiguous"
    assert receipt.get("conflict_kind") == "mode_here_implementation_conflict"
    assert receipt.get("conflict_category") == "policy"
    assert "ambiguity_resolved" in receipt["events"]
    # Primary HEAD stays on the prior implementation branch (no displacement).
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-99"
    assert _git(git_repo, "branch", "--list", "feature/WORKSTATE-50") == ""


# ---------------------------------------------------------------------------
# OQ2 Case 2f: workspace_ambiguous + same task listed with EXISTING linked
# worktree → allow (legitimate reuse). WORKSTATE-REF-66-BR-01 regression: before
# the fix, `_detect_real_conflict` over-fired `same_task_elsewhere` against
# the listed live row's worktree path, killing the canonical
# `_find_linked_worktree_for_branch` reuse path in task_start.py.
# ---------------------------------------------------------------------------


def test_oq2_case2f_same_task_with_existing_worktree_allows_reuse(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """workspace_ambiguous summary lists WORKSTATE-REF-50 with target_worktree_path
    pointing at an existing linked worktree for ``feature/WORKSTATE-50``. The
    guard must not refuse — this is the canonical resume-in-own-worktree
    case. task-start should fall through to the existing reuse path and
    return ``reused_worktree=True`` without an ``ambiguity_resolved``
    event.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    # Use the sibling-of-primary derived path so it lines up with the
    # convention task-start would itself pick for WORKSTATE-REF-50.
    reuse_worktree = git_repo.parent / f"{git_repo.name}-WORKSTATE-50"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/WORKSTATE-50",
            str(reuse_worktree),
        ],
        check=True,
    )
    _install_summary_cli(
        fake_cli,
        _workspace_ambiguous_summary(
            [
                _per_task_payload(
                    "WORKSTATE-REF-50",
                    target_branch="feature/WORKSTATE-50",
                    target_worktree_path=str(reuse_worktree),
                ),
                _per_task_payload("WORKSTATE-REF-99", target_branch="main"),
            ]
        ),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt.get("conflict_kind") is None
    assert receipt.get("conflict_category") is None
    assert "ambiguity_resolved" not in receipt["events"]
    assert receipt.get("reused_worktree") is True
    assert receipt.get("worktree_path") == str(reuse_worktree)


# ---------------------------------------------------------------------------
# OQ2 Case 3: single + active impl + different impl → refuse
# (preserved by the single-shape worktree-singleton invariant, not by
# ``_detect_real_conflict`` — so conflict_kind / conflict_category are None
# on this receipt path.)
# ---------------------------------------------------------------------------


def test_oq2_case3_single_implementation_refuses_different_impl(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """``single`` shape with an implementation active → refuse a
    different implementation request (preserves worktree-singleton).

    Asserted under the v2 ``single`` shape so the migrated guard is
    pinned against the live writer flip in implementation note — the v1 variant of
    this case is already covered by
    ``tests/lifecycle/test_task_start.py::test_task_start_ambiguity_hard_stops_before_mutating_git``.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _install_summary_cli(
        fake_cli,
        _v2_single_summary("WORKSTATE-REF-99", target_branch="feature/WORKSTATE-99"),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt.get("error") == "task_ref_ambiguous"
    assert "ambiguity_resolved" in receipt["events"]
    # Single-shape invariant refusal does not run _detect_real_conflict;
    # additive fields are present but None on this path.
    assert receipt.get("conflict_kind") is None
    assert receipt.get("conflict_category") is None
    assert _git(git_repo, "branch", "--show-current") == "main"
    assert _git(git_repo, "branch", "--list", "feature/WORKSTATE-50") == ""


# ---------------------------------------------------------------------------
# OQ2 Case 4: single + active planning/maintenance + impl request → allow
# ---------------------------------------------------------------------------


def test_oq2_case4_single_planning_allows_impl_request(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """``single`` shape with a planning/maintenance task active
    (``target_branch == "main"``) → allow a new implementation request.

    Planning/maintenance work on ``main`` does not contend with a
    sibling feature-branch worktree, so the guard must not block.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _install_summary_cli(
        fake_cli,
        _v2_single_summary("WORKSTATE-REF-TASK-01", target_branch="main"),
    )

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-50",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt.get("conflict_kind") is None
    assert receipt.get("conflict_category") is None
    assert "ambiguity_resolved" not in receipt["events"]
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-50"
