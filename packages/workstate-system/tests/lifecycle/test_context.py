"""implementation note contract tests for the read-only ``context`` subcommand.

The handler returns a stable JSON receipt under ``--json`` describing
the current worktree, branch, HEAD, derived task ref, dirty summary,
matching active-task plan path, and a next-recommended-command hint.
Cwd/branch is the source of truth for read-only ops: when the MCP
active-task snapshot disagrees with the branch-derived task ref, the
handler picks the branch-derived ref, writes a
``workflow_ambiguity_resolved`` decision (or spools it when MCP is
offline), and reports the ambiguity in ``events``.

Cases below cover the slice's required scenarios: no derived task on
``main``, conforming and non-conforming branches, dirty root, MCP
unavailable, MCP disagreement, and a linked-worktree reuse.
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
FIXTURES_DIR = Path(__file__).parent / "fixtures" / "receipts"


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


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "init", "-q",
        ],
        check=True,
    )
    return repo


@pytest.fixture
def fake_cli_dir(tmp_path: Path) -> Path:
    return tmp_path / "fake-cli"


def _run_context(
    cwd: Path,
    fake_cli: Path | None,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if fake_cli is not None:
        env["MCP_AGENT_HANDOFF_BIN"] = str(fake_cli)
    else:
        env["MCP_AGENT_HANDOFF_BIN"] = "/nonexistent/no-such-binary-xyz"
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "context", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _write_current_task(repo: Path, payload: dict) -> None:
    # Mirror the live writer (current_task_rendering._build_current_task_file_payload):
    # schema_version: 1 is part of every emitted payload. Tests omitted
    # it historically; the WORKSTATE-REF-54 implementation note.5 reader migration through
    # load_workspace_summary_compat is strict about the field.
    payload = {"schema_version": 1, **payload}
    target = repo / "CURRENT_TASK.json"
    target.write_text(json.dumps(payload))


def _fake_cli_body_with_workspace_summary(
    summary: dict | None,
    *,
    other_exit_code: int = 1,
) -> str:
    """Bash fake-CLI body that answers ``render-handoff`` with ``summary``.

    WORKSTATE-REF-54-FU implementation note: the lifecycle readers consume
    ``render-handoff --kind=current_task --no-write`` instead of reading
    ``CURRENT_TASK.json`` directly. The fake CLI must therefore surface
    the same summary through the rendered envelope.

    Non-render-handoff commands exit ``other_exit_code`` so the rest of
    the lifecycle's MCP behavior (e.g. spooled / synced projection) is
    preserved.
    """
    if summary is None:
        current_task_json = json.dumps(
            {"schema_version": 2, "shape": "none", "task_ref": None, "active": None, "tasks": []}
        )
        envelope_task_ref: str | None = None
    else:
        current_task_json = json.dumps(summary)
        envelope_task_ref = summary.get("task_ref")
    envelope = {
        "schema_version": 2,
        "tool": "render_handoff",
        "ok": True,
        "data": {
            "task_ref": envelope_task_ref,
            "path": "/tmp/CURRENT_TASK.json",
            "written": False,
            "current_task_json": current_task_json,
        },
    }
    return (
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"render-handoff"* ]]; then\n'
        f"  cat <<'ENVELOPE_EOF'\n{json.dumps(envelope)}\nENVELOPE_EOF\n"
        "  exit 0\n"
        "fi\n"
        f"exit {other_exit_code}\n"
    )


def _v2_single_summary_from_active(active: dict) -> dict:
    """Build a v2 ``shape='single'`` summary from a legacy ``active`` dict."""
    task_ref = active["task_ref"]
    return {
        "schema_version": 2,
        "shape": "single",
        "task_ref": task_ref,
        "active": active,
        "tasks": [],
    }


def _v2_ambiguous_summary_from_tasks(*tasks: dict) -> dict:
    return {
        "schema_version": 2,
        "shape": "workspace_ambiguous",
        "task_ref": None,
        "active": None,
        "tasks": list(tasks),
    }


# ---------- Required-field schema ----------


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


def test_context_receipt_has_required_schema_fields(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    for field in REQUIRED_FIELDS:
        assert field in receipt, f"missing required field {field!r}: {receipt!r}"


# ---------- Scenario coverage ----------


def test_context_on_main_with_no_current_task_reports_null_task_ref(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli, "--json")
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == "context"
    assert receipt["task_ref"] is None
    assert receipt["branch"] == "main"
    assert receipt["worktree_path"] == str(git_repo)
    assert receipt["head"] == _git(git_repo, "rev-parse", "HEAD")
    assert receipt["events"] == ["context_loaded"]


def test_context_on_conforming_branch_derives_task_ref(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli, "--json")
    receipt = json.loads(proc.stdout)
    assert receipt["task_ref"] == "WORKSTATE-REF-77"
    assert receipt["branch"] == "feature/WORKSTATE-77-x"


def test_context_on_non_conforming_branch_returns_null_task_ref(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "fix/wat"],
        check=True,
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli, "--json")
    receipt = json.loads(proc.stdout)
    assert receipt["task_ref"] is None
    # next_command points the operator at task-start because the branch
    # cannot derive a task ref.
    assert "task-start" in receipt["next_command"]


def test_context_reports_dirty_summary(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    (git_repo / "untracked.txt").write_text("u\n")
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli, "--json")
    receipt = json.loads(proc.stdout)
    assert receipt["dirty_summary"]["untracked"] == 1
    assert receipt["dirty_summary"]["total"] == 1


def test_context_mcp_unavailable_reports_synced_when_no_event_needed(
    git_repo: Path
) -> None:
    # No MCP binary at all: handoff_projection stays "synced" because
    # the read-only context did not attempt a write that needed
    # spooling.
    proc = _run_context(git_repo, None, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["handoff_projection"] == "synced"
    assert "ambiguity_resolved" not in receipt["events"]


def test_context_mcp_disagreement_writes_workflow_ambiguity_resolved(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-50-y"],
        check=True,
    )
    # The active task surfaced by render-handoff disagrees with the branch.
    active = {
        "task_ref": "WORKSTATE-REF-99",
        "status": "in_progress",
        "task_plan_path": "docs/plans/0099-other.md",
        "target_branch": "feature/WORKSTATE-99-other",
        "target_worktree_path": "/tmp/elsewhere",
    }
    # MCP rejects other commands (exit 1) so the ambiguity decision spools.
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(_v2_single_summary_from_active(active)),
    )
    proc = _run_context(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    # Branch wins for read-only ops.
    assert receipt["task_ref"] == "WORKSTATE-REF-50"
    assert "ambiguity_resolved" in receipt["events"]
    # WORKSTATE-REF-52 implementation note: CLI ran + rejected (exit 1) → ``spooled``.
    assert receipt["handoff_projection"] == "spooled"
    # The pending event records the resolved ambiguity for replay.
    spool = git_repo / PENDING_REL
    assert spool.exists()
    payload = json.loads(spool.read_text().splitlines()[-1])
    assert payload["kind"] == "decision"
    assert "workflow_ambiguity_resolved" in payload["decision_id"]


def test_context_ignores_completed_current_task_snapshot(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-50-y"],
        check=True,
    )
    _write_current_task(
        git_repo,
        {
            "active": {
                "task_ref": "WORKSTATE-REF-41",
                "status": "done",
                "task_plan_path": "docs/tasks/WORKSTATE-REF-41-task-plan.md",
                "target_branch": "feature/WORKSTATE-41-old",
            }
        },
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")

    proc = _run_context(git_repo, fake_cli, "--json")

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["task_ref"] == "WORKSTATE-REF-50"
    assert receipt["plan_path"] is None
    assert receipt["events"] == ["context_loaded"]
    assert receipt["handoff_projection"] == "synced"
    assert not (git_repo / PENDING_REL).exists()


def test_context_includes_plan_path_from_current_task(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-22-z"],
        check=True,
    )
    active = {
        "task_ref": "WORKSTATE-REF-22",
        "status": "in_progress",
        "task_plan_path": "docs/plans/0022-thing.md",
        "target_branch": "feature/WORKSTATE-22-z",
        "target_worktree_path": str(git_repo),
    }
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(_v2_single_summary_from_active(active)),
    )
    proc = _run_context(git_repo, fake_cli, "--json")
    receipt = json.loads(proc.stdout)
    assert receipt["plan_path"] == "docs/plans/0022-thing.md"


def test_context_prints_plan_line_when_plan_path_set(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """implementation note: cold-start `make context` must print a human-readable
    `plan: <branch>:<rel_path>` line to stderr so the operator sees the
    active plan without reading the JSON receipt or MCP state.
    """
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-22-z"],
        check=True,
    )
    active = {
        "task_ref": "WORKSTATE-REF-22",
        "status": "in_progress",
        "task_plan_path": "docs/plans/0022-thing.md",
        "target_branch": "feature/WORKSTATE-22-z",
        "target_worktree_path": str(git_repo),
    }
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(_v2_single_summary_from_active(active)),
    )
    proc = _run_context(git_repo, fake_cli)  # no --json: stderr summary
    assert proc.returncode == 0, proc.stderr
    assert "plan: feature/WORKSTATE-22-z:docs/plans/0022-thing.md" in proc.stderr, proc.stderr


def test_context_prints_branch_plan_when_workspace_summary_is_ambiguous(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77"],
        check=True,
    )
    active = {
        "task_ref": "WORKSTATE-REF-77",
        "status": "in_progress",
        "task_plan_path": "docs/plans/WORKSTATE-REF-77.md",
        "target_branch": "feature/WORKSTATE-77",
        "target_worktree_path": str(git_repo),
    }
    other = {
        "task_ref": "WORKSTATE-REF-79",
        "status": "in_progress",
        "task_plan_path": "docs/plans/WORKSTATE-REF-79.md",
        "target_branch": "feature/WORKSTATE-79",
        "target_worktree_path": "/tmp/other-worktree",
    }
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(
            _v2_ambiguous_summary_from_tasks(active, other)
        ),
    )

    proc = _run_context(git_repo, fake_cli)

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["task_ref"] == "WORKSTATE-REF-77"
    assert receipt["plan_path"] == "docs/plans/WORKSTATE-REF-77.md"
    assert "plan: feature/WORKSTATE-77:docs/plans/WORKSTATE-REF-77.md" in proc.stderr, proc.stderr


def test_context_prints_plan_unset_hint_when_plan_path_missing(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """implementation note: when no `task_plan_path` is set on the active task, the
    stderr summary still emits a `plan:` line that points the operator
    at `make plan-register`.
    """
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-23-w"],
        check=True,
    )
    _write_current_task(
        git_repo,
        {
            "active": {
                "task_ref": "WORKSTATE-REF-23",
                "target_branch": "feature/WORKSTATE-23-w",
                "target_worktree_path": str(git_repo),
            }
        },
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli)
    assert proc.returncode == 0, proc.stderr
    assert "plan: <unset>" in proc.stderr, proc.stderr
    assert "make plan-register" in proc.stderr, proc.stderr


def test_context_runs_inside_linked_worktree(
    git_repo: Path, tmp_path: Path, fake_cli_dir: Path
) -> None:
    extra = tmp_path / "wt-extra"
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-9-q", str(extra),
        ],
        check=True,
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(extra, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["worktree_path"] == str(extra)
    assert receipt["branch"] == "feature/WORKSTATE-9-q"
    assert receipt["task_ref"] == "WORKSTATE-REF-9"


def test_context_reads_current_task_from_canonical_workspace(
    git_repo: Path, tmp_path: Path, fake_cli_dir: Path
) -> None:
    """When run from a linked worktree, ``context`` must surface
    ``plan_path`` from the canonical workspace's ``CURRENT_TASK.json``,
    not just the worktree-local file. Regression for BR-WORKSTATE40-S2-02."""
    extra = tmp_path / "wt-canon"
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-12-k", str(extra),
        ],
        check=True,
    )
    # WORKSTATE-REF-54-FU implementation note: the active task is now surfaced through the
    # rendered envelope rather than a workspace-local CURRENT_TASK.json.
    # The fake CLI emits the same v2 single-shape summary regardless of
    # the cwd the wrapper invokes it from — preserving the original
    # contract that a linked-worktree caller picks up the canonical
    # workspace's view of "what task is active here?".
    active = {
        "task_ref": "WORKSTATE-REF-12",
        "status": "in_progress",
        "task_plan_path": "docs/plans/0012-thing.md",
        "target_branch": "feature/WORKSTATE-12-k",
        "target_worktree_path": str(extra),
    }
    assert not (extra / "CURRENT_TASK.json").exists()

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(_v2_single_summary_from_active(active)),
    )
    proc = _run_context(extra, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["worktree_path"] == str(extra)
    assert receipt["task_ref"] == "WORKSTATE-REF-12"
    assert receipt["plan_path"] == "docs/plans/0012-thing.md"


def test_context_falls_back_to_branch_projection_when_render_handoff_fails(
    git_repo: Path, tmp_path: Path, fake_cli_dir: Path
) -> None:
    extra = tmp_path / "wt-context-fallback"
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-77", str(extra),
        ],
        check=True,
    )
    projection_dir = git_repo / ".task-state" / "current"
    projection_dir.mkdir(parents=True, exist_ok=True)
    (projection_dir / "WORKSTATE-REF-77.json").write_text(
        json.dumps(
            {
                "task_projection_schema_version": 1,
                "task_ref": "WORKSTATE-REF-77",
                "status": "in_progress",
                "task_plan_path": "docs/plans/WORKSTATE-REF-77.md",
                "target_branch": "feature/WORKSTATE-77",
                "target_worktree_path": str(extra),
            }
        ),
        encoding="utf-8",
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"render-handoff"* ]]; then exit 124; fi\n'
        "exit 1\n",
    )

    proc = _run_context(extra, fake_cli, "--json")

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["task_ref"] == "WORKSTATE-REF-77"
    assert receipt["plan_path"] == "docs/plans/WORKSTATE-REF-77.md"


def test_context_outside_git_repo_returns_error_and_exits_two(
    tmp_path: Path
) -> None:
    outside = tmp_path / "no-repo"
    outside.mkdir()
    proc = _run_context(outside, None, "--json")
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["command"] == "context"
    assert "error" in receipt


# ---------- Receipt fixture round-trip ----------


def test_context_receipt_matches_fixture_shape(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """Fixture file pins the canonical key set; per-run values vary."""
    fixture_path = FIXTURES_DIR / "context.json"
    assert fixture_path.is_file(), f"missing fixture: {fixture_path}"
    fixture = json.loads(fixture_path.read_text())

    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli, "--json")
    receipt = json.loads(proc.stdout)

    assert set(receipt.keys()) == set(fixture.keys()), (
        f"receipt keys {sorted(receipt)!r} drift from fixture {sorted(fixture)!r}"
    )
    # Spot-check field types match the fixture exemplar.
    for key, exemplar in fixture.items():
        if exemplar is None:
            assert receipt[key] is None or isinstance(receipt[key], type(exemplar) | str), key
        else:
            assert isinstance(receipt[key], type(exemplar)), (
                f"field {key!r}: expected {type(exemplar).__name__}, "
                f"got {type(receipt[key]).__name__}"
            )


# ---------- Stub registry update ----------


# WORKSTATE-REF-72 implementation note: plan_baseline visibility on the context receipt.


def test_context_plan_baseline_accepted_when_plan_on_main(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    """When the active task's plan is committed on ``main``, the context
    receipt surfaces ``plan_baseline.status == 'accepted'`` so cold-start
    agents can see the gate has cleared without re-running the
    evaluator."""
    plan_path = git_repo / "plans" / "WORKSTATE-REF-22.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-22 Plan\n")
    subprocess.run(["git", "-C", str(git_repo), "add", "plans/WORKSTATE-REF-22.md"], check=True)
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t", "-c", "user.name=t",
            "commit", "-m", "accept plan", "-q",
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-22-z"],
        check=True,
    )
    active = {
        "task_ref": "WORKSTATE-REF-22",
        "status": "in_progress",
        "task_plan_path": "plans/WORKSTATE-REF-22.md",
        "target_branch": "feature/WORKSTATE-22-z",
        "target_worktree_path": str(git_repo),
    }
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(
        fake_cli,
        _fake_cli_body_with_workspace_summary(_v2_single_summary_from_active(active)),
    )
    proc = _run_context(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    baseline = receipt.get("plan_baseline")
    assert baseline is not None, receipt
    assert baseline["status"] == "accepted"
    assert baseline["task_plan_path"] == "plans/WORKSTATE-REF-22.md"
    assert baseline["target_branch"] == "feature/WORKSTATE-22-z"
    assert "plan_baseline_evaluated" in receipt["events"]


def test_context_plan_baseline_omitted_when_no_plan_registered(
    git_repo: Path,
    fake_cli_dir: Path,
) -> None:
    """Without a registered plan path the receipt must keep
    ``plan_baseline=None`` so the visibility surface does not invent a
    drift signal."""
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-23-w"],
        check=True,
    )
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 1\n")
    proc = _run_context(git_repo, fake_cli, "--json")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt.get("plan_baseline") is None


def test_context_no_longer_in_failing_stub_registry() -> None:
    """implementation note lifts ``context`` out of STUB_HANDLERS."""
    sys.path.insert(0, str(LIFECYCLE_PKG))
    try:
        for mod in ("cli", "handlers", "handlers._common", "handlers.context"):
            sys.modules.pop(mod, None)
        import cli  # type: ignore[import-not-found]

        assert "context" not in cli.STUB_HANDLERS
    finally:
        sys.path.remove(str(LIFECYCLE_PKG))
