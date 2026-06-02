"""Unit + integration tests for the WORKSTATE-REF-70 implementation note checklist-sync
guardrails wired into ``review-ready`` (warn-only) and ``close-check``
(blocking reason).

``review-ready`` is the mid-loop gate; an evidence-backed unchecked
checklist item adds a ``checklist_sync_pending: …`` entry to
``warnings`` but never enters ``reasons``. ``close-check`` is the
merge gate; the same condition emits ``checklist_sync_pending`` as a
blocking reason. Both surfaces share one helper —
``review_ready._probe_checklist_sync_pending`` — and both fall through
silently when the plan lookup fails (no task_ref, no stored
``task_plan_path``, missing file, parse error).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
HANDLERS_DIR = LIFECYCLE_PKG / "handlers"

SAMPLE_PLAN = """\
# Sample Plan

## Consolidated Checklist

### Checklist for implementation note: handler + dispatch

- [ ] Added `packages/example/foo.py`.
- [ ] implementation note closed.
"""


@pytest.fixture(scope="module")
def handlers_pkg():
    import importlib.util

    if str(LIFECYCLE_PKG) not in sys.path:
        sys.path.insert(0, str(LIFECYCLE_PKG))

    handlers_init = HANDLERS_DIR / "__init__.py"
    spec_pkg = importlib.util.spec_from_file_location(
        "handlers", handlers_init, submodule_search_locations=[str(HANDLERS_DIR)]
    )
    assert spec_pkg is not None
    pkg = importlib.util.module_from_spec(spec_pkg)
    sys.modules["handlers"] = pkg
    assert spec_pkg.loader is not None
    spec_pkg.loader.exec_module(pkg)

    for name in (
        "_common",
        "sync_task_plan_checklist",
        "review_ready",
        "close_check",
    ):
        path = HANDLERS_DIR / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"handlers.{name}", path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"handlers.{name}"] = mod
        assert spec.loader is not None
        spec.loader.exec_module(mod)

    return {
        "sync": sys.modules["handlers.sync_task_plan_checklist"],
        "review_ready": sys.modules["handlers.review_ready"],
        "close_check": sys.modules["handlers.close_check"],
    }


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        check=True,
    )
    return repo


# ---------------------------------------------------------------------------
# Helper unit tests (in-process)
# ---------------------------------------------------------------------------


def test_probe_returns_none_when_task_ref_missing(handlers_pkg, tmp_path: Path) -> None:
    """No task_ref means the gate cannot have an opinion — degrade silently."""
    review_ready = handlers_pkg["review_ready"]
    repo = _make_repo(tmp_path)
    count, path = review_ready._probe_checklist_sync_pending(repo, None)
    assert count is None
    assert path is None


def test_probe_returns_none_when_lookup_fails(
    handlers_pkg, tmp_path: Path, monkeypatch
) -> None:
    """A failed plan-path lookup (no stored ``task_plan_path``, no MCP
    CLI, etc.) yields ``(None, None)`` so callers know not to block."""
    review_ready = handlers_pkg["review_ready"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(sync, "_lookup_stored_plan_path", lambda r, t: None)
    monkeypatch.setattr(sync, "resolve_workspace_root", lambda plan_path=None: repo)
    count, path = review_ready._probe_checklist_sync_pending(repo, "WORKSTATE-REF-70")
    assert count is None
    assert path is None


def test_probe_counts_tick_resolutions(
    handlers_pkg, tmp_path: Path, monkeypatch
) -> None:
    """Returns the count of items the resolver would flip (action=TICK)
    plus the plan path string. The implementation note file-path anchor matches; the
    bare ``implementation note closed.`` line does NOT match because no decision_id
    is registered for implementation note."""
    review_ready = handlers_pkg["review_ready"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    plan = repo / "WORKSTATE-REF-99-task-plan.md"
    plan.write_text(SAMPLE_PLAN)

    def fake_query(workspace_root, task_ref):
        ev = sync.Evidence(
            slice_changed_files={1: {"packages/example/foo.py"}},
            slice_basenames={1: {"foo.py"}},
            all_changed_files={"packages/example/foo.py"},
            all_basenames={"foo.py"},
        )
        return ev, "synced", None

    monkeypatch.setattr(sync, "_lookup_stored_plan_path", lambda r, t: str(plan))
    monkeypatch.setattr(sync, "resolve_workspace_root", lambda plan_path=None: repo)
    monkeypatch.setattr(sync, "_query_handoff_evidence", fake_query)

    count, path = review_ready._probe_checklist_sync_pending(repo, "WORKSTATE-REF-99")
    assert count == 1, "exactly one file-path anchor matches the recorded evidence"
    assert path == str(plan)


def test_probe_returns_zero_when_no_evidence(
    handlers_pkg, tmp_path: Path, monkeypatch
) -> None:
    review_ready = handlers_pkg["review_ready"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    plan = repo / "WORKSTATE-REF-99-task-plan.md"
    plan.write_text(SAMPLE_PLAN)

    monkeypatch.setattr(sync, "_lookup_stored_plan_path", lambda r, t: str(plan))
    monkeypatch.setattr(sync, "resolve_workspace_root", lambda plan_path=None: repo)
    monkeypatch.setattr(
        sync, "_query_handoff_evidence",
        lambda w, t: (sync.Evidence(), "pending", "cli_missing"),
    )

    count, path = review_ready._probe_checklist_sync_pending(repo, "WORKSTATE-REF-99")
    assert count == 0
    assert path == str(plan)


def test_probe_returns_none_when_plan_file_missing(
    handlers_pkg, tmp_path: Path, monkeypatch
) -> None:
    review_ready = handlers_pkg["review_ready"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    monkeypatch.setattr(
        sync, "_lookup_stored_plan_path",
        lambda r, t: str(repo / "no-such-plan.md"),
    )
    monkeypatch.setattr(sync, "resolve_workspace_root", lambda plan_path=None: repo)
    count, path = review_ready._probe_checklist_sync_pending(repo, "WORKSTATE-REF-99")
    assert count is None
    assert path is None


# ---------------------------------------------------------------------------
# Owner / next-command wiring
# ---------------------------------------------------------------------------


def test_checklist_sync_pending_classified_under_checklist_sync_owner(
    handlers_pkg,
) -> None:
    review_ready = handlers_pkg["review_ready"]
    grouped = review_ready.reasons_by_owner(["checklist_sync_pending"])
    assert grouped["checklist_sync"] == ["checklist_sync_pending"]
    # Other buckets stay empty so nothing else accidentally absorbs it.
    for bucket, items in grouped.items():
        if bucket == "checklist_sync":
            continue
        assert items == [], (bucket, items)


def test_next_command_routes_to_sync_when_checklist_pending(
    handlers_pkg,
) -> None:
    review_ready = handlers_pkg["review_ready"]
    grouped = review_ready.reasons_by_owner(["checklist_sync_pending"])
    hint = review_ready.next_command_for(
        command="close-check",
        reasons=["checklist_sync_pending"],
        grouped=grouped,
        derived_task_ref="WORKSTATE-REF-70",
    )
    assert hint["reason"] == "task_plan_checklist_evidence_backed_unchecked_items"
    assert "sync-task-plan-checklist" in hint["command"]
    assert "TASK=WORKSTATE-REF-70" in hint["command"]


# ---------------------------------------------------------------------------
# CLI integration: full fake-mcp shim exercising review-ready & close-check
# ---------------------------------------------------------------------------


def _write_full_fake_cli(
    target: Path,
    *,
    findings_json: str,
    tests_json: str,
    state_identity_json: str,
    state_decisions_json: str,
) -> None:
    """Fake CLI handling every subcommand the gate + checklist probe make.

    The probe uses two ``state`` calls — one with ``--sections identity``
    (returns ``task_plan_abs_path``) and one with ``--sections
    decisions_recent`` (returns the slice-close decisions whose
    ``changed_files_json`` drives the resolver's TICK verdicts). Argv
    inspection — not full argparse — so optional flags like
    ``--decision-fields decision changed_files_json`` cannot break the
    fake.
    """
    target.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "argv = sys.argv[1:]\n"
        "joined = ' '.join(argv)\n"
        "if 'review-findings' in argv:\n"
        f"    sys.stdout.write({findings_json!r})\n"
        "elif 'get-verified-tests' in argv:\n"
        f"    sys.stdout.write({tests_json!r})\n"
        "elif 'state' in argv:\n"
        "    if '--sections' in argv:\n"
        "        i = argv.index('--sections')\n"
        "        sections = argv[i+1] if i+1 < len(argv) else ''\n"
        "    else:\n"
        "        sections = ''\n"
        "    if 'decisions_recent' in sections:\n"
        f"        sys.stdout.write({state_decisions_json!r})\n"
        "    else:\n"
        f"        sys.stdout.write({state_identity_json!r})\n"
        "elif 'search-handoff' in argv:\n"
        "    sys.stdout.write(json.dumps({'ok': True, 'data': {'rows': []}}))\n"
        "else:\n"
        "    sys.stderr.write('unknown subcommand: ' + joined + '\\n')\n"
        "    sys.exit(2)\n"
    )
    target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _empty_findings_payload() -> str:
    return json.dumps({
        "ok": True,
        "data": {
            "counts": {
                "severity": {"high": 0, "medium": 0, "low": 0},
                "status": {"open": 0},
            },
            "findings": [],
        },
    })


def _empty_tests_payload() -> str:
    return json.dumps({"ok": True, "data": {"tests": []}})


def _make_feature_repo_with_plan(tmp_path: Path) -> tuple[Path, Path]:
    """Returns ``(repo, plan_path)`` with one commit on a feature branch
    plus a plan file the fake state CLI will point at."""
    repo = _make_repo(tmp_path)
    plan_dir = repo / "packages" / "example" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-99-example-task-plan.md"
    plan.write_text(SAMPLE_PLAN)
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature/WORKSTATE-99"],
        check=True,
    )
    (repo / "packages" / "example" / "foo.py").parent.mkdir(parents=True, exist_ok=True)
    (repo / "packages" / "example" / "foo.py").write_text("# stub\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "feat: x", "-q"],
        check=True,
    )
    return repo, plan


def _run_lifecycle(
    cwd: Path, command: str, *extra: str, fake_cli: Path
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = str(fake_cli)
    return subprocess.run(
        [sys.executable, str(LIFECYCLE_PKG), command, *extra],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _identity_payload(plan: Path) -> str:
    return json.dumps({
        "ok": True,
        "data": {
            "active": {
                "task_ref": "WORKSTATE-REF-99",
                "task_plan_abs_path": str(plan),
                "task_plan_path": "packages/example/docs/tasks/WORKSTATE-REF-99-example-task-plan.md",
            },
        },
    })


def _decisions_recent_payload(*, with_match: bool) -> str:
    rows: list = []
    if with_match:
        rows.append({
            "decision": "codex_slice_complete_WORKSTATE-99_slice_1",
            "changed_files_json": json.dumps(["packages/example/foo.py"]),
        })
    return json.dumps({"ok": True, "data": {"decisions_recent": rows}})


def test_review_ready_emits_warning_when_checklist_sync_pending(
    tmp_path: Path,
) -> None:
    """Mid-loop guardrail: a plan with an evidence-backed `- [ ]` item
    surfaces as a ``warnings`` entry; ``reasons`` does NOT carry
    ``checklist_sync_pending`` (warn-only on the mid-loop gate)."""
    repo, plan = _make_feature_repo_with_plan(tmp_path)
    fake_cli = tmp_path / "fake-mcp"
    _write_full_fake_cli(
        fake_cli,
        findings_json=_empty_findings_payload(),
        tests_json=_empty_tests_payload(),
        state_identity_json=_identity_payload(plan),
        state_decisions_json=_decisions_recent_payload(with_match=True),
    )

    proc = _run_lifecycle(repo, "review-ready", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "checklist_sync_pending" not in receipt["reasons"], receipt
    assert any(
        "checklist_sync_pending" in w for w in receipt["warnings"]
    ), receipt["warnings"]


def test_close_check_blocks_when_checklist_sync_pending(tmp_path: Path) -> None:
    """Merge-gate guardrail: same evidence shape adds
    ``checklist_sync_pending`` to ``reasons`` and flips ``ready`` to
    false. Routes under the ``checklist_sync`` owner bucket."""
    repo, plan = _make_feature_repo_with_plan(tmp_path)
    fake_cli = tmp_path / "fake-mcp"
    _write_full_fake_cli(
        fake_cli,
        findings_json=_empty_findings_payload(),
        tests_json=_empty_tests_payload(),
        state_identity_json=_identity_payload(plan),
        state_decisions_json=_decisions_recent_payload(with_match=True),
    )

    proc = _run_lifecycle(repo, "close-check", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "checklist_sync_pending" in receipt["reasons"], receipt
    assert receipt["ready"] is False, receipt
    assert receipt["reasons_by_owner"]["checklist_sync"] == ["checklist_sync_pending"]


def test_close_check_does_not_block_when_lookup_fails(tmp_path: Path) -> None:
    """When the active row has no ``task_plan_abs_path`` / ``task_plan_path``
    the probe returns ``(None, None)`` and close-check does NOT add
    ``checklist_sync_pending`` — degraded MCP must not block merges."""
    repo, _plan = _make_feature_repo_with_plan(tmp_path)
    fake_cli = tmp_path / "fake-mcp"
    no_plan_identity = json.dumps({
        "ok": True,
        "data": {"active": {"task_ref": "WORKSTATE-REF-99"}},
    })
    _write_full_fake_cli(
        fake_cli,
        findings_json=_empty_findings_payload(),
        tests_json=_empty_tests_payload(),
        state_identity_json=no_plan_identity,
        state_decisions_json=_decisions_recent_payload(with_match=False),
    )

    proc = _run_lifecycle(repo, "close-check", "--json", fake_cli=fake_cli)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert "checklist_sync_pending" not in receipt["reasons"], receipt
