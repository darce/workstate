"""WORKSTATE-REF-54-FU implementation note: lifecycle readers derive workspace summary on read.

Locks the contract that four lifecycle handler readers no longer trust
the on-disk ``CURRENT_TASK.json`` — they instead derive the workspace
summary on each call by shelling out to
``mcp-workstate-handoff render-handoff --kind=current_task --no-write`` and
parsing the envelope's ``current_task_json`` field through
``load_workspace_summary_compat``.

The four readers under test:

- ``task_start._read_workspace_summary_view`` — full view for the
  ambiguity guard.
- ``context._read_active_state`` — ``active`` block for plan-path
  lookup.
- ``task_finish._read_active_task_ref`` — singular ``task_ref`` for the
  close sequence.
- ``shell_out._read_active_task_ref`` — singular ``task_ref`` for the
  CURRENT_TASK fallback after the cwd-keyed sqlite probe misses.

Each test writes a *stale* ``CURRENT_TASK.json`` to disk that claims a
different live task than what the live MCP state holds, then patches
the handoff CLI subprocess to return a synthetic ``render_handoff``
envelope reflecting the live truth. A correctly migrated reader returns
the live truth; a reader still trusting the on-disk file returns the
stale ``task_ref`` (and the assertions fail loudly with the stale
value).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_LIFECYCLE_PKG = _PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
if str(_LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(_LIFECYCLE_PKG))

from workstate.lifecycle.handlers import _common  # noqa: WORKSTATE-REF-402
from workstate.lifecycle.handlers import context as context_handler  # noqa: WORKSTATE-REF-402
from workstate.lifecycle.handlers import shell_out as shell_out_handler  # noqa: WORKSTATE-REF-402
from workstate.lifecycle.handlers import task_finish as task_finish_handler  # noqa: WORKSTATE-REF-402
from workstate.lifecycle.handlers import task_start as task_start_handler  # noqa: WORKSTATE-REF-402


_STALE_TASK_REF = "STALE-TASK-ON-DISK"
_LIVE_TASK_REF = "LIVE-TASK-FROM-MCP"


def _stale_v2_workspace_summary(task_ref: str) -> dict:
    return {
        "schema_version": 2,
        "shape": "single",
        "task_ref": task_ref,
        "active": {
            "task_ref": task_ref,
            "status": "in_progress",
            "objective": "stale projection on disk",
            "focus": "stale focus",
            "target_branch": f"feature/{task_ref.lower()}",
            "target_worktree_path": "/tmp/stale-wt",
            "task_plan_path": f"docs/tasks/{task_ref}.md",
            "revision": 1,
            "updated_at": "2026-05-10T00:00:00",
        },
        "tasks": [],
    }


def _live_v2_workspace_summary(task_ref: str | None) -> dict:
    if task_ref is None:
        return {
            "schema_version": 2,
            "shape": "none",
            "task_ref": None,
            "active": None,
            "tasks": [],
        }
    return {
        "schema_version": 2,
        "shape": "single",
        "task_ref": task_ref,
        "active": {
            "task_ref": task_ref,
            "status": "in_progress",
            "objective": "live MCP state",
            "focus": "live focus",
            "target_branch": f"feature/{task_ref.lower()}",
            "target_worktree_path": "/tmp/live-wt",
            "task_plan_path": f"docs/tasks/{task_ref}.md",
            "revision": 7,
            "updated_at": "2026-05-17T00:00:00",
        },
        "tasks": [],
    }


def _render_handoff_envelope(summary: dict, write_file: bool = False) -> dict:
    return {
        "schema_version": 2,
        "tool": "render_handoff",
        "ok": True,
        "scope": {"task_ref": summary.get("task_ref")},
        "data": {
            "task_ref": summary.get("task_ref"),
            "path": "/tmp/CURRENT_TASK.json",
            "written": write_file,
            "current_task_json": json.dumps(summary),
        },
        "artifacts": [],
    }


@pytest.fixture()
def stale_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Workspace whose on-disk CURRENT_TASK.json is stale relative to MCP."""

    (tmp_path / "CURRENT_TASK.json").write_text(
        json.dumps(_stale_v2_workspace_summary(_STALE_TASK_REF))
    )

    # Pin canonical workspace resolution to tmp_path so readers do not
    # walk to the real repo root.
    import resolver  # type: ignore[import-not-found]

    monkeypatch.setattr(resolver, "canonical_workspace_root", lambda _repo: tmp_path)

    return tmp_path


