"""Unit tests for ``sync_mcp_configs(prune_removed_managed=True)``.

WORKSTATE-REF-50 implementation note: when ``prune_removed_managed=True``, names that
appear in the ledger's ``mcp_servers`` block but are NOT in the
resolved managed map are removed from the rendered surfaces. The
ledger is the authoritative provenance source; this code path never
inspects client config keys to guess which look "managed."

Legacy targets (ledger missing the block or holding ``[]``) skip the
prune for the first run; the block is seeded from the resolved map at
write time so the next run has provenance.
"""

from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.mcp_sync import sync_mcp_configs


CURRENT_SERVERS = {
    "workstate-handoff-mcp": {
        "command": "uvx",
        "args": ["workstate-handoff-mcp@1.2.3"],
    },
}


def _seed_ledger(target: Path, *, mcp_servers: list[str] | None) -> None:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "remote_url": "file:///tmp/fake.git",
        "remote_ref": "main",
        "remote_sha": "0" * 40,
        "surfaces": [],
        "configs": [],
    }
    if mcp_servers is not None:
        payload["mcp_servers"] = mcp_servers
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2) + "\n"
    )


def _seed_claude_with_servers(target: Path, names: dict[str, dict]) -> None:
    (target / ".mcp.json").write_text(
        json.dumps({"mcpServers": names}, indent=2) + "\n"
    )


def _seed_vscode_with_servers(target: Path, names: dict[str, dict]) -> None:
    (target / ".vscode").mkdir(parents=True, exist_ok=True)
    (target / ".vscode" / "mcp.json").write_text(
        json.dumps({"servers": names}, indent=2) + "\n"
    )


def _seed_codex_with_servers(target: Path, names: dict[str, dict]) -> None:
    (target / ".codex").mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table(is_super_table=True)
    for server_name, spec in names.items():
        table = tomlkit.table()
        for k, v in spec.items():
            table[k] = v
        doc["mcp_servers"][server_name] = table
    (target / ".codex" / "config.toml").write_text(tomlkit.dumps(doc))


def test_prune_removes_managed_dropped_from_resolved_map(tmp_path: Path) -> None:
    """A name in ledger.mcp_servers but missing from the new map is dropped."""
    _seed_ledger(
        tmp_path,
        mcp_servers=["workstate-handoff-mcp", "workstate-orchestrator-mcp"],
    )
    _seed_claude_with_servers(
        tmp_path,
        {
            "workstate-handoff-mcp": {"command": "OLD", "args": []},
            "workstate-orchestrator-mcp": {"command": "OLD", "args": []},
        },
    )

    report = sync_mcp_configs(
        tmp_path, CURRENT_SERVERS, check_only=False, prune_removed_managed=True
    )

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "workstate-orchestrator-mcp" not in doc["mcpServers"]
    assert "workstate-handoff-mcp" in doc["mcpServers"]
    assert list(report.pruned_managed) == ["workstate-orchestrator-mcp"]


def test_prune_preserves_third_party_launcher(tmp_path: Path) -> None:
    """Third-party names absent from the ledger are not eligible for prune."""
    _seed_ledger(tmp_path, mcp_servers=["workstate-handoff-mcp"])
    _seed_claude_with_servers(
        tmp_path,
        {
            "workstate-handoff-mcp": {"command": "OLD", "args": []},
            "user-private-tool": {"command": "node", "args": ["./local.js"]},
        },
    )

    sync_mcp_configs(
        tmp_path, CURRENT_SERVERS, check_only=False, prune_removed_managed=True
    )

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert doc["mcpServers"]["user-private-tool"] == {
        "command": "node",
        "args": ["./local.js"],
    }


def test_prune_applies_across_all_three_surfaces(tmp_path: Path) -> None:
    _seed_ledger(
        tmp_path,
        mcp_servers=["workstate-handoff-mcp", "workstate-orchestrator-mcp"],
    )
    stale = {
        "workstate-handoff-mcp": {"command": "OLD", "args": []},
        "workstate-orchestrator-mcp": {"command": "OLD", "args": []},
    }
    _seed_claude_with_servers(tmp_path, stale)
    _seed_vscode_with_servers(tmp_path, stale)
    _seed_codex_with_servers(tmp_path, stale)

    sync_mcp_configs(
        tmp_path, CURRENT_SERVERS, check_only=False, prune_removed_managed=True
    )

    claude = json.loads((tmp_path / ".mcp.json").read_text())
    vscode = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    codex = tomlkit.parse((tmp_path / ".codex" / "config.toml").read_text())
    assert "workstate-orchestrator-mcp" not in claude["mcpServers"]
    assert "workstate-orchestrator-mcp" not in vscode["servers"]
    assert "workstate-orchestrator-mcp" not in dict(codex["mcp_servers"])


def test_prune_legacy_ledger_without_block_is_noop_first_run(
    tmp_path: Path,
) -> None:
    """Ledger has no mcp_servers block → first run does not prune anything,
    even if the surface contains stale managed-shaped names."""
    _seed_ledger(tmp_path, mcp_servers=None)
    _seed_claude_with_servers(
        tmp_path,
        {
            "workstate-handoff-mcp": {"command": "OLD", "args": []},
            "workstate-orchestrator-mcp": {"command": "OLD", "args": []},
        },
    )

    report = sync_mcp_configs(
        tmp_path, CURRENT_SERVERS, check_only=False, prune_removed_managed=True
    )

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "workstate-orchestrator-mcp" in doc["mcpServers"]
    assert report.pruned_managed == ()
    ledger = json.loads((tmp_path / BOOTSTRAP_MANIFEST_NAME).read_text())
    assert ledger["mcp_servers"] == ["workstate-handoff-mcp"]


def test_prune_check_only_does_not_write(tmp_path: Path) -> None:
    _seed_ledger(
        tmp_path, mcp_servers=["workstate-handoff-mcp", "workstate-orchestrator-mcp"]
    )
    _seed_claude_with_servers(
        tmp_path,
        {
            "workstate-handoff-mcp": {"command": "OLD", "args": []},
            "workstate-orchestrator-mcp": {"command": "OLD", "args": []},
        },
    )
    pre_bytes = (tmp_path / ".mcp.json").read_bytes()

    report = sync_mcp_configs(
        tmp_path, CURRENT_SERVERS, check_only=True, prune_removed_managed=True
    )

    assert (tmp_path / ".mcp.json").read_bytes() == pre_bytes
    assert list(report.pruned_managed) == ["workstate-orchestrator-mcp"]
    assert report.exit_code == 1


def test_prune_disabled_keeps_removed_managed_names(tmp_path: Path) -> None:
    """Default ``prune_removed_managed=False`` preserves ledger-managed
    names that fell out of the resolved map (no destructive default)."""
    _seed_ledger(
        tmp_path, mcp_servers=["workstate-handoff-mcp", "workstate-orchestrator-mcp"]
    )
    _seed_claude_with_servers(
        tmp_path,
        {
            "workstate-handoff-mcp": {"command": "OLD", "args": []},
            "workstate-orchestrator-mcp": {"command": "OLD", "args": []},
        },
    )

    report = sync_mcp_configs(tmp_path, CURRENT_SERVERS, check_only=False)

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "workstate-orchestrator-mcp" in doc["mcpServers"]
    assert report.pruned_managed == ()
