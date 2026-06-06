"""WORKSTATE-REF-1 implementation note: portable-commands manifest `mode` field schema + backfill."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
GENERATOR = PAYLOAD_ROOT / "scripts" / "generate_agent_workflows.py"
MANIFEST = PAYLOAD_ROOT / "config" / "agent-workflows" / "portable_commands.json"

VALID_MODES = frozenset({"guide", "verify", "write"})


def test_live_manifest_every_command_declares_a_valid_mode() -> None:
    """Every command in the live manifest must carry an explicit `mode`
    in {guide, verify, write}. No implicit default — this is the
    declare-or-fail contract introduced by the agent-workflow spec TR1."""
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    commands = manifest["commands"]
    assert commands, "live manifest must declare commands[]"
    missing: list[str] = []
    invalid: list[tuple[str, object]] = []
    for command in commands:
        mode = command.get("mode")
        if mode is None:
            missing.append(command["command_id"])
        elif mode not in VALID_MODES:
            invalid.append((command["command_id"], mode))
    assert not missing, (
        f"commands missing required `mode` field: {missing}. "
        f"Each command must declare mode in {sorted(VALID_MODES)}."
    )
    assert not invalid, f"commands with mode outside {sorted(VALID_MODES)}: {invalid}"


def test_generator_rejects_command_without_mode(tmp_path: Path) -> None:
    """The generator's manifest validator must reject any command without
    a declared `mode` (declare-or-fail; no implicit default)."""
    temp_repo = tmp_path / "repo"
    manifest_path = temp_repo / "config" / "agent-workflows" / "portable_commands.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    skill_dir = temp_repo / "skills" / "branch-review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "name: branch-review\n"
        "description: stub.\n"
        "scope: harness\n"
        "mode: advisory\n"
        "context_budget: 8000\n"
        "makefile_target: make review-run\n"
        "mcp_tools: []\n"
        "tdd_gate: none\n"
    )
    (skill_dir / "body.md").write_text("## Overview\n\nStub.\n")
    manifest_path.write_text(
        json.dumps(
            {
                "version": 2,
                "commands": [
                    {
                        "command_id": "branch-review",
                        "skill": "branch-review",
                        "makefile_target": "make review-run",
                        "description": "Review a branch diff.",
                        "execution_context": "Use for branch review.",
                        "argument_schema": [],
                        "loop": ["load diff", "record findings"],
                    }
                ],
            }
        )
    )

    proc = subprocess.run(
        [sys.executable, str(GENERATOR), "--manifest", str(manifest_path)],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0, (
        "generator must reject a manifest command that omits the `mode` field; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "mode" in proc.stderr, (
        f"generator error must name the missing `mode` field; stderr={proc.stderr!r}"
    )


def test_generator_rejects_command_with_invalid_mode(tmp_path: Path) -> None:
    """The generator's manifest validator must reject any mode value
    outside the {guide, verify, write} enum."""
    temp_repo = tmp_path / "repo"
    manifest_path = temp_repo / "config" / "agent-workflows" / "portable_commands.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    skill_dir = temp_repo / "skills" / "branch-review"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.yaml").write_text(
        "name: branch-review\n"
        "description: stub.\n"
        "scope: harness\n"
        "mode: advisory\n"
        "context_budget: 8000\n"
        "makefile_target: make review-run\n"
        "mcp_tools: []\n"
        "tdd_gate: none\n"
    )
    (skill_dir / "body.md").write_text("## Overview\n\nStub.\n")
    manifest_path.write_text(
        json.dumps(
            {
                "version": 2,
                "commands": [
                    {
                        "command_id": "branch-review",
                        "skill": "branch-review",
                        "mode": "advisory",  # not in {guide, verify, write}
                        "makefile_target": "make review-run",
                        "description": "Review a branch diff.",
                        "execution_context": "Use for branch review.",
                        "argument_schema": [],
                        "loop": ["load diff", "record findings"],
                    }
                ],
            }
        )
    )

    proc = subprocess.run(
        [sys.executable, str(GENERATOR), "--manifest", str(manifest_path)],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode != 0, (
        "generator must reject a manifest mode outside {guide, verify, write}; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "mode" in proc.stderr, (
        f"generator error must mention the offending `mode` value; stderr={proc.stderr!r}"
    )
