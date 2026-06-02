"""implementation note contract tests for the mutating ``task-start`` subcommand.

The handler creates a conforming feature branch from a supplied task
ref (or the current branch) and emits a stable JSON receipt. ``MODE=here``
checks the branch out in the current repo; ``MODE=worktree`` (default)
creates a linked worktree; if the branch already lives in a linked
worktree the existing path is reused. Adapter failure spools a
``task_started`` event but never blocks the underlying git operation.

Receipt extras (per ``§JSON Receipt Schema`` in implementation note):

* ``mode``: ``"worktree" | "here"``
* ``created_branch``: ``bool`` — true when ``git checkout -b`` ran
* ``reused_worktree``: ``bool`` — true when an existing linked worktree
  was reused
* ``plan_path``: ``str | None`` — the active task plan path resolved
  from the canonical handoff state, ``None`` when none is recorded.
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
PENDING_REL = Path(".task-state") / "pending-workflow-events.jsonl"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_cli_body_with_workspace_summary(
    summary: dict | None,
    *,
    planning_verdict: str | None = "pass",
    open_planning_finding_count: int = 0,
    candidate_review_runs: list[dict[str, object]] | None = None,
) -> str:
    """Bash fake-CLI body that answers ``render-handoff`` with ``summary``.

    WORKSTATE-REF-54-FU implementation note: the lifecycle readers consume
    ``render-handoff --kind=current_task --no-write`` instead of reading
    ``CURRENT_TASK.json`` directly. Tests that previously seeded a stale
    disk file must now also surface the same content through the
    rendered envelope; this helper bakes that envelope into the
    drop-in fake CLI.
    """
    if summary is None:
        envelope: dict = {
            "schema_version": 2,
            "tool": "render_handoff",
            "ok": True,
            "data": {
                "task_ref": None,
                "path": "/tmp/CURRENT_TASK.json",
                "written": False,
                "current_task_json": json.dumps(
                    {
                        "schema_version": 2,
                        "shape": "none",
                        "task_ref": None,
                        "active": None,
                        "tasks": [],
                    }
                ),
            },
        }
    else:
        envelope = {
            "schema_version": 2,
            "tool": "render_handoff",
            "ok": True,
            "data": {
                "task_ref": summary.get("task_ref"),
                "path": "/tmp/CURRENT_TASK.json",
                "written": False,
                "current_task_json": json.dumps(summary),
            },
        }
    runs_payload = (
        {"ok": True, "data": {"runs": []}}
        if planning_verdict is None
        else {"ok": True, "data": {"runs": [{"verdict": planning_verdict}]}}
    )
    findings_payload = {
        "ok": True,
        "data": {
            "findings": [
                {"finding_id": f"planning-{index}"}
                for index in range(open_planning_finding_count)
            ],
            "counts": {"status": {"open": open_planning_finding_count}},
        },
    }
    candidate_runs_payload = {
        "ok": True,
        "data": {"runs": candidate_review_runs or []},
    }
    body = (
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"render-handoff"* ]]; then\n'
        f"  cat <<'ENVELOPE_EOF'\n{json.dumps(envelope)}\nENVELOPE_EOF\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$*" == *"review-runs"* ]]; then\n'
        '  if [[ "$*" == *"--task-ref"* ]]; then\n'
        f"    cat <<'RUNS_EOF'\n{json.dumps(runs_payload)}\nRUNS_EOF\n"
        "  else\n"
        f"    cat <<'CANDIDATE_RUNS_EOF'\n{json.dumps(candidate_runs_payload)}\nCANDIDATE_RUNS_EOF\n"
        "  fi\n"
        "  exit 0\n"
        "fi\n"
        'if [[ "$*" == *"review-findings"* ]]; then\n'
        f"  cat <<'FINDINGS_EOF'\n{json.dumps(findings_payload)}\nFINDINGS_EOF\n"
        "  exit 0\n"
        "fi\n"
        "exit 0\n"
    )
    return body


def _commit_plan_on_current_branch(repo: Path, plan_path: str) -> None:
    target = repo / plan_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# demo task plan\n")
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


def _v2_single_summary(task_ref: str, **active_overrides: object) -> dict:
    """Convenience builder for a v2 ``shape='single'`` workspace summary."""
    active = {"task_ref": task_ref}
    active.update(active_overrides)
    return {
        "schema_version": 2,
        "shape": "single",
        "task_ref": task_ref,
        "active": active,
        "tasks": [],
    }


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
    fake_cli: Path | None,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if fake_cli is not None:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    else:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = "/nonexistent/no-such-binary-xyz"
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "task-start", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


REQUIRED_FIELDS = (
    "ok",
    "command",
    "task_ref",
    "branch",
    "worktree_path",
    "head",
    "handoff_projection",
    "events",
)

TASK_START_EXTRA_FIELDS = (
    "mode",
    "created_branch",
    "reused_worktree",
    "plan_path",
    # WORKSTATE-REF-66 implementation note: additive conflict-detection fields. Populated by
    # ``_detect_real_conflict`` on refusal paths through the
    # ``workspace_ambiguous`` guard branch; ``None`` everywhere else
    # (success, single-shape worktree-singleton refusal). Locked in the
    # required-fields tuple so the receipt shape can never silently drop
    # them on either success or error paths.
    "conflict_kind",
    "conflict_category",
)


def test_task_start_here_creates_conforming_branch_in_repo(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--objective",
        "smallest end-to-end task-start path",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    for field in (*REQUIRED_FIELDS, *TASK_START_EXTRA_FIELDS):
        assert field in receipt, f"missing required field {field!r}: {receipt!r}"
    assert receipt["ok"] is True
    assert receipt["command"] == "task-start"
    assert receipt["task_ref"] == "WORKSTATE-REF-99"
    assert receipt["branch"] == "feature/WORKSTATE-99"
    assert receipt["worktree_path"] == str(git_repo)
    assert len(receipt["head"]) == 40
    assert receipt["mode"] == "here"
    assert receipt["created_branch"] is True
    assert receipt["reused_worktree"] is False
    assert "task_started" in receipt["events"]
    # Branch was actually checked out in the repo.
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-99"


def test_task_start_here_with_slug_appends_lower_slug(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-101",
        "--slug",
        "Worktree-Creation",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["branch"] == "feature/WORKSTATE-101-worktree-creation"
    assert (
        _git(git_repo, "branch", "--show-current")
        == "feature/WORKSTATE-101-worktree-creation"
    )


def test_task_start_here_blank_task_ref_errors(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode != 0
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    # An error short-circuits before the adapter is touched.
    assert receipt["handoff_projection"] == "error"


@pytest.mark.parametrize(
    ("planning_verdict", "open_planning_finding_count", "expected_error"),
    (
        ("conditional_pass", 0, "latest_planning_verdict_conditional_pass"),
        ("pass", 2, "open_planning_findings"),
    ),
)
def test_task_start_plan_baseline_not_ready_does_not_suggest_plan_accept(
    git_repo: Path,
    fake_cli_dir: Path,
    planning_verdict: str,
    open_planning_finding_count: int,
    expected_error: str,
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(
            None,
            planning_verdict=planning_verdict,
            open_planning_finding_count=open_planning_finding_count,
        ),
    )
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    subprocess.run(
        ["git", "-C", str(git_repo), "switch", "-c", "feature/WORKSTATE-99", "-q"],
        check=True,
    )
    _commit_plan_on_current_branch(git_repo, plan_path)

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--plan",
        plan_path,
        "--mode",
        "here",
        "--json",
    )

    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["error"] == expected_error
    assert receipt["events"] == ["plan_baseline_checked"]
    assert receipt["plan_baseline"]["acceptance_ready"] is False
    assert receipt["plan_baseline"]["reason"] == expected_error
    assert receipt["plan_baseline"]["next_command"] is None
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-99"


def test_task_start_plan_baseline_ready_points_to_plan_accept(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _fake_cli_body_with_workspace_summary(None))
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    subprocess.run(
        ["git", "-C", str(git_repo), "switch", "-c", "feature/WORKSTATE-99", "-q"],
        check=True,
    )
    _commit_plan_on_current_branch(git_repo, plan_path)

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--plan",
        plan_path,
        "--mode",
        "here",
        "--json",
    )

    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["error"] == "plan_baseline_missing"
    assert receipt["events"] == ["plan_baseline_checked"]
    assert receipt["plan_baseline"]["acceptance_ready"] is True
    assert receipt["plan_baseline"]["next_command"] == (
        "make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS=--json"
    )


def test_task_start_plan_baseline_surfaces_wrong_ref_review_candidates(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(
            None,
            planning_verdict=None,
            candidate_review_runs=[
                {
                    "id": 361,
                    "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
                    "verdict": "pass",
                    "reviewed_at": "2026-05-20 18:44:00",
                    "session": "sess-maint-review",
                }
            ],
        ),
    )
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    subprocess.run(
        ["git", "-C", str(git_repo), "switch", "-c", "feature/WORKSTATE-99", "-q"],
        check=True,
    )
    _commit_plan_on_current_branch(git_repo, plan_path)

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--plan",
        plan_path,
        "--mode",
        "here",
        "--json",
    )

    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["error"] == "no_planning_review_recorded"
    assert receipt["events"] == ["plan_baseline_checked"]
    assert receipt["plan_baseline"]["acceptance_ready"] is False
    assert receipt["plan_baseline"]["detail_reason"] == "wrong_ref_review_run"
    assert receipt["plan_baseline"]["candidate_review_task_refs"] == [
        {
            "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
            "review_run_id": 361,
            "verdict": "pass",
            "reviewed_at": "2026-05-20 18:44:00",
            "session": "sess-maint-review",
        }
    ]
    # WORKSTATE-REF-08 implementation note: task-start forwards the baseline's single-eligible
    # recovery command verbatim (it does not rebuild it). The command names
    # the WORKSTATE-REF review explicitly so the cross-ref acceptance is auditable.
    recovery_command = (
        f'make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS="--json --plan {plan_path} '
        "--source-branch feature/WORKSTATE-99 "
        '--review-task-ref WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520"'
    )
    assert receipt["plan_baseline"]["next_command"] == recovery_command
    assert receipt["plan_baseline"]["safe_next_commands"] == [
        {
            "command": recovery_command,
            "reason": "wrong_ref_review_run_recoverable",
        }
    ]


def test_task_start_plan_baseline_multiple_review_candidates_are_ambiguous(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(
            None,
            planning_verdict=None,
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
        ),
    )
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    subprocess.run(
        ["git", "-C", str(git_repo), "switch", "-c", "feature/WORKSTATE-99", "-q"],
        check=True,
    )
    _commit_plan_on_current_branch(git_repo, plan_path)

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--plan",
        plan_path,
        "--mode",
        "here",
        "--json",
    )

    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["error"] == "no_planning_review_recorded"
    assert receipt["plan_baseline"]["acceptance_ready"] is False
    assert receipt["plan_baseline"]["detail_reason"] == "ambiguous_review_candidates"
    assert receipt["plan_baseline"]["next_command"] is None
    assert receipt["plan_baseline"]["safe_next_commands"] == []
    assert [
        c["task_ref"] for c in receipt["plan_baseline"]["candidate_review_task_refs"]
    ] == [
        "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
        "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260521",
    ]
    # No handoff mutation: task-start stays on the source branch.
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-99"


def test_task_start_plan_baseline_non_passing_candidate_keeps_plan_review(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(
            None,
            planning_verdict=None,
            candidate_review_runs=[
                {
                    "id": 361,
                    "task_ref": "WORKSTATE-REF-PLAN-REVIEW-TASK99-20260520",
                    "verdict": "fail",
                    "reviewed_at": "2026-05-20 18:44:00",
                    "session": "sess-maint-review",
                }
            ],
        ),
    )
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    subprocess.run(
        ["git", "-C", str(git_repo), "switch", "-c", "feature/WORKSTATE-99", "-q"],
        check=True,
    )
    _commit_plan_on_current_branch(git_repo, plan_path)

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--plan",
        plan_path,
        "--mode",
        "here",
        "--json",
    )

    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["error"] == "no_planning_review_recorded"
    assert receipt["plan_baseline"]["detail_reason"] == "wrong_ref_review_run"
    assert (
        receipt["plan_baseline"]["next_command"] == f"make plan-review DOC={plan_path}"
    )
    assert receipt["plan_baseline"]["safe_next_commands"] == [
        {
            "command": f"make plan-review DOC={plan_path}",
            "reason": "wrong_ref_review_run",
        }
    ]


def test_task_start_accepted_baseline_flows_through_creation(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """WORKSTATE-REF-08 item 179: an already-accepted baseline must not block task-start.

    When the plan is already committed on ``main`` the baseline evaluates to
    ``baseline_status == "accepted"``. The gate must fall through to the
    normal creation/reuse path rather than treating the absent feature-branch
    baseline as a missing/not-ready error. This regression-locks that
    pass-through so the recovery work of Slices 1-2 cannot silently start
    failing the happy path.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _fake_cli_body_with_workspace_summary(None))
    plan_path = "packages/example/docs/tasks/WORKSTATE-REF-99-demo-task-plan.md"
    # Commit the plan on ``main`` (the git_repo fixture starts on main), so the
    # baseline is accepted before task-start runs.
    _commit_plan_on_current_branch(git_repo, plan_path)

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--plan",
        plan_path,
        "--mode",
        "here",
        "--json",
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt.get("error") is None
    assert receipt["command"] == "task-start"
    assert receipt["task_ref"] == "WORKSTATE-REF-99"
    assert receipt["branch"] == "feature/WORKSTATE-99"
    assert receipt["created_branch"] is True
    assert receipt["reused_worktree"] is False
    assert "task_started" in receipt["events"]
    # The accepted baseline did not short-circuit into a plan_baseline error.
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-99"


