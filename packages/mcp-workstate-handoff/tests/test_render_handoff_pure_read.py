"""Pure-read contract for ``render_handoff(kind='current_task', write_file=False)``.

WORKSTATE-REF-54-FU implementation note: lock the contract that lifecycle-handler readers will rely on.

The plan declares this entry point as the canonical client-reader path and
forbids it from mutating disk or DB. The four lifecycle readers in
``workstate-system`` migrate to it in implementation note; this test pins the invariants
they rely on:

(a) N back-to-back invocations leave ``CURRENT_TASK.json`` mtime unchanged,
    including the case where the file does not exist.
(b) No row appears in the auditable mutation tables across the N calls.
(c) The returned envelopes are byte-equal across calls — every consumer
    that re-reads sees exactly the same snapshot of derived state.
(d) The envelope's ``current_task_json`` parses as the v2 workspace-summary
    payload (``schema_version=2`` + ``shape`` discriminator), so
    downstream consumers — including ``load_workspace_summary_compat`` in
    ``workstate-system`` — can round-trip it byte-for-byte against the
    on-disk file when the file is current.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import _get_db_connection

_PURE_READ_CALLS = 5
_AUDIT_TABLES = (
    "handoff_state",
    "decisions",
    "blockers",
    "verified_tests",
    "review_findings",
    "review_runs",
    "next_actions",
    "task_archives",
    "touched_files",
)


@pytest.fixture()
def isolated_handoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    current_task_path = tmp_path / "CURRENT_TASK.json"
    dashboard_path = tmp_path / "DASHBOARD.txt"
    monkeypatch.delenv("AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN", raising=False)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=current_task_path,
        dashboard_path=dashboard_path,
    )
    mcp_server.configure_runtime(runtime)
    return {
        "workspace": tmp_path,
        "current_task_path": current_task_path,
        "dashboard_path": dashboard_path,
        "runtime": runtime,
    }


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


def _seed_task(task_ref: str, status: str = "in_progress") -> None:
    payload = _parse(
        mcp_server.set_handoff_state(
            task_ref=task_ref,
            objective="Pure-read contract probe",
            status=status,
        )
    )
    assert payload["ok"] is True, payload


def _snapshot_row_counts() -> dict[str, int]:
    counts: dict[str, int] = {}
    with _get_db_connection() as conn:
        for table in _AUDIT_TABLES:
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            counts[table] = int(row[0]) if row else 0
    return counts


def test_pure_read_does_not_create_file_when_absent(isolated_handoff: dict) -> None:
    _seed_task("pure-read-no-file", status="in_progress")
    current_task_path: Path = isolated_handoff["current_task_path"]
    if current_task_path.exists():
        current_task_path.unlink()
    assert not current_task_path.exists()

    for _ in range(_PURE_READ_CALLS):
        result = _parse(
            mcp_server.render_handoff(
                kind="current_task",
                task_ref="pure-read-no-file",
                write_file=False,
            )
        )
        assert result["ok"] is True
        assert result["written"] is False

    assert not current_task_path.exists(), "render_handoff(write_file=False) must not materialize CURRENT_TASK.json"


def test_pure_read_leaves_existing_file_mtime_unchanged(isolated_handoff: dict) -> None:
    _seed_task("pure-read-mtime", status="in_progress")
    current_task_path: Path = isolated_handoff["current_task_path"]
    _parse(
        mcp_server.render_handoff(
            kind="current_task",
            task_ref="pure-read-mtime",
            write_file=True,
        )
    )
    assert current_task_path.exists()
    baseline_mtime_ns = current_task_path.stat().st_mtime_ns
    baseline_bytes = current_task_path.read_bytes()

    for _ in range(_PURE_READ_CALLS):
        _parse(
            mcp_server.render_handoff(
                kind="current_task",
                task_ref="pure-read-mtime",
                write_file=False,
            )
        )

    assert current_task_path.stat().st_mtime_ns == baseline_mtime_ns, (
        "pure-read calls must not touch the on-disk workspace summary"
    )
    assert current_task_path.read_bytes() == baseline_bytes, (
        "pure-read calls must not rewrite the on-disk workspace summary"
    )


def test_pure_read_emits_no_db_mutation(isolated_handoff: dict) -> None:
    _seed_task("pure-read-db", status="in_progress")
    before = _snapshot_row_counts()

    envelopes: list[str] = []
    for _ in range(_PURE_READ_CALLS):
        raw = mcp_server.render_handoff(
            kind="current_task",
            task_ref="pure-read-db",
            write_file=False,
        )
        envelopes.append(raw if isinstance(raw, str) else json.dumps(raw, sort_keys=True))

    after = _snapshot_row_counts()
    assert before == after, f"pure-read calls mutated auditable tables: before={before} after={after}"


def test_pure_read_envelopes_are_identical_across_calls(isolated_handoff: dict) -> None:
    _seed_task("pure-read-stable", status="in_progress")

    envelopes: list[dict] = []
    for _ in range(_PURE_READ_CALLS):
        envelopes.append(
            _parse(
                mcp_server.render_handoff(
                    kind="current_task",
                    task_ref="pure-read-stable",
                    write_file=False,
                )
            )
        )

    first = envelopes[0]
    for next_envelope in envelopes[1:]:
        assert next_envelope == first, "pure-read envelopes drifted across calls"


def test_pure_read_current_task_json_is_v2_workspace_summary_payload(
    isolated_handoff: dict,
) -> None:
    _seed_task("pure-read-shape-single", status="in_progress")

    envelope = _parse(
        mcp_server.render_handoff(
            kind="current_task",
            task_ref="pure-read-shape-single",
            write_file=False,
        )
    )
    assert envelope["written"] is False
    assert envelope["current_task_json"] is not None

    payload = json.loads(envelope["current_task_json"])
    assert payload["schema_version"] == 2, payload
    assert payload["shape"] in ("single", "workspace_ambiguous", "none"), payload
    if payload["shape"] == "single":
        assert payload["task_ref"] == "pure-read-shape-single"
        assert isinstance(payload.get("active"), dict)


def test_pure_read_round_trips_against_on_disk_file_when_current(
    isolated_handoff: dict,
) -> None:
    _seed_task("pure-read-roundtrip", status="in_progress")
    current_task_path: Path = isolated_handoff["current_task_path"]

    _parse(
        mcp_server.render_handoff(
            kind="current_task",
            task_ref="pure-read-roundtrip",
            write_file=True,
        )
    )
    on_disk = json.loads(current_task_path.read_text())

    envelope = _parse(
        mcp_server.render_handoff(
            kind="current_task",
            task_ref="pure-read-roundtrip",
            write_file=False,
        )
    )
    derived = json.loads(envelope["current_task_json"])

    assert derived == on_disk, "pure-read derived payload must match the on-disk file when the file is current"
