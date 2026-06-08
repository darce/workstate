"""Doctor + repair integration with ``sync_mcp_configs`` (WORKSTATE-REF-50 implementation note).

Doctor calls ``sync_mcp_configs(check_only=True)`` and emits one
``config_drift`` finding per drifted surface. Repair, when given
``mcp_servers``, delegates to ``sync_mcp_configs(check_only=False)`` so
the three managed surfaces (.mcp.json, .vscode/mcp.json,
.codex/config.toml) are reconciled through the same render seam the
``mcp-sync`` subcommand uses.

Tests use a seeded-ledger fixture (no ``install()`` call) so they
exercise the integration without depending on the worktree env having
``workstate_handoff_mcp`` available for ``init-state``.
"""

from __future__ import annotations

import json
from pathlib import Path

import tomlkit

from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.subcommands import doctor, repair


CURRENT_SERVERS = {
    "workstate-handoff-mcp": {
        "command": "uvx",
        "args": ["workstate-handoff-mcp@1.2.3"],
    },
}


def _seed_ledger_with_managed_configs(
    target: Path, *, mcp_servers: list[str]
) -> None:
    """Seed a ledger that records all three managed config surfaces.

    The ``configs`` array drives doctor's "did install register MCP
    servers?" check; the ``mcp_servers`` block drives prune provenance.
    """
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
        "mcp_servers": mcp_servers,
    }
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2) + "\n"
    )
    # State drift is suppressed when handoff.db is present.
    (target / ".task-state").mkdir(parents=True, exist_ok=True)
    (target / ".task-state" / "handoff.db").write_bytes(b"")


def _seed_clean_managed_surfaces(target: Path) -> None:
    """Render the three surfaces with the *current* spec so doctor has
    no managed drift to report."""
    (target / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {k: dict(v) for k, v in CURRENT_SERVERS.items()}},
            indent=2,
        )
        + "\n"
    )
    (target / ".vscode").mkdir(parents=True, exist_ok=True)
    (target / ".vscode" / "mcp.json").write_text(
        json.dumps(
            {"servers": {k: dict(v) for k, v in CURRENT_SERVERS.items()}},
            indent=2,
        )
        + "\n"
    )
    (target / ".codex").mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table(is_super_table=True)
    for name, spec in CURRENT_SERVERS.items():
        table = tomlkit.table()
        for key, value in spec.items():
            table[key] = value
        doc["mcp_servers"][name] = table
    (target / ".codex" / "config.toml").write_text(tomlkit.dumps(doc))


def _seed_stale_managed_surfaces(target: Path) -> None:
    (target / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"workstate-handoff-mcp": {"command": "OLD"}}}, indent=2
        )
        + "\n"
    )
    (target / ".vscode").mkdir(parents=True, exist_ok=True)
    (target / ".vscode" / "mcp.json").write_text(
        json.dumps(
            {"servers": {"workstate-handoff-mcp": {"command": "OLD"}}}, indent=2
        )
        + "\n"
    )
    (target / ".codex").mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table(is_super_table=True)
    table = tomlkit.table()
    table["command"] = "OLD"
    doc["mcp_servers"]["workstate-handoff-mcp"] = table
    (target / ".codex" / "config.toml").write_text(tomlkit.dumps(doc))


def test_doctor_emits_config_drift_for_each_drifted_surface(
    tmp_path: Path,
) -> None:
    """implementation note: doctor surfaces drift via sync_mcp_configs(check_only=True)
    for all three managed surfaces, including .codex/config.toml."""
    _seed_ledger_with_managed_configs(
        tmp_path, mcp_servers=["workstate-handoff-mcp"]
    )
    _seed_stale_managed_surfaces(tmp_path)

    findings = doctor(target=tmp_path, mcp_servers=CURRENT_SERVERS)

    drifted_paths = {
        f["path"] for f in findings if f["kind"] == "config_drift"
    }
    assert drifted_paths == {
        ".mcp.json",
        ".vscode/mcp.json",
        ".codex/config.toml",
    }


def test_doctor_clean_managed_surfaces_no_config_drift(
    tmp_path: Path,
) -> None:
    _seed_ledger_with_managed_configs(
        tmp_path, mcp_servers=["workstate-handoff-mcp"]
    )
    _seed_clean_managed_surfaces(tmp_path)

    findings = doctor(target=tmp_path, mcp_servers=CURRENT_SERVERS)

    assert all(f["kind"] != "config_drift" for f in findings), findings


def test_doctor_without_mcp_servers_skips_config_drift_check(
    tmp_path: Path,
) -> None:
    """When the caller does not pass mcp_servers, doctor cannot know what
    the managed map should look like, so config_drift must not appear."""
    _seed_ledger_with_managed_configs(
        tmp_path, mcp_servers=["workstate-handoff-mcp"]
    )
    _seed_stale_managed_surfaces(tmp_path)

    findings = doctor(target=tmp_path, mcp_servers=None)

    assert all(f["kind"] != "config_drift" for f in findings), findings


def test_repair_uses_sync_mcp_configs_for_all_three_surfaces(
    tmp_path: Path,
) -> None:
    """implementation note: repair --mcp-servers reconciles via sync_mcp_configs so
    .codex/config.toml is now repaired alongside the two JSON surfaces."""
    _seed_ledger_with_managed_configs(
        tmp_path, mcp_servers=["workstate-handoff-mcp"]
    )
    _seed_stale_managed_surfaces(tmp_path)

    report = repair(target=tmp_path, mcp_servers=CURRENT_SERVERS)

    repaired_paths = {f["path"] for f in report["repaired"]}
    assert {".mcp.json", ".vscode/mcp.json", ".codex/config.toml"} <= repaired_paths

    claude = json.loads((tmp_path / ".mcp.json").read_text())
    assert claude["mcpServers"]["workstate-handoff-mcp"]["args"] == [
        "workstate-handoff-mcp@1.2.3"
    ]
    vscode = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    assert vscode["servers"]["workstate-handoff-mcp"]["args"] == [
        "workstate-handoff-mcp@1.2.3"
    ]
    codex = tomlkit.parse((tmp_path / ".codex" / "config.toml").read_text())
    assert dict(codex["mcp_servers"]["workstate-handoff-mcp"])["args"] == [
        "workstate-handoff-mcp@1.2.3"
    ]


def test_repair_preserves_third_party_launcher(tmp_path: Path) -> None:
    """A user-added launcher (not in ledger.mcp_servers) survives repair."""
    _seed_ledger_with_managed_configs(
        tmp_path, mcp_servers=["workstate-handoff-mcp"]
    )
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "workstate-handoff-mcp": {"command": "OLD"},
                    "user-private-tool": {
                        "command": "node",
                        "args": ["./local.js"],
                    },
                }
            },
            indent=2,
        )
        + "\n"
    )
    (tmp_path / ".vscode").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".vscode" / "mcp.json").write_text(
        json.dumps(
            {"servers": {"workstate-handoff-mcp": {"command": "OLD"}}}, indent=2
        )
        + "\n"
    )
    (tmp_path / ".codex").mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table(is_super_table=True)
    table = tomlkit.table()
    table["command"] = "OLD"
    doc["mcp_servers"]["workstate-handoff-mcp"] = table
    (tmp_path / ".codex" / "config.toml").write_text(tomlkit.dumps(doc))

    repair(target=tmp_path, mcp_servers=CURRENT_SERVERS)

    claude = json.loads((tmp_path / ".mcp.json").read_text())
    assert claude["mcpServers"]["user-private-tool"] == {
        "command": "node",
        "args": ["./local.js"],
    }
