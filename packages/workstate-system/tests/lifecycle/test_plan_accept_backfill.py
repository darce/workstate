"""Contract tests for ``plan-accept-backfill`` (WORKSTATE-REF-69 implementation note).

The handler walks ``handoff-rows --status in_progress review blocked``
and, for each task whose plan path is set, checks (a) whether the plan
already lives on ``main`` (idempotency), (b) the latest ``review_runs``
planning verdict for the plan subject, and (c) the open planning-finding
count. Only tasks whose latest verdict is exactly ``pass`` AND have zero
open planning findings AND whose plan is not yet on ``main`` get a
docs-only accept command emitted; every other row is reported as a
``skip`` with an explicit reason.

The MCP CLI is stubbed via ``MCP_WORKSTATE_HANDOFF_BIN``; the script
recognises four subcommands (``handoff-rows``, ``review-runs``,
``review-findings``, ``get-handoff-state``). Each test seeds a per-task
scenario dict that drives the stub's behaviour, so a single test can
exercise multiple tasks with different verdict/finding combinations in
one invocation.
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


def _write_handoff_stub(target: Path, scenarios: dict[str, dict]) -> None:
    """Write a fake ``mcp-workstate-handoff`` CLI driven by per-task scenarios.

    Each entry of ``scenarios`` is keyed by ``task_ref`` and may carry:

    - ``target_branch``: branch the plan lives on (default ``feature/<lower>``).
    - ``task_plan_path``: plan path string, or ``None``/missing for an
      unset row.
    - ``verdict``: latest planning verdict, ``None`` for "no row".
    - ``open_finding_count``: int, default 0.
    - ``status``: handoff row status, default ``in_progress``.
    """
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, sys\n"
        f"SCENARIOS = json.loads({json.dumps(json.dumps(scenarios))})\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--workspace-root', required=True)\n"
        "sub = p.add_subparsers(dest='subcommand', required=True)\n"
        "hr = sub.add_parser('handoff-rows')\n"
        "hr.add_argument('--status', nargs='+')\n"
        "ghs = sub.add_parser('get-handoff-state')\n"
        "ghs.add_argument('--task-ref')\n"
        "ghs.add_argument('--sections')\n"
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
        "if args.subcommand == 'handoff-rows':\n"
        "    rows = []\n"
        "    for ref, s in SCENARIOS.items():\n"
        "        rows.append({\n"
        "            'task_ref': ref,\n"
        "            'status': s.get('status', 'in_progress'),\n"
        "            'target_branch': s.get('target_branch', 'feature/' + ref.lower()),\n"
        "            'target_worktree_path': s.get('target_worktree_path'),\n"
        "            'task_plan_path': s.get('task_plan_path'),\n"
        "            'updated_at': '2026-01-01 00:00:00',\n"
        "            'revision': 1,\n"
        "        })\n"
        "    sys.stdout.write(json.dumps(rows))\n"
        "elif args.subcommand == 'get-handoff-state':\n"
        "    ref = args.task_ref\n"
        "    s = SCENARIOS.get(ref, {})\n"
        "    sys.stdout.write(json.dumps({\n"
        "        'ok': True,\n"
        "        'data': {\n"
        "            'active': {\n"
        "                'task_ref': ref,\n"
        "                'target_branch': s.get('target_branch', 'feature/' + ref.lower()),\n"
        "                'task_plan_path': s.get('task_plan_path'),\n"
        "            }\n"
        "        }\n"
        "    }))\n"
        "elif args.subcommand == 'review-runs':\n"
        "    ref = args.task_ref\n"
        "    s = SCENARIOS.get(ref, {})\n"
        "    v = s.get('verdict')\n"
        "    runs = [] if v is None else [{'verdict': v}]\n"
        "    sys.stdout.write(json.dumps({'ok': True, 'data': {'runs': runs}}))\n"
        "elif args.subcommand == 'review-findings':\n"
        "    ref = args.task_ref\n"
        "    s = SCENARIOS.get(ref, {})\n"
        "    n = int(s.get('open_finding_count', 0) or 0)\n"
        "    sys.stdout.write(json.dumps({\n"
        "        'ok': True,\n"
        "        'data': {\n"
        "            'findings': [{'finding_id': 'F'+str(i)} for i in range(n)],\n"
        "            'counts': {'status': {'open': n}},\n"
        "        }\n"
        "    }))\n"
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def git_repo_with_branches(tmp_path: Path) -> Path:
    """Init a repo on ``main`` and create feature branches with plan files.

    Tests that need a plan to already live on ``main`` for a given task
    commit the plan path on ``main`` directly; otherwise plans live only
    on the named feature branch.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        check=True,
    )
    return repo


