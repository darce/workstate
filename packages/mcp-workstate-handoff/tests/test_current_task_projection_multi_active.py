"""Per-task projection writer tests (WORKSTATE-REF-54 implementation note).

Establishes the per-task projection contract: every call to
``_write_per_task_projection(task_ref)`` writes
``<state_dir>/current/<task_ref>.json`` atomically with the
``task_projection_schema_version=1`` payload defined in the WORKSTATE-REF-54
plan's *Per-task file payload* section. Wiring of this writer into
``set_handoff_state`` / ``close_slice`` / ``update_task_status`` /
``archive`` lands in subsequent sub-slices.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


def _configure_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    current_task_auto_regen: bool = False,
) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    monkeypatch.delenv("AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN", raising=False)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.txt",
        current_task_auto_regen=current_task_auto_regen,
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def _parse(payload):
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    if isinstance(raw, dict) and raw.get("schema_version") == 2:
        data = raw.get("data", {})
        scope = raw.get("scope", {})
        flat = {**raw, **data}
        if "task_ref" not in flat and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return raw


def test_per_task_projection_dir_property_resolves_under_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _configure_runtime(tmp_path, monkeypatch)

    assert runtime.per_task_projection_dir == runtime.state_dir / "current"


def test_write_per_task_projection_emits_documented_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _configure_runtime(tmp_path, monkeypatch)
    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-FIXTURE",
            objective="Per-task projection writer probe",
            focus="implementation note sub-implementation note.1",
            status="in_progress",
            target_branch="feature/WORKSTATE-54-fixture",
            target_worktree_path=str(tmp_path / "worktree"),
            task_plan_path="packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-fixture.md",
        )
    )
    assert created["ok"] is True
    expected_revision = created["active"]["revision"]

    from workstate_handoff_mcp.current_task_rendering import _write_per_task_projection

    written_path = _write_per_task_projection("WORKSTATE-REF-54-FIXTURE")

    expected_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-FIXTURE.json"
    assert written_path == expected_path
    assert expected_path.is_file()

    payload = json.loads(expected_path.read_text(encoding="utf-8"))
    assert payload["task_projection_schema_version"] == 1
    assert payload["task_ref"] == "WORKSTATE-REF-54-FIXTURE"
    assert payload["status"] == "in_progress"
    assert payload["objective"] == "Per-task projection writer probe"
    assert payload["focus"] == "implementation note sub-implementation note.1"
    assert payload["target_branch"] == "feature/WORKSTATE-54-fixture"
    assert payload["target_worktree_path"] == str(tmp_path / "worktree")
    assert payload["task_plan_path"] == "packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-fixture.md"
    assert payload["revision"] == expected_revision
    assert isinstance(payload["updated_at"], str) and payload["updated_at"]
    assert set(payload.keys()) == {
        "task_projection_schema_version",
        "task_ref",
        "status",
        "objective",
        "focus",
        "target_branch",
        "target_worktree_path",
        "task_plan_path",
        "revision",
        "updated_at",
    }


def test_write_per_task_projection_is_atomic_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runtime = _configure_runtime(tmp_path, monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-ATOMIC",
            objective="Atomic-write probe",
            status="in_progress",
        )
    )
    from workstate_handoff_mcp.current_task_rendering import _write_per_task_projection

    target = _write_per_task_projection("WORKSTATE-REF-54-ATOMIC")
    _write_per_task_projection("WORKSTATE-REF-54-ATOMIC")

    assert target.is_file()
    leftovers = [p for p in runtime.per_task_projection_dir.iterdir() if p.name != "WORKSTATE-REF-54-ATOMIC.json"]
    assert leftovers == [], f"unexpected leftover files: {leftovers}"


def test_set_handoff_state_insert_writes_per_task_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.2: insert path of set_handoff_state must emit the per-task
    projection file unconditionally (not gated by current_task_auto_regen)."""
    runtime = _configure_runtime(tmp_path, monkeypatch)
    assert runtime.current_task_auto_regen is False, "fixture must exercise the default-off path"

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-INSERT",
            objective="Insert-path probe",
            focus="implementation note sub-implementation note.2",
            status="in_progress",
            target_branch="feature/WORKSTATE-54-insert",
            target_worktree_path=str(tmp_path / "worktree"),
            task_plan_path="packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-insert.md",
        )
    )
    assert created["ok"] is True
    expected_revision = created["active"]["revision"]

    expected_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-INSERT.json"
    assert expected_path.is_file(), "set_handoff_state insert must write per-task projection"

    payload = json.loads(expected_path.read_text(encoding="utf-8"))
    assert payload["task_projection_schema_version"] == 1
    assert payload["task_ref"] == "WORKSTATE-REF-54-INSERT"
    assert payload["status"] == "in_progress"
    assert payload["objective"] == "Insert-path probe"
    assert payload["focus"] == "implementation note sub-implementation note.2"
    assert payload["target_branch"] == "feature/WORKSTATE-54-insert"
    assert payload["target_worktree_path"] == str(tmp_path / "worktree")
    assert payload["task_plan_path"] == "packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-insert.md"
    assert payload["revision"] == expected_revision


