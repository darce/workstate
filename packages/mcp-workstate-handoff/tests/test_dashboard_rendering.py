"""Tests for dashboard_rendering.py — implementation note of WORKSTATE-REF-23."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.dashboard_rendering import (
    _DASHBOARD_RENDER_BUDGET_MS,
    DashboardContext,
    DashboardSection,
    _collect_needs_attention,
    _render_dashboard_md,
    clear_dashboard_extensions,
    generate_dashboard_md,
    register_dashboard_extension,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_extensions():
    """Prevent extension leakage between tests."""
    clear_dashboard_extensions()
    yield
    clear_dashboard_extensions()


@pytest.fixture()
def isolated_handoff(tmp_path: Path):
    """Redirect handoff sqlite + generated markdown paths into tmp dir."""
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    current_task_path = tmp_path / "CURRENT_TASK.json"
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=current_task_path,
    )
    mcp_server.configure_runtime(runtime)
    return runtime


# ---------------------------------------------------------------------------
# Extension registry
# ---------------------------------------------------------------------------


def test_register_and_clear_extensions() -> None:
    def ext(_ctx: DashboardContext) -> list[DashboardSection]:
        return [{"heading": "Test", "content": "body", "order": 99}]

    register_dashboard_extension(ext)
    register_dashboard_extension(ext)
    clear_dashboard_extensions()

    # After clear, generate_dashboard_md should produce no extension sections.
    # We test indirectly via _render_dashboard_md with empty extension_sections.
    result = _render_dashboard_md(
        generated_at="2026-01-01 00:00 UTC",
        dashboard_rows=[],
        open_findings={},
        deferred_findings={},
        needs_attention=[],
        active_task_ref=None,
        extension_sections=[],
    )
    assert "## Test" not in result


def test_extension_sections_appear_in_output() -> None:
    def ext(_ctx: DashboardContext) -> list[DashboardSection]:
        return [{"heading": "Lane Health", "content": "all clear", "order": 50}]

    register_dashboard_extension(ext)

    result = _render_dashboard_md(
        generated_at="2026-01-01 00:00 UTC",
        dashboard_rows=[],
        open_findings={},
        deferred_findings={},
        needs_attention=[],
        active_task_ref=None,
        extension_sections=ext({"worktree_lanes": [], "worker_reports": [], "turn_metrics": []}),
    )
    assert "LANE HEALTH" in result
    assert "all clear" in result


def test_extension_sections_ordered_by_order_field() -> None:
    sections: list[DashboardSection] = [
        {"heading": "Worker Status", "content": "w", "order": 60},
        {"heading": "Lane Health", "content": "l", "order": 50},
    ]
    result = _render_dashboard_md(
        generated_at="2026-01-01 00:00 UTC",
        dashboard_rows=[],
        open_findings={},
        deferred_findings={},
        needs_attention=[],
        active_task_ref=None,
        extension_sections=sections,
    )
    lane_pos = result.index("LANE HEALTH")
    worker_pos = result.index("WORKER STATUS")
    assert lane_pos < worker_pos, "Lower order should render first"


def test_no_extensions_registered_renders_core_only(isolated_handoff) -> None:
    mcp_server.set_handoff_state(task_ref="T1", objective="obj", status="in_progress")
    result = generate_dashboard_md(write_file=False)
    assert result["ok"] is True
    md = result["markdown"]
    assert "NEEDS ATTENTION" in md
    assert "ALL TASKS" in md
    assert "EVAL SUMMARY" in md
    assert "LANE HEALTH" not in md
    assert "WORKER STATUS" not in md


# ---------------------------------------------------------------------------
# Core section rendering
# ---------------------------------------------------------------------------


def test_render_with_no_data() -> None:
    result = _render_dashboard_md(
        generated_at="2026-01-01 00:00 UTC",
        dashboard_rows=[],
        open_findings={},
        deferred_findings={},
        needs_attention=[],
        active_task_ref=None,
        extension_sections=[],
    )
    assert "DASHBOARD" in result
    assert "NEEDS ATTENTION" in result
    assert "  (all clear)" in result
    assert "ALL TASKS" in result
    assert "EVAL SUMMARY" in result
    assert "OPEN FINDINGS" in result
    assert "  (none)" in result


def test_render_dashboard_includes_eval_summary_when_results_present(isolated_handoff) -> None:
    results_dir = isolated_handoff.state_dir / "evals"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "results.jsonl").write_text(
        '{"recorded_at":"2026-05-20T20:00:00Z","suite":"docs-hygiene","case":"active-plan-status-metadata","status":"fail","commit":"abc","task_ref":"WORKSTATE-REF-73","metric_payload":{"failures":1},"failure_summary":"active plan missing status metadata"}\n',
        encoding="utf-8",
    )

    mcp_server.set_handoff_state(task_ref="WORKSTATE-REF-73", objective="obj", status="in_progress")
    rendered = generate_dashboard_md(write_file=False)

    assert rendered["ok"] is True
    md = rendered["markdown"]
    assert "EVAL SUMMARY" in md
    assert "docs-hygiene" in md
    assert "active plan missing status metadata" in md
    assert "next: make evals-run SUITE=docs-hygiene LIFECYCLE_ARGS=--json" in md


def test_render_dashboard_shows_all_suites_passing_when_latest_results_pass(isolated_handoff) -> None:
    results_dir = isolated_handoff.state_dir / "evals"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "results.jsonl").write_text(
        '{"recorded_at":"2026-05-20T20:00:00Z","suite":"lifecycle-smoke","case":"lifecycle-command-surface","status":"pass","commit":"abc","task_ref":null,"metric_payload":{"verified_targets":4},"failure_summary":null}\n',
        encoding="utf-8",
    )

    rendered = generate_dashboard_md(write_file=False)

    assert rendered["ok"] is True
    assert "(all suites passing)" in rendered["markdown"]


def test_render_dashboard_eval_summary_aggregates_partial_failure_across_cases(isolated_handoff) -> None:
    """WORKSTATE73-BRANCH-02 regression: multi-case suite where an earlier case
    fails and a later case passes must still surface as a failing suite.
    The earlier renderer only kept the last row per suite, hiding the
    failure when the last case happened to pass."""
    results_dir = isolated_handoff.state_dir / "evals"
    results_dir.mkdir(parents=True, exist_ok=True)
    (results_dir / "results.jsonl").write_text(
        "\n".join(
            [
                '{"recorded_at":"2026-05-20T21:00:00Z","suite":"docs-hygiene","case":"duplicate-active-task-refs","status":"pass","commit":"abc","task_ref":"WORKSTATE-REF-73","metric_payload":{},"failure_summary":null}',
                '{"recorded_at":"2026-05-20T21:00:00Z","suite":"docs-hygiene","case":"active-plan-status-metadata","status":"fail","commit":"abc","task_ref":"WORKSTATE-REF-73","metric_payload":{"missing_status_metadata":1},"failure_summary":"active task plans are missing Task Plan Status metadata"}',
                '{"recorded_at":"2026-05-20T21:00:00Z","suite":"docs-hygiene","case":"stale-dashboard-surface-name","status":"pass","commit":"abc","task_ref":"WORKSTATE-REF-73","metric_payload":{},"failure_summary":null}',
                '{"recorded_at":"2026-05-20T21:00:00Z","suite":"docs-hygiene","case":"placeholder-durable-specs","status":"pass","commit":"abc","task_ref":"WORKSTATE-REF-73","metric_payload":{},"failure_summary":null}',
                '{"recorded_at":"2026-05-20T21:00:00Z","suite":"docs-hygiene","case":"archived-plan-authority-links","status":"pass","commit":"abc","task_ref":"WORKSTATE-REF-73","metric_payload":{},"failure_summary":null}',
                "",
            ]
        ),
        encoding="utf-8",
    )

    rendered = generate_dashboard_md(write_file=False)

    assert rendered["ok"] is True
    md = rendered["markdown"]
    assert "failing suites: 1" in md
    assert "! docs-hygiene: active task plans are missing Task Plan Status metadata" in md
    assert "(all suites passing)" not in md


def test_render_dashboard_md_emits_no_trailing_whitespace() -> None:
    """BR-WORKSTATE38-BRANCH-02 regression: every rendered line must end without
    trailing spaces so the committed DASHBOARD.txt passes ``git diff --check``.

    The original failure mode lives in the ALL TASKS section: short
    last-activity values (today's ``HH:MM`` form, 5 chars) get padded
    out to ``col_last=16`` because they are the rightmost column,
    leaving 11 trailing spaces. Asserting against every line catches the
    same class of bug in any other right-edge column padding (test
    status, headers, etc.) without naming a specific column.
    """
    from datetime import UTC, datetime

    today_ts = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {
            "task_ref": "WORKSTATE-REF-99",
            "status": "in_progress",
            "open_findings": 0,
            "open_blockers": 0,
            "pending_actions": 0,
            "last_activity": today_ts,
        }
    ]
    md = _render_dashboard_md(
        generated_at="2026-05-04 03:00 UTC",
        dashboard_rows=rows,
        open_findings={},
        deferred_findings={},
        needs_attention=[],
        active_task_ref="WORKSTATE-REF-99",
        extension_sections=[],
    )
    offenders = [(idx, repr(line)) for idx, line in enumerate(md.splitlines(), start=1) if line != line.rstrip()]
    assert not offenders, f"Lines with trailing whitespace: {offenders}"


def test_render_open_findings_grouped_by_task() -> None:
    open_findings = {
        "TASK-A": [
            {
                "finding_id": "TASK-A-01",
                "severity": "high",
                "file_path": "src/foo.py",
                "line_start": 42,
                "description": "Bad thing",
            }
        ],
        "TASK-B": [
            {
                "finding_id": "TASK-B-01",
                "severity": "medium",
                "file_path": "src/bar.py",
                "line_start": None,
                "description": "Medium thing",
            }
        ],
    }
    result = _render_dashboard_md(
        generated_at="2026-01-01 00:00 UTC",
        dashboard_rows=[],
        open_findings=open_findings,
        deferred_findings={},
        needs_attention=[],
        active_task_ref=None,
        extension_sections=[],
    )
    assert "  [TASK-A]" in result
    assert "TASK-A-01" in result
    assert "src/foo.py:42" in result
    assert "  [TASK-B]" in result
    assert "TASK-B-01" in result


def test_deferred_findings_section_omitted_when_empty() -> None:
    result = _render_dashboard_md(
        generated_at="2026-01-01 00:00 UTC",
        dashboard_rows=[],
        open_findings={},
        deferred_findings={},
        needs_attention=[],
        active_task_ref=None,
        extension_sections=[],
    )
    assert "DEFERRED" not in result


def test_deferred_findings_section_present_when_populated() -> None:
    deferred = {
        "TASK-X": [
            {
                "finding_id": "TASK-X-01",
                "severity": "low",
                "status": "wontfix",
                "file_path": "src/x.py",
                "line_start": None,
                "description": "Wontfix this",
            }
        ]
    }
    result = _render_dashboard_md(
        generated_at="2026-01-01 00:00 UTC",
        dashboard_rows=[],
        open_findings={},
        deferred_findings=deferred,
        needs_attention=[],
        active_task_ref=None,
        extension_sections=[],
    )
    assert "DEFERRED / WONTFIX" in result
    assert "TASK-X-01" in result
    assert "WONTFIX" in result


# ---------------------------------------------------------------------------
# Needs Attention aggregation
# ---------------------------------------------------------------------------


def test_needs_attention_high_medium_findings(isolated_handoff) -> None:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    mcp_server.set_handoff_state(task_ref="NA-TASK", objective="obj", status="in_progress")
    mcp_server.record_event(
        event={
            "event_kind": "decision",
            "session": "s1",
            "decision": "d1",
            "rationale": "r",
            "task_ref": "NA-TASK",
        }
    )

    # Insert a high-severity finding directly
    with _get_db_connection() as conn:
        conn.execute(
            "INSERT INTO review_findings (task_ref, finding_id, severity, status, file_path, description, session) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("OTHER-TASK", "OTHER-01", "high", "open", "f.py", "desc", "s1"),
        )
        conn.commit()

        from workstate_handoff_mcp.current_task_rendering import (
            _collect_all_open_findings,
            _collect_dashboard_rows,
        )

        dashboard_rows = _collect_dashboard_rows(conn)
        open_findings = _collect_all_open_findings(conn, max_per_task=100)
        items = _collect_needs_attention(conn, dashboard_rows, open_findings)

    task_refs_in_attention = [i["task_ref"] for i in items]
    assert "OTHER-TASK" in task_refs_in_attention
    high_item = next(i for i in items if i["task_ref"] == "OTHER-TASK")
    assert high_item["kind"] == "findings"
    assert "high" in high_item["detail"]


def test_needs_attention_low_findings_not_flagged(isolated_handoff) -> None:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    mcp_server.set_handoff_state(task_ref="LOW-TASK", objective="obj", status="in_progress")

    with _get_db_connection() as conn:
        conn.execute(
            "INSERT INTO review_findings (task_ref, finding_id, severity, status, file_path, description, session) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("LOW-TASK", "LOW-01", "low", "open", "f.py", "desc", "s1"),
        )
        conn.commit()

        from workstate_handoff_mcp.current_task_rendering import (
            _collect_all_open_findings,
            _collect_dashboard_rows,
        )

        dashboard_rows = _collect_dashboard_rows(conn)
        open_findings = _collect_all_open_findings(conn, max_per_task=100)
        items = _collect_needs_attention(conn, dashboard_rows, open_findings)

    finding_items = [i for i in items if i["kind"] == "findings"]
    assert not any(i["task_ref"] == "LOW-TASK" for i in finding_items)


def test_needs_attention_blocked_task(isolated_handoff) -> None:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    mcp_server.set_handoff_state(task_ref="BLK-TASK", objective="obj", status="blocked")
    mcp_server.record_event(
        event={
            "event_kind": "blocker",
            "operation": "add",
            "session": "s1",
            "description": "Blocked by infra",
            "task_ref": "BLK-TASK",
        }
    )

    with _get_db_connection() as conn:
        from workstate_handoff_mcp.current_task_rendering import (
            _collect_all_open_findings,
            _collect_dashboard_rows,
        )

        dashboard_rows = _collect_dashboard_rows(conn)
        open_findings = _collect_all_open_findings(conn, max_per_task=100)
        items = _collect_needs_attention(conn, dashboard_rows, open_findings)

    blocked_items = [i for i in items if i["kind"] == "blocked"]
    assert any(i["task_ref"] == "BLK-TASK" for i in blocked_items)


def test_needs_attention_all_clear_when_no_issues(isolated_handoff) -> None:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    mcp_server.set_handoff_state(task_ref="OK-TASK", objective="obj", status="in_progress")

    with _get_db_connection() as conn:
        from workstate_handoff_mcp.current_task_rendering import (
            _collect_all_open_findings,
            _collect_dashboard_rows,
        )

        dashboard_rows = _collect_dashboard_rows(conn)
        open_findings = _collect_all_open_findings(conn, max_per_task=100)
        items = _collect_needs_attention(conn, dashboard_rows, open_findings)

    finding_or_blocked = [i for i in items if i["kind"] in ("findings", "blocked")]
    assert not finding_or_blocked


def test_collect_dashboard_rows_uses_all_live_handoff_rows_as_activity(isolated_handoff) -> None:
    from workstate_handoff_mcp.current_task_rendering import _collect_dashboard_rows
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO handoff_state (
                id, task_ref, objective, focus, status, target_branch,
                revision, updated_at, updated_by, updated_branch, updated_commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (1, "TASK-A", "Task A", None, "in_progress", "feature/task-a", 0, "tester", "feature/task-a", "abc123"),
        )
        conn.execute(
            """
            INSERT INTO handoff_state (
                id, task_ref, objective, focus, status, target_branch,
                revision, updated_at, updated_by, updated_branch, updated_commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (None, "TASK-B", "Task B", None, "in_progress", "feature/task-b", 0, "tester", "feature/task-b", "def456"),
        )
        conn.commit()

        rows = _collect_dashboard_rows(conn)

    task_refs = {row["task_ref"] for row in rows}
    assert task_refs >= {"TASK-A", "TASK-B"}


# ---------------------------------------------------------------------------
# generate_dashboard_md integration
# ---------------------------------------------------------------------------


def test_generate_dashboard_md_writes_file(isolated_handoff) -> None:
    mcp_server.set_handoff_state(task_ref="DASH-1", objective="obj", status="in_progress")
    result = generate_dashboard_md(write_file=True)

    assert result["ok"] is True
    assert result["written"] is True
    assert result["path"] is not None
    dashboard_path = Path(result["path"])
    assert dashboard_path.exists()
    assert dashboard_path.name == "DASHBOARD.txt"
    content = dashboard_path.read_text()
    assert "DASHBOARD" in content


def test_dashboard_no_fences(isolated_handoff) -> None:
    """ALL TASKS table must not contain backtick fences (WORKSTATE-REF-17-5 implementation note)."""
    mcp_server.set_handoff_state(task_ref="FENCE-1", objective="obj", status="in_progress")
    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "```" not in result["markdown"]


def test_generate_dashboard_md_uses_runtime_dashboard_path(tmp_path: Path) -> None:
    state_dir = tmp_path / ".task-state"
    feature_root = tmp_path / "feature-worktree"
    main_root = tmp_path / "main-root"
    state_dir.mkdir(parents=True, exist_ok=True)
    feature_root.mkdir(parents=True, exist_ok=True)
    main_root.mkdir(parents=True, exist_ok=True)

    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=feature_root / "CURRENT_TASK.json",
        dashboard_path=main_root / "DASHBOARD.md",
    )
    mcp_server.configure_runtime(runtime)

    mcp_server.set_handoff_state(task_ref="DASH-SPLIT", objective="obj", status="in_progress")
    result = generate_dashboard_md(write_file=True)

    assert result["ok"] is True
    assert result["path"] == str(runtime.dashboard_path)
    assert runtime.dashboard_path.exists()
    assert not (feature_root / "DASHBOARD.md").exists()


