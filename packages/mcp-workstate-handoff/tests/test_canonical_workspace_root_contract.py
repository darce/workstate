"""Cross-package canonical-root contract test (WORKSTATE-REF-54 implementation note, CTP-PR-02).

The server resolver
(``workstate_handoff_mcp.config._resolve_primary_worktree_root``) and the
client resolver
(``workstate.lifecycle.resolver.canonical_workspace_root``) live in
separate packages by design — see *Files and Surfaces* in the
WORKSTATE-REF-54 plan. They MUST agree on the canonical workspace root so the
server's per-task projection directory and the client's "where do I
read live state from?" answer never diverge across a linked worktree.

This test stands the contract up against three real git fixtures
(primary worktree, linked worktree, detached-HEAD) and asserts that
both implementations return the same resolved path.

Bare repositories are intentionally out of scope: handoff state lives
inside working trees, and the two implementations are documented to
diverge for the bare-repo case (server returns the bare directory
itself, client returns its parent). The handoff write paths cannot
reach a bare repo in any supported workflow.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from workstate_handoff_mcp.config import _resolve_primary_worktree_root

# The client resolver lives in a sibling package's scripts/ tree.
# Shimmed onto sys.path here so the contract test can import it
# without requiring an editable install for the consumer scripts.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLIENT_SCRIPTS = _REPO_ROOT / "packages" / "workstate-system" / "workstate_system" / "payload" / "scripts"
if str(_CLIENT_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_CLIENT_SCRIPTS))

from workstate.lifecycle.resolver import canonical_workspace_root  # noqa: WORKSTATE-REF-402


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def primary_repo(tmp_path: Path) -> Path:
    """Initialised git repo with one commit on its primary worktree."""
    repo = tmp_path / "primary"
    repo.mkdir()
    _git("init", "-b", "main", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test", cwd=repo)
    (repo / "README.md").write_text("seed\n")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "seed", cwd=repo)
    return repo


def test_canonical_root_primary_worktree(primary_repo: Path) -> None:
    server = _resolve_primary_worktree_root(primary_repo)
    client = canonical_workspace_root(primary_repo)
    assert server == primary_repo.resolve()
    assert client == primary_repo.resolve()
    assert server == client


def test_canonical_root_linked_worktree(primary_repo: Path, tmp_path: Path) -> None:
    linked = tmp_path / "linked"
    _git("worktree", "add", "-b", "feature/contract-test", str(linked), cwd=primary_repo)
    server = _resolve_primary_worktree_root(linked)
    client = canonical_workspace_root(linked)
    assert server == primary_repo.resolve()
    assert client == primary_repo.resolve()
    assert server == client


def test_canonical_root_detached_head(primary_repo: Path) -> None:
    head = subprocess.run(
        ["git", "-C", str(primary_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git("checkout", "--detach", head, cwd=primary_repo)
    server = _resolve_primary_worktree_root(primary_repo)
    client = canonical_workspace_root(primary_repo)
    assert server == primary_repo.resolve()
    assert client == primary_repo.resolve()
    assert server == client


def test_canonical_root_outside_git_returns_none(tmp_path: Path) -> None:
    """Both resolvers must agree to return ``None`` when not inside a git repo."""
    outside = tmp_path / "no-git"
    outside.mkdir()
    server = _resolve_primary_worktree_root(outside)
    client = canonical_workspace_root(outside)
    assert server is None
    assert client is None
