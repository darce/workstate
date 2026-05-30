"""WORKSTATE-REF-61 implementation note — contract-driven compaction advisory evaluator.

Asserts the documented envelope shape and warn-and-skip semantics for
``compute_compaction_advisory`` plus the token/char threshold gates.
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


def _write_package_contract(
    workspace: Path,
    *,
    threshold_tokens: int = 70_000,
    threshold_chars: int = 280_000,
) -> Path:
    contract_path = (
        workspace / "packages" / "workstate-system" / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    )
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    contract_path.write_text(
        _CONTRACT_YAML.replace("threshold_tokens: 120000", f"threshold_tokens: {threshold_tokens}").replace(
            "threshold_chars: 500000",
            f"threshold_chars: {threshold_chars}",
        ),
        encoding="utf-8",
    )
    return contract_path


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
        task_ref="WORKSTATE-REF-61",
        objective="Compaction advisory evaluator test fixture.",
        status="in_progress",
        target_branch="feature/WORKSTATE-61-compaction-advisory-evaluator",
    )
    return runtime


def test_advisory_returns_warn_and_skip_envelope_when_contract_missing(
    isolated_runtime: RuntimeConfig,
) -> None:
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={},
    )

    assert advisory["recommended"] is False
    assert advisory["thresholds"] == {"tokens": None, "chars": None}
    assert advisory["observed"] == {"tokens": None, "chars": None}
    assert advisory["harness"] is None
    assert advisory["transcript"] == {"path": None, "source": None}
    assert advisory["latest_compaction_id"] is None
    assert isinstance(advisory["warnings"], list)
    assert any("contract" in w.lower() for w in advisory["warnings"])


def test_advisory_recommends_when_token_total_exceeds_threshold(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("WORKSTATE-REF-61", "advisory-token-fixture", "agent_turn", "claude-code", 200_000),
        )
        conn.commit()

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["recommended"] is True
    assert advisory["thresholds"] == {"tokens": 120000, "chars": 500000}
    assert advisory["observed"]["tokens"] is not None and advisory["observed"]["tokens"] >= 120000
    assert advisory["harness"] == "claude-code"
    assert advisory["transcript"]["source"] == "env_var"
    assert advisory["transcript"]["path"] == str(transcript)
    assert advisory["warnings"] == []


def test_advisory_reports_contract_source_drift_and_record_action(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    root_contract = _write_contract(isolated_runtime.workspace_root)
    package_contract = _write_package_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("WORKSTATE-REF-61", "advisory-contract-source-fixture", "agent_turn", "claude-code", 200_000),
        )
        conn.commit()

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["recommended"] is True
    assert advisory["recommended_action"] == "compaction(operation=record)"
    assert advisory["contract_source"] == {
        "resolved": {
            "path": str(root_contract),
            "thresholds": {"tokens": 120_000, "chars": 500_000},
        },
        "package_reference": {
            "path": str(package_contract),
            "thresholds": {"tokens": 70_000, "chars": 280_000},
        },
        "drift": {
            "detected": True,
            "thresholds": {
                "tokens": {"resolved": 120_000, "package_reference": 70_000},
                "chars": {"resolved": 500_000, "package_reference": 280_000},
            },
        },
    }
    assert any("compaction_contract_drift" in warning for warning in advisory["warnings"])


def test_get_handoff_state_publishes_advisory_when_token_threshold_exceeded(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SESSION_TRANSCRIPT_PATH", str(transcript))

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("WORKSTATE-REF-61", "advisory-envelope-fixture", "agent_turn", "claude-code", 200_000),
        )
        conn.commit()

    envelope = mcp_server.get_handoff_state(task_ref="WORKSTATE-REF-61", sections="identity")

    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["compaction_recommended"] is True
    advisory = data["compaction_advisory"]
    assert advisory["recommended"] is True
    assert advisory["thresholds"] == {"tokens": 120000, "chars": 500000}
    assert advisory["harness"] == "claude-code"
    assert advisory["transcript"]["source"] == "env_var"


def test_load_session_mirrors_compaction_advisory_at_parallel_keys(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 600_000, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SESSION_TRANSCRIPT_PATH", str(transcript))

    envelope = mcp_server.load_session(task_ref="WORKSTATE-REF-61")

    assert envelope["ok"] is True
    data = envelope["data"]
    assert data["compaction_recommended"] is True
    advisory = data["compaction_advisory"]
    assert advisory["recommended"] is True
    assert advisory["harness"] == "claude-code"
    assert data["state"]["compaction_advisory"] == advisory


def test_advisory_recommends_when_transcript_chars_exceed_threshold(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 600_000, encoding="utf-8")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["recommended"] is True
    assert advisory["observed"]["chars"] is not None and advisory["observed"]["chars"] >= 500_000
    assert advisory["harness"] == "claude-code"
    assert advisory["transcript"]["path"] == str(transcript)


def test_current_task_projection_carries_compaction_advisory(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation note: CURRENT_TASK.json (workspace summary) must carry the advisory."""
    import json as _json

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 600_000, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SESSION_TRANSCRIPT_PATH", str(transcript))

    mcp_server.render_handoff(kind="current_task", task_ref="WORKSTATE-REF-61", write_file=True)

    current_task_payload = _json.loads(isolated_runtime.current_task_path.read_text(encoding="utf-8"))

    assert current_task_payload["shape"] == "single"
    active = current_task_payload["active"]
    assert active["task_ref"] == "WORKSTATE-REF-61"
    assert "compaction_advisory" in active, sorted(active)
    advisory = active["compaction_advisory"]
    assert advisory["recommended"] is True
    assert advisory["harness"] == "claude-code"
    assert advisory["thresholds"] == {"tokens": 120000, "chars": 500000}