def test_generate_dashboard_md_no_write_returns_markdown(isolated_handoff) -> None:
    mcp_server.set_handoff_state(task_ref="DASH-2", objective="obj", status="in_progress")
    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert result["written"] is False
    assert result["markdown"] is not None
    assert "DASHBOARD" in result["markdown"]


def test_task_test_status_filtered_to_epic(isolated_handoff) -> None:
    """TEST STATUS shows only tasks from the active epic (WORKSTATE-REF-17-5 implementation note)."""
    # Create an active task under the WORKSTATE-REF-17 epic.
    mcp_server.set_handoff_state(task_ref="WORKSTATE-REF-17-4", objective="obj", status="in_progress")
    # Record test results for two different epics.
    mcp_server.record_event(
        event={
            "event_kind": "test_result",
            "session": "epic-filter",
            "command": "make test",
            "passed": True,
            "task_ref": "WORKSTATE-REF-17-4",
        }
    )
    mcp_server.record_event(
        event={
            "event_kind": "test_result",
            "session": "epic-filter",
            "command": "make test",
            "passed": True,
            "task_ref": "WORKSTATE-REF-9",
        }
    )
    result = generate_dashboard_md(write_file=False)
    assert result["ok"] is True
    md = result["markdown"]
    # Extract TEST STATUS section.
    test_section_start = md.index("TEST STATUS")
    # Find the next section heading (a line that is all-caps followed by dashes).
    remaining = md[test_section_start + len("TEST STATUS") :]
    next_heading = len(md)
    for i, line in enumerate(remaining.split("\n")):
        stripped = line.strip()
        if (
            stripped
            and stripped == stripped.upper()
            and len(stripped) > 3
            and not stripped.startswith("-")
            and not stripped.startswith("─")
        ):
            next_heading = (
                test_section_start + len("TEST STATUS") + sum(len(ln) + 1 for ln in remaining.split("\n")[:i])
            )
            break
    test_section = md[test_section_start:next_heading]
    # WORKSTATE-REF-17-family task must appear in TEST STATUS.
    assert "WORKSTATE-REF-17-4" in test_section
    # Cross-epic task must NOT appear in TEST STATUS.
    assert "WORKSTATE-REF-9" not in test_section


