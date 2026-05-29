"""implementation note contract tests for plan-baseline enforcement in review-ready
and close-check (WORKSTATE-REF-72).

The review-ready and close-check gates must both refuse a plan-backed
task whose plan baseline is absent from ``main``. The blocking reason
is ``plan_baseline_missing`` and lives in the ``planning_baseline``
owner bucket so workflow clients can route on it without parsing free
text. Orphan planning artifacts continue to surface as warn-only on
the same receipt, distinct from the blocking reason.
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
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
if str(LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG))


def _write_handoff_stub(
    target: Path,
    *,
    task_plan_path: str | None,
    verdict: str | None = "pass",
    open_planning_finding_count: int = 0,
    open_findings_severity: dict[str, int] | None = None,
    latest_passing_test_sha: str | None = None,
    fail_get_handoff_state: bool = False,
    fail_review_runs: bool = False,
) -> None:
    """Write an executable fake mcp-workstate-handoff CLI.

    Responds to the four subcommands review-ready + close-check use:

    * ``get-handoff-state`` — returns identity envelope with task_plan_path
    * ``review-findings`` — used twice: once for branch open-findings
      severity counts, once for planning-mode finding count.
    * ``get-verified-tests`` — returns last passing test commit_sha
    * ``review-runs`` — returns latest planning verdict
    """
    sev = open_findings_severity or {"high": 0, "medium": 0, "low": 0}
    branch_findings_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "counts": {
                    "severity": sev,
                    "status": {"open": sum(sev.values())},
                },
                "findings": [],
            },
        }
    )
    planning_findings_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "findings": [
                    {"finding_id": f"F{i}"}
                    for i in range(open_planning_finding_count)
                ],
                "counts": {
                    "status": {"open": open_planning_finding_count},
                },
            },
        }
    )
    runs_payload = (
        json.dumps({"ok": True, "data": {"runs": []}})
        if verdict is None
        else json.dumps({"ok": True, "data": {"runs": [{"verdict": verdict}]}})
    )
    tests_payload = (
        json.dumps({"ok": True, "data": {"tests": []}})
        if latest_passing_test_sha is None
        else json.dumps(
            {
                "ok": True,
                "data": {"tests": [{"commit_sha": latest_passing_test_sha}]},
            }
        )
    )
    state_payload = (
        json.dumps({"ok": False, "data": {}})
        if fail_get_handoff_state
        else json.dumps(
            {
                "ok": True,
                "data": {
                    "active": {
                        "task_ref": "WORKSTATE-REF-99",
                        "task_plan_path": task_plan_path,
                        "target_branch": "feature/WORKSTATE-99",
                    }
                },
            }
        )
    )

    target.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--workspace-root', required=True)\n"
        "sub = p.add_subparsers(dest='subcommand', required=True)\n"
        "rf = sub.add_parser('review-findings')\n"
        "rf.add_argument('--operation', required=True)\n"
        "rf.add_argument('--status', default='all')\n"
        "rf.add_argument('--task-ref', default=None)\n"
        "rf.add_argument('--review-mode', default=None)\n"
        "rr = sub.add_parser('review-runs')\n"
        "rr.add_argument('--operation', required=True)\n"
        "rr.add_argument('--task-ref', default=None)\n"
        "rr.add_argument('--subject-path', default=None)\n"
        "rr.add_argument('--review-mode', default=None)\n"
        "rr.add_argument('--limit', default=None)\n"
        "vt = sub.add_parser('get-verified-tests')\n"
        "vt.add_argument('--task-ref', default=None)\n"
        "vt.add_argument('--passed', default=None)\n"
        "vt.add_argument('--exclude-never-passed', action='store_true')\n"
        "vt.add_argument('--limit', type=int, default=100)\n"
        "st = sub.add_parser('state')\n"
        "st.add_argument('task_ref', nargs='?', default=None)\n"
        "st.add_argument('--sections', default=None)\n"
        "st.add_argument('--detail', default='full')\n"
        "st.add_argument('--verbose', action='store_true')\n"
        "args = p.parse_args()\n"
        "if args.subcommand == 'review-findings':\n"
        "    if args.review_mode == 'planning':\n"
        f"        sys.stdout.write({planning_findings_payload!r})\n"
        "    else:\n"
        f"        sys.stdout.write({branch_findings_payload!r})\n"
        "elif args.subcommand == 'review-runs':\n"
        f"    sys.stdout.write({runs_payload!r})\n"
        f"    sys.exit({1 if fail_review_runs else 0})\n"
        "elif args.subcommand == 'get-verified-tests':\n"
        f"    sys.stdout.write({tests_payload!r})\n"
        "elif args.subcommand == 'state':\n"
        f"    sys.stdout.write({state_payload!r})\n"
        f"    sys.exit({0 if not fail_get_handoff_state else 1})\n"
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_cli(
    cwd: Path,
    command: str,
    *extra: str,
    fake_cli: Path,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_AGENT_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), command, *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


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
    return repo


def _commit_on_branch(repo: Path, branch: str, plan_path: str) -> str:
    subprocess.run(
        ["git", "-C", str(repo), "switch", "-c", branch, "-q"],
        check=True,
    )
    target = repo / plan_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# plan\n")
    subprocess.run(["git", "-C", str(repo), "add", plan_path], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", f"add {plan_path}", "-q",
        ],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return sha


def _commit_plan_on_main(repo: Path, plan_path: str) -> None:
    target = repo / plan_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# accepted plan\n")
    subprocess.run(["git", "-C", str(repo), "add", plan_path], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", f"docs: accept {plan_path}", "-q",
        ],
        check=True,
    )


def test_review_ready_plan_baseline_missing_blocks(
    git_repo: Path, tmp_path: Path
) -> None:
    """A plan-backed branch whose plan is absent from ``main`` must
    surface ``plan_baseline_missing`` in ``reasons`` and the
    ``planning_baseline`` owner bucket.
    """
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    sha = _commit_on_branch(git_repo, "feature/WORKSTATE-99", plan_path)
    fake_cli = tmp_path / "fake-mcp"
    _write_handoff_stub(
        fake_cli,
        task_plan_path=plan_path,
        latest_passing_test_sha=sha,
    )

    proc = _run_cli(git_repo, "review-ready", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "plan_baseline_missing" in receipt["reasons"], receipt
    grouped = receipt["reasons_by_owner"]
    assert "planning_baseline" in grouped, receipt
    assert "plan_baseline_missing" in grouped["planning_baseline"], receipt


def test_review_ready_plan_baseline_accepted_stays_ready(
    git_repo: Path, tmp_path: Path
) -> None:
    """A plan-backed branch whose plan exists on ``main`` does not
    contribute ``plan_baseline_missing`` to ``reasons``.
    """
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    _commit_plan_on_main(git_repo, plan_path)
    sha = _commit_on_branch(git_repo, "feature/WORKSTATE-99", "src/feat.txt")
    fake_cli = tmp_path / "fake-mcp"
    _write_handoff_stub(
        fake_cli,
        task_plan_path=plan_path,
        latest_passing_test_sha=sha,
    )

    proc = _run_cli(git_repo, "review-ready", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "plan_baseline_missing" not in receipt["reasons"], receipt


def test_review_ready_orphan_warning_and_missing_baseline_coexist(
    git_repo: Path, tmp_path: Path
) -> None:
    """When the canonical plan is missing from ``main`` AND an
    orphan planning draft is untracked on the feature branch, the
    receipt blocks on ``plan_baseline_missing`` while the orphan
    surfaces as a warn-only string.
    """
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    sha = _commit_on_branch(git_repo, "feature/WORKSTATE-99", plan_path)
    orphan = git_repo / "docs/plans/0099-draft.md"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_text("# orphan draft\n")
    fake_cli = tmp_path / "fake-mcp"
    _write_handoff_stub(
        fake_cli,
        task_plan_path=plan_path,
        latest_passing_test_sha=sha,
    )

    proc = _run_cli(git_repo, "review-ready", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "plan_baseline_missing" in receipt["reasons"], receipt
    assert any(
        "orphan planning artifact" in w for w in receipt["warnings"]
    ), receipt


def test_close_check_plan_baseline_missing_blocks(
    git_repo: Path, tmp_path: Path
) -> None:
    """``close-check`` inherits the baseline gate via
    ``augment_with_handoff_state`` — a missing plan baseline blocks
    the merge gate the same way it blocks ``review-ready``.
    """
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    sha = _commit_on_branch(git_repo, "feature/WORKSTATE-99", plan_path)
    fake_cli = tmp_path / "fake-mcp"
    _write_handoff_stub(
        fake_cli,
        task_plan_path=plan_path,
        latest_passing_test_sha=sha,
    )

    proc = _run_cli(git_repo, "close-check", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ready"] is False, receipt
    assert "plan_baseline_missing" in receipt["reasons"], receipt
    grouped = receipt["reasons_by_owner"]
    assert "planning_baseline" in grouped
    assert "plan_baseline_missing" in grouped["planning_baseline"]


def test_review_ready_mcp_get_handoff_state_failure_blocks_fail_closed(
    git_repo: Path, tmp_path: Path
) -> None:
    """Fail-closed contract (planning finding WORKSTATE-REF-72-PR-04): when the
    evaluator cannot read the active row to determine ``task_plan_path``,
    ``review-ready`` must refuse rather than silently skipping the gate.
    The receipt downgrades ``handoff_projection`` to ``pending``.

    WORKSTATE72-S3-BR-01: the emitted reason is ``plan_baseline_unknown`` (not
    ``plan_baseline_missing``) so operators see "retry the gate / MCP is
    degraded" instead of "go accept the plan".
    """
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    sha = _commit_on_branch(git_repo, "feature/WORKSTATE-99", plan_path)
    fake_cli = tmp_path / "fake-mcp"
    _write_handoff_stub(
        fake_cli,
        task_plan_path=plan_path,
        latest_passing_test_sha=sha,
        fail_get_handoff_state=True,
    )

    proc = _run_cli(git_repo, "review-ready", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "plan_baseline_unknown" in receipt["reasons"], receipt
    assert "plan_baseline_missing" not in receipt["reasons"], receipt
    assert receipt["handoff_projection"] == "pending", receipt
    grouped = receipt["reasons_by_owner"]
    assert "plan_baseline_unknown" in grouped["planning_baseline"], receipt


def test_review_ready_planning_verdict_query_failure_emits_unknown(
    git_repo: Path, tmp_path: Path
) -> None:
    """When ``evaluate_plan_baseline`` returns ``baseline_status='unknown'``
    because the planning-verdict MCP query failed (not because the plan is
    absent from ``main``), the receipt must surface ``plan_baseline_unknown``
    instead of conflating it with ``plan_baseline_missing``. WORKSTATE72-S3-BR-01.
    """
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    sha = _commit_on_branch(git_repo, "feature/WORKSTATE-99", plan_path)
    fake_cli = tmp_path / "fake-mcp"
    _write_handoff_stub(
        fake_cli,
        task_plan_path=plan_path,
        latest_passing_test_sha=sha,
        fail_review_runs=True,
    )

    proc = _run_cli(git_repo, "review-ready", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "plan_baseline_unknown" in receipt["reasons"], receipt
    assert "plan_baseline_missing" not in receipt["reasons"], receipt
    grouped = receipt["reasons_by_owner"]
    assert "plan_baseline_unknown" in grouped["planning_baseline"], receipt
    assert receipt["handoff_projection"] == "pending", receipt
    assert receipt["next_command"]["reason"] == "plan_baseline_unknown_retry", receipt


def test_review_ready_planless_task_does_not_block_on_baseline(
    git_repo: Path, tmp_path: Path
) -> None:
    """A task with no ``task_plan_path`` registered (maintenance task
    style) must not contribute ``plan_baseline_missing`` — the gate
    only applies to plan-backed tasks.
    """
    sha = _commit_on_branch(git_repo, "feature/WORKSTATE-99", "src/feat.txt")
    fake_cli = tmp_path / "fake-mcp"
    _write_handoff_stub(
        fake_cli,
        task_plan_path=None,
        latest_passing_test_sha=sha,
    )

    proc = _run_cli(git_repo, "review-ready", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "plan_baseline_missing" not in receipt["reasons"], receipt
