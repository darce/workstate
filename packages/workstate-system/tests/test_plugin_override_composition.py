from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from workstate_protocol.bootstrap import PluginEffectiveLock


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
GENERATOR = PAYLOAD_ROOT / "scripts" / "generate_agent_workflows.py"
MANIFEST = PAYLOAD_ROOT / "config" / "agent-workflows" / "portable_commands.json"
MCP_SERVERS_YAML = PAYLOAD_ROOT / "config" / "agent-workflows" / "mcp_servers.yaml"
SKILLS_ROOT = PAYLOAD_ROOT / "skills"


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _canonical_plugin_skill(slug: str) -> str:
    skill_dir = SKILLS_ROOT / slug
    structured = yaml.safe_load((skill_dir / "skill.yaml").read_text()) or {}
    structured.pop("generator", None)
    body = (skill_dir / "body.md").read_text()
    fm_text = yaml.safe_dump(structured, sort_keys=False, default_flow_style=False).rstrip()
    body = body if body.endswith("\n") else body + "\n"
    return f"---\n{fm_text}\n---\n\n{body}"


def test_plugin_override_replace_composes_effective_skill_and_lockfile(
    tmp_path: Path,
) -> None:
    plugin_out = tmp_path / "dist"
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    skill_override_dir = override_root / "skills" / "branch-review"
    skill_override_dir.mkdir(parents=True)
    override_skill = skill_override_dir / "SKILL.md"
    override_skill.write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nLocal branch review override body.\n"
    )

    base_skill = _canonical_plugin_skill("branch-review")
    base_digest = hashlib.sha256(base_skill.encode("utf-8")).hexdigest()
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "replace",
                            "path": "skills/branch-review/SKILL.md",
                            "upstream_digest": f"sha256:{base_digest}",
                            "on_upstream_change": "warn",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )

    proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "a" * 40,
    )

    assert proc.returncode == 0, (
        "plugin composition with a skill replacement override must succeed; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    effective_skill = plugin_out / "claude" / "skills" / "branch-review" / "SKILL.md"
    assert effective_skill.is_file()
    assert "Local branch review override body." in effective_skill.read_text()

    plugin_lock = plugin_out / "plugin-lock.json"
    assert plugin_lock.is_file(), "composition must emit a plugin-lock receipt"
    payload = json.loads(plugin_lock.read_text())
    assert payload["plugin"] == "workstate-system"
    assert payload["base_remote_sha"] == "a" * 40
    assert payload["effective_root"] == "."
    assert any(
        entry["name"] == "branch-review" and entry["mode"] == "replace"
        for entry in payload["components"]
    )
    assert PluginEffectiveLock.model_validate(payload).effective_root == "."


def test_plugin_override_add_emits_new_effective_skill(tmp_path: Path) -> None:
    plugin_out = tmp_path / "dist"
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    skill_override_dir = override_root / "skills" / "local-review"
    skill_override_dir.mkdir(parents=True)
    (skill_override_dir / "SKILL.md").write_text(
        "---\nname: local-review\ndescription: local skill\n---\n\nLocal added skill body.\n"
    )
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "local-review": {
                            "mode": "add",
                            "path": "skills/local-review/SKILL.md",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )

    proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "b" * 40,
    )

    assert proc.returncode == 0, (
        "plugin composition with an added skill must succeed; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    for harness in ("claude", "codex"):
        effective_skill = plugin_out / harness / "skills" / "local-review" / "SKILL.md"
        assert effective_skill.is_file()
        assert "Local added skill body." in effective_skill.read_text()

    payload = json.loads((plugin_out / "plugin-lock.json").read_text())
    assert any(
        entry["name"] == "local-review" and entry["mode"] == "add"
        for entry in payload["components"]
    )


def test_plugin_override_replace_warns_on_stale_upstream_digest(tmp_path: Path) -> None:
    plugin_out = tmp_path / "dist"
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    skill_override_dir = override_root / "skills" / "branch-review"
    skill_override_dir.mkdir(parents=True)
    (skill_override_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nLocal branch review override body.\n"
    )
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "replace",
                            "path": "skills/branch-review/SKILL.md",
                            "upstream_digest": "sha256:" + "0" * 64,
                            "on_upstream_change": "warn",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )

    proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "c" * 40,
    )

    assert proc.returncode == 0, (
        "on_upstream_change=warn must preserve the local override during normal composition; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    effective_skill = plugin_out / "claude" / "skills" / "branch-review" / "SKILL.md"
    assert effective_skill.is_file()
    assert "Local branch review override body." in effective_skill.read_text()

    payload = json.loads((plugin_out / "plugin-lock.json").read_text())
    assert any(
        entry["name"] == "branch-review"
        and entry["mode"] == "replace"
        and entry.get("status") == "stale"
        for entry in payload["components"]
    ), payload
    PluginEffectiveLock.model_validate(payload)

    check_proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "c" * 40,
        "--check",
    )

    assert check_proc.returncode != 0
    assert "stale override" in check_proc.stderr


def test_plugin_override_replace_ignores_stale_upstream_digest_when_configured(
    tmp_path: Path,
) -> None:
    plugin_out = tmp_path / "dist"
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    skill_override_dir = override_root / "skills" / "branch-review"
    skill_override_dir.mkdir(parents=True)
    (skill_override_dir / "SKILL.md").write_text(
        "---\nname: branch-review\ndescription: local override\n---\n\nLocal branch review override body.\n"
    )
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "replace",
                            "path": "skills/branch-review/SKILL.md",
                            "upstream_digest": "sha256:" + "0" * 64,
                            "on_upstream_change": "ignore",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )

    proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "c" * 40,
    )

    assert proc.returncode == 0, (
        "on_upstream_change=ignore must preserve the local override without failing composition; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    payload = json.loads((plugin_out / "plugin-lock.json").read_text())
    assert not any(entry.get("status") == "stale" for entry in payload["components"]), payload


def test_plugin_override_manifest_rejects_invalid_mcp_server_shape(tmp_path: Path) -> None:
    plugin_out = tmp_path / "dist"
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    override_root.mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": [],
                },
            },
            sort_keys=False,
        )
    )

    proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "f" * 40,
    )

    assert proc.returncode != 0
    assert "mcp_servers" in proc.stderr


def test_plugin_override_disable_removes_effective_skill(tmp_path: Path) -> None:
    plugin_out = tmp_path / "dist"
    base_proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
    )
    assert base_proc.returncode == 0, base_proc.stderr
    assert (plugin_out / "claude" / "skills" / "branch-review" / "SKILL.md").is_file()

    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    override_root.mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "disable",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )

    proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "d" * 40,
    )

    assert proc.returncode == 0, (
        "plugin composition with a disabled skill must succeed; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    for harness in ("claude", "codex"):
        assert not (plugin_out / harness / "skills" / "branch-review" / "SKILL.md").exists()

    stale_skill = plugin_out / "claude" / "skills" / "branch-review" / "SKILL.md"
    stale_skill.parent.mkdir(parents=True)
    stale_skill.write_text("stale disabled skill body\n")
    check_proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(plugin_out),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        "d" * 40,
        "--check",
    )
    assert check_proc.returncode != 0
    assert "unexpected plugin tree file" in check_proc.stderr

    payload = json.loads((plugin_out / "plugin-lock.json").read_text())
    assert any(
        entry["name"] == "branch-review" and entry["mode"] == "disable"
        for entry in payload["components"]
    )