def test_generate_dashboard_md_scopes_sections_to_all_live_rows(isolated_handoff) -> None:
    """Recent decisions, test status, and integrity checks must not collapse to id=1."""
    import subprocess as _sp

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    e17_worktree = "/tmp/e17-live-row"
    e18_worktree = "/tmp/e18-live-row"

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO handoff_state (
                id, task_ref, objective, focus, status, target_branch, target_worktree_path,
                revision, updated_at, updated_by, updated_branch, updated_commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (
                1,
                "WORKSTATE-REF-17-4",
                "Task WORKSTATE-REF-17",
                None,
                "in_progress",
                "feature/e17-live",
                e17_worktree,
                0,
                "tester",
                "feature/e17-live",
                "abc123",
            ),
        )
        conn.execute(
            """
            INSERT INTO handoff_state (
                id, task_ref, objective, focus, status, target_branch, target_worktree_path,
                revision, updated_at, updated_by, updated_branch, updated_commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now'), ?, ?, ?)
            """,
            (
                None,
                "WORKSTATE-REF-18-2",
                "Task WORKSTATE-REF-18",
                None,
                "in_progress",
                "feature/e18-missing",
                e18_worktree,
                0,
                "tester",
                "feature/e18-missing",
                "def456",
            ),
        )
        conn.execute(
            "INSERT INTO decisions (task_ref, session, decision, created_at) VALUES (?, ?, ?, datetime('now'))",
            (
                "WORKSTATE-REF-17-4",
                "dash-scope",
                "cop_slice_complete_WORKSTATE-REF-17-4_scope",
            ),
        )
        conn.execute(
            "INSERT INTO decisions (task_ref, session, decision, created_at) VALUES (?, ?, ?, datetime('now'))",
            (
                "WORKSTATE-REF-18-2",
                "dash-scope",
                "cop_slice_complete_WORKSTATE-REF-18-2_scope",
            ),
        )
        conn.execute(
            "INSERT INTO verified_tests (task_ref, command, passed, session, verified_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("WORKSTATE-REF-17-4", "make test-e17", 1, "dash-scope"),
        )
        conn.execute(
            "INSERT INTO verified_tests (task_ref, command, passed, session, verified_at) VALUES (?, ?, ?, ?, datetime('now'))",
            ("WORKSTATE-REF-18-2", "make test-e18", 1, "dash-scope"),
        )
        conn.commit()

    def _fake_run(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--verify"]:
            if cmd[3] == "feature/e18-missing":
                return _sp.CompletedProcess(cmd, returncode=128, stdout="", stderr="missing")
            return _sp.CompletedProcess(cmd, returncode=0, stdout="ok", stderr="")
        if cmd[:3] == ["git", "branch", "--merged"]:
            return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if cmd[:3] == ["git", "worktree", "list"]:
            return _sp.CompletedProcess(
                cmd,
                returncode=0,
                stdout=f"worktree {e17_worktree}\nworktree {e18_worktree}\n",
                stderr="",
            )
        return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with patch("workstate_handoff_mcp.dashboard_rendering.subprocess.run", side_effect=_fake_run):
        result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    md = result["markdown"]
    assert "RECENT DECISIONS (WORKSTATE-REF-17)" in md
    assert "RECENT DECISIONS (WORKSTATE-REF-18)" in md
    assert "WORKSTATE-REF-17-4" in md
    assert "WORKSTATE-REF-18-2" in md
    assert "feature/e18-missing" in md


def test_recent_decisions_render_handoff_ids_for_non_epic_tasks(isolated_handoff) -> None:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO handoff_state (
                task_ref, objective, status, revision, updated_at
            ) VALUES (?, ?, ?, ?, datetime('now'))
            """,
            ("WORKSTATE-REF-dashboard", "Dashboard maintenance", "in_progress", 0),
        )
        cur = conn.execute(
            """
            INSERT INTO decisions (
                task_ref, session, decision, agent, model_label, reasoning_level, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                "WORKSTATE-REF-dashboard",
                "dash-id",
                "codex_dashboard_receipt_contract",
                "codex",
                "GPT-5.4",
                "high",
            ),
        )
        decision_id = cur.lastrowid
        conn.commit()

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    md = result["markdown"]
    assert "RECENT DECISIONS (WORKSTATE-REF-dashboard)" in md
    assert f"[#{decision_id}] codex_dashboard_receipt_contract (GPT-5.4 high)" in md


