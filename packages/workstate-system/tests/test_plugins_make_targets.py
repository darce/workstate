"""WORKSTATE-REF-01 implementation note: ``make plugins-build`` / ``make plugins-check`` targets.

ADR-001 documents the operator-facing contract as ``make plugins-build &&
make plugins-check``. This module pins those Make targets end to end:
``plugins-build`` writes the same generated base plugin tree that bootstrap
marketplace pins reference by default, ``plugins-check`` re-runs the generator
with ``--check`` against that tree and exits zero when there is no drift and
non-zero when the freshly emitted tree has been hand-mutated.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]


def _run_make(target: str, *, plugin_out: Path | None = None) -> subprocess.CompletedProcess[str]:
    # The workstate-system fragments are pulled into the repo-root Makefile
    # via `-include packages/workstate-system/Makefile.d/*.mk`, so plugin
    # targets must be invoked from the repo root.
    env = os.environ.copy()
    if plugin_out is not None:
        env["PLUGINS_DIST_ROOT"] = str(plugin_out)
    return subprocess.run(
        ["make", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


@pytest.fixture
def isolated_plugin_root(tmp_path: Path) -> Path:
    return tmp_path / "generated" / "plugins" / "workstate-system" / "base"


def test_plugins_build_emits_per_harness_trees(isolated_plugin_root: Path) -> None:
    """``make plugins-build`` writes both harness trees."""
    proc = _run_make("plugins-build", plugin_out=isolated_plugin_root)
    assert proc.returncode == 0, (
        f"`make plugins-build` failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert (isolated_plugin_root / "claude" / ".claude-plugin" / "plugin.json").is_file()
    assert (isolated_plugin_root / "claude" / ".mcp.json").is_file()
    assert (isolated_plugin_root / "codex" / ".codex-plugin" / "plugin.json").is_file()
    assert (isolated_plugin_root / "codex" / ".mcp.json").is_file()


def test_plugins_check_passes_when_tree_matches(isolated_plugin_root: Path) -> None:
    """After ``make plugins-build``, ``make plugins-check`` must exit 0."""
    build = _run_make("plugins-build", plugin_out=isolated_plugin_root)
    assert build.returncode == 0, build.stderr

    check = _run_make("plugins-check", plugin_out=isolated_plugin_root)
    assert check.returncode == 0, (
        f"`make plugins-check` failed against a fresh tree: "
        f"stdout={check.stdout!r} stderr={check.stderr!r}"
    )


def test_plugins_check_fails_when_tree_is_mutated(isolated_plugin_root: Path) -> None:
    """If a plugin tree file is hand-edited after build, ``make plugins-check``
    must surface drift and exit non-zero."""
    build = _run_make("plugins-build", plugin_out=isolated_plugin_root)
    assert build.returncode == 0, build.stderr

    mutated = isolated_plugin_root / "claude" / ".mcp.json"
    original = mutated.read_text()
    mutated.write_text(original.replace("uvx", "uvy"))

    check = _run_make("plugins-check", plugin_out=isolated_plugin_root)
    assert check.returncode != 0, (
        "`make plugins-check` must detect hand-edited drift; "
        f"stdout={check.stdout!r} stderr={check.stderr!r}"
    )


def test_plugins_build_default_matches_bootstrap_marketplace_root() -> None:
    """The default output root must stay aligned with bootstrap's pins."""
    proc = subprocess.run(
        ["make", "--dry-run", "plugins-build"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert ".agentic/generated/plugins/workstate-system/base" in proc.stdout
