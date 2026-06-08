"""WORKSTATE-REF-67 implementation note — runtime disable surface tests.

Covers the unified ``resolve_compaction_disabled`` resolver, the
``compaction(operation="disable"|"enable"|"status")`` MCP op, the new
``compaction_settings`` table, advisory short-circuit semantics, and the
dashboard "compaction: disabled via <source>" rendering branch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig

_CONTRACT_YAML = """version: 1

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
      fallback_glob: ~/Library/Application Support/Code/User/workspaceStorage/**/*.json
"""


def _write_contract(workspace: Path) -> Path:
    contract_path = workspace / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(_CONTRACT_YAML, encoding="utf-8")
    return contract_path


def _seed_token_overage(task_ref: str) -> None:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            (task_ref, "disable-fixture", "agent_turn", "claude-code", 200_000),
        )
        conn.commit()


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-67",
        objective="Compaction disable fixture.",
        status="in_progress",
        target_branch="feature/WORKSTATE-67",
    )
    return runtime


# --- Resolver: env-var path ----------------------------------------------


def test_disabled_via_env_short_circuits_advisory(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """WORKSTATE_HANDOFF_COMPACTION_DISABLED truthy → disabled=True, source='env'."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    _seed_token_overage("WORKSTATE-REF-67")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-67",
        env={
            "CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript),
            "WORKSTATE_HANDOFF_COMPACTION_DISABLED": "1",
        },
    )

    assert advisory["recommended"] is False
    assert advisory["disabled"] is True
    assert advisory["disabled_source"] == "env"


# --- Resolver: db task-scoped --------------------------------------------


def test_disabled_via_db_task_scoped_short_circuits_advisory(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """compaction(operation='disable', task_ref=...) silences the advisory
    for that task only; other tasks remain enabled."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    _seed_token_overage("WORKSTATE-REF-67")

    mcp_server.compaction({"operation": "disable", "task_ref": "WORKSTATE-REF-67"})

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-67",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["recommended"] is False
    assert advisory["disabled"] is True
    assert advisory["disabled_source"] == "db"


# --- Resolver: db workspace-default --------------------------------------


def test_disabled_via_db_workspace_default_short_circuits_advisory(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """compaction(operation='disable') with no task_ref disables the workspace
    default; the advisory short-circuits for any task without an explicit row.
    """
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    _seed_token_overage("WORKSTATE-REF-67")

    mcp_server.compaction({"operation": "disable"})

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-67",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["recommended"] is False
    assert advisory["disabled"] is True
    assert advisory["disabled_source"] == "db"


# --- Precedence: env beats db --------------------------------------------


def test_env_beats_db_enable(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """If env says disabled and db has an enable row, env still wins
    (source='env')."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    _seed_token_overage("WORKSTATE-REF-67")

    mcp_server.compaction({"operation": "enable"})  # explicit db enable

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-67",
        env={
            "CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript),
            "WORKSTATE_HANDOFF_COMPACTION_DISABLED": "1",
        },
    )

    assert advisory["disabled"] is True
    assert advisory["disabled_source"] == "env"


