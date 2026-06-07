"""implementation note contract tests for the lifecycle git resolver.

Pure stdlib helpers wrap ``git`` invocations so the rest of the
runner can compute repo root, worktree path, current branch, HEAD,
merge-base against ``main``, dirty summary, linked-worktree
enumeration, and task-ref derivation without each handler reaching
into ``subprocess``. Each function returns ``None``/empty on failure;
no helper raises.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"


@pytest.fixture
def resolver():
    """Import the resolver module fresh per test (no cached env)."""
    sys.path.insert(0, str(LIFECYCLE_PKG))
    try:
        if "resolver" in sys.modules:
            del sys.modules["resolver"]
        import resolver as r  # type: ignore[import-not-found]

        return r
    finally:
        sys.path.remove(str(LIFECYCLE_PKG))


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit",
            "--allow-empty",
            "-m", "init",
            "-q",
        ],
        cwd=repo,
        check=True,
    )
    return repo


def test_repo_root_returns_toplevel_from_subdirectory(
    resolver, git_repo: Path
) -> None:
    nested = git_repo / "a" / "b"
    nested.mkdir(parents=True)
    assert resolver.repo_root(nested) == git_repo


def test_repo_root_returns_none_outside_repo(resolver, tmp_path: Path) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()
    assert resolver.repo_root(outside) is None


def test_current_branch_on_main(resolver, git_repo: Path) -> None:
    assert resolver.current_branch(git_repo) == "main"


def test_current_branch_after_checkout(resolver, git_repo: Path) -> None:
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-99-x"],
        check=True,
    )
    assert resolver.current_branch(git_repo) == "feature/WORKSTATE-99-x"


def test_head_sha_is_full_40_char(resolver, git_repo: Path) -> None:
    head = resolver.head_sha(git_repo)
    assert head is not None
    assert len(head) == 40
    assert head == _git(git_repo, "rev-parse", "HEAD")


def test_merge_base_against_main(resolver, git_repo: Path) -> None:
    main_sha = _git(git_repo, "rev-parse", "HEAD")
    subprocess.run(
        ["git", "-C", str(git_repo), "checkout", "-q", "-b", "feature/WORKSTATE-1-x"],
        check=True,
    )
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "--allow-empty", "-m", "branch-tip", "-q",
        ],
        check=True,
    )
    base = resolver.merge_base(git_repo, target="main")
    assert base == main_sha


def test_merge_base_missing_target_returns_none(
    resolver, git_repo: Path
) -> None:
    assert resolver.merge_base(git_repo, target="does-not-exist") is None


def test_dirty_summary_clean_repo(resolver, git_repo: Path) -> None:
    summary = resolver.dirty_summary(git_repo)
    assert summary == {
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "total": 0,
    }


def test_dirty_summary_counts_each_class(resolver, git_repo: Path) -> None:
    # Tracked + modified + staged + untracked.
    (git_repo / "tracked.txt").write_text("v1\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "tracked.txt"], check=True
    )
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "-c", "user.email=t@t",
            "-c", "user.name=t",
            "commit", "-m", "add tracked", "-q",
        ],
        check=True,
    )
    # 1 unstaged change to tracked.txt.
    (git_repo / "tracked.txt").write_text("v2\n")
    # 1 staged new file.
    (git_repo / "staged.txt").write_text("s\n")
    subprocess.run(
        ["git", "-C", str(git_repo), "add", "staged.txt"], check=True
    )
    # 1 untracked file.
    (git_repo / "untracked.txt").write_text("u\n")

    summary = resolver.dirty_summary(git_repo)
    assert summary["staged"] == 1
    assert summary["unstaged"] == 1
    assert summary["untracked"] == 1
    assert summary["total"] == 3


def test_linked_worktrees_lists_main_only(resolver, git_repo: Path) -> None:
    items = resolver.linked_worktrees(git_repo)
    assert len(items) == 1
    primary = items[0]
    assert primary["path"] == str(git_repo)
    assert primary["branch"] == "main"
    assert len(primary["head"]) == 40


def test_linked_worktrees_includes_added_worktree(
    resolver, git_repo: Path, tmp_path: Path
) -> None:
    extra = tmp_path / "wt-extra"
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-2-y", str(extra),
        ],
        check=True,
    )
    items = resolver.linked_worktrees(git_repo)
    paths = {item["path"]: item for item in items}
    assert str(extra) in paths
    assert paths[str(extra)]["branch"] == "feature/WORKSTATE-2-y"


def test_derive_task_ref_conforming(resolver) -> None:
    assert (
        resolver.derive_task_ref("feature/WORKSTATE-37-branch-naming-enforcement")
        == "WORKSTATE-REF-37"
    )


def test_derive_task_ref_maint_branch(resolver) -> None:
    assert resolver.derive_task_ref("feature/maint-dirty-br-01") == "WORKSTATE-REF-DIRTY-BR-01"


def test_derive_task_ref_main_branch_returns_none(resolver) -> None:
    assert resolver.derive_task_ref("main") is None


def test_derive_task_ref_non_conforming_returns_none(resolver) -> None:
    assert resolver.derive_task_ref("fix/foo") is None
    assert resolver.derive_task_ref("") is None
    assert resolver.derive_task_ref(None) is None


# ---------------------------------------------------------------------------
# WORKSTATE-REF-65: registered-ref selection
# ---------------------------------------------------------------------------


def test_derive_task_ref_picks_longest_registered_when_both_live(resolver) -> None:
    """When both ``WORKSTATE-REF-63`` and ``WORKSTATE-REF-63-FU-...`` are registered live
    refs, a ``-fu-`` branch must resolve to the follow-up, not the base.
    Regression for the 2026-05-17 incident where ``make context`` on
    ``feature/WORKSTATE-63-fu-tighten-compaction-defaults`` collapsed onto
    the unrelated ``WORKSTATE-REF-63`` row."""
    branch = "feature/WORKSTATE-63-fu-tighten-compaction-defaults"
    known = {"WORKSTATE-REF-63", "WORKSTATE-REF-63-FU-TIGHTEN-COMTASKCTION-DEFAULTS"}
    assert (
        resolver.derive_task_ref(branch, known_task_refs=known)
        == "WORKSTATE-REF-63-FU-TIGHTEN-COMTASKCTION-DEFAULTS"
    )


def test_derive_task_ref_picks_base_when_only_base_registered(resolver) -> None:
    """With only the base registered, no longer candidate intersects so
    the base wins. Locks in the ``feature/WORKSTATE-37-...`` -> ``WORKSTATE-REF-37``
    happy path."""
    branch = "feature/WORKSTATE-37-branch-naming-enforcement"
    assert (
        resolver.derive_task_ref(branch, known_task_refs={"WORKSTATE-REF-37"})
        == "WORKSTATE-REF-37"
    )


def test_derive_task_ref_done_status_excluded_at_read_boundary(resolver) -> None:
    """``known_task_refs`` is sourced via ``status_filter=LIVE_ACTIVE_STATUSES``
    at the caller boundary; a ``done``-status ref is therefore absent from
    the set. The selector sees no intersection on the long candidate and
    falls back through to the base (which is in the set). Documents that
    archived/done base refs cannot steal a follow-up resolution."""
    branch = "feature/WORKSTATE-63-fu-tighten-compaction-defaults"
    # WORKSTATE-REF-63-FU-... has status=done so it was filtered out at the read
    # boundary; only WORKSTATE-REF-63 remains.
    known = {"WORKSTATE-REF-63"}
    assert (
        resolver.derive_task_ref(branch, known_task_refs=known) == "WORKSTATE-REF-63"
    )


def test_derive_task_ref_empty_registry_uses_shortest_prefix(resolver) -> None:
    """Empty / ``None`` ``known_task_refs`` must produce today's
    shortest-prefix behavior byte-for-byte (degraded-environment lock)."""
    branch = "feature/WORKSTATE-63-fu-tighten-compaction-defaults"
    assert (
        resolver.derive_task_ref(branch, known_task_refs=None) == "WORKSTATE-REF-63"
    )
    assert (
        resolver.derive_task_ref(branch, known_task_refs=set()) == "WORKSTATE-REF-63"
    )
    # Positional/default invocation (today's signature) must still work.
    assert resolver.derive_task_ref(branch) == "WORKSTATE-REF-63"


def test_derive_task_ref_no_intersection_returns_none(resolver) -> None:
    """A non-empty but unrelated registry (e.g., the worktree's handoff
    DB only knows WORKSTATE-REF-* tasks) must return ``None`` — the resolver
    must not name a candidate absent from a populated registry
    (WORKSTATE65-BR-02). Shortest-prefix fallback applies only when no
    registry context is available at all (None / empty)."""
    branch = "feature/WORKSTATE-63-fu-tighten-compaction-defaults"
    assert (
        resolver.derive_task_ref(
            branch, known_task_refs={"WORKSTATE-REF-02", "WORKSTATE-REF-99"}
        )
        is None
    )


def test_derive_task_ref_maint_branch_unchanged_with_registry(resolver) -> None:
    """Single-segment maint refs (``WORKSTATE-REF-DIRTY-BR-01``) have only one
    candidate; behavior is unchanged with or without registry context."""
    branch = "feature/maint-dirty-br-01"
    assert (
        resolver.derive_task_ref(
            branch, known_task_refs={"WORKSTATE-REF-DIRTY-BR-01"}
        )
        == "WORKSTATE-REF-DIRTY-BR-01"
    )
    assert resolver.derive_task_ref(branch) == "WORKSTATE-REF-DIRTY-BR-01"


def test_derive_task_ref_case_insensitive_registry_intersection(resolver) -> None:
    """The selector normalizes case on both sides so a lowercase /
    mixed-case registry entry still intersects with the lowercase
    candidate list."""
    branch = "feature/WORKSTATE-63-fu-example"
    assert (
        resolver.derive_task_ref(
            branch, known_task_refs={"WORKSTATE-63-fu-example"}
        )
        == "WORKSTATE-REF-63-FU-EXAMPLE"
    )


def test_derive_task_ref_non_conforming_with_registry_returns_none(resolver) -> None:
    """A non-conforming branch produces no candidates; registry context
    is irrelevant — ``None`` regardless."""
    assert resolver.derive_task_ref("fix/foo", known_task_refs={"WORKSTATE-REF-1"}) is None
    assert resolver.derive_task_ref("", known_task_refs={"WORKSTATE-REF-1"}) is None
    assert resolver.derive_task_ref(None, known_task_refs={"WORKSTATE-REF-1"}) is None


def test_format_branch_name_uppercase_task_ref(resolver) -> None:
    assert resolver.format_branch_name("WORKSTATE-REF-40") == "feature/WORKSTATE-40"


def test_format_branch_name_with_slug(resolver) -> None:
    assert (
        resolver.format_branch_name("WORKSTATE-REF-40", slug="Worktree-Creation")
        == "feature/WORKSTATE-40-worktree-creation"
    )


def test_format_branch_name_empty_returns_none(resolver) -> None:
    assert resolver.format_branch_name("") is None
    assert resolver.format_branch_name(None) is None


def test_canonical_workspace_root_in_primary_returns_primary(
    resolver, git_repo: Path
) -> None:
    assert resolver.canonical_workspace_root(git_repo) == git_repo


def test_canonical_workspace_root_from_linked_worktree_returns_primary(
    resolver, git_repo: Path, tmp_path: Path
) -> None:
    """A linked worktree must resolve back to the primary repo path so
    the lifecycle adapter targets the canonical handoff state and not
    the worktree's local ``.task-state``. Regression for
    BR-WORKSTATE40-S2-01 / S2-02."""
    extra = tmp_path / "wt-link"
    subprocess.run(
        [
            "git", "-C", str(git_repo),
            "worktree", "add", "-q", "-b", "feature/WORKSTATE-3-r", str(extra),
        ],
        check=True,
    )
    assert resolver.canonical_workspace_root(extra) == git_repo
