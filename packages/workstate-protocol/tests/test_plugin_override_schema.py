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


def test_override_manifest_accepts_portable_command_add_component() -> None:
    manifest = PluginOverrideManifest.model_validate(
        {
            "schema_version": 1,
            "plugin": "workstate-system",
            "components": {
                "portable_commands": {
                    "local-refactor": {
                        "mode": "add",
                        "path": "portable_commands/local-refactor.json",
                    }
                }
            },
        }
    )

    assert manifest.components.portable_commands["local-refactor"].mode == "add"


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

    assert (
        PluginOverrideLock.model_validate_json(override_lock.model_dump_json())
        == override_lock
    )
    assert (
        PluginEffectiveLock.model_validate_json(effective_lock.model_dump_json())
        == effective_lock
    )


def test_skill_patch_mode_roundtrip() -> None:
    manifest = PluginOverrideManifest.model_validate(
        {
            "schema_version": 1,
            "plugin": "workstate-system",
            "components": {
                "skills": {
                    "branch-review": {
                        "mode": "patch",
                        "path": "skills/branch-review/SKILL.md",
                        "base_path": "skills/branch-review/SKILL.base.md",
                        "upstream_digest": "sha256:" + "a" * 64,
                        "on_upstream_change": "warn",
                    }
                }
            },
        }
    )

    override = manifest.components.skills["branch-review"]
    assert override.mode == "patch"
    assert override.base_path == "skills/branch-review/SKILL.base.md"
    assert (
        PluginOverrideManifest.model_validate_json(manifest.model_dump_json())
        == manifest
    )


def test_skill_patch_mode_requires_path_base_path_and_digest() -> None:
    base = {
        "mode": "patch",
        "path": "skills/branch-review/SKILL.md",
        "base_path": "skills/branch-review/SKILL.base.md",
        "upstream_digest": "sha256:" + "a" * 64,
    }
    for missing in ("path", "base_path", "upstream_digest"):
        payload = {key: value for key, value in base.items() if key != missing}
        with pytest.raises(ValidationError):
            PluginOverrideManifest.model_validate(
                {
                    "schema_version": 1,
                    "plugin": "workstate-system",
                    "components": {"skills": {"branch-review": payload}},
                }
            )


def test_skill_non_patch_modes_reject_base_path() -> None:
    with pytest.raises(ValidationError):
        PluginOverrideManifest.model_validate(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "replace",
                            "path": "skills/branch-review/SKILL.md",
                            "base_path": "skills/branch-review/SKILL.base.md",
                            "upstream_digest": "sha256:" + "a" * 64,
                        }
                    }
                },
            }
        )


def test_override_manifest_rejects_paths_outside_override_root() -> None:
    base_skill = {
        "mode": "patch",
        "path": "skills/branch-review/SKILL.md",
        "base_path": "skills/branch-review/SKILL.base.md",
        "upstream_digest": "sha256:" + "a" * 64,
    }

    for field, value in (
        ("path", "../outside/SKILL.md"),
        ("path", "/tmp/SKILL.md"),
        ("base_path", "skills/../../outside.base.md"),
    ):
        payload = {**base_skill, field: value}
        with pytest.raises(ValidationError):
            PluginOverrideManifest.model_validate(
                {
                    "schema_version": 1,
                    "plugin": "workstate-system",
                    "components": {"skills": {"branch-review": payload}},
                }
            )

    with pytest.raises(ValidationError):
        PluginOverrideManifest.model_validate(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": {
                        "workstate-handoff-mcp": {
                            "mode": "patch",
                            "patch_path": "../server.patch.yaml",
                            "requires_trust_ack": True,
                        }
                    }
                },
            }
        )


def test_effective_lock_supports_merge_conflict_and_passthrough() -> None:
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
                    "mode": "patch",
                    "effective_digest": "sha256:" + "e" * 64,
                    "status": "merge_conflict",
                    "override_path": "skills/branch-review/SKILL.md",
                    "recorded_upstream_digest": "sha256:" + "f" * 64,
                    "current_base_digest": "sha256:" + "0" * 64,
                },
                {
                    "component_kind": "skill",
                    "name": "scope",
                    "mode": "passthrough",
                    "effective_digest": "sha256:" + "1" * 64,
                },
            ],
        }
    )

    statuses = [entry.status for entry in effective_lock.components]
    modes = [entry.mode for entry in effective_lock.components]
    assert statuses == ["merge_conflict", None]
    assert modes == ["patch", "passthrough"]
    assert (
        PluginEffectiveLock.model_validate_json(effective_lock.model_dump_json())
        == effective_lock
    )


def test_override_lock_accept_upstream_provenance_roundtrip() -> None:
    override_lock = PluginOverrideLock.model_validate(
        {
            "schema_version": 1,
            "plugin": "workstate-system",
            "base_remote_sha": "b" * 40,
            "components": [
                {
                    "component_kind": "skill",
                    "name": "branch-review",
                    "mode": "patch",
                    "local_path": "skills/branch-review/SKILL.md",
                    "base_path": "skills/branch-review/SKILL.base.md",
                    "upstream_digest": "sha256:" + "c" * 64,
                    "last_accept_upstream": {
                        "previous_upstream_digest": "sha256:" + "9" * 64,
                        "new_upstream_digest": "sha256:" + "c" * 64,
                        "accepted_at": "2026-06-04T06:00:00Z",
                    },
                }
            ],
        }
    )

    entry = override_lock.components[0]
    assert entry.last_accept_upstream is not None
    assert entry.last_accept_upstream.new_upstream_digest == "sha256:" + "c" * 64
    assert (
        PluginOverrideLock.model_validate_json(override_lock.model_dump_json())
        == override_lock
    )


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
