"""implementation note Slice B — distributed MCP server identity cutover.

The two distributed runtime-managed servers were renamed from their legacy
``agent-*`` identities to Workstate-native names:

    agent-handoff-mcp       -> workstate-handoff-mcp
    agent-orchestrator-mcp  -> workstate-orchestrator-mcp

These tests pin the read-side compatibility behaviour:

- a fresh install / sync emits only the two canonical names, a single
  registration each (no duplicate old+new),
- an existing consumer config carrying the legacy ``agent-*`` name is
  rewritten forward to the canonical name (never left as a stale duplicate),
  across all three client surfaces,
- the update preserve-path rewrites an old-named ``.mcp.json`` registration
  forward,
- ``design-canvas-mcp`` is intentionally NOT in the rename map (D1).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import tomlkit

from workstate_bootstrap.install import (
    DEFAULT_MCP_SERVERS,
    LEGACY_MCP_SERVER_RENAMES,
    _legacy_prune_for,
    _render_codex_config,
    _render_mcp_json,
    _render_vscode_mcp_json,
)
from workstate_bootstrap.mcp_sync import sync_mcp_configs


CANONICAL_SERVERS = {
    "workstate-handoff-mcp": {
        "command": "uvx",
        "args": ["mcp-workstate-handoff@1.2.3", "serve-stdio"],
    },
    "workstate-orchestrator-mcp": {
        "command": "uvx",
        "args": ["mcp-workstate-orchestrator@1.2.3", "serve-stdio"],
    },
}


def _write_claude(target: Path, servers: dict[str, dict]) -> None:
    (target / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}, indent=2) + "\n"
    )


def _write_vscode(target: Path, servers: dict[str, dict]) -> None:
    (target / ".vscode").mkdir(parents=True, exist_ok=True)
    (target / ".vscode" / "mcp.json").write_text(
        json.dumps({"servers": servers}, indent=2) + "\n"
    )


def _write_codex(target: Path, servers: dict[str, dict]) -> None:
    (target / ".codex").mkdir(parents=True, exist_ok=True)
    doc = tomlkit.document()
    doc["mcp_servers"] = tomlkit.table(is_super_table=True)
    for name, spec in servers.items():
        table = tomlkit.table()
        for k, v in spec.items():
            table[k] = v
        doc["mcp_servers"][name] = table
    (target / ".codex" / "config.toml").write_text(tomlkit.dumps(doc))


# ---------------------------------------------------------------------------
# rename-map / defaults
# ---------------------------------------------------------------------------


def test_default_servers_use_only_canonical_names() -> None:
    """The built-in managed map carries only Workstate-native names —
    the writer never emits a legacy ``agent-*`` registration on its own."""
    assert set(DEFAULT_MCP_SERVERS) == {
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    }


def test_canvas_identity_kept_private_d1() -> None:
    """D1: design-canvas-mcp is intentionally not renamed (kept private)."""
    assert "design-canvas-mcp" not in LEGACY_MCP_SERVER_RENAMES
    assert "design-canvas-mcp" not in LEGACY_MCP_SERVER_RENAMES.values()
    assert set(LEGACY_MCP_SERVER_RENAMES) == {
        "agent-handoff-mcp",
        "agent-orchestrator-mcp",
    }


def test_legacy_prune_only_when_canonical_present() -> None:
    """A legacy name is pruned only when its canonical replacement is in
    the managed map — and is a no-op for a map with no canonical match."""
    assert set(_legacy_prune_for(CANONICAL_SERVERS, ())) == {
        "agent-handoff-mcp",
        "agent-orchestrator-mcp",
    }
    # Only handoff canonical present -> only handoff legacy pruned.
    handoff_only = {"workstate-handoff-mcp": CANONICAL_SERVERS["workstate-handoff-mcp"]}
    assert set(_legacy_prune_for(handoff_only, ())) == {"agent-handoff-mcp"}
    # No canonical names -> no legacy prune.
    assert _legacy_prune_for({"some-third-party": {}}, ()) == ()


# ---------------------------------------------------------------------------
# fresh install: single registration, no legacy
# ---------------------------------------------------------------------------


def test_fresh_install_single_canonical_registration(tmp_path: Path) -> None:
    """No pre-existing config: the writer emits exactly the two canonical
    names, one registration each, no legacy duplicate."""
    sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=False)

    claude = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(claude["mcpServers"]) == {
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    }
    assert "agent-handoff-mcp" not in claude["mcpServers"]
    assert "agent-orchestrator-mcp" not in claude["mcpServers"]


# ---------------------------------------------------------------------------
# upgrade: legacy registration rewritten forward (collapse old+new)
# ---------------------------------------------------------------------------


def test_legacy_only_config_rewritten_forward_claude(tmp_path: Path) -> None:
    """An existing ``.mcp.json`` with only the legacy name is rewritten
    forward to the canonical name (no stale duplicate left behind)."""
    _write_claude(
        tmp_path,
        {
            "agent-handoff-mcp": {"command": "OLD", "args": []},
            "agent-orchestrator-mcp": {"command": "OLD", "args": []},
        },
    )

    sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=False)

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert "agent-handoff-mcp" not in doc["mcpServers"]
    assert "agent-orchestrator-mcp" not in doc["mcpServers"]
    assert set(doc["mcpServers"]) == {
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    }
    # Forward-written spec is the canonical managed spec, not the OLD stub.
    assert doc["mcpServers"]["workstate-handoff-mcp"]["command"] == "uvx"


def test_duplicate_old_plus_new_collapses_to_one(tmp_path: Path) -> None:
    """The exact current-state bug: a config registering BOTH the legacy
    and the canonical name collapses to a single canonical registration."""
    _write_claude(
        tmp_path,
        {
            "agent-handoff-mcp": {"command": "uvx", "args": ["legacy"]},
            "workstate-handoff-mcp": {"command": "uv", "args": ["new"]},
            "agent-orchestrator-mcp": {"command": "uvx", "args": ["legacy"]},
            "workstate-orchestrator-mcp": {"command": "uv", "args": ["new"]},
        },
    )

    sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=False)

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert set(doc["mcpServers"]) == {
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    }


def test_legacy_rewrite_across_all_three_surfaces(tmp_path: Path) -> None:
    legacy = {
        "agent-handoff-mcp": {"command": "OLD", "args": []},
        "agent-orchestrator-mcp": {"command": "OLD", "args": []},
    }
    _write_claude(tmp_path, legacy)
    _write_vscode(tmp_path, legacy)
    _write_codex(tmp_path, legacy)

    sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=False)

    claude = json.loads((tmp_path / ".mcp.json").read_text())
    vscode = json.loads((tmp_path / ".vscode" / "mcp.json").read_text())
    codex = tomlkit.parse((tmp_path / ".codex" / "config.toml").read_text())
    for surface in (claude["mcpServers"], vscode["servers"], dict(codex["mcp_servers"])):
        assert "agent-handoff-mcp" not in surface
        assert "agent-orchestrator-mcp" not in surface
        assert "workstate-handoff-mcp" in surface
        assert "workstate-orchestrator-mcp" in surface


def test_legacy_not_reported_as_third_party(tmp_path: Path) -> None:
    """A renamed legacy entry is NOT surfaced as a preserved third-party
    launcher (it is being rewritten forward, not preserved)."""
    _write_claude(tmp_path, {"agent-handoff-mcp": {"command": "OLD", "args": []}})

    report = sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=False)

    assert "agent-handoff-mcp" not in report.preserved_third_party


def test_third_party_launcher_preserved_through_rename(tmp_path: Path) -> None:
    """A genuinely third-party launcher survives the legacy rewrite."""
    _write_claude(
        tmp_path,
        {
            "agent-handoff-mcp": {"command": "OLD", "args": []},
            "user-private-tool": {"command": "node", "args": ["./local.js"]},
        },
    )

    sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=False)

    doc = json.loads((tmp_path / ".mcp.json").read_text())
    assert doc["mcpServers"]["user-private-tool"] == {
        "command": "node",
        "args": ["./local.js"],
    }
    assert "agent-handoff-mcp" not in doc["mcpServers"]


def test_idempotent_canonical_only_config_unchanged(tmp_path: Path) -> None:
    """A config that already holds only the canonical names renders to the
    same bytes (the legacy prune is a no-op when nothing legacy is present)."""
    sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=False)
    first = (tmp_path / ".mcp.json").read_bytes()
    report = sync_mcp_configs(tmp_path, CANONICAL_SERVERS, check_only=True)
    assert report.exit_code == 0
    assert (tmp_path / ".mcp.json").read_bytes() == first


# ---------------------------------------------------------------------------
# update preserve-path rewrites the old name forward
# ---------------------------------------------------------------------------


def test_preserved_mcp_servers_rewrites_legacy_forward(tmp_path: Path) -> None:
    from workstate_bootstrap.subcommands import _preserved_mcp_servers

    _write_claude(
        tmp_path,
        {
            "agent-handoff-mcp": {"command": "uvx", "args": ["legacy"]},
            "agent-orchestrator-mcp": {"command": "uvx", "args": ["legacy"]},
        },
    )
    manifest = {"configs": [{"path": ".mcp.json", "action": "created"}]}

    preserved = _preserved_mcp_servers(tmp_path, manifest)

    assert preserved is not None
    assert set(preserved) == {
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    }


def test_preserved_mcp_servers_canonical_wins_over_legacy(tmp_path: Path) -> None:
    """When both legacy and canonical are registered, the canonical spec
    wins and the legacy one is dropped (never resurrect the old name)."""
    from workstate_bootstrap.subcommands import _preserved_mcp_servers

    _write_claude(
        tmp_path,
        {
            "workstate-handoff-mcp": {"command": "NEW", "args": []},
            "agent-handoff-mcp": {"command": "OLD", "args": []},
        },
    )
    manifest = {"configs": [{"path": ".mcp.json", "action": "created"}]}

    preserved = _preserved_mcp_servers(tmp_path, manifest)

    assert preserved is not None
    assert set(preserved) == {"workstate-handoff-mcp"}
    assert preserved["workstate-handoff-mcp"]["command"] == "NEW"


# ---------------------------------------------------------------------------
# render-seam byte-level guard
# ---------------------------------------------------------------------------


def test_render_seams_strip_legacy_tokens(tmp_path: Path) -> None:
    legacy = {
        "agent-handoff-mcp": {"command": "OLD", "args": []},
        "agent-orchestrator-mcp": {"command": "OLD", "args": []},
    }
    _write_claude(tmp_path, legacy)
    _write_vscode(tmp_path, legacy)
    _write_codex(tmp_path, legacy)

    claude_bytes = _render_mcp_json(tmp_path, CANONICAL_SERVERS)
    vscode_bytes = _render_vscode_mcp_json(tmp_path, CANONICAL_SERVERS)
    codex_bytes = _render_codex_config(tmp_path, CANONICAL_SERVERS)

    for rendered in (claude_bytes, vscode_bytes, codex_bytes):
        assert b"agent-handoff-mcp" not in rendered
        assert b"agent-orchestrator-mcp" not in rendered
        assert b"workstate-handoff-mcp" in rendered


# ---------------------------------------------------------------------------
# operational-surface guard: no live surface still advertises the old names
# ---------------------------------------------------------------------------


def test_no_operational_surface_advertises_legacy_server_names() -> None:
    """Grep guard: outside of historical docs / CHANGELOGs, the intentional
    rename-map + read-side fallbacks, and backward-compat hook matchers, no
    operational surface still advertises the legacy distributed server names.

    This is the Slice B regression net for the identity cutover. The
    allow-list below enumerates the *intentional* compat-detection strings.
    """
    repo_root = Path(__file__).resolve().parents[3]
    try:
        out = subprocess.run(
            [
                "git",
                "grep",
                "-l",
                "-e",
                "agent-handoff-mcp",
                "-e",
                "agent-orchestrator-mcp",
                "--",
                ":!*.md",
                ":!docs/plans/**",
                ":!*CHANGELOG*",
            ],
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:  # pragma: no cover - git always present in CI
        return
    if out.returncode not in (0, 1):  # pragma: no cover
        raise AssertionError(f"git grep failed: {out.stderr}")

    hits = {line for line in out.stdout.splitlines() if line.strip()}

    # Intentional, reviewed carve-outs (compat detection / test fixtures).
    allowed = {
        # The legacy rename map + read-side fallbacks live here.
        "packages/workstate-bootstrap/src/workstate_bootstrap/install.py",
        "packages/workstate-bootstrap/src/workstate_bootstrap/subcommands.py",
        # Backward-compat hook matchers keep the legacy tool-prefix as an
        # alternative alongside the new mcp__workstate-*-mcp prefix.
        "packages/workstate-system/.github/hooks/terminal-guard.json",
        # Test fixtures that use arbitrary package *paths* (not identities).
        "packages/workstate-system/.github/hooks/test_guard_main_branch.py",
        # This test file itself names the legacy tokens.
        "packages/workstate-bootstrap/tests/test_mcp_legacy_rename.py",
    }
    unexpected = sorted(hits - allowed)
    assert not unexpected, (
        "operational surfaces still advertise legacy MCP server identities "
        f"(rename them or add to the reviewed allow-list): {unexpected}"
    )
