"""Unit tests for ``sync_mcp_configs`` (WORKSTATE-REF-50 implementation note).

The sync API is the parameter-only entry point that the CLI subcommand,
``bootstrap doctor``, and ``bootstrap repair --mcp-servers`` will all
drive. These tests pin the contract for the basic check + apply paths
and the surfaces filter. ``--prune-removed-managed`` semantics land in
a follow-up slice (1e); this slice keeps prune support out so the
diff stays vertical.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import tomlkit

from workstate_bootstrap.install import (
    BOOTSTRAP_MANIFEST_NAME,
    SCHEMA_VERSION,
)
from workstate_bootstrap.mcp_sync import (
    DEFAULT_SURFACES,
    SUPPORTED_SURFACES,
    SurfaceReport,
    SyncReport,
    sync_mcp_configs,
)


SERVERS = {
    "workstate-handoff-mcp": {
        "command": "uvx",
        "args": ["workstate-handoff-mcp@1.2.3"],
    },
    "workstate-orchestrator-mcp": {
        "command": "uvx",
        "args": ["workstate-orchestrator-mcp@4.5.6"],
    },
}


def _seed_ledger(target: Path, *, mcp_servers: list[str] | None = None) -> None:
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


def test_sync_returns_typed_report(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    report = sync_mcp_configs(tmp_path, SERVERS)
    assert isinstance(report, SyncReport)
    assert {s.name for s in report.surfaces} == set(DEFAULT_SURFACES)
    for s in report.surfaces:
        assert isinstance(s, SurfaceReport)


def test_check_only_does_not_write_to_disk(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    report = sync_mcp_configs(tmp_path, SERVERS, check_only=True)
    assert all(s.drift for s in report.surfaces)
    assert all(s.action == "would_write" for s in report.surfaces)
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".vscode" / "mcp.json").exists()
    assert not (tmp_path / ".codex" / "config.toml").exists()
    assert report.exit_code == 1


def test_apply_writes_drifted_surfaces(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    report = sync_mcp_configs(tmp_path, SERVERS, check_only=False)
    assert (tmp_path / ".mcp.json").exists()
    assert (tmp_path / ".vscode" / "mcp.json").exists()
    assert (tmp_path / ".codex" / "config.toml").exists()
    actions = {s.name: s.action for s in report.surfaces}
    assert actions == {"claude": "created", "vscode": "created", "codex": "created"}
    assert report.exit_code == 0

    mcp_doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "workstate-handoff-mcp" in mcp_doc["mcpServers"]
    codex_doc = tomlkit.parse((tmp_path / ".codex" / "config.toml").read_text())
    assert "workstate-handoff-mcp" in dict(codex_doc["mcp_servers"])


def test_apply_is_idempotent_when_already_synced(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    sync_mcp_configs(tmp_path, SERVERS, check_only=False)
    rerun = sync_mcp_configs(tmp_path, SERVERS, check_only=False)
    assert all(not s.drift for s in rerun.surfaces)
    assert all(s.action == "unchanged" for s in rerun.surfaces)
    assert rerun.exit_code == 0


def test_apply_only_writes_drifted_surfaces(tmp_path: Path) -> None:
    """If only one surface drifts, only that surface is rewritten."""
    _seed_ledger(tmp_path, mcp_servers=[])
    sync_mcp_configs(tmp_path, SERVERS, check_only=False)
    claude_path = tmp_path / ".mcp.json"
    vscode_path = tmp_path / ".vscode" / "mcp.json"
    codex_path = tmp_path / ".codex" / "config.toml"
    vscode_mtime = vscode_path.stat().st_mtime_ns
    codex_mtime = codex_path.stat().st_mtime_ns

    claude_path.write_bytes(b"{}\n")

    report = sync_mcp_configs(tmp_path, SERVERS, check_only=False)
    by_name = {s.name: s for s in report.surfaces}
    assert by_name["claude"].action == "merged"
    assert by_name["vscode"].action == "unchanged"
    assert by_name["codex"].action == "unchanged"
    assert vscode_path.stat().st_mtime_ns == vscode_mtime
    assert codex_path.stat().st_mtime_ns == codex_mtime


def test_surfaces_filter_limits_writes(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    report = sync_mcp_configs(
        tmp_path, SERVERS, check_only=False, surfaces=("claude",)
    )
    assert {s.name for s in report.surfaces} == {"claude"}
    assert (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".vscode" / "mcp.json").exists()
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_surfaces_filter_rejects_unknown_name(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    with pytest.raises(ValueError, match="cursor"):
        sync_mcp_configs(tmp_path, SERVERS, surfaces=("claude", "cursor"))


def test_apply_rewrites_ledger_mcp_servers(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=["stale-server"])
    report = sync_mcp_configs(tmp_path, SERVERS, check_only=False)
    ledger = json.loads((tmp_path / BOOTSTRAP_MANIFEST_NAME).read_text())
    assert ledger["mcp_servers"] == sorted(SERVERS.keys())
    assert list(report.ledger_mcp_servers) == sorted(SERVERS.keys())


def test_check_only_does_not_rewrite_ledger(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=["stale-server"])
    sync_mcp_configs(tmp_path, SERVERS, check_only=True)
    ledger = json.loads((tmp_path / BOOTSTRAP_MANIFEST_NAME).read_text())
    assert ledger["mcp_servers"] == ["stale-server"]


def test_third_party_launchers_preserved_through_apply(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "third-party": {"command": "node", "args": ["./local.js"]},
                }
            },
            indent=2,
        )
        + "\n"
    )
    report = sync_mcp_configs(tmp_path, SERVERS, check_only=False)
    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "third-party" in doc["mcpServers"]
    assert "workstate-handoff-mcp" in doc["mcpServers"]
    claude = next(s for s in report.surfaces if s.name == "claude")
    assert "third-party" in claude.preserved_third_party


def test_supported_surfaces_constant_pins_three_clients() -> None:
    assert SUPPORTED_SURFACES == frozenset({"claude", "vscode", "codex"})
    assert DEFAULT_SURFACES == ("claude", "vscode", "codex")
