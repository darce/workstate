"""Unit tests for the manifest-build step in install.py.

WORKSTATE-REF-50 implementation note: when ``install()`` is called with managed
``mcp_servers``, the written ``.workstate-bootstrap.json`` ledger must
record ``mcp_servers=sorted(servers.keys())`` so future
``sync_mcp_configs(prune_removed_managed=True)`` runs know which
launchers were managed by bootstrap (vs. third-party entries the
consumer added themselves).

The integration-level install() flow is exercised in test_install.py;
this file pins the dict-shape contract via a pure helper so the assertion
runs without git clones or subprocess setup.
"""

from __future__ import annotations

from workstate_bootstrap.install import PROFILE_ALL, _build_install_manifest


_REMOTE_URL = "file:///tmp/fake-remote.git"
_REMOTE_REF = "main"
_REMOTE_SHA = "0" * 40


def test_build_manifest_records_sorted_mcp_servers() -> None:
    servers = {
        "workstate-orchestrator-mcp": {"command": "uvx", "args": ["x"]},
        "workstate-handoff-mcp": {"command": "uvx", "args": ["y"]},
    }
    manifest = _build_install_manifest(
        remote_url=_REMOTE_URL,
        remote_ref=_REMOTE_REF,
        remote_sha=_REMOTE_SHA,
        profile=PROFILE_ALL,
        surfaces=[],
        configs=[],
        mcp_servers=servers,
        plugin_overrides_path=None,
    )
    assert manifest["mcp_servers"] == [
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    ]


def test_build_manifest_empty_mcp_servers_when_none() -> None:
    manifest = _build_install_manifest(
        remote_url=_REMOTE_URL,
        remote_ref=_REMOTE_REF,
        remote_sha=_REMOTE_SHA,
        profile=PROFILE_ALL,
        surfaces=[],
        configs=[],
        mcp_servers=None,
        plugin_overrides_path=None,
    )
    assert manifest["mcp_servers"] == []


def test_build_manifest_empty_mcp_servers_when_mapping_empty() -> None:
    manifest = _build_install_manifest(
        remote_url=_REMOTE_URL,
        remote_ref=_REMOTE_REF,
        remote_sha=_REMOTE_SHA,
        profile=PROFILE_ALL,
        surfaces=[],
        configs=[],
        mcp_servers={},
        plugin_overrides_path=None,
    )
    assert manifest["mcp_servers"] == []


def test_build_manifest_carries_remote_provenance() -> None:
    manifest = _build_install_manifest(
        remote_url=_REMOTE_URL,
        remote_ref=_REMOTE_REF,
        remote_sha=_REMOTE_SHA,
        profile=PROFILE_ALL,
        surfaces=[{"path": ".claude/skills/x", "source": "shared"}],
        configs=[{"path": ".mcp.json", "action": "created"}],
        mcp_servers={"workstate-handoff-mcp": {"command": "uvx", "args": []}},
        plugin_overrides_path=None,
    )
    assert manifest["remote_url"] == _REMOTE_URL
    assert manifest["remote_ref"] == _REMOTE_REF
    assert manifest["remote_sha"] == _REMOTE_SHA
    assert manifest["surfaces"] == [{"path": ".claude/skills/x", "source": "shared"}]
    assert manifest["configs"] == [{"path": ".mcp.json", "action": "created"}]