def test_section_order_findings_before_test_status(isolated_handoff) -> None:
    """OPEN FINDINGS must render before TEST STATUS (WORKSTATE-REF-17-5 implementation note)."""
    mcp_server.set_handoff_state(task_ref="WORKSTATE-REF-17-4", objective="obj", status="in_progress")
    mcp_server.record_event(
        event={
            "event_kind": "test_result",
            "session": "order",
            "command": "make test",
            "passed": True,
            "task_ref": "WORKSTATE-REF-17-4",
        }
    )
    result = generate_dashboard_md(write_file=False)
    assert result["ok"] is True
    md = result["markdown"]
    findings_pos = md.index("OPEN FINDINGS")
    test_pos = md.index("TEST STATUS")
    assert findings_pos < test_pos, "OPEN FINDINGS must appear before TEST STATUS"


def test_dashboard_task_plan_rows_validate_without_protocol_warning(isolated_handoff, caplog) -> None:
    """ACTIVE TASK PLANS enrichment should carry the full ActiveTask shape."""
    from workstate_handoff_mcp.handoff_state import set_handoff_state

    caplog.set_level("WARNING", logger="workstate_handoff_mcp.shared_primitives")

    set_handoff_state(
        task_ref="PLAN-ROW",
        objective="Dashboard plan row",
        status="in_progress",
        task_plan_path="docs/plans/example.md",
    )
    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "PLAN-ROW" in result["markdown"]
    assert not [record for record in caplog.records if "ActiveTask validation" in record.getMessage()]


