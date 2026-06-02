"""Integration tests for ``Makefile.d/plans.mk`` (implementation note implementation note).

Drives ``make plan-show TASK=<ref>`` against a temp git repo with two
branches and a seeded handoff DB. Asserts the recipe:

- prints what ``git show <branch>:<rel_path>`` would print, byte-for-byte;
- does not change the operator's HEAD ref (zero-checkout invariant);
- exits non-zero with an actionable message when the plan path is
  unset or the plan does not exist on the target branch.

Skipped when ``make`` is not installed (CI image without build-essential).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
HANDOFF_SRC = (
    PACKAGE_ROOT.parent / "mcp-workstate-handoff" / "src"
).resolve()
PLANS_MK_SOURCE = (PACKAGE_ROOT / "Makefile.d" / "plans.mk").resolve()


def _git(*args: str, cwd: Path) -> str:
    return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()


def _have_make() -> bool:
    return shutil.which("make") is not None


pytestmark = pytest.mark.skipif(not _have_make(), reason="make not on PATH")


@pytest.fixture()
def consumer_repo(tmp_path: Path) -> Path:
    """A git repo wired up like a bootstrapped consumer.

    - root ``Makefile`` with ``include Makefile.d/*.mk``
    - ``Makefile.d/plans.mk`` symlinked from this package's source so the
      test always exercises the live recipe
    - ``.task-state/`` for the handoff DB
    - ``main`` branch with a seed commit so HEAD is resolvable
    """
    _git("init", "--initial-branch=main", cwd=tmp_path)
    _git("config", "user.email", "plans-mk@test", cwd=tmp_path)
    _git("config", "user.name", "Plans Mk Test", cwd=tmp_path)
    (tmp_path / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-m", "seed", cwd=tmp_path)

    (tmp_path / "Makefile").write_text("include Makefile.d/*.mk\n")
    mk_dir = tmp_path / "Makefile.d"
    mk_dir.mkdir()
    (mk_dir / "plans.mk").symlink_to(PLANS_MK_SOURCE)

    (tmp_path / ".task-state").mkdir()
    return tmp_path


def test_plan_accept_typed_make_vars_expand_to_flags(consumer_repo: Path) -> None:
    """implementation note B3: the discriminating plan-accept flags are first-class make
    variables (REVIEW_TASK_REF / LOCAL / PLAN / SOURCE_BRANCH), so operators no
    longer hand-assemble a quote-fragile LIFECYCLE_ARGS string. ``make -n``
    expands them into the lifecycle CLI invocation."""
    out = subprocess.check_output(
        [
            "make",
            "-n",
            "plan-accept",
            "TASK=WS-X",
            "REVIEW_TASK_REF=WORKSTATE-REF-Y",
            "LOCAL=1",
            "PLAN=docs/plans/0016.md",
            "SOURCE_BRANCH=main",
        ],
        cwd=consumer_repo,
        text=True,
    )
    assert "--review-task-ref WORKSTATE-REF-Y" in out, out
    assert "--local" in out, out
    assert "--plan docs/plans/0016.md" in out, out
    assert "--source-branch main" in out, out


def _commit_plan_on_branch(repo: Path, branch: str, rel_path: str, body: str) -> None:
    starting = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=repo)
    _git("checkout", "-b", branch, cwd=repo)
    plan_abs = repo / rel_path
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text(body)
    _git("add", rel_path, cwd=repo)
    _git("commit", "-m", f"add {rel_path}", cwd=repo)
    _git("checkout", starting, cwd=repo)


def _seed_handoff(
    repo: Path,
    *,
    task_ref: str,
    branch: str | None,
    path: str | None,
    worktree_path: str | None = None,
) -> None:
    """Configure runtime + write a handoff row using the package API."""
    import argparse

    from workstate_handoff_mcp import api as mcp_server
    from workstate_handoff_mcp.config import RuntimeConfig

    state_dir = repo / ".task-state"
    runtime = RuntimeConfig.for_workspace(
        repo,
        state_dir=state_dir,
        current_task_path=repo / "CURRENT_TASK.json",
        dashboard_path=repo / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    kwargs: dict = {
        "task_ref": task_ref,
        "objective": "plan-show test",
        "status": "in_progress",
    }
    if branch is not None:
        kwargs["target_branch"] = branch
    if path is not None:
        kwargs["task_plan_path"] = path
    if worktree_path is not None:
        kwargs["target_worktree_path"] = worktree_path
    mcp_server.set_handoff_state(**kwargs)
    mcp_server.reset_runtime_config()


def _provision_worktree(repo: Path, branch: str) -> Path:
    """Check out an existing branch into a linked worktree.

    The WORKSTATE-REF-52 write-context guard resolves a row's worktree by matching
    its ``target_branch`` against ``git worktree list --porcelain``.
    ``make plan-register`` performs a guarded ``set_handoff_state`` update,
    so the feature branch the row points at must be checked out in some
    worktree or the write raises ``WorktreeNotFoundError``.
    ``_commit_plan_on_branch`` leaves the branch as a bare ref (the primary
    checkout is restored to ``main``), so add a sibling linked worktree on
    it — mirroring the operator's real ``main`` primary + linked feature
    worktree layout. The sibling lives under pytest's tmp root and is
    discarded with the rest of the temp tree.
    """
    safe = branch.replace("/", "-")
    wt_path = repo.parent / f"{repo.name}-wt-{safe}"
    _git("worktree", "add", str(wt_path), branch, cwd=repo)
    return wt_path


def _make_env(repo: Path) -> dict[str, str]:
    """Override the launcher to call the package directly.

    The Makefile resolves ``$(WORKSTATE_HANDOFF_PLAN_CLI) show --task ...``;
    overriding with a plain ``python -m`` invocation hits the same code
    path the uvx launcher would route through, without depending on
    a real uvx install. The end-to-end uvx rehearsal test in
    workstate-bootstrap covers the launcher itself.
    """
    env = os.environ.copy()
    env["WORKSTATE_HANDOFF_PLAN_CLI"] = "python -m workstate_handoff_mcp.plan_cli"
    env["WORKSTATE_HANDOFF_WORKSPACE_ROOT"] = str(repo)
    env["PYTHONPATH"] = f"{HANDOFF_SRC}:{env.get('PYTHONPATH', '')}"
    return env


def _make_env_no_workspace_root(repo: Path) -> dict[str, str]:
    """Like ``_make_env`` but with ``WORKSTATE_HANDOFF_WORKSPACE_ROOT`` removed.

    Mirrors what a coordinator gets in a fresh shell on ``main``: they
    have not exported the preflight env var, so the only signal the
    plan-targets surface has about where the workspace lives is whatever
    the Make recipe forwards on the CLI argv. WORKSTATE-REF-69 implementation note fixes
    that by forwarding ``--workspace-root $(CURDIR)`` from every recipe.
    """
    env = _make_env(repo)
    env.pop("WORKSTATE_HANDOFF_WORKSPACE_ROOT", None)
    return env


def test_plan_show_prints_plan_via_git_show(consumer_repo: Path) -> None:
    """``make plan-show`` prints the plan's bytes from the target branch
    via ``git show``, without changing the operator's HEAD ref."""
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    body = "# demo plan\n\nfirst section.\n"
    _commit_plan_on_branch(consumer_repo, branch, rel, body)
    _seed_handoff(consumer_repo, task_ref="WORKSTATE-REF-99", branch=branch, path=rel)

    head_before = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=consumer_repo)
    expected = subprocess.check_output(
        ["git", "show", f"{branch}:{rel}"], cwd=consumer_repo, text=True
    )
    proc = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected, (proc.stdout, expected)
    head_after = _git("rev-parse", "--abbrev-ref", "HEAD", cwd=consumer_repo)
    assert head_before == head_after == "main"


def test_plan_show_errors_when_task_plan_path_unset(consumer_repo: Path) -> None:
    """Missing ``task_plan_path`` yields a non-zero exit + an actionable
    pointer at how to set it (``set_handoff_state(task_plan_path=...)``)."""
    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-99",
        branch="feature/WORKSTATE-99",
        path=None,
    )

    proc = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "task_plan_path" in proc.stderr, proc.stderr


def test_plan_show_errors_when_plan_not_committed_on_branch(
    consumer_repo: Path,
) -> None:
    """``exists_on_branch=False`` exits non-zero naming the missing
    branch and path so the operator knows what is wrong."""
    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-99",
        branch="feature/never-pushed",
        path="docs/plans/never.md",
    )

    proc = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "feature/never-pushed" in proc.stderr
    assert "docs/plans/never.md" in proc.stderr


def _record_editor(repo: Path) -> tuple[Path, Path]:
    """Write a recording shim that captures EDITOR argv to a file.

    Returns (shim_path, log_path). Tests read log_path after invoking
    `make plan-edit` to verify the editor was called with the expected
    absolute path. Avoids opening a real editor inside CI.
    """
    log = repo / "editor.log"
    shim = repo / "fake_editor.sh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {log!s}\n"
    )
    shim.chmod(0o755)
    return shim, log


def test_plan_edit_opens_existing_linked_worktree(consumer_repo: Path) -> None:
    """``make plan-edit`` invokes ``$EDITOR`` against the absolute path
    inside ``target_worktree_path``, not against a temp checkout."""
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    body = "# demo plan\n"
    plan_abs = consumer_repo / rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text(body)

    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-99",
        branch=branch,
        path=rel,
        worktree_path=str(consumer_repo),
    )

    shim, log = _record_editor(consumer_repo)
    env = _make_env(consumer_repo)
    env["EDITOR"] = str(shim)

    proc = subprocess.run(
        ["make", "plan-edit", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert log.exists(), f"editor shim never ran; stderr={proc.stderr!r}"
    recorded = log.read_text().strip().splitlines()
    assert recorded == [str(plan_abs)], (recorded, plan_abs)


def test_plan_edit_supports_multiword_editor_command(consumer_repo: Path) -> None:
    """``$EDITOR`` may be a shell-style command like ``code --wait``.

    Regression for BR-WORKSTATE38-S3-01: invoking ``[editor, abs_path]``
    treated the whole string as one argv token, so multi-word editors
    failed with ``executable not found``. The fix is to ``shlex.split``
    the env var before appending the plan path.
    """
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    plan_abs = consumer_repo / rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# demo\n")

    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-99",
        branch=branch,
        path=rel,
        worktree_path=str(consumer_repo),
    )

    shim, log = _record_editor(consumer_repo)
    env = _make_env(consumer_repo)
    env["EDITOR"] = f"{shim} --wait --diff"

    proc = subprocess.run(
        ["make", "plan-edit", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=env,
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    recorded = log.read_text().strip().splitlines()
    assert recorded == ["--wait", "--diff", str(plan_abs)], recorded


def test_plans_list_iterates_active_tasks(consumer_repo: Path) -> None:
    """``make plans-list`` prints one block per active task with a
    header line carrying ``task_ref``, ``branch``, and ``path``.
    Ordering follows ``list_active_task_locations`` (updated_at desc,
    task_ref asc tiebreaker)."""
    seeded = [
        ("WORKSTATE-REF-101", "feature/WORKSTATE-101", "docs/plans/0101.md"),
        ("WORKSTATE-REF-102", "feature/WORKSTATE-102", "docs/plans/0102.md"),
        ("WORKSTATE-REF-103", "feature/WORKSTATE-103", "docs/plans/0103.md"),
    ]
    for ref, branch, path in seeded:
        _seed_handoff(
            consumer_repo,
            task_ref=ref,
            branch=branch,
            path=path,
        )

    proc = subprocess.run(
        ["make", "plans-list"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    headers = [line for line in out.splitlines() if line.startswith("===")]
    assert len(headers) == 3, (headers, out)
    for ref, branch, path in seeded:
        assert any(ref in h and branch in h and path in h for h in headers), (ref, headers)


def test_plans_list_warns_for_unset_path_rows(consumer_repo: Path) -> None:
    """An active task with ``task_plan_path=None`` produces exactly one
    warning line naming the missing field; subsequent tasks still
    render."""
    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-NOTASKTH",
        branch="feature/WORKSTATE-nopath",
        path=None,
    )
    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-WITH",
        branch="feature/WORKSTATE-with",
        path="docs/plans/0202.md",
    )

    proc = subprocess.run(
        ["make", "plans-list"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    warnings = [line for line in out.splitlines() if line.startswith("WARNING:")]
    assert len(warnings) == 1, (warnings, out)
    assert "task_plan_path" in warnings[0]
    assert "WORKSTATE-REF-NOTASKTH" in warnings[0]
    headers = [line for line in out.splitlines() if line.startswith("===")]
    assert any("WORKSTATE-REF-WITH" in h and "docs/plans/0202.md" in h for h in headers), headers


def test_plans_list_no_include_unset_path_suppresses_unset_rows(
    consumer_repo: Path,
) -> None:
    """``plan_cli list --no-include-unset-path`` omits rows whose
    ``task_plan_path`` is unset, so callers that only care about
    registered plans can iterate without filtering warnings.

    Regression for the BooleanOptionalAction fix: the original
    ``action="store_true", default=True`` form made the flag a no-op,
    so direct CLI callers could not opt out of unset rows.
    """
    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-NOTASKTH",
        branch="feature/WORKSTATE-nopath",
        path=None,
    )
    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-WITH",
        branch="feature/WORKSTATE-with",
        path="docs/plans/0202.md",
    )

    proc = subprocess.run(
        [
            "python", "-m", "workstate_handoff_mcp.plan_cli",
            "list", "--no-include-unset-path",
        ],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    headers = [line for line in out.splitlines() if line.startswith("===")]
    warnings = [line for line in out.splitlines() if line.startswith("WARNING:")]
    assert warnings == [], (warnings, out)
    assert len(headers) == 1, (headers, out)
    assert "WORKSTATE-REF-WITH" in headers[0], headers
    assert "WORKSTATE-REF-NOTASKTH" not in out, out


def test_plan_register_explicit_round_trips_via_show(consumer_repo: Path) -> None:
    """``make plan-register TASK=<ref> PLAN=<path>`` registers the path on
    the active row and the registered path round-trips through
    ``make plan-show`` immediately afterwards.

    implementation note implementation note vertical: the operator's `make task-start` shells
    out to `make plan-register`; the package owns the registration
    logic. Test asserts the explicit-PLAN path end-to-end so consumer
    `task-start.sh` can rely on a single CLI surface.
    """
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    body = "# demo plan\n"
    _commit_plan_on_branch(consumer_repo, branch, rel, body)
    # Seed the active row WITHOUT a plan path so the register call has
    # something to set. The branch is required for plan-show to work
    # against the committed plan after register lands.
    _seed_handoff(consumer_repo, task_ref="WORKSTATE-REF-99", branch=branch, path=None)
    _provision_worktree(consumer_repo, branch)

    register = subprocess.run(
        ["make", "plan-register", "TASK=WORKSTATE-REF-99", f"PLAN={rel}"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )
    assert register.returncode == 0, register.stderr

    expected = subprocess.check_output(
        ["git", "show", f"{branch}:{rel}"], cwd=consumer_repo, text=True
    )
    show = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0, show.stderr
    assert show.stdout == expected, (show.stdout, expected)


def test_plan_register_globs_unique_match_when_plan_omitted(
    consumer_repo: Path,
) -> None:
    """When ``PLAN=`` is omitted, ``plan-register`` globs
    ``docs/**/*<task-id>*.md`` and registers the unique match. The
    registered path must round-trip through ``plan-show``.
    """
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-WORKSTATE-99-demo-plan.md"
    _commit_plan_on_branch(consumer_repo, branch, rel, "# demo plan\n")
    # The glob runs against the workspace root (consumer_repo), so the
    # plan file must exist on disk there. The branch checkout already
    # restored main; reseed the file in the working tree for discovery.
    plan_abs = consumer_repo / rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# demo plan\n")
    _seed_handoff(consumer_repo, task_ref="WORKSTATE-REF-99", branch=branch, path=None)
    _provision_worktree(consumer_repo, branch)

    register = subprocess.run(
        ["make", "plan-register", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert register.returncode == 0, register.stderr
    assert rel in register.stdout, register.stdout

    show = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0, show.stderr


def test_plan_register_discovers_numbered_plan_via_frontmatter(
    consumer_repo: Path,
) -> None:
    """Regression for BR-WORKSTATE38-S5-01.

    Real plans live at ``docs/plans/NNNN-<slug>.md`` whose filename
    does not embed the task ref. ``make plan-register TASK=<ref>``
    must still find them via the ``Task ID:`` frontmatter declaration
    instead of failing with zero matches.
    """
    branch = "feature/WORKSTATE-99-real"
    rel = "docs/plans/0099-real-numbered-plan.md"
    body = "# implementation note\n\n> - **Task ID**: `WORKSTATE-REF-99`\n"
    _commit_plan_on_branch(consumer_repo, branch, rel, body)
    plan_abs = consumer_repo / rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text(body)
    _seed_handoff(consumer_repo, task_ref="WORKSTATE-REF-99", branch=branch, path=None)
    _provision_worktree(consumer_repo, branch)

    register = subprocess.run(
        ["make", "plan-register", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert register.returncode == 0, register.stderr
    assert rel in register.stdout, register.stdout

    show = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )
    assert show.returncode == 0, show.stderr
    assert show.stdout == body, (show.stdout, body)


def test_plan_register_fails_on_multi_match_with_disambiguation(
    consumer_repo: Path,
) -> None:
    """Two glob candidates → exit 2 + stderr lists both so the operator
    can rerun with ``PLAN=`` explicitly. DoD: no silent fallback to the
    unset state."""
    for rel in (
        "docs/plans/0099-WORKSTATE-99-first.md",
        "docs/notes/WORKSTATE-99-scratch.md",
    ):
        plan_abs = consumer_repo / rel
        plan_abs.parent.mkdir(parents=True, exist_ok=True)
        plan_abs.write_text("# stub\n")
    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-99",
        branch="feature/WORKSTATE-99",
        path=None,
    )

    proc = subprocess.run(
        ["make", "plan-register", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert "multiple plan candidates" in proc.stderr, proc.stderr
    assert "0099-WORKSTATE-99-first.md" in proc.stderr
    assert "WORKSTATE-99-scratch.md" in proc.stderr


def test_plan_edit_errors_when_target_worktree_missing(
    consumer_repo: Path, tmp_path: Path
) -> None:
    """If ``target_worktree_path`` does not exist on disk, ``plan-edit``
    exits non-zero with a message naming the missing path and pointing
    at ``make task-start``."""
    missing = tmp_path / "never-created-worktree"
    assert not missing.exists()

    _seed_handoff(
        consumer_repo,
        task_ref="WORKSTATE-REF-99",
        branch="feature/WORKSTATE-99",
        path="docs/plans/0099-demo-plan.md",
        worktree_path=str(missing),
    )

    proc = subprocess.run(
        ["make", "plan-edit", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode != 0
    assert str(missing) in proc.stderr, proc.stderr
    assert "task-start" in proc.stderr, proc.stderr


GIT_PLAN_CAT_SCRIPT = (PACKAGE_ROOT / "scripts" / "workstate" / "git-plan-cat.sh").resolve()


def test_git_plan_cat_alias_resolves_via_resolver(consumer_repo: Path) -> None:
    """``git-plan-cat.sh <task>`` produces the same bytes as ``make plan-show``.

    The optional ``git plan-cat`` alias (implementation note implementation note) is a thin
    shell wrapper around the same plan-cli ``show`` subcommand the
    Makefile drives, so its sole acceptance criterion is byte-for-byte
    parity with ``git show <branch>:<path>`` (and therefore with
    ``make plan-show``). Driving the script directly keeps the test
    independent of whether the operator has actually installed the
    ``[alias]`` entry in their ``.gitconfig``.
    """
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    body = "# demo plan\n\nfirst section.\n"
    _commit_plan_on_branch(consumer_repo, branch, rel, body)
    _seed_handoff(consumer_repo, task_ref="WORKSTATE-REF-99", branch=branch, path=rel)

    expected = subprocess.check_output(
        ["git", "show", f"{branch}:{rel}"], cwd=consumer_repo, text=True
    )

    proc = subprocess.run(
        [str(GIT_PLAN_CAT_SCRIPT), "WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected, (proc.stdout, expected)


def test_git_plan_cat_alias_defaults_to_active_task(consumer_repo: Path) -> None:
    """Invoked with zero args, the script resolves the active task —
    matching ``make plan-show`` (no ``TASK=`` argument) so the
    ``[alias] plan-cat = !git-plan-cat.sh`` form works without
    arguments when only one task is active."""
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    body = "# active-only invocation\n"
    _commit_plan_on_branch(consumer_repo, branch, rel, body)
    _seed_handoff(consumer_repo, task_ref="WORKSTATE-REF-99", branch=branch, path=rel)

    expected = subprocess.check_output(
        ["git", "show", f"{branch}:{rel}"], cwd=consumer_repo, text=True
    )

    proc = subprocess.run(
        [str(GIT_PLAN_CAT_SCRIPT)],
        cwd=consumer_repo,
        env=_make_env(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected, (proc.stdout, expected)


# ---------------------------------------------------------------------------
# WORKSTATE-REF-69 implementation note — `Makefile.d/plans.mk` forwards ``--workspace-root $(CURDIR)``
# ---------------------------------------------------------------------------


def test_plan_show_works_with_no_env_preflight(consumer_repo: Path) -> None:
    """``make plan-show`` succeeds without the operator exporting
    ``WORKSTATE_HANDOFF_WORKSPACE_ROOT``.

    Before WORKSTATE-REF-69 implementation note, the recipe invoked the CLI bare, so the
    resolver fell back to a process-relative default that may not
    match the actual repo root. The fix forwards
    ``--workspace-root $(CURDIR)`` from every recipe so a fresh shell on
    ``main`` Just Works.
    """
    branch = "feature/WORKSTATE-99-demo"
    rel = "docs/plans/0099-demo-plan.md"
    body = "# demo plan\n\nfirst section.\n"
    _commit_plan_on_branch(consumer_repo, branch, rel, body)
    _seed_handoff(consumer_repo, task_ref="WORKSTATE-REF-99", branch=branch, path=rel)

    expected = subprocess.check_output(
        ["git", "show", f"{branch}:{rel}"], cwd=consumer_repo, text=True
    )
    proc = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=_make_env_no_workspace_root(consumer_repo),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == expected, (proc.stdout, expected)


def _record_plan_cli_argv(repo: Path) -> tuple[Path, Path]:
    """Write a recording shim that captures ``plan_cli`` argv to a file.

    Returns (shim_path, log_path). The shim records every argument it
    received (one per line) and exits zero so the Make recipe completes
    cleanly. Tests substitute the shim for the real plan CLI via the
    ``WORKSTATE_HANDOFF_PLAN_CLI`` env var, then read the log to verify the
    Make-level forwarding of ``--workspace-root`` and ``PLAN_MODE``.
    """
    log = repo / "plan_cli_argv.log"
    shim = repo / "fake_plan_cli.sh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$@\" > {log!s}\n"
        "exit 0\n"
    )
    shim.chmod(0o755)
    return shim, log


def test_plan_show_forwards_workspace_root_to_cli(consumer_repo: Path) -> None:
    """Make-level forwarding gate: the ``plan-show`` recipe must include
    ``--workspace-root <CURDIR>`` in the argv it hands to the plan CLI,
    regardless of whether ``WORKSTATE_HANDOFF_WORKSPACE_ROOT`` is set."""
    shim, log = _record_plan_cli_argv(consumer_repo)
    env = _make_env_no_workspace_root(consumer_repo)
    env["WORKSTATE_HANDOFF_PLAN_CLI"] = str(shim)

    proc = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99"],
        cwd=consumer_repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    recorded = log.read_text().splitlines()
    assert "--workspace-root" in recorded, recorded
    idx = recorded.index("--workspace-root")
    assert recorded[idx + 1] == str(consumer_repo), recorded


def test_plan_show_forwards_plan_mode(consumer_repo: Path) -> None:
    """``make plan-show PLAN_MODE=baseline`` and ``PLAN_MODE=working-copy``
    must append the matching ``--<mode>`` flag to the CLI argv so the
    operator can pick which snapshot to read through the Make facade.

    implementation note owns the CLI-side handling of ``--baseline``/``--working-copy``;
    implementation note only proves that the Make recipe forwards the choice. Raw
    ``PLAN_ARGS`` passthrough must also reach the CLI verbatim so
    operators can drop down to plan_cli flags without waiting for a
    Make-level alias.
    """
    shim, log = _record_plan_cli_argv(consumer_repo)
    env = _make_env(consumer_repo)
    env["WORKSTATE_HANDOFF_PLAN_CLI"] = str(shim)

    for mode in ("baseline", "working-copy", "auto"):
        proc = subprocess.run(
            ["make", "plan-show", "TASK=WORKSTATE-REF-99", f"PLAN_MODE={mode}"],
            cwd=consumer_repo,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        recorded = log.read_text().splitlines()
        assert f"--{mode}" in recorded, (mode, recorded)

    # Raw PLAN_ARGS passes through verbatim so plan_cli flags landing in
    # implementation note+ are reachable without a per-flag Make alias.
    proc = subprocess.run(
        ["make", "plan-show", "TASK=WORKSTATE-REF-99", "PLAN_ARGS=--working-copy"],
        cwd=consumer_repo,
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    recorded = log.read_text().splitlines()
    assert "--working-copy" in recorded, recorded