def test_set_handoff_state_update_refreshes_per_task_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.2: update path of set_handoff_state must refresh the
    per-task projection so its revision tracks the live row."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-UPDATE",
            objective="Update-path probe",
            status="in_progress",
        )
    )
    assert created["ok"] is True
    initial_revision = created["active"]["revision"]

    updated = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-UPDATE",
            focus="moved focus",
            status="blocked",
            expected_revision=initial_revision,
        )
    )
    assert updated["ok"] is True
    new_revision = updated["active"]["revision"]
    assert new_revision == initial_revision + 1

    expected_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-UPDATE.json"
    payload = json.loads(expected_path.read_text(encoding="utf-8"))
    assert payload["revision"] == new_revision
    assert payload["status"] == "blocked"
    assert payload["focus"] == "moved focus"


def test_close_slice_refreshes_per_task_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.3: close_slice must refresh the per-task projection
    so its revision tracks the post-slice live row."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-CLOSE",
            objective="close-slice probe",
            status="in_progress",
        )
    )
    assert created["ok"] is True
    initial_revision = created["active"]["revision"]

    closed = _parse(
        mcp_server.close_slice(
            session="WORKSTATE-54-sub-1.3",
            decision="WORKSTATE_slice_complete_WORKSTATE-54_sub_1_3_probe",
            rationale=(
                "## Changes\nProbe slice for sub-implementation note.3 wiring.\n\n"
                "## Verification\nTest assertion only.\n\n"
                "## Schema / Contract Changes\nNone.\n\n"
                "## Open Threads\nNone.\n"
            ),
            task_ref="WORKSTATE-REF-54-CLOSE",
            expected_revision=initial_revision,
        )
    )
    assert closed["ok"] is True
    new_revision = closed.get("task_revision") or closed.get("active", {}).get("revision")
    assert new_revision is not None and new_revision > initial_revision

    payload = json.loads((runtime.per_task_projection_dir / "WORKSTATE-REF-54-CLOSE.json").read_text(encoding="utf-8"))
    assert payload["revision"] == new_revision
    assert payload["status"] == "in_progress"


def test_update_task_status_refreshes_per_task_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.3: update_task_status (active path) must refresh the
    per-task projection so its status field tracks the live row."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-STATUS",
            objective="status probe",
            status="in_progress",
        )
    )
    initial_revision = created["active"]["revision"]

    updated = _parse(
        mcp_server.update_task_status(
            task_ref="WORKSTATE-REF-54-STATUS",
            status="blocked",
            expected_revision=initial_revision,
        )
    )
    assert updated["ok"] is True

    payload = json.loads((runtime.per_task_projection_dir / "WORKSTATE-REF-54-STATUS.json").read_text(encoding="utf-8"))
    assert payload["status"] == "blocked"
    assert payload["revision"] > initial_revision


def test_archive_reaps_per_task_projection(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.3: archive must remove the per-task projection file
    once the active row is gone (CTP-WORKSTATE-REF-T-07 orphan prevention)."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-ARCH",
            objective="archive probe",
            status="in_progress",
        )
    )
    target_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-ARCH.json"
    assert target_path.is_file(), "precondition: per-task file written by set_handoff_state"

    archived = _parse(mcp_server.archive(payload={"operation": "archive", "task_ref": "WORKSTATE-REF-54-ARCH"}))
    assert archived["ok"] is True
    assert not target_path.exists(), "archive must reap the per-task projection file"


def test_render_handoff_current_task_refreshes_per_task_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.4: render_handoff(kind='current_task') must emit the
    per-task projection alongside CURRENT_TASK.json so an explicit
    snapshot request also keeps the per-task directory live."""
    runtime = _configure_runtime(tmp_path, monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-RENDER",
            objective="render-handoff probe",
            status="in_progress",
        )
    )
    target_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-RENDER.json"
    target_path.unlink()  # erase the file written during set_handoff_state to isolate the render path
    assert not target_path.exists()

    rendered = mcp_server.render_handoff(kind="current_task", task_ref="WORKSTATE-REF-54-RENDER")
    assert rendered.get("ok") is True

    assert target_path.is_file(), "render_handoff(current_task) must write the per-task projection"
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    assert payload["task_projection_schema_version"] == 1
    assert payload["task_ref"] == "WORKSTATE-REF-54-RENDER"


def test_render_handoff_current_task_skip_when_write_file_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.4: write_file=False must NOT emit the per-task
    projection (caller explicitly opted out of disk I/O)."""
    runtime = _configure_runtime(tmp_path, monkeypatch)
    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-RENDER-NOWRITE",
            objective="render-handoff no-write probe",
            status="in_progress",
        )
    )
    target_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-RENDER-NOWRITE.json"
    target_path.unlink()
    assert not target_path.exists()

    rendered = mcp_server.render_handoff(kind="current_task", task_ref="WORKSTATE-REF-54-RENDER-NOWRITE", write_file=False)
    assert rendered.get("ok") is True
    assert not target_path.exists(), "write_file=False must skip per-task projection write"