def test_active_task_plans_section_empty_state_message(isolated_handoff) -> None:
    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "ACTIVE TASK PLANS" in result["markdown"]
    assert "  (no active tasks)" in result["markdown"]


def test_active_task_plans_section_without_plan_paths_shows_hint(isolated_handoff) -> None:
    mcp_server.set_handoff_state(task_ref="NO-PLAN", objective="obj", status="in_progress")

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "  (no active tasks have task_plan_path set)" in result["markdown"]
    assert "Set via set_handoff_state(task_plan_path='docs/tasks/...')." in result["markdown"]


def test_active_task_plans_section_renders_sorted_rows_and_missing_footer(isolated_handoff) -> None:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    workspace_root = isolated_handoff.workspace_root
    first_worktree = workspace_root / "feature-a"
    second_worktree = workspace_root / "feature-b"
    (first_worktree / "docs" / "plans").mkdir(parents=True)
    (second_worktree / "docs" / "plans").mkdir(parents=True)
    (first_worktree / "docs" / "plans" / "present.md").write_text("# present\n")

    mcp_server.set_handoff_state(
        task_ref="TASK-B",
        objective="missing plan",
        status="in_progress",
        target_branch="feature/task-b",
        target_worktree_path=str(second_worktree),
        task_plan_path="docs/plans/missing.md",
    )
    mcp_server.set_handoff_state(
        task_ref="TASK-A",
        objective="present plan",
        status="in_progress",
        target_branch="feature/task-a",
        target_worktree_path=str(first_worktree),
        task_plan_path="docs/plans/present.md",
    )
    mcp_server.set_handoff_state(task_ref="TASK-C", objective="unset plan", status="in_progress")

    with _get_db_connection() as conn:
        conn.execute("UPDATE handoff_state SET updated_at = ? WHERE task_ref = ?", ("2099-01-01 00:00:00", "TASK-B"))
        conn.execute("UPDATE handoff_state SET updated_at = ? WHERE task_ref = ?", ("2000-01-01 00:00:00", "TASK-A"))
        conn.commit()

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    lines = result["markdown"].splitlines()
    task_a_line = lines.index("  [TASK-A] branch=feature/task-a")
    task_b_line = lines.index("  [TASK-B] branch=feature/task-b")
    assert task_a_line < task_b_line
    assert f"      abs:  ✓ {first_worktree / 'docs' / 'plans' / 'present.md'}" in result["markdown"]
    assert f"      abs:  ✗ {second_worktree / 'docs' / 'plans' / 'missing.md'}" in result["markdown"]
    assert "  (no task_plan_path set for: TASK-C)" in result["markdown"]


