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
KNOWN_HARNESSES = frozenset({"claude-code", "codex", "vscode"})


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
        assert required_artifacts, f"hook {hook['hook_id']} must declare required_artifacts"
        for artifact in required_artifacts:
            assert artifact["kind"] == "file"
            artifact_path = PAYLOAD_ROOT / artifact["consumer_path"]
            assert artifact_path.is_file(), (
                f"hook {hook['hook_id']} references missing artifact {artifact['consumer_path']}"
            )
        for adapter in hook["adapters"]:
            assert adapter["harness"] in KNOWN_HARNESSES


def test_every_hook_carries_an_adapter_for_each_known_harness() -> None:
    """WORKSTATE-REF-56 implementation note: cross-harness adapter parity.

    Every hook in ``hooks[]`` must declare at least one adapter for each
    harness currently emitted by ``generate_agent_workflows.py``
    (``claude-code``, ``codex``, ``vscode``). This stops a future
    Stop-hook addition from shipping with parity gaps and silently
    leaving codex/vscode consumers without the manifest entries the
    walker needs.
    """
    manifest = json.loads(MANIFEST.read_text())
    hooks = manifest["hooks"]
    for hook in hooks:
        harness_set = {adapter["harness"] for adapter in hook["adapters"]}
        missing = KNOWN_HARNESSES - harness_set
        assert not missing, (
            f"hook {hook['hook_id']!r} is missing adapters for harness(es) "
            f"{sorted(missing)}; declared harnesses={sorted(harness_set)}"
        )


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
                                    "command": "{{consumer_root}}/scripts/hooks/compact-session.py",
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
