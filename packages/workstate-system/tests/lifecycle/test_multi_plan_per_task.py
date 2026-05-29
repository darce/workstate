"""implementation note implementation note — multi-plan-per-task in ``task-start``.

The handler accepts ``--plan`` as a glob pattern. When it matches more
than one file, the lexicographically-latest match wins (``-r2.md`` >
``-r1.md`` > the un-suffixed file). An explicit ``--plan-revision``
basename pins a specific match. A glob that matches nothing fails
closed with ``plan_glob_no_match``.

This is the bootstrap rule from implementation note deferred and re-anchored in
implementation note implementation note: existing single-plan tasks (no ``--plan``) keep
their current behavior unchanged.
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
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "init",
        ],
        check=True,
    )
    return repo


@pytest.fixture
def fake_cli_dir(tmp_path: Path) -> Path:
    return tmp_path / "fake-cli"


def _run_task_start(
    cwd: Path,
    fake_cli: Path,
    *extra: str,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_AGENT_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "task-start", *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _seed_plans(repo: Path, plan_files: list[str]) -> None:
    plans_dir = repo / "docs" / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)
    for name in plan_files:
        (plans_dir / name).write_text(f"# {name}\n")
    # WORKSTATE-REF-72 implementation note: task-start now refuses unless the plan exists on
    # ``main`` (accepted baseline). Commit the seeded plans so the gate
    # treats them as accepted — these tests exercise glob/pin selection,
    # not plan-baseline enforcement.
    subprocess.run(
        ["git", "-C", str(repo), "add", "docs/plans"],
        check=True,
    )
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
            "-q",
            "-m",
            "seed plans",
        ],
        check=True,
    )


def test_plan_glob_picks_lexicographically_latest_revision(git_repo: Path, fake_cli_dir: Path) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    _seed_plans(
        git_repo,
        [
            "0099-multi-plan-demo.md",
            "0099-multi-plan-demo-r1.md",
            "0099-multi-plan-demo-r2.md",
        ],
    )
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--objective",
        "multi-plan glob",
        "--mode",
        "here",
        "--plan",
        "docs/plans/0099-multi-plan-demo*.md",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["plan_path"] == "docs/plans/0099-multi-plan-demo-r2.md"


def test_plan_revision_pin_overrides_lex_latest(git_repo: Path, fake_cli_dir: Path) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    _seed_plans(
        git_repo,
        [
            "0099-multi-plan-demo.md",
            "0099-multi-plan-demo-r1.md",
            "0099-multi-plan-demo-r2.md",
        ],
    )
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--objective",
        "multi-plan pin",
        "--mode",
        "here",
        "--plan",
        "docs/plans/0099-multi-plan-demo*.md",
        "--plan-revision",
        "0099-multi-plan-demo-r1.md",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["plan_path"] == "docs/plans/0099-multi-plan-demo-r1.md"


def test_plan_glob_no_match_fails_closed(git_repo: Path, fake_cli_dir: Path) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--objective",
        "no plan match",
        "--mode",
        "here",
        "--plan",
        "docs/plans/does-not-exist*.md",
        "--json",
    )
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert "plan_glob_no_match" in receipt["error"]


def test_plan_revision_pin_rejected_when_not_in_glob(git_repo: Path, fake_cli_dir: Path) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    _seed_plans(
        git_repo,
        [
            "0099-multi-plan-demo.md",
            "0099-multi-plan-demo-r1.md",
        ],
    )
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--objective",
        "wrong pin",
        "--mode",
        "here",
        "--plan",
        "docs/plans/0099-multi-plan-demo*.md",
        "--plan-revision",
        "0099-multi-plan-demo-r9.md",
        "--json",
    )
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert "plan_revision_not_in_glob" in receipt["error"]


def test_no_plan_arg_falls_through_to_existing_behavior(git_repo: Path, fake_cli_dir: Path) -> None:
    fake_cli_dir.mkdir()
    fake_cli = fake_cli_dir / "mcp-workstate-handoff"
    _write_fake_cli(fake_cli, "#!/usr/bin/env bash\nexit 0\n")
    proc = _run_task_start(
        git_repo,
        fake_cli,
        "--task",
        "WORKSTATE-REF-99",
        "--objective",
        "fall-through",
        "--mode",
        "here",
        "--json",
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["plan_path"] is None
