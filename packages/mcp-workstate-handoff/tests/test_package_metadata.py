from __future__ import annotations

import re
import tomllib
from pathlib import Path

from packaging.version import Version

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict:
    with (PACKAGE_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_pyproject_declares_hoisted_install_metadata() -> None:
    pyproject = _load_pyproject()

    hoisted = pyproject["tool"]["hoisted"]
    assert hoisted["repository"] == "darce/workstate"
    assert (
        hoisted["install_url"]
        == "git+https://github.com/darce/workstate.git@mcp-workstate-handoff-v{version}#subdirectory=packages/mcp-workstate-handoff"
    )


def test_changelog_records_hoist_mvp_metadata_entry() -> None:
    changelog = (PACKAGE_ROOT / "CHANGELOG.md").read_text()

    # The public changelog should lead with the Workstate package URL after
    # the public-distribution rebrand.
    assert "Hoist Workstate System MVP" in changelog
    assert (
        "git+https://github.com/darce/workstate.git@mcp-workstate-handoff-v{version}#subdirectory=packages/mcp-workstate-handoff"
        in changelog
    )


def test_pyproject_pins_workstate_protocol_lower_bound_at_branch_naming_release() -> None:
    # `workstate_handoff_mcp.__init__` re-exports `workstate_protocol.branch_naming`
    # at import time (implementation note implementation note). If a `uvx mcp-workstate-handoff` env
    # resolves an `workstate-protocol` older than 0.1.2 (the first release
    # carrying `branch_naming`), the package crashes on import before the
    # CLI / MCP server / init-state / hook helpers can run. Pin the floor
    # here so the dep declaration cannot silently drift below the contract.
    pyproject = _load_pyproject()
    deps = pyproject["project"]["dependencies"]
    protocol_dep = next((d for d in deps if d.startswith("workstate-protocol")), None)
    assert protocol_dep is not None, "workstate-protocol must be declared as a runtime dep"
    floor_match = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", protocol_dep)
    assert floor_match, f"workstate-protocol pin must declare a >= lower bound, got {protocol_dep!r}"
    assert Version(floor_match.group(1)) >= Version("0.1.2"), (
        f"workstate-protocol lower bound must be >=0.1.2 (the first release containing "
        f"branch_naming), got {protocol_dep!r}"
    )
    assert "<0.3.0" in protocol_dep, (
        f"workstate-protocol upper bound must remain <0.3.0 (single minor hard pin; bumped "
        f"alongside the 0.2.0 override-schemas release), got {protocol_dep!r}"
    )
