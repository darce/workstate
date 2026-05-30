from __future__ import annotations

import json
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[2]


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_vscode_matchers_cover_current_altcontext_tool_names() -> None:
    payload = _read_json(PACKAGE_ROOT / ".github" / "hooks" / "terminal-guard.json")
    matchers = [entry.get("matcher", "") for entry in payload["hooks"]["PreToolUse"] + payload["hooks"]["PostToolUse"]]

    assert any("mcp_altcontext-mc_record_event" in matcher for matcher in matchers)
    assert any("mcp_altcontext-mc_close_slice" in matcher for matcher in matchers)
    assert any("mcp_altcontext-mc_get_handoff_state" in matcher for matcher in matchers)
    assert any("mcp_altcontext-mc_load_session" in matcher for matcher in matchers)
    assert any("mcp_altcontext-mc_review_findings" in matcher for matcher in matchers)


def test_vscode_hooks_register_bash_test_output_filter() -> None:
    payload = _read_json(PACKAGE_ROOT / ".github" / "hooks" / "terminal-guard.json")
    entries = payload["hooks"]["PostToolUse"]
    bash_entries = [entry for entry in entries if entry.get("matcher") == "Bash"]

    assert bash_entries, "expected a Bash PostToolUse hook registration for VS Code"
    commands = [entry.get("command", "") for entry in bash_entries]
    assert any("scripts/hooks/filter-test-output.py" in command for command in commands)


def test_vscode_pre_tool_hooks_remove_broad_terminal_guard_and_keep_targeted_guards() -> None:
    payload = _read_json(PACKAGE_ROOT / ".github" / "hooks" / "terminal-guard.json")
    commands = _stage_commands(payload, "PreToolUse")

    assert not any(".github/hooks/terminal-guard.py" in command for command in commands)
    assert any(".github/hooks/guard-worktree-drift.py" in command for command in commands)
    assert any(".github/hooks/guard-main-branch.py" in command for command in commands)
    assert any("scripts/hooks/guard-bash-main-branch.py" in command for command in commands)
    assert any("scripts/hooks/terminal-guard.py" in command for command in commands)
    assert any("scripts/hooks/guard-task-plan-findings.py" in command for command in commands)


def test_vscode_terminal_guard_matcher_is_narrow_until_WORKSTATE53() -> None:
    """Until WORKSTATE-REF-53 implementation note moves the Edit/Write dirty-main hot-path to
    publish/close, the generated VS Code terminal-guard entry must match
    `Bash` only. Broadening to `Bash|run_in_terminal` is owned by follow-up
    ``WORKSTATE-REF-59-FU-broaden-to-run-in-terminal``.

    See the Trajectory Correction (2026-05-15) section of
    ``packages/workstate-system/docs/tasks/WORKSTATE-REF-59-terminal-guard-canonical-port-task-plan.md``
    and ``docs/assessments/consumer-agent-main-worktree-hook-friction-assessment-2026-05-15.md``.
    """
    payload = _read_json(PACKAGE_ROOT / ".github" / "hooks" / "terminal-guard.json")
    entry = next(
        (
            item
            for item in payload["hooks"]["PreToolUse"]
            if item.get("command") == "python3 scripts/hooks/terminal-guard.py"
        ),
        None,
    )

    assert entry is not None
    assert entry.get("matcher") == "Bash", (
        "VS Code terminal-guard matcher must remain `Bash` only until WORKSTATE-REF-53 "
        "implementation note lands; broadening to `Bash|run_in_terminal` is gated on the "
        "Edit/Write dirty-main hot-path moving to publish/close."
    )


def test_vscode_hooks_do_not_register_regenerate_task_views() -> None:
    payload = _read_json(PACKAGE_ROOT / ".github" / "hooks" / "terminal-guard.json")
    post_tool_entries = payload["hooks"]["PostToolUse"]

    commands = [
        hook.get("command", "")
        for entry in post_tool_entries
        for hook in entry.get("hooks", [])
        if isinstance(hook, dict)
    ]

    assert not any("scripts/hooks/regenerate-task-views.sh" in command for command in commands)


def _stage_commands(payload: dict, stage: str) -> list[str]:
    """Collect command strings from both flat and nested-hooks entry shapes."""
    commands: list[str] = []
    for entry in payload["hooks"].get(stage, []):
        if not isinstance(entry, dict):
            continue
        nested = entry.get("hooks")
        if isinstance(nested, list):
            for hook in nested:
                if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                    commands.append(hook["command"])
            continue
        command = entry.get("command")
        if isinstance(command, str):
            commands.append(command)
    return commands


def test_vscode_hooks_register_advise_worktree_cd_for_session_start() -> None:
    payload = _read_json(PACKAGE_ROOT / ".github" / "hooks" / "terminal-guard.json")
    commands = _stage_commands(payload, "SessionStart")
    assert any("scripts/hooks/advise-worktree-cd.py" in command for command in commands), (
        f"expected advise-worktree-cd.py registered in SessionStart; got {commands!r}"
    )


def test_vscode_hooks_register_advise_worktree_cd_for_user_prompt_submit() -> None:
    payload = _read_json(PACKAGE_ROOT / ".github" / "hooks" / "terminal-guard.json")
    commands = _stage_commands(payload, "UserPromptSubmit")
    assert any("scripts/hooks/advise-worktree-cd.py" in command for command in commands), (
        f"expected advise-worktree-cd.py registered in UserPromptSubmit; got {commands!r}"
    )


def test_harness_contract_declares_terminal_guard_policy() -> None:
    """Pin the policy-agnostic terminal_guard contract section preserved under
    WORKSTATE-REF-60 (telemetry budget, fallback spool path, policy version). The
    `allowlist_supplement` extension surface is removed; see
    `test_harness_contract_has_no_allowlist_supplement_field` for the
    absence sentinel.
    """
    import yaml  # type: ignore[import-not-found]

    contract_path = PACKAGE_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    contract = yaml.safe_load(contract_path.read_text())

    section = contract.get("terminal_guard")
    assert isinstance(section, dict)
    assert section.get("policy_version") == "terminal-guard-v1"
    assert section.get("telemetry_write_budget_seconds") == 1.5
    assert section.get("fallback_spool_path") == ".task-state/terminal_guard.jsonl"


def test_harness_contract_has_no_allowlist_supplement_field() -> None:
    """Regression sentinel for WORKSTATE-REF-60 implementation note. Under the inverted (default-
    pass) policy the `terminal_guard.allowlist_supplement` contract field is
    removed end-to-end; if it reappears, the sync validator and this assertion
    must surface it before generated overlays drift.
    """
    import yaml  # type: ignore[import-not-found]

    contract_path = PACKAGE_ROOT / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    raw = contract_path.read_text()
    assert "allowlist_supplement" not in raw, (
        "terminal_guard.allowlist_supplement contract field must be absent under WORKSTATE-REF-60"
    )
    assert "terminal_guard_allowlist.json" not in raw, (
        "no reference to the legacy supplement path may survive in the contract"
    )

    contract = yaml.safe_load(raw)
    section = contract.get("terminal_guard")
    assert isinstance(section, dict)
    assert "allowlist_supplement" not in section
