"""WORKSTATE-REF-1 implementation note: ``make check-agent-workflows`` mode-gate contract.

The gate behind ``make check-agent-workflows`` is
``scripts/generate_agent_workflows.py --check``. implementation note enforces the
declare-or-fail mode rule at manifest validation time; implementation note projects
the mode into every adapter. implementation note pins both failure paths from the
gate's perspective so future refactors of the gate runner cannot
silently weaken mode enforcement:

  1. ``--check`` exits non-zero when any manifest command omits the
     required ``mode`` field (validator-driven gate failure).
  2. ``--check`` exits non-zero when an adapter output's projected mode
     diverges from the manifest's declared mode (drift-driven gate
     failure).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = PACKAGE_ROOT / "scripts" / "generate_agent_workflows.py"
CODEX_ROUTER_BEGIN = "<!-- BEGIN GENERATED: codex-command-router -->"
CODEX_ROUTER_END = "<!-- END GENERATED: codex-command-router -->"


def _seed_temp_repo(tmp_path: Path, command: dict[str, object]) -> Path:
    """Lay down a self-contained mini-repo the generator can target.

    Returns the manifest path. The repo includes:
      - ``config/agent-workflows/portable_commands.json``
      - ``skills/<slug>/skill.yaml`` + ``body.md`` (generator looks here)
      - ``CLAUDE.md`` and ``docs/agentic/instructions.md`` with the
        codex-router begin/end markers the generator rewrites.
    """
    temp_repo = tmp_path / "repo"
    manifest_path = temp_repo / "config" / "agent-workflows" / "portable_commands.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps({"version": 2, "commands": [command]}))

    slug = command["skill"]
    target = command.get("makefile_target", "make noop")
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
        "tdd_gate: none\n".format(slug, target)
    )
    (skill_dir / "body.md").write_text("## Overview\n\nTest stub.\n")

    instructions = temp_repo / "docs" / "agentic" / "instructions.md"
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text(
        f"before\n{CODEX_ROUTER_BEGIN}\nplaceholder\n{CODEX_ROUTER_END}\nafter\n"
    )
    (temp_repo / "CLAUDE.md").write_text(
        f"before\n{CODEX_ROUTER_BEGIN}\nplaceholder\n{CODEX_ROUTER_END}\nafter\n"
    )
    return manifest_path


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


_VALID_COMMAND = {
    "command_id": "planning-review",
    "skill": "planning-review",
    "mode": "verify",
    "makefile_target": "make plan-review DOC=<path>",
    "description": "Review a planning doc.",
    "execution_context": "Use for planning review.",
    "argument_schema": [{"name": "doc", "required": True, "description": "Plan path."}],
    "loop": ["load doc", "record findings"],
}


def test_check_gate_fails_when_manifest_omits_mode(tmp_path: Path) -> None:
    """``--check`` must surface a missing-mode manifest as a gate failure."""
    missing_mode_command = {k: v for k, v in _VALID_COMMAND.items() if k != "mode"}
    manifest = _seed_temp_repo(tmp_path, missing_mode_command)
    temp_repo = manifest.parents[2]

    proc = _run_generator(
        "--manifest",
        str(manifest),
        "--claude-out",
        str(temp_repo / ".claude" / "commands"),
        "--prompts-out",
        str(temp_repo / ".github" / "prompts"),
        "--check",
    )

    assert proc.returncode != 0, (
        "`--check` must fail when a manifest command omits the `mode` field; "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "mode" in proc.stderr, (
        f"gate error must name the missing `mode` field; stderr={proc.stderr!r}"
    )


def test_check_gate_fails_when_adapter_mode_diverges_from_manifest(tmp_path: Path) -> None:
    """If an adapter's projected mode is hand-edited away from the
    manifest's declared mode, ``--check`` must report drift and exit
    non-zero. This guards the manifest -> adapter projection contract
    against silent post-generation tampering."""
    manifest = _seed_temp_repo(tmp_path, _VALID_COMMAND)
    temp_repo = manifest.parents[2]
    claude_out = temp_repo / ".claude" / "commands"
    prompts_out = temp_repo / ".github" / "prompts"

    seed = _run_generator(
        "--manifest",
        str(manifest),
        "--claude-out",
        str(claude_out),
        "--prompts-out",
        str(prompts_out),
    )
    assert seed.returncode == 0, seed.stderr

    # WORKSTATE-REF-02 implementation note cutover: the `.claude/commands/<id>.md` surface is
    # no longer emitted; the Copilot prompt is now the per-slug carrier
    # of the projected Mode line.
    prompt_stub = prompts_out / f"{_VALID_COMMAND['command_id']}.prompt.md"
    original = prompt_stub.read_text()
    assert "Mode: `verify`" in original, (
        f"Slice-2 projection must have rendered a verify Mode line; got:\n{original}"
    )
    drifted = original.replace("Mode: `verify`", "Mode: `guide`")
    assert drifted != original, "drift mutation must change the rendered text"
    prompt_stub.write_text(drifted)

    proc = _run_generator(
        "--manifest",
        str(manifest),
        "--claude-out",
        str(claude_out),
        "--prompts-out",
        str(prompts_out),
        "--check",
    )

    assert proc.returncode != 0, (
        "`--check` must fail when an adapter's Mode line diverges from the "
        f"manifest; stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
    assert "drift" in proc.stderr.lower() or "mode" in proc.stderr.lower(), (
        f"gate error must signal mode drift; stderr={proc.stderr!r}"
    )