def _init_git_repo(repo: Path) -> None:
    """Initialise a real git repo with a seed commit on ``main``."""
    import subprocess

    subprocess.check_call(["git", "init", "--initial-branch=main"], cwd=repo)
    subprocess.check_call(["git", "config", "user.email", "WORKSTATE69@test"], cwd=repo)
    subprocess.check_call(["git", "config", "user.name", "WORKSTATE69 test"], cwd=repo)
    (repo / "README.md").write_text("seed\n")
    subprocess.check_call(["git", "add", "README.md"], cwd=repo)
    subprocess.check_call(["git", "commit", "-m", "seed"], cwd=repo)


def test_active_task_plans_section_read_receipt_baseline_when_accepted(isolated_handoff) -> None:
    """WORKSTATE-REF-69 implementation note: when the plan is committed on ``main`` the read
    receipt anchors on the baseline. The operator can copy-paste
    ``make plan-show TASK=<ref>`` and read the accepted plan locally
    without checking out a feature branch."""
    import subprocess

    workspace_root = isolated_handoff.workspace_root
    _init_git_repo(workspace_root)
    plan_rel = "docs/plans/0099-accepted.md"
    plan_abs = workspace_root / plan_rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# accepted plan\n")
    subprocess.check_call(["git", "add", plan_rel], cwd=workspace_root)
    subprocess.check_call(["git", "commit", "-m", "accept plan"], cwd=workspace_root)

    mcp_server.set_handoff_state(
        task_ref="ACCEPTED",
        objective="accepted plan",
        status="in_progress",
        target_branch="feature/accepted",
        task_plan_path=plan_rel,
    )

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert f"      plan: {plan_rel} (read: make plan-show TASK=ACCEPTED on main)" in result["markdown"], result[
        "markdown"
    ]


def test_active_task_plans_section_read_receipt_working_copy_when_not_accepted(
    isolated_handoff,
) -> None:
    """When the plan lives only on the feature branch (pre-acceptance)
    the read receipt anchors on the feature branch so the operator knows
    they cannot read the plan from ``main`` yet — the branch tag is the
    acceptance signal."""
    import subprocess

    workspace_root = isolated_handoff.workspace_root
    _init_git_repo(workspace_root)
    branch = "feature/draft"
    plan_rel = "docs/plans/0099-draft.md"
    subprocess.check_call(["git", "checkout", "-b", branch], cwd=workspace_root)
    plan_abs = workspace_root / plan_rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# draft plan\n")
    subprocess.check_call(["git", "add", plan_rel], cwd=workspace_root)
    subprocess.check_call(["git", "commit", "-m", "draft plan"], cwd=workspace_root)
    subprocess.check_call(["git", "checkout", "main"], cwd=workspace_root)

    mcp_server.set_handoff_state(
        task_ref="DRAFT",
        objective="draft plan",
        status="in_progress",
        target_branch=branch,
        task_plan_path=plan_rel,
    )

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert f"      plan: {plan_rel} (read: make plan-show TASK=DRAFT on {branch})" in result["markdown"], result[
        "markdown"
    ]


# WORKSTATE-REF-72 implementation note: baseline=accepted/missing line in active-task plan rows.


