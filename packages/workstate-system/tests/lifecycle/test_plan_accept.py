"""Contract tests for the ``plan-accept`` lifecycle subcommand (WORKSTATE-REF-69 implementation note).

The handler gates on (a) the latest ``review_runs`` planning verdict
for the plan subject and (b) the open planning-finding count for the
task. The default (receipt-only) mode prints a docs-only commit recipe;
``--local`` runs it inline against a clean canonical-root main checkout.

Tests stub the mcp-workstate-handoff CLI by writing a small Python script
to ``$MCP_WORKSTATE_HANDOFF_BIN`` that switches on subcommand and returns
canned JSON. The handler routes its three queries (``state``,
``review-runs``, ``review-findings``) through this stub, so each case
seeds whichever combination of verdict and finding count it needs.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"
if str(LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG))

from handlers.plan_accept import (  # noqa: WORKSTATE-REF-402
    _dirty_paths,
    _is_worktree_clean_or_only_plan,
)


def _write_handoff_stub(
    target: Path,
    *,
    plan_path: str = "docs/plans/0099-demo.md",
    target_branch: str = "feature/WORKSTATE-99-demo",
    verdict: str | None = "pass",
    open_finding_count: int = 0,
    identity_present: bool = True,
    candidate_review_runs: list[dict[str, object]] | None = None,
) -> None:
    """Write a fake ``mcp-workstate-handoff`` CLI that the handler shells to.

    Recognises three subcommands: ``state``, ``review-runs``,
    ``review-findings``. Returns JSON shaped like the real CLI envelope.
    """
    runs_payload = (
        json.dumps({"ok": True, "data": {"runs": []}})
        if verdict is None
        else json.dumps(
            {"ok": True, "data": {"runs": [{"verdict": verdict}]}}
        )
    )
    findings_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "findings": [{"finding_id": f"F{i}"} for i in range(open_finding_count)],
                "counts": {"status": {"open": open_finding_count}},
            },
        }
    )
    identity_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "active": (
                    {
                        "task_ref": "WORKSTATE-REF-99",
                        "target_branch": target_branch,
                        "task_plan_path": plan_path,
                    }
                    if identity_present
                    else None
                )
            },
        }
    )
    candidate_runs_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "runs": candidate_review_runs or [],
            },
        }
    )
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--workspace-root', required=True)\n"
        "sub = p.add_subparsers(dest='subcommand', required=True)\n"
        "st = sub.add_parser('state')\n"
        "st.add_argument('--sections')\n"
        "st.add_argument('--detail')\n"
        "st.add_argument('task_ref', nargs='?')\n"
        "rr = sub.add_parser('review-runs')\n"
        "rr.add_argument('--operation', required=True)\n"
        "rr.add_argument('--task-ref')\n"
        "rr.add_argument('--subject-path')\n"
        "rr.add_argument('--review-mode')\n"
        "rr.add_argument('--limit', type=int, default=50)\n"
        "rr.add_argument('--offset', type=int, default=0)\n"
        "rr.add_argument('--verdict')\n"
        "rf = sub.add_parser('review-findings')\n"
        "rf.add_argument('--operation', required=True)\n"
        "rf.add_argument('--status', default='all')\n"
        "rf.add_argument('--task-ref')\n"
        "rf.add_argument('--review-mode')\n"
        "args = p.parse_args()\n"
        "if args.subcommand == 'state':\n"
        f"    sys.stdout.write({identity_payload!r})\n"
        "elif args.subcommand == 'review-runs':\n"
        f"    sys.stdout.write({runs_payload!r} if args.task_ref else {candidate_runs_payload!r})\n"
        "elif args.subcommand == 'review-findings':\n"
        f"    sys.stdout.write({findings_payload!r})\n"
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    _commit_plan_on_branch(repo, "feature/WORKSTATE-99-demo", "docs/plans/0099-demo.md")
    return repo


def _commit_plan_on_branch(repo: Path, branch: str, rel_path: str) -> None:
    subprocess.run(["git", "-C", str(repo), "switch", "-c", branch, "-q"], check=True)
    plan = repo / rel_path
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# demo plan\n")
    subprocess.run(["git", "-C", str(repo), "add", rel_path], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", f"add {rel_path}", "-q",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "switch", "main", "-q"], check=True)


def _status_porcelain(repo: Path) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True,
    ).strip()


def _install_failing_pre_commit(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def _run_plan_accept(
    repo: Path, *extra: str, fake_cli: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "plan-accept", *extra],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_plan_accept_ready_when_pass_and_zero_findings(git_repo: Path, tmp_path: Path) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, verdict="pass", open_finding_count=0)

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert receipt["reasons"] == [], receipt
    assert receipt["latest_planning_verdict"] == "pass"
    assert receipt["open_planning_findings"] == 0
    assert receipt["plan_exists_on_target_branch"] is True
    assert "git switch main" in receipt["next_command"]
    assert "docs/plans/0099-demo.md" in receipt["next_command"]
    assert "docs(WORKSTATE-99): accept plan" in receipt["next_command"]


def test_plan_accept_blocks_when_plan_missing_on_target_branch(
    git_repo: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(
        stub,
        plan_path="docs/plans/missing.md",
        verdict="pass",
        open_finding_count=0,
    )

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "plan_missing_on_target_branch" in receipt["reasons"], receipt
    assert receipt["plan_exists_on_target_branch"] is False
    assert receipt["next_command"] is None


def test_plan_accept_local_failure_restores_clean_main(
    git_repo: Path, tmp_path: Path
) -> None:
    _install_failing_pre_commit(git_repo)
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, verdict="pass", open_finding_count=0)

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--local",
        fake_cli=stub,
    )

    assert proc.returncode == 2, proc.stdout
    receipt = json.loads(proc.stdout)
    assert any(reason.startswith("commit_failed:") for reason in receipt["reasons"]), receipt
    assert _status_porcelain(git_repo) == ""
    assert not (git_repo / "docs/plans/0099-demo.md").exists()


@pytest.mark.parametrize(
    "verdict, expected_reason",
    [
        ("pass_with_findings", "latest_planning_verdict_pass_with_findings"),
        ("conditional_pass", "latest_planning_verdict_conditional_pass"),
        ("fail", "latest_planning_verdict_fail"),
    ],
)
def test_plan_accept_blocks_on_non_pass_verdicts(
    git_repo: Path, tmp_path: Path, verdict: str, expected_reason: str
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, verdict=verdict, open_finding_count=0)

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert expected_reason in receipt["reasons"], receipt
    assert receipt["next_command"] is None


def test_plan_accept_blocks_when_no_planning_review_recorded(
    git_repo: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, verdict=None, open_finding_count=0)

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False
    assert "no_planning_review_recorded" in receipt["reasons"]


def test_plan_accept_blocks_when_open_planning_findings(
    git_repo: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, verdict="pass", open_finding_count=2)

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False
    assert "open_planning_findings" in receipt["reasons"]
    assert receipt["open_planning_findings"] == 2


def test_plan_accept_uses_real_cli_state_subcommand(
    git_repo: Path, tmp_path: Path
) -> None:
    # Regression for WORKSTATE72-FULL-BR-01. The real mcp-workstate-handoff CLI
    # exposes ``state`` (positional task_ref, --sections), not
    # ``get-handoff-state``. A fake CLI that only recognises ``state``
    # mirrors the real shape; the handler must succeed against it.
    stub = tmp_path / "fake-real-cli"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--workspace-root', required=True)\n"
        "sub = p.add_subparsers(dest='subcommand', required=True)\n"
        "st = sub.add_parser('state')\n"
        "st.add_argument('--sections')\n"
        "st.add_argument('--detail')\n"
        "st.add_argument('task_ref', nargs='?')\n"
        "rr = sub.add_parser('review-runs')\n"
        "rr.add_argument('--operation', required=True)\n"
        "rr.add_argument('--task-ref')\n"
        "rr.add_argument('--subject-path')\n"
        "rr.add_argument('--review-mode')\n"
        "rr.add_argument('--limit', type=int, default=50)\n"
        "rr.add_argument('--offset', type=int, default=0)\n"
        "rr.add_argument('--verdict')\n"
        "rf = sub.add_parser('review-findings')\n"
        "rf.add_argument('--operation', required=True)\n"
        "rf.add_argument('--status', default='all')\n"
        "rf.add_argument('--task-ref')\n"
        "rf.add_argument('--review-mode')\n"
        "args = p.parse_args()\n"
        "if args.subcommand == 'state':\n"
        "    sys.stdout.write(json.dumps({'ok': True, 'data': {'active': {"
        "'task_ref': 'WORKSTATE-REF-99', "
        "'target_branch': 'feature/WORKSTATE-99-demo', "
        "'task_plan_path': 'docs/plans/0099-demo.md'}}}))\n"
        "elif args.subcommand == 'review-runs':\n"
        "    sys.stdout.write(json.dumps({'ok': True, 'data': {'runs': "
        "[{'verdict': 'pass'}]}}))\n"
        "elif args.subcommand == 'review-findings':\n"
        "    sys.stdout.write(json.dumps({'ok': True, 'data': {'findings': [], "
        "'counts': {'status': {'open': 0}}}}))\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 0, (proc.stdout, proc.stderr)
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert receipt["reasons"] == [], receipt
    assert receipt["target_branch"] == "feature/WORKSTATE-99-demo"
    assert receipt["task_plan_path"] == "docs/plans/0099-demo.md"


def test_plan_accept_identity_missing_receipt_includes_explicit_recovery(
    git_repo: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, identity_present=False)

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert receipt["reasons"] == ["handoff_state_unavailable"], receipt
    assert receipt["recovery_kind"] == "handoff_identity_missing"
    assert receipt["recovery_explanation"].startswith("No active handoff identity"), receipt
    assert receipt["safe_next_commands"][0] == {
        "command": 'make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS="--json --plan <task-plan-path> --source-branch <planning-branch>"',
        "reason": "explicit_plan_source_required",
    }


def test_plan_accept_explicit_plan_and_source_branch_succeeds_without_identity(
    git_repo: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, identity_present=False, verdict="pass", open_finding_count=0)

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--plan", "docs/plans/0099-demo.md",
        "--source-branch", "feature/WORKSTATE-99-demo",
        fake_cli=stub,
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert receipt["reasons"] == [], receipt
    assert receipt["task_plan_path"] == "docs/plans/0099-demo.md"
    assert receipt["target_branch"] == "feature/WORKSTATE-99-demo"
    assert receipt["plan_path_source"] == "cli_plan_arg"
    assert "git switch main" in receipt["next_command"]


def test_plan_accept_local_accepts_reviewed_untracked_plan_on_main(
    git_repo: Path, tmp_path: Path
) -> None:
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-new-task-plan.md"
    plan = git_repo / plan_path
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# reviewed same-session plan\n")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(
        stub,
        identity_present=False,
        verdict="pass",
        open_finding_count=0,
    )

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--local",
        "--plan", plan_path,
        "--source-branch", "main",
        fake_cli=stub,
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert receipt["accepted"] is True, receipt
    assert receipt["detail_reason"] == "untracked_draft_on_main"
    assert receipt["source_branch_state"] == "plan_untracked_on_main"
    assert _status_porcelain(git_repo) == ""
    assert "reviewed same-session plan" in subprocess.check_output(
        ["git", "-C", str(git_repo), "show", f"HEAD:{plan_path}"],
        text=True,
    )


def test_plan_accept_surfaces_wrong_ref_review_run_candidates(
    git_repo: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(
        stub,
        identity_present=False,
        verdict=None,
        candidate_review_runs=[
            {
                "id": 361,
                "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
                "verdict": "pass",
                "reviewed_at": "2026-05-20 18:44:00",
                "session": "sess-maint-review",
            }
        ],
    )

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--plan", "docs/plans/0099-demo.md",
        "--source-branch", "feature/WORKSTATE-99-demo",
        fake_cli=stub,
    )

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert receipt["recovery_kind"] == "wrong_ref_review_run"
    assert receipt["reasons"] == ["no_planning_review_recorded"], receipt
    assert receipt["candidate_review_task_refs"] == [
        {
            "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
            "review_run_id": 361,
            "verdict": "pass",
            "reviewed_at": "2026-05-20 18:44:00",
            "session": "sess-maint-review",
        }
    ]
    recovery_command = (
        'make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS="--json '
        "--plan docs/plans/0099-demo.md "
        "--source-branch feature/WORKSTATE-99-demo "
        '--review-task-ref WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520"'
    )
    assert receipt["next_command"] == recovery_command
    assert receipt["safe_next_commands"] == [
        {
            "command": recovery_command,
            "reason": "wrong_ref_review_run_recoverable",
        }
    ]


def test_plan_accept_wrong_ref_plain_text_surfaces_recovery(
    git_repo: Path, tmp_path: Path
) -> None:
    """implementation note B2/B4: the non-JSON (operator) refusal must name the candidate
    review task_ref and the exact recovery command, not just the opaque reason
    list. The recovery already exists in the --json receipt; surface it in the
    plain-text path too."""
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(
        stub,
        identity_present=False,
        verdict=None,
        candidate_review_runs=[
            {
                "id": 361,
                "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
                "verdict": "pass",
                "reviewed_at": "2026-05-20 18:44:00",
                "session": "sess-maint-review",
            }
        ],
    )

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--plan", "docs/plans/0099-demo.md",
        "--source-branch", "feature/WORKSTATE-99-demo",
        fake_cli=stub,
    )

    assert proc.returncode == 2, proc.stderr
    # Operator-facing stderr names the candidate review ref and a recovery line.
    assert "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520" in proc.stderr, proc.stderr
    assert "--review-task-ref" in proc.stderr, proc.stderr


def test_plan_accept_review_task_ref_accepts_cross_ref_evidence(
    git_repo: Path, tmp_path: Path
) -> None:
    # WORKSTATE-REF-08 implementation note: a named WORKSTATE-REF planning-review row can supply
    # acceptance evidence for the implementation task. The receipt keeps
    # the implementation task_ref identity and adds review_task_ref.
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, identity_present=False, verdict="pass", open_finding_count=0)

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--plan", "docs/plans/0099-demo.md",
        "--source-branch", "feature/WORKSTATE-99-demo",
        "--review-task-ref", "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
        fake_cli=stub,
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is True, receipt
    assert receipt["reasons"] == [], receipt
    assert receipt["task_ref"] == "WORKSTATE-REF-99"
    assert receipt["review_task_ref"] == "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520"
    assert receipt["latest_planning_verdict"] == "pass"
    assert receipt["open_planning_findings"] == 0
    assert "git switch main" in receipt["next_command"]


@pytest.mark.parametrize(
    "verdict, expected_reason",
    [
        ("pass_with_findings", "latest_planning_verdict_pass_with_findings"),
        ("conditional_pass", "latest_planning_verdict_conditional_pass"),
        ("fail", "latest_planning_verdict_fail"),
    ],
)
def test_plan_accept_review_task_ref_blocks_on_non_pass(
    git_repo: Path, tmp_path: Path, verdict: str, expected_reason: str
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, identity_present=False, verdict=verdict, open_finding_count=0)

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--plan", "docs/plans/0099-demo.md",
        "--source-branch", "feature/WORKSTATE-99-demo",
        "--review-task-ref", "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
        fake_cli=stub,
    )

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert expected_reason in receipt["reasons"], receipt
    assert receipt["review_task_ref"] == "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520"
    assert receipt["next_command"] is None


def test_plan_accept_review_task_ref_blocks_on_open_findings(
    git_repo: Path, tmp_path: Path
) -> None:
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, identity_present=False, verdict="pass", open_finding_count=2)

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--plan", "docs/plans/0099-demo.md",
        "--source-branch", "feature/WORKSTATE-99-demo",
        "--review-task-ref", "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
        fake_cli=stub,
    )

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "open_planning_findings" in receipt["reasons"], receipt
    assert receipt["open_planning_findings"] == 2
    assert receipt["review_task_ref"] == "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520"


def test_plan_accept_review_task_ref_blocks_on_subject_mismatch(
    git_repo: Path, tmp_path: Path
) -> None:
    # The named review task ref has no passing planning run for this exact
    # plan subject (the subject-filtered verdict query returns nothing).
    # Acceptance must block with an auditable subject-mismatch reason and
    # must not fall back to wrong-ref candidate discovery.
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, identity_present=False, verdict=None)

    proc = _run_plan_accept(
        git_repo,
        "--task", "WORKSTATE-REF-99",
        "--json",
        "--plan", "docs/plans/0099-demo.md",
        "--source-branch", "feature/WORKSTATE-99-demo",
        "--review-task-ref", "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
        fake_cli=stub,
    )

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert receipt["reasons"] == ["review_subject_mismatch"], receipt
    assert receipt["detail_reason"] == "review_subject_mismatch", receipt
    assert receipt["review_task_ref"] == "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520"
    assert receipt.get("recovery_kind") != "wrong_ref_review_run", receipt


def _commit_plan_on_main(repo: Path, rel_path: str) -> None:
    """Land ``rel_path`` on ``main`` so the baseline reads ``already_accepted``."""
    plan = repo / rel_path
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# demo plan on main\n")
    subprocess.run(["git", "-C", str(repo), "add", rel_path], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", f"accept {rel_path}", "-q",
        ],
        check=True,
    )


def test_plan_accept_already_accepted_is_noop_success(
    git_repo: Path, tmp_path: Path
) -> None:
    # WORKSTATE-REF-05 implementation note: re-running plan-accept after the plan is already on
    # ``main`` is a satisfied lifecycle state, not a failure. It must exit
    # zero and point at the next useful lifecycle command (task-start).
    _commit_plan_on_main(git_repo, "docs/plans/0099-demo.md")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, verdict="pass", open_finding_count=0)

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["ready"] is True, receipt
    assert receipt["reasons"] == [], receipt
    assert receipt["baseline_status"] == "accepted", receipt
    assert receipt["reason"] == "already_accepted", receipt
    assert receipt["recovery_kind"] == "already_accepted", receipt
    assert "make task-start TASK=WORKSTATE-REF-99" in receipt["next_command"], receipt
    assert receipt["safe_next_commands"] == [
        {
            "command": receipt["next_command"],
            "reason": "already_accepted",
        }
    ], receipt


def test_plan_accept_already_accepted_does_not_promote_other_failures(
    git_repo: Path, tmp_path: Path
) -> None:
    # The no-op promotion is scoped to ``already_accepted``. A missing
    # baseline (plan absent from both main and the source branch) must
    # still block with exit 2.
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(
        stub,
        plan_path="docs/plans/missing.md",
        verdict="pass",
        open_finding_count=0,
    )

    proc = _run_plan_accept(git_repo, "--task", "WORKSTATE-REF-99", "--json", fake_cli=stub)

    assert proc.returncode == 2, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert receipt.get("recovery_kind") != "already_accepted", receipt


def _init_repo_with_commit(root: Path) -> Path:
    repo = root / "dirty-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "tracked file.txt").write_text("seed\n")
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "add", "-A"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-qm", "init"],
        check=True,
    )
    return repo


def test_dirty_paths_returns_verbatim_paths_and_skips_rename_origin(
    tmp_path: Path,
) -> None:
    # Regression for branch_review_main_dirty_dirty_paths_quoting: -z parsing
    # returns unquoted paths (even with spaces) and must not leak the original
    # path of a rename record into the dirty set.
    repo = _init_repo_with_commit(tmp_path)
    subprocess.run(
        ["git", "-C", str(repo), "mv", "tracked file.txt", "renamed file.txt"],
        check=True,
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "plan with space.md").write_text("draft\n")

    paths = _dirty_paths(repo)

    assert paths is not None
    # The rename's new path is present verbatim; its origin is skipped.
    assert sorted(paths) == ["docs/plan with space.md", "renamed file.txt"]
    assert "tracked file.txt" not in paths


def test_is_worktree_clean_or_only_plan_matches_spaced_plan_path(
    tmp_path: Path,
) -> None:
    # The only-the-plan-is-dirty check must hold when the plan path contains a
    # space, which default porcelain quoting would have broken.
    repo = _init_repo_with_commit(tmp_path)
    (repo / "docs").mkdir()
    plan_rel = "docs/plan with space.md"
    (repo / plan_rel).write_text("draft\n")

    assert _is_worktree_clean_or_only_plan(repo, plan_rel) is True

    (repo / "other.txt").write_text("noise\n")
    assert _is_worktree_clean_or_only_plan(repo, plan_rel) is False
