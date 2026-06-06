"""Unit tests for ``workstate_handoff_mcp.plan_resolve`` (implementation note implementation note).

The resolver is a pure read over ``get_handoff_state`` plus a single
``git show <branch>:<path>`` probe. It returns ``PlanLocation`` with
``branch``, ``path``, and ``exists_on_branch``. implementation note covers three
behaviours:

1. Happy path: branch + path round-trip from the seeded handoff row,
   and ``exists_on_branch`` is True when the path is committed on the
   target branch.
2. ``PlanPathNotRegistered`` when the active task has no
   ``task_plan_path`` (DoD: explicit error, not a silent ``None``).
3. ``exists_on_branch=False`` when ``git show`` exits non-zero (the
   branch does not exist locally / the path is not committed there).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


@pytest.fixture()
def workspace_repo(tmp_path: Path) -> Path:
    """Initialise a real git repo as the workspace root + configure runtime."""
    _git("init", "--initial-branch=main", cwd=tmp_path)
    _git("config", "user.email", "plan-resolve@test", cwd=tmp_path)
    _git("config", "user.name", "Plan Resolve Test", cwd=tmp_path)

    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    return tmp_path


def _commit_plan_on_branch(repo: Path, branch: str, rel_path: str, body: str) -> None:
    """Create ``branch`` and commit ``rel_path`` on it. Returns to original branch."""
    # Seed an initial commit on the default branch so HEAD is resolvable.
    if not (repo / ".git" / "refs" / "heads" / "main").exists():
        (repo / "README.md").write_text("seed\n")
        _git("add", "README.md", cwd=repo)
        _git("commit", "-m", "seed", cwd=repo)
    starting_branch = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    _git("checkout", "-b", branch, cwd=repo)
    plan_abs = repo / rel_path
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text(body)
    _git("add", rel_path, cwd=repo)
    _git("commit", "-m", f"add {rel_path}", cwd=repo)
    _git("checkout", starting_branch, cwd=repo)


def test_resolve_returns_branch_path_exists(workspace_repo: Path) -> None:
    from workstate_handoff_mcp import resolve_plan_location

    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    _commit_plan_on_branch(workspace_repo, branch, rel, "# demo plan\n")

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="resolver happy-path test",
        status="in_progress",
        target_branch=branch,
        task_plan_path=rel,
    )

    location = resolve_plan_location(task_ref="WORKSTATE-REF-99")

    assert location.branch == branch
    assert location.path == rel
    assert location.exists_on_branch is True


def test_resolve_raises_when_task_plan_path_unset(workspace_repo: Path) -> None:
    from workstate_handoff_mcp import PlanPathNotRegistered, resolve_plan_location

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="missing-path test",
        status="in_progress",
        target_branch="feature/WORKSTATE-99",
        # task_plan_path intentionally omitted
    )

    with pytest.raises(PlanPathNotRegistered):
        resolve_plan_location(task_ref="WORKSTATE-REF-99")


def test_resolve_marks_exists_false_when_branch_not_fetched(workspace_repo: Path) -> None:
    from workstate_handoff_mcp import resolve_plan_location

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="branch-not-fetched test",
        status="in_progress",
        target_branch="feature/never-pushed-here",
        task_plan_path="docs/plans/0099-never.md",
    )

    location = resolve_plan_location(task_ref="WORKSTATE-REF-99")

    assert location.branch == "feature/never-pushed-here"
    assert location.path == "docs/plans/0099-never.md"
    assert location.exists_on_branch is False


def test_list_active_task_locations_returns_one_per_active_task(
    workspace_repo: Path,
) -> None:
    """Default behaviour: enumerate every active (non-archived) row, one
    `PlanLocation` per row, ordered by `updated_at` desc.

    Seeds three tasks: one active with `task_plan_path` set, one active
    with it `None`, one archived (must be excluded). The two active rows
    are returned in `updated_at` descending order.
    """
    from workstate_handoff_mcp import list_active_task_locations

    # Created in order: archived (oldest), active+path (middle), active+no-path (newest).
    # Sleep 1.05s between writes so the second-resolution `updated_at`
    # column is strictly monotonic — proves the resolver honours
    # `ORDER BY updated_at DESC` rather than the alphabetic tiebreaker.
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-OLD",
        objective="archived task",
        status="in_progress",
        target_branch="feature/old",
        task_plan_path="docs/plans/old.md",
    )
    mcp_server.archive_task_state(task_ref="WORKSTATE-REF-OLD")

    time.sleep(1.05)
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-MID",
        objective="active with path",
        status="in_progress",
        target_branch="feature/mid",
        task_plan_path="docs/plans/mid.md",
    )

    time.sleep(1.05)
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-NEW",
        objective="active without path",
        status="in_progress",
        target_branch="feature/new",
        # task_plan_path intentionally omitted
    )

    locations = list_active_task_locations()

    refs = [(loc.task_ref, loc.path) for loc in locations]
    # Both non-archived returned; archived (WORKSTATE-REF-OLD) excluded by construction.
    assert refs == [
        ("WORKSTATE-REF-NEW", None),
        ("WORKSTATE-REF-MID", "docs/plans/mid.md"),
    ], refs


def test_list_active_task_locations_include_unset_path_flag(
    workspace_repo: Path,
) -> None:
    """`include_unset_path=True` (the default) returns the unset-path row
    with `path=None`. Locking the rendered shape so implementation note
    (`plans-list`) can rely on it without per-row None-checks creeping
    into the formatter.
    """
    from workstate_handoff_mcp import list_active_task_locations

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-NOTASKTH",
        objective="active without path",
        status="in_progress",
        target_branch="feature/nopath",
    )

    locations = list_active_task_locations(include_unset_path=True)

    assert len(locations) == 1
    only = locations[0]
    assert only.task_ref == "WORKSTATE-REF-NOTASKTH"
    assert only.path is None
    assert only.branch == "feature/nopath"
    assert only.exists_on_branch is False


def test_list_active_task_locations_excludes_unset_when_flag_false(
    workspace_repo: Path,
) -> None:
    """`include_unset_path=False` filters out rows with no plan path —
    the use case for callers that only care about resolvable plans."""
    from workstate_handoff_mcp import list_active_task_locations

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-NOTASKTH",
        objective="active without path",
        status="in_progress",
        target_branch="feature/nopath",
    )
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-WITH",
        objective="active with path",
        status="in_progress",
        target_branch="feature/with",
        task_plan_path="docs/plans/with.md",
    )

    locations = list_active_task_locations(include_unset_path=False)

    assert [loc.task_ref for loc in locations] == ["WORKSTATE-REF-WITH"]


def test_list_active_task_locations_excludes_done_status(
    workspace_repo: Path,
) -> None:
    """`done` rows live in `handoff_state` until archive runs but are
    not "active" for the plans-list coordinator surface — only
    `in_progress`/`review`/`blocked` belong on a "what plans do I have
    open" view. Regression test for BR-WORKSTATE38-S1B-01: the original
    implementation filtered with `HANDOFF_ACTIVE_STATUSES` which
    includes `done`, leaking completed-but-not-archived tasks into
    `plans-list` until the operator ran archive cleanup.
    """
    from workstate_handoff_mcp import list_active_task_locations

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-DONE",
        objective="completed task awaiting archive",
        status="in_progress",
        target_branch="feature/done",
        task_plan_path="docs/plans/done.md",
    )
    upd = mcp_server.update_task_status(task_ref="WORKSTATE-REF-DONE", status="done", expected_revision=0)
    assert upd.get("ok"), upd

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-LIVE",
        objective="still in progress",
        status="in_progress",
        target_branch="feature/live",
        task_plan_path="docs/plans/live.md",
    )

    locations = list_active_task_locations()

    assert [loc.task_ref for loc in locations] == ["WORKSTATE-REF-LIVE"], [loc.task_ref for loc in locations]


def test_plan_show_command_returns_git_show_argv() -> None:
    """``plan_show_command`` returns the argv list that drives the
    ``make plan-show`` recipe in implementation note. Locking the shape here so the
    implementation note wiring does not have to redefine it.
    """
    from workstate_handoff_mcp import plan_show_command

    argv = plan_show_command(branch="feature/x", path="docs/plans/0099.md")

    assert argv == ["git", "show", "feature/x:docs/plans/0099.md"]


def test_discover_recognises_numbered_plan_via_frontmatter(workspace_repo: Path) -> None:
    """Regression for BR-WORKSTATE38-S5-01.

    Real plans use the canonical ``docs/plans/NNNN-<slug>.md`` layout
    where the filename does *not* contain the task ref (e.g.
    ``docs/plans/0007-mcp-resolved-task-plan-paths.md`` for WORKSTATE-REF-38).
    Filename-substring matching alone returns zero candidates and
    breaks the default ``make plan-register`` path that implementation note claims
    to provide. Discovery must also scan plan content for a
    ``Task ID: <ref>`` declaration.
    """
    from workstate_handoff_mcp.plan_resolve import discover_plan_path_candidates

    plan_path = workspace_repo / "docs/plans/0007-mcp-resolved-task-plan-paths.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# implementation note — MCP-Resolved Task-Plan Paths\n\n> **Metadata**\n> - **Task ID**: `WORKSTATE-REF-38`\n")

    candidates = discover_plan_path_candidates("WORKSTATE-REF-38")

    assert candidates == ["docs/plans/0007-mcp-resolved-task-plan-paths.md"], candidates


def test_discover_dedupes_filename_and_frontmatter_match(workspace_repo: Path) -> None:
    """A plan whose filename contains the task ref AND whose frontmatter
    declares the same Task ID is one candidate, not two.
    """
    from workstate_handoff_mcp.plan_resolve import discover_plan_path_candidates

    plan_path = workspace_repo / "docs/plans/0099-WORKSTATE-99-demo.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# demo\n> - **Task ID**: `WORKSTATE-REF-99`\n")

    candidates = discover_plan_path_candidates("WORKSTATE-REF-99")

    assert candidates == ["docs/plans/0099-WORKSTATE-99-demo.md"], candidates


def test_resolve_prefer_baseline_returns_main_when_plan_present(workspace_repo: Path) -> None:
    """``prefer="baseline"`` returns a location anchored on ``main`` when
    the plan file exists on main. implementation note contract: coordinators on main
    read the plan from main, not the feature branch's working copy."""
    from workstate_handoff_mcp import resolve_plan_location

    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-baseline-present.md"
    # Plan exists on BOTH main and the feature branch. Baseline must win.
    (workspace_repo / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=workspace_repo)
    _git("commit", "-m", "seed", cwd=workspace_repo)
    plan_abs = workspace_repo / rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# baseline plan\n")
    _git("add", rel, cwd=workspace_repo)
    _git("commit", "-m", "add plan to main", cwd=workspace_repo)
    _git("checkout", "-b", branch, cwd=workspace_repo)
    _git("checkout", "main", cwd=workspace_repo)

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="prefer-baseline test",
        status="in_progress",
        target_branch=branch,
        task_plan_path=rel,
    )

    location = resolve_plan_location(task_ref="WORKSTATE-REF-99", prefer="baseline")

    assert location.branch == "main"
    assert location.path == rel
    assert location.exists_on_branch is True


