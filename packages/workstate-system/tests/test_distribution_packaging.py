"""WS-PKG-DELIVERY-01 implementation note: workstate-system is a buildable distribution.

The overlay payload (skills, generator, agent-workflows config, shared
surfaces) must ship inside the ``workstate-system`` wheel under an importable
``workstate_system`` namespace, while internal-only material (evals, internal
docs) is excluded. This pins the include/exclude contract that the package
delivery source (implementation note) materializes from.
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

PKG_DIR = Path(__file__).resolve().parents[1]  # packages/workstate-system


def _build_wheel(out_dir: Path) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available to build the wheel")
    proc = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(out_dir), str(PKG_DIR)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    wheels = list(out_dir.glob("*.whl"))
    assert len(wheels) == 1, [w.name for w in wheels]
    return wheels[0]


def _names(out_dir: Path) -> list[str]:
    with zipfile.ZipFile(_build_wheel(out_dir)) as zf:
        return zf.namelist()


def test_wheel_includes_overlay_payload(tmp_path: Path) -> None:
    names = _names(tmp_path)
    assert any(
        n.startswith("workstate_system/skills/") and n.endswith("/body.md")
        for n in names
    ), "skills source must ship"
    assert "workstate_system/scripts/generate_agent_workflows.py" in names, "generator must ship"
    assert (
        "workstate_system/config/agent-workflows/portable_commands.json" in names
    ), "agent-workflows manifest must ship"
    assert any(
        n.startswith("workstate_system/scripts/hooks/") for n in names
    ), "shared hook surface must ship"
    assert any(
        n.startswith("workstate_system/docs/workstate/contracts/") for n in names
    ), "contracts surface must ship"


def test_wheel_excludes_evals_and_internal_docs(tmp_path: Path) -> None:
    names = _names(tmp_path)
    forbidden = (
        "/config/evals/",
        "/tests/evals/",
        "/scripts/workstate/evals/",
        "/docs/tasks/",
        "/docs/specs/",
        "Makefile.d/evals.mk",
    )
    bad = [n for n in names for token in forbidden if token in n]
    assert not bad, f"internal-only paths leaked into the wheel: {bad}"


def test_sdist_to_wheel_roundtrip(tmp_path: Path) -> None:
    """PyPI (and ``uv build`` by default) builds the wheel FROM the sdist, not
    from the source tree. ``_build_wheel`` above uses ``--wheel`` (direct from
    source), which masks dangling-symlink / missing-file gaps in the sdist.
    This exercises the real publish path: build sdist, then wheel from it."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available to build")
    proc = subprocess.run(
        [uv, "build", "--out-dir", str(tmp_path), str(PKG_DIR)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    assert list(tmp_path.glob("*.tar.gz")), "sdist not built"
    assert list(tmp_path.glob("*.whl")), "wheel-from-sdist not built"


def test_no_symlinks_in_force_included_payload() -> None:
    """A standalone sdist must not contain symlinks that point outside the
    package: they dangle when the sdist is unpacked and break the wheel build.
    Guards the whole bug class, not just branch-review-guide.md."""
    payload_roots = ["skills", "scripts", "config", "docs/workstate", "Makefile.d"]
    bad: list[str] = []
    for root in payload_roots:
        base = PKG_DIR / root
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if p.is_symlink():
                bad.append(str(p.relative_to(PKG_DIR)))
    assert not bad, f"force-included payload contains symlinks (dangle in sdist): {bad}"


def test_branch_review_guide_matches_orchestrator_canonical() -> None:
    """branch-review-guide.md is now a real copy (the cross-package symlink was
    removed because it broke the sdist build). This guard fails if it drifts
    from the orchestrator's canonical rule, replacing the symlink's
    single-source-of-truth guarantee with a checked invariant."""
    ours = PKG_DIR / "docs" / "workstate" / "rules" / "branch-review-guide.md"
    canonical = (
        PKG_DIR.parent
        / "mcp-workstate-orchestrator"
        / "src"
        / "workstate_orchestrator_mcp"
        / "_assets"
        / "rules"
        / "branch-review-guide.md"
    )
    assert ours.is_file() and not ours.is_symlink(), "must be a real file, not a symlink"
    if not canonical.is_file():
        pytest.skip("orchestrator canonical not present (standalone checkout)")
    assert ours.read_text() == canonical.read_text(), (
        "branch-review-guide.md drifted from the orchestrator canonical; resync "
        "packages/workstate-system/docs/workstate/rules/branch-review-guide.md"
    )
