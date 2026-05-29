"""implementation note contract tests for the read-only ``status`` subcommand.

The handler replaces the current failing stub with a compact git-first
receipt that reports the operator's current workspace, branch/task
alignment, merge-base availability, daemon posture, workflow-file
presence, and the next safe local command.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from ._status_harness import write_fake_cli, write_status_handoff_cli


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "receipts"

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG))

from scripts.workstate.lifecycle.handlers import status as status_handler


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _run_status(
    cwd: Path,
    *,
    handoff_bin: str | None = "/nonexistent/no-such-binary-xyz",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if handoff_bin is not None:
        env["MCP_AGENT_HANDOFF_BIN"] = handoff_bin
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "status", "--json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES_DIR / name).read_text())


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("status:\n\t@true\n")
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "add", "Makefile"], check=True)
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


def test_status_on_main_emits_git_first_receipt(git_repo: Path) -> None:
    proc = _run_status(git_repo)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    expected = _fixture("status_main_no_task.json")
    expected["worktree_path"] = str(git_repo)
    expected["head"] = _git(git_repo, "rev-parse", "HEAD")
    expected["repo_root"] = str(git_repo)
    expected["cwd"] = str(git_repo)
    expected["target_worktree_path"] = str(git_repo)
    assert receipt == expected


def test_status_on_conforming_branch_detects_workflow_file(git_repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    workflow = git_repo / "WORKFLOW.md"
    workflow.write_text("# Workflow\n")

    proc = _run_status(git_repo)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    expected = _fixture("status_feature_branch_with_workflow.json")
    expected["worktree_path"] = str(git_repo)
    expected["head"] = _git(git_repo, "rev-parse", "HEAD")
    expected["repo_root"] = str(git_repo)
    expected["cwd"] = str(git_repo)
    expected["target_worktree_path"] = str(git_repo)
    expected["workflow_file"]["path"] = str(workflow)
    assert receipt == expected


def test_status_outside_git_repo_fails_fast(tmp_path: Path) -> None:
    outside_repo = tmp_path / "outside"
    outside_repo.mkdir()

    proc = _run_status(outside_repo)
    assert proc.returncode == 2, proc.stdout
    receipt = json.loads(proc.stdout)

    assert receipt == {
        "ok": False,
        "command": "status",
        "task_ref": None,
        "branch": "",
        "worktree_path": "",
        "head": "",
        "handoff_projection": "error",
        "error": "not_in_git_repo",
    }


def test_status_missing_makefile_fails_fast(git_repo: Path) -> None:
    (git_repo / "Makefile").unlink()

    proc = _run_status(git_repo)
    assert proc.returncode == 2, proc.stdout
    receipt = json.loads(proc.stdout)

    assert receipt == {
        "ok": False,
        "command": "status",
        "task_ref": None,
        "branch": "main",
        "worktree_path": str(git_repo),
        "head": _git(git_repo, "rev-parse", "HEAD"),
        "handoff_projection": "error",
        "error": "missing_makefile",
    }


def test_status_handoff_unavailable_preserves_git_facts(git_repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )

    proc = _run_status(
        git_repo,
        handoff_bin="/nonexistent/no-such-binary-xyz",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["task_ref"] == "WORKSTATE-REF-77"
    assert receipt["branch"] == "feature/WORKSTATE-77-x"
    assert receipt["handoff_available"] is False
    assert receipt["handoff"] is None
    assert receipt["plan"]["exists"] is False
    assert receipt["review"] == {
        "open_findings_count": None,
        "blockers_count": None,
        "last_test_summary": None,
        "ready_state": "pending_handoff",
    }
    assert any(
        warning["field"] == "handoff" and warning["reason"] == "unavailable"
        for warning in receipt["warnings"]
    )


def test_status_plan_exists_survives_handoff_outage_from_filesystem_stat(git_repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-42-proof"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-42.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-42 Local Plan\n")

    proc = _run_status(
        git_repo,
        handoff_bin="/nonexistent/no-such-binary-xyz",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["task_ref"] == "WORKSTATE-REF-42"
    assert receipt["handoff_projection"] == "pending"
    assert receipt["plan"] == {
        "path": "plans/WORKSTATE-REF-42.md",
        "exists": True,
        "title": "WORKSTATE-REF-42 Local Plan",
        "task_ref_matches_branch": True,
        "stale_reason": None,
        "read_branch": None,
        "read_command": None,
        "read_receipt": None,
    }


def test_status_reaches_only_bounded_handoff_commands(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    log_path = fake_cli_dir / "argv.log"
    write_fake_cli(
        fake_cli,
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import sys\n"
        "from pathlib import Path\n"
        f"log_path = Path({str(log_path)!r})\n"
        "argv = sys.argv[1:]\n"
        "log_path.write_text(log_path.read_text() + json.dumps(argv) + '\\n' if log_path.exists() else json.dumps(argv) + '\\n')\n"
        "if 'state' in argv and 'identity' in argv:\n"
        "    print(json.dumps({'ok': True, 'data': {'active': {'task_ref': 'WORKSTATE-REF-77', 'status': 'in_progress', 'target_branch': 'feature/WORKSTATE-77-x', 'target_worktree_path': sys.argv[2], 'task_plan_path': 'plans/WORKSTATE-REF-77.md'}, 'limits': {}}}))\n"
        "elif 'review-findings' in argv:\n"
        "    print(json.dumps({'ok': True, 'data': {'counts': {'severity': {'high': 0, 'medium': 0, 'low': 0}}}}))\n"
        "elif 'state' in argv and 'blockers_open' in argv:\n"
        "    print(json.dumps({'ok': True, 'data': {'active': {}, 'blockers_open': [], 'limits': {}}}))\n"
        "elif 'get-verified-tests' in argv:\n"
        "    print(json.dumps({'ok': True, 'data': {'tests': [], 'total_matching': 0, 'returned': 0, 'has_more': False}}))\n"
        "elif 'handoff-rows' in argv:\n"
        "    print(json.dumps([]))\n"
        "else:\n"
        "    print(json.dumps({'ok': False, 'argv': argv}), file=sys.stderr)\n"
        "    sys.exit(3)\n",
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr

    argv_lines = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
    # WORKSTATE65-BR-01: ``status`` now consults the live handoff registry
    # via ``handoff-rows`` so the resolver picks the most-specific
    # registered task ref instead of the unsafe shortest-prefix
    # fallback. The new call lands first (gather_git_facts threads the
    # registry into resolver.derive_task_ref) before the other state
    # reads.
    assert argv_lines == [
        ["--workspace-root", str(git_repo), "handoff-rows", "--status", "in_progress", "review", "blocked"],
        ["--workspace-root", str(git_repo), "state", "--sections", "identity", "--detail", "summary"],
        ["--workspace-root", str(git_repo), "review-findings", "--operation", "list", "--status", "open", "--task-ref", "WORKSTATE-REF-77"],
        ["--workspace-root", str(git_repo), "state", "--sections", "blockers_open", "--detail", "summary"],
        ["--workspace-root", str(git_repo), "get-verified-tests", "--passed", "true", "--exclude-never-passed", "--limit", "1", "--task-ref", "WORKSTATE-REF-77"],
    ]


def test_status_handoff_success_populates_identity_projection(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "plans/WORKSTATE-REF-77.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "add plan", "-q",
        ],
        check=True,
    )

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["handoff_projection"] == "synced"
    assert receipt["handoff_available"] is True
    assert receipt["handoff"] == {
        "task_ref": "WORKSTATE-REF-77",
        "status": "in_progress",
        "target_branch": "feature/WORKSTATE-77-x",
        "target_worktree_path": str(git_repo),
        "task_plan_path": "plans/WORKSTATE-REF-77.md",
    }
    assert receipt["plan"] == {
        "path": "plans/WORKSTATE-REF-77.md",
        "exists": True,
        "title": "WORKSTATE-REF-77 Plan",
        "task_ref_matches_branch": True,
        "stale_reason": None,
        "read_branch": "feature/WORKSTATE-77-x",
        "read_command": "make plan-show TASK=WORKSTATE-REF-77",
        "read_receipt": "plan: feature/WORKSTATE-77-x:plans/WORKSTATE-REF-77.md (read: make plan-show TASK=WORKSTATE-REF-77)",
    }
    assert receipt["review"] == {
        "open_findings_count": 0,
        "blockers_count": 0,
        "last_test_summary": None,
        "ready_state": "ready",
    }
    assert receipt["warnings"] == []


def test_status_handoff_success_populates_review_counts_and_ready_state(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=2,
        findings_high=1,
        findings_medium=1,
        findings_low=0,
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["handoff_projection"] == "synced"
    assert receipt["handoff_available"] is True
    assert receipt["review"] == {
        "open_findings_count": 2,
        "blockers_count": 2,
        "last_test_summary": None,
        "ready_state": "blocked",
    }
    assert receipt["warnings"] == []


def test_status_handoff_malformed_response_fails_soft(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_fake_cli(
        fake_cli,
        "#!/usr/bin/env python3\n"
        "print('{not-json}')\n",
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["handoff_projection"] == "pending"
    assert receipt["handoff_available"] is False
    assert receipt["handoff"] is None
    assert receipt["review"]["ready_state"] == "pending_handoff"
    assert receipt["review"]["last_test_summary"] is None
    assert receipt["warnings"] == [
        {
            "field": "handoff",
            "reason": "malformed",
            "exception_type": "JSONDecodeError",
        }
    ]


def test_status_handoff_no_active_task_is_not_malformed(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    """The live handoff CLI returns ``{"active": null, "message": ...}`` when
    no task is active. status must treat that as a clean ``pending`` state,
    not as a malformed response — otherwise the workflow-loop orientation
    path on root main reports ``handoff_available:false`` with reason
    ``malformed`` while ``make tasks`` reaches handoff fine.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    no_active_payload = json.dumps(
        {
            "ok": True,
            "data": {
                "active": None,
                "message": "No active handoff state.",
                "limits": {},
            },
        }
    )
    write_fake_cli(
        fake_cli,
        "#!/usr/bin/env python3\n"
        f"print({no_active_payload!r})\n",
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["handoff_projection"] == "pending"
    assert receipt["handoff_available"] is False
    assert receipt["handoff"] is None
    assert receipt["review"]["ready_state"] == "pending_handoff"
    assert receipt["warnings"] == []


def test_status_handoff_timeout_fails_soft(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_fake_cli(
        fake_cli,
        "#!/usr/bin/env python3\n"
        "import time\n"
        "time.sleep(0.2)\n"
        "print('{}')\n",
    )

    proc = subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "status", "--json", "--handoff-timeout", "0.05"],
        cwd=git_repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "MCP_AGENT_HANDOFF_BIN": str(fake_cli)},
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["handoff_projection"] == "pending"
    assert receipt["handoff_available"] is False
    assert receipt["handoff"] is None
    assert receipt["review"]["ready_state"] == "pending_handoff"
    assert receipt["review"]["last_test_summary"] is None
    assert receipt["warnings"] == [
        {
            "field": "handoff",
            "reason": "timeout",
            "exception_type": "TimeoutExpired",
        }
    ]


