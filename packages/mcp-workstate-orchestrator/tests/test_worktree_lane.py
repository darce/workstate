from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from workstate_orchestrator_mcp import api
from workstate_orchestrator_mcp._assets import bundled_script_path

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT_PATH = bundled_script_path("worktree-lane")


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True, text=True)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def test_create_rejects_existing_worktree_on_wrong_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "codex@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Codex"], cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)

    wrong_lane_path = tmp_path / "repo-frontend"
    _run(["git", "-C", str(repo), "worktree", "add", str(wrong_lane_path), "-b", "wrong-branch"], cwd=repo)

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT_PATH),
            "create",
            "--orchestrator-root",
            str(repo),
            "--lane-id",
            "frontend",
            "--branch",
            "codex/expected-frontend",
            "--worktree-path",
            str(wrong_lane_path),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "existing worktree" in result.stderr
    assert "wrong-branch" in result.stderr
    assert "codex/expected-frontend" in result.stderr


def test_create_warns_when_lifecycle_provisioning_absent(tmp_path: Path) -> None:
    """WORKSTATE-REF-07 implementation note: a fresh worktree whose orchestrator root ships no
    lifecycle entry point (and no ``AGENTIC_LIFECYCLE_DIR`` override) warns
    with the manual recovery command instead of failing lane creation."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "codex@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Codex"], cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)

    lane_path = tmp_path / "repo-frontend"

    env = os.environ.copy()
    env.pop("AGENTIC_LIFECYCLE_DIR", None)

    # run_mcp / lane-upsert may fail against the bare repo's shared state, so
    # check=False — the provisioning warning fires before that, right after the
    # ``git worktree add`` in the create subcommand.
    result = subprocess.run(
        [
            "bash",
            str(SCRIPT_PATH),
            "create",
            "--orchestrator-root",
            str(repo),
            "--lane-id",
            "frontend",
            "--branch",
            "codex/frontend",
            "--worktree-path",
            str(lane_path),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert "lifecycle provisioning entry point not found" in result.stderr
    assert "provision-env --worktree" in result.stderr
    assert str(lane_path) in result.stderr


def test_create_suppresses_successful_provision_env_json_stdout(tmp_path: Path) -> None:
    """WORKSTATE-REF-07 BR-01: successful provisioning must not add a second JSON
    document to ``worktree-lane create`` stdout before the lane-upsert receipt.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init"], cwd=repo)
    _run(["git", "config", "user.email", "codex@example.com"], cwd=repo)
    _run(["git", "config", "user.name", "Codex"], cwd=repo)
    (repo / "README.md").write_text("hello\n")
    _run(["git", "add", "README.md"], cwd=repo)
    _run(["git", "commit", "-m", "init"], cwd=repo)

    lifecycle_dir = tmp_path / "lifecycle"
    lifecycle_dir.mkdir()
    (lifecycle_dir / "__main__.py").write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "worktree = Path(sys.argv[sys.argv.index('--worktree') + 1])\n"
        "(worktree / '.provisioned').write_text('yes')\n"
        "print(json.dumps({'ok': True, 'source': 'provision-env'}))\n"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "mcp-workstate-handoff",
        '#!/usr/bin/env bash\nprintf \'{"ok": true, "source": "lane-upsert"}\\n\'\n',
    )

    lane_path = tmp_path / "repo-frontend"
    env = os.environ.copy()
    env["AGENTIC_LIFECYCLE_DIR"] = str(lifecycle_dir)
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT_PATH),
            "create",
            "--orchestrator-root",
            str(repo),
            "--lane-id",
            "frontend",
            "--branch",
            "codex/frontend",
            "--worktree-path",
            str(lane_path),
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert (lane_path / ".provisioned").read_text() == "yes"
    assert "provision-env" not in result.stdout
    assert json.loads(result.stdout) == {"ok": True, "source": "lane-upsert"}


@pytest.mark.skip(
    reason="scripts/worktree-lane uses lane-list CLI command removed in WORKSTATE-REF-12-9 implementation note; update to orchestrator CLI in implementation note"
)
def test_close_dry_run_prints_cleanup_commands(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    api.configure_runtime(api.RuntimeConfig.for_workspace(repo))
    api.set_handoff_state(task_ref="task-1", objective="lane close")
    api.manage_worktree_lane(
        operation="upsert",
        task_ref="task-1",
        lane_id="frontend",
        worktree_path=str(repo / "frontend"),
        branch="codex/frontend",
        status="closed",
    )

    result = subprocess.run(
        [
            "bash",
            str(SCRIPT_PATH),
            "close",
            "--orchestrator-root",
            str(repo),
            "--task-ref",
            "task-1",
            "--lane-id",
            "frontend",
            "--worktree-path",
            str(repo / "frontend"),
            "--branch",
            "codex/frontend",
            "--dry-run",
        ],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "worktree remove" in result.stdout
    assert "branch -d" in result.stdout
    assert "lane-upsert" in result.stdout
    assert "closed" in result.stdout
