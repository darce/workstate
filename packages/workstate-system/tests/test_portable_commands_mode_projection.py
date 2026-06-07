"""WORKSTATE-REF-1 implementation note: generator projects `mode` into adapter outputs.

Two adapter surfaces must surface the manifest's interaction mode so
consumers can route correctly (the `.claude/commands/<id>.md` stub
surface was retired by WORKSTATE-REF-02 implementation note; the plugin tree owns it now):
  - .github/prompts/<id>.prompt.md  (VS Code Copilot prompts)
  - codex command-router (CLAUDE.md, docs/workstate/instructions.md, and
    .codex/.../codex-command-router.md): the command map lists must
    annotate each entry with its declared mode.
"""

from __future__ import annotations

import generate_agent_workflows as gen


_GUIDE_COMMAND = {
    "command_id": "scope",
    "skill": "scope",
    "mode": "guide",
    "makefile_target": "(in-session intake; no standalone make target)",
    "description": "Question-first intake.",
    "execution_context": "Use for intake.",
    "argument_schema": [],
    "loop": ["ask", "record"],
}

_VERIFY_COMMAND = {
    "command_id": "planning-review",
    "skill": "planning-review",
    "mode": "verify",
    "makefile_target": "make plan-review DOC=<path>",
    "description": "Formal planning review.",
    "execution_context": "Use for plan review.",
    "argument_schema": [{"name": "doc", "required": True, "description": "Planning doc path."}],
    "loop": ["load", "review", "record"],
}

_WRITE_COMMAND = {
    "command_id": "tdd",
    "skill": "tdd",
    "mode": "write",
    "makefile_target": "make slice-start TASK=<task-ref> TEST_CMD=\"<command>\"",
    "description": "RED-first slice opening.",
    "execution_context": "Use at slice start.",
    "argument_schema": [
        {"name": "task-ref", "required": True, "description": "Active task."},
        {"name": "test-cmd", "required": True, "description": "Failing test."},
    ],
    "loop": ["red", "implement", "green"],
}


def test_vscode_prompt_surfaces_mode() -> None:
    for command in (_GUIDE_COMMAND, _VERIFY_COMMAND, _WRITE_COMMAND):
        out = gen._render_prompt_with_guidance(command, None, {})
        assert f"Mode: `{command['mode']}`" in out, (
            f"VS Code prompt for /{command['command_id']} must surface "
            f"`Mode: \\`{command['mode']}\\``; got:\n{out}"
        )


def test_codex_router_command_map_annotates_mode() -> None:
    manifest = {
        "version": 2,
        "commands": [_GUIDE_COMMAND, _VERIFY_COMMAND, _WRITE_COMMAND],
    }
    body = "\n".join(gen._render_codex_router_body(manifest))
    for command in manifest["commands"]:
        marker = f"`/{command['command_id']}`"
        assert marker in body
        # The annotation must travel with the same line as the command id
        # in the command map so consumers can read mode without a separate lookup.
        for line in body.splitlines():
            if line.startswith(f"- `/{command['command_id']}`"):
                assert f"({command['mode']})" in line, (
                    f"codex router command-map line for {marker} must annotate "
                    f"`({command['mode']})`; got: {line!r}"
                )
                break
        else:
            raise AssertionError(f"no command-map line for {marker} in:\n{body}")
