"""Regression coverage for the handoff schema bootstrap and migration paths.

These tests guard against the WORKSTATE-REF-9 silent-migration bug: when a new
ALTER TABLE step is added to ``_apply_handoff_migrations()`` without bumping
``HANDOFF_SCHEMA_VERSION``, the migration is unreachable on every already-
bootstrapped database. The minimal fix is "bump the version", and these
tests prove the fix actually propagates the migration on a stale DB.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp import shared_schema as handoff_schema
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import (
    _HANDOFF_REQUIRED_TABLES,
    HANDOFF_SCHEMA_VERSION,
    _apply_handoff_migrations,
    _get_db_connection,
    _handoff_schema_bootstrapped,
    _has_column,
)


@pytest.fixture()
def isolated_runtime(tmp_path: Path):
    """Point the handoff runtime at an empty tmp dir for each test."""
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def test_fresh_database_lands_at_current_schema_version(isolated_runtime: RuntimeConfig) -> None:
    """A brand-new DB should bootstrap to ``HANDOFF_SCHEMA_VERSION`` with every column present."""
    with _get_db_connection() as conn:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION
        assert _table_exists(conn, "test_traces") is True
        assert _table_exists(conn, "touched_files") is True
        # Spot-check the column whose missing migration motivated WORKSTATE-REF-9.
        assert "target_worktree_path" in _table_columns(conn, "handoff_state")
        assert "target_branch" in _table_columns(conn, "handoff_state")


def test_warm_start_migration_runs_when_version_bumped(isolated_runtime: RuntimeConfig) -> None:
    """Simulate the WORKSTATE-REF-9 bug pre-fix: a DB at v2 missing target_worktree_path.

    After ``_get_db_connection()`` re-opens the file, the warm-start migration
    path must run, the column must reappear, and the user_version must advance
    to ``HANDOFF_SCHEMA_VERSION``. This is the test that would have caught the
    original silent breakage.
    """
    # Bootstrap a fresh DB, then forcibly downgrade it to mimic the broken
    # state every existing checkout was in before WORKSTATE-REF-9.
    with _get_db_connection() as conn:
        conn.execute("ALTER TABLE handoff_state DROP COLUMN target_worktree_path")
        conn.execute("PRAGMA user_version = 2")
        conn.commit()

    # Sanity: confirm we successfully simulated the broken state.
    raw = sqlite3.connect(isolated_runtime.db_path)
    try:
        assert "target_worktree_path" not in _table_columns(raw, "handoff_state")
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 2
    finally:
        raw.close()

    # Re-open via the production code path. The warm-start migration must run.
    with _get_db_connection() as conn:
        assert "target_worktree_path" in _table_columns(conn, "handoff_state"), (
            "warm-start migration did not add target_worktree_path; "
            "_apply_handoff_migrations is unreachable from the warm path"
        )
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION, (
            f"user_version did not advance from 2 to {HANDOFF_SCHEMA_VERSION}; "
            "the bootstrap branch did not write the new sentinel"
        )


def test_warm_start_migration_adds_touched_files_table(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        conn.execute("DROP TABLE touched_files")
        conn.execute("PRAGMA user_version = 3")
        conn.commit()

    raw = sqlite3.connect(isolated_runtime.db_path)
    try:
        assert _table_exists(raw, "touched_files") is False
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 3
    finally:
        raw.close()

    with _get_db_connection() as conn:
        assert _table_exists(conn, "touched_files") is True, (
            "warm-start migration did not restore touched_files; the new schema step is unreachable from the warm path"
        )
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION


def test_warm_start_migration_adds_test_traces_table(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        conn.execute("DROP TABLE test_traces")
        conn.execute("PRAGMA user_version = 5")
        conn.commit()

    raw = sqlite3.connect(isolated_runtime.db_path)
    try:
        assert _table_exists(raw, "test_traces") is False
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 5
    finally:
        raw.close()

    with _get_db_connection() as conn:
        assert _table_exists(conn, "test_traces") is True, (
            "warm-start migration did not restore test_traces; the new schema step is unreachable from the warm path"
        )
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION


def test_required_tables_includes_session_compactions() -> None:
    assert "session_compactions" in _HANDOFF_REQUIRED_TABLES


def test_required_tables_include_terminal_guard_telemetry_tables() -> None:
    assert "repo_instances" in _HANDOFF_REQUIRED_TABLES
    assert "terminal_guard_events" in _HANDOFF_REQUIRED_TABLES


def test_warm_start_migration_adds_session_compactions_table(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS session_compactions")
        conn.execute("PRAGMA user_version = 7")
        conn.commit()

    raw = sqlite3.connect(isolated_runtime.db_path)
    try:
        assert _table_exists(raw, "session_compactions") is False
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 7
    finally:
        raw.close()

    with _get_db_connection() as conn:
        assert _table_exists(conn, "session_compactions") is True, (
            "warm-start migration did not restore session_compactions; "
            "the v7->v8 schema step is unreachable from the warm path"
        )
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION == 11


def test_cold_start_creates_session_compactions_table(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION == 11
        assert _table_exists(conn, "session_compactions") is True


def test_warm_start_migration_adds_terminal_guard_telemetry_tables(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS terminal_guard_events")
        conn.execute("DROP TABLE IF EXISTS repo_instances")
        conn.execute("PRAGMA user_version = 8")
        conn.commit()

    raw = sqlite3.connect(isolated_runtime.db_path)
    try:
        assert _table_exists(raw, "repo_instances") is False
        assert _table_exists(raw, "terminal_guard_events") is False
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 8
    finally:
        raw.close()

    with _get_db_connection() as conn:
        assert _table_exists(conn, "repo_instances") is True, (
            "warm-start migration did not restore repo_instances; "
            "the v8->v9 schema step is unreachable from the warm path"
        )
        assert _table_exists(conn, "terminal_guard_events") is True, (
            "warm-start migration did not restore terminal_guard_events; "
            "the v8->v9 schema step is unreachable from the warm path"
        )
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION == 11


def test_cold_start_creates_terminal_guard_telemetry_tables(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION == 11
        assert _table_exists(conn, "repo_instances") is True
        assert _table_exists(conn, "terminal_guard_events") is True


def test_warm_start_migration_adds_compaction_settings_table(isolated_runtime: RuntimeConfig) -> None:
    """WORKSTATE-REF-67 v9→v10 step adds ``compaction_settings`` on warm restart."""
    with _get_db_connection() as conn:
        conn.execute("DROP TABLE IF EXISTS compaction_settings")
        conn.execute("PRAGMA user_version = 9")
        conn.commit()

    raw = sqlite3.connect(isolated_runtime.db_path)
    try:
        assert _table_exists(raw, "compaction_settings") is False
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 9
    finally:
        raw.close()

    with _get_db_connection() as conn:
        assert _table_exists(conn, "compaction_settings") is True, (
            "warm-start migration did not restore compaction_settings; "
            "the v9->v10 schema step is unreachable from the warm path"
        )
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION == 11


def test_cold_start_creates_compaction_settings_table(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert user_version == HANDOFF_SCHEMA_VERSION == 11
        assert _table_exists(conn, "compaction_settings") is True


def test_repeated_open_on_current_database_is_a_noop(isolated_runtime: RuntimeConfig) -> None:
    """Opening a DB that is already at the target version must not error or rewrite anything."""
    with _get_db_connection() as conn:
        first_version = int(conn.execute("PRAGMA user_version").fetchone()[0])

    # Second open: bootstrap should short-circuit because the schema is current.
    with _get_db_connection() as conn:
        assert _handoff_schema_bootstrapped(conn) is True
        second_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        assert second_version == first_version == HANDOFF_SCHEMA_VERSION


def test_apply_handoff_migrations_is_idempotent(isolated_runtime: RuntimeConfig) -> None:
    """Calling ``_apply_handoff_migrations`` twice in a row must be a safe no-op.

    Idempotency is the property that lets the warm-start path run the
    migration block on every connect when the version is stale, without
    risking duplicate ALTER errors.
    """
    with _get_db_connection() as conn:
        _apply_handoff_migrations(conn)  # second pass; the first happened during bootstrap
        # Every column-add step uses `if not _has_column`, so this should not raise.
        assert _has_column(conn, "handoff_state", "target_worktree_path") is True


def test_handoff_schema_version_documents_target_worktree_path() -> None:
    """Anchor the WORKSTATE-REF-9 fix: the sentinel must be at least 3 (the version that adds target_worktree_path).

    If a future maintainer drops the version back to 2 without removing the
    column from ``_apply_handoff_migrations`` they will reintroduce the bug.
    """
    assert HANDOFF_SCHEMA_VERSION >= 3


def test_v4_handoff_state_migration_preserves_related_rows(isolated_runtime: RuntimeConfig) -> None:
    with _get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO handoff_state (
                id, task_ref, objective, focus, status, target_branch, target_worktree_path, revision
            ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "mig-task-a",
                "Migration task A",
                "Focus A",
                "in_progress",
                "feature/mig-a",
                "/tmp/mig-a",
                2,
            ),
        )
        conn.execute(
            "INSERT INTO decisions (task_ref, session, decision, rationale) VALUES (?, ?, ?, ?)",
            ("mig-task-a", "s-mig", "cop_progress_mig_task_a", "Decision before migration."),
        )
        conn.execute(
            """
            INSERT INTO review_findings (
                finding_id, task_ref, severity, status, file_path, description, session
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("mig-f1", "mig-task-a", "medium", "open", "src/demo.py", "Review finding before migration.", "s-mig"),
        )
        conn.execute(
            """
            INSERT INTO task_archives (
                task_ref, archived_by, notes, snapshot_json
            ) VALUES (?, ?, ?, ?)
            """,
            (
                "mig-archived",
                "pytest",
                "Archive row",
                '{"task_ref":"mig-archived","status":"done"}',
            ),
        )
        conn.execute("ALTER TABLE handoff_state RENAME TO handoff_state_v5_backup")
        conn.execute(
            """
            CREATE TABLE handoff_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                task_ref TEXT NOT NULL,
                objective TEXT,
                focus TEXT,
                status TEXT NOT NULL DEFAULT 'in_progress',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT,
                updated_branch TEXT,
                updated_commit_sha TEXT,
                target_branch TEXT,
                target_worktree_path TEXT,
                revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO handoff_state (
                id, task_ref, objective, focus, status, updated_at,
                updated_by, updated_branch, updated_commit_sha,
                target_branch, target_worktree_path, revision
            )
            SELECT id, task_ref, objective, focus, status, updated_at,
                   updated_by, updated_branch, updated_commit_sha,
                   target_branch, target_worktree_path, revision
            FROM handoff_state_v5_backup
            """
        )
        conn.execute("DROP TABLE handoff_state_v5_backup")
        conn.execute("PRAGMA user_version = 4")
        conn.commit()

    raw = sqlite3.connect(isolated_runtime.db_path)
    try:
        assert int(raw.execute("PRAGMA user_version").fetchone()[0]) == 4
        assert raw.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM review_findings").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM task_archives").fetchone()[0] == 1
    finally:
        raw.close()

    with _get_db_connection() as conn:
        migrated = conn.execute(
            "SELECT id, task_ref, objective, focus, revision FROM handoff_state ORDER BY task_ref"
        ).fetchall()
        assert len(migrated) == 1
        assert migrated[0][0] == 1
        assert migrated[0][1] == "mig-task-a"
        assert migrated[0][2] == "Migration task A"
        assert migrated[0][3] == "Focus A"
        assert migrated[0][4] == 2
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM review_findings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_archives").fetchone()[0] == 1

    create_second = mcp_server.set_handoff_state(
        task_ref="mig-task-b",
        objective="Migration task B",
        status="in_progress",
        expected_revision=0,
    )
    assert create_second["ok"] is True

    with _get_db_connection() as conn:
        rows = conn.execute("SELECT id, task_ref FROM handoff_state ORDER BY task_ref").fetchall()
        assert len(rows) == 2
        # WORKSTATE-REF-17-11: both rows coexist with id = NULL; the v4 migration retains
        # the legacy id = 1 row as-is, so the pre-migration row keeps its id
        # while new inserts use id = NULL.
        assert {row[1] for row in rows} == {"mig-task-a", "mig-task-b"}
        assert [row[1] for row in rows if row[0] is None] == ["mig-task-b"]
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM review_findings").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM task_archives").fetchone()[0] == 1


