"""Unit tests for check_harness_sync grok plugin hook parity gate."""

from __future__ import annotations

import json
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from scripts.check_harness_sync import (  # noqa: WORKSTATE-REF-402
    GROK_PLUGIN_HOOKS_PATH,
    _check_grok_plugin_hooks,
    _load_grok_plugin_hook_pairs,
    _load_wrap_guard_command,
)

# implementation note implementation note: emitted hook commands carry the fail-open wrapper prefix;
# fixtures emulate the generator by writing the wrapped form.
_wrap = _load_wrap_guard_command(PACKAGE_ROOT)


def _write_grok_hooks(
    root: Path,
    *,
    pairs: list[tuple[str, str]],
) -> None:
    entries = [
        {
            "matcher": matcher,
            "hooks": [{"type": "command", "command": _wrap(command)}],
        }
        for matcher, command in pairs
    ]
    path = root / GROK_PLUGIN_HOOKS_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"hooks": {"PreToolUse": entries}}, indent=2) + "\n",
        encoding="utf-8",
    )


def _minimal_contract(*rows: dict) -> dict:
    return {"hooks": {"pre_tool_use": list(rows)}}


def test_check_grok_plugin_hooks_passes_when_pairs_match(tmp_path: Path) -> None:
    contract = _minimal_contract(
        {
            "id": "worktree-drift",
            "matcher": "Edit|Write",
            "grok_command": "bash guard.sh",
        }
    )
    _write_grok_hooks(tmp_path, pairs=[("Edit|Write", "bash guard.sh")])
    grok_pairs = _load_grok_plugin_hook_pairs(tmp_path)

    assert _check_grok_plugin_hooks(contract, grok_pairs, repo_root=tmp_path) == []


def test_check_grok_plugin_hooks_errors_on_missing_row(tmp_path: Path) -> None:
    contract = _minimal_contract(
        {
            "id": "worktree-drift",
            "matcher": "Edit|Write",
            "grok_command": "bash guard.sh",
        },
        {
            "id": "bash-main-branch",
            "matcher": "Bash",
            "grok_command": "python3 guard.py",
        },
    )
    _write_grok_hooks(tmp_path, pairs=[("Edit|Write", "bash guard.sh")])
    grok_pairs = _load_grok_plugin_hook_pairs(tmp_path)

    errors = _check_grok_plugin_hooks(contract, grok_pairs, repo_root=tmp_path)
    assert errors == ["missing Grok plugin hook `bash-main-branch` (pre_tool_use)"]


def test_check_grok_plugin_hooks_errors_on_missing_grok_command(tmp_path: Path) -> None:
    # The anchor row keeps the contract grok-aware so the missing-command rule
    # binds (a contract with zero grok fields is treated as legacy and skipped).
    contract = _minimal_contract(
        {"id": "worktree-drift", "matcher": "Edit|Write"},
        {
            "id": "grok-anchor",
            "matcher": "Bash",
            "grok_command": "python3 anchor.py",
        },
    )
    _write_grok_hooks(tmp_path, pairs=[("Bash", "python3 anchor.py")])
    grok_pairs = _load_grok_plugin_hook_pairs(tmp_path)

    errors = _check_grok_plugin_hooks(contract, grok_pairs, repo_root=tmp_path)
    assert errors == ["missing grok_command on pre_tool_use guard `worktree-drift`"]


def test_check_grok_plugin_hooks_skips_legacy_contract_without_grok_fields(
    tmp_path: Path,
) -> None:
    # Mirror of the codex_aware posture: a stale consumer contract that
    # pre-dates grok fields entirely must not hard-fail the sync gate.
    contract = _minimal_contract(
        {"id": "worktree-drift", "matcher": "Edit|Write"},
    )

    errors = _check_grok_plugin_hooks(contract, set(), repo_root=tmp_path)
    assert errors == []


def test_check_grok_plugin_hooks_errors_when_hooks_file_missing(tmp_path: Path) -> None:
    contract = _minimal_contract(
        {
            "id": "worktree-drift",
            "matcher": "Edit|Write",
            "grok_command": "bash guard.sh",
        }
    )

    errors = _check_grok_plugin_hooks(contract, set(), repo_root=tmp_path)
    assert len(errors) == 1
    assert "missing generated grok plugin hooks" in errors[0]