"""Contract tests for the shared plan-baseline evaluator (WORKSTATE-REF-72 implementation note)."""

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

from handlers.plan_baseline import evaluate_plan_baseline, is_planning_path


def _write_handoff_stub(
    target: Path,
    *,
    verdict: str | None = "pass",
    open_finding_count: int = 0,
    fail_review_runs: bool = False,
    fail_findings: bool = False,
    candidate_review_runs: list[dict[str, object]] | None = None,
) -> None:
    runs_payload = (
        json.dumps({"ok": True, "data": {"runs": []}})
        if verdict is None
        else json.dumps({"ok": True, "data": {"runs": [{"verdict": verdict}]}})
    )
    findings_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "findings": [
                    {"finding_id": f"F{index}"}
                    for index in range(open_finding_count)
                ],
                "counts": {"status": {"open": open_finding_count}},
            },
        }
    )
    candidate_runs_payload = json.dumps(
        {"ok": True, "data": {"runs": candidate_review_runs or []}}
    )
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--workspace-root', required=True)\n"
        "sub = p.add_subparsers(dest='subcommand', required=True)\n"
        "rr = sub.add_parser('review-runs')\n"
        "rr.add_argument('--operation', required=True)\n"
        "rr.add_argument('--task-ref')\n"
        "rr.add_argument('--subject-path')\n"
        "rr.add_argument('--review-mode')\n"
        "rr.add_argument('--limit')\n"
        "rf = sub.add_parser('review-findings')\n"
        "rf.add_argument('--operation', required=True)\n"
        "rf.add_argument('--status')\n"
        "rf.add_argument('--task-ref')\n"
        "rf.add_argument('--review-mode')\n"
        "args = p.parse_args()\n"
        "if args.subcommand == 'review-runs':\n"
        f"    sys.exit(3) if {fail_review_runs!r} else sys.stdout.write({runs_payload!r} if args.task_ref else {candidate_runs_payload!r})\n"
        "elif args.subcommand == 'review-findings':\n"
        f"    sys.exit(4) if {fail_findings!r} else sys.stdout.write({findings_payload!r})\n"
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def git_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
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
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub)
    monkeypatch.setenv("MCP_WORKSTATE_HANDOFF_BIN", str(stub))
    return repo


def _commit_plan(repo: Path, branch: str, plan_path: str) -> None:
    if branch == "main":
        subprocess.run(["git", "-C", str(repo), "switch", "main", "-q"], check=True)
    else:
        subprocess.run(["git", "-C", str(repo), "switch", "-c", branch, "-q"], check=True)
    target = repo / plan_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# demo plan\n")
    subprocess.run(["git", "-C", str(repo), "add", plan_path], check=True)
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
            "-m",
            f"add {plan_path}",
            "-q",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "switch", "main", "-q"], check=True)


def test_evaluator_reports_missing_baseline_ready_for_acceptance(git_repo: Path) -> None:
    _commit_plan(git_repo, "feature/WORKSTATE-99", "docs/plans/0099-demo.md")

    result = evaluate_plan_baseline(
        git_repo,
        task_ref="WORKSTATE-REF-99",
        task_plan_path="docs/plans/0099-demo.md",
        target_branch="feature/WORKSTATE-99",
    )

    assert result.baseline_status == "missing"
    assert result.baseline_exists_on_main is False
    assert result.plan_exists_on_target_branch is True
    assert result.latest_planning_verdict == "pass"
    assert result.open_planning_findings == 0
    assert result.acceptance_ready is True
    assert result.reason == "plan_baseline_missing"
    assert result.next_command == "make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS=--json"
    assert result.detail_reason == "accepted_baseline_missing"
    assert result.plan_path_source == "task_plan_path"
    assert result.identity_state == "active_task_identity"
    assert result.source_branch_state == "plan_present"
    assert result.safe_next_commands == [
        {
            "command": "make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS=--json",
            "reason": "accepted_baseline_missing",
        }
    ]


def test_evaluator_reports_accepted_baseline(git_repo: Path) -> None:
    _commit_plan(git_repo, "main", "docs/plans/0099-demo.md")

    result = evaluate_plan_baseline(
        git_repo,
        task_ref="WORKSTATE-REF-99",
        task_plan_path="docs/plans/0099-demo.md",
        target_branch="feature/WORKSTATE-99",
    )

    assert result.baseline_status == "accepted"
    assert result.baseline_exists_on_main is True
    assert result.acceptance_ready is False
    assert result.reason == "already_accepted"
    assert result.next_command is None


def test_evaluator_reports_untracked_planning_draft_on_main(git_repo: Path) -> None:
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    target = git_repo / plan_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# draft\n")

    result = evaluate_plan_baseline(
        git_repo,
        task_ref="WORKSTATE-REF-99",
        task_plan_path=plan_path,
        target_branch="feature/WORKSTATE-99",
    )

    assert result.plan_untracked_on_main is True
    assert result.reason == "plan_baseline_missing"
    assert result.latest_planning_verdict == "pass"
    assert result.open_planning_findings == 0
    assert result.acceptance_ready is True
    assert result.detail_reason == "untracked_draft_on_main"
    assert result.plan_path_source == "task_plan_path"
    assert result.source_branch_state == "plan_untracked_on_main"
    assert result.safe_next_commands == [
        {
            "command": "make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS=\"--json --local --plan packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md --source-branch main\"",
            "reason": "untracked_draft_on_main",
        }
    ]
    assert is_planning_path(plan_path) is True