def test_resolve_prefer_baseline_marks_exists_false_when_main_missing(workspace_repo: Path) -> None:
    """``prefer="baseline"`` never falls back to the working copy. When
    main lacks the plan the call returns the baseline branch with
    ``exists_on_branch=False`` so the operator knows the plan was not
    accepted yet."""
    from workstate_handoff_mcp import resolve_plan_location

    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-branch-only.md"
    _commit_plan_on_branch(workspace_repo, branch, rel, "# branch-only plan\n")

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="prefer-baseline missing test",
        status="in_progress",
        target_branch=branch,
        task_plan_path=rel,
    )

    location = resolve_plan_location(task_ref="WORKSTATE-REF-99", prefer="baseline")

    assert location.branch == "main"
    assert location.path == rel
    assert location.exists_on_branch is False


def test_resolve_prefer_working_copy_returns_branch_snapshot(workspace_repo: Path) -> None:
    """``prefer="working_copy"`` returns the feature branch even when
    main also has the plan — operator forced the working-copy view."""
    from workstate_handoff_mcp import resolve_plan_location

    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-working.md"
    _commit_plan_on_branch(workspace_repo, branch, rel, "# branch plan\n")

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="prefer-working-copy test",
        status="in_progress",
        target_branch=branch,
        task_plan_path=rel,
    )

    location = resolve_plan_location(task_ref="WORKSTATE-REF-99", prefer="working_copy")

    assert location.branch == branch
    assert location.path == rel
    assert location.exists_on_branch is True