@pytest.mark.parametrize("auto_regen", [False, True], ids=["auto_regen_off", "auto_regen_on"])
@pytest.mark.parametrize(
    "entry_point",
    ["set_handoff_state", "close_slice", "update_task_status", "archive"],
)
def test_per_task_writer_invocation_matrix_is_unconditional(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_regen: bool,
    entry_point: str,
) -> None:
    """Sub-implementation note.6 (CTP-PR-T3-01): all four DB-write entry points must
    keep the per-task projection live regardless of
    ``current_task_auto_regen``. The flag was re-scoped by implementation note to
    control only the workspace-summary eager-write; per-task files are
    unconditional on every task-affecting DB write.

    Total matrix: 4 entry points × 2 flag values = 8 cases.
    """
    runtime = _configure_runtime(tmp_path, monkeypatch, current_task_auto_regen=auto_regen)
    assert runtime.current_task_auto_regen is auto_regen

    task_ref = f"WORKSTATE-REF-54-MATRIX-{entry_point.upper()}-{int(auto_regen)}"

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref=task_ref,
            objective=f"matrix probe ({entry_point}, auto_regen={auto_regen})",
            status="in_progress",
        )
    )
    assert created["ok"] is True
    revision_after_create = created["active"]["revision"]
    target_path = runtime.per_task_projection_dir / f"{task_ref}.json"

    if entry_point == "set_handoff_state":
        assert target_path.is_file()
        return

    target_path.unlink()
    assert not target_path.exists(), "fixture precondition: erase the file written by initial set_handoff_state"

    if entry_point == "close_slice":
        result = _parse(
            mcp_server.close_slice(
                session=f"WORKSTATE-54-matrix-{auto_regen}",
                decision=f"WORKSTATE_slice_complete_WORKSTATE-54_matrix_{entry_point}_{int(auto_regen)}",
                rationale=(
                    "## Changes\nMatrix probe.\n\n"
                    "## Verification\nTest assertion only.\n\n"
                    "## Schema / Contract Changes\nNone.\n\n"
                    "## Open Threads\nNone.\n"
                ),
                task_ref=task_ref,
                expected_revision=revision_after_create,
            )
        )
    elif entry_point == "update_task_status":
        result = _parse(
            mcp_server.update_task_status(
                task_ref=task_ref,
                status="blocked",
                expected_revision=revision_after_create,
            )
        )
    elif entry_point == "archive":
        result = _parse(mcp_server.archive(payload={"operation": "archive", "task_ref": task_ref}))
    else:  # pragma: no cover - parametrize covers all branches
        raise AssertionError(f"unhandled entry point {entry_point!r}")

    assert result.get("ok") is True

    if entry_point == "archive":
        # Archive REAPS rather than writes. The unconditional contract
        # for archive is "no orphan file remains" rather than "file is
        # rewritten" — see sub-implementation note.3.
        assert not target_path.exists(), (
            f"archive must reap the per-task file regardless of current_task_auto_regen={auto_regen}"
        )
    else:
        assert target_path.is_file(), (
            f"{entry_point} must refresh per-task projection regardless of current_task_auto_regen={auto_regen}"
        )


