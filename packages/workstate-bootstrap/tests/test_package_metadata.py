"""Packaging-floor pin for `workstate-bootstrap`.

Bootstrap's default install path invokes `uvx mcp-workstate-handoff` which
imports `workstate_protocol.branch_naming` at module import. Bootstrap's
own `workstate-protocol` declaration must therefore not allow `uvx` to
resolve a protocol older than what those servers import at startup.
The floor tracks `mcp-workstate-handoff` / `mcp-workstate-orchestrator`'s own
declared `workstate-protocol` floor so the bootstrap venv cannot resolve
an older protocol than the launched servers expect.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]

# Minimum acceptable lower bound. Bumped alongside coordinated releases
# of mcp-workstate-handoff / mcp-workstate-orchestrator that raise their own
# `workstate-protocol` floor. >=0.1.2 was the original branch_naming
# floor; >=0.1.4 matches the 0.4.1 / 0.11.0 / 0.4.4 release coordinate.
_MIN_PROTOCOL_FLOOR = (0, 1, 4)
_FLOOR_RE = re.compile(r">=(\d+)\.(\d+)\.(\d+)")


def _load_pyproject() -> dict:
    with (PACKAGE_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_pyproject_pins_workstate_protocol_lower_bound_at_branch_naming_release() -> None:
    pyproject = _load_pyproject()
    deps = pyproject["project"]["dependencies"]
    protocol_dep = next((d for d in deps if d.startswith("workstate-protocol")), None)
    assert protocol_dep is not None, "workstate-protocol must be declared as a runtime dep"
    match = _FLOOR_RE.search(protocol_dep)
    assert match is not None, (
        f"workstate-protocol must declare an explicit >=X.Y.Z lower bound, got {protocol_dep!r}"
    )
    actual_floor = tuple(int(part) for part in match.groups())
    assert actual_floor >= _MIN_PROTOCOL_FLOOR, (
        f"workstate-protocol lower bound must be >={'.'.join(str(p) for p in _MIN_PROTOCOL_FLOOR)} "
        f"(matches the floor pinned by mcp-workstate-handoff / mcp-workstate-orchestrator that bootstrap "
        f"launches via uvx), got {protocol_dep!r}"
    )
    assert "<0.3.0" in protocol_dep, (
        f"workstate-protocol upper bound must remain <0.3.0 (single minor hard pin; bumped "
        f"alongside the 0.2.0 override-schemas release), got {protocol_dep!r}"
    )
