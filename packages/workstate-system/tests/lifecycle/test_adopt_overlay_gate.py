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
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"
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

    monkeypatch.setattr(
        task_start, "_adopt_overlay_command", lambda **_: ["fake-adopt"]
    )
    monkeypatch.setattr(task_start._common, "run_subprocess", _fake_run)
    return calls


def test_default_adopt_command_prefers_worktree_venv_script(
    tmp_path, monkeypatch
) -> None:
    """Source checkouts should adopt with the freshly provisioned worktree venv
    package instead of an older published ``uvx`` package."""
    monkeypatch.delenv("WORKSTATE_ADOPT_CMD", raising=False)
    wt = tmp_path / "wt"
    script = wt / ".venv" / "bin" / "workstate-bootstrap"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env sh\n")
    script.chmod(0o755)

    assert task_start._adopt_overlay_command(worktree_path=wt) == [
        str(script),
        "adopt-worktree",
    ]


def test_default_adopt_command_falls_back_to_uvx_without_venv_script(
    tmp_path, monkeypatch
) -> None:
    """The core 'prefer-when-available, then fall back' promise: with no
    worktree ``.venv`` console script present, the default ``uvx`` command is
    used unchanged."""
    monkeypatch.delenv("WORKSTATE_ADOPT_CMD", raising=False)
    wt = tmp_path / "wt"
    wt.mkdir()

    assert task_start._adopt_overlay_command(worktree_path=wt) == list(
        task_start._DEFAULT_ADOPT_CMD
    )


def test_default_adopt_command_without_worktree_uses_uvx(monkeypatch) -> None:
    """No ``worktree_path`` (and no override) resolves to the ``uvx`` default."""
    monkeypatch.delenv("WORKSTATE_ADOPT_CMD", raising=False)

    assert task_start._adopt_overlay_command() == list(task_start._DEFAULT_ADOPT_CMD)


def test_default_adopt_command_ignores_non_executable_venv_script(
    tmp_path, monkeypatch
) -> None:
    """A present-but-non-executable venv script must NOT be selected; the
    resolver falls through to the ``uvx`` default so an unspawnable path can
    never raise out of the best-effort adopt."""
    monkeypatch.delenv("WORKSTATE_ADOPT_CMD", raising=False)
    wt = tmp_path / "wt"
    script = wt / ".venv" / "bin" / "workstate-bootstrap"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env sh\n")
    script.chmod(0o644)  # readable but not executable

    assert task_start._adopt_overlay_command(worktree_path=wt) == list(
        task_start._DEFAULT_ADOPT_CMD
    )


def test_adopt_command_empty_env_disables(tmp_path, monkeypatch) -> None:
    """``WORKSTATE_ADOPT_CMD=''`` disables adoption (empty argv) even when a
    worktree venv script is present."""
    wt = tmp_path / "wt"
    script = wt / ".venv" / "bin" / "workstate-bootstrap"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env sh\n")
    script.chmod(0o755)
    monkeypatch.setenv("WORKSTATE_ADOPT_CMD", "")

    assert task_start._adopt_overlay_command(worktree_path=wt) == []


def test_adopt_command_env_override_still_wins(tmp_path, monkeypatch) -> None:
    """Explicit operators keep control over disabling or replacing auto-adopt."""
    wt = tmp_path / "wt"
    script = wt / ".venv" / "bin" / "workstate-bootstrap"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env sh\n")
    monkeypatch.setenv("WORKSTATE_ADOPT_CMD", "/tmp/custom-adopt --flag")

    assert task_start._adopt_overlay_command(worktree_path=wt) == [
        "/tmp/custom-adopt",
        "--flag",
    ]


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
