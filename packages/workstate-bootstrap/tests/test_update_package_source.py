"""implementation note implementation note: ``update`` works for package-source manifests.

The 2026-06-05 consumer incident: a `source_kind=package` install could only
be refreshed by hand-re-running ``install --source package`` because
``update`` refused package manifests and the CLI hard-required
``--remote-ref`` before the manifest was even loaded. This suite pins the new
contract:

* ``update --target <repo>`` is valid for package-source manifests (no
  ``--remote-ref``); it re-runs install from the installed workstate-system
  payload and records the fresh ``package_version``.
* ``--remote-ref`` with a package-source manifest is a clear error.
* ``git_overlay`` manifests still require ``--remote-ref`` (clear error
  before any mutation).
* Package install/update records stack provenance (``stack_distribution``,
  ``stack_version``, ``stack_members``) when the ``workstate-stack`` anchor
  distribution is installed; legacy manifests without it stay valid.
"""

from __future__ import annotations

import importlib
import json
import shutil
import subprocess
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


@pytest.fixture(scope="module")
def package_payload_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build + unpack the workstate-system wheel once for this module."""
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


def _package_install(target: Path, package_root: Path) -> dict[str, object]:
    from workstate_bootstrap.install import install

    return install(
        target=target,
        source="package",
        package_root=package_root,
        mcp_servers=None,
        enforce_required_surfaces=False,
    )


def test_update_package_source_reinstalls_without_remote_ref(
    tmp_path: Path, package_payload_root: Path
) -> None:
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _package_install(target, package_payload_root)

    # Simulate an older install so the refresh visibly rewrites provenance.
    manifest_path = target / ".workstate-bootstrap.json"
    seeded = json.loads(manifest_path.read_text(encoding="utf-8"))
    seeded["package_version"] = "0.0.1"
    manifest_path.write_text(json.dumps(seeded), encoding="utf-8")

    manifest = update(target=target, package_root=package_payload_root)

    assert manifest["source_kind"] == "package"
    assert manifest["package_version"] != "0.0.1"
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert on_disk["source_kind"] == "package"
    assert on_disk["package_version"] == manifest["package_version"]
    assert not (target / ".workstate" / "remote").exists(), (
        "package-source update must not create a clone"
    )


def test_update_package_source_rejects_remote_ref(
    tmp_path: Path, package_payload_root: Path
) -> None:
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    _package_install(target, package_payload_root)

    with pytest.raises(ValueError, match="(?i)package"):
        update(target=target, remote_ref="v9.9.9", package_root=package_payload_root)


def test_update_git_overlay_still_requires_remote_ref(tmp_path: Path) -> None:
    """A git-overlay manifest without --remote-ref fails clearly, pre-mutation."""
    from workstate_bootstrap.subcommands import update

    target = tmp_path / "consumer"
    target.mkdir()
    (target / ".workstate-bootstrap.json").write_text(
        json.dumps(
            {
                "schema_version": 5,
                "source_kind": "git_overlay",
                "remote_url": "https://example.invalid/workstate.git",
                "remote_ref": "v0.1.0",
                "remote_sha": "a" * 40,
                "surfaces": [],
                "configs": [],
                "mcp_servers": [],
            }
        ),
        encoding="utf-8",
    )

    sentinel = target / "untouched.txt"
    sentinel.write_text("before", encoding="utf-8")
    with pytest.raises(ValueError, match="remote.ref|remote_ref"):
        update(target=target)
    assert sentinel.read_text(encoding="utf-8") == "before"


def test_update_cli_accepts_missing_remote_ref() -> None:
    """Parser-level: --remote-ref is optional; validation happens after the
    manifest is loaded (so package-source consumers never need the flag)."""
    from workstate_bootstrap.cli import _build_parser

    args = _build_parser().parse_args(["update", "--target", "."])
    assert args.remote_ref is None


def test_stack_provenance_parses_exact_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    install_mod = importlib.import_module("workstate_bootstrap.install")

    def fake_version(distribution: str) -> str:
        if distribution == "workstate-stack":
            return "0.1.0"
        raise install_mod.importlib_metadata.PackageNotFoundError(distribution)

    def fake_requires(distribution: str) -> list[str]:
        assert distribution == "workstate-stack"
        return [
            "workstate-protocol==0.2.1",
            "mcp-workstate-handoff==0.12.3",
            'pytest>=8; extra == "dev"',  # non-pin requirement is ignored
        ]

    monkeypatch.setattr(install_mod.importlib_metadata, "version", fake_version)
    monkeypatch.setattr(install_mod.importlib_metadata, "requires", fake_requires)

    distribution, version, members = install_mod._stack_provenance()
    assert distribution == "workstate-stack"
    assert version == "0.1.0"
    assert members == {
        "workstate-protocol": "0.2.1",
        "mcp-workstate-handoff": "0.12.3",
    }


def test_stack_provenance_absent_when_stack_not_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_mod = importlib.import_module("workstate_bootstrap.install")

    def missing(distribution: str) -> str:
        raise install_mod.importlib_metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(install_mod.importlib_metadata, "version", missing)

    assert install_mod._stack_provenance() == (None, None, None)


def test_package_install_records_stack_provenance(
    tmp_path: Path, package_payload_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_mod = importlib.import_module("workstate_bootstrap.install")

    monkeypatch.setattr(
        install_mod,
        "_stack_provenance",
        lambda: (
            "workstate-stack",
            "0.1.0",
            {"workstate-system": "0.2.1", "workstate-protocol": "0.2.1"},
        ),
    )

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    manifest = _package_install(target, package_payload_root)

    assert manifest["stack_distribution"] == "workstate-stack"
    assert manifest["stack_version"] == "0.1.0"
    assert manifest["stack_members"] == {
        "workstate-system": "0.2.1",
        "workstate-protocol": "0.2.1",
    }
    on_disk = json.loads(
        (target / ".workstate-bootstrap.json").read_text(encoding="utf-8")
    )
    assert on_disk["stack_version"] == "0.1.0"


def test_package_install_without_stack_omits_provenance(
    tmp_path: Path, package_payload_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy shape: no workstate-stack installed -> no stack fields; the
    manifest still validates (BootstrapManifest keeps them optional)."""
    install_mod = importlib.import_module("workstate_bootstrap.install")

    monkeypatch.setattr(install_mod, "_stack_provenance", lambda: (None, None, None))

    target = tmp_path / "consumer"
    target.mkdir()
    _init_git_repo(target)
    manifest = _package_install(target, package_payload_root)

    assert not manifest.get("stack_distribution")
    assert not manifest.get("stack_version")
    assert not manifest.get("stack_members")
