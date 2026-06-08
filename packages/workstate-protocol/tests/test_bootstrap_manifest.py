"""Schema tests for BootstrapManifest mcp_servers field.

WORKSTATE-REF-50 implementation note adds a ``mcp_servers: list[str]`` field to the manifest
so the bootstrap ledger can carry the previously-managed server names.
``sync_mcp_configs(prune_removed_managed=True)`` reads this list to
distinguish managed launchers (subject to prune) from third-party ones
(left untouched). The field is optional with a ``[]`` default so older
ledgers (no block) round-trip without losing data.
"""

from __future__ import annotations

import json
from pathlib import Path

from workstate_protocol.bootstrap import BootstrapManifest


_VALID_BASE = {
    "schema_version": 1,
    "remote_url": "https://example.invalid/repo.git",
    "remote_ref": "main",
    "remote_sha": "0" * 40,
}


def test_mcp_servers_defaults_to_empty_list() -> None:
    manifest = BootstrapManifest(**_VALID_BASE)
    assert manifest.mcp_servers == []


def test_mcp_servers_accepts_sorted_names() -> None:
    manifest = BootstrapManifest(
        **_VALID_BASE,
        mcp_servers=["workstate-handoff-mcp", "workstate-orchestrator-mcp"],
    )
    assert manifest.mcp_servers == [
        "workstate-handoff-mcp",
        "workstate-orchestrator-mcp",
    ]


def test_mcp_servers_roundtrips_through_json() -> None:
    manifest = BootstrapManifest(
        **_VALID_BASE,
        mcp_servers=["workstate-handoff-mcp"],
    )
    dumped = manifest.model_dump_json()
    parsed = BootstrapManifest.model_validate_json(dumped)
    assert parsed.mcp_servers == ["workstate-handoff-mcp"]


def test_legacy_manifest_without_mcp_servers_still_loads() -> None:
    legacy = json.dumps(_VALID_BASE)
    parsed = BootstrapManifest.model_validate_json(legacy)
    assert parsed.mcp_servers == []


def test_generated_schema_includes_mcp_servers() -> None:
    schema = BootstrapManifest.model_json_schema()
    assert "mcp_servers" in schema["properties"]
    on_disk = (
        Path(__file__).resolve().parent.parent
        / "schemas"
        / "bootstrap-manifest.json"
    )
    persisted = json.loads(on_disk.read_text())
    assert "mcp_servers" in persisted["properties"]


def test_stack_provenance_fields_roundtrip() -> None:
    manifest = BootstrapManifest(
        schema_version=5,
        source_kind="package",
        package_version="0.2.1",
        stack_distribution="workstate-stack",
        stack_version="0.1.0",
        stack_members={
            "workstate-protocol": "0.2.1",
            "workstate-system": "0.2.1",
        },
    )
    parsed = BootstrapManifest.model_validate_json(manifest.model_dump_json())
    assert parsed.stack_distribution == "workstate-stack"
    assert parsed.stack_version == "0.1.0"
    assert parsed.stack_members == {
        "workstate-protocol": "0.2.1",
        "workstate-system": "0.2.1",
    }


def test_legacy_package_manifest_without_stack_fields_still_loads() -> None:
    legacy = json.dumps(
        {
            "schema_version": 5,
            "source_kind": "package",
            "package_version": "0.2.1",
        }
    )
    parsed = BootstrapManifest.model_validate_json(legacy)
    assert parsed.stack_distribution is None
    assert parsed.stack_version is None
    assert parsed.stack_members is None


def test_worktree_manifest_requires_remote_sha() -> None:
    manifest = BootstrapManifest(
        schema_version=5,
        source_kind="worktree",
        remote_sha="a" * 40,
    )
    parsed = BootstrapManifest.model_validate_json(manifest.model_dump_json())
    assert parsed.source_kind == "worktree"
    assert parsed.remote_sha == "a" * 40


def test_generated_schema_includes_stack_fields() -> None:
    schema = BootstrapManifest.model_json_schema()
    for field in ("stack_distribution", "stack_version", "stack_members"):
        assert field in schema["properties"]
    on_disk = (
        Path(__file__).resolve().parent.parent
        / "schemas"
        / "bootstrap-manifest.json"
    )
    persisted = json.loads(on_disk.read_text())
    for field in ("stack_distribution", "stack_version", "stack_members"):
        assert field in persisted["properties"]
