from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = PACKAGE_ROOT / "scripts" / "generate_agent_workflows.py"
CODEX_ROUTER_BEGIN = "<!-- BEGIN GENERATED: codex-command-router -->"
CODEX_ROUTER_END = "<!-- END GENERATED: codex-command-router -->"


def _run_generator(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GENERATOR), *args],
        cwd=PACKAGE_ROOT,
        capture_output=True,
        text=True,
    )


_MINIMAL_BODY = """## Overview

Test stub.
"""


def _write_temp_manifest(tmp_path: Path, payload: dict[str, object]) -> Path:
    temp_repo = tmp_path / "repo"
    manifest = temp_repo / "config" / "agent-workflows" / "portable_commands.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(payload))

    # Seed canonical skill sources for every skill referenced in the
    # manifest (implementation note step 1: generator validates skill.yaml exists).
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
        (skill_dir / "body.md").write_text(_MINIMAL_BODY)

    instructions = temp_repo / "docs" / "workstate" / "instructions.md"
    instructions.parent.mkdir(parents=True, exist_ok=True)
    instructions.write_text(f"before\n{CODEX_ROUTER_BEGIN}\nplaceholder\n{CODEX_ROUTER_END}\nafter\n")

    claude_md = temp_repo / "CLAUDE.md"
    claude_md.write_text(f"before\n{CODEX_ROUTER_BEGIN}\nplaceholder\n{CODEX_ROUTER_END}\nafter\n")
    return manifest


def test_generate_agent_workflows_writes_expected_files(tmp_path: Path) -> None:
    manifest = _write_temp_manifest(
        tmp_path,
        {
            "version": 1,
            "commands": [
                {
                    "command_id": "branch-review",
                    "skill": "branch-review",
                    "mode": "verify",
                    "makefile_target": "make review-run",
                    "description": "Review a branch diff.",
                    "execution_context": "Use for branch review.",
                    "argument_schema": [{"name": "scope", "required": False, "description": "Optional diff scope."}],
                    "loop": ["load diff", "record findings"],
                }
            ],
        },
    )
    temp_repo = manifest.parents[2]
    claude_out = temp_repo / ".claude" / "commands"
    prompts_out = temp_repo / ".github" / "prompts"

    proc = _run_generator(
        "--manifest",
        str(manifest),
        "--claude-out",
        str(claude_out),
        "--prompts-out",
        str(prompts_out),
    )
    assert proc.returncode == 0, proc.stderr

    # WORKSTATE-REF-02 implementation note cutover: the `.claude/commands/<id>.md` surface is
    # no longer emitted by the legacy mode; the plugin tree owns it.
    claude_file = claude_out / "branch-review.md"
    prompt_file = prompts_out / "branch-review.prompt.md"
    assert not claude_file.exists()
    assert prompt_file.exists()
    assert "Load the `branch-review` skill" in prompt_file.read_text()
    assert "Test stub." in prompt_file.read_text()


def test_generate_agent_workflows_check_detects_drift(tmp_path: Path) -> None:
    manifest = _write_temp_manifest(
        tmp_path,
        {
            "version": 1,
            "commands": [
                {
                    "command_id": "planning-review",
                    "skill": "planning-review",
                    "mode": "verify",
                    "makefile_target": "make plan-review DOC=<path>",
                    "description": "Review a planning doc.",
                    "execution_context": "Use for planning review.",
                    "argument_schema": [{"name": "doc", "required": True, "description": "Planning document path."}],
                    "loop": ["load doc", "record findings"],
                }
            ],
        },
    )
    temp_repo = manifest.parents[2]
    claude_out = temp_repo / ".claude" / "commands"
    prompts_out = temp_repo / ".github" / "prompts"

    first = _run_generator(
        "--manifest",
        str(manifest),
        "--claude-out",
        str(claude_out),
        "--prompts-out",
        str(prompts_out),
    )
    assert first.returncode == 0, first.stderr

    # WORKSTATE-REF-02 implementation note: `.claude/commands/<id>.md` is no longer emitted,
    # so drift detection on the Claude surface no longer applies. The
    # Copilot prompt surface is now the per-slug drift signal.
    drift_target = prompts_out / "planning-review.prompt.md"
    drift_target.write_text(drift_target.read_text() + "\nmanual drift\n")

    drift = _run_generator(
        "--manifest",
        str(manifest),
        "--claude-out",
        str(claude_out),
        "--prompts-out",
        str(prompts_out),
        "--check",
    )
    assert drift.returncode == 1
    assert "drift detected" in drift.stderr


