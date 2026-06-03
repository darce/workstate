"""WORKSTATE-REF-41 implementation note: ``task-start`` ``uv`` preflight + per-package sync.

Asserts the integration behavior wired into
``handlers/task_start.py``:

* Missing ``uv`` aborts before any git mutation; no branch is created.
* ``uv sync --extra dev`` runs once per discovered package after the
  worktree is created; sync output is streamed to stderr.
* A failing ``uv sync`` aborts ``task-start`` with the documented
  remediation message AND tears down the just-created linked worktree
  so no half-provisioned state row remains.
* ``SYNC_PACKAGES=<csv>`` narrows the synced set.
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

# A fake ``uv`` that logs every call (``called=$* cwd=...``) AND materializes
# ``<venv>/bin/python`` on the ``venv`` subcommand so WORKSTATE-REF-07 root provisioning
# (which validates ``python_path.exists()`` after ``uv venv --seed``) succeeds
# without a real interpreter. ``pip install`` and ``sync`` are logged no-ops.
_FAKE_UV_LOGGING_BODY = (
    "#!/usr/bin/env bash\n"
    'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
    'if [[ "$1" == "venv" ]]; then mkdir -p "$2/bin"; : > "$2/bin/python"; '
    'chmod +x "$2/bin/python"; fi\n'
    'echo "called=$* cwd=$(pwd)" >> {log}\n'
    "exit 0\n"
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def _write_executable(target: Path, body: str) -> None:
    target.write_text(body)
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _seed_pkg(root: Path, name: str) -> Path:
    pkg = root / "packages" / name
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname='x'\n")
    return pkg


def _commit_seeded_packages(repo: Path) -> None:
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "-q", "-m", "seed packages",
        ],
        check=True,
    )


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
def fake_handoff_cli(tmp_path: Path) -> Path:
    target = tmp_path / "fake-handoff"
    _write_executable(target, "#!/usr/bin/env bash\nexit 0\n")
    return target


def _run_task_start(
    cwd: Path,
    *,
    handoff_cli: Path,
    uv_bin: str | Path | None,
    extra_env: dict[str, str] | None = None,
    args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(handoff_cli)
    if uv_bin is None:
        env.pop("WORKSTATE_LIFECYCLE_UV_BIN", None)
    else:
        env["WORKSTATE_LIFECYCLE_UV_BIN"] = str(uv_bin)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), "task-start", *(args or [])],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_task_start_aborts_when_uv_missing_without_creating_branch(
    git_repo: Path, fake_handoff_cli: Path, tmp_path: Path
) -> None:
    nope = tmp_path / "nonexistent" / "no-uv-here"
    proc = _run_task_start(
        git_repo,
        handoff_cli=fake_handoff_cli,
        uv_bin=nope,
        args=["--task", "WORKSTATE-REF-300", "--mode", "here", "--json"],
    )
    assert proc.returncode != 0
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert "uv_preflight_failed" in receipt["error"]

    # No branch should have been created.
    branches = _git(git_repo, "branch", "--list", "feature/WORKSTATE-300")
    assert branches.strip() == ""
    # Still on main.
    assert _git(git_repo, "branch", "--show-current") == "main"


def test_task_start_runs_uv_sync_per_discovered_package(
    git_repo: Path, fake_handoff_cli: Path, tmp_path: Path
) -> None:
    _seed_pkg(git_repo, "alpha")
    _seed_pkg(git_repo, "bravo")

    log = tmp_path / "uv.log"
    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_LOGGING_BODY.format(log=log))

    proc = _run_task_start(
        git_repo,
        handoff_cli=fake_handoff_cli,
        uv_bin=fake_uv,
        args=["--task", "WORKSTATE-REF-301", "--mode", "here", "--json"],
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["task_ref"] == "WORKSTATE-REF-301"

    log_text = log.read_text() if log.exists() else ""
    assert log_text.count("called=sync --extra dev") == 2
    # Stderr surfaces the per-package header.
    assert "uv sync: alpha" in proc.stderr
    assert "uv sync: bravo" in proc.stderr


def test_task_start_uv_sync_failure_aborts_and_tears_down_worktree(
    git_repo: Path, fake_handoff_cli: Path, tmp_path: Path
) -> None:
    _seed_pkg(git_repo, "alpha")
    _commit_seeded_packages(git_repo)

    fake_uv = tmp_path / "fake-uv"
    _write_executable(
        fake_uv,
        '#!/usr/bin/env bash\n'
        'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
        'echo "boom" 1>&2; exit 1\n',
    )

    proc = _run_task_start(
        git_repo,
        handoff_cli=fake_handoff_cli,
        uv_bin=fake_uv,
        args=["--task", "WORKSTATE-REF-302", "--mode", "worktree", "--json"],
    )
    assert proc.returncode != 0
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert "uv_sync_failed" in receipt["error"]
    assert "alpha" in receipt["error"]
    assert "rerun manually" in receipt["error"]

    # The linked worktree should have been torn down (rolled back).
    canonical_sibling = git_repo.parent / f"{git_repo.name}-WORKSTATE-302"
    assert not canonical_sibling.exists()
    # The branch should not be left dangling either.
    branches = _git(git_repo, "branch", "--list", "feature/WORKSTATE-302")
    assert branches.strip() == ""


def test_task_start_uv_sync_failure_in_here_mode_restores_previous_branch(
    git_repo: Path, fake_handoff_cli: Path, tmp_path: Path
) -> None:
    """BR-WORKSTATE41-r6-02: MODE=here must roll back the new branch on uv sync fail.

    Before the fix the rollback only ran for MODE=worktree, so a here-mode
    sync failure left the caller checked out on the freshly-created feature
    branch with no handoff projection — the same half-started lifecycle
    state implementation note was meant to avoid.
    """
    _seed_pkg(git_repo, "alpha")
    _commit_seeded_packages(git_repo)

    fake_uv = tmp_path / "fake-uv"
    _write_executable(
        fake_uv,
        '#!/usr/bin/env bash\n'
        'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
        'echo "boom" 1>&2; exit 1\n',
    )

    proc = _run_task_start(
        git_repo,
        handoff_cli=fake_handoff_cli,
        uv_bin=fake_uv,
        args=["--task", "WORKSTATE-REF-304", "--mode", "here", "--json"],
    )
    assert proc.returncode != 0
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert "uv_sync_failed" in receipt["error"]

    # Caller is back on main, not stranded on the new feature branch.
    assert _git(git_repo, "branch", "--show-current") == "main"
    # And the dangling feature branch was cleaned up.
    branches = _git(git_repo, "branch", "--list", "feature/WORKSTATE-304")
    assert branches.strip() == ""


def test_task_start_sync_packages_override_narrows_target_set(
    git_repo: Path, fake_handoff_cli: Path, tmp_path: Path
) -> None:
    _seed_pkg(git_repo, "alpha")
    _seed_pkg(git_repo, "bravo")
    _seed_pkg(git_repo, "charlie")

    log = tmp_path / "uv.log"
    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_LOGGING_BODY.format(log=log))

    proc = _run_task_start(
        git_repo,
        handoff_cli=fake_handoff_cli,
        uv_bin=fake_uv,
        extra_env={"SYNC_PACKAGES": "bravo,alpha"},
        args=["--task", "WORKSTATE-REF-303", "--mode", "here", "--json"],
    )
    assert proc.returncode == 0, proc.stderr
    log_text = log.read_text() if log.exists() else ""
    assert "cwd=" + str((git_repo / "packages" / "alpha").resolve()) in log_text
    assert "cwd=" + str((git_repo / "packages" / "bravo").resolve()) in log_text
    assert "cwd=" + str((git_repo / "packages" / "charlie").resolve()) not in log_text


# ---------------------------------------------------------------------------
# WORKSTATE-REF-07 implementation note: root ``.venv`` provisioning in ``task-start``
# ---------------------------------------------------------------------------


def test_task_start_provisions_root_venv_before_projection(
    git_repo: Path, fake_handoff_cli: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-07 implementation note: ``task-start`` creates ``<worktree>/.venv`` (via
    ``uv venv --seed``) after package sync, and the success receipt names
    it through the additive ``root_venv_path`` field. ``here`` mode keeps
    the worktree == repo so the venv lands in the repo root.
    """
    _seed_pkg(git_repo, "alpha")
    _commit_seeded_packages(git_repo)

    log = tmp_path / "uv.log"
    fake_uv = tmp_path / "fake-uv"
    _write_executable(fake_uv, _FAKE_UV_LOGGING_BODY.format(log=log))

    proc = _run_task_start(
        git_repo,
        handoff_cli=fake_handoff_cli,
        uv_bin=fake_uv,
        args=["--task", "WORKSTATE-REF-305", "--mode", "here", "--json"],
    )
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    # Additive receipt field names the provisioned venv.
    assert receipt["root_venv_path"] == str(git_repo / ".venv")
    # The venv (and its python) was actually materialized in the worktree.
    assert (git_repo / ".venv" / "bin" / "python").exists()
    # Provisioning ran the venv-create command.
    log_text = log.read_text() if log.exists() else ""
    assert f"called=venv {git_repo / '.venv'} --seed" in log_text