def test_daemon_status_parses_yaml_with_inline_comment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract = tmp_path / "harness-protocol.yaml"
    contract.write_text(
        "orchestrator:\n"
        "  daemons:\n"
        "    enabled: true  # inline comment should still parse\n"
    )

    monkeypatch.setattr(status_handler, "HARNESS_PROTOCOL", contract)

    assert status_handler._daemon_status().enabled is True


def test_status_handoff_success_populates_latest_verification_summary(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
        latest_test={
            "id": 7,
            "commit_sha": "1234567890abcdef1234567890abcdef12345678",
            "command": "pytest packages/workstate-system/tests/lifecycle/test_status.py -q",
            "passed": True,
            "verified_at": "2026-05-06 19:08:00",
        },
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["review"]["last_test_summary"] == {
        "command": "pytest packages/workstate-system/tests/lifecycle/test_status.py -q",
        "commit_sha": "1234567890abcdef1234567890abcdef12345678",
        "passed": True,
        "verified_at": "2026-05-06 19:08:00",
    }
    assert receipt["warnings"] == []


def test_status_handoff_partial_review_failure_warns_per_field(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=1,
        findings_low=0,
        latest_test={
            "id": 7,
            "commit_sha": "1234567890abcdef1234567890abcdef12345678",
            "command": "pytest packages/workstate-system/tests/lifecycle/test_status.py -q",
            "passed": True,
            "verified_at": "2026-05-06 19:08:00",
        },
        fail_blockers=True,
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["review"]["open_findings_count"] == 1
    assert receipt["review"]["last_test_summary"] == {
        "command": "pytest packages/workstate-system/tests/lifecycle/test_status.py -q",
        "commit_sha": "1234567890abcdef1234567890abcdef12345678",
        "passed": True,
        "verified_at": "2026-05-06 19:08:00",
    }
    assert receipt["review"]["blockers_count"] is None
    assert receipt["review"]["ready_state"] == "review_required"
    assert {
        "field": "review.blockers_count",
        "reason": "unavailable",
        "exception_type": None,
    } in receipt["warnings"]


def test_review_state_bulkheads_unexpected_field_exceptions(
    git_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        status_handler,
        "_read_open_findings_count",
        lambda repo, *, task_ref, timeout_seconds: (1, None),
    )

    def _explode_blockers(repo: Path, *, timeout_seconds: float) -> tuple[int | None, object]:
        raise RuntimeError("boom")

    monkeypatch.setattr(status_handler, "_read_blockers_count", _explode_blockers)
    monkeypatch.setattr(
        status_handler,
        "_read_last_test_summary",
        lambda repo, *, task_ref, timeout_seconds: (
            status_handler.LastTestSummary(
                command="pytest packages/workstate-system/tests/lifecycle/test_status.py -q",
                commit_sha="1234567890abcdef1234567890abcdef12345678",
                passed=True,
                verified_at="2026-05-06 19:08:00",
            ),
            None,
        ),
    )

    review, warnings = status_handler._review_state(
        git_repo,
        task_ref="WORKSTATE-REF-77",
        timeout_seconds=0.1,
    )

    assert review == status_handler.ReviewState(
        open_findings_count=1,
        blockers_count=None,
        last_test_summary=status_handler.LastTestSummary(
            command="pytest packages/workstate-system/tests/lifecycle/test_status.py -q",
            commit_sha="1234567890abcdef1234567890abcdef12345678",
            passed=True,
            verified_at="2026-05-06 19:08:00",
        ),
        ready_state="review_required",
    )
    assert warnings == [
        status_handler.ReceiptWarning(
            field="review.blockers_count",
            reason="exception",
            exception_type="RuntimeError",
        )
    ]


def test_status_partial_review_degradation_marks_ready_state_degraded(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        findings_high=0,
        findings_medium=0,
        findings_low=0,
        fail_blockers=True,
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["review"]["open_findings_count"] == 0
    assert receipt["review"]["blockers_count"] is None
    assert receipt["review"]["ready_state"] == "degraded"
    assert {
        "field": "review.blockers_count",
        "reason": "unavailable",
        "exception_type": None,
    } in receipt["warnings"]


# WORKSTATE-REF-53 implementation note: status receipt classifies the workspace and points
# at a canonical implementation worktree so an agent on root `main` does
# not have to discover the worktree path through grep / git worktree
# list / DASHBOARD spelunking.

def test_status_on_main_classifies_workspace_role_as_control_plane(
    git_repo: Path,
) -> None:
    proc = _run_status(git_repo)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["workspace_role"] == "control_plane", (
        "WORKSTATE-REF-53 implementation note: root `main` is the control plane. The receipt "
        "must classify it explicitly so generated guidance can route the "
        "operator into a task worktree instead of editing root `main`."
    )


def test_status_on_feature_branch_classifies_workspace_role_as_implementation_plane(
    git_repo: Path,
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    proc = _run_status(git_repo)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["workspace_role"] == "implementation_plane", (
        "WORKSTATE-REF-53 implementation note: a conforming feature branch is the "
        "implementation plane. Required so root-main / worktree status "
        "outputs can recommend distinct next commands."
    )


def test_status_on_main_with_active_tasks_recommends_make_tasks(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    """When more than zero active tasks exist on root `main`, `status`
    should point at `make tasks LIFECYCLE_ARGS=--json` (the workflow loop
    step 2) rather than blanket-suggesting `make task-start` for an
    unknown task ref."""
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "plans/WORKSTATE-REF-77.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "add plan", "-q",
        ],
        check=True,
    )
    subprocess.run(["git", "-C", str(git_repo), "checkout", "-q", "main"], check=True)

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    # Reuse status harness: we only need the identity active row to be
    # present so the receipt sees an active task projection.
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
    )
    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["next_command"] == {
        "command": "make tasks LIFECYCLE_ARGS=--json",
        "reason": "control_plane_with_active_tasks",
    }, (
        "WORKSTATE-REF-53 implementation note: a control-plane checkout with at least one "
        "active task should recommend the workflow loop step 2 "
        "(`make tasks LIFECYCLE_ARGS=--json`). Got: "
        f"{receipt.get('next_command')!r}"
    )
    assert receipt["plan"] == {
        "path": "plans/WORKSTATE-REF-77.md",
        "exists": False,
        "title": None,
        "task_ref_matches_branch": True,
        "stale_reason": "missing_from_worktree",
        "read_branch": "feature/WORKSTATE-77-x",
        "read_command": "make plan-show TASK=WORKSTATE-REF-77",
        "read_receipt": "plan: feature/WORKSTATE-77-x:plans/WORKSTATE-REF-77.md (read: make plan-show TASK=WORKSTATE-REF-77)",
    }


def test_status_plan_read_receipt_omits_unreadable_target_branch(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["plan"] == {
        "path": "plans/WORKSTATE-REF-77.md",
        "exists": False,
        "title": None,
        "task_ref_matches_branch": True,
        "stale_reason": "missing_from_worktree",
        "read_branch": None,
        "read_command": None,
        "read_receipt": None,
    }


def test_status_plan_read_receipt_prefers_main_baseline(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# Accepted WORKSTATE-REF-77 Plan\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "plans/WORKSTATE-REF-77.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "accept plan", "-q",
        ],
        check=True,
    )

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["plan"]["read_branch"] == "main"
    assert receipt["plan"]["read_receipt"] == (
        "plan: main:plans/WORKSTATE-REF-77.md (read: make plan-show TASK=WORKSTATE-REF-77)"
    )


def test_status_includes_canonical_worktree_path_from_handoff_projection(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
    )
    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "canonical_worktree_path" in receipt, (
        "WORKSTATE-REF-53 implementation note: status must expose the canonical worktree "
        "path so root-main agents can `cd` directly into the "
        "implementation plane without grepping `git worktree list`."
    )
    # When the handoff projection names a target_worktree_path, the
    # receipt must surface it as the canonical worktree path.
    assert receipt["canonical_worktree_path"] == str(git_repo)


def test_status_default_handoff_timeout_covers_reviewed_cold_start_budget() -> None:
    assert status_handler.DEFAULT_HANDOFF_TIMEOUT >= 5.0


def test_status_stub_is_removed_from_expected_stubs() -> None:
    stub_test = (Path(__file__).parent / "test_failing_stubs.py").read_text()
    assert '"status": "slice-6"' not in stub_test
    assert '"tasks": "slice-6"' not in stub_test


# WORKSTATE-REF-72 implementation note: plan_baseline visibility on the status receipt.


def test_status_plan_baseline_accepted_when_plan_on_main(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    """A plan committed on ``main`` surfaces as ``baseline=accepted`` in the
    status receipt so coordinators see the gate has cleared without a
    secondary MCP probe."""
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "plans/WORKSTATE-REF-77.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "accept plan", "-q",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
    )

    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    baseline = receipt["plan_baseline"]
    assert baseline is not None, receipt
    assert baseline["status"] == "accepted"
    assert baseline["task_plan_path"] == "plans/WORKSTATE-REF-77.md"
    assert baseline["target_branch"] == "feature/WORKSTATE-77-x"
    assert baseline["acceptance_ready"] is False


def test_status_plan_baseline_missing_when_plan_absent_on_main(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    """Plan committed only on the feature branch surfaces as
    ``baseline=missing`` so the operator sees the gap from the status
    receipt alone."""
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "plans/WORKSTATE-REF-77.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "draft plan on branch", "-q",
        ],
        check=True,
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_status_handoff_cli(
        fake_cli,
        repo_path=str(git_repo),
        task_ref="WORKSTATE-REF-77",
        branch="feature/WORKSTATE-77-x",
        task_plan_path="plans/WORKSTATE-REF-77.md",
        blockers_count=0,
        findings_high=0,
        findings_medium=0,
        findings_low=0,
    )
    proc = _run_status(git_repo, handoff_bin=str(fake_cli))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    baseline = receipt["plan_baseline"]
    assert baseline is not None
    assert baseline["status"] == "missing"


def test_status_plan_baseline_null_when_no_task_in_scope(
    git_repo: Path,
) -> None:
    """On root ``main`` with no active task, the receipt must omit a
    ``baseline=unknown`` surface — callers should not see drift signals
    for genuinely planless workflows."""
    proc = _run_status(git_repo)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["plan_baseline"] is None