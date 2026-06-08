from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp import core as handoff_core
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import _get_db_connection


@pytest.fixture()
def isolated_env(tmp_path: Path) -> dict:
    state_dir = tmp_path / ".task-state"
    runtime = RuntimeConfig.for_workspace(tmp_path, state_dir=state_dir)
    mcp_server.configure_runtime(runtime)
    handoff_core.set_handoff_state(
        task_ref="file-touch-task",
        objective="File touch query coverage",
        status="in_progress",
    )
    return {"state_dir": state_dir, "task_ref": "file-touch-task"}


def _parse(payload: str | dict) -> dict:
    raw = json.loads(payload) if isinstance(payload, str) else payload
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        return {**raw, **raw.get("data", {})}
    return raw


def test_record_file_touch_and_get_touched_files_roundtrip(isolated_env: dict) -> None:
    recorded = _parse(
        handoff_core.record_file_touch(
            file_path="packages/mcp-workstate-handoff/src/workstate_handoff_mcp/core.py",
            change_kind="edit",
            session="s1",
        )
    )

    assert recorded["ok"] is True
    touch = recorded["touch"]
    assert touch["task_ref"] == "file-touch-task"
    assert touch["file_path"] == "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/core.py"
    assert touch["change_kind"] == "edit"
    assert touch["session"] == "s1"
    assert recorded["mutation"]["entity"] == "touched_file"
    assert recorded["mutation"]["operation"] == "insert"

    listed = _parse(handoff_core.get_touched_files())

    assert listed["ok"] is True
    assert listed["task_ref"] == "file-touch-task"
    assert listed["returned"] == 1
    assert listed["touches"][0]["file_path"] == "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/core.py"
    assert listed["touches"][0]["change_kind"] == "edit"


def test_get_touched_files_defaults_to_active_task_scope_and_limit(isolated_env: dict) -> None:
    handoff_core.record_file_touch(
        file_path="packages/mcp-workstate-handoff/tests/test_file_touches.py",
        change_kind="add",
        session="s1",
        task_ref="file-touch-task",
    )
    handoff_core.record_file_touch(
        file_path="packages/mcp-workstate-handoff/tests/test_http.py",
        change_kind="edit",
        session="s1",
        task_ref="other-task",
    )
    handoff_core.record_file_touch(
        file_path="packages/mcp-workstate-handoff/tests/test_stdio.py",
        change_kind="edit",
        session="s1",
        task_ref="file-touch-task",
    )

    listed = _parse(handoff_core.get_touched_files(limit=1))

    assert listed["ok"] is True
    assert listed["task_ref"] == "file-touch-task"
    assert listed["total_matching"] == 2
    assert listed["returned"] == 1
    assert listed["has_more"] is True
    assert [row["file_path"] for row in listed["touches"]] == [
        "packages/mcp-workstate-handoff/tests/test_stdio.py",
    ]


@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "/Users/daniel/some/file.py",
        "packages/../../../etc/passwd",
        "foo/bar/../../baz/../../../etc/shadow",
    ],
)
def test_record_file_touch_rejects_non_relative_paths(isolated_env: dict, bad_path: str) -> None:
    result = _parse(
        handoff_core.record_file_touch(
            file_path=bad_path,
            change_kind="edit",
            session="s1",
        )
    )
    assert result["ok"] is False
    assert "monorepo-relative" in result["error"]


def test_record_file_touch_accepts_relative_paths(isolated_env: dict) -> None:
    result = _parse(
        handoff_core.record_file_touch(
            file_path="packages/mcp-workstate-handoff/src/some_file.py",
            change_kind="add",
            session="s1",
        )
    )
    assert result["ok"] is True
    assert result["touch"]["file_path"] == "packages/mcp-workstate-handoff/src/some_file.py"


def test_record_file_touch_attributes_to_caller_cwd_when_no_explicit_actor(
    isolated_env: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-52 implementation note: caller cwd wins; explicit ``WriteActor`` is the opt-out."""
    handoff_core.set_handoff_state(
        task_ref="ft-attr",
        objective="File touch attribution",
        status="in_progress",
        target_branch="feature/task-touch",
        target_worktree_path="/tmp/feature-task-touch",
    )
    with _get_db_connection() as conn:
        conn.execute(
            """
            UPDATE handoff_state
            SET updated_branch = ?, updated_commit_sha = ?
            WHERE task_ref = ?
            """,
            ("feature/task-touch", "touchsha456", "ft-attr"),
        )

    monkeypatch.setattr(handoff_core, "_detect_git_write_context", lambda: ("main", "rootsha999"))

    result = _parse(
        handoff_core.record_file_touch(
            file_path="packages/mcp-workstate-handoff/src/some_file.py",
            change_kind="edit",
            session="s1",
            task_ref="ft-attr",
        )
    )

    assert result["ok"] is True
    assert result["touch"]["branch"] == "main"
    assert result["touch"]["commit_sha"] == "rootsha999"
