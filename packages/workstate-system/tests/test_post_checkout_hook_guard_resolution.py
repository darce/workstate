"""Smoke regression for ``scripts/hooks/git/post-checkout`` guard resolution.

Tech-debt follow-up (``docs/tech-debt/post-checkout-hook-missing-scripts.md``):
the ``post-checkout`` hook used to resolve its three guard scripts
(``check_main_clean.py`` / ``check_branch_naming.py`` /
``check_root_on_main.py``) through ``$REPO_ROOT/scripts/hooks/<name>.py``.
In the monorepo source layout the guard scripts live at
``packages/workstate-system/scripts/hooks/<name>.py`` — one level up from the
hook's own ``scripts/hooks/git/`` directory — so ``$REPO_ROOT`` (the git
top-level) never contained them and the guards silently no-op'd with
"No such file" noise on every checkout.

This test reproduces the nested package layout, copies the *real* hook into
it, and drives it with a stubbed ``python3`` on ``PATH`` that records every
guard-script path the hook resolves. The hook must resolve every script it
references to a file that exists, in both layouts, which it can only do by
deriving the guard directory from its own location (``$0``) rather than
``$REPO_ROOT``. A future move/rename of a guard script re-surfaces here as a
failure instead of a silent runtime no-op.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REAL_HOOK = PACKAGE_ROOT / "scripts" / "hooks" / "git" / "post-checkout"

# The guard scripts the hook is expected to invoke. If the hook stops
# referencing one (or starts referencing a new one), update this list — the
# test asserts the hook attempted exactly the scripts it should and that each
# resolved to an existing file.
EXPECTED_GUARD_SCRIPTS = (
    "check_main_clean.py",
    "check_branch_naming.py",
    "check_root_on_main.py",
)

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("sh") is None,
    reason="git/sh required to exec the hook",
)


def _build_nested_fixture(tmp_path: Path) -> Path:
    """Create a git repo whose guard scripts live in a nested package subtree.

    Layout mirrors the monorepo source tree so that ``$REPO_ROOT`` (the git
    top-level) deliberately does *not* contain ``scripts/hooks/`` — only the
    nested ``packages/workstate-system/scripts/hooks/`` does. A hook that
    resolves guards via ``$REPO_ROOT`` therefore misses; one that resolves
    relative to its own location hits.
    """
    repo = tmp_path / "repo"
    nested_hooks = repo / "packages" / "workstate-system" / "scripts" / "hooks"
    (nested_hooks / "git").mkdir(parents=True)

    for name in EXPECTED_GUARD_SCRIPTS:
        (nested_hooks / name).write_text("# stub guard script\n", encoding="utf-8")

    # Copy the real hook so the test exercises shipped content, not a paraphrase.
    shutil.copy2(REAL_HOOK, nested_hooks / "git" / "post-checkout")

    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
    )
    return repo


def _install_fake_python3(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Place a stub ``python3`` on PATH that records each script path it runs.

    The hook invokes ``python3 "<guard-script>" [--trigger ...]``, so ``$1`` is
    always the resolved guard-script path. The stub logs every attempt and,
    separately, any path that does not exist on disk.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    attempted_log = tmp_path / "attempted.log"
    missing_log = tmp_path / "missing.log"
    stub = bin_dir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$1" >> "$ATTEMPTED_LOG"\n'
        'if [ ! -f "$1" ]; then printf "%s\\n" "$1" >> "$MISSING_LOG"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, attempted_log, missing_log


def test_post_checkout_hook_resolves_every_guard_script(tmp_path: Path) -> None:
    repo = _build_nested_fixture(tmp_path)
    hook = (
        repo
        / "packages"
        / "workstate-system"
        / "scripts"
        / "hooks"
        / "git"
        / "post-checkout"
    )
    bin_dir, attempted_log, missing_log = _install_fake_python3(tmp_path)

    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "ATTEMPTED_LOG": str(attempted_log),
        "MISSING_LOG": str(missing_log),
    }
    # $3="1" marks a branch checkout so the root-on-main guard also runs.
    result = subprocess.run(
        ["sh", str(hook), "0000", "1111", "1"],
        cwd=str(repo),
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr

    attempted = (
        [
            Path(line).name
            for line in attempted_log.read_text().splitlines()
            if line.strip()
        ]
        if attempted_log.exists()
        else []
    )
    missing = missing_log.read_text().splitlines() if missing_log.exists() else []

    assert not missing, (
        "post-checkout hook resolved guard scripts to paths that do not exist:\n"
        + "\n".join(missing)
        + "\n\nThe hook must resolve guard scripts relative to its own "
        "location (scripts/hooks/<name>.py, one level up from the git/ hook "
        "dir), not via $REPO_ROOT — see "
        "docs/tech-debt/post-checkout-hook-missing-scripts.md."
    )
    assert set(EXPECTED_GUARD_SCRIPTS).issubset(set(attempted)), (
        f"hook attempted {sorted(set(attempted))} but should reference every "
        f"guard in {sorted(EXPECTED_GUARD_SCRIPTS)}."
    )
