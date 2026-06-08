from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
GENERATOR = PAYLOAD_ROOT / "scripts" / "generate_agent_workflows.py"
MANIFEST = PAYLOAD_ROOT / "config" / "agent-workflows" / "portable_commands.json"
CODEX_ROUTER_BEGIN = "<!-- BEGIN GENERATED: codex-command-router -->"
CODEX_ROUTER_END = "<!-- END GENERATED: codex-command-router -->"
KNOWN_HARNESSES = frozenset({"claude-code", "codex", "grok", "vscode"})


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_temp_manifest(tmp_path: Path, payload: dict[str, object]) -> Path:
    temp_repo = tmp_path / "repo"
    manifest = temp_repo / "config" / "agent-workflows" / "portable_commands.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload))

    for command in payload.get("commands", []):
        slug = command.get("skill")
        if not slug:
            continue
        skill_dir = temp_repo / "skills" / slug
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "skill.yaml").write_text(
            "name: {0}\n"
            "description: Test stub for {0}.\n"
            "scope: harness\n"
            "mode: advisory\n"
            "context_budget: 8000\n"
            "makefile_target: {1}\n"
            "mcp_tools: []\n"
            "tdd_gate: none\n".format(slug, command.get("makefile_target", "make noop"))
        )
        (skill_dir / "body.md").write_text("## Overview\n\nTest stub.\n")

    instructions = temp_repo / "docs" / "workstate" / "instructions.md"
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text(
        f"before\n{CODEX_ROUTER_BEGIN}\nplaceholder\n{CODEX_ROUTER_END}\nafter\n"
    )
    (temp_repo / "CLAUDE.md").write_text(
        f"before\n{CODEX_ROUTER_BEGIN}\nplaceholder\n{CODEX_ROUTER_END}\nafter\n"
    )
    return manifest


def test_live_manifest_declares_version_2_hook_paths_that_exist() -> None:
    manifest = json.loads(MANIFEST.read_text())

    assert manifest["version"] == 2
    hooks = manifest.get("hooks")
    assert isinstance(hooks, list) and hooks, "live manifest must declare hooks[]"

    for hook in hooks:
        required_artifacts = hook["required_artifacts"]
        assert required_artifacts, (
            f"hook {hook['hook_id']} must declare required_artifacts"
        )
        for artifact in required_artifacts:
            assert artifact["kind"] == "file"
            artifact_path = PAYLOAD_ROOT / artifact["consumer_path"]
            assert artifact_path.is_file(), (
                f"hook {hook['hook_id']} references missing artifact {artifact['consumer_path']}"
            )
        for adapter in hook["adapters"]:
            assert adapter["harness"] in KNOWN_HARNESSES


def test_every_hook_carries_an_adapter_for_each_known_harness() -> None:
    """WORKSTATE-REF-56 implementation note, amended by WS-REINJ-01 implementation note: cross-harness
    adapter parity *within the hook's supported-harness allowlist*.

    A hook without ``supported_harnesses`` must declare at least one
    adapter for every harness emitted by ``generate_agent_workflows.py``
    (``claude-code``, ``codex``, ``grok``, ``vscode``) — the original
    WORKSTATE-REF-56 guarantee, preserved for compact-session. A hook MAY narrow
    the set with ``supported_harnesses``, but then (a) the allowlist must
    be a non-empty subset of the known harnesses, (b) a non-empty
    ``rationale`` string must document why the deferral is deliberate,
    (c) parity is enforced within the allowlist, and (d) no adapter may
    target a harness outside it. This keeps "shipped with parity gaps
    silently" impossible while letting a family defer harnesses that have
    no equivalent event.
    """
    manifest = json.loads(MANIFEST.read_text())
    hooks = manifest["hooks"]
    for hook in hooks:
        supported = hook.get("supported_harnesses")
        if supported is None:
            allowlist = KNOWN_HARNESSES
        else:
            assert isinstance(supported, list) and supported, (
                f"hook {hook['hook_id']!r} supported_harnesses must be a "
                f"non-empty list when declared"
            )
            allowlist = frozenset(supported)
            unknown = allowlist - KNOWN_HARNESSES
            assert not unknown, (
                f"hook {hook['hook_id']!r} supported_harnesses contains "
                f"unknown harness(es) {sorted(unknown)}"
            )
            if allowlist != KNOWN_HARNESSES:
                rationale = hook.get("rationale")
                assert isinstance(rationale, str) and rationale.strip(), (
                    f"hook {hook['hook_id']!r} narrows supported_harnesses "
                    f"to {sorted(allowlist)} but declares no rationale; "
                    f"deferrals must stay documented in the manifest itself"
                )
        harness_set = {adapter["harness"] for adapter in hook["adapters"]}
        missing = allowlist - harness_set
        assert not missing, (
            f"hook {hook['hook_id']!r} is missing adapters for harness(es) "
            f"{sorted(missing)}; declared harnesses={sorted(harness_set)}"
        )
        outside = harness_set - allowlist
        assert not outside, (
            f"hook {hook['hook_id']!r} declares adapters for harness(es) "
            f"{sorted(outside)} outside its supported_harnesses allowlist "
            f"{sorted(allowlist)}"
        )


