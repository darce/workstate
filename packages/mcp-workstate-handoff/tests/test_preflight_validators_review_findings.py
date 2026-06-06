"""implementation note implementation note — preflight validator review fixes (BR-01 / BR-02).

* BR-01: ``validate_review_ready`` must recognise tasks present in the
  handoff state. The earlier shape lookup (``data["identity"]``) was
  always absent, so every call mis-reported ``unknown_task``.
* BR-02: ``validate_finding_resolution`` must enforce the WORKSTATE-REF-41
  same-or-newer-descendant rule. A ``fixed_commit_sha`` whose relation
  to the finding's recorded commit is ``ancestor`` or ``diverged``
  must be rejected with ``descendant_commit_required``.
"""

from __future__ import annotations

from typing import Any

import pytest


def _envelope_with_active(task_ref: str) -> dict[str, Any]:
    """Mimic the real ``get_handoff_state(sections='identity')`` shape."""
    return {
        "ok": True,
        "schema_version": 2,
        "tool": "get_handoff_state",
        "scope": {"task_ref": task_ref},
        "data": {
            "active": {"task_ref": task_ref, "status": "in_progress"},
            "limits": {},
        },
    }


def _envelope_no_active() -> dict[str, Any]:
    return {
        "ok": True,
        "schema_version": 2,
        "tool": "get_handoff_state",
        "scope": {"task_ref": None},
        "data": {"active": None, "message": "No active handoff state."},
    }


def test_validate_review_ready_recognises_known_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from workstate_handoff_mcp import preflight

    monkeypatch.setattr(
        preflight,
        "get_handoff_state",
        lambda **kwargs: _envelope_with_active(kwargs.get("task_ref", "")),
    )
    monkeypatch.setattr(preflight, "list_review_findings", lambda **kwargs: {"data": {"findings": []}})

    result = preflight.validate_review_ready(task_ref="WORKSTATE-REF-DEFERRED-FRICTION")

    assert result["boundary_state"]["task_known"] is True
    assert all(b["kind"] != "unknown_task" for b in result["blockers"])


def test_validate_review_ready_still_rejects_truly_missing_task(monkeypatch: pytest.MonkeyPatch) -> None:
    from workstate_handoff_mcp import preflight

    monkeypatch.setattr(preflight, "get_handoff_state", lambda **kwargs: _envelope_no_active())
    monkeypatch.setattr(preflight, "list_review_findings", lambda **kwargs: {"data": {"findings": []}})

    result = preflight.validate_review_ready(task_ref="WORKSTATE-REF-NEVER-REGISTERED")

    assert result["ok"] is False
    assert any(b["kind"] == "unknown_task" for b in result["blockers"])


def _finding_envelope(commit_sha: str | None) -> dict[str, Any]:
    return {
        "ok": True,
        "data": {
            "findings": [
                {
                    "id": 1,
                    "finding_id": "WORKSTATE-REF-DEMO-001",
                    "task_ref": "WORKSTATE-REF-DEMO",
                    "status": "open",
                    "commit_sha": commit_sha,
                    "branch": "feature/demo",
                }
            ]
        },
    }


def test_validate_finding_resolution_rejects_ancestor_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from workstate_handoff_mcp import preflight

    monkeypatch.setattr(preflight, "list_review_findings", lambda **kwargs: _finding_envelope("aaa1111"))
    # workspace HEAD is older than the finding's recorded commit -> ancestor
    monkeypatch.setattr(preflight, "_git_head_sha", lambda *a, **k: "bbb2222")
    monkeypatch.setattr(preflight, "_classify_commit_relation", lambda ref, cand: "ancestor")

    result = preflight.validate_finding_resolution(finding_id_or_db_id="WORKSTATE-REF-DEMO-001")

    assert result["ok"] is False
    assert result["suggested"]["reason"] == "descendant_commit_required"


def test_validate_finding_resolution_rejects_diverged_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    from workstate_handoff_mcp import preflight

    monkeypatch.setattr(preflight, "list_review_findings", lambda **kwargs: _finding_envelope("aaa1111"))
    monkeypatch.setattr(preflight, "_git_head_sha", lambda *a, **k: "ccc3333")
    monkeypatch.setattr(preflight, "_classify_commit_relation", lambda ref, cand: "diverged")

    result = preflight.validate_finding_resolution(finding_id_or_db_id="WORKSTATE-REF-DEMO-001", fixed_commit_sha="ccc3333")

    assert result["ok"] is False
    assert result["suggested"]["reason"] == "descendant_commit_required"


def test_validate_finding_resolution_accepts_same_or_descendant(monkeypatch: pytest.MonkeyPatch) -> None:
    from workstate_handoff_mcp import preflight

    monkeypatch.setattr(preflight, "list_review_findings", lambda **kwargs: _finding_envelope("aaa1111"))
    monkeypatch.setattr(preflight, "_git_head_sha", lambda *a, **k: "ddd4444")
    monkeypatch.setattr(preflight, "_classify_commit_relation", lambda ref, cand: "descendant")

    result = preflight.validate_finding_resolution(finding_id_or_db_id="WORKSTATE-REF-DEMO-001")

    assert result["ok"] is True
    assert result["suggested"]["commit_sha"] == "ddd4444"
