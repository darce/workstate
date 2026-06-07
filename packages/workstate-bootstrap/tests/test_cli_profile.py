"""CLI ``--profile`` flag for the install subcommand.

WORKSTATE-REF-56 implementation note flipped the CLI default profile back to ``all`` so a
no-argument ``workstate-bootstrap install`` materializes the full surface
set out of the box. The library ``install()`` API has always kept
``profile="all"`` as the default, so this slice aligns the two
layers. ``--profile minimal`` and ``--profile lifecycle`` remain
opt-in.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_install import (  # noqa: F401  -- reused fixture
    fake_remote_with_surfaces,
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


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "workstate_bootstrap", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=90,
    )


def test_install_cli_default_profile_is_all(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """WORKSTATE-REF-56 implementation note: default `workstate-bootstrap install` (no --profile)
    must materialize the broad surface set — per-agent generated
    surfaces, shared overlay symlinks, and the lifecycle hoist with the
    Makefile include block."""
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    result = _run_cli(
        "install",
        "--target", str(target),
        "--remote-url", url,
        "--remote-ref", ref,
        "--no-mcp-servers",
        "--no-enforce-required-surfaces",
    )
    assert result.returncode == 0, result.stderr

    # WORKSTATE-REF-02 implementation note cutover: the Copilot prompt surface is the only
    # per-agent generated dir under default (all). The cross-harness
    # skill + slash-command surfaces moved to the plugin tree.
    assert (target / ".github" / "prompts").is_dir()
    # Shared overlay symlink for hooks MUST be materialized.
    assert (target / "scripts" / "hooks").exists()
    # Lifecycle hoist MUST inject the Makefile include block.
    assert (target / "Makefile").is_file()
    makefile_text = (target / "Makefile").read_text()
    assert "WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE" in makefile_text

    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert manifest["profile"] == "all"
    surface_paths = {entry["path"] for entry in manifest["surfaces"]}
    assert ".github/prompts" in surface_paths


def test_install_cli_explicit_minimal_profile_skips_broad_surfaces(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """``--profile minimal`` remains opt-in after the implementation note default
    flip: no per-agent surfaces, no shared overlay symlinks, no
    Makefile lifecycle injection."""
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    result = _run_cli(
        "install",
        "--target", str(target),
        "--remote-url", url,
        "--remote-ref", ref,
        "--no-mcp-servers",
        "--no-enforce-required-surfaces",
        "--profile", "minimal",
    )
    assert result.returncode == 0, result.stderr

    assert not (target / ".github" / "prompts").exists()
    assert not (target / "scripts" / "hooks").exists()
    assert not (target / "Makefile").exists()
    assert not (target / "Makefile.d").exists()

    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert manifest["profile"] == "minimal"
    assert manifest["surfaces"] == []


def test_install_cli_lifecycle_profile_hoists_runner(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """`--profile lifecycle` must hoist Makefile.d/lifecycle.mk and the
    runner package, regardless of whether the fake remote happens to ship
    those paths (it doesn't — the hoist is no-op in that case but the
    Makefile include block must still be created)."""
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    result = _run_cli(
        "install",
        "--target", str(target),
        "--remote-url", url,
        "--remote-ref", ref,
        "--no-mcp-servers",
        "--no-enforce-required-surfaces",
        "--profile", "lifecycle",
    )
    assert result.returncode == 0, result.stderr

    # Even when the remote doesn't ship the lifecycle source paths, the
    # Makefile include block must still be created so the consumer can
    # later drop in the fragment manually.
    assert (target / "Makefile").exists()
    text = (target / "Makefile").read_text()
    assert "WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE" in text
    assert "-include Makefile.d/*.mk" in text


def test_install_cli_rejects_unknown_profile(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    result = _run_cli(
        "install",
        "--target", str(target),
        "--remote-url", url,
        "--remote-ref", ref,
        "--no-mcp-servers",
        "--profile", "frobnicate",
    )
    assert result.returncode != 0
    assert "frobnicate" in result.stderr or "frobnicate" in result.stdout


def test_install_cli_all_profile_preserves_legacy_surfaces(
    tmp_path: Path, fake_remote_with_surfaces: tuple[str, str]
) -> None:
    """Explicit `--profile all` must materialize the legacy broad-surface
    behavior so existing rehearsals keep working when opted-in."""
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    url, ref = fake_remote_with_surfaces

    result = _run_cli(
        "install",
        "--target", str(target),
        "--remote-url", url,
        "--remote-ref", ref,
        "--no-mcp-servers",
        "--profile", "all",
    )
    assert result.returncode == 0, result.stderr

    # WORKSTATE-REF-02 implementation note cutover: only the Copilot prompt surface remains
    # under the generator's per-agent contract. Shared surface symlinks
    # must still materialize under `all`.
    assert (target / ".github" / "prompts").is_dir()
    assert (target / "scripts" / "hooks").exists()
