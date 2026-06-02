"""implementation note S4 — `_adopt_overlay` gate alignment (finding
revC-nested-source-marker-gate-mismatch).

The task-start self-heal gate must resolve the overlay root by the SAME upward
walk workstate-bootstrap's ``primary_overlay_root`` uses (preferring a
materialized overlay), so a nested-source layout (the git repo lives inside the
overlay dir, marker is an ancestor) is healed instead of skipped. It must still
skip the monorepo self-host case (tracked marker, no ``.workstate/remote``
clone) so it never spawns a doomed adopt subprocess.

These target the gate helper directly (not the CLI); sys.path-prepend the
lifecycle package to import ``handlers.task_start`` (per WORKSTATE-REF-66 PR-04).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
if str(LIFECYCLE_PKG) not in sys.path:
    sys.path.insert(0, str(LIFECYCLE_PKG))

from handlers import task_start  # noqa: WORKSTATE-REF-402


def _patch_adopt_subprocess(monkeypatch) -> list[list[str]]:
    """Make ``_adopt_overlay_command`` return a fake cmd and record every
    ``run_subprocess`` invocation; returns the recording list."""
    calls: list[list[str]] = []

    def _fake_run(argv, timeout=None):  # type: ignore[no-untyped-def]
        calls.append(list(argv))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(task_start, "_adopt_overlay_command", lambda: ["fake-adopt"])
    monkeypatch.setattr(task_start._common, "run_subprocess", _fake_run)
    return calls


def test_gate_heals_nested_source_marker_above_primary(tmp_path, monkeypatch) -> None:
    """Nested-source: a materialized overlay one level ABOVE the primary git
    root is resolved and adopted (not skipped)."""
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / task_start._BOOTSTRAP_MARKER).write_text("{}\n")
    (overlay / ".workstate" / "remote").mkdir(parents=True)
    primary = overlay / "repo"  # primary git root carries no marker of its own
    primary.mkdir()

    calls = _patch_adopt_subprocess(monkeypatch)
    wt = tmp_path / "wt"

    result = task_start._adopt_overlay(primary, wt)

    assert result == {"adopted": True, "skipped": None}
    assert calls and calls[0][-2:] == ["--target", str(wt)]


def test_gate_skips_marker_without_clone(tmp_path, monkeypatch) -> None:
    """Monorepo self-host: a tracked marker with NO clone must skip without
    spawning the (doomed) adopt subprocess (revC-monorepo-marker-without-clone)."""
    primary = tmp_path / "repo"
    primary.mkdir()
    (primary / task_start._BOOTSTRAP_MARKER).write_text("{}\n")

    calls = _patch_adopt_subprocess(monkeypatch)

    result = task_start._adopt_overlay(primary, tmp_path / "wt")

    assert result == {"adopted": False, "skipped": "no_overlay_clone"}
    assert not calls


def test_gate_skips_when_no_marker_anywhere(tmp_path, monkeypatch) -> None:
    """A non-bootstrap primary (no marker at/above it) skips, no subprocess."""
    primary = tmp_path / "repo"
    primary.mkdir()

    calls = _patch_adopt_subprocess(monkeypatch)

    result = task_start._adopt_overlay(primary, tmp_path / "wt")

    assert result == {"adopted": False, "skipped": "no_overlay_marker"}
    assert not calls