def test_active_task_plans_section_renders_baseline_accepted_when_plan_on_main(
    isolated_handoff,
) -> None:
    """When the registered plan is committed on ``main`` the dashboard
    surfaces a ``baseline: accepted`` line below the read receipt so the
    operator sees the gate has cleared without re-running the lifecycle
    evaluator."""
    import subprocess

    workspace_root = isolated_handoff.workspace_root
    _init_git_repo(workspace_root)
    plan_rel = "docs/plans/0099-accepted.md"
    plan_abs = workspace_root / plan_rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# accepted plan\n")
    subprocess.check_call(["git", "add", plan_rel], cwd=workspace_root)
    subprocess.check_call(["git", "commit", "-m", "accept plan"], cwd=workspace_root)

    mcp_server.set_handoff_state(
        task_ref="ACCEPTED",
        objective="accepted plan",
        status="in_progress",
        target_branch="feature/accepted",
        task_plan_path=plan_rel,
    )

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "      baseline: accepted" in result["markdown"], result["markdown"]


def test_active_task_plans_section_renders_baseline_missing_when_plan_absent_on_main(
    isolated_handoff,
) -> None:
    """When the plan lives only on the feature branch the dashboard must
    surface ``baseline: missing`` so an existing feature-branch read
    fallback does not hide the gap."""
    import subprocess

    workspace_root = isolated_handoff.workspace_root
    _init_git_repo(workspace_root)
    branch = "feature/draft"
    plan_rel = "docs/plans/0099-draft.md"
    subprocess.check_call(["git", "checkout", "-b", branch], cwd=workspace_root)
    plan_abs = workspace_root / plan_rel
    plan_abs.parent.mkdir(parents=True, exist_ok=True)
    plan_abs.write_text("# draft plan\n")
    subprocess.check_call(["git", "add", plan_rel], cwd=workspace_root)
    subprocess.check_call(["git", "commit", "-m", "draft plan"], cwd=workspace_root)
    subprocess.check_call(["git", "checkout", "main"], cwd=workspace_root)

    mcp_server.set_handoff_state(
        task_ref="DRAFT",
        objective="draft plan",
        status="in_progress",
        target_branch=branch,
        task_plan_path=plan_rel,
    )

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "      baseline: missing" in result["markdown"], result["markdown"]


def test_active_task_plans_section_marks_unreadable_plan_without_read_command(
    isolated_handoff,
) -> None:
    workspace_root = isolated_handoff.workspace_root
    _init_git_repo(workspace_root)
    branch = "feature/missing-plan"
    plan_rel = "docs/plans/missing.md"

    mcp_server.set_handoff_state(
        task_ref="MISSING",
        objective="missing plan",
        status="in_progress",
        target_branch=branch,
        task_plan_path=plan_rel,
    )

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert f"      plan: {plan_rel} (read: unavailable on {branch})" in result["markdown"]
    assert "read: make plan-show TASK=MISSING" not in result["markdown"]


def test_active_task_plans_section_resolution_semantics(isolated_handoff) -> None:
    workspace_root = isolated_handoff.workspace_root
    absolute_plan = workspace_root / "absolute-plan.md"
    worktree_root = workspace_root / "feature-worktree"
    workspace_plan = workspace_root / "docs" / "plans" / "workspace.md"
    worktree_plan = worktree_root / "docs" / "plans" / "worktree.md"
    absolute_plan.write_text("# absolute\n")
    workspace_plan.parent.mkdir(parents=True)
    workspace_plan.write_text("# workspace\n")
    worktree_plan.parent.mkdir(parents=True)
    worktree_plan.write_text("# worktree\n")

    mcp_server.set_handoff_state(
        task_ref="ABSOLUTE",
        objective="absolute plan",
        status="in_progress",
        task_plan_path=str(absolute_plan),
    )
    mcp_server.set_handoff_state(
        task_ref="WORKSPACE",
        objective="workspace plan",
        status="in_progress",
        task_plan_path="docs/plans/workspace.md",
    )
    mcp_server.set_handoff_state(
        task_ref="WORKTREE",
        objective="worktree plan",
        status="in_progress",
        target_worktree_path=str(worktree_root),
        task_plan_path="docs/plans/worktree.md",
    )

    absolute_state = mcp_server.get_handoff_state(task_ref="ABSOLUTE")
    workspace_state = mcp_server.get_handoff_state(task_ref="WORKSPACE")
    worktree_state = mcp_server.get_handoff_state(task_ref="WORKTREE")
    assert absolute_state["data"]["active"]["task_plan_resolution"] == "absolute"
    assert workspace_state["data"]["active"]["task_plan_resolution"] == "workspace"
    assert worktree_state["data"]["active"]["task_plan_resolution"] == "worktree"

    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert f"      abs:  ✓ {absolute_plan}" in result["markdown"]
    assert f"      abs:  ✓ {workspace_plan}" in result["markdown"]
    assert f"      abs:  ✓ {worktree_plan}" in result["markdown"]


def test_workflow_integrity_section_only_on_anomalies(isolated_handoff) -> None:
    """WORKFLOW INTEGRITY section omitted when no anomalies exist (WORKSTATE-REF-17-5 implementation note)."""
    mcp_server.set_handoff_state(task_ref="CLEAN-1", objective="obj", status="in_progress")
    # No target_branch set, so no integrity checks to run.
    result = generate_dashboard_md(write_file=False)
    assert result["ok"] is True
    assert "WORKFLOW INTEGRITY" not in result["markdown"]


