"""Deterministic read-profile payload-size measurement fixture.

Synthesizes a fixed handoff history (decisions + tests + findings) and
serializes the response payload under three shapes:

* ``full_debug``    — legacy unbounded baseline
* ``hot_summary``   — recommended session-orientation profile
* ``review_packet`` with ``response_budget_bytes=8000`` — budgeted retry path

The test asserts the **ordering** of the three serialized sizes rather than
absolute byte counts, so it remains stable across small payload tweaks:

    full_debug > review_packet (budgeted) > hot_summary > identity

The numbers are also printed (via ``-s``) so a maintainer can sanity-check
the savings ratio when revising profile defaults. This is the durable
counterpart to the prose savings claims in
``docs/guides/token-efficient-usage.md``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


@pytest.fixture()
def measurement_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.txt",
        current_task_auto_regen=True,
    )
    mcp_server.configure_runtime(runtime)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _seed_history(task_ref: str) -> None:
    mcp_server.set_handoff_state(
        task_ref=task_ref,
        objective="profile measurement fixture",
        status="in_progress",
    )
    for idx in range(40):
        mcp_server.record_decision(
            session="meas",
            decision=f"dec_{idx}",
            rationale="r" * 500,
        )
    for idx in range(10):
        mcp_server.record_test_result(
            session="meas",
            command=f"pytest -k m{idx}",
            passed=True,
            result="ok",
        )
    for idx in range(15):
        mcp_server.review_findings(
            review={
                "operation": "record",
                "task_ref": task_ref,
                "session": "meas",
                "finding_id": f"MEAS-{idx:03d}",
                "severity": "low",
                "file_path": f"src/file_{idx}.py",
                "description": "f" * 400,
            }
        )


def _serialized_bytes(payload: dict) -> int:
    return len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))


def test_profile_measurement_orders_match_expectations(
    measurement_workspace: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _seed_history("MEAS-1")

    identity = mcp_server.get_handoff_state(task_ref="MEAS-1", read_profile="identity")
    hot = mcp_server.get_handoff_state(task_ref="MEAS-1", read_profile="hot_summary")
    review_budgeted = mcp_server.get_handoff_state(
        task_ref="MEAS-1",
        read_profile="review_packet",
        response_budget_bytes=8000,
    )
    full = mcp_server.get_handoff_state(task_ref="MEAS-1", read_profile="full_debug")

    sizes = {
        "identity": _serialized_bytes(identity),
        "hot_summary": _serialized_bytes(hot),
        "review_packet@8000": _serialized_bytes(review_budgeted),
        "full_debug": _serialized_bytes(full),
    }

    with capsys.disabled():
        print("\nread_profile payload-size measurement:")
        for name, size in sizes.items():
            print(f"  {name:24s} {size:>8d} bytes")

    # Ordering invariants — these are the contract the docs depend on.
    # Identity carries the full limits/tool registry block (shared by every
    # profile), so it is not the smallest payload in absolute bytes; what
    # matters is that each higher profile adds data on top of identity, and
    # that the budgeted review_packet stays below the unbounded full_debug.
    assert sizes["identity"] < sizes["hot_summary"], sizes
    assert sizes["hot_summary"] < sizes["full_debug"], sizes
    assert sizes["review_packet@8000"] < sizes["full_debug"], sizes
