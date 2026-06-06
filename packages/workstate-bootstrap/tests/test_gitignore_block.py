"""TDD gate for implementation note Slice S4: managed consumer ``.gitignore`` block.

An installed/adopted overlay materializes the runtime dir (``.workstate``) and a
set of surface SYMLINKS (``Makefile.d``, ``scripts/hooks`` …). Without ignore
rules these show as untracked, so ``git status`` is never clean. A managed,
sentinel-delimited block (mirroring the Makefile-include pattern) keeps it clean
without clobbering a user-authored ``.gitignore``.

M1: the patterns must be ROOT-ANCHORED and NON-trailing-slash — a trailing-slash
pattern matches only directories, but git treats a symlink as a non-directory,
so ``Makefile.d/`` would miss an adopted ``Makefile.d`` symlink.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from workstate_bootstrap.install import (
    GITIGNORE_SENTINEL_BEGIN,
    _consumer_gitignore_entries,
    _ensure_consumer_gitignore_block,
)


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


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.email", "t@e.com", cwd=root)
    _git("config", "user.name", "T", cwd=root)


def _is_ignored(repo: Path, rel: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", "--", rel], timeout=30
        ).returncode
        == 0
    )


def test_block_created_when_no_gitignore(tmp_path: Path) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    result = _ensure_consumer_gitignore_block(target)
    assert result == {"path": ".gitignore", "action": "created"}
    text = (target / ".gitignore").read_text()
    assert GITIGNORE_SENTINEL_BEGIN in text
    assert "/.workstate" in text
    assert "/Makefile.d" in text


def test_block_appended_preserves_user_content(tmp_path: Path) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    (target / ".gitignore").write_text("# user\n*.log\nbuild/\n")
    result = _ensure_consumer_gitignore_block(target)
    assert result == {"path": ".gitignore", "action": "appended"}
    text = (target / ".gitignore").read_text()
    assert "*.log" in text and "build/" in text  # user content preserved
    assert GITIGNORE_SENTINEL_BEGIN in text


def test_block_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    _ensure_consumer_gitignore_block(target)
    result = _ensure_consumer_gitignore_block(target)
    assert result == {"path": ".gitignore", "action": "already_present"}
    # No duplicate block.
    assert (target / ".gitignore").read_text().count(GITIGNORE_SENTINEL_BEGIN) == 1


def test_block_ignores_symlink_surfaces_git_status_clean(tmp_path: Path) -> None:
    """M1 regression: the block must ignore adopted SYMLINK surfaces + .workstate
    so `git status` shows nothing but the .gitignore itself."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    external = tmp_path / "external"
    external.mkdir()

    # Materialize the overlay shape: a real .workstate dir with a child symlink,
    # plus whole-dir surface symlinks (as adoption produces).
    (repo / ".workstate").mkdir()
    (repo / ".workstate" / "remote").symlink_to(external)
    (repo / "Makefile.d").symlink_to(external)
    (repo / "scripts").mkdir()
    (repo / "scripts" / "hooks").symlink_to(external)
    (repo / ".github").mkdir()
    (repo / ".github" / "hooks").symlink_to(external)

    _ensure_consumer_gitignore_block(repo)

    status_lines = [
        line
        for line in _git("status", "--porcelain", cwd=repo).splitlines()
        if line.strip()
    ]
    # Only the .gitignore itself may be untracked; no overlay path leaks.
    assert all(".gitignore" in line for line in status_lines), status_lines
    for leaked in (".workstate", "Makefile.d", "scripts/hooks", ".github/hooks"):
        assert all(leaked not in line for line in status_lines), (leaked, status_lines)


# ---------------------------------------------------------------------------
# Self-hosting source repo guard: the workstate monorepo IS the overlay source,
# so its surfaces are tracked (or already ignored by a hand-authored .gitignore).
# Appending the managed block there dirties every feature worktree's TRACKED
# .gitignore — and would ignore the repo's own tracked source. The block must be
# skipped when the consumer already manages every managed surface itself.
# ---------------------------------------------------------------------------


# Managed entries that name overlay *surfaces* (not the runtime .workstate /
# .task-state dirs). These are the paths a self-hosting repo tracks as real
# source and that the root-anchored block would dangerously ignore.
_TRACKED_SURFACE_DIRS = (
    "scripts/hooks",
    "Makefile.d",
    "scripts/workstate",
    "docs/workstate/contracts",
    "docs/workstate/rules",
    ".github/hooks",
    ".github/prompts",
)


