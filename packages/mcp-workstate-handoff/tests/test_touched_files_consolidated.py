"""WORKSTATE-REF-45 implementation note: touched_files(operation=...) merge.

Asserts the merged ``touched_files`` MCP tool dispatches to ``record``
and ``list`` operations with the same envelopes the legacy
``record_file_touch`` and ``get_touched_files`` tools produced, and
that the legacy registrations have been removed in favor of the
consolidated entry.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


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
        task_ref="WORKSTATE-REF-DEMO",
        objective="exercise touched_files",
        status="in_progress",
    )
    return runtime


def test_touched_files_operation_record_returns_envelope(isolated_runtime: RuntimeConfig) -> None:
    from workstate_handoff_mcp.api import touched_files

    result = touched_files(
        {
            "operation": "record",
            "file_path": "packages/foo/bar.py",
            "change_kind": "edit",
            "task_ref": "WORKSTATE-REF-DEMO",
        }
    )

    assert result["ok"] is True
    assert result["tool"] == "touched_files"
    assert result["data"]["touch"]["file_path"] == "packages/foo/bar.py"
    assert result["data"]["touch"]["change_kind"] == "edit"


def test_touched_files_operation_list_returns_recorded_rows(isolated_runtime: RuntimeConfig) -> None:
    from workstate_handoff_mcp.api import touched_files

    touched_files(
        {
            "operation": "record",
            "file_path": "packages/foo/a.py",
            "change_kind": "edit",
            "task_ref": "WORKSTATE-REF-DEMO",
        }
    )
    touched_files(
        {
            "operation": "record",
            "file_path": "packages/foo/b.py",
            "change_kind": "add",
            "task_ref": "WORKSTATE-REF-DEMO",
        }
    )

    listed = touched_files({"operation": "list", "task_ref": "WORKSTATE-REF-DEMO"})

    assert listed["ok"] is True
    assert listed["tool"] == "touched_files"
    paths = {row["file_path"] for row in listed["data"]["touches"]}
    assert paths == {"packages/foo/a.py", "packages/foo/b.py"}
    assert listed["data"]["total_matching"] == 2


def test_touched_files_schema_announces_change_kind_enum() -> None:
    """WORKSTATE-REF-84: the MCP input schema announces the closed change_kind set as an enum.

    Agents introspecting the ``touched_files`` record op must see the valid values in
    the schema itself, not discover them only via a runtime error envelope. The enum is
    asserted against ``ChangeKind`` (the single source of truth) so it cannot drift.
    """
    from workstate_handoff_mcp.api import TouchedFilesRecordOp
    from workstate_handoff_mcp.touched_files import ChangeKind

    schema = TouchedFilesRecordOp.model_json_schema()
    enum = schema["properties"]["change_kind"]["enum"]
    assert enum == [kind.value for kind in ChangeKind]
    assert enum == ["edit", "add", "delete"]


def test_touched_files_record_rejects_modified_at_schema_layer(isolated_runtime: RuntimeConfig) -> None:
    """WORKSTATE-REF-84: git's 'modified' is rejected at the Pydantic schema layer, and the error
    names the canonical set so the MCP caller is routed to ``edit`` without guessing."""
    from pydantic import ValidationError

    from workstate_handoff_mcp.api import touched_files

    with pytest.raises(ValidationError) as excinfo:
        touched_files(
            {
                "operation": "record",
                "file_path": "packages/foo/bar.py",
                "change_kind": "modified",
                "task_ref": "WORKSTATE-REF-DEMO",
            }
        )
    message = str(excinfo.value)
    assert "edit" in message and "add" in message and "delete" in message


def test_touched_files_record_rejects_invalid_change_kind(isolated_runtime: RuntimeConfig) -> None:
    """WORKSTATE-REF-84: with change_kind typed as a Literal, an unknown value fails closed at the
    schema layer (ValidationError) before reaching record_file_touch."""
    from pydantic import ValidationError

    from workstate_handoff_mcp.api import touched_files

    with pytest.raises(ValidationError):
        touched_files(
            {
                "operation": "record",
                "file_path": "packages/foo/bar.py",
                "change_kind": "bogus",
                "task_ref": "WORKSTATE-REF-DEMO",
            }
        )


def test_record_file_touch_runtime_guard_still_rejects_modified(isolated_runtime: RuntimeConfig) -> None:
    """WORKSTATE-REF-84: the runtime envelope guard remains the friendly fallback for the
    direct-helper / CLI path where Pydantic schema validation does not run."""
    from workstate_handoff_mcp.touched_files import record_file_touch

    result = record_file_touch(
        file_path="packages/foo/bar.py",
        change_kind="modified",
        task_ref="WORKSTATE-REF-DEMO",
    )
    assert result["ok"] is False
    assert "edit, add, delete" in result["data"]["error"]


def test_change_kind_schema_enum_bound_to_changekind_source_of_truth() -> None:
    """WORKSTATE-REF-84 implementation note drift guard: the announced ``change_kind`` schema enum must stay
    equal to the ``ChangeKind`` source of truth (surfaced as ``CHANGE_KIND_VALUES``).

    The ``TouchedFilesRecordOp.change_kind`` ``Literal`` is a hardcoded constant (``typing.Literal``
    cannot be splatted from an enum), so parity with ``ChangeKind`` is enforced here rather than at
    the type level. Adding a ``ChangeKind`` member without extending the announced ``Literal`` makes
    this fail, so the schema-discoverable set cannot silently drift from the canonical set.
    """
    from workstate_handoff_mcp.api import TouchedFilesRecordOp
    from workstate_handoff_mcp.touched_files import CHANGE_KIND_VALUES, ChangeKind

    schema_enum = TouchedFilesRecordOp.model_json_schema()["properties"]["change_kind"]["enum"]
    assert tuple(schema_enum) == CHANGE_KIND_VALUES
    assert CHANGE_KIND_VALUES == tuple(kind.value for kind in ChangeKind)


def test_registry_replaces_touched_files_split_with_consolidated_tool() -> None:
    from workstate_handoff_mcp.api import _build_tool_registry

    registry = _build_tool_registry()
    names = {entry.name for entry in registry}

    assert "touched_files" in names, "consolidated touched_files tool must be registered"
    assert "record_file_touch" not in names, (
        "legacy record_file_touch must be removed in favor of touched_files(operation='record')"
    )
    assert "get_touched_files" not in names, (
        "legacy get_touched_files must be removed in favor of touched_files(operation='list')"
    )


def test_expected_handoff_tool_count_decremented_for_slice_3() -> None:
    """implementation note dropped the count by exactly 1 (record_file_touch + get_touched_files -> touched_files).

    The live invariant is asserted in the per-slice test file for the most recent
    slice, so this test only enforces a ceiling of 26 (the post-slice-3 value).
    """
    from workstate_handoff_mcp.invariants import EXPECTED_HANDOFF_TOOL_COUNT

    assert EXPECTED_HANDOFF_TOOL_COUNT <= 26
