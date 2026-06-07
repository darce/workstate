"""WORKSTATE-REF-45 implementation note: fold update_task_status into set_handoff_state(status_only=True).

Asserts the consolidated surface preserves the four-case concurrency
contract verbatim:

1. Active in_progress -> done **without** ``expected_revision`` succeeds
   (status='done' revision-inference path).
2. Active in_progress -> review **without** ``expected_revision`` is
   rejected with the existing stale-write envelope (mid-lifecycle
   protection preserved).
3. Active in_progress -> review **with** the current revision succeeds.
4. Archived snapshot status update succeeds without ``expected_revision``
   (revisionless archive path).

Plus: legacy ``update_task_status`` MCP tool registration is removed in
favor of ``set_handoff_state(status_only=True, ...)``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


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


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=tmp_path / ".task-state",
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return tmp_path


def test_set_handoff_state_status_only_active_done_elides_expected_revision(workspace: Path) -> None:
    """Case 1: active in_progress -> done without expected_revision succeeds."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-DONE",
            objective="exercise done elide",
            status="in_progress",
        )
    )

    envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-DONE",
            status="done",
            status_only=True,
        )
    )

    assert envelope["ok"] is True
    assert envelope["status"] == "done"
    assert envelope["updated_scope"] == "active"


def test_set_handoff_state_status_only_active_mid_lifecycle_rejects_missing_revision(workspace: Path) -> None:
    """Case 2: active in_progress -> review without expected_revision is rejected."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-MID-REJECT",
            objective="exercise mid-lifecycle reject",
            status="in_progress",
        )
    )

    envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-MID-REJECT",
            status="review",
            status_only=True,
        )
    )

    assert envelope["ok"] is False
    assert "revision" in envelope.get("error", "").lower() or "stale" in envelope.get("error", "").lower()


def test_set_handoff_state_status_only_active_mid_lifecycle_accepts_current_revision(workspace: Path) -> None:
    """Case 3: active in_progress -> review with current revision succeeds."""
    set_envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-MID-ACCEPT",
            objective="exercise mid-lifecycle accept",
            status="in_progress",
        )
    )
    current_revision = set_envelope["active"]["revision"]

    envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-MID-ACCEPT",
            status="review",
            expected_revision=current_revision,
            status_only=True,
        )
    )

    assert envelope["ok"] is True
    assert envelope["status"] == "review"
    assert envelope["updated_scope"] == "active"


def test_set_handoff_state_status_only_archived_revisionless(workspace: Path) -> None:
    """Case 4: archived snapshot update without expected_revision succeeds."""
    _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-ARCHIVED",
            objective="exercise archived revisionless update",
            status="in_progress",
        )
    )
    _parse(mcp_server.archive({"operation": "archive", "task_ref": "STATUS-ONLY-ARCHIVED"}))

    envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="STATUS-ONLY-ARCHIVED",
            status="blocked",
            status_only=True,
        )
    )

    assert envelope["ok"] is True
    assert envelope["status"] == "blocked"
    assert envelope["updated_scope"] == "archived"


def test_registry_removes_update_task_status_in_favor_of_set_handoff_state_status_only() -> None:
    from workstate_handoff_mcp.api import _build_tool_registry

    registry = _build_tool_registry()
    names = {entry.name for entry in registry}

    assert "set_handoff_state" in names, "consolidated set_handoff_state must remain registered"
    assert "update_task_status" not in names, (
        "legacy update_task_status must be removed in favor of set_handoff_state(status_only=True, ...)"
    )


def test_expected_handoff_tool_count_decremented_for_slice_6() -> None:
    from workstate_handoff_mcp.invariants import EXPECTED_HANDOFF_TOOL_COUNT

    assert EXPECTED_HANDOFF_TOOL_COUNT == 22


def test_set_handoff_state_status_only_terminal_flush_active(workspace: Path) -> None:
    """WORKSTATE-REF-54-FU implementation note: terminal-status transition flushes CURRENT_TASK.json.

    When ``set_handoff_state(status='done', status_only=True)`` lands on
    an active row, the resolved post-write status leaves
    ``LIVE_ACTIVE_STATUSES`` and the on-disk workspace summary must be
    rewritten unconditionally so it agrees with the live derive. Mirrors
    ``decisions.py:758`` (close_check) and the archive flush. Without
    this flush, the stale on-disk file would still advertise the task as
    live to legacy file consumers.
    """
    _parse(
        mcp_server.set_handoff_state(
            task_ref="TERMINAL-FLUSH-DONE",
            objective="terminal flush",
            status="in_progress",
        )
    )
    _parse(
        mcp_server.set_handoff_state(
            task_ref="TERMINAL-FLUSH-NEIGHBOR",
            objective="co-resident live task",
            status="in_progress",
        )
    )

    envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="TERMINAL-FLUSH-DONE",
            status="done",
            status_only=True,
        )
    )
    assert envelope["ok"] is True

    current_task_path = workspace / "CURRENT_TASK.json"
    assert current_task_path.exists(), (
        "terminal status transition must flush CURRENT_TASK.json "
        "unconditionally so legacy file readers see the live derive"
    )

    on_disk = json.loads(current_task_path.read_text(encoding="utf-8"))
    assert on_disk.get("schema_version") == 2
    refs: list[str] = []
    if on_disk.get("shape") == "single":
        ref = on_disk.get("task_ref")
        if isinstance(ref, str):
            refs.append(ref)
    elif on_disk.get("shape") == "workspace_ambiguous":
        for entry in on_disk.get("tasks", []):
            ref = entry.get("task_ref") if isinstance(entry, dict) else None
            if isinstance(ref, str):
                refs.append(ref)
    assert "TERMINAL-FLUSH-DONE" not in refs, (
        "task transitioned to terminal status must drop out of the on-disk "
        "workspace summary; status='done' is outside LIVE_ACTIVE_STATUSES"
    )
    assert "TERMINAL-FLUSH-NEIGHBOR" in refs, (
        "unrelated live task must remain in the workspace summary after the terminal flush"
    )


def test_set_handoff_state_status_only_live_transition_does_not_force_flush(workspace: Path) -> None:
    """WORKSTATE-REF-54-FU implementation note: live-to-live transitions remain routine-gated.

    Transitioning between two ``LIVE_ACTIVE_STATUSES`` values (e.g.
    ``in_progress`` -> ``review``) is a mid-lifecycle write, not a
    terminal transition. The post-write summary flush stays gated on
    ``current_task_auto_regen`` so we do not regress WORKSTATE-REF-54's
    derive-on-read posture for routine writes. The default fixture has
    ``current_task_auto_regen=False``, so the file must NOT be force-
    written for a live-to-live status flip.
    """
    set_envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="LIVE-FLIP",
            objective="live-to-live flip",
            status="in_progress",
        )
    )
    rev = set_envelope["active"]["revision"]

    envelope = _parse(
        mcp_server.set_handoff_state(
            task_ref="LIVE-FLIP",
            status="review",
            expected_revision=rev,
            status_only=True,
        )
    )
    assert envelope["ok"] is True

    current_task_path = workspace / "CURRENT_TASK.json"
    # Routine live-to-live transitions stay gated; the file is on-demand.
    assert not current_task_path.exists(), (
        "live-to-live status transitions must not force the on-disk "
        "workspace summary write — that surface stays gated on "
        "current_task_auto_regen for mid-lifecycle mutations"
    )
