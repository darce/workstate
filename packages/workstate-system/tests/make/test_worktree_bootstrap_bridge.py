"""implementation note S2 — ``LIFECYCLE_WORKTREE_BOOTSTRAP`` Make bridge.

The consumer-facing Make variable must forward into the runner env as
``WORKSTATE_WORKTREE_BOOTSTRAP_CMD`` on the ``task-start`` recipe, matching
the ``WORKSTATE_ADOPT_CMD`` / ``LIFECYCLE_FORMATTER`` naming families.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
LIFECYCLE_MK = (
    REPO_ROOT
    / "packages/workstate-system/workstate_system/payload/Makefile.d/lifecycle.mk"
)

pytestmark = pytest.mark.skipif(
    shutil.which("make") is None, reason="make not installed"
)

_SENTINEL_CMD = "cd apps/prototype-wp-alt-context && npm install"


def test_lifecycle_mk_declares_worktree_bootstrap_variable() -> None:
    text = LIFECYCLE_MK.read_text()
    assert "LIFECYCLE_WORKTREE_BOOTSTRAP ?=" in text
    assert "WORKSTATE_WORKTREE_BOOTSTRAP_CMD" in text


def test_make_n_task_start_forwards_bootstrap_env_bridge() -> None:
    """``LIFECYCLE_WORKTREE_BOOTSTRAP`` lands as runner env on task-start."""
    proc = subprocess.run(
        [
            "make",
            "-n",
            "task-start",
            f"LIFECYCLE_WORKTREE_BOOTSTRAP={_SENTINEL_CMD}",
            "TASK=WS-WTBOOT-S2",
            'OBJECTIVE="bridge test"',
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`make -n task-start` failed (exit {proc.returncode}). "
        f"stderr: {proc.stderr!r}"
    )
    assert "WORKSTATE_WORKTREE_BOOTSTRAP_CMD=" in proc.stdout, proc.stdout
    assert _SENTINEL_CMD in proc.stdout, proc.stdout


def test_make_n_task_start_bootstrap_bridge_empty_disables() -> None:
    """Empty consumer var emits the env bridge as '' (feature off).

    The empty value is passed on the command line so the assertion is
    hermetic: a command-line assignment overrides whatever this repo's root
    Makefile sets ``LIFECYCLE_WORKTREE_BOOTSTRAP`` to (S3 dogfood wires it to a
    real ``npm install``). This proves the bridge's empty-value contract
    independent of the dogfood override that ships on the same branch.
    """
    proc = subprocess.run(
        [
            "make",
            "-n",
            "task-start",
            "LIFECYCLE_WORKTREE_BOOTSTRAP=",
            "TASK=WS-WTBOOT-S2B",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert re.search(
        r"WORKSTATE_WORKTREE_BOOTSTRAP_CMD=''",
        proc.stdout,
    ), proc.stdout


def test_make_n_task_start_lifecycle_args_still_forwarded() -> None:
    proc = subprocess.run(
        [
            "make",
            "-n",
            "task-start",
            "LIFECYCLE_ARGS=--json",
            "TASK=WS-WTBOOT-S2C",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--json" in proc.stdout, proc.stdout
    assert "WORKSTATE_WORKTREE_BOOTSTRAP_CMD=" in proc.stdout, proc.stdout