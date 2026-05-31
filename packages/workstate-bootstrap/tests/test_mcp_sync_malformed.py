"""Malformed-existing-file recovery for the mcp-sync render seam
(WORKSTATE-REF-50 review finding BR-WORKSTATWORKSTATE-REF-50-20260510-01).

Doctor, repair, and the ``mcp-sync`` CLI must treat an unparseable
``.mcp.json`` / ``.vscode/mcp.json`` / ``.codex/config.toml`` as
recoverable drift rather than letting a parser exception escape. The
managed surfaces are the tool's own output: if a consumer (or a
previous interrupted run) left the file invalid, the next reconcile
should rewrite it cleanly.

Contract:
- ``--check`` reports drift for the malformed surface and exits ``1``.
- ``--apply`` rewrites the file with the resolved managed map.
- ``doctor(target=..., mcp_servers=...)`` returns a ``config_drift``
  finding for the malformed surface (no exception escapes).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_bootstrap.cli import main as cli_main
from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.subcommands import doctor, repair


CURRENT_SERVERS = {
    "workstate-handoff-mcp": {
        "command": "uvx",
        "args": ["workstate-handoff-mcp@1.2.3"],
    },
}


def _seed_ledger(target: Path) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "remote_url": "file:///tmp/fake.git",
        "remote_ref": "main",
        "remote_sha": "0" * 40,
        "surfaces": [],
        "configs": [
            {"path": ".mcp.json", "action": "merged"},
            {"path": ".vscode/mcp.json", "action": "merged"},
            {"path": ".codex/config.toml", "action": "merged"},
        ],
        "mcp_servers": [],
    }
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2) + "\n"
    )
    (target / ".task-state").mkdir(parents=True, exist_ok=True)
    (target / ".task-state" / "handoff.db").write_bytes(b"")


def _write_servers_spec(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "workstate-handoff-mcp": {
                        "command": "uvx",
                        "args": ["workstate-handoff-mcp@1.2.3"],
                    },
                }
            }
        )
    )
    return path


def test_mcp_sync_check_treats_malformed_mcp_json_as_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path)
    (tmp_path / ".mcp.json").write_text("{ this is not json")
    spec = _write_servers_spec(tmp_path / "servers.json")

    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--check",
        ]
    )
    capsys.readouterr()
    assert rc == 1, "malformed .mcp.json must surface as drift, not exception"


def test_mcp_sync_apply_recovers_malformed_mcp_json(tmp_path: Path) -> None:
    _seed_ledger(tmp_path)
    (tmp_path / ".mcp.json").write_text("{ this is not json")
    spec = _write_servers_spec(tmp_path / "servers.json")

    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--apply",
        ]
    )
    assert rc == 0
    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert doc["mcpServers"]["workstate-handoff-mcp"]["args"] == [
        "workstate-handoff-mcp@1.2.3"
    ]


def test_mcp_sync_check_treats_malformed_codex_toml_as_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path)
    (tmp_path / ".codex").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".codex" / "config.toml").write_text("not = a = valid = toml")
    spec = _write_servers_spec(tmp_path / "servers.json")

    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--check",
        ]
    )
    capsys.readouterr()
    assert rc == 1


def test_mcp_sync_check_treats_malformed_vscode_json_as_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path)
    (tmp_path / ".vscode").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".vscode" / "mcp.json").write_text("{[malformed")
    spec = _write_servers_spec(tmp_path / "servers.json")

    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--check",
        ]
    )
    capsys.readouterr()
    assert rc == 1


def test_doctor_treats_malformed_managed_surface_as_config_drift(
    tmp_path: Path,
) -> None:
    _seed_ledger(tmp_path)
    (tmp_path / ".mcp.json").write_text("{ this is not json")

    findings = doctor(target=tmp_path, mcp_servers=CURRENT_SERVERS)

    drifted_paths = {
        f["path"] for f in findings if f["kind"] == "config_drift"
    }
    assert ".mcp.json" in drifted_paths


def test_repair_recovers_malformed_managed_surface(tmp_path: Path) -> None:
    _seed_ledger(tmp_path)
    (tmp_path / ".mcp.json").write_text("{ this is not json")

    repair(target=tmp_path, mcp_servers=CURRENT_SERVERS)

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert doc["mcpServers"]["workstate-handoff-mcp"]["args"] == [
        "workstate-handoff-mcp@1.2.3"
    ]