def test_per_task_writer_concurrent_set_handoff_state_no_partial_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.6: 100-iteration sequential write loop on the same
    task_ref must leave exactly one canonical file behind and no
    leftover .tmp files (atomic-replace contract per implementation note Proof).
    """
    runtime = _configure_runtime(tmp_path, monkeypatch)

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-RACE",
            objective="race probe",
            status="in_progress",
        )
    )
    revision = created["active"]["revision"]

    for _ in range(100):
        result = _parse(
            mcp_server.set_handoff_state(
                task_ref="WORKSTATE-REF-54-RACE",
                status="in_progress",
                expected_revision=revision,
            )
        )
        assert result["ok"] is True
        revision = result["active"]["revision"]

    canonical = runtime.per_task_projection_dir / "WORKSTATE-REF-54-RACE.json"
    leftovers = sorted(p.name for p in runtime.per_task_projection_dir.iterdir())
    assert leftovers == ["WORKSTATE-REF-54-RACE.json"], f"unexpected leftover files after 100 iterations: {leftovers}"
    assert canonical.is_file()
    final_payload = json.loads(canonical.read_text(encoding="utf-8"))
    assert final_payload["revision"] == revision


def test_set_handoff_state_writer_isolates_per_task_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.2: two concurrent task_refs each get their own
    per-task projection file with independent payloads."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-A",
            objective="task A",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-B",
            objective="task B",
            status="in_progress",
        )
    )

    a_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-A.json"
    b_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-B.json"
    assert a_path.is_file() and b_path.is_file()

    a_payload = json.loads(a_path.read_text(encoding="utf-8"))
    b_payload = json.loads(b_path.read_text(encoding="utf-8"))
    assert a_payload["task_ref"] == "WORKSTATE-REF-54-A"
    assert a_payload["objective"] == "task A"
    assert b_payload["task_ref"] == "WORKSTATE-REF-54-B"
    assert b_payload["objective"] == "task B"


def test_import_handoff_state_set_active_writes_per_task_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.7 (BR-WORKSTATWORKSTATE-REF-54-20260510-01): import_handoff_state with
    set_active=True must write the per-task projection file from a fresh
    connection after the import transaction commits, so an imported active
    task is immediately visible to the workspace-summary derive path."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-IMPORT",
            objective="import-path probe",
            focus="implementation note sub-implementation note.7",
            status="in_progress",
            target_branch="feature/WORKSTATE-54-import",
            target_worktree_path=str(tmp_path / "import-worktree"),
            task_plan_path="packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-import.md",
        )
    )
    export_path = tmp_path / "WORKSTATE-54-import.snapshot.json"
    exported = _parse(
        mcp_server.export_handoff_state(
            task_ref="WORKSTATE-REF-54-IMPORT",
            output_path=str(export_path),
        )
    )
    assert exported["ok"] is True
    assert export_path.is_file()

    target_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-IMPORT.json"
    target_path.unlink()
    assert not target_path.exists()

    imported = _parse(
        mcp_server.import_handoff_state(
            input_path=str(export_path),
            mode="merge",
            set_active=True,
        )
    )
    assert imported["ok"] is True

    assert target_path.is_file(), (
        "import_handoff_state(set_active=True) must write the per-task "
        "projection so imported active tasks are visible to workspace-summary derive"
    )
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    assert payload["task_projection_schema_version"] == 1
    assert payload["task_ref"] == "WORKSTATE-REF-54-IMPORT"
    assert payload["status"] == "in_progress"
    assert payload["objective"] == "import-path probe"


def test_import_handoff_state_set_active_preserves_target_branch_worktree_plan_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-54-BR-03: importing a snapshot with ``set_active=True`` into
    a fresh DB (no prior handoff_state row) must persist
    ``target_branch``, ``target_worktree_path``, and ``task_plan_path``
    from the exported active block. Dropping these breaks the
    canonical-root/worktree resolution contract — the per-task
    projection ends up with null routing fields and downstream readers
    cannot map cwd to the imported task."""
    # Source workspace exports a fully-populated active row.
    _configure_runtime(tmp_path, monkeypatch)
    src_target_branch = "feature/WORKSTATE-54-br-03"
    src_target_worktree = str(tmp_path / "br-03-worktree")
    src_task_plan_path = "packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-br-03.md"
    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-BR-03-ROUNDTRIP",
            objective="round-trip routing fields",
            focus="BR-03",
            status="in_progress",
            target_branch=src_target_branch,
            target_worktree_path=src_target_worktree,
            task_plan_path=src_task_plan_path,
        )
    )
    export_path = tmp_path / "WORKSTATE-54-br-03.snapshot.json"
    _parse(
        mcp_server.export_handoff_state(
            task_ref="WORKSTATE-REF-54-BR-03-ROUNDTRIP",
            output_path=str(export_path),
        )
    )

    # Import into a fresh workspace: new state_dir, no prior handoff_state row.
    fresh_dir = tmp_path / "fresh-workspace"
    fresh_dir.mkdir()
    fresh_runtime = _configure_runtime(fresh_dir, monkeypatch)

    imported = _parse(
        mcp_server.import_handoff_state(
            input_path=str(export_path),
            mode="merge",
            set_active=True,
        )
    )
    assert imported["ok"] is True

    # Per-task projection must carry the routing fields.
    target_path = fresh_runtime.per_task_projection_dir / "WORKSTATE-REF-54-BR-03-ROUNDTRIP.json"
    assert target_path.is_file()
    payload = json.loads(target_path.read_text(encoding="utf-8"))
    assert payload["target_branch"] == src_target_branch
    assert payload["target_worktree_path"] == src_target_worktree
    assert payload["task_plan_path"] == src_task_plan_path


