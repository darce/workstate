"""implementation note S3 — inverse-dependency invariant (RED criterion c).

`workstate_system` is consumer #0's payload package; `workstate_bootstrap`
depends on it, never the reverse (decision #117). S3 co-locates the shipped
payload under `workstate_system/payload/` and dogfoods it via git-tracked
symlinks — explicitly WITHOUT importing `workstate_bootstrap` (that would
invert the dependency and risk a build/runtime cycle).

This locks the invariant BEFORE the risky payload cutover, so the move cannot
silently introduce a bootstrap import. It asserts in a clean subprocess that
importing `workstate_system` (and calling `data_root()`) never loads
`workstate_bootstrap`.
"""

from __future__ import annotations

import subprocess
import sys


def test_importing_workstate_system_does_not_import_workstate_bootstrap() -> None:
    code = (
        "import sys\n"
        "import workstate_system\n"
        "workstate_system.data_root()\n"
        "leaked = [m for m in sys.modules if m == 'workstate_bootstrap' "
        "or m.startswith('workstate_bootstrap.')]\n"
        "assert not leaked, "
        "f'workstate_system import pulled in workstate_bootstrap: {leaked}'\n"
        "print('ok')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        "inverse-dependency invariant violated or import failed:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )
