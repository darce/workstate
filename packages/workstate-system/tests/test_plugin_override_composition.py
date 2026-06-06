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
    fm_text = yaml.safe_dump(
        structured, sort_keys=False, default_flow_style=False
    ).rstrip()
    # Mirror _render_plugin_skill: manifest global_instructions are prepended
    # to every emitted SKILL.md body, so the rendered upstream a consumer
    # forks from includes the section.
    instructions = json.loads(MANIFEST.read_text()).get("global_instructions") or []
    if instructions:
        body = (
            "## Global Instructions\n\n"
            + "\n".join(f"- {item}" for item in instructions)
            + "\n\n"
            + body
        )
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
    assert not any(entry.get("status") == "stale" for entry in payload["components"]), (
        payload
    )


def test_plugin_override_manifest_rejects_invalid_mcp_server_shape(
    tmp_path: Path,
) -> None:
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


def test_plugin_override_manifest_rejects_escaping_paths(tmp_path: Path) -> None:
    plugin_out = tmp_path / "dist"
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
                            "mode": "replace",
                            "path": "../outside/SKILL.md",
                            "upstream_digest": "sha256:" + "a" * 64,
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
        "f" * 40,
    )

    assert proc.returncode != 0
    assert "override paths must be relative" in proc.stderr


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
        assert not (
            plugin_out / harness / "skills" / "branch-review" / "SKILL.md"
        ).exists()

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


def test_plugin_passthrough_lock_emits_effective_identical_to_base(
    tmp_path: Path,
) -> None:
    base_out = tmp_path / "base"
    effective_out = tmp_path / "effective"
    common = (
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
    )

    base_proc = _run_generator(*common, "--plugin-out", str(base_out))
    assert base_proc.returncode == 0, base_proc.stderr

    effective_proc = _run_generator(
        *common,
        "--plugin-out",
        str(effective_out),
        "--plugin-passthrough-lock",
        "--plugin-base-remote-sha",
        "a" * 40,
    )
    assert effective_proc.returncode == 0, (
        "zero-override passthrough composition must succeed; "
        f"stdout={effective_proc.stdout!r} stderr={effective_proc.stderr!r}"
    )

    base_files = {
        path.relative_to(base_out): path.read_bytes()
        for path in base_out.rglob("*")
        if path.is_file()
    }
    effective_files = {
        path.relative_to(effective_out): path.read_bytes()
        for path in effective_out.rglob("*")
        if path.is_file()
    }
    lock_rel = Path("plugin-lock.json")

    assert set(effective_files) == set(base_files) | {lock_rel}, (
        "effective tree must be byte-equivalent to base plus only plugin-lock.json"
    )
    for rel, content in base_files.items():
        assert effective_files[rel] == content, f"effective drift at {rel}"

    lock = PluginEffectiveLock.model_validate_json(
        effective_files[lock_rel].decode("utf-8")
    )
    assert lock.components, "passthrough lock must enumerate components"
    assert all(entry.mode == "passthrough" for entry in lock.components)
    assert all(entry.status is None for entry in lock.components)
    assert {entry.component_kind for entry in lock.components} == {
        "skill",
        "mcp_server",
    }


def test_plugin_passthrough_lock_requires_base_remote_sha(tmp_path: Path) -> None:
    proc = _run_generator(
        "--mode=plugin",
        "--manifest",
        str(MANIFEST),
        "--skills-source-root",
        str(SKILLS_ROOT),
        "--plugin-mcp-servers",
        str(MCP_SERVERS_YAML),
        "--plugin-out",
        str(tmp_path / "effective"),
        "--plugin-passthrough-lock",
    )
    assert proc.returncode != 0
    assert "--plugin-base-remote-sha" in proc.stderr