def test_set_import_active_state_update_writes_routing_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """WORKSTATE-REF-54-BR-03: when ``_set_import_active_state`` UPDATE-s an
    existing row, target_branch / target_worktree_path / task_plan_path
    from the imported active block must overwrite the prior values —
    otherwise re-importing an updated plan leaves the row's routing
    metadata stale."""
    import sqlite3

    from workstate_handoff_mcp.import_export import _set_import_active_state
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    _configure_runtime(tmp_path, monkeypatch)

    # Insert a pre-existing handoff_state row with OLD routing values.
    with _get_db_connection() as conn:  # type: sqlite3.Connection
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO handoff_state (
                task_ref, objective, focus, status, target_branch,
                target_worktree_path, task_plan_path, revision,
                updated_at, updated_by, updated_branch, updated_commit_sha
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, datetime('now'), 'tester', 'main', 'abc123')
            """,
            (
                "WORKSTATE-REF-54-BR-03-UPDATE",
                "initial",
                None,
                "in_progress",
                "feature/old",
                str(tmp_path / "old-wt"),
                "docs/old.md",
            ),
        )
        conn.commit()

    new_target_branch = "feature/WORKSTATE-54-br-03-updated"
    new_target_worktree = str(tmp_path / "new-wt")
    new_task_plan_path = "docs/new.md"

    with _get_db_connection() as conn:
        _set_import_active_state(
            conn,
            "WORKSTATE-REF-54-BR-03-UPDATE",
            {
                "task_ref": "WORKSTATE-REF-54-BR-03-UPDATE",
                "objective": "updated",
                "focus": "BR-03",
                "status": "in_progress",
                "target_branch": new_target_branch,
                "target_worktree_path": new_target_worktree,
                "task_plan_path": new_task_plan_path,
            },
        )
        conn.commit()

    with _get_db_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = dict(
            conn.execute(
                "SELECT target_branch, target_worktree_path, task_plan_path FROM handoff_state WHERE task_ref = ?",
                ("WORKSTATE-REF-54-BR-03-UPDATE",),
            ).fetchone()
        )

    assert row["target_branch"] == new_target_branch
    assert row["target_worktree_path"] == new_target_worktree
    assert row["task_plan_path"] == new_task_plan_path


def test_import_handoff_state_set_active_false_skips_per_task_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.7: when set_active=False, importing a snapshot must
    NOT touch the per-task projection directory — the per-task file is a
    projection of the active row, and a non-set_active import does not
    promote that row to active."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-IMPORT-NOACT",
            objective="import no-active probe",
            status="in_progress",
        )
    )
    export_path = tmp_path / "WORKSTATE-54-import-noact.snapshot.json"
    _parse(
        mcp_server.export_handoff_state(
            task_ref="WORKSTATE-REF-54-IMPORT-NOACT",
            output_path=str(export_path),
        )
    )

    target_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-IMPORT-NOACT.json"
    target_path.unlink()

    imported = _parse(
        mcp_server.import_handoff_state(
            input_path=str(export_path),
            mode="merge",
            set_active=False,
        )
    )
    assert imported["ok"] is True
    assert not target_path.exists(), "import_handoff_state(set_active=False) must not write the per-task projection"


# ---------------------------------------------------------------------------
# implementation note sub-implementation note.1: workspace summary derive-on-read (3 shapes + orphan filter)
# ---------------------------------------------------------------------------


def test_workspace_summary_shape_none_when_no_live_tasks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.1: with no per-task files and no live rows, the
    derive-on-read function returns the ``none`` shape with
    workspace-summary ``schema_version=2`` (CTP-WORKSTATE-REF-T-10)."""
    runtime = _configure_runtime(tmp_path, monkeypatch)
    runtime.per_task_projection_dir.mkdir(parents=True, exist_ok=True)

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    summary = _render_workspace_summary_from_per_task_files()

    assert summary["schema_version"] == 2
    assert summary["shape"] == "none"
    assert summary.get("task_ref") is None
    assert summary.get("active") is None


def test_workspace_summary_shape_single_when_one_live_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.1: exactly one per-task file with a live row produces the
    ``single`` shape carrying the task's projected payload."""
    _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-S2-SINGLE",
            objective="single-task probe",
            focus="implementation note sub-implementation note.1",
            status="in_progress",
            target_branch="feature/WORKSTATE-54-s2-single",
            target_worktree_path=str(tmp_path / "s2-single-worktree"),
            task_plan_path="packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-s2-single.md",
        )
    )

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    summary = _render_workspace_summary_from_per_task_files()

    assert summary["schema_version"] == 2
    assert summary["shape"] == "single"
    assert summary["task_ref"] == "WORKSTATE-REF-54-S2-SINGLE"
    active = summary["active"]
    assert active["task_projection_schema_version"] == 1
    assert active["task_ref"] == "WORKSTATE-REF-54-S2-SINGLE"
    assert active["status"] == "in_progress"
    assert active["objective"] == "single-task probe"
    assert active["focus"] == "implementation note sub-implementation note.1"
    assert active["target_branch"] == "feature/WORKSTATE-54-s2-single"


