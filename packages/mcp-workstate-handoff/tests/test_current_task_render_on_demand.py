from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.review_findings_queries import reconcile_review_findings
from workstate_handoff_mcp.shared_schema import _get_db_connection


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


def _configure_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, auto_regen: bool) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    current_task_path = tmp_path / "CURRENT_TASK.json"
    dashboard_path = tmp_path / "DASHBOARD.txt"
    monkeypatch.delenv("AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN", raising=False)
    if auto_regen:
        monkeypatch.setenv("AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN", "1")
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=current_task_path,
        dashboard_path=dashboard_path,
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def _seed_active_task(task_ref: str = "DEMOTE-TASK") -> dict:
    created = _parse(mcp_server.set_handoff_state(task_ref=task_ref, objective="Demotion probe", status="in_progress"))
    assert created["ok"] is True
    return created


def _record_finding(task_ref: str, finding_id: str) -> dict:
    finding = _parse(
        mcp_server.record_review_finding(
            session="demote-session",
            finding_id=finding_id,
            severity="medium",
            file_path="docs/plan.md",
            description="Demotion contract probe",
            task_ref=task_ref,
        )
    )
    assert finding["ok"] is True, finding
    return finding


def _close_slice(task_ref: str, _monkeypatch: pytest.MonkeyPatch) -> dict:
    created = _seed_active_task(task_ref)
    payload = _parse(
        mcp_server.close_slice(
            session="demote-close-slice",
            decision="codex_slice_complete_demote_render_gate",
            rationale=(
                "## Changes\n- Exercise close_slice current-task write gate.\n\n"
                "## Verification\n- Covered by targeted regression.\n\n"
                "## Schema / Contract Changes\n- None.\n\n"
                "## Open Threads\n- None."
            ),
            task_ref=task_ref,
            expected_revision=created["active"]["revision"],
        )
    )
    assert payload["ok"] is True, payload
    return payload


def _record_review_finding(task_ref: str, _monkeypatch: pytest.MonkeyPatch) -> dict:
    _seed_active_task(task_ref)
    return _record_finding(task_ref, "DEMOTE-FINDING-1")


def _batch_record_review_findings(task_ref: str, _monkeypatch: pytest.MonkeyPatch) -> dict:
    _seed_active_task(task_ref)
    payload = _parse(
        mcp_server.batch_record_review_findings(
            session="demote-batch",
            task_ref=task_ref,
            findings=[
                {
                    "finding_id": "DEMOTE-BATCH-1",
                    "severity": "medium",
                    "file_path": "docs/plan.md",
                    "description": "Batch demotion probe",
                }
            ],
        )
    )
    assert payload["ok"] is True, payload
    return payload


def _update_review_finding(task_ref: str, _monkeypatch: pytest.MonkeyPatch) -> dict:
    _seed_active_task(task_ref)
    _record_finding(task_ref, "DEMOTE-UPDATE-1")
    payload = _parse(
        mcp_server.update_review_finding(
            task_ref=task_ref,
            finding_id="DEMOTE-UPDATE-1",
            status="deferred",
            resolution_notes="Deferred to preserve the repro fixture.",
            session="demote-update",
        )
    )
    assert payload["ok"] is True, payload
    return payload


def _reconcile_review_findings_apply(task_ref: str, _monkeypatch: pytest.MonkeyPatch) -> dict:
    _seed_active_task(task_ref)
    payload = _parse(reconcile_review_findings(task_ref=task_ref, apply=True))
    assert payload["ok"] is True, payload
    assert payload["checks"]["duplicates"]["deduped_rows_removed"] > 0
    return payload


RoutineWrite = Callable[[str, pytest.MonkeyPatch], dict]


@pytest.mark.parametrize(
    ("operation", "writer"),
    [
        pytest.param("close_slice", _close_slice, id="close-slice"),
        pytest.param("record_review_finding", _record_review_finding, id="record-review-finding"),
        pytest.param("batch_record_review_findings", _batch_record_review_findings, id="batch-record-review-findings"),
        pytest.param("update_review_finding", _update_review_finding, id="update-review-finding"),
        pytest.param("reconcile_review_findings", _reconcile_review_findings_apply, id="reconcile-review-findings"),
    ],
)
@pytest.mark.parametrize("auto_regen", [False, True], ids=["default-off", "env-opt-in"])
def test_routine_write_paths_respect_current_task_auto_regen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    writer: RoutineWrite,
    auto_regen: bool,
) -> None:
    runtime = _configure_runtime(tmp_path, monkeypatch, auto_regen=auto_regen)

    if operation == "reconcile_review_findings":
        monkeypatch.setattr(
            "workstate_handoff_mcp.review_findings_queries._collect_review_findings_integrity",
            lambda conn, task_ref, apply=False: {
                "healthy": True,
                "checks": {
                    "duplicates": {
                        "count": 0,
                        "items": [],
                        "deduped_rows_removed": 1 if apply else 0,
                    },
                    "done_with_open_findings": {
                        "active_status": "in_progress",
                        "open_count": 0,
                        "is_violation": False,
                    },
                    "stale_open_findings": {"count": 0, "items": []},
                    "missing_provenance": {"count": 0, "items": []},
                    "reopen_metadata": {"count": 0, "items": []},
                },
            },
        )

    assert runtime.current_task_path.exists() is False
    payload = writer(f"{operation}-{auto_regen}", monkeypatch)

    assert runtime.current_task_path.exists() is auto_regen
    if auto_regen:
        data = json.loads(runtime.current_task_path.read_text())
        assert data["task_ref"] == f"{operation}-{auto_regen}"

    if operation == "close_slice":
        assert payload["current_task_md_written"] is auto_regen
        current_task_artifacts = [
            artifact for artifact in payload.get("artifacts", []) if artifact.get("type") == "current_task_md"
        ]
        assert len(current_task_artifacts) == 1
        assert current_task_artifacts[0]["path"] == "CURRENT_TASK.json"
        assert current_task_artifacts[0]["written"] is auto_regen


def test_render_handoff_current_task_writes_even_when_auto_regen_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _configure_runtime(tmp_path, monkeypatch, auto_regen=False)
    _seed_active_task("explicit-render")

    payload = _parse(mcp_server.render_handoff(kind="current_task", task_ref="explicit-render"))

    assert payload["ok"] is True
    assert runtime.current_task_path.exists() is True
    assert json.loads(runtime.current_task_path.read_text())["task_ref"] == "explicit-render"


def test_export_handoff_state_includes_current_task_markdown_when_auto_regen_is_off(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _configure_runtime(tmp_path, monkeypatch, auto_regen=False)
    _seed_active_task("export-current-task")
    export_path = tmp_path / ".task-state" / "exports" / "export-current-task.json"

    payload = _parse(
        mcp_server.export_handoff_state(
            task_ref="export-current-task",
            output_path=str(export_path),
            include_markdown=True,
        )
    )
    exported = json.loads(export_path.read_text())

    assert payload["ok"] is True
    assert exported["current_task_markdown"] is not None
    assert "export-current-task" in exported["current_task_markdown"]
    assert runtime.current_task_path.exists() is False