def test_advisory_discovers_transcript_via_fallback_glob_when_env_vars_unset(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WORKSTATE-REF-61-BR-01: contract fallback_glob must resolve without env vars.

    The WORKSTATE-REF-1 contract documents transcript discovery as env-var first,
    fallback_glob second. When no transcript env var is set but exactly one
    harness's fallback_glob matches a file on disk, the advisory must still
    resolve the harness and surface ``transcript.source == 'fallback_glob'``.
    """

    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    monkeypatch.setenv("HOME", str(tmp_path))
    _write_contract(isolated_runtime.workspace_root)
    fallback_path = tmp_path / ".claude" / "projects" / "proj-alpha" / "transcript-abc.jsonl"
    fallback_path.parent.mkdir(parents=True, exist_ok=True)
    fallback_path.write_text("x" * 600_000, encoding="utf-8")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={},
    )

    assert advisory["harness"] == "claude-code"
    assert advisory["transcript"]["source"] == "fallback_glob"
    assert advisory["transcript"]["path"] == str(fallback_path)
    assert advisory["observed"]["chars"] is not None and advisory["observed"]["chars"] >= 500_000


def test_advisory_does_not_recommend_on_chars_alone_after_recent_compaction(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """WORKSTATE-REF-61-BR-02: char gate must require post-compaction evidence.

    Once a compaction has been recorded for the task, the transcript on disk
    still carries pre-compaction bytes. The char gate must not re-recommend
    solely on that stale length; there must be post-compaction token activity
    (or some other freshness signal) before the advisory recommends again.
    """

    from datetime import datetime, timezone

    from workstate_protocol import StructuredSummary, TurnRange

    from workstate_handoff_mcp.compaction import compute_compaction_advisory
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 600_000, encoding="utf-8")

    structured = StructuredSummary(
        compaction_id="C-WORKSTATE-REF-61-BR-02",
        session_id="br-02-fixture",
        harness="claude-code",
        task_ref="WORKSTATE-REF-61",
        turn_range=TurnRange(start_turn=1, end_turn=10),
        created_at=datetime.now(timezone.utc),
    )
    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO session_compactions
                (compaction_id, session_id, harness, task_ref, turn_range, structured_summary_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "C-WORKSTATE-REF-61-BR-02",
                "br-02-fixture",
                "claude-code",
                "WORKSTATE-REF-61",
                '{"start_turn":1,"end_turn":10}',
                structured.model_dump_json(),
            ),
        )
        conn.commit()

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["latest_compaction_id"] == "C-WORKSTATE-REF-61-BR-02"
    assert advisory["recommended"] is False, f"char gate fired post-compaction without fresh activity: {advisory}"


def test_dashboard_flags_task_when_compaction_recommended(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """implementation note: DASHBOARD.txt's needs-attention block must include the task when
    the advisory recommends compaction."""
    from workstate_handoff_mcp.dashboard_rendering import generate_dashboard_md

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 600_000, encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SESSION_TRANSCRIPT_PATH", str(transcript))

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    markdown = result["markdown"]
    assert "NEEDS ATTENTION" in markdown
    needs_block = markdown.split("NEEDS ATTENTION", 1)[1]
    assert "WORKSTATE-REF-61" in needs_block
    assert "compaction" in needs_block.lower()
    assert "record via compaction(operation=record)" in needs_block


# ---------------------------------------------------------------------------
# WORKSTATE-REF-63: per-deployment threshold overrides
# ---------------------------------------------------------------------------


def test_env_var_overrides_threshold_tokens(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """WORKSTATE_HANDOFF_COMPACTION_THRESHOLD_TOKENS lowers the token gate.

    Contract default is 120000. With the env override at 70000 and observed
    tokens at 90000 (below contract, above override), the advisory must
    recommend compaction and report thresholds_source.tokens == 'env'.
    """
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("WORKSTATE-REF-61", "WORKSTATE63-env-tokens-fixture", "agent_turn", "claude-code", 90_000),
        )
        conn.commit()

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={
            "CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript),
            "WORKSTATE_HANDOFF_COMPACTION_THRESHOLD_TOKENS": "70000",
        },
    )

    assert advisory["recommended"] is True
    assert advisory["thresholds"] == {"tokens": 70000, "chars": 500000}
    assert advisory["thresholds_source"] == {"tokens": "env", "chars": "contract"}
    assert advisory["observed"]["tokens"] == 90_000
    assert advisory["warnings"] == []


def test_env_var_overrides_threshold_chars(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """WORKSTATE_HANDOFF_COMPACTION_THRESHOLD_CHARS lowers the char fallback
    gate, with no deprecation warning."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    # 400000 chars — below contract default 500000 but above override 350000.
    transcript.write_text("x" * 400_000, encoding="utf-8")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={
            "CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript),
            "WORKSTATE_HANDOFF_COMPACTION_THRESHOLD_CHARS": "350000",
        },
    )

    assert advisory["recommended"] is True
    assert advisory["thresholds"] == {"tokens": 120000, "chars": 350000}
    assert advisory["thresholds_source"] == {"tokens": "contract", "chars": "env"}
    assert advisory["warnings"] == []