def test_live_manifest_declares_capture_agent_errors_post_tool_use_hook() -> None:
    """implementation note implementation note: capture-agent-errors PostToolUse family — claude-code
    only (allowlisted + rationale), two opt-in adapters writing the managed
    ``$.hooks.PostToolUse`` Bash entry."""
    manifest = json.loads(MANIFEST.read_text())
    hooks = {hook["hook_id"]: hook for hook in manifest["hooks"]}
    assert "capture-agent-errors" in hooks, (
        "live manifest must declare the capture-agent-errors hook (implementation note implementation note)"
    )
    hook = hooks["capture-agent-errors"]
    assert hook["trigger"] == "post-tool-use"
    assert hook["supported_harnesses"] == ["claude-code"]
    assert str(hook.get("rationale", "")).strip()
    artifact_paths = [a["consumer_path"] for a in hook["required_artifacts"]]
    assert artifact_paths == ["scripts/hooks/capture-agent-errors.py"]

    adapters_by_flag = {a["opt_in_flag"]: a for a in hook["adapters"]}
    assert set(adapters_by_flag) == {
        "--install-claude-error-hook",
        "--install-claude-error-hook-local",
    }
    shared = adapters_by_flag["--install-claude-error-hook"]
    local = adapters_by_flag["--install-claude-error-hook-local"]
    assert shared["target"] == ".claude/settings.json"
    assert local["target"] == ".claude/settings.local.json"
    for adapter in (shared, local):
        assert adapter["harness"] == "claude-code"
        patch = adapter["patch"]
        assert patch["operation"] == "merge_array_entry"
        assert patch["json_path"] == "$.hooks.PostToolUse"
        assert patch["match_key"] == "_managed_by"
        entry = patch["entry"]
        assert entry["matcher"] == "Bash"
        assert "capture-agent-errors.py" in entry["hooks"][0]["command"]
        assert entry["hooks"][0].get("timeout") == 15


def test_live_manifest_declares_reinject_context_session_start_hook() -> None:
    """WS-REINJ-01 implementation note: the live manifest ships the reinject-context
    SessionStart family — claude-code only (allowlisted + rationale), two
    opt-in adapters writing the managed ``$.hooks.SessionStart`` entry.
    """
    manifest = json.loads(MANIFEST.read_text())
    hooks = {hook["hook_id"]: hook for hook in manifest["hooks"]}
    assert "reinject-context" in hooks, (
        "live manifest must declare the reinject-context hook (implementation note implementation note)"
    )
    hook = hooks["reinject-context"]
    assert hook["trigger"] == "session-start"
    assert hook["supported_harnesses"] == ["claude-code"]
    assert str(hook.get("rationale", "")).strip(), (
        "narrowed supported_harnesses requires a rationale"
    )
    artifact_paths = [a["consumer_path"] for a in hook["required_artifacts"]]
    assert artifact_paths == ["scripts/hooks/reinject-context.py"]

    adapters_by_flag = {a["opt_in_flag"]: a for a in hook["adapters"]}
    assert set(adapters_by_flag) == {
        "--install-claude-reinject-hook",
        "--install-claude-reinject-hook-local",
    }
    shared = adapters_by_flag["--install-claude-reinject-hook"]
    local = adapters_by_flag["--install-claude-reinject-hook-local"]
    assert shared["target"] == ".claude/settings.json"
    assert shared["write_kind"] == "shared_checked_in"
    assert local["target"] == ".claude/settings.local.json"
    assert local["write_kind"] == "user_owned_local"
    for adapter in (shared, local):
        assert adapter["harness"] == "claude-code"
        patch = adapter["patch"]
        assert patch["operation"] == "merge_array_entry"
        assert patch["json_path"] == "$.hooks.SessionStart"
        assert patch["match_key"] == "_managed_by"


