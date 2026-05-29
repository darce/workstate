from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from workstate_protocol import StructuredSummary, TurnRange

from workstate_handoff_mcp.config import RuntimeConfig


def _write_contract(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        """version: 1

compaction:
  advisory_field: compaction_recommended
  threshold_tokens: 120000
  threshold_chars: 500000
  unknown_harness: warn_and_skip
  transcript_discovery:
    claude-code:
      env_var: CLAUDE_SESSION_TRANSCRIPT_PATH
      fallback_glob: ~/.claude/projects/**/transcript*.jsonl
    codex:
      env_var: CODEX_SESSION_TRANSCRIPT_PATH
      fallback_glob: ~/.codex/sessions/**/*.jsonl
    vscode:
      env_var: VSCODE_TARGET_SESSION_LOG
      fallback_glob: ~/Library/Application Support/Code/User/workspaceStorage/**/GitHub.copilot-chat/**/*.json
""",
        encoding="utf-8",
    )


def test_load_compaction_contract_uses_overlay_shared_root(tmp_path: Path) -> None:
    from workstate_handoff_mcp.compaction_contract import load_compaction_contract

    RuntimeConfig.for_workspace(tmp_path)
    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_url": "git@example.com:demo/repo.git",
                "remote_ref": "main",
                "remote_sha": "a" * 40,
                "surfaces": {
                    "contracts": {
                        "shared_root": ".agentic/shared",
                        "local_root": ".agentic/local",
                    }
                },
                "configs": [],
            }
        ),
        encoding="utf-8",
    )
    shared_contract = tmp_path / ".agentic" / "shared" / "harness-protocol.yaml"
    _write_contract(shared_contract)

    contract = load_compaction_contract(tmp_path)

    assert contract.contract_path == shared_contract
    assert contract.advisory_field == "compaction_recommended"
    assert contract.transcript_discovery["vscode"].env_var == "VSCODE_TARGET_SESSION_LOG"


def test_load_compaction_contract_prefers_local_over_shared(tmp_path: Path) -> None:
    """WORKSTATE-REF-1-BR3-03: local overlay must win when both contracts exist.

    The orchestrator precedent is local-overrides-shared. A previous
    implementation walked candidates in (shared_root, local_root) order
    and returned the first existing path, which inverted the precedence:
    a local overlay declaring `threshold_tokens: 42` was silently
    shadowed by a shared `threshold_tokens: 120000`. This test pins the
    correct ordering: the loader must return the local contract when
    both files exist.
    """
    from workstate_handoff_mcp.compaction_contract import load_compaction_contract

    RuntimeConfig.for_workspace(tmp_path)
    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_url": "git@example.com:demo/repo.git",
                "remote_ref": "main",
                "remote_sha": "a" * 40,
                "surfaces": {
                    "contracts": {
                        "shared_root": ".agentic/shared",
                        "local_root": ".agentic/local",
                    }
                },
                "configs": [],
            }
        ),
        encoding="utf-8",
    )
    shared_contract = tmp_path / ".agentic" / "shared" / "harness-protocol.yaml"
    _write_contract(shared_contract)
    local_contract = tmp_path / ".agentic" / "local" / "harness-protocol.yaml"
    local_contract.parent.mkdir(parents=True, exist_ok=True)
    local_contract.write_text(
        """version: 1

compaction:
  advisory_field: compaction_recommended
  threshold_tokens: 42
  threshold_chars: 500000
  unknown_harness: warn_and_skip
  transcript_discovery:
    claude-code:
      env_var: CLAUDE_SESSION_TRANSCRIPT_PATH
      fallback_glob: ~/.claude/projects/**/transcript*.jsonl
""",
        encoding="utf-8",
    )

    contract = load_compaction_contract(tmp_path)

    assert contract.contract_path == local_contract, (
        "Local overlay contract must take precedence over the shared contract when both exist; got shared instead."
    )
    assert contract.threshold_tokens == 42, "Local threshold_tokens=42 should override shared 120000."


def test_detect_active_harness_returns_single_matching_env_var(tmp_path: Path) -> None:
    from workstate_handoff_mcp.compaction_contract import detect_active_harness, load_compaction_contract

    RuntimeConfig.for_workspace(tmp_path)
    contract_path = tmp_path / "docs" / "agentic" / "contracts" / "harness-protocol.yaml"
    _write_contract(contract_path)
    contract = load_compaction_contract(tmp_path)

    resolution = detect_active_harness(
        contract,
        env={
            "CODEX_SESSION_TRANSCRIPT_PATH": "/tmp/codex.jsonl",
        },
    )

    assert resolution.harness == "codex"
    assert resolution.env_var == "CODEX_SESSION_TRANSCRIPT_PATH"
    assert resolution.warnings == ()


def test_detect_active_harness_warns_when_no_env_var_matches(tmp_path: Path) -> None:
    from workstate_handoff_mcp.compaction_contract import detect_active_harness, load_compaction_contract

    RuntimeConfig.for_workspace(tmp_path)
    contract_path = tmp_path / "docs" / "agentic" / "contracts" / "harness-protocol.yaml"
    _write_contract(contract_path)
    contract = load_compaction_contract(tmp_path)

    resolution = detect_active_harness(contract, env={})

    assert resolution.harness is None
    assert resolution.env_var is None
    assert resolution.warnings == (
        "No active harness detected from transcript env vars: CLAUDE_SESSION_TRANSCRIPT_PATH, CODEX_SESSION_TRANSCRIPT_PATH, VSCODE_TARGET_SESSION_LOG",
    )


def test_detect_active_harness_warns_when_multiple_env_vars_match(tmp_path: Path) -> None:
    from workstate_handoff_mcp.compaction_contract import detect_active_harness, load_compaction_contract

    RuntimeConfig.for_workspace(tmp_path)
    contract_path = tmp_path / "docs" / "agentic" / "contracts" / "harness-protocol.yaml"
    _write_contract(contract_path)
    contract = load_compaction_contract(tmp_path)

    resolution = detect_active_harness(
        contract,
        env={
            "CLAUDE_SESSION_TRANSCRIPT_PATH": "/tmp/claude.jsonl",
            "VSCODE_TARGET_SESSION_LOG": "/tmp/vscode.json",
        },
    )

    assert resolution.harness is None
    assert resolution.env_var is None
    assert resolution.warnings == ("Multiple active harnesses detected from transcript env vars: claude-code, vscode",)


def test_normalize_compaction_harness_accepts_cursor_alias() -> None:
    from workstate_handoff_mcp.compaction_contract import normalize_compaction_harness

    assert normalize_compaction_harness("cursor") == "vscode"
    assert normalize_compaction_harness(" vscode ") == "vscode"
    assert normalize_compaction_harness("codex") == "codex"


def test_structured_summary_canonicalizes_cursor_alias() -> None:
    summary = StructuredSummary(
        compaction_id="C-WORKSTATE-REF-60-0001",
        session_id="session-123",
        harness="cursor",
        task_ref="WORKSTATE-REF-60",
        turn_range=TurnRange(start_turn=1, end_turn=2),
        created_at=datetime(2026, 5, 15, tzinfo=UTC),
    )

    assert summary.harness == "vscode"
