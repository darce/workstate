"""Required-argv contract for the review/close shell-out wrappers
(BR-WORKSTATE40-S1-02).

The implementation note wrappers shipped with only a static argv prefix and
passthrough args, so the underlying ``mcp-workstate-handoff review-runs
record`` invocation was missing every required record field
(``--workspace-root``, ``--review-run-id``, ``--session``,
``--subject-path``, ``--task-ref``). This module pins the FULL
required argv per command so a partial wrapper regression cannot
silently re-ship a non-executable runner.
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


def _write_fake_cli(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", "-b", "feature/test-branch"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit",
         "--allow-empty", "-m", "init", "-q"],
        cwd=tmp_path,
        check=True,
    )
    # Synthesize a CURRENT_TASK.json so the wrapper can derive --task-ref.
    # schema_version: 1 mirrors the live writer; the WORKSTATE-REF-54 implementation note.5
    # compat reader is strict about the field.
    (tmp_path / "CURRENT_TASK.json").write_text(json.dumps({
        "schema_version": 1,
        "active": {
            "task_ref": "TEST-42",
            "target_branch": "feature/test-branch",
        },
    }))
    return tmp_path


@pytest.fixture
def fake_cli_dir(tmp_path: Path) -> Path:
    return tmp_path / "fake-cli"


def _make_logging_fake_cli(
    fake_cli_dir: Path,
    *,
    workspace_summary: dict | None = None,
) -> tuple[Path, Path]:
    """Build a fake-CLI that logs argv and answers ``render-handoff``.

    WORKSTATE-REF-54-FU implementation note: the shell-out wrapper's CURRENT_TASK fallback
    now goes through ``render-handoff --kind=current_task --no-write``,
    so the fake CLI must surface a workspace-summary envelope for that
    subcommand. Non-render-handoff invocations are logged for argv
    assertions (the wrapper-under-test is the second call).
    """
    fake_cli_dir.mkdir(parents=True)
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    argv_log = fake_cli_dir / "argv.log"
    if workspace_summary is None:
        workspace_summary = {
            "schema_version": 2,
            "shape": "single",
            "task_ref": "TEST-42",
            "active": {
                "task_ref": "TEST-42",
                "status": "in_progress",
                "target_branch": "feature/test-branch",
            },
            "tasks": [],
        }
    envelope = {
        "schema_version": 2,
        "tool": "render_handoff",
        "ok": True,
        "data": {
            "task_ref": workspace_summary.get("task_ref"),
            "path": "/tmp/CURRENT_TASK.json",
            "written": False,
            "current_task_json": json.dumps(workspace_summary),
        },
    }
    body = (
        "#!/usr/bin/env bash\n"
        'if [[ "$*" == *"render-handoff"* ]]; then\n'
        f"  cat <<'ENVELOPE_EOF'\n{json.dumps(envelope)}\nENVELOPE_EOF\n"
        "  exit 0\n"
        "fi\n"
        f'printf "%s\\n" "$@" > {argv_log}\n'
        "echo ok\n"
        "exit 0\n"
    )
    _write_fake_cli(fake_cli, body)
    return fake_cli, argv_log


def _run_lifecycle(
    cwd: Path, fake_cli: Path, subcommand: str, *extra: str
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), subcommand, *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _argv_value(logged_argv: list[str], flag: str) -> str | None:
    """Return the value following ``flag`` in argv, or None if absent."""
    try:
        idx = logged_argv.index(flag)
    except ValueError:
        return None
    if idx + 1 >= len(logged_argv):
        return None
    return logged_argv[idx + 1]


def test_review_run_passes_full_required_argv(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """`review-run` must invoke the CLI with every record-required field
    populated. Defaults are derived from CURRENT_TASK.json + git HEAD."""
    fake_cli, argv_log = _make_logging_fake_cli(fake_cli_dir)
    proc = _run_lifecycle(git_repo, fake_cli, "review-run", "--json")
    assert proc.returncode == 0, proc.stderr
    logged = argv_log.read_text().splitlines()

    # Verb prefix.
    assert "review-runs" in logged
    assert "--operation" in logged and _argv_value(logged, "--operation") == "record"
    assert "--review-mode" in logged and _argv_value(logged, "--review-mode") == "branch"

    # Workspace + record-required fields, all non-empty.
    workspace_root = _argv_value(logged, "--workspace-root")
    assert workspace_root and Path(workspace_root).resolve() == git_repo.resolve()
    assert _argv_value(logged, "--review-run-id"), logged
    assert _argv_value(logged, "--session"), logged
    assert _argv_value(logged, "--subject-path"), logged
    # Task ref is read from CURRENT_TASK.json synthesized in the fixture.
    assert _argv_value(logged, "--task-ref") == "TEST-42"
    # Branch reviews must declare subject-kind=branch.
    assert _argv_value(logged, "--subject-kind") == "branch"


def test_handoff_review_run_passes_full_required_argv(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """`handoff-review-run` must invoke the CLI with every record-required
    field populated. The subject is the planning doc passed via --doc."""
    fake_cli, argv_log = _make_logging_fake_cli(fake_cli_dir)
    proc = _run_lifecycle(
        git_repo, fake_cli, "handoff-review-run", "--doc", "docs/plan.md", "--json"
    )
    assert proc.returncode == 0, proc.stderr
    logged = argv_log.read_text().splitlines()

    assert "review-runs" in logged
    assert _argv_value(logged, "--operation") == "record"
    assert _argv_value(logged, "--review-mode") == "planning"

    workspace_root = _argv_value(logged, "--workspace-root")
    assert workspace_root and Path(workspace_root).resolve() == git_repo.resolve()
    assert _argv_value(logged, "--review-run-id"), logged
    assert _argv_value(logged, "--session"), logged
    assert _argv_value(logged, "--subject-path") == "docs/plan.md"
    assert _argv_value(logged, "--task-ref") == "TEST-42"


def test_review_run_user_overrides_take_precedence(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    fake_cli, argv_log = _make_logging_fake_cli(fake_cli_dir)
    proc = _run_lifecycle(
        git_repo, fake_cli, "review-run",
        "--review-run-id", "br-XYZ-1",
        "--session", "sess-XYZ",
        "--subject-path", "feature/custom-subject",
        "--task-ref", "OVERRIDE-99",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    logged = argv_log.read_text().splitlines()
    assert _argv_value(logged, "--review-run-id") == "br-XYZ-1"
    assert _argv_value(logged, "--session") == "sess-XYZ"
    assert _argv_value(logged, "--subject-path") == "feature/custom-subject"
    assert _argv_value(logged, "--task-ref") == "OVERRIDE-99"


def test_handoff_close_check_passes_workspace_root(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """`handoff-close-check` must pass --workspace-root so the underlying
    CLI can resolve the workspace without falling back to cwd."""
    fake_cli, argv_log = _make_logging_fake_cli(fake_cli_dir)
    proc = _run_lifecycle(git_repo, fake_cli, "handoff-close-check", "--json")
    assert proc.returncode == 0, proc.stderr
    logged = argv_log.read_text().splitlines()
    assert "integrity-check" in logged
    assert _argv_value(logged, "--kind") == "close"
    workspace_root = _argv_value(logged, "--workspace-root")
    assert workspace_root and Path(workspace_root).resolve() == git_repo.resolve()


def _write_strict_argparse_fake_cli(target: Path) -> None:
    """Fake CLI that mirrors the real ``mcp-workstate-handoff`` argparser
    shape: ``--workspace-root`` is a *parent* flag registered before
    ``add_subparsers``. Putting it after the subcommand causes
    ``argparse`` to exit 2 with ``unrecognized arguments``.
    """
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import argparse, json, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--workspace-root', required=True)\n"
        "sub = p.add_subparsers(dest='subcommand', required=True)\n"
        "cc = sub.add_parser('integrity-check')\n"
        "cc.add_argument('--kind', required=True)\n"
        "cc.add_argument('--task-ref', default=None)\n"
        "cc.add_argument('--enforce', action='store_true')\n"
        "cc.add_argument('--allow-no-active-task', action='store_true')\n"
        "cc.add_argument('--require-fresh-tests', action='store_true')\n"
        "cc.add_argument('--current-commit-sha', default=None)\n"
        "rr = sub.add_parser('review-runs')\n"
        "rr.add_argument('--operation', required=True)\n"
        "rr.add_argument('--review-run-id', default=None)\n"
        "rr.add_argument('--session', default=None)\n"
        "rr.add_argument('--subject-path', default=None)\n"
        "rr.add_argument('--subject-kind', default=None)\n"
        "rr.add_argument('--review-mode', default=None)\n"
        "rr.add_argument('--verdict', default=None)\n"
        "rr.add_argument('--verdict-decision', default=None)\n"
        "rr.add_argument('--task-ref', default=None)\n"
        "args = p.parse_args()\n"
        "sys.stdout.write(json.dumps({'ok': True, 'data': {}}))\n"
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def test_handoff_close_check_workspace_root_precedes_subcommand(
    git_repo: Path, tmp_path: Path
) -> None:
    """Regression: ``--workspace-root`` is a *global* flag on the real
    ``mcp-workstate-handoff`` argparser. If shell_out positions it after the
    ``handoff-close-check`` subcommand, the real CLI exits 2 with
    ``unrecognized arguments``. The strict-argparse fake CLI here would
    pass with a permissive bash stub; it fails when the flag order is
    wrong, exposing the regression.
    """
    fake_cli = tmp_path / "fake-strict-mcp"
    _write_strict_argparse_fake_cli(fake_cli)
    proc = _run_lifecycle(git_repo, fake_cli, "handoff-close-check", "--json")
    # Without the fix the wrapper subprocess exits 2; the lifecycle
    # handler then surfaces a non-zero ``delegated_exit_code`` and
    # propagates the failure.
    assert proc.returncode == 0, (proc.stderr, proc.stdout)
    receipt = json.loads(proc.stdout)
    assert receipt["delegated_exit_code"] == 0, receipt


def test_review_run_workspace_root_precedes_subcommand(
    git_repo: Path, tmp_path: Path
) -> None:
    """Same regression scope, ``review-run`` flavor. ``review-runs`` is a
    real subcommand on ``mcp-workstate-handoff`` and does not register
    ``--workspace-root`` on its subparser.
    """
    fake_cli = tmp_path / "fake-strict-mcp-rr"
    _write_strict_argparse_fake_cli(fake_cli)
    proc = _run_lifecycle(git_repo, fake_cli, "review-run", "--json")
    assert proc.returncode == 0, (proc.stderr, proc.stdout)
    receipt = json.loads(proc.stdout)
    assert receipt["delegated_exit_code"] == 0, receipt


def _seed_handoff_state(repo: Path, rows: list[dict[str, str]]) -> None:
    """Seed ``<repo>/.task-state/handoff.db`` with active rows.

    Each row dict supplies ``task_ref``, ``target_worktree_path``,
    ``status`` (defaults to ``in_progress``), and ``target_branch``
    (optional). Mirrors the minimal columns the wrapper reads.
    """
    import sqlite3

    state_dir = repo / ".task-state"
    state_dir.mkdir(exist_ok=True)
    db_path = state_dir / "handoff.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS handoff_state ("
            " task_ref TEXT PRIMARY KEY,"
            " status TEXT,"
            " target_branch TEXT,"
            " target_worktree_path TEXT)"
        )
        for row in rows:
            conn.execute(
                "INSERT OR REPLACE INTO handoff_state(task_ref, status, target_branch, target_worktree_path) VALUES (?, ?, ?, ?)",
                (
                    row["task_ref"],
                    row.get("status", "in_progress"),
                    row.get("target_branch", ""),
                    row["target_worktree_path"],
                ),
            )
        conn.commit()


def test_handoff_close_check_prefers_cwd_sqlite_match_over_stale_current_task_json(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """When the canonical handoff DB has an active row whose
    ``target_worktree_path`` matches the operator's cwd, the wrapper must
    bind ``--task-ref`` to that row — not to whatever task
    ``CURRENT_TASK.json`` happens to point at, since CURRENT_TASK.json is
    workspace-singular and can be stale relative to the worktree the
    operator is actually invoking close-check from.
    """
    fake_cli, argv_log = _make_logging_fake_cli(fake_cli_dir)

    # Seed the canonical handoff DB with the row that matches cwd.
    _seed_handoff_state(
        git_repo,
        [
            {
                "task_ref": "CWD-MATCHES-ROW",
                "target_worktree_path": str(git_repo.resolve()),
                "target_branch": "feature/cwd-matches-row",
            }
        ],
    )
    # The fixture's CURRENT_TASK.json is stale: it claims TEST-42 is
    # active even though the canonical row pinned to this cwd is
    # CWD-MATCHES-ROW. The wrapper must trust the cwd-keyed sqlite
    # match over the stale projection.

    proc = _run_lifecycle(git_repo, fake_cli, "handoff-close-check", "--json")
    assert proc.returncode == 0, proc.stderr
    logged = argv_log.read_text().splitlines()
    assert _argv_value(logged, "--task-ref") == "CWD-MATCHES-ROW", logged


def test_handoff_close_check_falls_back_to_current_task_json_when_no_cwd_match(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """If the handoff DB has no row whose ``target_worktree_path`` matches
    cwd (or the DB does not exist yet), the wrapper preserves the
    existing CURRENT_TASK.json fallback so the historical contract still
    holds.
    """
    fake_cli, argv_log = _make_logging_fake_cli(fake_cli_dir)

    _seed_handoff_state(
        git_repo,
        [
            {
                "task_ref": "ELSEWHERE-ROW",
                "target_worktree_path": "/nonexistent/path",
                "target_branch": "feature/elsewhere",
            }
        ],
    )

    proc = _run_lifecycle(git_repo, fake_cli, "handoff-close-check", "--json")
    assert proc.returncode == 0, proc.stderr
    logged = argv_log.read_text().splitlines()
    # Falls back to CURRENT_TASK.json's TEST-42 from the fixture.
    assert _argv_value(logged, "--task-ref") == "TEST-42", logged


def test_handoff_close_check_cwd_subdir_walks_to_worktree_root(
    git_repo: Path, fake_cli_dir: Path
) -> None:
    """When the wrapper is invoked from a subdirectory of the worktree,
    the cwd-keyed lookup must walk cwd's parents to find an active row
    whose ``target_worktree_path`` matches an ancestor — otherwise the
    third-commit fix silently falls back to the workspace-singular
    CURRENT_TASK.json the moment the operator runs from anywhere
    deeper than the worktree root.
    """
    fake_cli, argv_log = _make_logging_fake_cli(fake_cli_dir)
    _seed_handoff_state(
        git_repo,
        [
            {
                "task_ref": "CWD-PARENT-MATCH",
                "target_worktree_path": str(git_repo.resolve()),
                "target_branch": "feature/cwd-parent-match",
            }
        ],
    )
    subdir = git_repo / "packages" / "foo"
    subdir.mkdir(parents=True)

    proc = _run_lifecycle(subdir, fake_cli, "handoff-close-check", "--json")
    assert proc.returncode == 0, proc.stderr
    logged = argv_log.read_text().splitlines()
    assert _argv_value(logged, "--task-ref") == "CWD-PARENT-MATCH", logged


def test_shell_out_live_active_statuses_matches_canonical_handoff_constant() -> None:
    """``shell_out._LIVE_ACTIVE_STATUSES`` must mirror
    ``workstate_handoff_mcp.shared_primitives.LIVE_ACTIVE_STATUSES``.

    The wrapper deliberately does NOT import the canonical tuple at
    runtime (lifecycle handlers run in environments that may lack
    ``workstate_handoff_mcp`` on the import path). This test pins parity at
    test time so any future change to the canonical active-task status
    set forces a matching update in the wrapper, instead of silently
    drifting and either over- or under-matching active rows.

    Parses ``shell_out.py`` via the ``ast`` module rather than importing
    the handler — importing it transitively requires the lifecycle
    scripts directory on ``sys.path`` (``import resolver``), which the
    surrounding subprocess-style tests don't need. AST parsing keeps
    the parity guard self-contained.
    """
    import ast

    from workstate_handoff_mcp.shared_primitives import LIVE_ACTIVE_STATUSES

    shell_out_src = (
        PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"
        / "handlers" / "shell_out.py"
    ).read_text()
    tree = ast.parse(shell_out_src)
    found: tuple[str, ...] | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_LIVE_ACTIVE_STATUSES"
            and node.value is not None
        ):
            found = tuple(ast.literal_eval(node.value))
            break
    assert found is not None, (
        "shell_out._LIVE_ACTIVE_STATUSES not found via AST parse"
    )
    assert found == LIVE_ACTIVE_STATUSES, (
        "shell_out._LIVE_ACTIVE_STATUSES has drifted from "
        f"workstate_handoff_mcp.shared_primitives.LIVE_ACTIVE_STATUSES: "
        f"local={found} canonical={LIVE_ACTIVE_STATUSES}"
    )
