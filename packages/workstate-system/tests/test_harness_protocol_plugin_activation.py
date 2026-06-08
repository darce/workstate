"""WORKSTATE-REF-09: harness-protocol plugin_activation capability rows."""

from __future__ import annotations

from pathlib import Path

# Plain import (not pytest.importorskip): this module is the contract drift
# guard for plugin_activation rows, so a venv without pyyaml must fail loudly
# rather than silently skip. pyyaml is declared in this package's dev extra.
import yaml

PAYLOAD_ROOT = Path(__file__).resolve().parents[1] / "workstate_system" / "payload"
CONTRACT_PATH = (
    PAYLOAD_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
)

REQUIRED_HARNESSES = {"claude-code", "codex", "grok"}
REQUIRED_FIELDS = {
    "harness_key",
    "mechanism",
    "scope",
    "selector_form",
    "selector",
}


def test_plugin_activation_rows_present_for_three_harnesses() -> None:
    protocol = yaml.safe_load(CONTRACT_PATH.read_text())
    rows = protocol["harness_capabilities"]["plugin_activation"]["rows"]
    keys = {row["harness_key"] for row in rows}
    assert REQUIRED_HARNESSES <= keys


def test_plugin_activation_row_schema() -> None:
    protocol = yaml.safe_load(CONTRACT_PATH.read_text())
    rows = protocol["harness_capabilities"]["plugin_activation"]["rows"]
    for row in rows:
        assert REQUIRED_FIELDS <= set(row)
        assert row["mechanism"] in {
            "settings_json",
            "project_toml",
            "cli_mediated",
        }
        assert row["selector_form"] in {"marketplace_qualified", "bare_name"}


def test_grok_activation_row_records_cli_mediated_mechanism() -> None:
    protocol = yaml.safe_load(CONTRACT_PATH.read_text())
    rows = protocol["harness_capabilities"]["plugin_activation"]["rows"]
    grok = next(row for row in rows if row["harness_key"] == "grok")
    assert grok["mechanism"] == "cli_mediated"
    assert grok["selector"] == "workstate-system"
    assert grok["adopt_materialization"] == "symlink"
    assert "grok plugin install" in grok["commands"][0]
