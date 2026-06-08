"""TDD gate for implementation note Slice S2: worktree-aware doctor/repair + adopt CLI.

A bare linked worktree (overlay absent) must produce a SINGLE
``unadopted_worktree`` doctor finding that short-circuits the
``missing_clone`` + ``surface_drift`` storm (whose naive repair would target the
worktree's own absent clone). ``repair`` routes that finding to
``adopt_worktree``. The ``adopt-worktree`` CLI is the steady-state surface:
``--check`` exits 1 on drift, 0 when adopted.

Adoption state is keyed on the CLONE redirect (``.workstate/remote``), not the
(tracked) marker — the marker survives into a worktree via git, the clone does
not.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from workstate_bootstrap.subcommands import doctor, repair

MARKER = ".workstate-bootstrap.json"
PLAIN_SURFACES = (".github/hooks", "docs/workstate/contracts", "docs/workstate/rules")


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
        cwd=str(cwd),
        timeout=30,
    )
    return result.stdout.strip()


def _make_installed_primary(root: Path) -> Path:
    """Primary repo with a TRACKED marker, a gitignored .workstate clone, and
    enough surface content for adopt to materialize."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "--initial-branch=main", cwd=root)
    _git("config", "user.email", "test@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)

    clone = root / ".workstate" / "remote"
    (clone / ".git").mkdir(parents=True)  # looks like a real clone to doctor
    for surface in PLAIN_SURFACES:
        d = clone / surface
        d.mkdir(parents=True)
        (d / "MARKER.md").write_text(f"shared {surface}\n")
    hooks_git = clone / "scripts" / "hooks" / "git"
    hooks_git.mkdir(parents=True)
    (hooks_git / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    mkd = clone / "Makefile.d"
    mkd.mkdir(parents=True)
    (mkd / "common.mk").write_text("# common\n")
    (mkd / "lifecycle.mk").write_text("# lifecycle\n")
    sw = clone / "scripts" / "workstate"
    (sw / "lifecycle").mkdir(parents=True)
    (sw / "lifecycle" / "runner.py").write_text("# runner\n")
    (sw / "other.py").write_text("# other\n")
    gen = root / ".workstate" / "generated"
    gen.mkdir(parents=True)
    (gen / "PLUGINS.md").write_text("generated\n")

    # Marker is TRACKED (survives into worktrees); .workstate is gitignored.
    (root / MARKER).write_text('{"surfaces": []}\n')
    (root / ".gitignore").write_text(".workstate/\n")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "seed", cwd=root)
    return root


def _add_worktree(primary: Path, wt: Path) -> Path:
    _git("worktree", "add", str(wt), cwd=primary)
    return wt


# ---------------------------------------------------------------------------
# doctor short-circuit
# ---------------------------------------------------------------------------


def test_doctor_reports_single_unadopted_worktree_finding(tmp_path: Path) -> None:
    primary = _make_installed_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt")
    # Bare worktree: marker present (tracked), clone absent (gitignored).
    assert (wt / MARKER).exists()
    assert not (wt / ".workstate" / "remote").exists()

    findings = doctor(target=wt)

    kinds = [f["kind"] for f in findings]
    assert kinds == ["unadopted_worktree"], kinds
    # The storm is suppressed.
    assert "missing_clone" not in kinds
    assert "surface_drift" not in kinds
    assert findings[0]["path"] == str(primary.resolve())


def test_doctor_no_unadopted_finding_after_adopt(tmp_path: Path) -> None:
    from workstate_bootstrap.adopt import adopt_worktree

    primary = _make_installed_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt")
    adopt_worktree(target=wt, primary=primary)

    findings = doctor(target=wt)
    assert "unadopted_worktree" not in {f["kind"] for f in findings}


def test_doctor_primary_is_not_unadopted(tmp_path: Path) -> None:
    primary = _make_installed_primary(tmp_path / "primary")
    findings = doctor(target=primary)
    assert "unadopted_worktree" not in {f["kind"] for f in findings}


# ---------------------------------------------------------------------------
# repair routes to adopt
# ---------------------------------------------------------------------------


def test_repair_adopts_unadopted_worktree(tmp_path: Path) -> None:
    primary = _make_installed_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt")

    report = repair(target=wt)

    assert any(f["kind"] == "unadopted_worktree" for f in report["repaired"])
    # The worktree is now adopted: clone redirect + a surface symlink exist.
    assert (wt / ".workstate" / "remote").exists()
    assert (wt / "docs" / "workstate" / "rules").is_symlink()
    # And doctor is quiet about adoption afterwards.
    assert "unadopted_worktree" not in {f["kind"] for f in doctor(target=wt)}


# ---------------------------------------------------------------------------
# adopt-worktree CLI
# ---------------------------------------------------------------------------


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "workstate_bootstrap", *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_cli_adopt_worktree_check_exit_codes(tmp_path: Path) -> None:
    primary = _make_installed_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt")

    # --check on an unadopted worktree: drift -> exit 1.
    pre = _cli("adopt-worktree", "--target", str(wt), "--check")
    assert pre.returncode == 1, pre.stdout + pre.stderr

    # Apply.
    applied = _cli("adopt-worktree", "--target", str(wt))
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert (wt / ".workstate" / "remote").exists()

    # --check after adopt: clean -> exit 0.
    post = _cli("adopt-worktree", "--target", str(wt), "--check")
    assert post.returncode == 0, post.stdout + post.stderr


def test_cli_adopt_worktree_json(tmp_path: Path) -> None:
    import json

    primary = _make_installed_primary(tmp_path / "primary")
    wt = _add_worktree(primary, tmp_path / "wt")
    result = _cli("adopt-worktree", "--target", str(wt), "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    receipt = json.loads(result.stdout)
    assert receipt["adopted"] is True
    assert receipt["primary"] == str(primary.resolve())
