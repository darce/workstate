"""Lifecycle integration tests for ``sync-task-plan-checklist`` (WORKSTATE-REF-64 implementation note)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
REPO_ROOT = PACKAGE_ROOT.parents[1]
LIFECYCLE_MK_SOURCE = PAYLOAD_ROOT / "Makefile.d" / "lifecycle.mk"
LIFECYCLE_PKG = PAYLOAD_ROOT / "scripts" / "workstate" / "lifecycle"
HANDOFF_SRC = (PACKAGE_ROOT.parent / "mcp-workstate-handoff" / "src").resolve()

if str(HANDOFF_SRC) not in sys.path:
    sys.path.insert(0, str(HANDOFF_SRC))

from workstate_handoff_mcp import api as mcp_server  # noqa: WORKSTATE-REF-402
from workstate_handoff_mcp.config import RuntimeConfig  # noqa: WORKSTATE-REF-402


pytestmark = pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")


def _parse(payload: str | dict) -> dict:
    return json.loads(payload) if isinstance(payload, str) else payload


def _write_handoff_cli_shim(path: Path) -> None:
    path.write_text(
        "#!/usr/bin/env bash\n"
        f"exec {sys.executable!s} -c 'from workstate_handoff_mcp.cli import main; main()' \"$@\"\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _consumer_repo(tmp_path: Path, branch: str = "feature/WORKSTATE-64") -> Path:
    repo = tmp_path / "consumer"
    repo.mkdir()
    (repo / "Makefile").write_text("include Makefile.d/*.mk\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        ".task-state/\nCURRENT_TASK.json\nDASHBOARD.txt\n"
        # test scaffolding — Makefile + symlinked lifecycle pkg are not
        # real consumer-repo content; ignore so slice-commit's
        # untracked-files check does not block the test.
        "Makefile\nMakefile.d/\nscripts/\n",
        encoding="utf-8",
    )
    mk_dir = repo / "Makefile.d"
    mk_dir.mkdir()
    (mk_dir / "lifecycle.mk").symlink_to(LIFECYCLE_MK_SOURCE)
    runner_parent = repo / "scripts" / "workstate"
    runner_parent.mkdir(parents=True)
    (runner_parent / "lifecycle").symlink_to(LIFECYCLE_PKG)
    # set_handoff_state validates target_branch by running
    # `git worktree list --porcelain`; the consumer repo must be a real
    # git worktree on the named branch for that lookup to succeed.
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q", "-b", branch, str(repo)], check=True, env=git_env)
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True, env=git_env)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "-m", "init"],
        check=True, env=git_env,
    )
    return repo


def _make_env(repo: Path, shim: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(shim)
    env["WORKSTATE_HANDOFF_STATE_DIR"] = str(repo / ".task-state")
    env["WORKSTATE_HANDOFF_CURRENT_TASK_PATH"] = str(repo / "CURRENT_TASK.json")
    env["WORKSTATE_HANDOFF_DASHBOARD_PATH"] = str(repo / "DASHBOARD.txt")
    env["PYTHONPATH"] = f"{HANDOFF_SRC}:{env.get('PYTHONPATH', '')}"
    env["LIFECYCLE_PYTHON"] = sys.executable
    return env


def _seed_handoff_evidence(repo: Path, task_ref: str) -> None:
    runtime = RuntimeConfig.for_workspace(
        repo,
        state_dir=repo / ".task-state",
        current_task_path=repo / "CURRENT_TASK.json",
        dashboard_path=repo / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    try:
        created = _parse(
            mcp_server.set_handoff_state(
                task_ref=task_ref,
                objective="Checklist sync integration",
                status="in_progress",
                target_branch="feature/WORKSTATE-64",
                target_worktree_path=str(repo),
                task_plan_path="docs/tasks/WORKSTATE-REF-64-plan.md",
            )
        )
        assert created["ok"] is True, created
        active = created.get("active") or created.get("data", {}).get("active")
        assert active is not None, created
        closed = _parse(
            mcp_server.close_slice(
                session="sync-plan-integration-close",
                decision="copilot_slice_complete_WORKSTATE_64_slice_2_lifecycle_wiring",
                rationale=(
                    "## Changes\n- Wired lifecycle checklist sync.\n\n"
                    "## Verification\n- Integration test exercises the make target.\n\n"
                    "## Schema / Contract Changes\n- Additive make target.\n\n"
                    "## Open Threads\n- None."
                ),
                task_ref=task_ref,
                expected_revision=active["revision"],
                changed_files=["packages/workstate-system/Makefile.d/lifecycle.mk"],
            )
        )
        assert closed["ok"] is True, closed
        test_result = _parse(
            mcp_server.record_event(
                event={
                    "event_kind": "test_result",
                    "session": "sync-plan-integration-test",
                    "task_ref": task_ref,
                    "command": "make check-agent-workflows",
                    "passed": True,
                    "exit_code": 0,
                    "result": "green",
                }
            )
        )
        assert test_result["ok"] is True, test_result
    finally:
        mcp_server.reset_runtime_config()


def test_make_sync_task_plan_checklist_uses_stored_plan_path(tmp_path: Path) -> None:
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    plan = repo / "docs" / "tasks" / "WORKSTATE-REF-64-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# WORKSTATE-REF-64 fixture\n\n"
        "### Checklist for implementation note: lifecycle wiring\n\n"
        "- [ ] Added `packages/workstate-system/Makefile.d/lifecycle.mk`.\n"
        "- [ ] `make check-agent-workflows` green.\n"
        "- [ ] Added `packages/workstate-system/docs/not-yet.md`.\n\n"
        "## Stretch Goals\n\n"
        "- [ ] Stretch item touching `packages/workstate-system/Makefile.d/lifecycle.mk`.\n",
        encoding="utf-8",
    )
    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)
    _seed_handoff_evidence(repo, task_ref)

    proc = subprocess.run(
        ["make", "sync-task-plan-checklist", f"TASK={task_ref}", "APPLY=1", "LIFECYCLE_ARGS=--quiet"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["command"] == "sync-task-plan-checklist"
    assert receipt["applied"] is True
    assert receipt["ticked"] == 2, receipt
    updated = plan.read_text(encoding="utf-8")
    assert "- [x] Added `packages/workstate-system/Makefile.d/lifecycle.mk`." in updated
    assert "- [x] `make check-agent-workflows` green." in updated
    assert "- [ ] Added `packages/workstate-system/docs/not-yet.md`." in updated
    assert "- [ ] Stretch item touching `packages/workstate-system/Makefile.d/lifecycle.mk`." in updated


def test_make_slice_commit_runs_checklist_sync_post_step(tmp_path: Path) -> None:
    """slice-commit's recipe-level post-step must invoke sync and merge
    its receipt under the parent's ``checklist_sync`` key (WORKSTATE-REF-64 implementation note)."""
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    plan = repo / "docs" / "tasks" / "WORKSTATE-REF-64-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# WORKSTATE-REF-64 fixture\n\n"
        "### Checklist for implementation note: lifecycle wiring\n\n"
        "- [ ] Added `packages/workstate-system/Makefile.d/lifecycle.mk`.\n"
        "- [ ] Touched `untouched.md`.\n",
        encoding="utf-8",
    )
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}
    feature = repo / "feature.py"
    feature.write_text("# initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feature.py", "docs/tasks/WORKSTATE-REF-64-plan.md"],
                   check=True, env=git_env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
                   check=True, env=git_env)
    feature.write_text("# updated for implementation note\n", encoding="utf-8")

    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)
    _seed_handoff_evidence(repo, task_ref)

    proc = subprocess.run(
        ["make", "slice-commit", f"TASK={task_ref}", "MSG=WORKSTATE-REF-64: implementation note wiring"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["command"] == "slice-commit"
    assert "checklist_sync" in receipt, receipt
    sync = receipt["checklist_sync"]
    assert sync["ok"] is True, sync
    assert sync["ticked"] == 1, sync
    updated = plan.read_text(encoding="utf-8")
    assert "- [x] Added `packages/workstate-system/Makefile.d/lifecycle.mk`." in updated
    assert "- [ ] Touched `untouched.md`." in updated


def test_make_slice_commit_includes_sync_plan_rewrites_in_the_same_commit(
    tmp_path: Path,
) -> None:
    """Regression for WORKSTATE-REF-64-BR-01.

    The recipe-level post-step (implementation note) wires the sync handler AFTER
    ``git commit`` runs; any box the sync flips ends up in the worktree
    as an uncommitted edit and is never part of the slice commit. The
    fix moves the sync BEFORE the ``git add``/``commit`` pair so the
    plan rewrite lands inside the same slice commit and the worktree
    is left clean.
    """
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    plan = repo / "docs" / "tasks" / "WORKSTATE-REF-64-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# WORKSTATE-REF-64 fixture\n\n"
        "### Checklist for implementation note: lifecycle wiring\n\n"
        "- [ ] Added `packages/workstate-system/Makefile.d/lifecycle.mk`.\n",
        encoding="utf-8",
    )
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}
    feature = repo / "feature.py"
    feature.write_text("# initial\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "feature.py", "docs/tasks/WORKSTATE-REF-64-plan.md"],
                   check=True, env=git_env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed"],
                   check=True, env=git_env)
    feature.write_text("# updated for implementation note\n", encoding="utf-8")

    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)
    _seed_handoff_evidence(repo, task_ref)

    proc = subprocess.run(
        ["make", "slice-commit", f"TASK={task_ref}", "MSG=WORKSTATE-REF-64: implementation note"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["checklist_sync"]["ticked"] == 1, receipt["checklist_sync"]

    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain=v1"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert status.stdout.strip() == "", (
        f"slice-commit left an uncommitted plan rewrite:\n{status.stdout}"
    )
    show = subprocess.run(
        ["git", "-C", str(repo), "show", "--stat", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "docs/tasks/WORKSTATE-REF-64-plan.md" in show.stdout, show.stdout
    final_plan = plan.read_text(encoding="utf-8")
    assert "- [x] Added `packages/workstate-system/Makefile.d/lifecycle.mk`." in final_plan


def test_sync_handler_reads_more_than_default_three_decisions(
    tmp_path: Path,
) -> None:
    """Regression for WORKSTATE-REF-64-BR-02.

    The default ``decisions_recent`` page size is 3. When a task has
    >3 slice closes the oldest evidence falls off the page and any plan
    item that only matches against an older slice's ``changed_files``
    never ticks. The handler must explicitly bump
    ``--top-n-decisions`` so the full slice-complete history is in the
    sync's evidence input.
    """
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    plan = repo / "docs" / "tasks" / "WORKSTATE-REF-64-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# WORKSTATE-REF-64 fixture\n\n"
        "### Checklist for implementation note: oldest slice\n\n"
        "- [ ] Touched `packages/example/slice_1_only.py`.\n",
        encoding="utf-8",
    )
    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)

    runtime = RuntimeConfig.for_workspace(
        repo,
        state_dir=repo / ".task-state",
        current_task_path=repo / "CURRENT_TASK.json",
        dashboard_path=repo / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    try:
        created = _parse(
            mcp_server.set_handoff_state(
                task_ref=task_ref,
                objective="Top-N regression",
                status="in_progress",
                target_branch="feature/WORKSTATE-64",
                target_worktree_path=str(repo),
                task_plan_path="docs/tasks/WORKSTATE-REF-64-plan.md",
            )
        )
        active = created.get("active") or created.get("data", {}).get("active")
        revision = active["revision"]
        slices = [
            (1, "packages/example/slice_1_only.py"),
            (2, "packages/example/slice_2_only.py"),
            (3, "packages/example/slice_3_only.py"),
            (4, "packages/example/slice_4_only.py"),
        ]
        for slice_num, path in slices:
            closed = _parse(
                mcp_server.close_slice(
                    session=f"sync-plan-top-n-{slice_num}",
                    decision=f"copilot_slice_complete_WORKSTATE_64_slice_{slice_num}_payload",
                    rationale=(
                        "## Changes\n- payload.\n\n"
                        "## Verification\n- payload.\n\n"
                        "## Schema / Contract Changes\n- none.\n\n"
                        "## Open Threads\n- none."
                    ),
                    task_ref=task_ref,
                    expected_revision=revision,
                    changed_files=[path],
                )
            )
            assert closed["ok"] is True, closed
            revision = closed["data"]["task_revision"]
    finally:
        mcp_server.reset_runtime_config()

    proc = subprocess.run(
        ["make", "sync-task-plan-checklist", f"TASK={task_ref}", "APPLY=1",
         "LIFECYCLE_ARGS=--quiet"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["ticked"] == 1, receipt
    final_plan = plan.read_text(encoding="utf-8")
    assert "- [x] Touched `packages/example/slice_1_only.py`." in final_plan


def test_sync_writes_sidecar_and_dashboard_surfaces_warning(tmp_path: Path) -> None:
    """A non-ok sync result lands in ``.task-state/checklist_sync.json`` and
    surfaces as a ``checklist_sync_warning`` line in the rendered dashboard
    (WORKSTATE-REF-64 implementation note)."""
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)
    # _seed_handoff_evidence stores ``task_plan_path =
    # docs/tasks/WORKSTATE-REF-64-plan.md`` but the test deliberately omits the
    # file on disk so the sync resolves to ``plan_not_found`` — the
    # canonical hard-failure case the dashboard warning is meant to
    # surface.
    _seed_handoff_evidence(repo, task_ref)

    proc = subprocess.run(
        ["make", "sync-task-plan-checklist", f"TASK={task_ref}", "APPLY=1",
         "LIFECYCLE_ARGS=--quiet"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )
    # plan_not_found returns rc=2 from the sync handler, but the sidecar
    # must still be written so the dashboard renderer can surface it.
    assert proc.returncode == 2, proc.stdout
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "plan_not_found"

    sidecar = repo / ".task-state" / "checklist_sync.json"
    assert sidecar.exists(), f"sidecar not written; stdout={proc.stdout}"
    sidecar_data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert task_ref in sidecar_data, sidecar_data
    entry = sidecar_data[task_ref]
    assert entry["ok"] is False, entry
    assert "plan_not_found" in (entry.get("warning") or entry.get("error") or "")

    runtime = RuntimeConfig.for_workspace(
        repo,
        state_dir=repo / ".task-state",
        current_task_path=repo / "CURRENT_TASK.json",
        dashboard_path=repo / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    try:
        render_result = _parse(mcp_server.render_handoff(kind="dashboard"))
    finally:
        mcp_server.reset_runtime_config()
    assert render_result["ok"] is True, render_result

    dashboard_text = (repo / "DASHBOARD.txt").read_text(encoding="utf-8")
    assert "checklist_sync_warning" in dashboard_text, (
        f"warning not surfaced:\n{dashboard_text}"
    )
    assert task_ref in dashboard_text


def test_make_task_finish_checklist_sync_is_verify_only(tmp_path: Path) -> None:
    """task-finish's post-merge checklist sweep is VERIFY-ONLY: it must NOT
    write ticks into the plan (it runs post-merge in a worktree teardown
    discards, and plan docs reach the integration branch only via the merge).
    A box whose evidence is recorded but is still unticked is surfaced as a
    ``plan_checklist_drift`` warning, and the plan file is left untouched.
    The persisting sweep is ``make finalize-plan`` (run pre-merge)."""
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    plan = repo / "docs" / "tasks" / "WORKSTATE-REF-64-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# WORKSTATE-REF-64 fixture\n\n"
        "### Checklist for implementation note: lifecycle wiring\n\n"
        "- [ ] Added `packages/workstate-system/Makefile.d/lifecycle.mk`.\n"
        "- [ ] Touched `untouched.md`.\n",
        encoding="utf-8",
    )

    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)
    _seed_handoff_evidence(repo, task_ref)

    proc = subprocess.run(
        ["make", "task-finish", f"TASK={task_ref}"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["command"] == "task-finish"
    sync = receipt["checklist_sync"]
    assert sync["ok"] is True, sync
    # Dry-run: ``ticked`` still reports what WOULD flip, but nothing is written.
    assert sync["applied"] is False, sync
    assert sync["ticked"] == 1, sync
    updated = plan.read_text(encoding="utf-8")
    assert "- [ ] Added `packages/workstate-system/Makefile.d/lifecycle.mk`." in updated, updated
    assert any("plan_checklist_drift" in w for w in receipt.get("warnings", [])), receipt


def test_make_finalize_plan_commits_ticks_to_feature_branch(tmp_path: Path) -> None:
    """``make finalize-plan`` runs the sweep with --apply AND commits the
    ticks (only the plan file) onto the branch, so they ride into main via the
    subsequent merge. This is the persisting counterpart to task-finish's
    verify-only post-merge sweep."""
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    plan = repo / "docs" / "tasks" / "WORKSTATE-REF-64-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# WORKSTATE-REF-64 fixture\n\n"
        "### Checklist for implementation note: lifecycle wiring\n\n"
        "- [ ] Added `packages/workstate-system/Makefile.d/lifecycle.mk`.\n"
        "- [ ] Touched `untouched.md`.\n",
        encoding="utf-8",
    )
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}
    # Plan is already tracked (committed by an earlier slice); finalize-plan
    # adds the final tick on top.
    subprocess.run(["git", "-C", str(repo), "add", "docs/tasks/WORKSTATE-REF-64-plan.md"],
                   check=True, env=git_env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed plan"],
                   check=True, env=git_env)

    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)
    _seed_handoff_evidence(repo, task_ref)

    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    proc = subprocess.run(
        ["make", "finalize-plan", f"TASK={task_ref}"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["command"] == "finalize-plan"
    assert receipt["ticked"] == 1, receipt
    assert receipt["commit_status"] == "committed", receipt
    assert not receipt["warnings"], receipt

    # A new commit landed and the plan tick is IN it (persisted on the branch).
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after != head_before, "finalize-plan must create a commit"
    committed_plan = subprocess.run(
        ["git", "-C", str(repo), "show", "HEAD:docs/tasks/WORKSTATE-REF-64-plan.md"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "- [x] Added `packages/workstate-system/Makefile.d/lifecycle.mk`." in committed_plan
    assert "- [ ] Touched `untouched.md`." in committed_plan
    # Targeted commit: working tree is clean (only the plan was committed).
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert status == "", f"finalize-plan left the worktree dirty:\n{status}"


def test_make_finalize_plan_noop_when_nothing_to_tick(tmp_path: Path) -> None:
    """With no matching evidence the sweep ticks nothing, so finalize-plan
    creates no commit and reports ``nothing_to_tick``."""
    repo = _consumer_repo(tmp_path)
    task_ref = "WORKSTATE-REF-64"
    plan = repo / "docs" / "tasks" / "WORKSTATE-REF-64-plan.md"
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# WORKSTATE-REF-64 fixture\n\n"
        "### Checklist for implementation note: no evidence\n\n"
        "- [ ] Touched `packages/example/never_touched.py`.\n",
        encoding="utf-8",
    )
    git_env = {**os.environ, "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "-C", str(repo), "add", "docs/tasks/WORKSTATE-REF-64-plan.md"],
                   check=True, env=git_env)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "seed plan"],
                   check=True, env=git_env)
    head_before = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()

    shim = tmp_path / "mcp-workstate-handoff"
    _write_handoff_cli_shim(shim)
    _seed_handoff_evidence(repo, task_ref)

    proc = subprocess.run(
        ["make", "finalize-plan", f"TASK={task_ref}"],
        cwd=repo,
        env=_make_env(repo, shim),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True, receipt
    assert receipt["ticked"] == 0, receipt
    assert receipt["commit_status"] == "nothing_to_tick", receipt
    head_after = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert head_after == head_before, "finalize-plan must not commit when nothing ticks"