def test_task_start_worktree_creates_sibling_linked_worktree(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-55",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["mode"] == "worktree"
    assert receipt["created_branch"] is True
    assert receipt["reused_worktree"] is False
    assert receipt["task_ref"] == "WORKSTATE-REF-55"
    assert receipt["branch"] == "feature/WORKSTATE-55"
    expected = git_repo.parent / f"{git_repo.name}-WORKSTATE-55"
    assert receipt["worktree_path"] == str(expected)
    # Primary repo HEAD did NOT move; the new branch lives in the linked
    # worktree only.
    assert _git(git_repo, "branch", "--show-current") == "main"
    # Linked worktree exists, owns the new branch, and shares the primary
    # repo's commit lineage.
    assert expected.is_dir()
    assert _git(expected, "branch", "--show-current") == "feature/WORKSTATE-55"


def test_task_start_default_mode_is_worktree(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """Omitting --mode must default to worktree per implementation note implementation note."""
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-66",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["mode"] == "worktree"
    expected = git_repo.parent / f"{git_repo.name}-WORKSTATE-66"
    assert receipt["worktree_path"] == str(expected)
    assert expected.is_dir()


def test_task_start_plan_baseline_missing_refuses_before_git_mutation(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    plan_path = "docs/plans/0099-demo-task-plan.md"
    plan = git_repo / plan_path
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# demo plan\n")
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, _fake_cli_body_with_workspace_summary(None))

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--plan",
        plan_path,
        "--mode",
        "worktree",
        "--json",
    )

    assert proc.returncode != 0, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "plan_baseline_missing"
    assert receipt["plan_path"] == plan_path
    assert receipt["plan_baseline"]["baseline_status"] == "missing"
    assert receipt["plan_baseline"]["baseline_exists_on_main"] is False
    assert receipt["plan_baseline"]["plan_untracked_on_main"] is True
    assert receipt["plan_baseline"]["detail_reason"] == "untracked_draft_on_main"
    assert receipt["plan_baseline"]["plan_path_source"] == "cli_plan_arg"
    assert receipt["plan_baseline"]["identity_state"] == "task_row_missing"
    assert receipt["plan_baseline"]["source_branch_state"] == "plan_untracked_on_main"
    assert receipt["plan_baseline"]["next_command"] == (
        'make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS="--json --local --plan docs/plans/0099-demo-task-plan.md --source-branch main"'
    )
    assert receipt["plan_baseline"]["safe_next_commands"][0] == {
        "command": 'make plan-accept TASK=WORKSTATE-REF-99 LIFECYCLE_ARGS="--json --local --plan docs/plans/0099-demo-task-plan.md --source-branch main"',
        "reason": "untracked_draft_on_main",
    }
    assert _git(git_repo, "branch", "--show-current") == "main"
    assert _git(git_repo, "branch", "--list", "feature/WORKSTATE-99") == ""
    assert not (git_repo.parent / f"{git_repo.name}-WORKSTATE-99").exists()


def test_task_start_worktree_reuses_existing_linked_worktree(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """Branch already checked out in another linked worktree must be
    reused, not re-created. Spec: ``If the target branch is already
    checked out in a linked worktree, reuse that path instead of
    creating a new one.``
    """
    # Pre-create a linked worktree for the target branch in a non-canonical
    # location. task-start should pick up the existing path, not collide
    # by trying to create a fresh one.
    existing = tmp_path / "existing-wt"
    subprocess.run(
        [
            "git",
            "-C",
            str(git_repo),
            "worktree",
            "add",
            "-q",
            "-b",
            "feature/WORKSTATE-44",
            str(existing),
        ],
        check=True,
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-44",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["mode"] == "worktree"
    assert receipt["created_branch"] is False
    assert receipt["reused_worktree"] is True
    assert receipt["worktree_path"] == str(existing)
    # No second worktree should have been created at the canonical sibling
    # path because the branch was already linked elsewhere.
    canonical_sibling = git_repo.parent / f"{git_repo.name}-WORKSTATE-44"
    assert not canonical_sibling.exists()


def test_task_start_ambiguity_hard_stops_before_mutating_git(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """BR-WORKSTATE40-S3-01: when ``CURRENT_TASK.json`` names a different
    active task than the request, task-start MUST NOT touch git. It
    exits non-zero, projects ``ambiguity_resolved``, and leaves the
    branch list untouched.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(
            _v2_single_summary(
                "WORKSTATE-REF-99",
                status="in_progress",
                target_branch="feature/WORKSTATE-99",
            )
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
    assert "ambiguity_resolved" in receipt["events"]
    assert receipt.get("error") == "task_ref_ambiguous"
    # Git state is untouched: still on main, no feature branch created.
    assert _git(git_repo, "branch", "--show-current") == "main"
    branches = _git(git_repo, "branch", "--list", "feature/WORKSTATE-50")
    assert branches == ""


def test_task_start_ignores_completed_current_task_snapshot(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """A stale done snapshot must not block starting fresh work.

    ``make tasks`` hides done rows via LIVE_ACTIVE_STATUSES. task-start's
    file-based ambiguity guard must honor the same boundary or a shipped task
    left in CURRENT_TASK.json can permanently block the next task.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    (git_repo / "CURRENT_TASK.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active": {
                    "task_ref": "WORKSTATE-REF-41",
                    "status": "done",
                    "task_plan_path": "docs/tasks/WORKSTATE-REF-41-task-plan.md",
                },
            }
        )
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

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert "ambiguity_resolved" not in receipt["events"]
    assert receipt["plan_path"] is None
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-50"


def test_task_start_treats_status_missing_snapshot_as_live(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """Older snapshots without a ``status`` field must still hard-stop.

    The conservative fallback in ``_snapshot_is_live`` keeps the ambiguity
    guard honest for legacy CURRENT_TASK.json shapes that predate the
    LIVE_ACTIVE_STATUSES contract.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    # Note: the v2 summary builder emits ``shape='single'`` with the live
    # ``active`` block. The conservative-fallback contract here is that a
    # status-less ``active`` block reads as live by ``_snapshot_is_live``
    # (see ``_common.snapshot_is_live``); the WORKSTATE-REF-54-FU migration
    # preserves that semantic by surfacing the same active block through
    # the rendered envelope.
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(
            _v2_single_summary(
                "WORKSTATE-REF-41",
                target_branch="feature/WORKSTATE-41",
                task_plan_path="docs/tasks/WORKSTATE-REF-41-task-plan.md",
            )
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
    assert "ambiguity_resolved" in receipt["events"]
    assert _git(git_repo, "branch", "--show-current") == "main"


def test_task_start_matching_active_task_proceeds(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """When the request matches the active snapshot, task-start runs
    normally — the ambiguity guard must not false-trigger.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    (git_repo / "CURRENT_TASK.json").write_text(
        json.dumps({"schema_version": 1, "active": {"task_ref": "WORKSTATE-REF-50"}})
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
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert "ambiguity_resolved" not in receipt["events"]
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-50"


def test_task_start_here_invokes_handoff_state_sync(
    git_repo: Path, fake_cli_dir: Path, tmp_path: Path
) -> None:
    """BR-WORKSTATE40-S3-02: task-start MUST sync handoff state with the
    git-derived task_ref, target_branch, and target_worktree_path —
    not just project a generic decision event.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = tmp_path / "argv.log"
    fake_cli.write_text(
        f'#!/usr/bin/env bash\nprintf "%s\\n" "$@" >> {argv_log}\necho "---" >> {argv_log}\nexit 0\n'
    )
    fake_cli.chmod(fake_cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-31",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["handoff_projection"] == "synced"
    # Split the multi-invocation log into per-call argv blocks.
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in argv_log.read_text().splitlines():
        if line == "---":
            blocks.append(current)
            current = []
        else:
            current.append(line)
    state_sync_blocks = [b for b in blocks if "set" in b]
    assert state_sync_blocks, f"no state-sync (`set`) call found in {blocks!r}"
    args = state_sync_blocks[-1]
    assert "--task-ref" in args and args[args.index("--task-ref") + 1] == "WORKSTATE-REF-31"
    assert "--target-branch" in args
    assert args[args.index("--target-branch") + 1] == "feature/WORKSTATE-31"
    assert "--target-worktree-path" in args
    assert args[args.index("--target-worktree-path") + 1] == str(git_repo)
    assert "--branch" in args
    assert args[args.index("--branch") + 1] == "feature/WORKSTATE-31"
    assert "--commit-sha" in args
    assert args[args.index("--commit-sha") + 1] == receipt["head"]


def test_task_start_here_adapter_rejection_spools_spooled(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """WORKSTATE-REF-52 implementation note: ``exit 1`` is CLI ran + rejected → ``spooled``;
    ``pending`` is reserved for CLI-unreachable cases (returncode 124/127).
    The git operation must still succeed regardless.
    """
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-77",
        "--mode",
        "here",
        "--json",
    )
    # Adapter failure must NOT block the git operation.
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["handoff_projection"] == "spooled"
    spool = git_repo / PENDING_REL
    assert spool.exists()
    payload = json.loads(spool.read_text().splitlines()[-1])
    assert payload["kind"] == "state_sync"
    assert payload["task_ref"] == "WORKSTATE-REF-77"
    assert payload["target_branch"] == "feature/WORKSTATE-77"
    assert payload["target_worktree_path"] == str(git_repo)
    # Branch was still created locally even though MCP write was deferred.
    assert _git(git_repo, "branch", "--show-current") == "feature/WORKSTATE-77"


# ---------------------------------------------------------------------------
# implementation note S3: task-start adopts the bootstrap overlay into the new worktree
# ---------------------------------------------------------------------------


def test_task_start_worktree_adopts_overlay_when_marker_present(
    git_repo: Path,
    fake_cli_dir: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worktree created via task-start is healed synchronously: the bootstrap
    adopt command is invoked with the new worktree as --target."""
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    # Mark the primary as bootstrap-installed (marker + materialized clone) so
    # adoption is attempted.
    (git_repo / ".workstate-bootstrap.json").write_text("{}\n")
    (git_repo / ".workstate" / "remote").mkdir(parents=True)
    # Fake adopt CLI: record args + drop a sentinel in --target to prove it ran.
    adopt_log = tmp_path / "adopt-args.txt"
    fake_adopt = tmp_path / "fake-adopt.sh"
    fake_adopt.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "%s\\n" "$*" >> "{adopt_log}"\n'
        'tgt=""\n'
        'while [ $# -gt 0 ]; do if [ "$1" = "--target" ]; then tgt="$2"; fi; shift; done\n'
        'mkdir -p "$tgt/.workstate" && : > "$tgt/.workstate/ADOPTED"\n'
        "exit 0\n"
    )
    fake_adopt.chmod(0o755)
    monkeypatch.setenv("WORKSTATE_ADOPT_CMD", str(fake_adopt))

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-78",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["overlay_adopted"] is True
    wt = Path(receipt["worktree_path"])
    assert (wt / ".workstate" / "ADOPTED").exists()
    assert f"--target {wt}" in adopt_log.read_text()


def test_task_start_worktree_skips_adopt_without_marker(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """No .workstate-bootstrap.json marker -> adoption is skipped (non-fatal),
    so task-start never spawns the bootstrap CLI for a non-overlay primary."""
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-79",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["overlay_adopted"] is False


def test_task_start_worktree_skips_adopt_without_clone(
    git_repo: Path, fake_cli_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marker present but NO .workstate/remote clone (the self-hosting monorepo
    shape) -> adoption is skipped, so task-start never spawns a doomed bootstrap
    subprocess for a source repo that ships (not adopts) the overlay."""
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    # Marker only, no clone.
    (git_repo / ".workstate-bootstrap.json").write_text("{}\n")
    # If the gate were marker-only, this fake would run and exit 1 (overlay_adopted
    # would flip via exit code); with the clone gate it must never be invoked.
    monkeypatch.setenv("WORKSTATE_ADOPT_CMD", "/nonexistent/should-not-run")

    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-80",
        "--mode",
        "worktree",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["overlay_adopted"] is False
