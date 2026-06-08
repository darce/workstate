"""Failing-stub guard for the lifecycle runner (implementation note implementation note).

Every lifecycle subcommand whose real implementation lands in a later
slice (Slices 2-6) MUST ship as a visibly failing stub: it prints
``{"ok": false, "command": <name>, "status": "not_implemented",
"owning_slice": "slice-N"}`` to stdout and exits non-zero (exit code
2). This is the explicit guard against fake-green stubs and the
precondition for implementation note (skill/router cleanup): no skill or router
entry points at a stubbed target until every entry in
``EXPECTED_STUBS`` has been replaced with a real body.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"

EXPECTED_STUBS: dict[str, str] = {}


def _run_stub(subcommand: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), subcommand, "--json"],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize(("subcommand", "owning_slice"), sorted(EXPECTED_STUBS.items()))
def test_stub_emits_failing_receipt_and_exits_two(
    subcommand: str, owning_slice: str
) -> None:
    proc = _run_stub(subcommand)
    assert proc.returncode == 2, (
        f"{subcommand} stub must exit 2 (not_implemented); got {proc.returncode}\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )
    receipt = json.loads(proc.stdout)
    assert receipt == {
        "ok": False,
        "command": subcommand,
        "status": "not_implemented",
        "owning_slice": owning_slice,
    }
