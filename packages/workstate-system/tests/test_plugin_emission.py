"""WORKSTATE-REF-01 implementation note: plugin-tree emission contract.

The generator's ``--mode=plugin`` projection emits one ``dist/<harness>/``
plugin tree per supported harness from the same canonical inputs that
feed the legacy mode (skills + portable_commands.json) plus the new
``config/agent-workflows/mcp_servers.yaml`` registration manifest.

This module pins the emitted layout, the cross-harness SKILL.md byte-equality
invariant spelled out by ADR-001, and determinism. It is the
RED-before-GREEN gate for the plugin emission path; it does not exercise
the legacy ``--mode=legacy`` path.

Layout pinned by these tests::

    dist/claude/.claude-plugin/plugin.json
    dist/claude/.mcp.json
    dist/claude/skills/<slug>/SKILL.md   (one per manifest skill)

    dist/codex/.codex-plugin/plugin.json
    dist/codex/.mcp.json
    dist/codex/skills/<slug>/SKILL.md    (byte-identical to claude/)

    dist/grok/.grok-plugin/plugin.json   # skills + hooks only; no MCP pins
    dist/grok/hooks/hooks.json
    dist/grok/skills/<slug>/SKILL.md     (byte-identical to claude/)

Both ``plugin.json`` files are metadata-only and point at the sibling
``./.mcp.json`` via the ``mcpServers`` field. Claude's ``.mcp.json`` uses
the camelCase ``mcpServers`` wrapper; Codex's uses the bare server-map
shape accepted by the live Codex plugin loader. Both launch the canonical
``uvx`` pins with ``"type": "stdio"``; both ``SKILL.md`` bodies are
byte-identical between harnesses so consumer discovery does not depend
on which harness loaded the plugin.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import yaml
except ImportError as exc:  # pragma: no cover - PyYAML is required at runtime
    raise SystemExit("PyYAML is required for plugin emission tests") from exc


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
GENERATOR = PAYLOAD_ROOT / "scripts" / "generate_agent_workflows.py"
GUARD_WRAP = PAYLOAD_ROOT / "scripts" / "_guard_wrap.py"


def _wrap_guard_command(command: str, *, fail_mode: object = None) -> str:
    """implementation note implementation note: emitted hook commands carry the fail-open wrapper
    prefix; expectations are derived through the renderer's own transform."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("_emission_guard_wrap", GUARD_WRAP)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.wrap_guard_command(command, fail_mode=fail_mode)