def test_workspace_summary_shape_workspace_ambiguous_when_two_live_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.1: 2+ per-task files with live rows produce the
    ``workspace_ambiguous`` shape listing every active task in a
    deterministic (sorted by task_ref) order so cold-start readers see
    a stable surface across runs."""
    _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-S2-B",
            objective="task B",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-S2-A",
            objective="task A",
            status="in_progress",
        )
    )

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    summary = _render_workspace_summary_from_per_task_files()

    assert summary["schema_version"] == 2
    assert summary["shape"] == "workspace_ambiguous"
    assert summary.get("task_ref") is None
    assert summary.get("active") is None
    tasks = summary["tasks"]
    assert isinstance(tasks, list) and len(tasks) == 2
    assert [t["task_ref"] for t in tasks] == ["WORKSTATE-REF-54-S2-A", "WORKSTATE-REF-54-S2-B"]
    for t in tasks:
        assert t["task_projection_schema_version"] == 1
        assert t["status"] == "in_progress"


def test_workspace_summary_cross_process_race_returns_valid_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.2: under cross-process write contention,
    ``_render_workspace_summary_from_per_task_files()`` must always
    return a valid shape (single/workspace_ambiguous/none) and never
    raise or observe a partial file. Drives ≥100 reads while two
    subprocess writers race set_handoff_state calls against two
    distinct task_refs.

    The atomic ``tempfile.mkstemp + os.replace`` writer contract from
    implementation note is what makes this hold; the test is the cross-process
    proof for it (implementation note *Proof*: "passes for ≥100 iterations across
    two subprocess writers")."""
    import subprocess
    import sys
    import textwrap

    runtime = _configure_runtime(tmp_path, monkeypatch)

    _parse(mcp_server.set_handoff_state(task_ref="WORKSTATE-REF-54-RACE-A", objective="A", status="in_progress"))
    _parse(mcp_server.set_handoff_state(task_ref="WORKSTATE-REF-54-RACE-B", objective="B", status="in_progress"))

    writer_script = textwrap.dedent(
        """
        import sys
        from pathlib import Path
        from workstate_handoff_mcp import api as mcp_server
        from workstate_handoff_mcp.config import RuntimeConfig

        workspace = Path(sys.argv[1])
        task_ref = sys.argv[2]
        iterations = int(sys.argv[3])

        runtime = RuntimeConfig.for_workspace(
            workspace,
            state_dir=workspace / ".task-state",
            current_task_path=workspace / "CURRENT_TASK.json",
            dashboard_path=workspace / "DASHBOARD.txt",
            current_task_auto_regen=False,
        )
        mcp_server.configure_runtime(runtime)

        from workstate_handoff_mcp.shared_schema import _get_db_connection

        for _ in range(iterations):
            with _get_db_connection() as conn:
                row = conn.execute("SELECT revision FROM handoff_state WHERE task_ref = ?", (task_ref,)).fetchone()
            if row is None:
                continue
            mcp_server.set_handoff_state(
                task_ref=task_ref,
                status="in_progress",
                expected_revision=row[0],
            )
        """
    )

    iterations = 50
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", writer_script, str(tmp_path), task_ref, str(iterations)],
        )
        for task_ref in ("WORKSTATE-REF-54-RACE-A", "WORKSTATE-REF-54-RACE-B")
    ]

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    valid_shapes = {"single", "workspace_ambiguous", "none"}
    reads_observed = 0
    while any(p.poll() is None for p in procs):
        summary = _render_workspace_summary_from_per_task_files()
        assert summary["schema_version"] == 2
        assert summary["shape"] in valid_shapes, summary
        reads_observed += 1
    for p in procs:
        assert p.wait(timeout=30) == 0, f"writer subprocess failed with exit code {p.returncode}"

    final = _render_workspace_summary_from_per_task_files()
    assert final["schema_version"] == 2
    assert final["shape"] in valid_shapes
    assert reads_observed >= 100, f"expected at least 100 reads under contention, got {reads_observed}"

    leftovers = sorted(p.name for p in runtime.per_task_projection_dir.iterdir() if p.suffix == ".tmp")
    assert leftovers == [], f"writer left partial .tmp files: {leftovers}"