def test_evaluator_surfaces_wrong_ref_planning_review_candidates(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan_path = "docs/plans/0099-demo.md"
    _commit_plan(git_repo, "feature/WORKSTATE-99", plan_path)
    stub = tmp_path / "fake-mcp-wrong-ref"
    _write_handoff_stub(
        stub,
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
    monkeypatch.setenv("MCP_WORKSTATE_HANDOFF_BIN", str(stub))

    result = evaluate_plan_baseline(
        git_repo,
        task_ref="WORKSTATE-REF-99",
        task_plan_path=plan_path,
        target_branch="feature/WORKSTATE-99",
    )

    assert result.acceptance_ready is False
    assert result.reason == "no_planning_review_recorded"
    assert result.detail_reason == "wrong_ref_review_run"
    assert result.candidate_review_task_refs == [
        {
            "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
            "review_run_id": 361,
            "verdict": "pass",
            "reviewed_at": "2026-05-20 18:44:00",
            "session": "sess-maint-review",
        }
    ]
    # WORKSTATE-REF-08 implementation note: exactly one eligible (passing) WORKSTATE-REF review candidate
    # exists for this plan subject, so the baseline surfaces the explicit,
    # auditable plan-accept --review-task-ref recovery command rather than a
    # bare re-review instruction.
    recovery_command = (
        'make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS="--json '
        "--plan docs/plans/0099-demo.md "
        "--source-branch feature/WORKSTATE-99 "
        '--review-task-ref WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520"'
    )
    assert result.next_command == recovery_command
    assert result.safe_next_commands == [
        {
            "command": recovery_command,
            "reason": "wrong_ref_review_run_recoverable",
        }
    ]


def test_evaluator_wrong_ref_candidates_without_passing_verdict_keep_plan_review(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan_path = "docs/plans/0099-demo.md"
    _commit_plan(git_repo, "feature/WORKSTATE-99", plan_path)
    stub = tmp_path / "fake-mcp-no-eligible"
    _write_handoff_stub(
        stub,
        verdict=None,
        candidate_review_runs=[
            {
                "id": 361,
                "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
                "verdict": "fail",
                "reviewed_at": "2026-05-20 18:44:00",
                "session": "sess-maint-review",
            }
        ],
    )
    monkeypatch.setenv("MCP_WORKSTATE_HANDOFF_BIN", str(stub))

    result = evaluate_plan_baseline(
        git_repo,
        task_ref="WORKSTATE-REF-99",
        task_plan_path=plan_path,
        target_branch="feature/WORKSTATE-99",
    )

    # A candidate exists but did not pass, so there is no eligible review to
    # adopt: fall back to a re-review instruction, never an auto-accept.
    assert result.acceptance_ready is False
    assert result.reason == "no_planning_review_recorded"
    assert result.detail_reason == "wrong_ref_review_run"
    assert result.next_command == "make plan-review DOC=docs/plans/0099-demo.md"
    assert result.safe_next_commands == [
        {
            "command": "make plan-review DOC=docs/plans/0099-demo.md",
            "reason": "wrong_ref_review_run",
        }
    ]


def test_evaluator_multiple_passing_review_candidates_are_ambiguous(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    plan_path = "docs/plans/0099-demo.md"
    _commit_plan(git_repo, "feature/WORKSTATE-99", plan_path)
    stub = tmp_path / "fake-mcp-ambiguous"
    _write_handoff_stub(
        stub,
        verdict=None,
        candidate_review_runs=[
            {
                "id": 361,
                "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
                "verdict": "pass",
                "reviewed_at": "2026-05-20 18:44:00",
                "session": "sess-maint-review-a",
            },
            {
                "id": 362,
                "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260521",
                "verdict": "pass",
                "reviewed_at": "2026-05-21 09:10:00",
                "session": "sess-maint-review-b",
            },
        ],
    )
    monkeypatch.setenv("MCP_WORKSTATE_HANDOFF_BIN", str(stub))

    result = evaluate_plan_baseline(
        git_repo,
        task_ref="WORKSTATE-REF-99",
        task_plan_path=plan_path,
        target_branch="feature/WORKSTATE-99",
    )

    # Two passing reviews under different refs: the evaluator must not guess
    # which one to adopt. It lists both candidates and offers no auto-accept.
    assert result.acceptance_ready is False
    assert result.reason == "no_planning_review_recorded"
    assert result.detail_reason == "ambiguous_review_candidates"
    assert result.next_command is None
    assert result.safe_next_commands == []
    assert [c["task_ref"] for c in result.candidate_review_task_refs] == [
        "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
        "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260521",
    ]