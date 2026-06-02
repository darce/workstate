"""WORKSTATE-REF-17-14 implementation note acceptance test: root-visible task plans.

Goal from founding implementation note:
    "From the root workspace, the operator can see every active task's
    plan path and branch in DASHBOARD.txt. From the root workspace,
    the operator can open any active task plan on demand without
    changing the root checkout."

This test simulates a consuming repo that has two concurrent active
tasks living in sibling worktrees and asserts that:

1. Both task plans appear in DASHBOARD.txt under the ACTIVE TASK PLANS
   section, with their branch, repo-relative plan path, and resolved
   absolute path.
2. The exists marker (✓/✗) reflects the live filesystem state of the
   plan file in its sibling worktree.
3. The root workspace itself stays on its primary checkout — discovery
   does not require switching branches.

The test home is `workstate-bootstrap` rather than a target repo so the
property holds for *every* future consumer repo, not a specific
installation.
"""

from __future__ import annotations

import pathlib
import re
import subprocess
import tempfile

import pytest

pytest.importorskip("workstate_handoff_mcp")
pytest.importorskip("workstate_protocol")


def _git(*args: str, cwd: pathlib.Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
    )


def _make_consumer_repo(root: pathlib.Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "README.md").write_text("consumer\n")
    _git("add", "README.md", cwd=root)
    _git("commit", "-m", "seed", cwd=root)


def test_root_workspace_sees_every_active_task_plan() -> None:
    from workstate_handoff_mcp.config import RuntimeConfig
    from workstate_handoff_mcp.dashboard_rendering import generate_dashboard_md
    from workstate_handoff_mcp.handoff_state import set_handoff_state
    from workstate_handoff_mcp.runtime import configure_runtime

    with tempfile.TemporaryDirectory() as raw:
        consumer_root = pathlib.Path(raw) / "consumer"
        _make_consumer_repo(consumer_root)

        # Two sibling worktrees, each owning its own task plan file.
        # We do not need real git worktrees for the visibility check —
        # the handoff runtime resolves plan paths against any directory
        # passed as target_worktree_path.
        wt_a = pathlib.Path(raw) / "wt-feature-a"
        wt_b = pathlib.Path(raw) / "wt-feature-b"
        (wt_a / "docs" / "tasks").mkdir(parents=True)
        (wt_b / "docs" / "tasks").mkdir(parents=True)
        (wt_a / "docs" / "tasks" / "a.md").write_text("# Plan A\n")
        # Intentionally do NOT write b.md — we want to see the missing
        # marker in the dashboard for the second task.

        configure_runtime(RuntimeConfig.for_workspace(consumer_root))

        set_handoff_state(
            task_ref="FEAT-A",
            objective="feature A",
            target_branch="feature/a",
            target_worktree_path=str(wt_a),
            task_plan_path="docs/tasks/a.md",
        )
        set_handoff_state(
            task_ref="FEAT-B",
            objective="feature B",
            target_branch="feature/b",
            target_worktree_path=str(wt_b),
            task_plan_path="docs/tasks/b.md",
        )

        result = generate_dashboard_md(write_file=False)
        md = result["markdown"]

        # 1. The dashboard surfaces both plans under one section.
        section_match = re.search(
            r"ACTIVE TASK PLANS\n-+\n(?P<body>.*?)(?:\n[A-Z][A-Z ]+\n-+|\Z)",
            md,
            re.DOTALL,
        )
        assert section_match, "ACTIVE TASK PLANS section missing from dashboard"
        body = section_match.group("body")

        # 2. Each task is listed with branch, repo-relative plan path,
        #    and the current read-hint contract.
        assert "[FEAT-A] branch=feature/a" in body, body
        assert "[FEAT-B] branch=feature/b" in body, body
        assert "plan: docs/tasks/a.md (read: unavailable on feature/a)" in body, body
        assert "plan: docs/tasks/b.md (read: unavailable on feature/b)" in body, body

        # 3. Resolved absolute paths point into the sibling worktrees.
        assert str(wt_a / "docs" / "tasks" / "a.md") in body, body
        assert str(wt_b / "docs" / "tasks" / "b.md") in body, body

        # 4. The exists marker reflects live filesystem state.
        assert "✓" in body, "missing ✓ marker for FEAT-A whose plan exists"
        assert "✗" in body, "missing ✗ marker for FEAT-B whose plan does not exist"

        # 5. The consumer root never had to switch branches.
        head = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(consumer_root),
            text=True,
        ).strip()
        assert head == "main", f"root workspace switched to {head!r} during discovery"


def test_dashboard_lists_tasks_missing_task_plan_path_separately() -> None:
    """Operator-facing nudge: tasks that haven't yet declared a
    task_plan_path are listed at the bottom so it's obvious which
    ones still need metadata.
    """
    from workstate_handoff_mcp.config import RuntimeConfig
    from workstate_handoff_mcp.dashboard_rendering import generate_dashboard_md
    from workstate_handoff_mcp.handoff_state import set_handoff_state
    from workstate_handoff_mcp.runtime import configure_runtime

    with tempfile.TemporaryDirectory() as raw:
        consumer_root = pathlib.Path(raw) / "consumer"
        _make_consumer_repo(consumer_root)
        configure_runtime(RuntimeConfig.for_workspace(consumer_root))

        set_handoff_state(
            task_ref="WITH-PLAN",
            objective="has plan_path",
            target_branch="feature/with",
            target_worktree_path=str(consumer_root),
            task_plan_path="docs/tasks/with.md",
        )
        set_handoff_state(
            task_ref="WITHOUT-PLAN",
            objective="legacy task that hasn't been migrated yet",
            target_branch="feature/without",
            target_worktree_path=str(consumer_root),
        )

        md = generate_dashboard_md(write_file=False)["markdown"]
        assert "[WITH-PLAN]" in md
        # The "without plan" footer is exactly what nudges operators to backfill.
        assert "(no task_plan_path set for: WITHOUT-PLAN)" in md, md
