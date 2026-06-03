"""WS-PKG-DELIVERY-01 implementation note: install from the workstate-system package source.

``install(source="package", package_root=<installed workstate_system data>)``
materializes the overlay by **copy** (not symlink-into-a-clone), records
``source_kind="package"`` + ``package_version`` in the manifest, and creates no
``.workstate/remote`` git clone. Surfaces must match a git-overlay install of
the same content (parity), proving the package source is a drop-in delivery.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tomllib
import zipfile
from pathlib import Path

import pytest

WORKSTATE_SYSTEM_DIR = Path(__file__).resolve().parents[2] / "workstate-system"


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args], check=True, capture_output=True, cwd=str(cwd), timeout=30
    )


def _init_git_repo(path: Path) -> None:
    _git("init", "--initial-branch=main", cwd=path)
    _git("config", "user.email", "test@example.com", cwd=path)
    _git("config", "user.name", "Test", cwd=path)


def _build_and_unpack_package(tmp: Path) -> Path:
    """Build the workstate-system wheel and unpack it; return the installed
    ``workstate_system`` data root (what the package source resolves to)."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv not available to build the workstate-system wheel")
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
    root = unpacked / "workstate_system" / "payload"
    assert (root / "scripts" / "hooks").is_dir(), (
        "unpacked payload missing scripts/hooks"
    )
    return root


def test_install_from_package_copies_surfaces_and_records_manifest(
    tmp_path: Path,
) -> None:
    from workstate_bootstrap.install import install

    package_root = _build_and_unpack_package(tmp_path)

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    manifest = install(
        target=target,
        source="package",
        package_root=package_root,
        mcp_servers=None,
        enforce_required_surfaces=False,
    )

    # 1) Surfaces materialized by COPY (real dir, not a symlink into a clone).
    hooks = target / "scripts" / "hooks"
    assert hooks.is_dir(), "scripts/hooks surface must be materialized"
    assert not hooks.is_symlink(), "package source must copy, not symlink"

    # 2) No git clone created.
    assert not (target / ".workstate" / "remote").exists(), (
        "package source must not clone"
    )

    # 3) Manifest records the package provenance.
    assert manifest["source_kind"] == "package"
    assert manifest.get("package_version")
    on_disk = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert on_disk["source_kind"] == "package"


def test_install_from_package_copies_surface_contents_faithfully(
    tmp_path: Path,
) -> None:
    """Copied surfaces are byte-identical to the package data (the content-parity
    proxy for the package-vs-git-overlay cutover gate, which is owned by the
    follow-on)."""
    from workstate_bootstrap.install import install

    package_root = _build_and_unpack_package(tmp_path)
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    install(
        target=target,
        source="package",
        package_root=package_root,
        mcp_servers=None,
        enforce_required_surfaces=False,
    )

    src_contracts = package_root / "docs" / "workstate" / "contracts"
    sample = next((p for p in src_contracts.rglob("*.md") if p.is_file()), None)
    assert sample is not None, "package must ship at least one contract doc"
    copied = target / sample.relative_to(package_root)
    assert copied.is_file(), f"contract surface not materialized: {copied}"
    assert copied.read_bytes() == sample.read_bytes(), (
        "copied surface drifted from package source"
    )


def test_doctor_clean_on_package_install(tmp_path: Path) -> None:
    """implementation note: doctor recognizes a package install and does not false-flag the
    copied surfaces as drift or the (intentionally absent) git clone."""
    from workstate_bootstrap.install import install
    from workstate_bootstrap.subcommands import doctor

    package_root = _build_and_unpack_package(tmp_path)
    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    install(
        target=target,
        source="package",
        package_root=package_root,
        mcp_servers=None,
        enforce_required_surfaces=False,
    )

    findings = doctor(target=target)
    kinds = {f.get("kind") for f in findings}
    assert "missing_clone" not in kinds, findings
    assert "surface_drift" not in kinds, findings


def test_cli_install_from_package_resolves_installed_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workstate_bootstrap.cli import main as cli_main

    package_root = _build_and_unpack_package(tmp_path)
    monkeypatch.syspath_prepend(str(package_root.parents[1]))

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)

    rc = cli_main(
        [
            "install",
            "--target",
            str(target),
            "--source",
            "package",
            "--no-mcp-servers",
            "--no-enforce-required-surfaces",
        ]
    )

    assert rc == 0
    manifest = json.loads((target / ".workstate-bootstrap.json").read_text())
    assert manifest["source_kind"] == "package"
    # Track the installed workstate-system version dynamically instead of a
    # hardcoded literal that drifts on every bump — mirrors
    # install._package_version() (importlib.metadata + "0.0.0+local" fallback).
    from importlib import metadata as importlib_metadata

    try:
        expected_version = importlib_metadata.version("workstate-system")
    except Exception:  # noqa: BLE001
        expected_version = "0.0.0+local"
    assert manifest["package_version"] == expected_version
    assert not (target / ".workstate" / "remote").exists()
    assert (target / "scripts" / "hooks").is_dir()
    assert not (target / "scripts" / "hooks").is_symlink()


def test_bootstrap_depends_on_workstate_system_for_package_source() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )

    dependencies = [
        dep.lower().replace("_", "-")
        for dep in pyproject["project"]["dependencies"]
    ]

    assert any(dep.startswith("workstate-system") for dep in dependencies)