def _commit_plan(repo: Path, branch: str, plan_path: str, body: str = "plan body") -> None:
    """Commit ``plan_path`` on ``branch``, returning to ``main`` afterwards."""
    if branch != "main":
        subprocess.run(
            ["git", "-C", str(repo), "switch", "-c", branch, "-q"],
            check=True,
        )
    target = repo / plan_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body)
    subprocess.run(["git", "-C", str(repo), "add", plan_path], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", f"add {plan_path}", "-q"],
        check=True,
    )
    if branch != "main":
        subprocess.run(
            ["git", "-C", str(repo), "switch", "main", "-q"],
            check=True,
        )


def _run_backfill(repo: Path, *extra: str, fake_cli: Path) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "plan-accept-backfill", *extra],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _install_failing_pre_commit(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def test_backfill_emits_accept_for_clean_pass_with_no_baseline(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    _commit_plan(repo, "feature/WORKSTATE-99", "docs/plans/0099-demo.md")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 1, receipt
    assert receipt["skipped_count"] == 0, receipt
    tasks = receipt["tasks"]
    assert len(tasks) == 1
    row = tasks[0]
    assert row["task_ref"] == "WORKSTATE-REF-99"
    assert row["action"] == "accept"
    assert "docs/plans/0099-demo.md" in row["next_command"]
    assert "docs(WORKSTATE-99): accept plan" in row["next_command"]
    assert row["plan_exists_on_target_branch"] is True


def test_backfill_skips_when_plan_missing_on_target_branch(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 0, receipt
    assert receipt["skipped_count"] == 1, receipt
    row = receipt["tasks"][0]
    assert row["action"] == "skip"
    assert row["reason"] == "plan_missing_on_target_branch"
    assert row["plan_exists_on_target_branch"] is False


def test_backfill_local_failure_restores_clean_main_and_exits_nonzero(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    _commit_plan(repo, "feature/WORKSTATE-99", "docs/plans/0099-demo.md")
    _install_failing_pre_commit(repo)
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", "--local", fake_cli=stub)

    assert proc.returncode == 2, proc.stdout
    receipt = json.loads(proc.stdout)
    row = receipt["tasks"][0]
    assert row["action"] == "skip"
    assert row["reason"].startswith("commit_failed:"), receipt
    assert receipt["local_errors"] == [f"WORKSTATE-REF-99:{row['reason']}"]
    status = subprocess.check_output(
        ["git", "-C", str(repo), "status", "--porcelain"],
        text=True,
    ).strip()
    assert status == ""
    assert not (repo / "docs/plans/0099-demo.md").exists()


def test_backfill_skips_when_plan_already_on_main(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    # Land the plan on main directly so the baseline is already present.
    _commit_plan(repo, "main", "docs/plans/0099-demo.md")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 0
    assert receipt["skipped_count"] == 1
    row = receipt["tasks"][0]
    assert row["action"] == "skip"
    assert row["reason"] == "already_accepted"
    assert row.get("next_command") is None


@pytest.mark.parametrize(
    "verdict, expected_reason",
    [
        ("pass_with_findings", "latest_planning_verdict_pass_with_findings"),
        ("conditional_pass", "latest_planning_verdict_conditional_pass"),
        ("fail", "latest_planning_verdict_fail"),
    ],
)
def test_backfill_skips_non_pass_verdicts(
    git_repo_with_branches: Path,
    tmp_path: Path,
    verdict: str,
    expected_reason: str,
) -> None:
    repo = git_repo_with_branches
    _commit_plan(repo, "feature/WORKSTATE-99", "docs/plans/0099-demo.md")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": verdict,
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 0
    assert receipt["skipped_count"] == 1
    row = receipt["tasks"][0]
    assert row["action"] == "skip"
    assert row["reason"] == expected_reason


def test_backfill_skips_when_no_planning_review_recorded(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    _commit_plan(repo, "feature/WORKSTATE-99", "docs/plans/0099-demo.md")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": None,
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 0
    row = receipt["tasks"][0]
    assert row["action"] == "skip"
    assert row["reason"] == "no_planning_review_recorded"


def test_backfill_skips_when_open_planning_findings(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    _commit_plan(repo, "feature/WORKSTATE-99", "docs/plans/0099-demo.md")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": "pass",
            "open_finding_count": 3,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 0
    row = receipt["tasks"][0]
    assert row["action"] == "skip"
    assert row["reason"] == "open_planning_findings"
    assert row["open_planning_findings"] == 3


def test_backfill_skips_rows_without_task_plan_path(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": None,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 0
    row = receipt["tasks"][0]
    assert row["action"] == "skip"
    assert row["reason"] == "task_plan_path_unset"


def test_backfill_mixed_population_only_clean_pass_emits(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    # Three feature branches each with their own plan; one already on main.
    _commit_plan(repo, "feature/WORKSTATE-67", "docs/plans/0067-clean.md")
    _commit_plan(repo, "feature/WORKSTATE-68", "docs/plans/0068-blocked.md")
    _commit_plan(repo, "feature/WORKSTATE-66", "docs/plans/0066-accepted.md")
    _commit_plan(repo, "main", "docs/plans/0066-accepted.md")

    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-67": {
            "target_branch": "feature/WORKSTATE-67",
            "task_plan_path": "docs/plans/0067-clean.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
        "WORKSTATE-REF-68": {
            "target_branch": "feature/WORKSTATE-68",
            "task_plan_path": "docs/plans/0068-blocked.md",
            "verdict": "pass",
            "open_finding_count": 2,
        },
        "WORKSTATE-REF-66": {
            "target_branch": "feature/WORKSTATE-66",
            "task_plan_path": "docs/plans/0066-accepted.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 1, receipt
    assert receipt["skipped_count"] == 2, receipt
    by_ref = {row["task_ref"]: row for row in receipt["tasks"]}
    assert by_ref["WORKSTATE-REF-67"]["action"] == "accept"
    assert by_ref["WORKSTATE-REF-68"]["action"] == "skip"
    assert by_ref["WORKSTATE-REF-68"]["reason"] == "open_planning_findings"
    assert by_ref["WORKSTATE-REF-66"]["action"] == "skip"
    assert by_ref["WORKSTATE-REF-66"]["reason"] == "already_accepted"


def test_backfill_task_filter_limits_evaluation_to_requested_task(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    _commit_plan(repo, "feature/WORKSTATE-67", "docs/plans/0067-clean.md")
    _commit_plan(repo, "feature/WORKSTATE-68", "docs/plans/0068-clean.md")

    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-67": {
            "target_branch": "feature/WORKSTATE-67",
            "task_plan_path": "docs/plans/0067-clean.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
        "WORKSTATE-REF-68": {
            "target_branch": "feature/WORKSTATE-68",
            "task_plan_path": "docs/plans/0068-clean.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
    })

    proc = _run_backfill(repo, "--json", "--task", "WORKSTATE-REF-68", fake_cli=stub)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["accepted_count"] == 1, receipt
    assert receipt["skipped_count"] == 0, receipt
    assert [row["task_ref"] for row in receipt["tasks"]] == ["WORKSTATE-REF-68"]


def test_backfill_is_idempotent_when_rerun(
    git_repo_with_branches: Path, tmp_path: Path
) -> None:
    repo = git_repo_with_branches
    # Plan is already on main from a prior backfill run.
    _commit_plan(repo, "main", "docs/plans/0099-demo.md")
    stub = tmp_path / "fake-mcp"
    _write_handoff_stub(stub, {
        "WORKSTATE-REF-99": {
            "target_branch": "feature/WORKSTATE-99",
            "task_plan_path": "docs/plans/0099-demo.md",
            "verdict": "pass",
            "open_finding_count": 0,
        },
    })

    first = _run_backfill(repo, "--json", fake_cli=stub)
    second = _run_backfill(repo, "--json", fake_cli=stub)

    assert first.returncode == 0
    assert second.returncode == 0
    first_receipt = json.loads(first.stdout)
    second_receipt = json.loads(second.stdout)
    assert first_receipt["accepted_count"] == 0
    assert second_receipt["accepted_count"] == 0
    assert first_receipt["tasks"][0]["reason"] == "already_accepted"
    assert second_receipt["tasks"][0]["reason"] == "already_accepted"
