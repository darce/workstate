"""implementation note S3 — docs presence + monorepo dogfood wiring."""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
ROOT_MAKEFILE = REPO_ROOT / "Makefile"
DEV_WORKFLOW = (
    REPO_ROOT
    / "packages/workstate-system/workstate_system/payload/docs/workstate/rules/development-workflow.md"
)
ENV_REGISTRY = REPO_ROOT / "docs/workstate/environment-variables.md"
UPGRADING = REPO_ROOT / "docs/UPGRADING.md"


def test_root_makefile_dogfoods_worktree_bootstrap() -> None:
    text = ROOT_MAKEFILE.read_text()
    # The monorepo dogfoods the hook against a private app whose whole
    # assignment line the public export strips, so the variable is present only
    # in the private tree. Skip rather than hardcode the private path — the
    # literal would also trip the public-export forbidden-token gate.
    bootstrap_line = next(
        (line for line in text.splitlines() if line.startswith("LIFECYCLE_WORKTREE_BOOTSTRAP")),
        None,
    )
    if bootstrap_line is None:
        pytest.skip("LIFECYCLE_WORKTREE_BOOTSTRAP dogfood line absent (public export)")
    command = bootstrap_line.split(":=", 1)[1].strip()
    assert command, "monorepo must wire LIFECYCLE_WORKTREE_BOOTSTRAP to a bootstrap command"


def test_development_workflow_documents_bootstrap_hook() -> None:
    text = DEV_WORKFLOW.read_text()
    assert "LIFECYCLE_WORKTREE_BOOTSTRAP" in text
    assert "WORKSTATE_WORKTREE_BOOTSTRAP_CMD" in text
    assert "best-effort" in text.lower()


def test_environment_registry_lists_bootstrap_vars() -> None:
    text = ENV_REGISTRY.read_text()
    assert "WORKSTATE_WORKTREE_BOOTSTRAP_CMD" in text
    assert "WORKSTATE_WORKTREE_BOOTSTRAP_TIMEOUT" in text


def test_upgrading_mentions_worktree_bootstrap() -> None:
    text = UPGRADING.read_text()
    assert "LIFECYCLE_WORKTREE_BOOTSTRAP" in text