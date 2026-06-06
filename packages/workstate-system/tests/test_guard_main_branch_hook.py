"""Shell-exec regression for ``scripts/hooks/guard-main-branch.sh``.

WORKSTATE-REF-1-BR2-03: the prior test (`test_fixture_env_hides_live_handoff_cli`)
only asserted that the fixture env *sets* ``WORKSTATE_SKIP_ACTIVE_TASK_PROBE=1``.
It did not exercise the shell branch that consumes it, so a future
refactor that drops the early ``exit 0`` would not be caught. This test
invokes the hook directly with a stubbed ``mcp-workstate-handoff`` to prove
the bypass actually short-circuits the active-task probe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
HOOK_PATH = PAYLOAD_ROOT / "scripts" / "hooks" / "guard-main-branch.sh"
HOOKS_DIR = PAYLOAD_ROOT / "scripts" / "hooks"
CONTRACT_PATH = (
    PAYLOAD_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
)


def _build_fixture_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo on ``main`` with the hook's deps linked in."""
    if shutil.which("git") is None:
        pytest.skip("git not available")

    repo = tmp_path / "repo"
    repo.mkdir()
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    subprocess.run(
        ["git", "-C", str(repo), "init", "-q", "--initial-branch=main"],
        check=True,
    )

    # Symlink the inline-python helpers and the contract the hook reads
    # so `git rev-parse --show-toplevel` resolves to this tmp repo without
    # us needing to duplicate the entire monorepo tree.
    (repo / "scripts" / "hooks").mkdir(parents=True)
    for fname in (
        "_guard_main_branch_inline.py",
        "_branch_isolation_guard.py",
        "_harness_protocol.py",
    ):
        (repo / "scripts" / "hooks" / fname).symlink_to(HOOKS_DIR / fname)

    (repo / "docs" / "workstate" / "contracts").mkdir(parents=True)
    (repo / "docs" / "workstate" / "contracts" / "harness-protocol.yaml").symlink_to(
        CONTRACT_PATH
    )

    # Commit the symlinks so the branch-isolation guard does not flag them
    # as dirty protected-path edits when the hook runs on main.
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"],
        check=True,
        env=git_env,
    )
    return repo


def _install_probe_stub(tmp_path: Path) -> tuple[Path, Path]:
    """Place a stub ``mcp-workstate-handoff`` on PATH that touches a sentinel."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    sentinel = tmp_path / "probe_called.flag"
    stub = bin_dir / "mcp-workstate-handoff"
    stub.write_text(
        f"#!/usr/bin/env bash\ntouch {sentinel!s}\nprintf '{{}}'\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, sentinel


def _run_hook(repo: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(HOOK_PATH)],
        input="{}",
        text=True,
        cwd=str(repo),
        env=env,
        capture_output=True,
    )


def test_guard_main_branch_skips_probe_when_bypass_set(tmp_path: Path) -> None:
    repo = _build_fixture_repo(tmp_path)
    bin_dir, sentinel = _install_probe_stub(tmp_path)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "WORKSTATE_SKIP_ACTIVE_TASK_PROBE": "1",
    }
    result = _run_hook(repo, env)

    assert result.returncode == 0, result.stderr
    assert not sentinel.exists(), (
        "WORKSTATE_SKIP_ACTIVE_TASK_PROBE=1 must short-circuit before the "
        "active-task probe runs. The stub `mcp-workstate-handoff` was invoked, "
        "so the early `exit 0` did not fire."
    )
    assert "WARNING" not in result.stderr


def test_guard_main_branch_runs_probe_without_bypass(tmp_path: Path) -> None:
    repo = _build_fixture_repo(tmp_path)
    bin_dir, sentinel = _install_probe_stub(tmp_path)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    env.pop("WORKSTATE_SKIP_ACTIVE_TASK_PROBE", None)
    result = _run_hook(repo, env)

    assert result.returncode == 0, result.stderr
    assert sentinel.exists(), (
        "Without WORKSTATE_SKIP_ACTIVE_TASK_PROBE, the hook must invoke the "
        "`mcp-workstate-handoff` probe. The stub was not called, so the probe "
        "branch never ran (or the hook exited earlier than expected)."
    )
