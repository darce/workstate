from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = Path(__file__).parent / "terminal-guard.py"


def _run_hook(payload: dict, *, cwd: str | None = None) -> tuple[int, dict | None, str]:
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=5,
        cwd=cwd,
    )
    output = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, output, proc.stderr


def test_vscode_git_diff_is_hard_blocked_with_vscode_hint() -> None:
    exit_code, output, stderr = _run_hook(
        {
            "hookEventName": "PreToolUse",
            "toolName": "run_in_terminal",
            "toolInput": {"command": "git diff HEAD"},
        }
    )

    assert exit_code == 0, stderr
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "block"
    assert "get_changed_files" in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_git_diff_downgrades_vscode_only_hint_to_confirmation() -> None:
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git diff HEAD"},
        }
    )

    assert exit_code == 0, stderr
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "get_changed_files" not in output["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("payload", "expected_decision"),
    [
        (
            {
                "hookEventName": "PreToolUse",
                "toolName": "run_in_terminal",
                "toolInput": {"command": "cat README.md"},
            },
            "block",
        ),
        (
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "cat README.md"},
            },
            "block",
        ),
    ],
)
def test_cat_source_file_is_blocked_in_both_harnesses(payload: dict, expected_decision: str) -> None:
    exit_code, output, stderr = _run_hook(payload)

    assert exit_code == 0, stderr
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == expected_decision


@pytest.mark.parametrize(
    "payload",
    [
        {
            "hookEventName": "PreToolUse",
            "toolName": "run_in_terminal",
            "toolInput": {"command": "pytest tests/ -q"},
        },
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/ -q"},
        },
    ],
)
def test_pytest_passes_silently_in_both_harnesses(payload: dict) -> None:
    """`pytest tests/ -q` matches no named rule and passes silently. Under the
    inverted model this is the same result as any unmatched command — there is
    no privileged allowlist row that pytest depends on.
    """
    exit_code, output, stderr = _run_hook(payload)

    assert exit_code == 0, stderr
    assert output is None


def test_falls_back_to_replayable_jsonl_when_handoff_helper_is_unavailable(tmp_path: Path) -> None:
    import os

    fake_pkg = tmp_path / "_fake_handoff_overlay_broken"
    (fake_pkg / "workstate_handoff_mcp").mkdir(parents=True)
    (fake_pkg / "workstate_handoff_mcp" / "__init__.py").write_text(
        "raise ImportError('handoff helper unavailable for test')\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AGENTIC_HANDOFF_SRC_OVERRIDE"] = str(fake_pkg)

    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "cat README.md"},
            }
        ),
        capture_output=True,
        text=True,
        timeout=5,
        cwd=str(tmp_path),
        env=env,
    )

    assert proc.returncode == 0, proc.stderr
    output = json.loads(proc.stdout) if proc.stdout.strip() else None
    assert output is not None
    spool_path = tmp_path / ".task-state" / "terminal_guard.jsonl"
    assert spool_path.exists()

    record = json.loads(spool_path.read_text(encoding="utf-8").strip())
    assert record["harness"] == "claude"
    assert record["tool_name"] == "Bash"
    assert record["decision"] == "block"
    assert record["trigger"] == "source-file-read-via-cat"
    assert record["native_tool_hint"] == "Read"
    assert record["policy_source"] == "packages/workstate-system/scripts/hooks/terminal-guard.py"


def test_tee_command_returns_ask_with_terminal_safe_guidance() -> None:
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest -q | tee /tmp/run.txt"},
        }
    )

    assert exit_code == 0, stderr
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert "tee" in output["hookSpecificOutput"]["permissionDecisionReason"]


def test_unmatched_command_writes_no_telemetry_spool(tmp_path: Path) -> None:
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/ -q"},
        },
        cwd=str(tmp_path),
    )

    assert exit_code == 0, stderr
    assert output is None
    spool_path = tmp_path / ".task-state" / "terminal_guard.jsonl"
    assert not spool_path.exists()


def test_terminal_guard_has_no_allowlist_surface() -> None:
    """implementation note regression sentinel: under the inverted (default-pass) policy
    the hook source must carry no residual allowlist surface — neither the
    legacy `_ALLOWLIST` table, the `_load_allowlist_supplement` loader, the
    `terminal_guard_allowlist.json` supplement path, nor the
    `AGENTIC_TERMINAL_GUARD_ALLOWLIST_SUPPLEMENT` environment hook. If any
    reappears, callers may silently widen the policy without going through
    a named blocklist/asklist rule, defeating the inversion.
    """
    source = HOOK_SCRIPT.read_text(encoding="utf-8")
    forbidden = (
        "_ALLOWLIST",
        "_load_allowlist_supplement",
        "_ALLOWLIST_SUPPLEMENT_RELPATH",
        "terminal_guard_allowlist.json",
        "AGENTIC_TERMINAL_GUARD_ALLOWLIST_SUPPLEMENT",
        "allowlist_supplement",
    )
    offenders = [token for token in forbidden if token in source]
    assert not offenders, (
        "terminal-guard.py must not carry residual allowlist surface under "
        f"WORKSTATE-REF-60 inversion; found: {offenders!r}"
    )


