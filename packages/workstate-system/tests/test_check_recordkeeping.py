"""implementation note implementation note — recordkeeping discipline lint."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
RK_CHECK = PACKAGE_ROOT / "scripts" / "check_recordkeeping.py"
RK_SPEC = importlib.util.spec_from_file_location("check_recordkeeping", RK_CHECK)
assert RK_SPEC is not None and RK_SPEC.loader is not None
check_recordkeeping = importlib.util.module_from_spec(RK_SPEC)
RK_SPEC.loader.exec_module(check_recordkeeping)


def _run_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(RK_CHECK), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def _write_skill_body(root: Path, slug: str, body: str) -> None:
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "body.md").write_text(body, encoding="utf-8")


def test_lint_rejects_per_file_write_record_decision(tmp_path: Path) -> None:
    _write_skill_body(
        tmp_path,
        "tdd",
        """## Core Process

1. After each file write, call `record_decision(decision='file_touched_x.py', rationale=...)` so the dashboard reflects every edit.
""",
    )
    proc = _run_check(tmp_path)
    assert proc.returncode == 1
    assert "tdd/body.md" in proc.stderr
    assert "record_decision" in proc.stderr


def test_lint_rejects_make_dashboard_in_cold_start_block(tmp_path: Path) -> None:
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, run `make dashboard` to refresh the view, then `load_session(...)`.
""",
    )
    proc = _run_check(tmp_path)
    assert proc.returncode == 1
    assert "handoff-lifecycle/body.md" in proc.stderr
    assert "make dashboard" in proc.stderr


def test_lint_accepts_slice_boundary_record_decision(tmp_path: Path) -> None:
    _write_skill_body(
        tmp_path,
        "incremental-implementation",
        """## Core Process

1. Close the slice via `close_slice(...)` which records the slice-complete decision atomically. Do not call `record_decision` per file write.
""",
    )
    proc = _run_check(tmp_path)
    assert proc.returncode == 0, proc.stderr