def test_migrate_add_audit_tables_creates_all_five_tables() -> None:
    """_migrate_add_audit_tables must create session_compactions, touched_files, test_traces, and terminal telemetry tables."""
    from workstate_handoff_mcp.shared_schema import _migrate_add_audit_tables

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        _migrate_add_audit_tables(conn)

        for table in (
            "session_compactions",
            "touched_files",
            "test_traces",
            "repo_instances",
            "terminal_guard_events",
        ):
            assert _table_exists(conn, table), f"{table} must exist after migration"

        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_session_compactions_task_recent'"
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_touched_files_task_touched'"
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_test_traces_test_order'"
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_repo_instances_last_seen_at'"
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_terminal_guard_events_repo_created'"
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_terminal_guard_events_task_created'"
            ).fetchone()
            is not None
        )


def test_migrate_add_audit_tables_is_idempotent() -> None:
    """_migrate_add_audit_tables must be safely callable more than once."""
    from workstate_handoff_mcp.shared_schema import _migrate_add_audit_tables

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        _migrate_add_audit_tables(conn)
        _migrate_add_audit_tables(conn)  # must not raise


def test_migrate_add_column_extensions_adds_columns(isolated_runtime: RuntimeConfig) -> None:
    """_migrate_add_column_extensions must add all expected columns to core tables."""
    from workstate_handoff_mcp.shared_schema import _migrate_add_column_extensions

    with _get_db_connection() as conn:
        conn.execute("ALTER TABLE decisions DROP COLUMN model")
        conn.execute("ALTER TABLE review_findings DROP COLUMN verification_evidence")
        conn.execute("ALTER TABLE lane_messages DROP COLUMN payload_json")
        conn.execute("ALTER TABLE handoff_state DROP COLUMN focus")
        conn.commit()

    with _get_db_connection() as conn:
        _migrate_add_column_extensions(conn)
        assert _has_column(conn, "decisions", "model"), "decisions.model must be restored"
        assert _has_column(conn, "review_findings", "verification_evidence"), (
            "review_findings.verification_evidence must be restored"
        )
        assert _has_column(conn, "lane_messages", "payload_json"), "lane_messages.payload_json must be restored"
        assert _has_column(conn, "handoff_state", "focus"), "handoff_state.focus must be restored"