def test_workspace_summary_filters_orphan_per_task_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.1 (CTP-WORKSTATE-REF-T-07): a per-task file whose handoff_state
    row was deleted out-of-band must be filtered out of the workspace
    summary. With one valid live task and one orphan file, the summary
    must collapse to ``single`` (not ``workspace_ambiguous``)."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-S2-LIVE",
            objective="live task",
            status="in_progress",
        )
    )
    orphan_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-S2-ORPHAN.json"
    orphan_path.write_text(
        json.dumps(
            {
                "task_projection_schema_version": 1,
                "task_ref": "WORKSTATE-REF-54-S2-ORPHAN",
                "status": "in_progress",
                "objective": "stale orphan",
                "focus": None,
                "target_branch": None,
                "target_worktree_path": None,
                "task_plan_path": None,
                "revision": 0,
                "updated_at": "2026-05-10T00:00:00",
            },
            indent=2,
            sort_keys=True,
        )
    )
    assert orphan_path.is_file()

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    summary = _render_workspace_summary_from_per_task_files()

    assert summary["shape"] == "single", (
        f"orphan filter must drop per-task files with no live row (got shape={summary['shape']!r}, summary={summary!r})"
    )
    assert summary["task_ref"] == "WORKSTATE-REF-54-S2-LIVE"


def test_workspace_summary_transition_none_to_single(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-implementation note.3 (transition coverage): the derive-on-read builder
    must transition from ``none`` to ``single`` when the first live
    task appears. Each call to the builder is a fresh re-derive — no
    cache, no mtime-skip — so the second call must reflect the new
    state without any explicit invalidation."""
    _configure_runtime(tmp_path, monkeypatch)

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    before = _render_workspace_summary_from_per_task_files()
    assert before["shape"] == "none"
    assert before["schema_version"] == 2

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-T-N2S",
            objective="transition none -> single",
            status="in_progress",
        )
    )

    after = _render_workspace_summary_from_per_task_files()
    assert after["shape"] == "single"
    assert after["schema_version"] == 2
    assert after["task_ref"] == "WORKSTATE-REF-54-T-N2S"
    assert after["active"]["task_ref"] == "WORKSTATE-REF-54-T-N2S"


def test_workspace_summary_transition_single_to_workspace_ambiguous(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.3 (transition coverage): the derive-on-read builder
    must transition from ``single`` to ``workspace_ambiguous`` when a
    second concurrent live task appears. The ``single``-shape
    ``task_ref`` field MUST NOT leak into the ambiguous shape (which
    uses ``tasks: [...]`` instead)."""
    _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-T-S2A-FIRST",
            objective="transition single -> ambiguous (first task)",
            status="in_progress",
        )
    )

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    before = _render_workspace_summary_from_per_task_files()
    assert before["shape"] == "single"
    assert before["task_ref"] == "WORKSTATE-REF-54-T-S2A-FIRST"

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-T-S2A-SECOND",
            objective="transition single -> ambiguous (second task)",
            status="in_progress",
        )
    )

    after = _render_workspace_summary_from_per_task_files()
    assert after["shape"] == "workspace_ambiguous"
    assert after["schema_version"] == 2
    assert after.get("task_ref") is None, "single-shape task_ref must not leak into the ambiguous shape"
    assert after.get("active") is None
    assert [t["task_ref"] for t in after["tasks"]] == [
        "WORKSTATE-REF-54-T-S2A-FIRST",
        "WORKSTATE-REF-54-T-S2A-SECOND",
    ]


def test_workspace_summary_excludes_done_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """BR-WORKSTATWORKSTATE-REF-54-20260510-S2-01: a per-task projection file backed by a
    handoff_state row whose status is outside ``LIVE_ACTIVE_STATUSES`` (e.g.
    ``done``) MUST be filtered out of the workspace-summary derive.

    Prior to the fix, the SELECT in ``_render_workspace_summary_from_per_task_files``
    treated ``done`` rows as live so a single-task workspace whose row had
    been flipped to ``done`` (but not yet archived) reported ``shape=single``
    instead of ``shape=none``. The fix narrows the SELECT to
    ``LIVE_ACTIVE_STATUSES`` from ``shared_primitives``.
    """
    runtime = _configure_runtime(tmp_path, monkeypatch)

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-S2-DONE",
            objective="done-status probe",
            status="in_progress",
        )
    )
    assert created["ok"] is True
    revision = created["active"]["revision"]

    flipped = _parse(
        mcp_server.update_task_status(
            task_ref="WORKSTATE-REF-54-S2-DONE",
            status="done",
            expected_revision=revision,
        )
    )
    assert flipped["ok"] is True

    target_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-S2-DONE.json"
    assert target_path.is_file(), "precondition: per-task file is still on disk (flip-to-done does not reap)"

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    summary = _render_workspace_summary_from_per_task_files()

    assert summary["schema_version"] == 2
    assert summary["shape"] == "none", f"done-status row must be filtered from derive (got {summary!r})"
    assert summary.get("task_ref") is None
    assert summary.get("active") is None