def test_env_disabled_survives_invalid_floor_env(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WORKSTATE-REF-67-BR-02: unrelated invalid floor settings must not erase
    the explicit env disable override for advisory or status callers.
    """
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    _seed_token_overage("WORKSTATE-REF-67")
    env = {
        "CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript),
        "WORKSTATE_HANDOFF_COMPACTION_DISABLED": "1",
        "WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TURNS": "not-an-int",
    }

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-67",
        env=env,
    )

    assert advisory["recommended"] is False
    assert advisory["disabled"] is True
    assert advisory["disabled_source"] == "env"

    monkeypatch.setenv("WORKSTATE_HANDOFF_COMPACTION_DISABLED", "1")
    monkeypatch.setenv("WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TURNS", "not-an-int")
    status = mcp_server.compaction({"operation": "status", "task_ref": "WORKSTATE-REF-67"})
    assert status["disabled"] is True
    assert status["source"] == "env"
    assert status["env_override"] is True


# --- Schema: workspace-default singleton via UNIQUE index ----------------


def test_two_workspace_default_disables_produce_one_row(
    isolated_runtime: RuntimeConfig,
) -> None:
    """UNIQUE(scope_kind, COALESCE(task_ref,'')) makes the workspace row a
    singleton. Two consecutive disables MUST leave exactly one row."""
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    mcp_server.compaction({"operation": "disable"})
    mcp_server.compaction({"operation": "disable"})

    with _get_db_connection() as conn:
        rows = conn.execute(
            "SELECT scope_kind, task_ref, enabled FROM compaction_settings WHERE scope_kind='workspace'"
        ).fetchall()

    assert len(rows) == 1
    assert rows[0]["scope_kind"] == "workspace"
    assert rows[0]["task_ref"] is None
    assert rows[0]["enabled"] == 0


# --- Status round-trip ---------------------------------------------------


def test_status_roundtrip_shape(
    isolated_runtime: RuntimeConfig,
) -> None:
    """compaction(operation='status') returns the documented receipt shape
    with disabled/source/env_override/db_row keys.
    """
    # Initial state — no env, no db row.
    status = mcp_server.compaction({"operation": "status"})
    assert status["disabled"] is False
    assert status["source"] is None
    assert status["env_override"] is False
    assert status["db_row"] is None

    # Disable via db, then status reflects the row.
    mcp_server.compaction({"operation": "disable"})
    status = mcp_server.compaction({"operation": "status"})
    assert status["disabled"] is True
    assert status["source"] == "db"
    assert status["env_override"] is False
    assert status["db_row"] is not None
    assert status["db_row"]["scope_kind"] == "workspace"
    assert status["db_row"]["enabled"] is False

    # Re-enable via db.
    mcp_server.compaction({"operation": "enable"})
    status = mcp_server.compaction({"operation": "status"})
    assert status["disabled"] is False
    assert status["source"] is None
    assert status["db_row"] is not None
    assert status["db_row"]["enabled"] is True


def test_status_env_override_flag(
    isolated_runtime: RuntimeConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With env disabled and a db enable row, status reports source='env'
    and env_override=True."""
    mcp_server.compaction({"operation": "enable"})
    monkeypatch.setenv("WORKSTATE_HANDOFF_COMPACTION_DISABLED", "1")

    status = mcp_server.compaction({"operation": "status"})

    assert status["disabled"] is True
    assert status["source"] == "env"
    assert status["env_override"] is True
    assert status["db_row"] is not None
    assert status["db_row"]["enabled"] is True


# --- Envelope shape: disabled keys always present ------------------------


def test_envelope_keys_stable_when_enabled(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """The disabled / disabled_source keys are always present on the envelope
    for shape stability; null when enabled."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-67",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert "disabled" in advisory
    assert "disabled_source" in advisory
    assert advisory["disabled"] is False
    assert advisory["disabled_source"] is None


# --- Dashboard rendering -------------------------------------------------


def test_dashboard_renders_disabled_line(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When disabled via env, the dashboard 'Needs Attention' block renders
    'compaction: disabled via env' for the live task."""
    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SESSION_TRANSCRIPT_PATH", str(transcript))
    monkeypatch.setenv("WORKSTATE_HANDOFF_COMPACTION_DISABLED", "1")
    _seed_token_overage("WORKSTATE-REF-67")

    rendered = mcp_server.render_handoff(kind="dashboard", write_file=False)
    markdown = rendered.get("markdown") or ""
    assert "compaction: disabled via env" in markdown, markdown


# --- Identity envelope mirrors disable state -----------------------------


def test_cli_compaction_disable_enable_status_round_trip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """WORKSTATE-REF-67 / PLAN-02: ``mcp-workstate-handoff compaction --operation
    status|disable|enable`` round-trips end-to-end through the CLI."""
    import json as _json
    import sys

    from workstate_handoff_mcp import cli

    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    def _run(argv: list[str]) -> dict:
        original = sys.argv
        sys.argv = argv
        try:
            cli.main()
        finally:
            sys.argv = original
        return _json.loads(capsys.readouterr().out)

    base = [
        "mcp-workstate-handoff",
        "--workspace-root",
        str(tmp_path),
        "--state-dir",
        str(state_dir),
        "compaction",
    ]

    initial = _run(base + ["--operation", "status"])
    initial_data = initial.get("data", initial)
    assert initial_data["disabled"] is False
    assert initial_data["source"] is None

    disabled_envelope = _run(base + ["--operation", "disable"])
    disabled_data = disabled_envelope.get("data", disabled_envelope)
    assert disabled_data["disabled"] is True
    assert disabled_data["source"] == "db"

    enabled_envelope = _run(base + ["--operation", "enable"])
    enabled_data = enabled_envelope.get("data", enabled_envelope)
    assert enabled_data["disabled"] is False
    assert enabled_data["db_row"]["enabled"] is True


def test_get_handoff_state_identity_carries_disabled_state(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SESSION_TRANSCRIPT_PATH", str(transcript))
    monkeypatch.setenv("WORKSTATE_HANDOFF_COMPACTION_DISABLED", "1")
    _seed_token_overage("WORKSTATE-REF-67")

    envelope = mcp_server.get_handoff_state(task_ref="WORKSTATE-REF-67", sections="identity")
    advisory = envelope["data"]["compaction_advisory"]

    assert envelope["data"]["compaction_recommended"] is False
    assert advisory["disabled"] is True
    assert advisory["disabled_source"] == "env"
