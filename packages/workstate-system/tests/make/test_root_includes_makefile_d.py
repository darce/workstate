"""Root-include + lifecycle target reachability (implementation note implementation note).

Asserts the deterministic-trigger contract from §Constraints: the
monorepo root ``Makefile`` carries the
``-include packages/workstate-system/Makefile.d/*.mk`` directive, and
every lifecycle target listed in the plan resolves under ``make -n``
(i.e. Make can locate the rule even though its body is a one-liner
that defers to the Python runner).
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ROOT_MAKEFILE = REPO_ROOT / "Makefile"

ROOT_INCLUDE_RE = re.compile(
    r"^[-]?include\s+packages/workstate-system/workstate_system/payload/Makefile\.d/\*\.mk\s*$",
    re.MULTILINE,
)

LIFECYCLE_TARGETS: tuple[str, ...] = (
    "task-start",
    "context",
    "slice-start",
    "slice-commit",
    "review-ready",
    "close-check",
    "handoff-close-check",
    "plan-review",
    "plan-analyze",
    "review-run",
    "handoff-review-run",
    "status",
    "tasks",
    "project-events-replay",
)


def test_root_makefile_includes_packages_makefile_d() -> None:
    contents = ROOT_MAKEFILE.read_text()
    assert ROOT_INCLUDE_RE.search(contents), (
        "monorepo root Makefile must declare "
        "'-include packages/workstate-system/workstate_system/payload/Makefile.d/*.mk' "
        "so package-level Make fragments are visible to root `make`."
    )


@pytest.mark.skipif(shutil.which("make") is None, reason="make not installed")
@pytest.mark.parametrize("target", LIFECYCLE_TARGETS)
def test_lifecycle_target_resolves_under_make_dry_run(target: str) -> None:
    proc = subprocess.run(
        ["make", "-n", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`make -n {target}` failed (exit {proc.returncode}). "
        f"stderr: {proc.stderr!r}"
    )