def _write_overlay(workspace: Path, payload: dict) -> Path:
    import json as _json

    overlay_path = workspace / ".workstate-overlay.json"
    overlay_path.write_text(_json.dumps(payload), encoding="utf-8")
    return overlay_path


def test_overlay_json_overrides_threshold_tokens(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """`.workstate-overlay.json -> compaction.thresholds.tokens` lowers the token gate."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    _write_overlay(isolated_runtime.workspace_root, {"compaction": {"thresholds": {"tokens": 70000}}})
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("WORKSTATE-REF-61", "WORKSTATE63-overlay-tokens-fixture", "agent_turn", "claude-code", 90_000),
        )
        conn.commit()

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["recommended"] is True
    assert advisory["thresholds"] == {"tokens": 70000, "chars": 500000}
    assert advisory["thresholds_source"] == {"tokens": "overlay", "chars": "contract"}


def test_overlay_json_overrides_threshold_chars(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """`.workstate-overlay.json -> compaction.thresholds.chars` lowers the char gate."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    _write_overlay(isolated_runtime.workspace_root, {"compaction": {"thresholds": {"chars": 350000}}})
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("x" * 400_000, encoding="utf-8")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["recommended"] is True
    assert advisory["thresholds"] == {"tokens": 120000, "chars": 350000}
    assert advisory["thresholds_source"] == {"tokens": "contract", "chars": "overlay"}


def test_get_handoff_state_uses_git_workspace_root_for_worktree_overlay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Linked-worktree overlays tune advisory thresholds even with shared state.

    ``RuntimeConfig.for_repo(<linked-worktree>)`` collapses ``workspace_root``
    to the primary checkout for DB sharing, but preserves the linked worktree
    at ``git_workspace_root``. The advisory must load contracts and
    ``.agentic-overlay.json`` from that git worktree, not from the primary DB
    root.
    """
    primary_root = tmp_path / "primary"
    linked_root = tmp_path / "linked"
    primary_root.mkdir()
    linked_root.mkdir()
    state_dir = primary_root / ".task-state"
    state_dir.mkdir(parents=True)
    runtime = RuntimeConfig.for_workspace(
        primary_root,
        state_dir=state_dir,
        current_task_path=primary_root / "CURRENT_TASK.json",
        git_workspace_root=linked_root,
    )
    mcp_server.configure_runtime(runtime)
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-63-WORKTREE-OVERLAY",
        objective="Exercise compaction threshold overlay resolution in a linked worktree.",
        status="in_progress",
        target_branch="feature/WORKSTATE-63-worktree-overlay",
    )
    _write_contract(linked_root)
    _write_overlay(linked_root, {"compaction": {"thresholds": {"tokens": 70000}}})
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")
    monkeypatch.setenv("CLAUDE_SESSION_TRANSCRIPT_PATH", str(transcript))

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "WORKSTATE-REF-63-WORKTREE-OVERLAY",
                "worktree-overlay-fixture",
                "agent_turn",
                "claude-code",
                90_000,
            ),
        )
        conn.commit()

    envelope = mcp_server.get_handoff_state(task_ref="WORKSTATE-REF-63-WORKTREE-OVERLAY", sections="identity")

    assert envelope["ok"] is True
    advisory = envelope["data"]["compaction_advisory"]
    assert advisory["recommended"] is True
    assert advisory["thresholds"] == {"tokens": 70000, "chars": 500000}
    assert advisory["thresholds_source"] == {"tokens": "overlay", "chars": "contract"}
    assert advisory["contract_source"]["resolved"]["path"].startswith(str(linked_root))


def test_env_var_beats_overlay_for_tokens(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """Env > overlay precedence: when both override tokens, env wins."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    _write_overlay(isolated_runtime.workspace_root, {"compaction": {"thresholds": {"tokens": 100000}}})
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO turn_metrics
                (task_ref, session, phase, backend, total_tokens)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("WORKSTATE-REF-61", "WORKSTATE63-precedence-fixture", "agent_turn", "claude-code", 80_000),
        )
        conn.commit()

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={
            "CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript),
            "WORKSTATE_HANDOFF_COMPACTION_THRESHOLD_TOKENS": "70000",
        },
    )

    assert advisory["recommended"] is True
    assert advisory["thresholds"]["tokens"] == 70000
    assert advisory["thresholds_source"]["tokens"] == "env"