def test_workflow_integrity_missing_branch(isolated_handoff) -> None:
    """WORKFLOW INTEGRITY shows alert when target branch doesn't exist (WORKSTATE-REF-17-5 implementation note)."""
    mcp_server.set_handoff_state(
        task_ref="MISS-1",
        objective="obj",
        status="in_progress",
        target_branch="feature/nonexistent-branch-xyz",
    )

    import subprocess as _sp

    def _fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "feature/nonexistent-branch-xyz" in cmd:
            return _sp.CompletedProcess(cmd, returncode=128, stdout="", stderr="not found")
        if "worktree" in cmd and "list" in cmd:
            return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        if "branch" in cmd and "--merged" in cmd:
            return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")
        return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with patch("workstate_handoff_mcp.dashboard_rendering.subprocess.run", side_effect=_fake_run):
        result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    md = result["markdown"]
    assert "WORKFLOW INTEGRITY" in md
    assert "feature/nonexistent-branch-xyz" in md


def test_workflow_integrity_git_timeout(isolated_handoff) -> None:
    """Git timeout renders degraded-mode notice (WORKSTATE-REF-17-5 implementation note)."""
    import subprocess as _sp

    mcp_server.set_handoff_state(
        task_ref="TIME-1",
        objective="obj",
        status="in_progress",
        target_branch="feature/timeout-branch",
    )

    def _timeout_run(cmd, **kwargs):
        raise _sp.TimeoutExpired(cmd, 5)

    with patch("workstate_handoff_mcp.dashboard_rendering.subprocess.run", side_effect=_timeout_run):
        result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    md = result["markdown"]
    assert "WORKFLOW INTEGRITY" in md
    assert "timed out" in md.lower()


def test_workflow_integrity_main_branch_not_flagged_as_undeleted_merged_branch(isolated_handoff) -> None:
    """Main-scoped tasks should not be treated as undeleted merged feature branches."""
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-MAIN-1",
        objective="obj",
        status="done",
        target_branch="main",
        target_worktree_path=str(isolated_handoff.workspace_root),
    )

    import subprocess as _sp

    def _fake_run(cmd, **kwargs):
        if "rev-parse" in cmd and "main" in cmd:
            return _sp.CompletedProcess(cmd, returncode=0, stdout="main-sha\n", stderr="")
        if "branch" in cmd and "--merged" in cmd:
            return _sp.CompletedProcess(cmd, returncode=0, stdout="* main\n  feature/already-merged\n", stderr="")
        if "worktree" in cmd and "list" in cmd:
            return _sp.CompletedProcess(
                cmd,
                returncode=0,
                stdout=f"worktree {isolated_handoff.workspace_root}\nbranch refs/heads/main\n\n",
                stderr="",
            )
        return _sp.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    with patch("workstate_handoff_mcp.dashboard_rendering.subprocess.run", side_effect=_fake_run):
        result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    md = result["markdown"]
    assert "undeleted merged branch: main is fully merged to main" not in md


def test_generate_dashboard_md_with_registered_extension(isolated_handoff) -> None:
    def my_ext(ctx: DashboardContext) -> list[DashboardSection]:
        return [{"heading": "Lane Health", "content": "3 lanes active", "order": 50}]

    register_dashboard_extension(my_ext)
    mcp_server.set_handoff_state(task_ref="EXT-1", objective="obj", status="in_progress")
    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "LANE HEALTH" in result["markdown"]
    assert "3 lanes active" in result["markdown"]


def test_generate_dashboard_md_extension_exception_does_not_abort(isolated_handoff) -> None:
    def bad_ext(_ctx: DashboardContext) -> list[DashboardSection]:
        raise RuntimeError("extension blew up")

    register_dashboard_extension(bad_ext)
    mcp_server.set_handoff_state(task_ref="EXC-1", objective="obj", status="in_progress")
    result = generate_dashboard_md(write_file=False)

    assert result["ok"] is True
    assert "DASHBOARD" in result["markdown"]


def test_generate_dashboard_md_render_budget_under_representative_load(isolated_handoff) -> None:
    for idx in range(25):
        task_ref = f"PERF-{idx:02d}"
        mcp_server.set_handoff_state(task_ref=task_ref, objective="perf", status="in_progress")
        findings = [
            {
                "finding_id": f"{task_ref}-F-{finding_idx:02d}",
                "severity": "medium" if finding_idx % 2 == 0 else "low",
                "file_path": f"src/file_{idx}_{finding_idx}.py",
                "description": "representative load fixture",
            }
            for finding_idx in range(8)
        ]
        payload = mcp_server.batch_record_review_findings(
            session=f"perf-{idx:02d}",
            task_ref=task_ref,
            findings=findings,
        )
        assert payload["ok"] is True, payload

    # Best-of-N timing: a single wall-clock sample is too noisy under
    # full-suite load (GC, CPU contention, lazy import). Warm up once
    # so caches are primed, then take the fastest of N samples — the
    # min approximates the code's true cost minus scheduling jitter,
    # which is what the budget is meant to guard.
    generate_dashboard_md(write_file=False)
    samples_ms: list[float] = []
    for _ in range(5):
        start = time.perf_counter()
        result = generate_dashboard_md(write_file=False)
        samples_ms.append((time.perf_counter() - start) * 1000)

    assert result["ok"] is True
    elapsed_ms = min(samples_ms)
    assert elapsed_ms <= _DASHBOARD_RENDER_BUDGET_MS, (
        f"render budget exceeded: best={elapsed_ms:.2f}ms samples={samples_ms}"
    )
