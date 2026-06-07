"""TDD gate for implementation note Slice S0: pure worktree / overlay-root detection.

The ``workstate_bootstrap.worktree`` module provides three pure helpers used by
the linked-worktree self-heal (``adopt-worktree``) capability:

1. ``is_linked_worktree(path)`` — True only for a git *linked* worktree
   (``git worktree add``). The primary worktree, a bare repo, and a detached
   primary all return False; a detached *linked* worktree still returns True.
2. ``primary_overlay_root(path)`` — resolve the primary overlay root by marker
   (``.workstate-bootstrap.json``), searching upward from ``parent(--git-common-dir)``.
   It must NOT assume the overlay sits at the git root (nested-source layouts put
   the git repo *inside* the overlay), and must fail loudly when no marker exists.
3. ``overlay_is_materialized(root)`` — True when ``root`` carries both the marker
   and the ``.workstate/remote`` clone.

All layouts are built with real ``git`` against ``tmp_path`` so the behavior is
exercised end to end and offline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from workstate_bootstrap.worktree import (
    NotAGitRepositoryError,
    OverlayMarkerNotFoundError,
    is_linked_worktree,
    overlay_is_materialized,
    primary_overlay_root,
)

MARKER = ".workstate-bootstrap.json"


# ---------------------------------------------------------------------------
# Fixtures / layout builders (real git)
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    )
    return result.stdout.strip()


def _write_overlay(root: Path) -> None:
    """Materialize a minimal overlay: marker + ``.workstate/remote`` clone dir."""
    (root / MARKER).write_text("{}\n")
    (root / ".workstate" / "remote").mkdir(parents=True, exist_ok=True)


def _make_primary(root: Path, *, with_overlay: bool = True) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "seed.txt").write_text("seed\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "seed", cwd=root)
    if with_overlay:
        _write_overlay(root)
    return root


def _add_linked_worktree(primary: Path, wt: Path, *, detach: bool = False) -> Path:
    if detach:
        _git("worktree", "add", "--detach", str(wt), "HEAD", cwd=primary)
    else:
        _git("worktree", "add", str(wt), cwd=primary)
    return wt


# ---------------------------------------------------------------------------
# is_linked_worktree
# ---------------------------------------------------------------------------


def test_primary_is_not_linked(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary")
    assert is_linked_worktree(primary) is False


def test_linked_worktree_is_linked(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary")
    wt = _add_linked_worktree(primary, tmp_path / "wt")
    assert is_linked_worktree(wt) is True


def test_bare_repo_is_not_linked(tmp_path: Path) -> None:
    bare = tmp_path / "bare.git"
    _git("init", "--bare", str(bare), cwd=tmp_path)
    assert is_linked_worktree(bare) is False


def test_detached_primary_is_not_linked(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary")
    _git("checkout", "--detach", cwd=primary)
    assert is_linked_worktree(primary) is False


def test_detached_linked_worktree_is_linked(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary")
    wt = _add_linked_worktree(primary, tmp_path / "wt", detach=True)
    assert is_linked_worktree(wt) is True


def test_non_git_path_raises(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(NotAGitRepositoryError):
        is_linked_worktree(plain)


# ---------------------------------------------------------------------------
# primary_overlay_root
# ---------------------------------------------------------------------------


def test_overlay_root_at_primary_git_root(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary")
    assert primary_overlay_root(primary).resolve() == primary.resolve()


def test_overlay_root_resolves_primary_from_linked_worktree(tmp_path: Path) -> None:
    """From a bare linked worktree, the overlay root is the PRIMARY, not the wt."""
    primary = _make_primary(tmp_path / "primary")
    wt = _add_linked_worktree(primary, tmp_path / "wt")
    # The linked worktree carries no marker of its own.
    assert not (wt / MARKER).exists()
    assert primary_overlay_root(wt).resolve() == primary.resolve()


def test_overlay_root_searches_upward_for_nested_source_layout(tmp_path: Path) -> None:
    """Nested-source: git repo lives INSIDE the overlay; marker is an ancestor.

    Resolution must not assume the marker sits at the git root — it walks upward
    from ``parent(--git-common-dir)`` until it finds ``.workstate-bootstrap.json``.
    """
    overlay = tmp_path / "outer"
    overlay.mkdir()
    _write_overlay(overlay)
    repo = _make_primary(overlay / "repo", with_overlay=False)
    assert not (repo / MARKER).exists()
    assert primary_overlay_root(repo).resolve() == overlay.resolve()


def test_overlay_root_skips_unmaterialized_stray_ancestor_marker(
    tmp_path: Path,
) -> None:
    """A stray marker (marker file but NO clone) closer than the real overlay
    must not win: the upward walk prefers a *materialized* overlay, skipping an
    unmaterialized stray marker (revA-overlay-root-unbounded-walk).

    Layout (the walk goes UPWARD from the git root):
      real_overlay/            <- materialized (marker + .workstate/remote)
      real_overlay/stray/      <- stray marker only, NO clone (unmaterialized)
      real_overlay/stray/repo/ <- nested-source git repo (no own marker)
    """
    real_overlay = tmp_path / "real_overlay"
    real_overlay.mkdir()
    _write_overlay(real_overlay)
    stray = real_overlay / "stray"
    stray.mkdir()
    (stray / MARKER).write_text("{}\n")  # marker only — not materialized
    repo = _make_primary(stray / "repo", with_overlay=False)
    assert not (repo / MARKER).exists()
    # Old behavior returned the nearest marker (the stray) — a silent
    # mis-resolution; the materialized overlay one level up must win instead.
    assert primary_overlay_root(repo).resolve() == real_overlay.resolve()


def test_overlay_root_returns_nearest_marker_when_none_materialized(
    tmp_path: Path,
) -> None:
    """Fallback: when a marker exists but NO overlay at/above it is materialized,
    resolution still returns the nearest marker so the caller surfaces the
    specific 'not materialized' error rather than a misleading 'no marker'."""
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / MARKER).write_text("{}\n")  # marker only, no clone
    repo = _make_primary(overlay / "repo", with_overlay=False)
    assert primary_overlay_root(repo).resolve() == overlay.resolve()
    assert overlay_is_materialized(primary_overlay_root(repo)) is False


def test_overlay_root_fails_loudly_when_no_marker(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary", with_overlay=False)
    with pytest.raises(OverlayMarkerNotFoundError):
        primary_overlay_root(primary)


# ---------------------------------------------------------------------------
# overlay_is_materialized
# ---------------------------------------------------------------------------


def test_overlay_is_materialized_true_when_marker_and_clone_present(
    tmp_path: Path,
) -> None:
    primary = _make_primary(tmp_path / "primary")
    assert overlay_is_materialized(primary) is True


def test_overlay_is_materialized_false_without_marker(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary", with_overlay=False)
    assert overlay_is_materialized(primary) is False


def test_overlay_is_materialized_false_when_clone_missing(tmp_path: Path) -> None:
    primary = _make_primary(tmp_path / "primary", with_overlay=False)
    (primary / MARKER).write_text("{}\n")  # marker present, clone absent
    assert overlay_is_materialized(primary) is False


# ---------------------------------------------------------------------------
# git-absent typed-error contract
# ---------------------------------------------------------------------------


def test_git_absent_raises_not_a_git_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When git is not on PATH (FileNotFoundError), helpers must raise the typed
    NotAGitRepositoryError, not leak a raw FileNotFoundError."""
    import workstate_bootstrap.worktree as wt_mod

    def _git_absent(*_args: object, **_kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(wt_mod.subprocess, "run", _git_absent)
    with pytest.raises(NotAGitRepositoryError):
        is_linked_worktree(tmp_path)


# ---------------------------------------------------------------------------
# implementation note S4 follow-up — cross-package literal pin
# (revD-gate-bootstrap-literal-drift)
# ---------------------------------------------------------------------------


def test_overlay_contract_literals_match_task_start_gate() -> None:
    """workstate-system's task_start._adopt_overlay gate re-implements the
    overlay-root walk WITHOUT importing workstate-bootstrap (the inverse-
    dependency invariant), hardcoding the marker filename and the
    ``.workstate/remote`` clone path. Pin those literals to bootstrap's
    canonical constants so a future rename here trips a red test rather than
    silently desyncing the gate from the bootstrap CLI.
    """
    from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, CLONE_SUBDIR

    # These are the literals task_start.py depends on; keep them in lockstep.
    assert BOOTSTRAP_MANIFEST_NAME == ".workstate-bootstrap.json"
    assert CLONE_SUBDIR == (".workstate", "remote")