def test_invalid_env_override_falls_through_with_warning(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """Non-int env override appends a warning and falls through to contract default."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={
            "CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript),
            "WORKSTATE_HANDOFF_COMPACTION_THRESHOLD_TOKENS": "not-a-number",
        },
    )

    assert advisory["thresholds"]["tokens"] == 120000
    assert advisory["thresholds_source"]["tokens"] == "contract"
    assert any(
        "compaction_threshold_override_invalid" in w
        and "WORKSTATE_HANDOFF_COMPACTION_THRESHOLD_TOKENS=not-a-number" in w
        for w in advisory["warnings"]
    )


def test_invalid_overlay_override_falls_through_with_warning(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
) -> None:
    """Non-int overlay override appends a warning and falls through to contract default."""
    from workstate_handoff_mcp.compaction import compute_compaction_advisory

    _write_contract(isolated_runtime.workspace_root)
    _write_overlay(
        isolated_runtime.workspace_root,
        {"compaction": {"thresholds": {"chars": "bogus"}}},
    )
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("hello\n", encoding="utf-8")

    advisory = compute_compaction_advisory(
        workspace_root=isolated_runtime.workspace_root,
        task_ref="WORKSTATE-REF-61",
        env={"CLAUDE_SESSION_TRANSCRIPT_PATH": str(transcript)},
    )

    assert advisory["thresholds"]["chars"] == 500000
    assert advisory["thresholds_source"]["chars"] == "contract"
    assert any(
        "compaction_threshold_override_invalid" in w and "overlay=compaction.thresholds.chars=bogus" in w
        for w in advisory["warnings"]
    )
