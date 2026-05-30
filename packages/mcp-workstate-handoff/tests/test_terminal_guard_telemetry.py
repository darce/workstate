from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import _get_db_connection


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> RuntimeConfig:
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=tmp_path / ".task-state",
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def _parse(payload: str | dict) -> dict:
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        data = raw.get("data", {})
        scope = raw.get("scope", {})
        flat = {**raw, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return raw


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_terminal_guard_telemetry_schema_has_required_columns(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        repo_columns = _table_columns(conn, "repo_instances")
        event_columns = _table_columns(conn, "terminal_guard_events")

    assert repo_columns == {
        "repo_instance_id",
        "workspace_root",
        "git_common_dir",
        "created_at",
        "last_seen_at",
    }
    assert event_columns == {
        "event_key",
        "repo_instance_id",
        "task_ref",
        "worktree_path",
        "harness",
        "tool_name",
        "decision",
        "trigger",
        "native_tool_hint",
        "command_preview",
        "policy_version",
        "policy_source",
        "fallback_source",
        "created_at",
    }


def test_terminal_guard_telemetry_schema_enforces_event_key_and_decision(
    isolated_runtime: RuntimeConfig,
) -> None:
    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO repo_instances (repo_instance_id, workspace_root, git_common_dir)
            VALUES (?, ?, ?)
            """,
            ("repo-instance-a", "/tmp/repo", "/tmp/repo/.git"),
        )
        conn.execute(
            """
            INSERT INTO terminal_guard_events (
                event_key, repo_instance_id, task_ref, harness, tool_name, decision,
                trigger, native_tool_hint, command_preview, policy_version, policy_source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "event-key-a",
                "repo-instance-a",
                "WORKSTATE-REF-59",
                "vscode",
                "run_in_terminal",
                "block",
                "source-read",
                "read_file",
                "cat packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py",
                "terminal-guard-v1",
                "packages/workstate-system/scripts/hooks/terminal-guard.py",
            ),
        )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO terminal_guard_events (
                    event_key, repo_instance_id, harness, tool_name, decision, command_preview
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("event-key-a", "repo-instance-a", "vscode", "run_in_terminal", "ask", "grep foo bar"),
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO terminal_guard_events (
                    event_key, repo_instance_id, harness, tool_name, decision, command_preview
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                ("event-key-b", "repo-instance-a", "claude", "Bash", "allow", "echo ok"),
            )


def test_terminal_guard_telemetry_schema_enforces_repo_instance_foreign_key(
    isolated_runtime: RuntimeConfig,
) -> None:
    with _get_db_connection() as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO terminal_guard_events (
                    event_key, repo_instance_id, harness, tool_name, decision, command_preview
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "event-key-missing-repo",
                    "repo-instance-missing",
                    "vscode",
                    "run_in_terminal",
                    "ask",
                    "grep foo bar",
                ),
            )


def test_terminal_guard_telemetry_record_normalizes_preview_and_deduplicates(
    isolated_runtime: RuntimeConfig,
) -> None:
    telemetry = {
        "operation": "record",
        "task_ref": "WORKSTATE-REF-59",
        "worktree_path": "/tmp/feature-worktree",
        "harness": "vscode",
        "tool_name": "run_in_terminal",
        "decision": "block",
        "trigger": "source-read",
        "native_tool_hint": "read_file",
        "command_preview": "python script.py token=abc123 --password=hunter2\necho second line",
        "policy_version": "terminal-guard-v1",
        "policy_source": "packages/workstate-system/scripts/hooks/terminal-guard.py",
        "created_at": "2026-05-14 12:34:56",
    }

    recorded = _parse(mcp_server.terminal_guard_telemetry(telemetry=telemetry))
    replayed = _parse(mcp_server.terminal_guard_telemetry(telemetry=telemetry))

    assert recorded["ok"] is True
    event = recorded["event"]
    assert re.fullmatch(r"[0-9a-f]{64}", event["event_key"])
    assert re.fullmatch(r"[0-9a-f-]{36}", event["repo_instance_id"])
    assert event["command_preview"] == "python script.py token=[REDACTED] --password=[REDACTED]"
    assert event["created_at"] == "2026-05-14 12:34:56"
    assert replayed["event"]["event_key"] == event["event_key"]

    with _get_db_connection() as conn:
        repo_count = conn.execute("SELECT COUNT(*) FROM repo_instances").fetchone()[0]
        event_count = conn.execute("SELECT COUNT(*) FROM terminal_guard_events").fetchone()[0]

    assert repo_count == 1
    assert event_count == 1


def test_terminal_guard_telemetry_list_filters_by_task_ref(isolated_runtime: RuntimeConfig) -> None:
    common = {
        "harness": "vscode",
        "tool_name": "run_in_terminal",
        "decision": "block",
        "trigger": "source-read",
        "command_preview": "grep foo bar",
        "policy_version": "terminal-guard-v1",
        "policy_source": "packages/workstate-system/scripts/hooks/terminal-guard.py",
    }
    _parse(
        mcp_server.terminal_guard_telemetry(
            telemetry={
                "operation": "record",
                "task_ref": "WORKSTATE-REF-59",
                "created_at": "2026-05-14 12:34:56",
                **common,
            }
        )
    )
    _parse(
        mcp_server.terminal_guard_telemetry(
            telemetry={
                "operation": "record",
                "task_ref": "OTHER-1",
                "created_at": "2026-05-14 12:34:57",
                **common,
            }
        )
    )

    listed = _parse(
        mcp_server.terminal_guard_telemetry(
            telemetry={
                "operation": "list",
                "task_ref": "WORKSTATE-REF-59",
                "decision": "block",
                "harness": "vscode",
                "limit": 20,
                "offset": 0,
            }
        )
    )

    assert listed["ok"] is True
    assert listed["returned"] == 1
    assert listed["total_matching"] == 1
    assert listed["events"][0]["task_ref"] == "WORKSTATE-REF-59"


def test_terminal_guard_telemetry_replay_ingests_jsonl_and_deduplicates(
    isolated_runtime: RuntimeConfig,
) -> None:
    spool_path = isolated_runtime.state_dir / "terminal_guard.jsonl"
    spool_path.parent.mkdir(parents=True, exist_ok=True)
    duplicate_event = {
        "task_ref": "WORKSTATE-REF-59",
        "worktree_path": "/tmp/feature-worktree",
        "harness": "vscode",
        "tool_name": "run_in_terminal",
        "decision": "block",
        "trigger": "source-read",
        "native_tool_hint": "read_file",
        "command_preview": "cat secrets.txt token=abc123",
        "policy_version": "terminal-guard-v1",
        "policy_source": "packages/workstate-system/scripts/hooks/terminal-guard.py",
        "created_at": "2026-05-14 12:34:56",
    }
    distinct_event = {
        **duplicate_event,
        "created_at": "2026-05-14 12:34:57",
        "command_preview": "grep foo bar",
    }
    spool_path.write_text(
        "\n".join(
            [
                json.dumps(duplicate_event),
                json.dumps(duplicate_event),
                json.dumps(distinct_event),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    replayed = _parse(
        mcp_server.terminal_guard_telemetry(
            telemetry={
                "operation": "replay",
                "spool_path": str(spool_path),
            }
        )
    )

    assert replayed["ok"] is True
    assert replayed["spool_path"] == str(spool_path)
    assert replayed["processed"] == 3
    assert replayed["ingested"] == 2
    assert replayed["deduped"] == 1
    assert replayed["invalid"] == 0

    with _get_db_connection() as conn:
        rows = conn.execute(
            "SELECT task_ref, fallback_source, command_preview FROM terminal_guard_events ORDER BY created_at ASC, event_key ASC"
        ).fetchall()

    assert len(rows) == 2
    assert all(row["task_ref"] == "WORKSTATE-REF-59" for row in rows)
    assert all(row["fallback_source"] == str(spool_path) for row in rows)
    assert rows[0]["command_preview"] == "cat secrets.txt token=[REDACTED]"