def test_migrate_add_column_extensions_is_idempotent(isolated_runtime: RuntimeConfig) -> None:
    """_migrate_add_column_extensions must be safely callable on an already-current schema."""
    from workstate_handoff_mcp.shared_schema import _migrate_add_column_extensions

    with _get_db_connection() as conn:
        _migrate_add_column_extensions(conn)  # second pass on an already-current schema
        assert _has_column(conn, "decisions", "model")
        assert _has_column(conn, "review_findings", "verification_evidence")
        assert _has_column(conn, "lane_messages", "payload_json")


def test_migrate_handoff_state_schema_is_idempotent(isolated_runtime: RuntimeConfig) -> None:
    """_migrate_handoff_state_schema must be safely callable on an already-current schema."""
    from workstate_handoff_mcp.shared_schema import (
        _handoff_state_uses_task_keyed_rows,
        _migrate_handoff_state_schema,
    )

    with _get_db_connection() as conn:
        _migrate_handoff_state_schema(conn)  # second pass; schema is already task-keyed
        assert _handoff_state_uses_task_keyed_rows(conn) is True


def test_migrate_handoff_state_schema_converts_legacy_schema(isolated_runtime: RuntimeConfig) -> None:
    """_migrate_handoff_state_schema must convert the legacy id-primary-key schema."""
    from workstate_handoff_mcp.shared_schema import (
        _handoff_state_uses_task_keyed_rows,
        _migrate_handoff_state_schema,
    )

    with _get_db_connection() as conn:
        # Simulate the pre-v4 schema: id is the PK, task_ref is not.
        conn.execute("ALTER TABLE handoff_state RENAME TO handoff_state_legacy_v4")
        conn.execute(
            """
            CREATE TABLE handoff_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                task_ref TEXT NOT NULL,
                objective TEXT,
                focus TEXT,
                status TEXT NOT NULL DEFAULT 'in_progress',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_by TEXT,
                updated_branch TEXT,
                updated_commit_sha TEXT,
                target_branch TEXT,
                target_worktree_path TEXT,
                revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO handoff_state (
                id, task_ref, objective, focus, status,
                target_branch, target_worktree_path, revision
            )
            SELECT CASE WHEN id = 1 THEN 1 ELSE NULL END,
                   task_ref, objective, focus, status,
                   target_branch, target_worktree_path, revision
            FROM handoff_state_legacy_v4
            """
        )
        conn.execute("DROP TABLE handoff_state_legacy_v4")
        assert _handoff_state_uses_task_keyed_rows(conn) is False, "pre-condition: legacy schema must not be task-keyed"

        _migrate_handoff_state_schema(conn)

        assert _handoff_state_uses_task_keyed_rows(conn) is True, (
            "_migrate_handoff_state_schema must convert handoff_state to task_ref primary key"
        )


def test_migrate_add_turn_metrics_creates_table_and_indexes() -> None:
    """_migrate_add_turn_metrics must create the turn_metrics table and both indexes."""
    from workstate_handoff_mcp.shared_schema import _migrate_add_turn_metrics

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        _migrate_add_turn_metrics(conn)

        assert _table_exists(conn, "turn_metrics"), "turn_metrics table must exist"
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_turn_metrics_task_lane_created'"
            ).fetchone()
            is not None
        )
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_turn_metrics_task_backend_model'"
            ).fetchone()
            is not None
        )


def test_migrate_add_turn_metrics_is_idempotent() -> None:
    """_migrate_add_turn_metrics must be safely callable more than once."""
    from workstate_handoff_mcp.shared_schema import _migrate_add_turn_metrics

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        _migrate_add_turn_metrics(conn)
        _migrate_add_turn_metrics(conn)  # must not raise
