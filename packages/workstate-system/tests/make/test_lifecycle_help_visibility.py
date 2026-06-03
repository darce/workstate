"""``make help`` visibility for lifecycle targets (WORKSTATE-REF-46 implementation note).

Pins the contract from the plan's Success Criteria:

* Every operator-facing lifecycle/plans/compaction target appears in
  ``make help`` so contributors and external review agents can discover
  them without grepping the included ``Makefile.d/*.mk`` fragments.
* The intentionally-excluded internal targets
  (``project-events-replay`` / ``dashboard`` / ``tasks-gc``) stay
  hidden so adding a doc string to them later requires an explicit
  test update, not a silent leak.
* Each documented target's recipe still resolves under ``make -n``;
  this guards against the ``target: ## description ; @body`` shape that
  Make parses as a comment and silently strips the recipe (``Nothing
  to be done for 'target'``) — a regression that would pass the help
  visibility check while breaking the actual command.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


pytestmark = pytest.mark.skipif(
    shutil.which("make") is None, reason="make not installed"
)


DOCUMENTED_TARGETS: tuple[str, ...] = (
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
    "doctor",
    "provision-env",
    "plan-show",
    "plan-edit",
    "plans-list",
    "plan-register",
    "compact-now",
    "compaction-disable",
    "compaction-enable",
    "compaction-status",
)


EXCLUDED_INTERNAL_TARGETS: tuple[str, ...] = (
    "project-events-replay",
    "dashboard",
    "tasks-gc",
)


_HELP_LINE_RE = re.compile(r"^\s*(?:\x1b\[[0-9;]*m)?([a-zA-Z0-9_.-]+)\s")


def _make_help_targets() -> set[str]:
    proc = subprocess.run(
        ["make", "help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    targets: set[str] = set()
    for line in proc.stdout.splitlines():
        match = _HELP_LINE_RE.match(line)
        if not match:
            continue
        targets.add(match.group(1))
    return targets


@pytest.fixture(scope="module")
def help_targets() -> set[str]:
    return _make_help_targets()


@pytest.mark.parametrize("target", DOCUMENTED_TARGETS)
def test_make_help_lists_documented_target(
    target: str, help_targets: set[str]
) -> None:
    assert target in help_targets, (
        f"`make help` does not list {target!r}. The plan's implementation note "
        f"requires every operator-facing lifecycle/plans/compaction "
        f"target to carry a `## description` doc string so the "
        f"existing awk walker at root Makefile picks it up."
    )


@pytest.mark.parametrize("target", EXCLUDED_INTERNAL_TARGETS)
def test_make_help_excludes_internal_target(
    target: str, help_targets: set[str]
) -> None:
    assert target not in help_targets, (
        f"`make help` lists internal target {target!r}, but it is "
        f"intentionally excluded (project-events-replay/dashboard/"
        f"tasks-gc). If the exclusion changes, update both this test "
        f"and the justifying `# …` comment above the recipe in "
        f"Makefile.d/lifecycle.mk."
    )


# WORKSTATE-REF-53 implementation note: the canonical workflow loop has five operator-facing
# entry points (`status`, `tasks`, `task-start`, `review-ready`,
# `close-check`). Their help text must surface the JSON form because the
# only honest Make spelling is `make <target> LIFECYCLE_ARGS=--json` —
# raw `make <target> --json` is parsed as a target list, not a flag, and
# silently does the wrong thing. Pinning this in `make help` keeps the
# canonical form in muscle memory for both operators and review agents.
WORKFLOW_LOOP_TARGETS: tuple[str, ...] = (
    "status",
    "tasks",
    "task-start",
    "review-ready",
    "close-check",
)


@pytest.mark.parametrize("target", WORKFLOW_LOOP_TARGETS)
def test_workflow_loop_help_documents_lifecycle_args_json(
    target: str,
) -> None:
    """The five workflow-loop targets must mention ``LIFECYCLE_ARGS=--json``
    in their `make help` line so operators do not learn the unsupported
    raw `make <target> --json` form by reading help text."""
    proc = subprocess.run(
        ["make", "help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    target_line = next(
        (line for line in proc.stdout.splitlines() if line.lstrip().startswith(target + " ")
         or line.lstrip().startswith(target + ":")
         or re.match(rf"^\s*(?:\x1b\[[0-9;]*m)?{re.escape(target)}\s", line)),
        None,
    )
    assert target_line is not None, (
        f"`make help` did not produce a line for {target!r}; cannot check "
        f"for LIFECYCLE_ARGS=--json guidance."
    )
    assert "LIFECYCLE_ARGS=--json" in target_line, (
        f"`make help` line for {target!r} does not mention "
        f"`LIFECYCLE_ARGS=--json`. The canonical workflow loop "
        f"(WORKSTATE-REF-53 implementation note) requires every loop target to surface the "
        f"honest JSON spelling here so operators do not invent raw "
        f"`make {target} --json` (which Make parses as a target list, "
        f"not a flag). help line was: {target_line!r}"
    )


@pytest.mark.parametrize("target", DOCUMENTED_TARGETS)
def test_make_n_recipe_still_runnable(target: str) -> None:
    """``make -n <target>`` exits 0 with a non-empty resolved command —
    guards against the ``target: ## description ; @body`` trap where Make
    parses the doc string as a comment and silently strips the recipe."""
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
    assert "Nothing to be done" not in proc.stdout, (
        f"`make -n {target}` resolved to a no-op recipe; the doc "
        f"string likely swallowed the recipe body. stdout: {proc.stdout!r}"
    )
    assert proc.stdout.strip(), (
        f"`make -n {target}` produced empty stdout — recipe stripped. "
        f"stderr: {proc.stderr!r}"
    )