def test_every_adapter_declares_non_null_opt_in_flag() -> None:
    """WORKSTATE-REF-56 implementation note: every adapter must carry an explicit
    ``opt_in_flag`` so the walker's default install never touches any
    harness settings file. ``opt_in_flag == null`` would re-introduce
    the pre-Slice-2 hidden default-write behavior for that adapter."""
    manifest = json.loads(MANIFEST.read_text())
    for hook in manifest["hooks"]:
        for adapter in hook["adapters"]:
            flag = adapter.get("opt_in_flag")
            assert flag, (
                f"hook {hook['hook_id']!r} adapter for {adapter['harness']!r} "
                f"-> {adapter['target']!r} must declare a non-null opt_in_flag"
            )


def test_claude_hook_adapters_use_matcher_hooks_array_shape() -> None:
    """Claude Code settings require each event entry to carry nested hooks[]."""
    manifest = json.loads(MANIFEST.read_text())
    for hook in manifest["hooks"]:
        for adapter in hook["adapters"]:
            if adapter["harness"] != "claude-code":
                continue
            entry = adapter["patch"]["entry"]
            assert entry.get("_managed_by") == "workstate-bootstrap"
            assert isinstance(entry.get("matcher"), str)
            assert isinstance(entry.get("hooks"), list) and entry["hooks"], (
                f"{hook['hook_id']} Claude adapter must declare hooks[]"
            )
            assert "command" not in entry, (
                f"{hook['hook_id']} Claude adapter must not use flat command shape"
            )
            for nested in entry["hooks"]:
                assert nested["type"] == "command"
                assert nested["command"]


def test_generate_agent_workflows_accepts_version_2_manifest_with_hooks(
    tmp_path: Path,
) -> None:
    manifest = _write_temp_manifest(
        tmp_path,
        {
            "version": 2,
            "commands": [
                {
                    "command_id": "branch-review",
                    "skill": "branch-review",
                    "mode": "verify",
                    "makefile_target": "make review-run",
                    "description": "Review a branch diff.",
                    "execution_context": "Use for branch review.",
                    "argument_schema": [
                        {
                            "name": "scope",
                            "required": False,
                            "description": "Optional diff scope.",
                        }
                    ],
                    "loop": ["load diff", "record findings"],
                }
            ],
            "hooks": [
                {
                    "hook_id": "compact-session",
                    "description": "Managed compact-session adapter surface.",
                    "trigger": "stop",
                    "required_artifacts": [
                        {
                            "kind": "file",
                            "consumer_path": "scripts/hooks/compact-session.py",
                        }
                    ],
                    "profiles": ["all"],
                    "adapters": [
                        {
                            "harness": "claude-code",
                            "target": ".claude/settings.json",
                            "write_kind": "shared_checked_in",
                            "opt_in_flag": "--install-claude-stop-hook",
                            "patch": {
                                "operation": "merge_array_entry",
                                "json_path": "$.hooks.Stop",
                                "match_key": "_managed_by",
                                "entry": {
                                    "_managed_by": "workstate-bootstrap",
                                    "matcher": "",
                                    "hooks": [
                                        {
                                            "type": "command",
                                            "command": "{{consumer_root}}/scripts/hooks/compact-session.py",
                                        }
                                    ],
                                },
                            },
                        }
                    ],
                }
            ],
        },
    )

    proc = _run_generator("--manifest", str(manifest))

    assert proc.returncode == 0, proc.stderr
