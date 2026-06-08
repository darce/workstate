"""WORKSTATE-REF-54 implementation note sub-implementation note.6: ``snapshot_is_live_for_task`` helper.

Under the v2 ``workspace_ambiguous`` shape, ``CURRENT_TASK.json`` no
longer carries a single ``active`` block — it lists multiple per-task
projections. To answer "is this specific ``task_ref`` still live work?"
without picking one of the candidates ad-hoc, migrated readers consult
the per-task projection file directly at
``<workspace_root>/.task-state/current/<task_ref>.json``.

Contract under test:

- The per-task file's ``status`` field is the source of truth for
  liveness; it is checked against ``live_active_statuses()`` (the same
  vocabulary the legacy ``snapshot_is_live(active)`` uses).
- A missing per-task file means there is no active projection for that
  ``task_ref`` — the helper returns ``False``. This matches the writer
  contract: the projection is reaped on ``archive`` and absent before
  any ``set_handoff_state`` write.
- A per-task file without a ``status`` field is treated as live, so
  the helper inherits the legacy conservative-fallback behavior the
  ambiguity guard already relies on.
- Corrupt JSON yields ``False`` (fail-safe non-live). The compat
  reader hard-stops on corrupt JSON for the workspace summary because
  that file is operator-actionable; per-task projections are reaped
  and rewritten by every task-affecting MCP write, so degrading to
  "not live" here surfaces as a one-tick miss in the worst case
  rather than a hidden mis-bind.

Back-compat: ``snapshot_is_live(active)`` MUST remain importable and
behaviorally unchanged so the v1 ``single``-shape readers
(``context.py``, ``task_start.py``) continue to work until the
implementation note writer flip. This file pins both halves of that contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_LIFECYCLE_PKG = _PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"
if str(_LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(_LIFECYCLE_PKG))

from workstate.lifecycle.handlers._common import (  # noqa: WORKSTATE-REF-402
    snapshot_is_live,
    snapshot_is_live_for_task,
)


def _per_task_dir(workspace_root: Path) -> Path:
    """Mirror the writer-side path convention the per-task projection
    uses: ``<workspace_root>/.task-state/current/``. Centralized here
    so a single-source-of-truth change (e.g. relocating ``state_dir``)
    only touches one place in the test module."""
    return workspace_root / ".task-state" / "current"


def _write_per_task(
    workspace_root: Path,
    task_ref: str,
    *,
    status: str | None,
    extra: dict | None = None,
) -> Path:
    target_dir = _per_task_dir(workspace_root)
    target_dir.mkdir(parents=True, exist_ok=True)
    payload: dict = {
        "task_projection_schema_version": 1,
        "task_ref": task_ref,
        "objective": f"objective-{task_ref}",
        "focus": None,
        "target_branch": f"feature/{task_ref.lower()}",
        "target_worktree_path": None,
        "task_plan_path": None,
        "revision": 1,
        "updated_at": "2026-05-10T00:00:00Z",
    }
    if status is not None:
        payload["status"] = status
    if extra:
        payload.update(extra)
    target = target_dir / f"{task_ref}.json"
    target.write_text(json.dumps(payload, sort_keys=True, indent=2))
    return target


def test_snapshot_is_live_for_task_in_progress_is_live(tmp_path: Path) -> None:
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="in_progress")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is True


def test_snapshot_is_live_for_task_review_is_live(tmp_path: Path) -> None:
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="review")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is True


def test_snapshot_is_live_for_task_blocked_is_live(tmp_path: Path) -> None:
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="blocked")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is True


def test_snapshot_is_live_for_task_done_is_not_live(tmp_path: Path) -> None:
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="done")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is False


def test_snapshot_is_live_for_task_paused_is_not_live(tmp_path: Path) -> None:
    """``paused`` is intentionally non-live — it sits outside
    ``LIVE_ACTIVE_STATUSES`` (``in_progress``, ``review``, ``blocked``).
    A paused task must not pass the ambiguity guard."""
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="paused")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is False


def test_snapshot_is_live_for_task_abandoned_is_not_live(tmp_path: Path) -> None:
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="abandoned")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is False


def test_snapshot_is_live_for_task_missing_file_is_not_live(tmp_path: Path) -> None:
    """No per-task projection on disk → no live work for that task_ref.

    The writer reaps the file on ``archive``; the absence is therefore
    the canonical "not live anymore" signal. Returning False keeps
    readers from trying to re-bind to an archived task_ref."""
    assert not (_per_task_dir(tmp_path) / "WORKSTATE-REF-54.json").exists()
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is False


def test_snapshot_is_live_for_task_status_absent_is_conservative_live(
    tmp_path: Path,
) -> None:
    """A per-task projection without ``status`` is treated as live so
    the helper inherits ``snapshot_is_live(active)``'s conservative
    fallback. Pre-status writers should never reach this code path in
    practice (the v1 schema always writes ``status``), but pinning the
    semantic prevents a future writer regression from silently turning
    every read into "not live"."""
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status=None)
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is True


def test_snapshot_is_live_for_task_empty_status_is_conservative_live(
    tmp_path: Path,
) -> None:
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is True


def test_snapshot_is_live_for_task_corrupt_json_is_not_live(tmp_path: Path) -> None:
    """Corrupt JSON is fail-safe non-live. Per-task projections are
    rewritten on every task-affecting MCP write, so a one-tick "not
    live" reading is preferable to raising into reader callers that
    have no degrade-with-warning surface for this file."""
    target_dir = _per_task_dir(tmp_path)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "WORKSTATE-REF-54.json").write_text("{not valid json")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is False


def test_snapshot_is_live_for_task_does_not_consult_workspace_summary(
    tmp_path: Path,
) -> None:
    """Helper reads the per-task file, NOT ``CURRENT_TASK.json``. A
    workspace summary that disagrees with the per-task projection must
    not influence the verdict — the per-task file is the source of
    truth under ``workspace_ambiguous`` (where the summary has no
    single ``active`` block to consult)."""
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="in_progress")
    # Stale workspace summary claiming the task is done.
    summary_payload = {
        "schema_version": 1,
        "active": {"task_ref": "WORKSTATE-REF-54", "status": "done"},
    }
    (tmp_path / "CURRENT_TASK.json").write_text(json.dumps(summary_payload))
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is True


def test_snapshot_is_live_for_task_isolates_task_refs(tmp_path: Path) -> None:
    """The helper must select by ``task_ref``: a live projection for
    task A must not bleed into a query for task B. This is the
    workspace_ambiguous discriminator the helper exists to enable."""
    _write_per_task(tmp_path, "WORKSTATE-REF-54", status="in_progress")
    _write_per_task(tmp_path, "WORKSTATE-REF-OTHER", status="done")
    assert snapshot_is_live_for_task("WORKSTATE-REF-54", tmp_path) is True
    assert snapshot_is_live_for_task("WORKSTATE-REF-OTHER", tmp_path) is False


def test_snapshot_is_live_for_task_rejects_path_traversal(tmp_path: Path) -> None:
    """Defense-in-depth: a hostile ``task_ref`` containing path
    separators, a leading dot, NUL, or empty must return False without
    touching the filesystem outside the projection directory. Upstream
    handoff writers validate ``^[A-Z][A-Z0-9_-]+$`` before this helper
    is reached, but a regression in that validation must not turn the
    helper into a disk-traversal surface — the guard pins fail-safe
    non-live for hostile input regardless of what sits at the resolved
    path."""
    sentinel = tmp_path / "outside.json"
    sentinel.write_text(
        json.dumps({"status": "in_progress", "task_projection_schema_version": 1})
    )
    for hostile in (
        "..",
        "../outside",
        "../../etc/passwd",
        ".hidden",
        "WORKSTATE-REF/54",
        "WORKSTATE-REF\\54",
        "WORKSTATE-REF\x0054",
        "",
    ):
        assert snapshot_is_live_for_task(hostile, tmp_path) is False


# ---------------------------------------------------------------------------
# Back-compat: ``snapshot_is_live(active)`` must keep working for the v1
# single-block reader callsites (``context.py``, ``task_start.py``)
# until implementation note retires the writer. The plan calls this out explicitly
# at *implementation note -> snapshot_is_live_for_task back-compat preservation*. We pin
# the import + behavior here so the retirement ordering is auditable.
# ---------------------------------------------------------------------------


def test_legacy_snapshot_is_live_still_imports_and_passes_in_progress() -> None:
    assert snapshot_is_live({"status": "in_progress"}) is True


def test_legacy_snapshot_is_live_still_rejects_done_status() -> None:
    assert snapshot_is_live({"status": "done"}) is False


def test_legacy_snapshot_is_live_treats_missing_status_as_live() -> None:
    """The conservative-fallback semantic the ambiguity guard depends on
    must survive sub-implementation note.6. A regression here would let stale
    snapshots silently pass the guard."""
    assert snapshot_is_live({}) is True
