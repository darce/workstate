"""Root ``make dogfood`` release-upgrade contract."""

from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
ROOT_MAKEFILE = REPO_ROOT / "Makefile"


def test_dogfood_target_has_package_source_mode() -> None:
    """Agents must have a one-command path that runs released bootstrap code.

    The default dogfood path intentionally uses the local checkout's bootstrap
    project for branch/tag overlay installs. The package mode is the release
    recovery path: it runs ``workstate-bootstrap`` from PyPI together with the
    matching ``workstate-system`` wheel, so an older local checkout can upgrade
    MCP pins and plugin payloads using the newly published installer.
    """
    contents = ROOT_MAKEFILE.read_text(encoding="utf-8")

    assert "DOGFOOD_SOURCE ?= git_overlay" in contents
    assert 'if [ "$$source" = "package" ]; then' in contents
    assert 'uvx $$uvx_flags --from "$$bootstrap_spec" --with "$$system_spec"' in contents
    assert "workstate-bootstrap install --source package --target" in contents
    assert "workstate-bootstrap status --target" in contents


def test_dogfood_package_mode_dry_run_renders_released_package_install() -> None:
    proc = subprocess.run(
        [
            "make",
            "-n",
            "dogfood",
            "DOGFOOD_SOURCE=package",
            "DOGFOOD_BOOTSTRAP_SPEC=workstate-bootstrap==9.8.7",
            "DOGFOOD_SYSTEM_SPEC=workstate-system==6.5.4",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "DOGFOOD_SOURCE" not in proc.stderr
    assert "workstate-bootstrap==9.8.7" in proc.stdout
    assert "workstate-system==6.5.4" in proc.stdout
    assert "workstate-bootstrap install --source package --target" in proc.stdout
