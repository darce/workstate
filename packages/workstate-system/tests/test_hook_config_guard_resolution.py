"""Smoke regression for PreToolUse/PostToolUse guard resolution (implementation note).

Mirror of ``test_post_checkout_hook_guard_resolution.py`` for the rendered
tool-hook configs: every ``command`` path the canonical payload hook configs
name must exist in the payload itself. WS-TERMGUARD-RETIRE-01 deleted
``scripts/hooks/terminal-guard.py`` while an installed config still invoked
it, fail-closing every tool in a Copilot session; a retirement that deletes a
guard script but leaves any payload config reference re-surfaces here as a
CI failure instead of a runtime errno-2.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from scripts.check_harness_sync import _flatten_vscode_entries

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"

# Every hooks-bearing config the payload ships. Extend when the renderer
# gains new output channels (the installed-side enumeration is single-sourced
# via workstate_bootstrap.coherence; this source-side pin stays explicit so a
# new config home is a conscious decision).
PAYLOAD_HOOK_CONFIGS = (
    PAYLOAD_ROOT / ".github" / "hooks" / "terminal-guard.json",
    PAYLOAD_ROOT / ".codex" / "hooks.json",
)

_INTERPRETERS = {"python", "python3", "bash", "sh"}


def _command_strings(node: object) -> list[str]:
    commands: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                commands.append(value)
            else:
                commands.extend(_command_strings(value))
    elif isinstance(node, list):
        for item in node:
            commands.extend(_command_strings(item))
    return commands


def _stage_entries(config_path: Path, stage: str) -> list[dict]:
    payload = json.loads(config_path.read_text())
    return payload["hooks"][stage]


@pytest.mark.parametrize("config_path", PAYLOAD_HOOK_CONFIGS, ids=lambda p: p.name)
def test_every_payload_hook_command_resolves(config_path: Path) -> None:
    assert config_path.is_file(), f"payload hook config missing: {config_path}"
    commands = _command_strings(json.loads(config_path.read_text()))
    assert commands, "config carries no commands — shape drift?"

    missing: list[str] = []
    for command in commands:
        for token in shlex.split(command):
            if token in _INTERPRETERS or token.startswith("-"):
                continue
            if "/" not in token and not token.endswith((".py", ".sh")):
                continue
            if not (PAYLOAD_ROOT / token).exists():
                missing.append(f"{command!r} -> {token}")
    assert not missing, (
        "payload hook config references scripts absent from the payload "
        f"(retirement left a dangling reference): {missing}"
    )


def test_vscode_post_tool_use_uses_nested_hooks_shape() -> None:
    entries = _stage_entries(
        PAYLOAD_ROOT / ".github" / "hooks" / "terminal-guard.json",
        "PostToolUse",
    )
    assert entries, "expected PostToolUse entries in VS Code payload config"
    for entry in entries:
        nested = entry.get("hooks")
        assert isinstance(nested, list) and nested, entry


_PRE_NESTED_POST_TOOL_USE = [
    {
        "matcher": "Edit|Write",
        "hooks": [
            {
                "type": "command",
                "command": "python3 scripts/hooks/record-file-touch.py",
                "timeout": 10,
            }
        ],
    },
    {
        "matcher": "mcp_workstate-handoff-mcp_review_findings|mcp__workstate-handoff-mcp__review_findings",
        "hooks": [
            {
                "type": "command",
                "command": "python3 scripts/hooks/ace-detect.py",
                "timeout": 5,
            }
        ],
    },
    {
        "matcher": "mcp_workstate-handoff-mcp_get_handoff_state|mcp_workstate-handoff-mcp_load_session|mcp_workstate-handoff-mcp_render_handoff|mcp__workstate-handoff-mcp__get_handoff_state|mcp__workstate-handoff-mcp__load_session|mcp__workstate-handoff-mcp__render_handoff",
        "type": "command",
        "command": "python3 scripts/hooks/slim-handoff-response.py",
        "timeout": 5,
    },
    {
        "matcher": "Bash",
        "type": "command",
        "command": "python3 scripts/hooks/filter-test-output.py",
        "timeout": 5,
    },
    {
        "matcher": "Bash",
        "type": "command",
        "command": "python3 scripts/hooks/capture-agent-errors.py",
        "timeout": 15,
    },
]


def test_vscode_post_tool_use_flattening_stays_wiring_neutral() -> None:
    # implementation note implementation note: rendered commands carry the fail-open wrapper prefix;
    # the frozen pre-conversion wiring is compared modulo the same transform
    # (matcher -> handler dispatch is what must stay identical).
    from scripts.check_harness_sync import _load_wrap_guard_command

    wrap = _load_wrap_guard_command(PACKAGE_ROOT)
    stage_entries = _stage_entries(
        PAYLOAD_ROOT / ".github" / "hooks" / "terminal-guard.json",
        "PostToolUse",
    )
    expected = {
        (matcher, wrap(command))
        for matcher, command in _flatten_vscode_entries(_PRE_NESTED_POST_TOOL_USE)
    }
    assert _flatten_vscode_entries(stage_entries) == expected
