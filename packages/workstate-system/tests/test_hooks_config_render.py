"""WS-HOOKGEN-01 implementation note: per-harness hook configs render from the contract.

Pins the single-source generation path: ``harness-protocol.yaml`` ``hooks:``
is the only authoring surface; ``.github/hooks/terminal-guard.json`` (VS Code)
and ``.codex/hooks.json`` (Codex) are generated goldens. Codex output carries
``statusMessage`` on every inner command handler — including nested
``PostToolUse[].hooks[]`` entries — so the Codex UI shows named hooks instead
of "Hook 1 / Hook 2".
"""

from __future__ import annotations

import importlib.util
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
    _flatten_vscode_entries,
)

PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
GENERATOR = PAYLOAD_ROOT / "scripts" / "generate_agent_workflows.py"
CONTRACT_PATH = (
    PAYLOAD_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
)
VSCODE_GOLDEN = PAYLOAD_ROOT / ".github" / "hooks" / "terminal-guard.json"
CODEX_GOLDEN = PAYLOAD_ROOT / ".codex" / "hooks.json"


def _generator_module():
    spec = importlib.util.spec_from_file_location("generate_agent_workflows", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _contract_hooks() -> dict:
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    return contract["hooks"]


def _iter_command_handlers(config: dict):
    """Yield every inner command handler, flat or nested under hooks[]."""
    for stage_entries in config["hooks"].values():
        for entry in stage_entries:
            nested = entry.get("hooks")
            if isinstance(nested, list):
                yield from nested
            elif "command" in entry:
                yield entry


# --- golden parity ----------------------------------------------------------


def test_rendered_vscode_config_matches_payload_golden() -> None:
    module = _generator_module()
    rendered = module.render_vscode_hooks_config(_contract_hooks())
    assert rendered == json.loads(VSCODE_GOLDEN.read_text())


def test_rendered_codex_config_matches_payload_golden() -> None:
    module = _generator_module()
    rendered = module.render_codex_hooks_config(_contract_hooks())
    assert CODEX_GOLDEN.is_file(), (
        "payload must ship a generated .codex/hooks.json golden"
    )
    assert rendered == json.loads(CODEX_GOLDEN.read_text())


def test_goldens_serialize_with_generator_formatting() -> None:
    module = _generator_module()
    expected = module.render_hooks_config_text(
        module.render_vscode_hooks_config(_contract_hooks())
    )
    assert VSCODE_GOLDEN.read_text() == expected
    expected_codex = module.render_hooks_config_text(
        module.render_codex_hooks_config(_contract_hooks())
    )
    assert CODEX_GOLDEN.read_text() == expected_codex


# --- codex display metadata ---------------------------------------------------


def test_codex_render_sets_status_message_on_every_command_handler() -> None:
    module = _generator_module()
    rendered = module.render_codex_hooks_config(_contract_hooks())
    handlers = list(_iter_command_handlers(rendered))
    assert handlers
    for handler in handlers:
        assert handler.get("type") == "command"
        message = handler.get("statusMessage")
        assert isinstance(message, str) and message.strip(), handler


def test_codex_post_tool_use_uses_nested_hooks_shape() -> None:
    module = _generator_module()
    rendered = module.render_codex_hooks_config(_contract_hooks())
    post = rendered["hooks"]["PostToolUse"]
    assert post
    for entry in post:
        assert isinstance(entry.get("hooks"), list) and entry["hooks"], entry
        for handler in entry["hooks"]:
            assert (
                isinstance(handler.get("statusMessage"), str)
                and handler["statusMessage"].strip()
            )


def test_vscode_render_omits_status_message() -> None:
    # VS Code's runner has no display-metadata field yet; the contract value is
    # codex-only until other harness schemas grow an equivalent.
    module = _generator_module()
    rendered = module.render_vscode_hooks_config(_contract_hooks())
    for handler in _iter_command_handlers(rendered):
        assert "statusMessage" not in handler


# --- round trip through the validator ----------------------------------------


def test_rendered_configs_satisfy_check_hooks_round_trip(tmp_path: Path) -> None:
    module = _generator_module()
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    vscode_path = tmp_path / ".github" / "hooks" / "terminal-guard.json"
    codex_path = tmp_path / ".codex" / "hooks.json"
    vscode_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    vscode_path.write_text(
        module.render_hooks_config_text(
            module.render_vscode_hooks_config(contract["hooks"])
        )
    )
    codex_path.write_text(
        module.render_hooks_config_text(
            module.render_codex_hooks_config(contract["hooks"])
        )
    )
    grok_path = (
        tmp_path
        / ".workstate"
        / "generated"
        / "plugins"
        / "workstate-system"
        / "base"
        / "grok"
        / "hooks"
        / "hooks.json"
    )
    grok_path.parent.mkdir(parents=True, exist_ok=True)
    grok_path.write_text(module._render_grok_plugin_hooks_json(contract))
    assert _check_hooks(contract, repo_root=tmp_path) == []


# --- codex_enabled opt-out (renderer side) ------------------------------------


def _optout_spec(**entry_overrides) -> dict:
    entry = {
        "id": "no-codex",
        "matcher": "Bash",
        "status_message": "Guarding",
        "timeout": 5,
        "claude_command": "python3 claude.py",
        "vscode_command": "python3 vscode.py",
    }
    entry.update(entry_overrides)
    # The anchor entry keeps the spec codex-aware so the opt-out rule binds
    # (a contract with zero codex fields is treated as legacy and skipped).
    anchor = {
        "id": "codex-anchor",
        "matcher": "Bash",
        "status_message": "Anchoring",
        "timeout": 5,
        "claude_command": "python3 claude-anchor.py",
        "vscode_command": "python3 vscode-anchor.py",
        "codex_command": "python3 codex-anchor.py",
    }
    return {"pre_tool_use": [entry, anchor]}


def test_codex_render_rejects_silent_codex_command_omission() -> None:
    module = _generator_module()
    with pytest.raises(ValueError, match="omits `codex_command`"):
        module.render_codex_hooks_config(_optout_spec())


def test_codex_render_treats_whitespace_codex_command_as_omitted() -> None:
    # Renderer and check_harness_sync must classify a whitespace-only
    # codex_command identically (omitted): the renderer may not emit a
    # whitespace handler the checker would then flag as missing opt-out.
    module = _generator_module()
    with pytest.raises(ValueError, match="omits `codex_command`"):
        module.render_codex_hooks_config(_optout_spec(codex_command="   "))


def test_codex_render_rejects_optout_without_reason() -> None:
    module = _generator_module()
    with pytest.raises(ValueError, match="unsupported_reason"):
        module.render_codex_hooks_config(_optout_spec(codex_enabled=False))


def test_codex_render_skips_documented_optout_entry() -> None:
    module = _generator_module()
    spec = _optout_spec(
        codex_enabled=False, unsupported_reason="no Codex equivalent event"
    )
    rendered = module.render_codex_hooks_config(spec)
    # Only the anchor survives; the opted-out entry is skipped from the
    # rendered config (the contract's own unsupported_reason is the audit
    # trail — the renderer emits nothing for it).
    assert [e["command"] for e in rendered["hooks"]["PreToolUse"]] == [
        "python3 scripts/hooks/_run_guard.py codex-anchor.py"
    ]
    # The VS Code renderer is unaffected by a codex opt-out.
    vscode = module.render_vscode_hooks_config(spec)
    assert [e["command"] for e in vscode["hooks"]["PreToolUse"]] == [
        "python3 scripts/hooks/_run_guard.py vscode.py",
        "python3 scripts/hooks/_run_guard.py vscode-anchor.py",
    ]


def test_codex_render_rejects_optout_combined_with_codex_command() -> None:
    module = _generator_module()
    with pytest.raises(ValueError, match="still sets `codex_command`"):
        module.render_codex_hooks_config(
            _optout_spec(
                codex_enabled=False,
                unsupported_reason="contradiction",
                codex_command="python3 codex.py",
            )
        )


def test_codex_render_tolerates_legacy_contract_without_codex_fields() -> None:
    # A stale consumer mirror whose contract pre-dates the Codex fields must
    # keep rendering (skip-silently) instead of hard-failing generation; the
    # opt-out rule only binds codex-aware contracts.
    module = _generator_module()
    legacy_spec = {
        "pre_tool_use": [
            {
                "id": "legacy",
                "matcher": "Bash",
                "claude_command": "python3 claude.py",
                "vscode_command": "python3 vscode.py",
            }
        ]
    }
    rendered = module.render_codex_hooks_config(legacy_spec)
    assert rendered["hooks"]["PreToolUse"] == []


# --- flat -> nested PostToolUse conversion is wiring-neutral -------------------


# Verbatim PostToolUse section of the last HAND-AUTHORED terminal-guard.json
# (main @ f3b8c450551d, immediately before this branch converted the file to a
# generated golden). slim-handoff-response and filter-test-output were FLAT
# entries there; record-file-touch and ace-detect were already nested — which
# is the existing proof the VS Code runner accepts the nested shape.
_PRE_CONVERSION_POST_TOOL_USE = [
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


def test_converted_post_tool_use_handlers_flatten_identically_to_legacy_flat_shape() -> (
    None
):
    # Non-tautological parity: the fixture above is the actual pre-conversion
    # hand-authored content (frozen from main @ f3b8c450551d, plus the flat
    # capture-agent-errors entry main added @ 7de6bd3 before this branch
    # merged main back in), NOT derived from the generator or the current
    # golden. The committed golden's PostToolUse must flatten to exactly the
    # same (matcher, command) wiring, so the flat -> nested conversion of the
    # hand-authored handlers cannot have changed what the runner dispatches.
    # implementation note implementation note: rendered commands now carry the fail-open wrapper
    # prefix, so the frozen pre-conversion wiring is compared modulo the same
    # transform the renderer applies (the dispatch wiring itself — matcher ->
    # handler script — is what must stay identical).
    module = _generator_module()
    golden_post = json.loads(VSCODE_GOLDEN.read_text())["hooks"]["PostToolUse"]
    expected = {
        (matcher, module._wrap_guard_command(command))
        for matcher, command in _flatten_vscode_entries(_PRE_CONVERSION_POST_TOOL_USE)
    }
    assert _flatten_vscode_entries(golden_post) == expected


# --- live contract vs shipped surfaces (not just synthetic fixtures) -----------


def test_live_contract_commands_reference_shipped_hook_scripts() -> None:
    # The contract grew entries (main-branch, filter-test-output) whose
    # claude/vscode/codex commands must reference scripts the payload actually
    # ships; otherwise the augmented contract silently outruns the distributed
    # surfaces.
    import shlex

    missing: list[str] = []
    for stage, entries in _contract_hooks().items():
        for item in entries:
            for key in ("claude_command", "vscode_command", "codex_command"):
                command = item.get(key)
                if not isinstance(command, str) or not command.strip():
                    continue
                for token in shlex.split(command)[1:]:
                    if token.startswith("-"):
                        continue
                    rel = token.replace("$CLAUDE_PROJECT_DIR/", "")
                    if not rel.endswith((".py", ".sh")):
                        continue
                    if not (PAYLOAD_ROOT / rel).is_file():
                        missing.append(f"{stage}:{item.get('id')} {key} -> {rel}")
    assert not missing, f"contract commands referencing unshipped scripts: {missing}"


def test_codex_status_message_enforcement_fires_on_rendered_live_config(
    tmp_path: Path,
) -> None:
    # _check_codex_status_messages is a no-op at a repo root without
    # .codex/hooks.json; pin that it actually validates (and passes) against
    # the live contract rendered into a target root, and fires when a nested
    # handler label is blanked.
    module = _generator_module()
    codex_path = tmp_path / ".codex" / "hooks.json"
    codex_path.parent.mkdir(parents=True, exist_ok=True)
    codex_path.write_text(
        module.render_hooks_config_text(
            module.render_codex_hooks_config(_contract_hooks())
        )
    )
    assert _check_codex_status_messages(repo_root=tmp_path) == []

    config = json.loads(codex_path.read_text())
    config["hooks"]["PostToolUse"][0]["hooks"][0]["statusMessage"] = "   "
    codex_path.write_text(json.dumps(config))
    errors = _check_codex_status_messages(repo_root=tmp_path)
    assert errors and "missing statusMessage" in errors[0]


# --- overlay symlink safety ---------------------------------------------------


def test_expected_hooks_outputs_skip_symlinked_surfaces(tmp_path: Path) -> None:
    # Overlay consumers materialize .github/hooks as a symlink into the
    # .workstate/remote clone; the renderer must never write through it.
    module = _generator_module()
    repo_root = tmp_path / "consumer"
    contract_dst = (
        repo_root / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    )
    contract_dst.parent.mkdir(parents=True, exist_ok=True)
    contract_dst.write_text(CONTRACT_PATH.read_text())

    clone_hooks = tmp_path / "clone" / ".github" / "hooks"
    clone_hooks.mkdir(parents=True, exist_ok=True)
    (repo_root / ".github").mkdir(parents=True, exist_ok=True)
    (repo_root / ".github" / "hooks").symlink_to(clone_hooks)

    outputs = module._expected_hooks_outputs(repo_root)
    assert repo_root / ".codex" / "hooks.json" in outputs
    assert repo_root / ".github" / "hooks" / "terminal-guard.json" not in outputs


# --- implementation note implementation note: fail-open wrapper injection ----------------------------


def test_wrap_guard_command_relative() -> None:
    module = _generator_module()
    assert module._wrap_guard_command("python3 scripts/hooks/x.py") == (
        "python3 scripts/hooks/_run_guard.py scripts/hooks/x.py"
    )


def test_wrap_guard_command_keeps_handler_surface_and_args() -> None:
    module = _generator_module()
    assert module._wrap_guard_command(
        "python3 .github/hooks/guard-main-branch.py --strict"
    ) == (
        "python3 scripts/hooks/_run_guard.py .github/hooks/guard-main-branch.py"
        " --strict"
    )


def test_wrap_guard_command_anchored_bash_keeps_anchor_quoting() -> None:
    # The wrapper path is emitted with the SAME per-harness anchor the command
    # uses, double-quoted so the harness still expands the variable; the
    # original interpreter word is dropped (the wrapper re-derives bash vs
    # python3 from the handler extension).
    module = _generator_module()
    wrapped = module._wrap_guard_command(
        'bash "$CLAUDE_PROJECT_DIR/scripts/hooks/guard-worktree-drift.sh"'
    )
    assert wrapped == (
        'python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/_run_guard.py" '
        '"$CLAUDE_PROJECT_DIR/scripts/hooks/guard-worktree-drift.sh"'
    )


def test_wrap_guard_command_grok_anchor() -> None:
    module = _generator_module()
    wrapped = module._wrap_guard_command(
        'python3 "${GROK_WORKSPACE_ROOT}/scripts/hooks/guard-rationale-size.py"'
    )
    assert wrapped == (
        'python3 "${GROK_WORKSPACE_ROOT}/scripts/hooks/_run_guard.py" '
        '"${GROK_WORKSPACE_ROOT}/scripts/hooks/guard-rationale-size.py"'
    )


def test_wrap_guard_command_fail_mode_closed() -> None:
    module = _generator_module()
    wrapped = module._wrap_guard_command(
        "python3 scripts/hooks/security-guard.py", fail_mode="closed"
    )
    assert wrapped == (
        "python3 scripts/hooks/_run_guard.py --fail-mode=closed"
        " scripts/hooks/security-guard.py"
    )


def test_wrap_guard_command_idempotent() -> None:
    module = _generator_module()
    once = module._wrap_guard_command("python3 scripts/hooks/x.py")
    assert module._wrap_guard_command(once) == once


def test_wrap_guard_command_refuses_non_path_leading_token() -> None:
    # REV-A-002: `uv run <script>` style commands have an interpreter
    # subcommand before the handler path; wrapping would treat `run` as the
    # handler and silently fail open. The transform must refuse instead.
    module = _generator_module()
    assert module._wrap_guard_command("uv run scripts/hooks/x.py") == (
        "uv run scripts/hooks/x.py"
    )


def test_rendered_vscode_commands_all_carry_wrapper_prefix() -> None:
    module = _generator_module()
    rendered = module.render_vscode_hooks_config(_contract_hooks())
    handlers = list(_iter_command_handlers(rendered))
    assert handlers
    for handler in handlers:
        assert handler["command"].startswith(
            "python3 scripts/hooks/_run_guard.py "
        ), handler["command"]


def test_rendered_codex_commands_all_carry_wrapper_prefix() -> None:
    module = _generator_module()
    rendered = module.render_codex_hooks_config(_contract_hooks())
    handlers = list(_iter_command_handlers(rendered))
    assert handlers
    for handler in handlers:
        assert handler["command"].startswith(
            "python3 scripts/hooks/_run_guard.py "
        ), handler["command"]


def test_grok_plugin_hooks_commands_all_carry_wrapper_prefix() -> None:
    module = _generator_module()
    contract = yaml.safe_load(CONTRACT_PATH.read_text())
    payload = json.loads(module._render_grok_plugin_hooks_json(contract))
    entries = payload["hooks"]["PreToolUse"]
    assert entries
    for entry in entries:
        for handler in entry["hooks"]:
            assert 'scripts/hooks/_run_guard.py"' in handler["command"].split()[1], (
                handler["command"]
            )


def test_wrapper_script_shipped_in_payload() -> None:
    assert (PAYLOAD_ROOT / "scripts" / "hooks" / "_run_guard.py").is_file()


# --- contract completeness (hooks previously hand-authored only) --------------


def test_contract_owns_every_golden_hook_command() -> None:
    # The two hooks that were only in the hand-authored golden must now be
    # contract entries so generation is complete: main-branch (edit-matcher
    # guard) and filter-test-output.
    hook_ids = {
        item["id"]
        for stage_entries in _contract_hooks().values()
        for item in stage_entries
    }
    assert "main-branch" in hook_ids
    assert "filter-test-output" in hook_ids
