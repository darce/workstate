"""WS-HOOKGEN-01 implementation note: hooks contract display metadata + Codex validation.

Pins two behaviors:

* The live ``harness-protocol.yaml`` ``hooks:`` section carries per-entry
  display metadata (``status_message``), an explicit ``timeout``, and a
  ``codex_command`` so per-harness configs can be generated from the contract
  instead of hand-authored.
* ``check_harness_sync._check_hooks`` validates Codex hook wiring
  (``.codex/hooks.json``) with per-harness matcher overrides, mirroring the
  consumer-proven shape (context-alt-text-monorepo) so a package refresh does
  not regress consumers to codex-blind validation.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

yaml = pytest.importorskip("yaml")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from scripts.check_harness_sync import (  # noqa: WORKSTATE-REF-402
    _check_codex_status_messages,
    _check_hooks,
    _load_wrap_guard_command,
)

# implementation note implementation note: generated configs carry the fail-open wrapper prefix;
# fixtures emulate the generator by writing the wrapped form.
_wrap = _load_wrap_guard_command(PACKAGE_ROOT)

CONTRACT_PATH = (
    PACKAGE_ROOT
    / "workstate_system"
    / "payload"
    / "docs"
    / "workstate"
    / "contracts"
    / "harness-protocol.yaml"
)
HOOK_STAGES = ("pre_tool_use", "post_tool_use", "session_start", "user_prompt_submit")


def _live_hook_entries() -> list[tuple[str, dict]]:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    spec = contract["hooks"]
    return [(stage, item) for stage in HOOK_STAGES for item in (spec.get(stage) or [])]


# --- live contract: display + codex metadata -------------------------------


def test_live_contract_has_hook_entries() -> None:
    assert _live_hook_entries(), "harness-protocol.yaml hooks section must not be empty"


def test_every_live_hook_entry_declares_status_message() -> None:
    missing = [
        f"{stage}:{item.get('id')}"
        for stage, item in _live_hook_entries()
        if not isinstance(item.get("status_message"), str)
        or not item["status_message"].strip()
    ]
    assert not missing, f"hooks missing non-empty status_message: {missing}"


def test_every_live_hook_entry_declares_positive_int_timeout() -> None:
    bad = [
        f"{stage}:{item.get('id')}"
        for stage, item in _live_hook_entries()
        if not isinstance(item.get("timeout"), int)
        or isinstance(item.get("timeout"), bool)
        or item["timeout"] <= 0
    ]
    assert not bad, f"hooks missing positive integer timeout: {bad}"


def test_every_live_hook_entry_declares_codex_command_or_documented_optout() -> None:
    # Codex opt-out contract: omitting codex_command requires
    # `codex_enabled: false` + a non-empty `unsupported_reason`; an opted-out
    # entry must not also carry a codex_command.
    bad = []
    for stage, item in _live_hook_entries():
        command = item.get("codex_command")
        if isinstance(command, str) and command.strip():
            if item.get("codex_enabled") is False:
                bad.append(
                    f"{stage}:{item.get('id')} (codex_enabled: false with codex_command)"
                )
            continue
        reason = item.get("unsupported_reason")
        if (
            item.get("codex_enabled") is False
            and isinstance(reason, str)
            and reason.strip()
        ):
            continue
        bad.append(
            f"{stage}:{item.get('id')} (missing codex_command without documented opt-out)"
        )
    assert not bad, f"hooks violating codex_command/opt-out contract: {bad}"


# --- _check_hooks codex validation ------------------------------------------


def _fixture_contract() -> dict:
    return {
        "hooks": {
            "pre_tool_use": [
                {
                    "id": "guard-x",
                    "matcher": "Bash",
                    "status_message": "Guarding X",
                    "timeout": 5,
                    "claude_command": "python3 claude-x.py",
                    "vscode_command": "python3 vscode-x.py",
                    "codex_command": "python3 codex-x.py",
                }
            ],
            "post_tool_use": [
                {
                    # Shared matcher carries both harness alternations; the
                    # codex config only lists the double-underscore form, so the
                    # entry needs a codex_matcher override.
                    "id": "touch-y",
                    "matcher": "mcp_h_y|mcp__h__y",
                    "codex_matcher": "mcp__h__y",
                    "status_message": "Touching Y",
                    "timeout": 10,
                    "claude_command": "python3 claude-y.py",
                    "vscode_command": "python3 vscode-y.py",
                    "codex_command": "python3 codex-y.py",
                }
            ],
        }
    }


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _write_vscode_config(repo_root: Path) -> None:
    _write_json(
        repo_root / ".github" / "hooks" / "terminal-guard.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "type": "command",
                        "command": _wrap("python3 vscode-x.py"),
                    }
                ],
                "PostToolUse": [
                    {
                        "matcher": "mcp_h_y|mcp__h__y",
                        "hooks": [
                            {"type": "command", "command": _wrap("python3 vscode-y.py")}
                        ],
                    }
                ],
            }
        },
    )


def _write_codex_config(repo_root: Path, *, include_post: bool = True) -> None:
    post = (
        [
            {
                "matcher": "mcp__h__y",
                "hooks": [
                    {
                        "type": "command",
                        "command": _wrap("python3 codex-y.py"),
                        "timeout": 10,
                        "statusMessage": "Touching Y",
                    }
                ],
            }
        ]
        if include_post
        else []
    )
    _write_json(
        repo_root / ".codex" / "hooks.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "type": "command",
                        "command": _wrap("python3 codex-x.py"),
                        "timeout": 5,
                        "statusMessage": "Guarding X",
                    }
                ],
                "PostToolUse": post,
            }
        },
    )


def test_check_hooks_passes_when_codex_pairs_present(tmp_path: Path) -> None:
    _write_vscode_config(tmp_path)
    _write_codex_config(tmp_path)
    errors = _check_hooks(_fixture_contract(), repo_root=tmp_path)
    assert errors == []


def test_check_hooks_flags_missing_codex_hook(tmp_path: Path) -> None:
    _write_vscode_config(tmp_path)
    _write_codex_config(tmp_path, include_post=False)
    errors = _check_hooks(_fixture_contract(), repo_root=tmp_path)
    assert "missing Codex hook `touch-y` (post_tool_use)" in errors
    assert not any("vscode" in error.lower() for error in errors)


def test_check_hooks_skips_codex_when_config_absent(tmp_path: Path) -> None:
    # Generated per-agent artifacts are optional at this layer (same posture as
    # .claude/settings.json): codex wiring is only validated when the file exists.
    _write_vscode_config(tmp_path)
    errors = _check_hooks(_fixture_contract(), repo_root=tmp_path)
    assert errors == []


# --- codex_enabled opt-out contract -------------------------------------------


def test_check_hooks_flags_missing_codex_command_without_optout(tmp_path: Path) -> None:
    # Silent omission of codex_command is contract drift even when no
    # .codex/hooks.json exists — this is a contract-shape rule, not a
    # rendered-config presence check.
    contract = _fixture_contract()
    del contract["hooks"]["pre_tool_use"][0]["codex_command"]
    _write_vscode_config(tmp_path)
    errors = _check_hooks(contract, repo_root=tmp_path)
    assert any("omits codex_command" in e and "guard-x" in e for e in errors)


def test_check_hooks_accepts_documented_codex_optout(tmp_path: Path) -> None:
    contract = _fixture_contract()
    entry = contract["hooks"]["pre_tool_use"][0]
    del entry["codex_command"]
    entry["codex_enabled"] = False
    entry["unsupported_reason"] = "Codex surfaces no equivalent tool event"
    _write_vscode_config(tmp_path)
    errors = _check_hooks(contract, repo_root=tmp_path)
    assert errors == []


def test_check_hooks_tolerates_legacy_contract_without_codex_fields(
    tmp_path: Path,
) -> None:
    # A stale consumer mirror whose contract pre-dates the Codex fields must
    # not fail the opt-out shape rule (it only binds codex-aware contracts).
    contract = _fixture_contract()
    for entries in contract["hooks"].values():
        for entry in entries:
            entry.pop("codex_command", None)
            entry.pop("codex_matcher", None)
    _write_vscode_config(tmp_path)
    errors = _check_hooks(contract, repo_root=tmp_path)
    assert errors == []


def test_check_hooks_flags_optout_combined_with_codex_command(tmp_path: Path) -> None:
    contract = _fixture_contract()
    entry = contract["hooks"]["pre_tool_use"][0]
    entry["codex_enabled"] = False
    entry["unsupported_reason"] = "contradicts the codex_command it still carries"
    _write_vscode_config(tmp_path)
    _write_codex_config(tmp_path)
    errors = _check_hooks(contract, repo_root=tmp_path)
    assert any("still sets codex_command" in e and "guard-x" in e for e in errors)


def test_check_hooks_flags_missing_claude_and_vscode_commands(tmp_path: Path) -> None:
    # claude_command / vscode_command have no opt-out analogue; omitting either
    # must surface as named contract drift, not an uncaught KeyError.
    contract = _fixture_contract()
    del contract["hooks"]["pre_tool_use"][0]["claude_command"]
    del contract["hooks"]["pre_tool_use"][0]["vscode_command"]
    _write_vscode_config(tmp_path)
    errors = _check_hooks(contract, repo_root=tmp_path)
    assert any("omits claude_command" in e and "guard-x" in e for e in errors)
    assert any("omits vscode_command" in e and "guard-x" in e for e in errors)
    assert not any("missing VS Code hook `guard-x`" in e for e in errors)


# --- WS-HOOKGEN-01 implementation note: statusMessage enforcement -------------------------


def test_codex_status_messages_pass_on_fully_labelled_config(tmp_path: Path) -> None:
    _write_codex_config(tmp_path)
    assert _check_codex_status_messages(repo_root=tmp_path) == []


def test_codex_status_messages_flag_nested_handler_missing_label(
    tmp_path: Path,
) -> None:
    _write_json(
        tmp_path / ".codex" / "hooks.json",
        {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Edit|Write",
                        "hooks": [
                            {
                                "type": "command",
                                "command": "python3 scripts/hooks/record-file-touch.py",
                                "timeout": 10,
                            }
                        ],
                    }
                ]
            }
        },
    )
    errors = _check_codex_status_messages(repo_root=tmp_path)
    assert errors == [
        "Codex hook handler missing statusMessage: PostToolUse "
        "`python3 scripts/hooks/record-file-touch.py`"
    ]


def test_codex_status_messages_flag_flat_handler_missing_label(tmp_path: Path) -> None:
    _write_json(
        tmp_path / ".codex" / "hooks.json",
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "type": "command",
                        "command": _wrap("python3 codex-x.py"),
                        "timeout": 5,
                    }
                ],
                "SessionStart": [
                    {
                        "type": "command",
                        "command": "python3 advise.py",
                        "timeout": 5,
                        "statusMessage": "   ",
                    }
                ],
            }
        },
    )
    errors = _check_codex_status_messages(repo_root=tmp_path)
    assert (
        "Codex hook handler missing statusMessage: PreToolUse "
        f"`{_wrap('python3 codex-x.py')}`"
        in errors
    )
    assert (
        "Codex hook handler missing statusMessage: SessionStart `python3 advise.py`"
        in errors
    )


def test_codex_status_messages_skip_when_config_absent(tmp_path: Path) -> None:
    assert _check_codex_status_messages(repo_root=tmp_path) == []


def test_check_hooks_codex_matcher_override_is_respected(tmp_path: Path) -> None:
    _write_vscode_config(tmp_path)
    _write_codex_config(tmp_path)
    contract = _fixture_contract()
    # Without the override the shared matcher would not match the codex config.
    assert contract["hooks"]["post_tool_use"][0]["codex_matcher"] == "mcp__h__y"
    errors = _check_hooks(contract, repo_root=tmp_path)
    assert errors == []
