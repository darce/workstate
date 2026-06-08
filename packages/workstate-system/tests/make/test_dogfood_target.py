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


def test_dogfood_declares_install_flags_passthrough() -> None:
    """``DOGFOOD_INSTALL_FLAGS`` is the documented opt-in path for harness
    Stop adapters (``--install-claude-stop-hook[-local]``,
    ``--install-codex-stop-hook``, ``--install-vscode-stop-hook``) when the
    self-hosted overlay is reinstalled via ``make dogfood``. Without the
    passthrough, the hardcoded install invocation makes hook opt-in
    unreachable from the sanctioned dogfood path (finding
    ``dogfood-no-stop-hook-flag-forwarding-20260605``).
    """
    contents = ROOT_MAKEFILE.read_text(encoding="utf-8")

    assert "DOGFOOD_INSTALL_FLAGS ?=" in contents


def test_dogfood_package_mode_dry_run_forwards_install_flags() -> None:
    proc = subprocess.run(
        [
            "make",
            "-n",
            "dogfood",
            "DOGFOOD_SOURCE=package",
            "DOGFOOD_INSTALL_FLAGS=--install-claude-stop-hook-local",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    install_lines = [
        line
        for line in proc.stdout.splitlines()
        if "workstate-bootstrap install --source package" in line
    ]
    assert install_lines, proc.stdout
    assert all(
        "--install-claude-stop-hook-local" in line for line in install_lines
    ), proc.stdout
    # status invocations must not inherit install-only flags
    status_lines = [
        line
        for line in proc.stdout.splitlines()
        if "workstate-bootstrap status --target" in line
    ]
    assert status_lines, proc.stdout
    assert all(
        "--install-claude-stop-hook-local" not in line for line in status_lines
    ), proc.stdout


def test_dogfood_git_overlay_mode_dry_run_forwards_install_flags() -> None:
    proc = subprocess.run(
        [
            "make",
            "-n",
            "dogfood",
            "DOGFOOD_INSTALL_FLAGS=--install-codex-stop-hook --install-vscode-stop-hook",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    install_lines = [
        line
        for line in proc.stdout.splitlines()
        if "workstate-bootstrap install --target" in line
    ]
    # both git_overlay branches (with and without DOGFOOD_REMOTE_URL) render
    assert install_lines, proc.stdout
    assert all(
        "--install-codex-stop-hook --install-vscode-stop-hook" in line
        for line in install_lines
    ), proc.stdout
