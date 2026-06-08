"""implementation note ask G: every git hook resolves its guard scripts relative to the
hook directory (``GUARD_DIR``), not ``$REPO_ROOT``.

`post-checkout` already used the `HOOK_DIR`/`GUARD_DIR` pattern (see
`test_post_checkout_hook_guard_resolution.py`); `pre-push`, `post-merge`,
`post-rewrite`, `post-commit`, and `pre-commit` resolved guards via
`$REPO_ROOT/scripts/hooks/...`, which misses in nested-source / hoisted
consumer layouts where the git root is not the shared hook source.

Two guards:
- a cheap content floor: no shipped git hook references `$REPO_ROOT/scripts/hooks`;
- the real proof: drive each hook from a synthetic nested layout (git root ≠
  shared hook source) with a stubbed `python3` that records every guard path
  the hook resolves, and assert each resolves to an existing file.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
GIT_HOOK_DIR = PAYLOAD_ROOT / "scripts" / "hooks" / "git"

# hook -> (guard scripts it must resolve, argv after the hook path, stdin)
HOOK_SPECS: dict[str, tuple[tuple[str, ...], list[str], str]] = {
    "pre-push": (("check_main_clean.py", "check_branch_naming.py"), ["origin", "url"], "refs\n"),
    "post-merge": (("check_main_clean.py",), ["0"], ""),
    "post-rewrite": (("check_main_clean.py",), ["amend"], "rec\n"),
    "post-commit": (("_post_commit_refresh_sha.py", "check_main_clean.py"), [], ""),
    "pre-commit": (("check_branch_naming.py",), [], ""),
}

ALL_GUARDS = sorted({g for guards, _, _ in HOOK_SPECS.values() for g in guards})

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("sh") is None,
    reason="git/sh required to exec the hooks",
)


def test_no_git_hook_resolves_guards_via_repo_root() -> None:
    """Content floor: a guard resolved through ``$REPO_ROOT/scripts/hooks`` is
    the broken pattern. Every git hook must resolve via the hook directory."""
    offenders = []
    for hook in sorted(GIT_HOOK_DIR.iterdir()):
        if not hook.is_file():
            continue
        if "$REPO_ROOT/scripts/hooks" in hook.read_text(encoding="utf-8"):
            offenders.append(hook.name)
    assert not offenders, (
        "these git hooks still resolve guards via $REPO_ROOT/scripts/hooks "
        f"instead of $GUARD_DIR: {offenders}"
    )


def _build_nested_fixture(tmp_path: Path, hook_name: str) -> Path:
    """Git repo whose guard scripts live ONLY in a nested package subtree, so a
    hook resolving via $REPO_ROOT (the git top-level) deliberately misses."""
    repo = tmp_path / "repo"
    nested_hooks = repo / "packages" / "workstate-system" / "scripts" / "hooks"
    (nested_hooks / "git").mkdir(parents=True)
    for guard in ALL_GUARDS:
        (nested_hooks / guard).write_text("# stub guard\n", encoding="utf-8")
    shutil.copy2(GIT_HOOK_DIR / hook_name, nested_hooks / "git" / hook_name)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    return repo


def _fake_python3(tmp_path: Path) -> tuple[Path, Path, Path]:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    attempted = tmp_path / "attempted.log"
    missing = tmp_path / "missing.log"
    stub = bin_dir / "python3"
    stub.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$1" >> "$ATTEMPTED_LOG"\n'
        'if [ ! -f "$1" ]; then printf "%s\\n" "$1" >> "$MISSING_LOG"; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return bin_dir, attempted, missing


@pytest.mark.parametrize("hook_name", sorted(HOOK_SPECS))
def test_hook_resolves_guards_via_hook_dir(tmp_path: Path, hook_name: str) -> None:
    guards, argv, stdin = HOOK_SPECS[hook_name]
    repo = _build_nested_fixture(tmp_path, hook_name)
    hook = repo / "packages" / "workstate-system" / "scripts" / "hooks" / "git" / hook_name
    bin_dir, attempted_log, missing_log = _fake_python3(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "ATTEMPTED_LOG": str(attempted_log),
        "MISSING_LOG": str(missing_log),
    }
    result = subprocess.run(
        ["sh", str(hook), *argv],
        cwd=str(repo),
        env=env,
        input=stdin,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr

    attempted = (
        [Path(l).name for l in attempted_log.read_text().splitlines() if l.strip()]
        if attempted_log.exists()
        else []
    )
    missing = missing_log.read_text().splitlines() if missing_log.exists() else []
    assert not missing, (
        f"{hook_name} resolved guard scripts to non-existent paths (resolving via "
        f"$REPO_ROOT instead of the hook dir):\n" + "\n".join(missing)
    )
    assert set(guards).issubset(set(attempted)), (
        f"{hook_name} attempted {sorted(set(attempted))} but must reference {sorted(guards)}"
    )
