"""implementation note (implementation note / WS-ERRTEL-01): agent_errors schema + record_event error kind.

Covers the explicit write path only: table bootstrap (v12), the
``record_event(event_kind='error')`` variant, write-contract registry
alignment, and redaction/truncation of summary/detail. Server
self-capture, hooks, and harvest are later slices.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import (
    HANDOFF_SCHEMA_VERSION,
    _get_db_connection,
)


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


def _agent_error_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM agent_errors ORDER BY id ASC").fetchall()


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------


def test_agent_errors_schema_has_required_columns(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        columns = _table_columns(conn, "agent_errors")

    assert columns == {
        "id",
        "repo_instance_id",
        "task_ref",
        "harness",
        "error_class",
        "summary",
        "detail",
        "tool_name",
        "command_preview",
        "package_name",
        "package_version",
        "workstate_release",
        "occurrence_count",
        "created_at",
        "last_seen_at",
    }


def test_schema_version_bumped_for_agent_errors(isolated_runtime: RuntimeConfig) -> None:
    assert HANDOFF_SCHEMA_VERSION >= 12
    with _get_db_connection() as conn:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == HANDOFF_SCHEMA_VERSION


def test_warm_start_migration_adds_agent_errors_to_v11_db(isolated_runtime: RuntimeConfig) -> None:
    """A pre-v12 DB (agent_errors dropped, user_version rolled back) is healed."""

    with _get_db_connection() as conn:
        conn.execute("DROP TABLE agent_errors")
        conn.execute(f"PRAGMA user_version = {HANDOFF_SCHEMA_VERSION - 1}")

    # Reopening must re-bootstrap: table back, version current.
    with _get_db_connection() as conn:
        columns = _table_columns(conn, "agent_errors")
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]
    assert "error_class" in columns
    assert user_version == HANDOFF_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# record_event(event_kind='error') accepted path
# ---------------------------------------------------------------------------


def test_record_error_event_inserts_row(isolated_runtime: RuntimeConfig) -> None:
    result = _parse(
        mcp_server.record_event(
            event={
                "event_kind": "error",
                "error_class": "install_drift",
                "summary": ("ImportError: cannot import name 'list_handoff_rows' from 'workstate_handoff_mcp'"),
                "detail": 'Traceback (most recent call last):\n  File "<stdin>", line 8\n',
                "tool_name": "Bash",
                "package_name": "workstate_handoff_mcp",
                "package_version": "0.1.0",
                "task_ref": "WS-ERRTEL-01",
            }
        )
    )
    assert result["ok"] is True

    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["error_class"] == "install_drift"
    assert row["package_name"] == "workstate_handoff_mcp"
    assert row["package_version"] == "0.1.0"
    assert row["task_ref"] == "WS-ERRTEL-01"
    assert row["occurrence_count"] == 1
    assert row["repo_instance_id"]
    assert row["created_at"]
    assert row["last_seen_at"]


def test_record_error_event_minimal_payload(isolated_runtime: RuntimeConfig) -> None:
    result = _parse(
        mcp_server.record_event(
            event={
                "event_kind": "error",
                "error_class": "other",
                "summary": "something workstate-ish failed",
            }
        )
    )
    assert result["ok"] is True
    with _get_db_connection() as conn:
        rows = _agent_error_rows(conn)
    assert len(rows) == 1
    assert rows[0]["task_ref"] is None


# ---------------------------------------------------------------------------
# Rejected payloads
# ---------------------------------------------------------------------------


def test_record_error_event_requires_error_class(isolated_runtime: RuntimeConfig) -> None:
    with pytest.raises(ValidationError):
        mcp_server.record_event(
            event={
                "event_kind": "error",
                "summary": "missing error_class",
            }
        )


def test_record_error_event_rejects_malformed_error_class(isolated_runtime: RuntimeConfig) -> None:
    result = _parse(
        mcp_server.record_event(
            event={
                "event_kind": "error",
                "error_class": "Not A Valid Class!",
                "summary": "bad class string",
            }
        )
    )
    assert result["ok"] is False


# ---------------------------------------------------------------------------
# Redaction + truncation
# ---------------------------------------------------------------------------


def test_record_error_event_redacts_secrets(isolated_runtime: RuntimeConfig) -> None:
    result = _parse(
        mcp_server.record_event(
            event={
                "event_kind": "error",
                "error_class": "cli_failure",
                "summary": "curl --token=supersecret123 failed",
                "detail": "env API_KEY=abcd1234 leaked\nAuthorization: Bearer xyz",
                "command_preview": "curl --token=supersecret123 https://example.test",
            }
        )
    )
    assert result["ok"] is True
    with _get_db_connection() as conn:
        row = _agent_error_rows(conn)[0]
    assert "supersecret123" not in row["summary"]
    assert "[REDACTED]" in row["summary"]
    assert "abcd1234" not in row["detail"]
    assert "xyz" not in row["detail"]
    assert "supersecret123" not in row["command_preview"]


def test_record_error_event_truncates_limits(isolated_runtime: RuntimeConfig) -> None:
    result = _parse(
        mcp_server.record_event(
            event={
                "event_kind": "error",
                "error_class": "other",
                "summary": "s" * 1000,
                "detail": "d" * 10_000,
            }
        )
    )
    assert result["ok"] is True
    with _get_db_connection() as conn:
        row = _agent_error_rows(conn)[0]
    assert len(row["summary"]) <= 256
    assert len(row["detail"]) <= 4096


# ---------------------------------------------------------------------------
# Write-contract registry alignment
# ---------------------------------------------------------------------------


def test_write_contract_registry_has_error_variant() -> None:
    from workstate_handoff_mcp.write_contracts import get_write_contract

    record_event_contract = get_write_contract("record_event")
    assert record_event_contract is not None
    contract = record_event_contract.variants.get("error")
    assert contract is not None
    assert contract.tool_name == "record_event.event_kind=error"
    assert set(contract.required) == {"error_class", "summary"}
    for field in ("detail", "tool_name", "command_preview", "package_name", "package_version", "task_ref"):
        assert field in contract.optional