MANIFEST = PAYLOAD_ROOT / "config" / "agent-workflows" / "portable_commands.json"
MCP_SERVERS_YAML = PAYLOAD_ROOT / "config" / "agent-workflows" / "mcp_servers.yaml"
HARNESS_PROTOCOL = PAYLOAD_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
SKILLS_ROOT = PAYLOAD_ROOT / "skills"


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _emit_plugin_tree(plugin_out: Path) -> subprocess.CompletedProcess[str]:
    return _run_generator(
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


def _emit_plugin_tree_with_overrides(
    plugin_out: Path, override_root: Path, *, base_remote_sha: str = "a" * 40
) -> subprocess.CompletedProcess[str]:
    return _run_generator(
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
        base_remote_sha,
    )


def _expected_skill_slugs() -> set[str]:
    manifest = json.loads(MANIFEST.read_text())
    return {command["skill"] for command in manifest["commands"]}


def _all_authored_skill_slugs() -> set[str]:
    return {p.name for p in SKILLS_ROOT.iterdir() if p.is_dir()}


def _expected_plugin_version() -> str:
    payload = yaml.safe_load(MCP_SERVERS_YAML.read_text())
    return str(payload["plugin_version"])


def _mcp_servers_for_harness(plugin_out: Path, harness: str) -> dict:
    payload = json.loads((plugin_out / harness / ".mcp.json").read_text())
    if harness in {"claude", "grok"}:
        assert set(payload) == {"mcpServers"}, (
            f"{harness}: .mcp.json must use only the 'mcpServers' wrapper; got {payload!r}"
        )
        servers = payload["mcpServers"]
        assert isinstance(servers, dict), f"{harness}: mcpServers must be a mapping"
    elif harness == "codex":
        assert "mcpServers" not in payload and "mcp_servers" not in payload, (
            f"{harness}: .mcp.json must be a bare server map; got {payload!r}"
        )
        servers = payload
    else:
        raise AssertionError(f"unsupported harness: {harness!r}")
    return servers


@pytest.fixture
def emitted_tree(tmp_path: Path) -> Path:
    plugin_out = tmp_path / "dist"
    proc = _emit_plugin_tree(plugin_out)
    assert proc.returncode == 0, (
        "`--mode=plugin` emission must exit 0; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    return plugin_out


def test_plugin_tree_has_per_harness_manifest_directories(emitted_tree: Path) -> None:
    """Each harness gets its own plugin envelope directory with the
    harness-specific manifest name (.claude-plugin / .codex-plugin / .grok-plugin)."""
    assert (emitted_tree / "claude" / ".claude-plugin" / "plugin.json").is_file()
    assert (emitted_tree / "codex" / ".codex-plugin" / "plugin.json").is_file()
    assert (emitted_tree / "grok" / ".grok-plugin" / "plugin.json").is_file()


def test_plugin_json_is_metadata_only_with_path_references(
    emitted_tree: Path,
) -> None:
    """plugin.json is metadata-only: skills and mcpServers are sibling
    path references (live-doc-verified schema), not inline arrays."""
    expected_version = _expected_plugin_version()
    for harness, plugin_dir in (
        ("claude", ".claude-plugin"),
        ("codex", ".codex-plugin"),
    ):
        manifest_path = emitted_tree / harness / plugin_dir / "plugin.json"
        payload = json.loads(manifest_path.read_text())
        assert payload["name"] == "workstate-system", (
            f"{manifest_path}: expected name=workstate-system; got {payload!r}"
        )
        assert payload["version"] == expected_version, (
            f"{manifest_path}: version must track mcp_servers.yaml plugin_version "
            f"({expected_version!r}); got {payload.get('version')!r}"
        )
        assert payload["skills"] == "./skills/", (
            f"{manifest_path}: skills must be the sibling skills/ path reference"
        )
        assert payload["mcpServers"] == "./.mcp.json", (
            f"{manifest_path}: mcpServers must be the sibling .mcp.json path reference"
        )
        # Schema is metadata-only: there must be no inline slashCommands or
        # inline skills array (live docs say plugins auto-discover from
        # skills/ + commands/ directories).
        assert "slashCommands" not in payload, (
            f"{manifest_path}: plugin.json must not carry a slashCommands key"
        )
        assert not isinstance(payload["skills"], list), (
            f"{manifest_path}: skills must be a path string, not an inline array"
        )


def test_grok_plugin_json_omits_mcp_pins(emitted_tree: Path) -> None:
    """Grok compat loads repo-root .mcp.json; plugin manifest must not re-register MCP."""
    manifest_path = emitted_tree / "grok" / ".grok-plugin" / "plugin.json"
    payload = json.loads(manifest_path.read_text())
    assert payload["skills"] == "./skills/"
    assert "mcpServers" not in payload
    assert not (emitted_tree / "grok" / ".mcp.json").exists()


def test_mcp_json_files_use_per_harness_shapes(
    emitted_tree: Path,
) -> None:
    """Claude consumes a wrapped .mcp.json object; Codex consumes the
    same canonical server map without a top-level wrapper."""
    claude_servers = _mcp_servers_for_harness(emitted_tree, "claude")
    codex_servers = _mcp_servers_for_harness(emitted_tree, "codex")
    assert claude_servers == codex_servers


def test_mcp_json_pins_uvx_servers_with_stdio_type(emitted_tree: Path) -> None:
    """The emitted .mcp.json launches both MCP servers via uvx with the
    pinned package@version, --workspace-root ., serve-stdio invocation
    and ``"type": "stdio"`` (bootstrap-compatible shape)."""
    for harness in ("claude", "codex"):
        servers = _mcp_servers_for_harness(emitted_tree, harness)
        assert set(servers) == {"workstate-handoff-mcp", "workstate-orchestrator-mcp"}
        for name, expected_pkg in (
            ("workstate-handoff-mcp", "mcp-workstate-handoff@0.12.4"),
            ("workstate-orchestrator-mcp", "mcp-workstate-orchestrator[bridge]@0.6.1"),
        ):
            entry = servers[name]
            assert entry["type"] == "stdio", (
                f"{harness}/{name}: missing type=stdio for bootstrap shape compatibility"
            )
            assert entry["command"] == "uvx"
            assert entry["args"] == [
                expected_pkg,
                "--workspace-root",
                ".",
                "serve-stdio",
            ], f"{harness}/{name}: args drift from canonical uvx pin"


def test_skill_bodies_byte_identical_across_harnesses(emitted_tree: Path) -> None:
    """ADR-001 invariant: emitted SKILL.md bodies are byte-identical
    across claude/, codex/, and grok/ for every manifest-referenced skill."""
    expected_slugs = _expected_skill_slugs()
    assert expected_slugs, "manifest must declare at least one skill"
    for slug in expected_slugs:
        claude_skill = emitted_tree / "claude" / "skills" / slug / "SKILL.md"
        codex_skill = emitted_tree / "codex" / "skills" / slug / "SKILL.md"
        grok_skill = emitted_tree / "grok" / "skills" / slug / "SKILL.md"
        assert claude_skill.is_file(), f"missing emitted claude skill: {claude_skill}"
        assert codex_skill.is_file(), f"missing emitted codex skill: {codex_skill}"
        assert grok_skill.is_file(), f"missing emitted grok skill: {grok_skill}"
        claude_bytes = claude_skill.read_bytes()
        assert codex_skill.read_bytes() == claude_bytes, (
            f"skill body drift between harnesses for {slug!r}"
        )
        assert grok_skill.read_bytes() == claude_bytes, (
            f"skill body drift between harnesses for {slug!r}"
        )


def test_only_manifest_referenced_skills_are_emitted(emitted_tree: Path) -> None:
    """Plugin emission tracks the portable-commands manifest, not the
    authored skills/ root. Unreferenced skills (e.g. internal helpers
    not exposed as portable commands) must not leak into the dist tree."""
    expected_slugs = _expected_skill_slugs()
    authored = _all_authored_skill_slugs()
    # Only meaningful if the authored set is a strict superset.
    if not authored - expected_slugs:
        pytest.skip("no authored-only skills to filter; invariant is trivially true")
    for harness in ("claude", "codex", "grok"):
        skills_root = emitted_tree / harness / "skills"
        emitted = {p.name for p in skills_root.iterdir() if p.is_dir()}
        assert emitted == expected_slugs, (
            f"{harness}: emitted slugs {emitted!r} != manifest slugs {expected_slugs!r}"
        )


def _frontmatter(path: Path) -> dict:
    text = path.read_text()
    assert text.startswith("---\n"), f"{path}: missing frontmatter delimiter"
    _, fm_raw, _ = text.split("---\n", 2)
    return yaml.safe_load(fm_raw) or {}


def test_branch_review_persistence_guidance_reaches_emitted_skill(
    emitted_tree: Path,
) -> None:
    """Load-bearing branch-review body content must reach the emitted
    plugin skill. Byte-identity across harnesses is pinned separately;
    asserting against the Claude tree is sufficient."""
    body = (
        emitted_tree / "claude" / "skills" / "branch-review" / "SKILL.md"
    ).read_text()
    assert 'subject_kind="branch"' in body
    assert "Do not write directly to `.task-state/handoff.db`" in body
    assert "MCP writes:" in body


def test_manifest_global_instructions_prefix_every_emitted_skill(
    emitted_tree: Path,
) -> None:
    """Every manifest global_instructions item must lead every emitted
    SKILL.md body (both harnesses) as a '## Global Instructions' section
    placed directly after the frontmatter. Reads the live manifest so the
    test pins the mechanism, not the instruction wording."""
    instructions = json.loads(MANIFEST.read_text()).get("global_instructions") or []
    if not instructions:
        pytest.skip("manifest declares no global_instructions")
    expected_section = (
        "## Global Instructions\n\n"
        + "\n".join(f"- {item}" for item in instructions)
        + "\n\n"
    )
    for harness in ("claude", "codex", "grok"):
        for path in sorted((emitted_tree / harness / "skills").glob("*/SKILL.md")):
            text = path.read_text()
            _, _, body = text.split("---\n", 2)
            assert body.lstrip("\n").startswith(expected_section), (
                f"{path}: emitted skill body must open with the manifest "
                "global-instructions section"
            )


def test_emitted_skills_validate_against_skill_manifest(emitted_tree: Path) -> None:
    """Every emitted SKILL.md must validate against
    ``workstate_protocol.SkillManifest`` so plugin consumers do not pick up
    structurally-broken skills."""
    pytest.importorskip("workstate_protocol")
    from workstate_protocol import SkillManifest

    failures: list[str] = []
    for harness in ("claude", "codex", "grok"):
        for path in (emitted_tree / harness / "skills").glob("*/SKILL.md"):
            try:
                SkillManifest.model_validate(_frontmatter(path))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{path}: {exc}")
    assert not failures, "\n".join(failures)


def test_emission_is_deterministic(tmp_path: Path) -> None:
    """Re-running --mode=plugin against the same inputs must produce a
    byte-identical tree."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    assert _emit_plugin_tree(first).returncode == 0
    assert _emit_plugin_tree(second).returncode == 0

    first_files = sorted(p.relative_to(first) for p in first.rglob("*") if p.is_file())
    second_files = sorted(
        p.relative_to(second) for p in second.rglob("*") if p.is_file()
    )
    assert first_files == second_files, "plugin emission produced different file sets"
    for relative in first_files:
        assert (first / relative).read_bytes() == (second / relative).read_bytes(), (
            f"plugin emission is non-deterministic at {relative}"
        )


def test_plugin_overrides_patch_mcp_servers_and_emit_audit_receipt(
    tmp_path: Path,
) -> None:
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    (override_root / "tools").mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": {
                        "workstate-handoff-mcp": {
                            "mode": "patch",
                            "patch_path": "tools/mcp_servers.patch.yaml",
                            "requires_trust_ack": True,
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    (override_root / "tools" / "mcp_servers.patch.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "target_server": "workstate-handoff-mcp",
                "ops": [
                    {
                        "op": "replace_args",
                        "value": [
                            "mcp-workstate-handoff@0.12.4",
                            "--profile",
                            "consumer",
                        ],
                    },
                    {
                        "op": "upsert_env",
                        "name": "HANDOFF_PROFILE",
                        "value": "consumer",
                    },
                ],
            },
            sort_keys=False,
        )
    )

    plugin_out = tmp_path / "dist"
    proc = _emit_plugin_tree_with_overrides(plugin_out, override_root)

    assert proc.returncode == 0, proc.stderr
    for harness in ("claude", "codex"):
        handoff = _mcp_servers_for_harness(plugin_out, harness)["workstate-handoff-mcp"]
        assert handoff["args"] == [
            "mcp-workstate-handoff@0.12.4",
            "--profile",
            "consumer",
        ]
        assert handoff["env"] == {"HANDOFF_PROFILE": "consumer"}

    effective_lock = json.loads((plugin_out / "plugin-lock.json").read_text())
    assert {
        "component_kind": "mcp_server",
        "name": "workstate-handoff-mcp",
        "mode": "patch",
    }.items() <= effective_lock["components"][0].items()
    assert "mcp audit: workstate-handoff-mcp args, env" in proc.stdout


def test_plugin_overrides_reject_mcp_patches_without_trust_ack(tmp_path: Path) -> None:
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    (override_root / "tools").mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": {
                        "workstate-handoff-mcp": {
                            "mode": "patch",
                            "patch_path": "tools/mcp_servers.patch.yaml",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    (override_root / "tools" / "mcp_servers.patch.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "target_server": "workstate-handoff-mcp",
                "ops": [
                    {
                        "op": "replace_args",
                        "value": [
                            "mcp-workstate-handoff@0.12.4",
                            "--profile",
                            "consumer",
                        ],
                    }
                ],
            },
            sort_keys=False,
        )
    )

    plugin_out = tmp_path / "dist"
    proc = _emit_plugin_tree_with_overrides(plugin_out, override_root)

    assert proc.returncode == 1
    assert "requires_trust_ack" in proc.stderr
    assert not (plugin_out / "claude" / ".mcp.json").exists()


def test_plugin_overrides_can_disable_mcp_server_without_patch_file(
    tmp_path: Path,
) -> None:
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    override_root.mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": {
                        "workstate-orchestrator-mcp": {
                            "mode": "disable",
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )

    plugin_out = tmp_path / "dist"
    proc = _emit_plugin_tree_with_overrides(plugin_out, override_root)

    assert proc.returncode == 0, proc.stderr
    for harness in ("claude", "codex"):
        assert "workstate-orchestrator-mcp" not in _mcp_servers_for_harness(plugin_out, harness)

    effective_lock = json.loads((plugin_out / "plugin-lock.json").read_text())
    assert {
        "component_kind": "mcp_server",
        "name": "workstate-orchestrator-mcp",
        "mode": "disable",
    }.items() <= effective_lock["components"][0].items()


def test_plugin_overrides_can_add_mcp_server_with_trust_ack(tmp_path: Path) -> None:
    override_root = tmp_path / "workstate-overrides" / "workstate-system"
    (override_root / "tools").mkdir(parents=True)
    (override_root / "overrides.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "plugin": "workstate-system",
                "components": {
                    "mcp_servers": {
                        "consumer-helper-mcp": {
                            "mode": "add",
                            "patch_path": "tools/mcp_servers.patch.yaml",
                            "requires_trust_ack": True,
                        }
                    }
                },
            },
            sort_keys=False,
        )
    )
    (override_root / "tools" / "mcp_servers.patch.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "target_server": "consumer-helper-mcp",
                "ops": [
                    {"op": "replace_command", "value": "uvx"},
                    {
                        "op": "replace_args",
                        "value": ["mcp-consumer-helper@1.2.3", "serve-stdio"],
                    },
                    {"op": "upsert_env", "name": "CONSUMER_MODE", "value": "project"},
                ],
            },
            sort_keys=False,
        )
    )

    plugin_out = tmp_path / "dist"
    proc = _emit_plugin_tree_with_overrides(plugin_out, override_root)

    assert proc.returncode == 0, proc.stderr
    for harness in ("claude", "codex"):
        helper = _mcp_servers_for_harness(plugin_out, harness)["consumer-helper-mcp"]
        assert helper["command"] == "uvx"
        assert helper["args"] == ["mcp-consumer-helper@1.2.3", "serve-stdio"]
        assert helper["env"] == {"CONSUMER_MODE": "project"}

    effective_lock = json.loads((plugin_out / "plugin-lock.json").read_text())
    assert {
        "component_kind": "mcp_server",
        "name": "consumer-helper-mcp",
        "mode": "add",
    }.items() <= effective_lock["components"][0].items()


def _load_harness_protocol() -> dict:
    return yaml.safe_load(HARNESS_PROTOCOL.read_text()) or {}


def _grok_plugin_hooks(emitted_tree: Path) -> dict:
    hooks_path = emitted_tree / "grok" / "hooks" / "hooks.json"
    assert hooks_path.is_file(), f"missing grok plugin hooks: {hooks_path}"
    return json.loads(hooks_path.read_text())


def test_grok_plugin_emits_guard_hooks_from_harness_protocol(emitted_tree: Path) -> None:
    """PreToolUse guards project deterministically into base/grok/hooks/hooks.json."""
    payload = _grok_plugin_hooks(emitted_tree)
    pre_tool_use = payload["hooks"]["PreToolUse"]
    assert len(pre_tool_use) == 6
    matchers = {entry["matcher"] for entry in pre_tool_use}
    assert "Bash" in matchers
    commands = {
        hook["command"]
        for entry in pre_tool_use
        for hook in entry["hooks"]
    }
    assert any("guard-bash-main-branch.py" in command for command in commands)
    assert all("${GROK_WORKSPACE_ROOT}" in command for command in commands)


@pytest.mark.parametrize(
    "guard_id,matcher,grok_command",
    [
        (
            row["id"],
            row.get("matcher", ""),
            row["grok_command"],
        )
        for row in (_load_harness_protocol().get("hooks") or {}).get("pre_tool_use", [])
        if isinstance(row, dict) and row.get("id")
    ],
)
def test_grok_plugin_hook_row_matches_harness_protocol(
    emitted_tree: Path,
    guard_id: str,
    matcher: str,
    grok_command: str,
) -> None:
    """Each harness-protocol pre_tool_use row projects exactly once into hooks.json."""
    payload = _grok_plugin_hooks(emitted_tree)
    pre_tool_use = payload["hooks"]["PreToolUse"]
    expected_command = _wrap_guard_command(grok_command)
    matches = [
        entry
        for entry in pre_tool_use
        if entry.get("matcher") == matcher
        and any(
            hook.get("command") == expected_command
            for hook in entry.get("hooks", [])
        )
    ]
    assert len(matches) == 1, (
        f"guard {guard_id!r}: expected one emitted hook for matcher={matcher!r} "
        f"and wrapped grok_command={expected_command!r}; got {len(matches)}"
    )


def test_harness_protocol_pre_tool_use_rows_require_grok_command() -> None:
    """Every pre_tool_use guard must declare a grok_command for Grok projection."""
    hook_rows = (_load_harness_protocol().get("hooks") or {}).get("pre_tool_use") or []
    for row in hook_rows:
        if not isinstance(row, dict):
            continue
        grok_command = row.get("grok_command")
        assert isinstance(grok_command, str) and grok_command.strip(), (
            f"pre_tool_use guard {row.get('id')!r} must declare grok_command"
        )


def test_grok_plugin_hooks_are_sole_pretooluse_source(emitted_tree: Path) -> None:
    """Plugin hooks deliver PreToolUse only; compat-loaded SessionStart stays separate."""
    payload = _grok_plugin_hooks(emitted_tree)
    hooks = payload.get("hooks") or {}
    assert set(hooks) == {"PreToolUse"}, (
        "grok plugin hooks.json must only declare PreToolUse guards; "
        f"found stages: {sorted(hooks)}"
    )
    compat_session_start = "bash .claude/hooks/ensure-agent-surfaces.sh"
    for entry in hooks["PreToolUse"]:
        for hook in entry.get("hooks") or []:
            command = hook.get("command", "")
            assert compat_session_start not in command, (
                "PreToolUse guards must not overlap compat SessionStart hooks"
            )


def test_review_parallel_emitted_skill_includes_grok_routing_row(
    emitted_tree: Path,
) -> None:
    body = (emitted_tree / "grok" / "skills" / "review-parallel" / "SKILL.md").read_text()
    assert "Grok (coordinator is a Grok agent)" in body
    assert "in-process `task` tool" in body
    assert "_routing table rendered" not in body
