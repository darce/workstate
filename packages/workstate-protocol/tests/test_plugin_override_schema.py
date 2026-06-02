from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from workstate_protocol.bootstrap import (
    PluginEffectiveLock,
    PluginMcpServerPatch,
    PluginOverrideLock,
    PluginOverrideManifest,
)


def test_override_manifest_accepts_skill_and_mcp_components() -> None:
    manifest = PluginOverrideManifest.model_validate(
        {
            "schema_version": 1,
            "plugin": "workstate-system",
            "components": {
                "skills": {
                    "branch-review": {
                        "mode": "replace",
                        "path": "skills/branch-review/SKILL.md",
                        "upstream_digest": "sha256:" + "a" * 64,
                        "on_upstream_change": "warn",
                    },
                    "review-parallel": {
                        "mode": "disable",
                    },
                },
                "mcp_servers": {
                    "workstate-handoff-mcp": {
                        "mode": "patch",
                        "patch_path": "tools/mcp_servers.patch.yaml",
                        "requires_trust_ack": True,
                    }
                },
            },
        }
    )

    assert manifest.plugin == "workstate-system"
    assert manifest.components.skills["branch-review"].mode == "replace"
    assert manifest.components.mcp_servers["workstate-handoff-mcp"].mode == "patch"


def test_mcp_server_patch_accepts_typed_operations() -> None:
    patch = PluginMcpServerPatch.model_validate(
        {
            "schema_version": 1,
            "target_server": "workstate-handoff-mcp",
            "ops": [
                {"op": "replace_command", "value": "uvx"},
                {
                    "op": "replace_args",
                    "value": ["mcp-workstate-handoff@0.11.4", "--profile", "consumer"],
                },
                {"op": "upsert_env", "name": "HANDOFF_PROFILE", "value": "consumer"},
                {"op": "remove_env", "name": "LEGACY_FLAG"},
            ],
        }
    )

    assert [entry.op for entry in patch.ops] == [
        "replace_command",
        "replace_args",
        "upsert_env",
        "remove_env",
    ]


def test_mcp_server_patch_rejects_wildcard_and_unsupported_file_replacement() -> None:
    with pytest.raises(ValidationError):
        PluginMcpServerPatch.model_validate(
            {
                "schema_version": 1,
                "target_server": "*",
                "ops": [
                    {
                        "op": "replace_file",
                        "path": "../../../tmp/override.sh",
                        "value": "hacked",
                    }
                ],
            }
        )


def test_lock_models_roundtrip() -> None:
    override_lock = PluginOverrideLock.model_validate(
        {
            "schema_version": 1,
            "plugin": "workstate-system",
            "base_remote_sha": "b" * 40,
            "components": [
                {
                    "component_kind": "skill",
                    "name": "branch-review",
                    "mode": "replace",
                    "local_path": "skills/branch-review/SKILL.md",
                    "upstream_digest": "sha256:" + "c" * 64,
                }
            ],
        }
    )
    effective_lock = PluginEffectiveLock.model_validate(
        {
            "schema_version": 1,
            "plugin": "workstate-system",
            "base_remote_sha": "d" * 40,
            "effective_root": ".workstate/generated/plugins/workstate-system/effective/claude",
            "components": [
                {
                    "component_kind": "skill",
                    "name": "branch-review",
                    "mode": "replace",
                    "effective_digest": "sha256:" + "e" * 64,
                }
            ],
        }
    )

    assert PluginOverrideLock.model_validate_json(override_lock.model_dump_json()) == override_lock
    assert PluginEffectiveLock.model_validate_json(effective_lock.model_dump_json()) == effective_lock


def test_generated_schema_artifacts_include_plugin_override_models() -> None:
    artifact_dir = Path(__file__).resolve().parent.parent / "schemas"
    expected = {
        "plugin-override-manifest.json": PluginOverrideManifest,
        "plugin-override-lock.json": PluginOverrideLock,
        "plugin-effective-lock.json": PluginEffectiveLock,
        "plugin-mcp-server-patch.json": PluginMcpServerPatch,
    }

    for file_name, model in expected.items():
        schema = model.model_json_schema()
        assert "schema_version" in schema["properties"]

        on_disk = artifact_dir / file_name
        persisted = json.loads(on_disk.read_text())
        assert "schema_version" in persisted["properties"]