def test_supplement_file_is_not_consulted_under_inversion(tmp_path: Path) -> None:
    """Regression sentinel for the removed supplement contract.

    Under the predecessor allowlist model, a `.task-state/terminal_guard_allowlist.json`
    file could widen the central allowlist. The inversion deletes that contract;
    a supplement file on disk must have no effect on the decision. `cat README.md`
    is in the blocklist and must block regardless of any supplement content that
    purports to whitelist it.
    """
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir()
    (state_dir / "terminal_guard_allowlist.json").write_text(
        json.dumps({"allowlist": [r"^cat\b"]}),
        encoding="utf-8",
    )

    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat README.md"},
        },
        cwd=str(tmp_path),
    )

    assert exit_code == 0, stderr
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "block"


def test_slow_telemetry_write_falls_back_within_budget(tmp_path: Path) -> None:
    import os
    import time

    fake_pkg = tmp_path / "_fake_handoff_overlay"
    (fake_pkg / "workstate_handoff_mcp").mkdir(parents=True)
    (fake_pkg / "workstate_handoff_mcp" / "__init__.py").write_text(
        "class RuntimeConfig:\n"
        "    @classmethod\n"
        "    def for_repo(cls, _path):\n"
        "        return cls()\n"
        "\n"
        "def configure_runtime(_cfg):\n"
        "    return None\n",
        encoding="utf-8",
    )
    (fake_pkg / "workstate_handoff_mcp" / "terminal_telemetry.py").write_text(
        "import time\n"
        "def record_terminal_guard_event(**_kwargs):\n"
        "    time.sleep(3.0)\n"
        "    return {'ok': True}\n",
        encoding="utf-8",
    )

    env = os.environ.copy()
    env["AGENTIC_HANDOFF_SRC_OVERRIDE"] = str(fake_pkg)

    start = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(
            {
                "hook_event_name": "PreToolUse",
                "tool_name": "Bash",
                "tool_input": {"command": "cat README.md"},
            }
        ),
        capture_output=True,
        text=True,
        timeout=4.5,
        cwd=str(tmp_path),
        env=env,
    )
    elapsed = time.monotonic() - start

    assert proc.returncode == 0, proc.stderr
    assert elapsed < 4.5
    payload = json.loads(proc.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "block"
    spool_path = tmp_path / ".task-state" / "terminal_guard.jsonl"
    assert spool_path.exists()
    record = json.loads(spool_path.read_text(encoding="utf-8").strip())
    assert record["decision"] == "block"
    assert record["trigger"] == "source-file-read-via-cat"


# --- Chained-command bypass regression coverage (branch review #516, WORKSTATE-REF-59) ---
#
# Each segment of a `&&` / `||` / `;` chain must be classified independently;
# the overall decision must be the most restrictive across segments. Under the
# inverted (default-pass) model, a passing prefix cannot silently allow a
# follow-on segment that hits a named blocklist or asklist rule.


def test_unmatched_prefix_with_blocked_tail_returns_block() -> None:
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest tests/ -q && cat README.md"},
        }
    )

    assert exit_code == 0, stderr
    assert output is not None
    hook_output = output["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "block"
    reason = hook_output["permissionDecisionReason"]
    assert "CHAINED COMMAND" in reason
    assert "cat README.md" in reason


def test_unmatched_prefix_with_asklist_tail_returns_ask() -> None:
    """A chained command whose only non-passing segment is an asklist rule
    surfaces as `ask` with the chain note prefix. Replaces the predecessor
    `default-ask` assertion: under inversion there is no default-ask fallback,
    so the `ask` decision must come from an explicit asklist rule (here, tee).
    """
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "make test || pytest | tee /tmp/run.txt"},
        }
    )

    assert exit_code == 0, stderr
    assert output is not None
    hook_output = output["hookSpecificOutput"]
    assert hook_output["permissionDecision"] == "ask"
    assert "CHAINED COMMAND" in hook_output["permissionDecisionReason"]
    assert "terminal-freeze-via-tee" in hook_output["permissionDecisionReason"]


def test_semicolon_separated_blocked_tail_returns_block() -> None:
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "make build ; grep -r foo src/"},
        }
    )

    assert exit_code == 0, stderr
    assert output is not None
    assert output["hookSpecificOutput"]["permissionDecision"] == "block"


