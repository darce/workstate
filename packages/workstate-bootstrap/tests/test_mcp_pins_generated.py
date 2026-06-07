"""implementation note implementation note: bootstrap MCP pins are generated, not hand-maintained.

``workstate_bootstrap._mcp_pins`` (``DEFAULT_MCP_SERVERS`` +
``MCP_REGISTRATION``) is rendered from the canonical
``mcp_servers.yaml`` manifest by ``scripts/mcp_pins.py sync``. These
tests pin:

* the generated module matches a fresh render (the ``check`` gate);
* the launch specs agree with the manifest server-by-server;
* ``install.py`` re-exports the generated map (API compat);
* ``mcp_sync`` derives its default surfaces from registration
  ownership, so a harness flipped to ``plugin`` ownership loses its
  bootstrap-written root surface instead of double-registering.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import yaml

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parents[1]
MANIFEST = (
    REPO_ROOT
    / "packages"
    / "workstate-system"
    / "workstate_system"
    / "payload"
    / "config"
    / "agent-workflows"
    / "mcp_servers.yaml"
)
PINS_MODULE = PACKAGE_ROOT / "src" / "workstate_bootstrap" / "_mcp_pins.py"
GENERATOR = REPO_ROOT / "scripts" / "mcp_pins.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("_mcp_pins_generator", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["_mcp_pins_generator"] = module
    spec.loader.exec_module(module)
    return module


def test_generated_module_matches_fresh_render() -> None:
    """`make mcp-pins-check` semantics: the committed module is byte-equal
    to a fresh render of the canonical manifest."""
    generator = _load_generator()
    assert PINS_MODULE.read_text() == generator.render(), (
        "_mcp_pins.py drifts from mcp_servers.yaml; run `make mcp-pins-sync`"
    )


def test_default_mcp_servers_track_manifest() -> None:
    from workstate_bootstrap._mcp_pins import DEFAULT_MCP_SERVERS

    manifest = yaml.safe_load(MANIFEST.read_text())
    expected_names = [server["name"] for server in manifest["mcp_servers"]]
    assert list(DEFAULT_MCP_SERVERS) == expected_names
    for server in manifest["mcp_servers"]:
        entry = DEFAULT_MCP_SERVERS[server["name"]]
        assert entry["type"] == "stdio"
        assert entry["command"] == server["command"]
        assert entry["args"] == server["args"]


def test_registration_tracks_manifest() -> None:
    from workstate_bootstrap._mcp_pins import MCP_REGISTRATION

    manifest = yaml.safe_load(MANIFEST.read_text())
    assert MCP_REGISTRATION == manifest["registration"]


def test_install_reexports_generated_default_servers() -> None:
    """Callers keep importing DEFAULT_MCP_SERVERS from install (API compat)."""
    import importlib

    from workstate_bootstrap import _mcp_pins

    # `workstate_bootstrap.install` the attribute is the install() function;
    # import the module object explicitly.
    install_module = importlib.import_module("workstate_bootstrap.install")
    assert install_module.DEFAULT_MCP_SERVERS is _mcp_pins.DEFAULT_MCP_SERVERS
    assert install_module.MCP_REGISTRATION is _mcp_pins.MCP_REGISTRATION


def test_default_surfaces_follow_root_ownership(monkeypatch) -> None:
    from workstate_bootstrap import mcp_sync

    # Live table: every harness is root-owned today, so all three
    # bootstrap surfaces are written.
    assert mcp_sync.DEFAULT_SURFACES == ("claude", "vscode", "codex")

    # A harness flipped to plugin ownership must drop out of the default
    # surface set — the plugin tree becomes its only registration carrier.
    # setattr on the imported module object (not a string target): earlier
    # suite members can leave the parent package's submodule attribute
    # unresolvable for monkeypatch's string-path lookup.
    from workstate_bootstrap import _mcp_pins

    monkeypatch.setattr(
        _mcp_pins,
        "MCP_REGISTRATION",
        {"claude": "plugin", "codex": "root", "vscode": "root", "grok": "root"},
    )
    assert mcp_sync._root_owned_surfaces() == ("vscode", "codex")
