"""Behavioral guarantee for the BR-01 packaging floor pin.

The pyproject metadata test (`test_package_metadata.py`) only asserts the
*declaration* `workstate-protocol>=0.1.2,<0.2.0`. It does not prove that
declaration actually permits `import workstate_handoff_mcp` in a clean
resolver env -- so a future bad release could ship an `workstate_handoff_mcp`
that re-exports `workstate_protocol.branch_naming` from a stale-floor
release without the test catching it.

This module installs both packages from local source into an ephemeral
`uv` venv and asserts:

1. `from workstate_handoff_mcp import TASK_REF_RE` succeeds.
2. `workstate_handoff_mcp.TASK_REF_RE is workstate_protocol.branch_naming.TASK_REF_RE`
   (the identity-by-reference contract -- re-export, not copy).

Slow by design (creates venv + installs from source). Skipped when `uv`
is not on PATH so contributor laptops without the project toolchain do
not see spurious failures; CI gates the run by exposing `uv`.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
PROTOCOL_PACKAGE_DIR = REPO_ROOT / "packages" / "workstate-protocol"
HANDOFF_PACKAGE_DIR = REPO_ROOT / "packages" / "mcp-workstate-handoff"


pytestmark = pytest.mark.timeout(240)


def _have_uv() -> bool:
    return shutil.which("uv") is not None


@pytest.mark.skipif(
    not _have_uv(),
    reason="uv not on PATH; BR-01 floor smoke needs uv to materialize a clean env",
)
def test_clean_env_resolves_packaging_floor_and_imports_task_ref_re(
    tmp_path: Path,
) -> None:
    venv_dir = tmp_path / "smoke-venv"
    subprocess.run(
        ["uv", "venv", str(venv_dir), "--python", sys.executable],
        check=True,
        capture_output=True,
        text=True,
    )

    venv_python = venv_dir / "bin" / "python"
    assert venv_python.exists(), f"uv venv did not produce expected interpreter at {venv_python}"

    install = subprocess.run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(venv_python),
            "--no-cache",
            str(PROTOCOL_PACKAGE_DIR),
            str(HANDOFF_PACKAGE_DIR),
        ],
        capture_output=True,
        text=True,
    )
    assert install.returncode == 0, (
        f"uv pip install failed (rc={install.returncode}); "
        f"the BR-01 floor pin may have started rejecting a real release.\n"
        f"stdout:\n{install.stdout}\n"
        f"stderr:\n{install.stderr}"
    )

    proof = subprocess.run(
        [
            str(venv_python),
            "-c",
            (
                "from workstate_handoff_mcp import TASK_REF_RE as _h; "
                "from workstate_protocol.branch_naming import TASK_REF_RE as _p; "
                "assert _h is _p, 'identity-by-reference broken'; "
                "import workstate_protocol as ap; "
                "print(f'OK protocol={ap.__version__}')"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert proof.returncode == 0, (
        f"clean-env import sanity check failed (rc={proof.returncode}); "
        f"BR-01's behavioral guarantee is not held.\n"
        f"stdout:\n{proof.stdout}\n"
        f"stderr:\n{proof.stderr}"
    )
    assert "OK protocol=" in proof.stdout