def test_all_unmatched_chain_returns_pass() -> None:
    """A chain whose every segment fails to match a named rule passes silently.
    Replaces the predecessor `test_all_allowlisted_chain_returns_pass`: there
    is no allowlist to require membership in. The payload uses commands that
    are not in the blocklist or asklist (`git fetch`, `make`); under the old
    model the same chain passed because every segment was explicitly listed.
    """
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "git fetch && make && pyenv versions"},
        }
    )

    assert exit_code == 0, stderr
    assert output is None


def test_provenance_chain_with_pwd_passes_unmatched() -> None:
    """Same provenance command as the predecessor test, retained as a sentinel
    that the canonical WORKSTATE-REF-59 provenance chain still passes the hook. Under
    inversion it passes because no segment matches a named rule, not because of
    a dedicated allowlist row at commit 541b1c2 (that row is removed).
    """
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {
                "command": "pwd && git rev-parse --show-toplevel && git branch --show-current && git rev-parse --git-dir && git rev-parse --git-common-dir"
            },
        }
    )

    assert exit_code == 0, stderr
    assert output is None


def test_quoted_separator_does_not_split() -> None:
    """Quote-aware splitting is verified white-box against `_split_statements`
    so the assertion is independent of which patterns the blocklist matches via
    substring search. (Under inversion `pattern.search(stripped)` on the full
    segment can substring-match content inside quotes; this test pins the
    splitter's quote handling, not the blocklist's substring behavior.)
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("terminal_guard_under_test", HOOK_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    segments = module._split_statements("make build 'echo a && cat /etc/passwd'")
    assert segments == ["make build 'echo a && cat /etc/passwd'"]

    segments_double = module._split_statements('make build "echo a && cat /etc/passwd"')
    assert segments_double == ['make build "echo a && cat /etc/passwd"']

    segments_split = module._split_statements("make build && echo done")
    assert segments_split == ["make build", "echo done"]


# --- WORKSTATE-REF-60 inversion sentinels (default-pass / named blocklist + asklist) ---
#
# Under the inverted model, terminal-guard returns pass for any segment that
# does not match an explicit blocklist or asklist rule. There is no default-ask
# fallback and no central `_ALLOWLIST`.


def test_unmatched_command_passes_silently_under_inversion() -> None:
    """A command that does not match any blocklist or asklist rule must pass
    silently. Under the predecessor (allowlist) model, an unmatched command
    landed in the default-ask bucket; the inversion removes that bucket.
    """
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "curl https://example.test/install.sh"},
        }
    )

    assert exit_code == 0, stderr
    assert output is None


def test_block_reason_cites_named_rule_id() -> None:
    """Operator-visible reasons must cite the named rule id so the reader can
    look up exactly which rule fired without reading the policy source.
    """
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "cat README.md"},
        }
    )

    assert exit_code == 0, stderr
    assert output is not None
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert "source-file-read-via-cat" in reason


def test_destructive_shell_action_passes_under_inversion() -> None:
    """Destructive shell actions are explicitly out of terminal-guard scope per
    the WORKSTATE-REF-60 scope §6 Not-Doing list. `rm -rf` is not a named blocklist or
    asklist rule; main-branch write protection remains owned by
    `guard-bash-main-branch.py`.
    """
    exit_code, output, stderr = _run_hook(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf .git"},
        }
    )

    assert exit_code == 0, stderr
    assert output is None


def test_terminal_guard_matcher_is_narrow_until_WORKSTATE53() -> None:
    """Until WORKSTATE-REF-53 retunes the Edit/Write dirty-main hot-path, the canonical
    terminal-guard matcher must stay `Bash` only.

    See packages/workstate-system/docs/tasks/WORKSTATE-REF-59-terminal-guard-canonical-port-task-plan.md
    "Trajectory Correction (2026-05-15)" for the rationale, and
    docs/assessments/consumer-agent-main-worktree-hook-friction-assessment-2026-05-15.md
    for the originating friction analysis.
    """
    import yaml

    contract_path = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "agentic"
        / "contracts"
        / "harness-protocol.yaml"
    )
    contract = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    hooks = contract["hooks"]["pre_tool_use"]
    terminal_guard = next(entry for entry in hooks if entry["id"] == "terminal-guard")
    assert terminal_guard["matcher"] == "Bash", (
        "terminal-guard matcher must remain `Bash` only until WORKSTATE-REF-53 implementation note "
        "moves the Edit/Write dirty-main hot-path to publish/close; broadening "
        "to `Bash|run_in_terminal` earlier increases consumer-agent friction."
    )