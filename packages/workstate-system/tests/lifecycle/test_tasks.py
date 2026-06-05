"""implementation note contract tests for the read-only ``tasks`` subcommand."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ._status_harness import write_tasks_handoff_cli


PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"

if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))
if str(LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG))

from workstate.lifecycle.handlers import tasks as tasks_handler


def _run_tasks(
    cwd: Path,
    *,
    handoff_bin: Path | None = None,
    pythonpath_root: Path | None = None,
    extra_args: list[str] | None = None,
    emit_json: bool = True,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if handoff_bin is not None:
        env["MCP_WORKSTATE_HANDOFF_BIN"] = str(handoff_bin)
    if pythonpath_root is not None:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{pythonpath_root}{os.pathsep}{existing}" if existing else str(pythonpath_root)
        )
    return subprocess.run(
        [
            sys.executable,
            str(LIFECYCLE_PKG),
            "tasks",
            *(["--json"] if emit_json else []),
            *(extra_args or []),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _git_head(repo: Path) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()


def _git_branch(repo: Path) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "branch", "--show-current"], text=True).strip()


def _git_fields(repo: Path) -> dict[str, str]:
    return {
        "branch": _git_branch(repo),
        "worktree_path": str(repo),
        "head": _git_head(repo),
        "repo_root": str(repo),
        "cwd": str(repo),
    }


def _write_fake_handoff_pkg(
    root: Path,
    *,
    api_body: str,
    init_body: str = "from . import api\n",
    plan_cli_body: str | None = None,
) -> None:
    pkg = root / "workstate_handoff_mcp"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text(init_body)
    (pkg / "api.py").write_text(api_body)
    if plan_cli_body is not None:
        (pkg / "plan_cli.py").write_text(plan_cli_body)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "Makefile").write_text("tasks:\n\t@true\n")
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


def test_tasks_lists_primary_path_rows(git_repo: Path, tmp_path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")

    fake_cli = tmp_path / "fake-handoff"
    write_tasks_handoff_cli(
        fake_cli,
        rows=[
            {
                "task_ref": "WORKSTATE-REF-77",
                "status": "in_progress",
                "target_branch": "feature/WORKSTATE-77-x",
                "target_worktree_path": str(git_repo),
                "task_plan_path": "plans/WORKSTATE-REF-77.md",
                "updated_at": "2026-05-06 21:30:00",
                "revision": 7,
            }
        ],
    )

    proc = _run_tasks(git_repo, handoff_bin=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt == {
        "ok": True,
        "command": "tasks",
        **_git_fields(git_repo),
        "tasks": [
            {
                "task_ref": "WORKSTATE-REF-77",
                "status": "in_progress",
                "target_branch": "feature/WORKSTATE-77-x",
                "target_worktree_path": str(git_repo),
                "task_plan_path": "plans/WORKSTATE-REF-77.md",
                "task_plan_exists": True,
                "cwd_matches_target": True,
                "updated_at": "2026-05-06 21:30:00",
            }
        ],
        "active_count": 1,
        "stale_done_count": 0,
        "handoff_available": True,
        "truncated": False,
        "limit": 50,
        "warnings": [],
        "workspace_role": "implementation_plane",
    }


def test_tasks_handoff_unavailable_fails_soft(git_repo: Path, tmp_path: Path) -> None:
    proc = _run_tasks(git_repo, handoff_bin=tmp_path / "no-such-handoff-cli")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt == {
        "ok": True,
        "command": "tasks",
        **_git_fields(git_repo),
        "tasks": [],
        "active_count": 0,
        "stale_done_count": 0,
        "handoff_available": False,
        "truncated": False,
        "limit": 50,
        "warnings": [
            {
                "field": "tasks",
                "reason": "unavailable",
                "exception_type": None,
            }
        ],
        "workspace_role": "control_plane",
    }


def test_tasks_fallback_enumeration_warns_and_stays_green(git_repo: Path, tmp_path: Path) -> None:
    fake_root = tmp_path / "fake-py"
    fake_cli = tmp_path / "fake-handoff"
    write_tasks_handoff_cli(fake_cli, unsupported_rows=True)
    _write_fake_handoff_pkg(
        fake_root,
        api_body="PASS = True\n",
        plan_cli_body=(
            "from __future__ import annotations\n"
            "\n"
            "import sys\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    if 'list' not in sys.argv:\n"
            "        raise SystemExit(2)\n"
            "    print('=== task_ref=WORKSTATE-REF-88 branch=feature/WORKSTATE-88-x path=plans/WORKSTATE-REF-88.md exists=false ===')\n"
        ),
    )

    proc = _run_tasks(git_repo, handoff_bin=fake_cli, pythonpath_root=fake_root)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt == {
        "ok": True,
        "command": "tasks",
        **_git_fields(git_repo),
        "tasks": [
            {
                "task_ref": "WORKSTATE-REF-88",
                "status": None,
                "target_branch": "feature/WORKSTATE-88-x",
                "target_worktree_path": None,
                "task_plan_path": "plans/WORKSTATE-REF-88.md",
                "task_plan_exists": False,
                "cwd_matches_target": False,
                "updated_at": None,
            }
        ],
        "active_count": 1,
        "stale_done_count": 0,
        "handoff_available": True,
        "truncated": False,
        "limit": 50,
        "warnings": [
            {
                "field": "tasks_source",
                "reason": "list_handoff_rows unavailable; using legacy enumeration",
                "exception_type": "AttributeError",
            },
            {
                "field": "active_set_semantics",
                "reason": "may include stale done rows",
                "exception_type": None,
            },
        ],
        "workspace_role": "control_plane",
    }


def test_tasks_fallback_with_unset_plan_path_stays_green(git_repo: Path, tmp_path: Path) -> None:
    fake_root = tmp_path / "fake-py"
    fake_cli = tmp_path / "fake-handoff"
    write_tasks_handoff_cli(fake_cli, unsupported_rows=True)
    _write_fake_handoff_pkg(
        fake_root,
        api_body="PASS = True\n",
        plan_cli_body=(
            "from __future__ import annotations\n"
            "\n"
            "import sys\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    if 'list' not in sys.argv:\n"
            "        raise SystemExit(2)\n"
            "    print('=== task_ref=WORKSTATE-REF-UNSET branch=feature/WORKSTATE-unset-x path=<unset> exists=false ===')\n"
            '    print("WARNING: task_plan_path is unset for WORKSTATE-REF-UNSET; set it via set_handoff_state(task_plan_path=\'docs/plans/...\').")\n'
        ),
    )

    proc = _run_tasks(git_repo, handoff_bin=fake_cli, pythonpath_root=fake_root)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["branch"] == _git_branch(git_repo)
    assert receipt["head"] == _git_head(git_repo)
    assert receipt["repo_root"] == str(git_repo)
    assert receipt["worktree_path"] == str(git_repo)
    assert receipt["cwd"] == str(git_repo)
    assert receipt["tasks"] == [
        {
            "task_ref": "WORKSTATE-REF-UNSET",
            "status": None,
            "target_branch": "feature/WORKSTATE-unset-x",
            "target_worktree_path": None,
            "task_plan_path": None,
            "task_plan_exists": False,
            "cwd_matches_target": False,
            "updated_at": None,
        }
    ]
    assert receipt["warnings"] == [
        {
            "field": "tasks_source",
            "reason": "list_handoff_rows unavailable; using legacy enumeration",
            "exception_type": "AttributeError",
        },
        {
            "field": "active_set_semantics",
            "reason": "may include stale done rows",
            "exception_type": None,
        },
    ]


def test_tasks_primary_path_nonexistent_plan_stays_false(git_repo: Path, tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake-handoff"
    write_tasks_handoff_cli(
        fake_cli,
        rows=[
            {
                "task_ref": "WORKSTATE-REF-MISSING",
                "status": "in_progress",
                "target_branch": "feature/WORKSTATE-missing-x",
                "target_worktree_path": "/tmp/wt-missing",
                "task_plan_path": "plans/WORKSTATE-REF-MISSING.md",
                "updated_at": "2026-05-06 21:35:00",
                "revision": 9,
            }
        ],
    )

    proc = _run_tasks(git_repo, handoff_bin=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["branch"] == _git_branch(git_repo)
    assert receipt["tasks"] == [
        {
            "task_ref": "WORKSTATE-REF-MISSING",
            "status": "in_progress",
            "target_branch": "feature/WORKSTATE-missing-x",
            "target_worktree_path": "/tmp/wt-missing",
            "task_plan_path": "plans/WORKSTATE-REF-MISSING.md",
            "task_plan_exists": False,
            "cwd_matches_target": False,
            "updated_at": "2026-05-06 21:35:00",
        }
    ]


def test_tasks_no_daemon_start_path_is_reachable(monkeypatch: pytest.MonkeyPatch, git_repo: Path) -> None:
    emitted: list[dict[str, object]] = []
    calls: list[list[str]] = []

    def _run_subprocess(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(
            args=argv,
            returncode=0,
            stdout=json.dumps(
                [
                    {
                        "task_ref": "WORKSTATE-REF-77",
                        "status": "in_progress",
                        "target_branch": "feature/WORKSTATE-77-x",
                        "target_worktree_path": str(git_repo),
                        "task_plan_path": None,
                        "updated_at": "2026-05-06 21:36:00",
                        "revision": 10,
                    }
                ]
            ),
            stderr="",
        )

    monkeypatch.setattr(tasks_handler.resolver, "repo_root", lambda: git_repo)
    monkeypatch.setattr(tasks_handler.resolver, "current_worktree", lambda repo: git_repo)
    monkeypatch.setattr(tasks_handler._common, "mcp_handoff_bin", lambda: "fake-handoff")
    monkeypatch.setattr(tasks_handler._common, "run_subprocess", _run_subprocess)
    monkeypatch.setattr(tasks_handler._common, "emit", lambda payload: emitted.append(payload))

    exit_code = tasks_handler.run(["--json"])

    assert exit_code == 0
    # WORKSTATE65-BR-01: ``tasks`` calls ``gather_git_facts`` first, which
    # now consults the live handoff registry via ``handoff-rows`` so the
    # resolver can pick the most-specific registered task ref. The
    # second ``handoff-rows`` call is the existing tasks enumeration.
    handoff_rows_call = [
        "fake-handoff",
        "--workspace-root",
        str(git_repo),
        "handoff-rows",
        "--status",
        "in_progress",
        "review",
        "blocked",
    ]
    assert calls == [handoff_rows_call, handoff_rows_call]
    assert emitted[0]["ok"] is True
    assert emitted[0]["tasks"][0]["task_ref"] == "WORKSTATE-REF-77"
    assert emitted[0]["branch"] == _git_branch(git_repo)


def test_tasks_live_status_filter_matches_shared_primitives() -> None:
    shared_primitives = importlib.import_module("workstate_handoff_mcp.shared_primitives")
    assert tasks_handler._live_active_statuses() == getattr(shared_primitives, "LIVE_ACTIVE_STATUSES")


def test_tasks_truncates_to_limit(git_repo: Path, tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake-handoff"
    write_tasks_handoff_cli(
        fake_cli,
        rows=[
            {
                "task_ref": "WORKSTATE-REF-77",
                "status": "in_progress",
                "target_branch": "feature/WORKSTATE-77-x",
                "target_worktree_path": "/tmp/wt-77",
                "task_plan_path": "plans/WORKSTATE-REF-77.md",
                "updated_at": "2026-05-06 21:30:00",
                "revision": 7,
            },
            {
                "task_ref": "WORKSTATE-REF-88",
                "status": "review",
                "target_branch": "feature/WORKSTATE-88-x",
                "target_worktree_path": "/tmp/wt-88",
                "task_plan_path": "plans/WORKSTATE-REF-88.md",
                "updated_at": "2026-05-06 21:31:00",
                "revision": 8,
            },
        ],
    )

    proc = _run_tasks(git_repo, handoff_bin=fake_cli, extra_args=["--limit", "1"])
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["branch"] == _git_branch(git_repo)
    assert receipt["truncated"] is True
    assert receipt["limit"] == 1
    assert receipt["active_count"] == 2
    assert [task["task_ref"] for task in receipt["tasks"]] == ["WORKSTATE-REF-77"]


def test_tasks_filters_done_rows_from_receipt(git_repo: Path, tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake-handoff"
    write_tasks_handoff_cli(
        fake_cli,
        rows=[
            {
                "task_ref": "WORKSTATE-REF-DONE",
                "status": "done",
                "target_branch": "feature/done",
                "target_worktree_path": "/tmp/wt-done",
                "task_plan_path": "plans/WORKSTATE-REF-DONE.md",
                "updated_at": "2026-05-06 21:29:00",
                "revision": 4,
            },
            {
                "task_ref": "WORKSTATE-REF-LIVE",
                "status": "blocked",
                "target_branch": "feature/live",
                "target_worktree_path": "/tmp/wt-live",
                "task_plan_path": "plans/WORKSTATE-REF-LIVE.md",
                "updated_at": "2026-05-06 21:30:00",
                "revision": 5,
            },
        ],
    )

    proc = _run_tasks(git_repo, handoff_bin=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert [task["task_ref"] for task in receipt["tasks"]] == ["WORKSTATE-REF-LIVE"]
    assert receipt["active_count"] == 1
    assert receipt["stale_done_count"] == 1


def test_tasks_default_mode_prints_one_line_per_task(git_repo: Path, tmp_path: Path) -> None:
    fake_cli = tmp_path / "fake-handoff"
    write_tasks_handoff_cli(
        fake_cli,
        rows=[
            {
                "task_ref": "WORKSTATE-REF-77",
                "status": "in_progress",
                "target_branch": "feature/WORKSTATE-77-x",
                "target_worktree_path": str(git_repo),
                "task_plan_path": "plans/WORKSTATE-REF-77.md",
                "updated_at": "2026-05-06 21:30:00",
                "revision": 7,
            }
        ],
    )

    proc = _run_tasks(git_repo, handoff_bin=fake_cli, emit_json=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "WORKSTATE-REF-77\tin_progress\tfeature/WORKSTATE-77-x\tplans/WORKSTATE-REF-77.md"


def test_tasks_timeout_fails_soft_via_shared_handoff_cli(
    monkeypatch: pytest.MonkeyPatch,
    git_repo: Path,
    tmp_path: Path,
) -> None:
    fake_cli = tmp_path / "fake-handoff"
    emitted: list[dict[str, object]] = []

    write_tasks_handoff_cli(fake_cli, delay_seconds=1.0)
    monkeypatch.setattr(tasks_handler, "DEFAULT_HANDOFF_TIMEOUT", 0.01)
    monkeypatch.setattr(tasks_handler.resolver, "repo_root", lambda: git_repo)
    monkeypatch.setattr(tasks_handler.resolver, "current_worktree", lambda repo: git_repo)
    monkeypatch.setattr(tasks_handler._common, "mcp_handoff_bin", lambda: str(fake_cli))
    monkeypatch.setattr(tasks_handler._common, "emit", lambda payload: emitted.append(payload))

    exit_code = tasks_handler.run(["--json"])

    assert exit_code == 0
    assert emitted[0]["handoff_available"] is False
    assert emitted[0]["warnings"] == [
        {
            "field": "tasks",
            "reason": "timeout",
            "exception_type": "TimeoutExpired",
        }
    ]


def test_tasks_real_handoff_runtime_smoke(git_repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-77-x"],
        check=True,
    )
    plan_path = git_repo / "plans" / "WORKSTATE-REF-77.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text("# WORKSTATE-REF-77 Plan\n")

    subprocess.run(
        [
            "mcp-workstate-handoff",
            "--workspace-root",
            str(git_repo),
            "set",
            "--task-ref",
            "WORKSTATE-REF-77",
            "--objective",
            "Smoke-test tasks runtime",
            "--status",
            "in_progress",
            "--target-branch",
            "feature/WORKSTATE-77-x",
            "--target-worktree-path",
            str(git_repo),
            "--task-plan-path",
            "plans/WORKSTATE-REF-77.md",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    proc = _run_tasks(git_repo)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)

    assert receipt["handoff_available"] is True
    assert receipt["active_count"] >= 1
    assert any(task["task_ref"] == "WORKSTATE-REF-77" for task in receipt["tasks"])


# WORKSTATE-REF-53 implementation note: tasks receipt classifies the workspace_role too so
# generated guidance can stay consistent across `status` and `tasks`.

def test_tasks_receipt_includes_workspace_role(
    git_repo: Path,
    tmp_path: Path,
) -> None:
    fake_cli_dir = tmp_path / "fake-cli"
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    write_tasks_handoff_cli(fake_cli, rows=[])
    proc = _run_tasks(git_repo, handoff_bin=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["workspace_role"] == "control_plane", (
        "WORKSTATE-REF-53 implementation note: `tasks` from root `main` must classify the "
        "workspace_role as control_plane so generated guidance is "
        "consistent with `status`."
    )


def test_tasks_stub_is_removed_from_expected_stubs() -> None:
    stub_test = (Path(__file__).parent / "test_failing_stubs.py").read_text()
    assert '"tasks": "slice-6"' not in stub_test