def test_block_skipped_when_surfaces_are_tracked_source(tmp_path: Path) -> None:
    """Footgun guard: when the overlay surfaces are TRACKED source dirs (the
    self-hosting monorepo), the managed block must NOT be written — its
    root-anchored ``/scripts/hooks`` … patterns would start ignoring the repo's
    own version-controlled source."""
    repo = tmp_path / "monorepo"
    _init_repo(repo)
    # Track real source at every managed surface path.
    for rel in _TRACKED_SURFACE_DIRS:
        d = repo / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "source.py").write_text("# real tracked overlay source\n")
    # WS-HOOKGEN-01: the Codex hook config is a managed file surface; the
    # self-hosting repo tracks it as a generated golden.
    codex_hooks = repo / ".codex" / "hooks.json"
    codex_hooks.parent.mkdir(parents=True, exist_ok=True)
    codex_hooks.write_text("{}\n")
    # The repo still ignores its runtime dirs itself (as the live monorepo does).
    (repo / ".gitignore").write_text(".workstate/\n.task-state/\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "tracked overlay source", cwd=repo)

    result = _ensure_consumer_gitignore_block(repo)

    assert result == {"path": ".gitignore", "action": "skipped_self_managed"}
    assert GITIGNORE_SENTINEL_BEGIN not in (repo / ".gitignore").read_text()
    # The footgun must not occur: tracked source must never become ignored.
    for rel in _TRACKED_SURFACE_DIRS:
        assert not _is_ignored(repo, rel), f"tracked source {rel} must not be ignored"
    # Sanity: every tracked surface still appears in git's tracked set.
    tracked = _git("ls-files", cwd=repo).splitlines()
    assert any(line.startswith("scripts/hooks/") for line in tracked)


def test_block_skipped_when_surfaces_already_ignored(tmp_path: Path) -> None:
    """The self-hosting monorepo ships a hand-authored ``.gitignore`` that already
    ignores the overlay surfaces (some with directory-only ``dir/`` patterns). The
    managed block must not be appended as a redundant duplicate that dirties every
    feature worktree's tracked ``.gitignore``."""
    repo = tmp_path / "monorepo"
    _init_repo(repo)
    # Mirror the live monorepo: a mix of root-anchored and directory-only
    # (trailing-slash) patterns covering every managed entry.
    hand_authored = (
        ".workstate/\n"
        ".task-state/\n"
        "/scripts/hooks\n"
        "/.github/hooks\n"
        "/docs/workstate/contracts\n"
        "/docs/workstate/rules\n"
        "/Makefile.d\n"
        "/scripts/workstate\n"
        "/.github/prompts/\n"
        "/.codex/hooks.json\n"
    )
    (repo / ".gitignore").write_text(hand_authored)
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-m", "hand-authored overlay ignores", cwd=repo)
    before = (repo / ".gitignore").read_text()

    result = _ensure_consumer_gitignore_block(repo)

    assert result == {"path": ".gitignore", "action": "skipped_self_managed"}
    assert (repo / ".gitignore").read_text() == before  # untouched, no duplicate
    assert GITIGNORE_SENTINEL_BEGIN not in before
    # Idempotent: a second pass is still a clean skip, never an append.
    assert _ensure_consumer_gitignore_block(repo)["action"] == "skipped_self_managed"


def test_block_written_when_any_surface_leaks(tmp_path: Path) -> None:
    """A real external consumer (surfaces freshly materialized, untracked, not yet
    ignored) still gets the block — the self-managed guard only fires when every
    managed surface is already tracked or ignored."""
    repo = tmp_path / "consumer"
    _init_repo(repo)
    # Ignore the runtime dirs but leave the surfaces leaking (as before install).
    (repo / ".gitignore").write_text(".workstate/\n.task-state/\n")
    _git("add", ".gitignore", cwd=repo)
    _git("commit", "-m", "runtime ignores only", cwd=repo)

    result = _ensure_consumer_gitignore_block(repo)

    assert result["action"] == "appended"
    assert GITIGNORE_SENTINEL_BEGIN in (repo / ".gitignore").read_text()


def test_mixed_self_host_never_ignores_tracked_surface(tmp_path: Path) -> None:
    """MIXED self-host footgun gate: one surface is TRACKED source, another is a
    still-leaking materialized symlink. The block must be written for the leaking
    surface but must NEVER ignore the tracked surface — emitting a root-anchored
    ``/scripts/hooks`` ignore over tracked source silently makes it un-trackable.
    Per-entry filtering (not all-or-nothing) is what closes this."""
    repo = tmp_path / "monorepo"
    _init_repo(repo)
    # Tracked source at one surface; runtime dirs ignored by hand-authored config.
    (repo / "scripts" / "hooks").mkdir(parents=True)
    (repo / "scripts" / "hooks" / "src.py").write_text("# tracked overlay source\n")
    (repo / ".gitignore").write_text(".workstate/\n.task-state/\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "tracked scripts/hooks + runtime ignores", cwd=repo)
    # A genuinely-leaking surface: a materialized symlink, untracked + unignored.
    external = tmp_path / "external"
    external.mkdir()
    (repo / "Makefile.d").symlink_to(external)

    result = _ensure_consumer_gitignore_block(repo)

    # A block IS written (the leaking surface needs ignoring)...
    assert result["action"] == "appended"
    text = (repo / ".gitignore").read_text()
    assert GITIGNORE_SENTINEL_BEGIN in text
    # ...but the tracked surface must NOT appear in it / must stay trackable.
    assert "/scripts/hooks" not in text
    assert not _is_ignored(repo, "scripts/hooks")
    # ...while the leaking surface is now ignored.
    assert "/Makefile.d" in text
    assert _is_ignored(repo, "Makefile.d")


def test_entries_helper_covers_surface_dirs() -> None:
    """Pin: the surfaces the self-managed guard inspects are exactly the managed
    entries, so a future surface addition is not silently excluded from the guard.
    """
    entries = {e.lstrip("/") for e in _consumer_gitignore_entries()}
    for rel in _TRACKED_SURFACE_DIRS:
        assert rel in entries, rel
