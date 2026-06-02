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