def test_generate_agent_workflows_embeds_lint_safe_router_lists(tmp_path: Path) -> None:
    manifest = _write_temp_manifest(
        tmp_path,
        {
            "version": 1,
            "commands": [
                {
                    "command_id": "handoff-lifecycle",
                    "skill": "handoff-lifecycle",
                    "mode": "guide",
                    "makefile_target": "make context",
                    "description": "Load task context.",
                    "execution_context": "Use for session context loading.",
                    "argument_schema": [],
                    "loop": ["run make context", "load task state"],
                }
            ],
        },
    )

    proc = _run_generator("--manifest", str(manifest))

    assert proc.returncode == 0, proc.stderr
    claude_md = manifest.parents[2] / "CLAUDE.md"
    content = claude_md.read_text()
    assert "Routing rules:\n\n- Strip" in content
    assert "Command map:\n\n- `" in content


def test_generate_agent_workflows_rejects_invalid_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "portable_commands.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "commands": [
                    {
                        "command_id": "bad",
                        "skill": "",
                        "makefile_target": "make something",
                        "description": "Broken entry.",
                        "execution_context": "Broken.",
                        "argument_schema": [],
                        "loop": ["one step"],
                    }
                ],
            }
        )
    )

    proc = _run_generator("--manifest", str(manifest), "--claude-out", str(tmp_path / "claude"))
    assert proc.returncode == 1
    assert "skill must be a non-empty string" in proc.stderr


def test_generate_agent_workflows_skips_cross_harness_skill_files(tmp_path: Path) -> None:
    """WORKSTATE-REF-02 implementation note: cutover regression.

    After the cutover the generator must NOT emit per-slug
    ``.claude/commands/<id>.md``, ``.claude/skills/<slug>/SKILL.md``, or
    ``.codex/skills/<slug>/SKILL.md`` for slugs declared in the manifest
    (the "portable" definition). The plugin tree owns the cross-harness
    skill surface; the legacy mode only produces the Copilot prompt and
    the codex-command-router.
    """
    manifest = _write_temp_manifest(
        tmp_path,
        {
            "version": 1,
            "commands": [
                {
                    "command_id": "branch-review",
                    "skill": "branch-review",
                    "mode": "verify",
                    "makefile_target": "make review-run",
                    "description": "Review a branch diff.",
                    "execution_context": "Use for branch review.",
                    "argument_schema": [],
                    "loop": ["load diff", "record findings"],
                },
                {
                    "command_id": "planning-review",
                    "skill": "planning-review",
                    "mode": "verify",
                    "makefile_target": "make plan-review DOC=<path>",
                    "description": "Review a planning doc.",
                    "execution_context": "Use for planning review.",
                    "argument_schema": [],
                    "loop": ["load doc", "record findings"],
                },
            ],
        },
    )
    target = manifest.parents[2]

    proc = _run_generator("--manifest", str(manifest), "--target", str(target))
    assert proc.returncode == 0, proc.stderr

    portable_slugs = {"branch-review", "planning-review"}
    for slug in portable_slugs:
        assert not (target / ".claude" / "commands" / f"{slug}.md").exists(), (
            f".claude/commands/{slug}.md must not be emitted after WORKSTATE-REF-02 cutover"
        )
        assert not (target / ".claude" / "skills" / slug).exists(), (
            f".claude/skills/{slug}/ must not be emitted after WORKSTATE-REF-02 cutover"
        )
        assert not (target / ".codex" / "skills" / slug).exists(), (
            f".codex/skills/{slug}/ must not be emitted after WORKSTATE-REF-02 cutover"
        )

    for slug in portable_slugs:
        assert (target / ".github" / "prompts" / f"{slug}.prompt.md").exists(), (
            f"Copilot prompt for {slug} must still be emitted"
        )
    assert (target / "docs" / "workstate" / "generated" / "codex-command-router.md").exists()