def test_workspace_summary_transition_workspace_ambiguous_to_single(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sub-implementation note.3 (transition coverage): the derive-on-read builder
    must transition from ``workspace_ambiguous`` back to ``single``
    when one of two live tasks is archived. ``archive`` reaps the
    per-task projection file (sub-implementation note.3 contract), so the next
    builder call sees only the surviving task — no stale ambiguity."""
    runtime = _configure_runtime(tmp_path, monkeypatch)

    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-T-A2S-KEEP",
            objective="surviving task",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-T-A2S-DROP",
            objective="task to be archived",
            status="in_progress",
        )
    )

    from workstate_handoff_mcp.current_task_rendering import (
        _render_workspace_summary_from_per_task_files,
    )

    before = _render_workspace_summary_from_per_task_files()
    assert before["shape"] == "workspace_ambiguous"
    assert {t["task_ref"] for t in before["tasks"]} == {
        "WORKSTATE-REF-54-T-A2S-KEEP",
        "WORKSTATE-REF-54-T-A2S-DROP",
    }

    archived = _parse(mcp_server.archive(payload={"operation": "archive", "task_ref": "WORKSTATE-REF-54-T-A2S-DROP"}))
    assert archived["ok"] is True
    drop_path = runtime.per_task_projection_dir / "WORKSTATE-REF-54-T-A2S-DROP.json"
    assert not drop_path.exists(), "precondition: archive must reap the per-task file (sub-implementation note.3)"

    after = _render_workspace_summary_from_per_task_files()
    assert after["shape"] == "single"
    assert after["schema_version"] == 2
    assert after["task_ref"] == "WORKSTATE-REF-54-T-A2S-KEEP"
    assert after["active"]["task_ref"] == "WORKSTATE-REF-54-T-A2S-KEEP"


# ---------------------------------------------------------------------------
# implementation note: live writer flip — schema_version: 2 derive-on-read
# ---------------------------------------------------------------------------


def test_live_current_task_json_writer_emits_schema_version_2_single_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """implementation note contract: the on-disk CURRENT_TASK.json produced by the live
    writer must be the v2 workspace summary, not the legacy v1 active block.

    With one live task, the file shape is ``single`` and ``active`` is the
    per-task projection payload (``task_projection_schema_version=1``).
    """
    runtime = _configure_runtime(tmp_path, monkeypatch, current_task_auto_regen=True)

    created = _parse(
        mcp_server.set_handoff_state(
            task_ref="WORKSTATE-REF-54-S6-LIVE",
            objective="implementation note live writer flip probe",
            focus="schema_version: 2 derive-on-read",
            status="in_progress",
            target_branch="feature/WORKSTATE-54",
            target_worktree_path=str(tmp_path / "worktree"),
            task_plan_path="packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-54-fixture.md",
        )
    )
    assert created["ok"] is True

    _parse(mcp_server.render_handoff(kind="current_task", task_ref="WORKSTATE-REF-54-S6-LIVE"))

    payload = json.loads(runtime.current_task_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2, payload
    assert payload["shape"] == "single", payload
    assert payload["task_ref"] == "WORKSTATE-REF-54-S6-LIVE"
    active = payload["active"]
    assert active["task_projection_schema_version"] == 1
    assert active["task_ref"] == "WORKSTATE-REF-54-S6-LIVE"
    assert active["status"] == "in_progress"
    assert active["objective"] == "implementation note live writer flip probe"
    # Legacy v1 top-level fields must not leak through the v2 writer.
    assert "decisions_recent" not in payload
    assert "blockers_open" not in payload


def test_live_current_task_json_writer_emits_workspace_ambiguous_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With two live tasks, the live writer emits ``workspace_ambiguous``."""
    runtime = _configure_runtime(tmp_path, monkeypatch, current_task_auto_regen=True)

    for task_ref, branch in [
        ("WORKSTATE-REF-54-S6-A", "feature/WORKSTATE-54-a"),
        ("WORKSTATE-REF-54-S6-B", "feature/WORKSTATE-54-b"),
    ]:
        _parse(
            mcp_server.set_handoff_state(
                task_ref=task_ref,
                objective=f"Live writer ambiguous probe for {task_ref}",
                status="in_progress",
                target_branch=branch,
                target_worktree_path=str(tmp_path / f"worktree-{task_ref}"),
            )
        )

    _parse(mcp_server.render_handoff(kind="current_task", task_ref="WORKSTATE-REF-54-S6-A"))

    payload = json.loads(runtime.current_task_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 2, payload
    assert payload["shape"] == "workspace_ambiguous", payload
    refs = sorted(t["task_ref"] for t in payload["tasks"])
    assert refs == ["WORKSTATE-REF-54-S6-A", "WORKSTATE-REF-54-S6-B"]
    assert all(t["task_projection_schema_version"] == 1 for t in payload["tasks"])