def _install_live_render_handoff_stub(
    monkeypatch: pytest.MonkeyPatch,
    live_task_ref: str | None,
) -> list[list[str]]:
    """Patch ``_common.run_subprocess`` to answer render-handoff calls.

    Returns a captured-argv list so individual tests can assert the
    correct CLI invocation was used. Non-render-handoff probes (e.g.
    sqlite-less fallbacks) pass through to a 127 stub so the reader has
    nowhere else to source state from — the live truth must come from
    the patched render-handoff response.
    """
    envelope = _render_handoff_envelope(_live_v2_workspace_summary(live_task_ref))
    captured: list[list[str]] = []

    def _stub(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        captured.append(list(argv))
        if any("render-handoff" in token for token in argv):
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=json.dumps(envelope),
                stderr="",
            )
        return subprocess.CompletedProcess(args=argv, returncode=127, stdout="", stderr="")

    monkeypatch.setattr(_common, "run_subprocess", _stub)
    return captured


def test_task_start_reader_derives_live_state(
    stale_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_live_render_handoff_stub(monkeypatch, _LIVE_TASK_REF)

    view = task_start_handler._read_workspace_summary_view(stale_workspace)

    assert view.shape == "single"
    assert view.task_ref == _LIVE_TASK_REF, (
        f"task_start reader returned stale task_ref={view.task_ref!r} — "
        "expected live MCP state. Reader is still trusting CURRENT_TASK.json."
    )
    assert view.task_ref != _STALE_TASK_REF
    assert any(
        "render-handoff" in token for argv in captured for token in argv
    ), "task_start reader did not invoke render-handoff at all"


def test_context_reader_derives_live_state(
    stale_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_live_render_handoff_stub(monkeypatch, _LIVE_TASK_REF)

    active = context_handler._read_active_state(stale_workspace)

    assert active.get("task_ref") == _LIVE_TASK_REF, (
        f"context reader returned stale active.task_ref={active.get('task_ref')!r} — "
        "expected live MCP state."
    )
    assert any(
        "render-handoff" in token for argv in captured for token in argv
    ), "context reader did not invoke render-handoff at all"


def test_task_finish_reader_derives_live_state(
    stale_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_live_render_handoff_stub(monkeypatch, _LIVE_TASK_REF)

    task_ref = task_finish_handler._read_active_task_ref(stale_workspace)

    assert task_ref == _LIVE_TASK_REF, (
        f"task_finish reader returned stale task_ref={task_ref!r} — "
        "expected live MCP state."
    )
    assert any(
        "render-handoff" in token for argv in captured for token in argv
    ), "task_finish reader did not invoke render-handoff at all"


def test_shell_out_reader_derives_live_state(
    stale_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_live_render_handoff_stub(monkeypatch, _LIVE_TASK_REF)

    task_ref = shell_out_handler._read_active_task_ref(stale_workspace)

    assert task_ref == _LIVE_TASK_REF, (
        f"shell_out reader returned stale task_ref={task_ref!r} — "
        "expected live MCP state."
    )
    assert any(
        "render-handoff" in token for argv in captured for token in argv
    ), "shell_out reader did not invoke render-handoff at all"


def test_readers_collapse_to_none_when_mcp_reports_no_live_state(
    stale_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale on-disk file claims a live task; MCP says no active task.

    A correctly migrated reader treats the MCP-derived ``shape='none'``
    response as authoritative and reports "no active task," not the
    stale file contents.
    """
    _install_live_render_handoff_stub(monkeypatch, None)

    view = task_start_handler._read_workspace_summary_view(stale_workspace)
    assert view.shape == "none", (
        f"task_start reader returned shape={view.shape!r} task_ref={view.task_ref!r} — "
        "expected 'none' from live MCP."
    )
    assert context_handler._read_active_state(stale_workspace) == {}
    assert task_finish_handler._read_active_task_ref(stale_workspace) is None
    assert shell_out_handler._read_active_task_ref(stale_workspace) is None


def test_readers_fail_open_when_handoff_cli_unavailable(
    stale_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI 127 → fail-open ``shape='none'`` (matches prior soft-fail).

    Pinning this preserves the historical degrade-on-error semantics:
    a missing handoff CLI must not raise into lifecycle handlers; the
    readers collapse to "no active task" so callers degrade gracefully.
    """

    def _stub(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args=argv, returncode=127, stdout="", stderr="not found")

    monkeypatch.setattr(_common, "run_subprocess", _stub)

    view = task_start_handler._read_workspace_summary_view(stale_workspace)
    assert view.shape == "none"
    assert context_handler._read_active_state(stale_workspace) == {}
    assert task_finish_handler._read_active_task_ref(stale_workspace) is None
    assert shell_out_handler._read_active_task_ref(stale_workspace) is None