def test_resolve_prefer_auto_falls_back_to_working_copy(workspace_repo: Path) -> None:
    """``prefer="auto"`` checks baseline first; falls back to the
    working copy when the plan is not yet on main. The fallback is the
    pre-acceptance state — plans live on feature branches until the
    accept flow lands."""
    from workstate_handoff_mcp import resolve_plan_location

    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-not-on-main.md"
    _commit_plan_on_branch(workspace_repo, branch, rel, "# branch plan\n")

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="prefer-auto fallback test",
        status="in_progress",
        target_branch=branch,
        task_plan_path=rel,
    )

    location = resolve_plan_location(task_ref="WORKSTATE-REF-99", prefer="auto")

    assert location.branch == branch
    assert location.exists_on_branch is True


def test_resolve_prefer_auto_picks_baseline_when_main_has_plan(workspace_repo: Path) -> None:
    """When the plan exists on both main and the feature branch,
    ``prefer="auto"`` returns the baseline view — the post-acceptance
    steady state where coordinators on main read the plan locally."""
    from workstate_handoff_mcp import resolve_plan_location

    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-on-both.md"
    (workspace_repo / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=workspace_repo)
    _git("commit", "-m", "seed", cwd=workspace_repo)
    plan_abs = workspace_repo / rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# accepted plan\n")
    _git("add", rel, cwd=workspace_repo)
    _git("commit", "-m", "accept plan on main", cwd=workspace_repo)
    _git("checkout", "-b", branch, cwd=workspace_repo)
    _git("checkout", "main", cwd=workspace_repo)

    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-99",
        objective="prefer-auto baseline-present test",
        status="in_progress",
        target_branch=branch,
        task_plan_path=rel,
    )

    location = resolve_plan_location(task_ref="WORKSTATE-REF-99", prefer="auto")

    assert location.branch == "main"
    assert location.exists_on_branch is True


def test_discover_ignores_plans_referencing_other_tasks(workspace_repo: Path) -> None:
    """Discovery must not return plans that merely *mention* the task
    ref in prose; only the canonical ``Task ID:`` declaration counts.
    Otherwise revision-history bullets that cite related tasks would
    create false-positive multi-match collisions.
    """
    from workstate_handoff_mcp.plan_resolve import discover_plan_path_candidates

    other = workspace_repo / "docs/plans/0008-other.md"
    other.parent.mkdir(parents=True, exist_ok=True)
    other.write_text("# unrelated plan\n\nThis plan touches WORKSTATE-REF-38 in passing.\n")

    own = workspace_repo / "docs/plans/0007-real-plan.md"
    own.write_text("# real plan\n> - **Task ID**: `WORKSTATE-REF-38`\n")

    candidates = discover_plan_path_candidates("WORKSTATE-REF-38")

    assert candidates == ["docs/plans/0007-real-plan.md"], candidates
