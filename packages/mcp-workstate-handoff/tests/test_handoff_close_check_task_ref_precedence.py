"""WORKSTATE-REF-04 implementation note: close-check explicit ``task_ref`` precedence regression.

``handoff_close_check`` already scopes its multi-row ambiguity branch
under ``if task_ref is None`` (decisions.py). This file is the WORKSTATE-REF-04
regression anchor that locks that contract in place:

- with multiple in_progress rows and an explicit ``task_ref``, close-check
  binds to the named row instead of taking the ambiguity branch;
- with multiple in_progress rows and no ``task_ref``, it still refuses
  loudly and returns the candidate list.

No ``decisions.py`` code edit accompanies these tests — they prove the
existing guard stays correct (the constraint in the WORKSTATE-REF-04 plan).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


@pytest.fixture()
def isolated_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.txt",
    )
    mcp_server.configure_runtime(runtime)
    return {"state_dir": state_dir, "db_path": runtime.db_path}


def _parse(payload: str | dict) -> dict:
    raw = payload if isinstance(payload, dict) else json.loads(payload)
    data = raw.get("data", {}) if isinstance(raw, dict) else {}
    merged = {**raw, **(data if isinstance(data, dict) else {})}
    return merged


def _seed_two_active(suffix_a: str = "a", suffix_b: str = "b") -> None:
    for suffix in (suffix_a, suffix_b):
        _parse(
            mcp_server.set_handoff_state(
                task_ref=f"WORKSTATE04-close-{suffix}",
                objective=f"Active task {suffix}",
                status="in_progress",
                target_worktree_path=f"/tmp/WORKSTATE04-close-{suffix}-wt",
                target_branch=f"feature/WORKSTATE04-close-{suffix}",
            )
        )


def test_explicit_task_ref_binds_close_check_to_named_row(isolated_handoff: dict) -> None:
    _seed_two_active()

    response = _parse(mcp_server.handoff_close_check(enforce=True, task_ref="WORKSTATE04-close-a"))

    # The named row is in_progress, so close-check legitimately reports not
    # ready on "status must be done" — the point is that the *named* row was
    # evaluated, not the distractor, and no ambiguity error was raised.
    assert response["checks"]["active_task"]["matches_target"] is True
    assert response["checks"]["active_task"]["status"] == "in_progress"


def test_no_task_ref_still_refuses_on_multiple_active_rows(isolated_handoff: dict) -> None:
    _seed_two_active()

    response = _parse(mcp_server.handoff_close_check(enforce=True))

    assert response["ok"] is False
    error_text = response.get("error") or response.get("data", {}).get("error") or ""
    assert "ambiguous" in error_text.lower() or "multiple" in error_text.lower()
    candidates = response.get("candidates") or []
    assert sorted(candidates) == ["WORKSTATE04-close-a", "WORKSTATE04-close-b"]
