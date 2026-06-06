"""WORKSTATE-REF-01 implementation note: operator-facing plugin distribution doc.

The plugin distribution model defined in ADR-001 is operator-visible
through ``make plugins-build`` / ``make plugins-check`` and consumer
``claude plugin install`` / Codex marketplace pin flows. This module
pins the doc anchor at ``packages/workstate-system/docs/plugin-distribution.md``
and the README link so operators can land on the right page without
spelunking ADRs.

The anchors checked here are the load-bearing pieces of the workflow;
deeper prose can evolve without breaking the test, but operator-critical
commands and pins must remain mentioned.
"""

from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = PACKAGE_ROOT / "docs" / "plugin-distribution.md"
README_PATH = PACKAGE_ROOT / "README.md"
EPIC_PATH = (
    PACKAGE_ROOT.parents[1] / "docs" / "epics" / "agentic-plugin-distribution-epic.md"
)
ADR_003_PATH = (
    PACKAGE_ROOT
    / "docs"
    / "workstate"
    / "adrs"
    / "ADR-003-plugin-consumer-overrides.md"
)


def test_plugin_distribution_doc_exists() -> None:
    assert DOC_PATH.is_file(), f"missing operator-facing doc: {DOC_PATH}"


def test_plugin_distribution_doc_anchors_load_bearing_commands() -> None:
    """The doc must mention the canonical operator surface verbatim:
    both Make targets, the ADR-001 link, the mcp_servers.yaml input, and
    the two MCP server uvx pins (so a reader can copy/paste without
    cross-referencing other files)."""
    text = DOC_PATH.read_text()
    required_anchors = (
        "make plugins-build",
        "make plugins-check",
        "ADR-001",
        "mcp_servers.yaml",
        "mcp-workstate-handoff@0.12.4",
        "mcp-workstate-orchestrator[bridge]@0.6.1",
        ".workstate/generated/plugins/workstate-system/base/claude",
        ".workstate/generated/plugins/workstate-system/base/codex",
        ".workstate/generated/plugins/workstate-system/base/{claude,codex}",
        "Codex shape: bare server map",
        "live Codex CLI 0.131.0",
    )
    missing = [anchor for anchor in required_anchors if anchor not in text]
    assert not missing, (
        f"plugin-distribution.md missing operator-critical anchors: {missing}"
    )


def test_plugin_distribution_doc_mentions_override_contract() -> None:
    text = DOC_PATH.read_text()
    required_anchors = (
        "ADR-003",
        "workstate-overrides/workstate-system/",
        ".workstate/generated/plugins/workstate-system/effective",
        "--reset-overrides",
        "do not edit",
        "replace a shipped skill",
        "disable a shipped skill",
        "patch MCP server args",
        "workstate-bootstrap doctor",
        "stale_override",
        "overrides.lock.json",
        "plugin-lock.json",
    )
    missing = [anchor for anchor in required_anchors if anchor not in text]
    assert not missing, (
        f"plugin-distribution.md missing WORKSTATE-REF-03 override anchors: {missing}"
    )


def test_plugin_distribution_doc_pins_always_effective_model() -> None:
    """WORKSTATE-REF-07: pins always target effective/; no pin-flipping by override
    presence. The doc must state the always-effective contract."""
    text = DOC_PATH.read_text()
    required_anchors = (
        "Marketplace pins always target",
        "`.workstate/generated/plugins/workstate-system/effective/{claude,codex}`",
        "passthrough",
        "pin_target_drift",
    )
    missing = [anchor for anchor in required_anchors if anchor not in text]
    assert not missing, (
        f"plugin-distribution.md must state the always-effective pin model: {missing}"
    )
    assert "No-override consumers keep their marketplace pins pointed at" not in text, (
        "superseded pin-flipping wording must not survive WORKSTATE-REF-07"
    )


def test_plugin_distribution_doc_records_claude_delivery_proof() -> None:
    text = DOC_PATH.read_text()
    required_anchors = (
        "Claude docs checked on 2026-05-22",
        "https://code.claude.com/docs/en/plugin-marketplaces",
        "https://code.claude.com/docs/en/plugins-reference",
        "claude plugin validate .",
        "claude plugin marketplace add ./ --scope project",
        "claude plugin install workstate-system@workstate-marketplace --scope project",
        "claude plugin list --json",
        "Claude delivery proof result: pass",
    )
    missing = [anchor for anchor in required_anchors if anchor not in text]
    assert not missing, (
        "plugin-distribution.md must record the Claude delivery proof command "
        f"sequence, official docs checked, and result: {missing}"
    )


def test_plugin_distribution_doc_records_codex_delivery_proof() -> None:
    text = DOC_PATH.read_text()
    required_anchors = (
        "Codex docs checked on 2026-05-22",
        "https://developers.openai.com/codex/plugins/build",
        "codex plugin marketplace add ./",
        "codex plugin marketplace list",
        "codex plugin list --marketplace workstate-marketplace",
        "codex plugin add workstate-system@workstate-marketplace",
        "Codex delivery proof result: pass",
    )
    missing = [anchor for anchor in required_anchors if anchor not in text]
    assert not missing, (
        "plugin-distribution.md must record the Codex delivery proof command "
        f"sequence, official docs checked, and result: {missing}"
    )


def test_adr_003_exists_for_override_policy() -> None:
    assert ADR_003_PATH.is_file(), f"missing override policy ADR: {ADR_003_PATH}"


def test_WORKSTATE_epic_marks_WORKSTATE_03_and_WORKSTATE_04_complete() -> None:
    text = EPIC_PATH.read_text()
    required_anchors = (
        "WORKSTATE-REF-03 merged the override-aware consumer path on `main`, and WORKSTATE-REF-04 completed the delivery-proof / epic-alignment pass.",
        "WORKSTATE-REF-04 completed the Phase 4 delivery-proof and WORKSTATE-REF doc reconciliation pass against the merged WORKSTATE-REF-03 path.",
    )
    missing = [anchor for anchor in required_anchors if anchor not in text]
    assert not missing, (
        "agentic-plugin-distribution-epic.md must reflect WORKSTATE-REF-03/WORKSTATE-REF-04 "
        f"completion status: {missing}"
    )


def test_readme_links_to_plugin_distribution_doc() -> None:
    """The package README must link to the operator doc so the install
    path is discoverable from the package landing surface."""
    readme = README_PATH.read_text()
    assert "docs/plugin-distribution.md" in readme, (
        "README must link to docs/plugin-distribution.md so operators can "
        "find the install + verify workflow from the package landing page"
    )
