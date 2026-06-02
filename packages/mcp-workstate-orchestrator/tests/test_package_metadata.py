from __future__ import annotations

import tomllib
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def _load_pyproject() -> dict:
    with (PACKAGE_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)


def test_pyproject_pins_handoff_dependency_and_declares_hoisted_metadata() -> None:
    pyproject = _load_pyproject()

    deps = pyproject["project"]["dependencies"]
    assert any(d.startswith("mcp-workstate-handoff") for d in deps), deps

    hoisted = pyproject["tool"]["hoisted"]
    assert "repository" in hoisted
    assert "install_url" in hoisted


def test_changelog_records_hoist_mvp_metadata_entry() -> None:
    changelog = (PACKAGE_ROOT / "CHANGELOG.md").read_text()

    # The CHANGELOG is a preserved historical ledger: the Workstate rebrand
    # (implementation note §6 RESOLVED) does NOT rewrite dated entries, so this entry
    # legitimately still records the pre-rename `mcp-workstate-orchestrator`
    # install URL. The current (renamed) install metadata is asserted against
    # pyproject above; this asserts the historical record stays intact.
    assert "Hoist Agentic System MVP" in changelog
    assert "git+ssh://git@github.com/darce/mcp-workstate-orchestrator.git@v{version}" in changelog
