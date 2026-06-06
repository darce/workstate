"""implementation note implementation note: the update surface ships with both install profiles.

``Makefile.d/update.mk`` (the ``make workstate-update`` target) and
``scripts/workstate/update.sh`` (the deterministic updater) are
``LIFECYCLE_HOISTS`` entries, so they land under ``--profile lifecycle`` AND
``--profile all`` — and the ``Makefile.d`` / ``scripts/workstate`` carve must
record exactly ONE manifest entry per update surface (the lifecycle hoist),
never a duplicate ``shared`` entry from the carved parent copy.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

WORKSTATE_SYSTEM_DIR = Path(__file__).resolve().parents[2] / "workstate-system"
UPDATE_SURFACES = ("Makefile.d/update.mk", "scripts/workstate/update.sh")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], check=True, capture_output=True, cwd=str(cwd), timeout=30
    )


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


@pytest.fixture(scope="module")
def package_payload_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available to build the workstate-system wheel")
    tmp = tmp_path_factory.mktemp("pkg")
    dist = tmp / "dist"
    proc = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(dist), str(WORKSTATE_SYSTEM_DIR)],
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, proc.stderr
    wheel = next(dist.glob("*.whl"))
    unpacked = tmp / "site"
    with zipfile.ZipFile(wheel) as zf:
        zf.extractall(unpacked)
    return unpacked / "workstate_system" / "payload"


def _install(target: Path, package_root: Path, profile: str) -> dict[str, object]:
    from workstate_bootstrap.install import install

    return install(
        target=target,
        source="package",
        package_root=package_root,
        mcp_servers=None,
        enforce_required_surfaces=False,
        profile=profile,
    )


@pytest.mark.parametrize("profile", ["lifecycle", "all"])
def test_update_surfaces_land_once_per_profile(
    tmp_path: Path, package_payload_root: Path, profile: str
) -> None:
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    manifest = _install(target, package_payload_root, profile)

    for surface in UPDATE_SURFACES:
        assert (target / surface).is_file(), f"{surface} missing under {profile}"
        entries = [
            entry
            for entry in manifest["surfaces"]
            if isinstance(entry, dict) and entry.get("path") == surface
        ]
        assert len(entries) == 1, (
            f"{surface} must have exactly one manifest entry under "
            f"--profile {profile}, got {entries!r}"
        )

    # The hoisted make fragment is reachable through the root include.
    makefile = (target / "Makefile").read_text(encoding="utf-8")
    assert "-include Makefile.d/*.mk" in makefile
    on_disk = json.loads(
        (target / ".workstate-bootstrap.json").read_text(encoding="utf-8")
    )
    assert on_disk["surfaces"] == manifest["surfaces"]


def test_update_script_is_executable_payload(package_payload_root: Path) -> None:
    script = package_payload_root / "scripts" / "workstate" / "update.sh"
    assert script.is_file()
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh"), "POSIX sh, not bash"
    assert "workstate-stack" in text
