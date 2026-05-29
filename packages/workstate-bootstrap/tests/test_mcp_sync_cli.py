"""CLI tests for ``workstate-bootstrap mcp-sync`` (WORKSTATE-REF-50 implementation note).

Exit-code contract:
- ``0``: clean reconcile (apply, or check with no drift).
- ``1``: drift detected with ``--check``.
- ``2`` (or higher): resolution failure raised before reaching
  ``sync_mcp_configs`` (e.g. ``--target`` missing, ``--mcp-servers``
  unparseable).

JSON shape is pinned per the plan (implementation note changes, line 153):
``{"surfaces": [{"name", "path", "drift", "action"}],
   "preserved_third_party": [...], "pruned_managed": [...],
   "ledger_mcp_servers": [...], "exit_code": int}``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_bootstrap.cli import main as cli_main
from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION


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


def _write_servers_json(path: Path) -> Path:
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


def test_check_exits_one_when_surfaces_drift(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    spec = _write_servers_json(tmp_path / "servers.json")
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
    assert rc == 1
    assert not (tmp_path / ".mcp.json").exists()


def test_check_exits_zero_on_clean_target(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    spec = _write_servers_json(tmp_path / "servers.json")
    cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--apply",
        ]
    )
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
    assert rc == 0


def test_apply_writes_surfaces_and_exits_zero(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    spec = _write_servers_json(tmp_path / "servers.json")
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
    assert (tmp_path / ".mcp.json").exists()


def test_default_action_is_apply_when_neither_flag_set(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    spec = _write_servers_json(tmp_path / "servers.json")
    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
        ]
    )
    assert rc == 0
    assert (tmp_path / ".mcp.json").exists()


def test_check_and_apply_mutex(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    spec = _write_servers_json(tmp_path / "servers.json")
    with pytest.raises(SystemExit) as exc:
        cli_main(
            [
                "mcp-sync",
                "--target",
                str(tmp_path),
                "--mcp-servers",
                str(spec),
                "--check",
                "--apply",
            ]
        )
    assert exc.value.code == 2


def test_surfaces_filter_limits_writes(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    spec = _write_servers_json(tmp_path / "servers.json")
    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--surfaces",
            "claude",
            "--apply",
        ]
    )
    assert rc == 0
    assert (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".vscode" / "mcp.json").exists()
    assert not (tmp_path / ".codex" / "config.toml").exists()


def test_json_output_matches_pinned_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    spec = _write_servers_json(tmp_path / "servers.json")
    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--check",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert set(payload) == {
        "surfaces",
        "preserved_third_party",
        "pruned_managed",
        "ledger_mcp_servers",
        "exit_code",
    }
    for s in payload["surfaces"]:
        assert set(s) >= {"name", "path", "drift", "action"}
        assert s["action"] in {"created", "merged", "unchanged", "would_write"}
    assert payload["exit_code"] == rc == 1


def test_human_output_is_non_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    spec = _write_servers_json(tmp_path / "servers.json")
    cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--apply",
        ]
    )
    captured = capsys.readouterr()
    assert captured.out.strip(), "human-readable output should not be empty"


def test_prune_removed_managed_drops_stale_keys(tmp_path: Path) -> None:
    _seed_ledger(
        tmp_path,
        mcp_servers=["workstate-handoff-mcp", "workstate-orchestrator-mcp"],
    )
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "workstate-handoff-mcp": {"command": "OLD"},
                    "workstate-orchestrator-mcp": {"command": "OLD"},
                }
            }
        )
    )
    spec = _write_servers_json(tmp_path / "servers.json")
    rc = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--apply",
            "--prune-removed-managed",
        ]
    )
    assert rc == 0
    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "workstate-orchestrator-mcp" not in doc["mcpServers"]