def test_task_start_root_venv_failure_tears_down_worktree(
    git_repo: Path, fake_handoff_cli: Path, tmp_path: Path
) -> None:
    """WORKSTATE-REF-07 implementation note: a hard root-venv failure (``uv venv`` non-zero)
    after a successful package sync rolls back the just-created linked
    worktree and feature branch — exactly like a sync failure — so no
    half-provisioned task row is projected.
    """
    _seed_pkg(git_repo, "alpha")
    _commit_seeded_packages(git_repo)

    # sync (exit 0) passes; the root venv-create step fails hard.
    fake_uv = tmp_path / "fake-uv"
    _write_executable(
        fake_uv,
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "--version" ]]; then echo "uv 0.4.0"; exit 0; fi\n'
        'if [[ "$1" == "venv" ]]; then echo "venv boom" 1>&2; exit 1; fi\n'
        "exit 0\n",
    )

    proc = _run_task_start(
        git_repo,
        handoff_cli=fake_handoff_cli,
        uv_bin=fake_uv,
        args=["--task", "WORKSTATE-REF-306", "--mode", "worktree", "--json"],
    )
    assert proc.returncode != 0
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert "root_venv" in receipt["error"]

    # The linked worktree was torn down (rolled back).
    canonical_sibling = git_repo.parent / f"{git_repo.name}-WORKSTATE-306"
    assert not canonical_sibling.exists()
    # And the feature branch is not left dangling.
    branches = _git(git_repo, "branch", "--list", "feature/WORKSTATE-306")
    assert branches.strip() == ""