def _write_patch_override_root(
    tmp_path: Path,
    forked_base: str,
    consumer_edit: str,
) -> Path:
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    skill_dir = override_root / "skills" / "branch-review"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.base.md").write_text(forked_base)
    (skill_dir / "SKILL.md").write_text(consumer_edit)
    base_digest = hashlib.sha256(forked_base.encode("utf-8")).hexdigest()
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "skills": {
                        "branch-review": {
                            "mode": "patch",
                            "path": "skills/branch-review/SKILL.md",
                            "base_path": "skills/branch-review/SKILL.base.md",
                            "upstream_digest": f"sha256:{base_digest}",
                            "on_upstream_change": "warn",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    return override_root


def _patch_compose_args(plugin_out: Path, override_root: Path) -> tuple[str, ...]:
    return (
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


def test_plugin_patch_mode_clean_merge_composes_merged_body(tmp_path: Path) -> None:
    canonical = _canonical_plugin_skill("branch-review")
    lines = canonical.splitlines(keepends=True)
    # Upstream change since fork: the forked base copy is missing the final
    # line that current upstream carries.
    forked_base = "".join(lines[:-1])
    # Consumer edit: an insertion near the top, far from the upstream change.
    consumer_marker = "<!-- consumer customization -->\n"
    consumer_edit = lines[0] + consumer_marker + "".join(lines[1:-1])
    expected_merged = lines[0] + consumer_marker + "".join(lines[1:])

    plugin_out = tmp_path / "dist"
    override_root = _write_patch_override_root(tmp_path, forked_base, consumer_edit)

    proc = _run_generator(*_patch_compose_args(plugin_out, override_root))
    assert proc.returncode == 0, (
        f"clean 3-way merge must succeed; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )

    effective_skill = plugin_out / "claude" / "skills" / "branch-review" / "SKILL.md"
    assert effective_skill.read_text() == expected_merged, (
        "clean merge must carry both the consumer edit and the upstream change"
    )

    payload = json.loads((plugin_out / "plugin-lock.json").read_text())
    entry = next(
        item for item in payload["components"] if item["name"] == "branch-review"
    )
    assert entry["mode"] == "patch"
    assert entry.get("status") is None

    check_proc = _run_generator(
        *_patch_compose_args(plugin_out, override_root), "--check"
    )
    assert check_proc.returncode == 0, (
        "clean merge with changed upstream is informational; --check must exit 0; "
        f"stderr={check_proc.stderr!r}"
    )


def test_plugin_patch_mode_conflict_falls_back_to_consumer_copy(
    tmp_path: Path,
) -> None:
    canonical = _canonical_plugin_skill("branch-review")
    lines = canonical.splitlines(keepends=True)
    # Fork-time base and the consumer edit both rewrite the SAME final line
    # that current upstream also changed -> guaranteed conflict.
    forked_base = "".join(lines[:-1]) + "OLD ENDING\n"
    consumer_edit = "".join(lines[:-1]) + "CONSUMER ENDING\n"

    plugin_out = tmp_path / "dist"
    override_root = _write_patch_override_root(tmp_path, forked_base, consumer_edit)

    proc = _run_generator(*_patch_compose_args(plugin_out, override_root))
    assert proc.returncode == 0, (
        "emit mode preserves the consumer copy on conflict instead of failing; "
        f"stderr={proc.stderr!r}"
    )

    effective_skill = plugin_out / "claude" / "skills" / "branch-review" / "SKILL.md"
    assert effective_skill.read_text() == consumer_edit, (
        "conflicted patch must fall back to the consumer copy verbatim"
    )

    payload = json.loads((plugin_out / "plugin-lock.json").read_text())
    entry = next(
        item for item in payload["components"] if item["name"] == "branch-review"
    )
    assert entry["mode"] == "patch"
    assert entry["status"] == "merge_conflict"

    check_proc = _run_generator(
        *_patch_compose_args(plugin_out, override_root), "--check"
    )
    assert check_proc.returncode != 0, "--check must fail on merge conflicts"
    assert "merge conflict" in check_proc.stderr.lower()


def test_plugin_patch_mode_upstream_unchanged_yields_consumer_edit(
    tmp_path: Path,
) -> None:
    canonical = _canonical_plugin_skill("branch-review")
    forked_base = canonical
    consumer_edit = canonical + "\nConsumer addendum.\n"

    plugin_out = tmp_path / "dist"
    override_root = _write_patch_override_root(tmp_path, forked_base, consumer_edit)

    proc = _run_generator(*_patch_compose_args(plugin_out, override_root))
    assert proc.returncode == 0, proc.stderr

    effective_skill = plugin_out / "claude" / "skills" / "branch-review" / "SKILL.md"
    assert effective_skill.read_text() == consumer_edit

    payload = json.loads((plugin_out / "plugin-lock.json").read_text())
    entry = next(
        item for item in payload["components"] if item["name"] == "branch-review"
    )
    assert entry["mode"] == "patch"
    assert entry.get("status") is None

    check_proc = _run_generator(
        *_patch_compose_args(plugin_out, override_root), "--check"
    )
    assert check_proc.returncode == 0, check_proc.stderr
