"""Unit tests for ``task-plan-checklist-backfill`` (WORKSTATE-REF-70 implementation note).

The backfill is the writer-side counterpart of the audit handler. It
runs sync_task_plan_checklist's parse/resolve/apply pipeline across one
or more plans and — under ``--apply`` — flips the boxes the evidence
supports. These tests pin: dry-run default, APPLY=1 mutation, byte-
preserving rewrite, the WORKSTATE-REF-60 collision invariant
(``suppress_bare_slice_refs``), Stretch never auto-ticks, and the
no-untick guarantee.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "scripts" / "workstate" / "lifecycle"
HANDLERS_DIR = LIFECYCLE_PKG / "handlers"


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
        "task_plan_checklist_audit",
        "task_plan_checklist_backfill",
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
        "audit": sys.modules["handlers.task_plan_checklist_audit"],
        "backfill": sys.modules["handlers.task_plan_checklist_backfill"],
    }


SAMPLE_PLAN = """\
# Sample Plan

## Consolidated Checklist

## Context and Ownership

- [ ] Loaded `TASK_PLAN.template.md` and reviewed `lifecycle.mk`.

### Checklist for implementation note: handler + dispatch

- [ ] Added `packages/example/foo.py`.
- [x] Already-ticked thing.

## Stretch Goals

- [ ] Add `--explain` flag (anchor: `packages/example/foo.py`).

## Success Criteria

- [ ] Operators see deterministic counts.
"""


def _run_backfill_cli(
    cwd: Path, *extra: str, mcp_bin: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = (
        mcp_bin or "/nonexistent/no-such-binary-backfill-xyz"
    )
    return subprocess.run(
        [
            sys.executable, str(LIFECYCLE_PKG),
            "task-plan-checklist-backfill",
            *extra,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


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
# CLI contract
# ---------------------------------------------------------------------------


def test_backfill_dry_run_by_default_does_not_mutate(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    plan_dir = repo / "packages" / "example" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-99-example-task-plan.md"
    plan.write_text(SAMPLE_PLAN)
    before = plan.read_bytes()
    proc = _run_backfill_cli(repo, "--plans", str(plan))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["dry_run"] is True
    assert plan.read_bytes() == before
    assert receipt["totals"]["applied"] == 0


def test_backfill_glob_uses_linked_worktree_not_primary_checkout(
    tmp_path: Path,
) -> None:
    """Dry-run discovery must target the linked feature worktree so
    APPLY=1 cannot rewrite the primary checkout by accident."""
    primary = _make_repo(tmp_path)
    worktree = tmp_path / "repo-WORKSTATE-60"
    subprocess.run(
        [
            "git", "-C", str(primary), "worktree", "add", "-q",
            "-b", "feature/WORKSTATE-60", str(worktree),
        ],
        check=True,
    )
    plan_dir = worktree / "packages" / "workstate-system" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-60-linked-worktree-task-plan.md"
    plan.write_text(SAMPLE_PLAN)

    proc = _run_backfill_cli(worktree, "--tasks", "WORKSTATE-REF-60")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["workspace_root"] == str(worktree.resolve())
    paths = [row["plan_path"] for row in receipt["rows"]]
    assert paths == [str(plan.resolve())]


def test_backfill_with_apply_mutates_when_evidence_matches(
    tmp_path: Path, handlers_pkg
) -> None:
    """With APPLY=1 and matching evidence, the box flips and the file is
    rewritten byte-for-byte except for the single `[ ]` -> `[x]`.

    We mock the handoff evidence query directly so the test does not
    require a live MCP CLI: the backfill's ``_backfill_one_plan`` reuses
    ``sync_handler._query_handoff_evidence``, but the test exercises the
    in-process path by importing the module and calling it directly.
    """
    backfill = handlers_pkg["backfill"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    plan_dir = repo / "packages" / "example" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-99-example-task-plan.md"
    plan.write_text(SAMPLE_PLAN)

    # Stub _query_handoff_evidence to return evidence that ticks the
    # implementation note "Added `packages/example/foo.py`" box.
    def fake_query(workspace_root, task_ref):
        ev = sync.Evidence(
            slice_changed_files={1: {"packages/example/foo.py"}},
            slice_basenames={1: {"foo.py"}},
            slice_close_decision_ids={1: "fake_slice_complete_WORKSTATE-99_x"},
            all_changed_files={"packages/example/foo.py"},
            all_basenames={"foo.py"},
        )
        return ev, {"decisions_seen": 1}, None

    original_query = sync._query_handoff_evidence
    sync._query_handoff_evidence = fake_query
    try:
        row = backfill._backfill_one_plan(
            repo, "WORKSTATE-REF-99", plan, "flag", apply_changes=True,
            suppress_bare_slice_refs=False,
        )
    finally:
        sync._query_handoff_evidence = original_query

    assert row["applied"] is True
    assert row["ticked"] >= 1
    text = plan.read_text()
    # The line for foo.py must now be `- [x]`.
    assert "- [x] Added `packages/example/foo.py`." in text
    # The already-ticked line is preserved verbatim.
    assert "- [x] Already-ticked thing." in text
    # Stretch line never flips.
    assert "- [ ] Add `--explain` flag" in text


def test_backfill_stretch_never_auto_ticks(tmp_path: Path, handlers_pkg) -> None:
    backfill = handlers_pkg["backfill"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    plan = repo / "WORKSTATE-REF-99-task-plan.md"
    plan.write_text(SAMPLE_PLAN)

    # Evidence that *would* match the Stretch path anchor if not for the
    # Stretch carveout.
    def fake_query(workspace_root, task_ref):
        ev = sync.Evidence(
            all_changed_files={"packages/example/foo.py"},
            all_basenames={"foo.py"},
        )
        return ev, {}, None

    original = sync._query_handoff_evidence
    sync._query_handoff_evidence = fake_query
    try:
        row = backfill._backfill_one_plan(
            repo, "WORKSTATE-REF-99", plan, "flag", apply_changes=True,
            suppress_bare_slice_refs=False,
        )
    finally:
        sync._query_handoff_evidence = original

    text = plan.read_text()
    assert "- [ ] Add `--explain` flag" in text, (
        "Stretch box must NOT be auto-ticked even with matching anchor"
    )
    assert row["stretch_skipped"] >= 1


def test_backfill_no_untick_invariant(tmp_path: Path, handlers_pkg) -> None:
    """Already-ticked items must remain ticked even when evidence is
    empty (the one-way ratchet)."""
    backfill = handlers_pkg["backfill"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    plan = repo / "WORKSTATE-REF-99-task-plan.md"
    plan.write_text(SAMPLE_PLAN)

    def fake_query(workspace_root, task_ref):
        return sync.Evidence(), {}, None

    original = sync._query_handoff_evidence
    sync._query_handoff_evidence = fake_query
    try:
        backfill._backfill_one_plan(
            repo, "WORKSTATE-REF-99", plan, "flag", apply_changes=True,
            suppress_bare_slice_refs=False,
        )
    finally:
        sync._query_handoff_evidence = original

    text = plan.read_text()
    assert "- [x] Already-ticked thing." in text


# ---------------------------------------------------------------------------
# WORKSTATE-REF-60 collision invariant
# ---------------------------------------------------------------------------


COLLIDING_PLAN_TEMPLATE = """\
# Plan for {pkg}

## Consolidated Checklist

### Checklist for implementation note: do thing in {pkg}

- [ ] Implement `packages/{pkg}/thing.py`.
- [ ] implementation note closed.
"""


def test_backfill_collision_suppresses_bare_slice_refs(
    tmp_path: Path, handlers_pkg
) -> None:
    """Two unrelated plans both claim ``WORKSTATE-REF-60``. A slice-close decision
    recorded for the workstate-system plan must NOT tick the bare
    ``implementation note closed.`` box in the mcp-workstate-handoff plan.

    We feed the CLI a ``--tasks WORKSTATE-REF-60`` argument so it walks the
    full discovery + collision-detection path. The mocked
    ``_query_handoff_evidence`` returns a slice-close decision whose
    ``changed_files`` only touches the workstate-system tree.
    """
    backfill = handlers_pkg["backfill"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    for pkg in ("workstate-system", "mcp-workstate-handoff"):
        d = repo / "packages" / pkg / "docs" / "tasks"
        d.mkdir(parents=True)
        (d / f"WORKSTATE-REF-60-{pkg}-plan-task-plan.md").write_text(
            COLLIDING_PLAN_TEMPLATE.format(pkg=pkg)
        )

    # Build evidence that touches *only* the workstate-system path; the
    # slice_close_decision_ids is populated so a bare ``implementation note`` would
    # otherwise match in both plans.
    def fake_query(workspace_root, task_ref):
        ev = sync.Evidence(
            slice_changed_files={1: {"packages/workstate-system/thing.py"}},
            slice_basenames={1: {"thing.py"}},
            slice_close_decision_ids={1: "x_slice_complete_WORKSTATE-60_y"},
            all_changed_files={"packages/workstate-system/thing.py"},
            all_basenames={"thing.py"},
        )
        return ev, {}, None

    original = sync._query_handoff_evidence
    sync._query_handoff_evidence = fake_query
    try:
        # We can't drive the subprocess CLI with monkeypatched in-process
        # state, so we call run() directly.
        old_cwd = Path.cwd()
        os.chdir(repo)
        try:
            rc = backfill.run(["--tasks", "WORKSTATE-REF-60", "--apply"])
        finally:
            os.chdir(old_cwd)
    finally:
        sync._query_handoff_evidence = original
    assert rc == 0

    # Verify state on disk.
    workstate_plan = (
        repo / "packages" / "workstate-system" / "docs" / "tasks"
        / "WORKSTATE-REF-60-workstate-system-plan-task-plan.md"
    ).read_text()
    mcp_plan = (
        repo / "packages" / "mcp-workstate-handoff" / "docs" / "tasks"
        / "WORKSTATE-REF-60-mcp-workstate-handoff-plan-task-plan.md"
    ).read_text()

    # Agentic plan: the file-path anchor matches, so its first box ticks.
    assert "- [x] Implement `packages/workstate-system/thing.py`." in workstate_plan
    # MCP plan: the file-path anchor does NOT match (different package),
    # and the bare ``implementation note closed.`` box must NOT tick because the
    # collision invariant suppressed bare slice_ref matching.
    assert "- [ ] implementation note closed." in mcp_plan, (
        "collision invariant violated: bare ``implementation note`` box flipped on a "
        "plan whose file paths the evidence does not cover"
    )
    # Verify the same invariant on the agentic plan side too — the bare
    # ``implementation note`` should NOT have ticked from the suppression even though
    # the path-anchored box did tick.
    assert "- [ ] implementation note closed." in workstate_plan


def test_backfill_receipt_lists_collision_refs(tmp_path: Path, handlers_pkg) -> None:
    """The top-level ``slice_ref_suppressed`` receipt field records every
    ref that resolved to >1 plan, so operators can spot collision-mode
    runs without diffing the rows."""
    backfill = handlers_pkg["backfill"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    for pkg in ("workstate-system", "mcp-workstate-handoff"):
        d = repo / "packages" / pkg / "docs" / "tasks"
        d.mkdir(parents=True)
        (d / f"WORKSTATE-REF-60-{pkg}-plan-task-plan.md").write_text(
            COLLIDING_PLAN_TEMPLATE.format(pkg=pkg)
        )

    def fake_query(workspace_root, task_ref):
        return sync.Evidence(), {}, None

    captured: dict = {}

    def fake_emit(payload):
        captured["payload"] = payload

    common = sys.modules["handlers._common"]
    original_query = sync._query_handoff_evidence
    original_emit = common.emit
    sync._query_handoff_evidence = fake_query
    common.emit = fake_emit
    try:
        old_cwd = Path.cwd()
        os.chdir(repo)
        try:
            backfill.run(["--tasks", "WORKSTATE-REF-60"])
        finally:
            os.chdir(old_cwd)
    finally:
        sync._query_handoff_evidence = original_query
        common.emit = original_emit

    payload = captured["payload"]
    assert "WORKSTATE-REF-60" in payload["slice_ref_suppressed"]
    # Two plan rows (one per package).
    assert payload["totals"]["rows"] == 2


def test_backfill_missing_active_path_does_not_suppress_bare_slice_refs(
    tmp_path: Path, handlers_pkg
) -> None:
    """A stale active-row path in a deleted worktree is not a plan collision."""
    backfill = handlers_pkg["backfill"]
    audit = handlers_pkg["audit"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    plan = repo / "packages" / "workstate-system" / "docs" / "tasks" / (
        "WORKSTATE-REF-99-example-task-plan.md"
    )
    plan.parent.mkdir(parents=True)
    plan.write_text(
        "# Sample Plan\n\n"
        "## Consolidated Checklist\n\n"
        "### Checklist for implementation note: do thing\n\n"
        "- [ ] implementation note closed.\n",
        encoding="utf-8",
    )
    missing = tmp_path / "deleted-worktree" / "packages" / "workstate-system" / (
        "docs/tasks/WORKSTATE-REF-99-example-task-plan.md"
    )

    def fake_discover(workspace_root, task_ref):
        return [str(missing), str(plan)], ["active", "glob"]

    def fake_query(workspace_root, task_ref):
        ev = sync.Evidence(
            slice_close_decision_ids={1: "x_slice_complete_WORKSTATE-99_slice_1_done"}
        )
        return ev, {}, None

    original_discover = audit.discover_plans_for_task
    original_query = sync._query_handoff_evidence
    audit.discover_plans_for_task = fake_discover
    sync._query_handoff_evidence = fake_query
    try:
        old_cwd = Path.cwd()
        os.chdir(repo)
        try:
            rc = backfill.run(["--tasks", "WORKSTATE-REF-99", "--apply"])
        finally:
            os.chdir(old_cwd)
    finally:
        audit.discover_plans_for_task = original_discover
        sync._query_handoff_evidence = original_query

    assert rc == 0
    text = plan.read_text(encoding="utf-8")
    assert "- [x] implementation note closed." in text


def test_backfill_explicit_colliding_plan_suppresses_bare_slice_refs(
    tmp_path: Path, handlers_pkg
) -> None:
    """Explicit ``PLANS=`` must keep the same collision invariant as
    task discovery. A single colliding plan path is not enough evidence
    that a bare ``Slice N`` close belongs to that plan."""
    backfill = handlers_pkg["backfill"]
    sync = handlers_pkg["sync"]
    repo = _make_repo(tmp_path)
    for pkg in ("workstate-system", "mcp-workstate-handoff"):
        d = repo / "packages" / pkg / "docs" / "tasks"
        d.mkdir(parents=True)
        (d / f"WORKSTATE-REF-60-{pkg}-plan-task-plan.md").write_text(
            COLLIDING_PLAN_TEMPLATE.format(pkg=pkg)
        )
    mcp_plan_path = (
        repo / "packages" / "mcp-workstate-handoff" / "docs" / "tasks"
        / "WORKSTATE-REF-60-mcp-workstate-handoff-plan-task-plan.md"
    )

    def fake_query(workspace_root, task_ref):
        ev = sync.Evidence(
            slice_changed_files={1: {"packages/workstate-system/thing.py"}},
            slice_basenames={1: {"thing.py"}},
            slice_close_decision_ids={1: "x_slice_complete_WORKSTATE-60_y"},
            all_changed_files={"packages/workstate-system/thing.py"},
            all_basenames={"thing.py"},
        )
        return ev, {}, None

    original = sync._query_handoff_evidence
    sync._query_handoff_evidence = fake_query
    try:
        old_cwd = Path.cwd()
        os.chdir(repo)
        try:
            rc = backfill.run(["--plans", str(mcp_plan_path), "--apply"])
        finally:
            os.chdir(old_cwd)
    finally:
        sync._query_handoff_evidence = original

    assert rc == 0
    mcp_plan = mcp_plan_path.read_text()
    assert "- [ ] Implement `packages/mcp-workstate-handoff/thing.py`." in mcp_plan
    assert "- [ ] implementation note closed." in mcp_plan


# ---------------------------------------------------------------------------
# Resolver-level: bare slice_ref suppression
# ---------------------------------------------------------------------------


def test_match_item_honors_suppress_bare_slice_refs_flag(handlers_pkg) -> None:
    """Direct resolver test: with ``suppress_bare_slice_refs=True`` and
    an item whose only anchor is a bare ``implementation note`` reference, the
    resolver returns no_slice_evidence_match. File-path and decision-id
    anchors are unaffected."""
    sync = handlers_pkg["sync"]
    item = sync.ChecklistItem(
        line_index=0,
        raw_line="- [ ] implementation note closed.",
        section_class=sync.SECTION_SLICE,
        slice_number=1,
        already_ticked=False,
        body="implementation note closed.",
        anchors=sync.Anchors(slice_refs=(1,)),
    )
    ev_open = sync.Evidence(
        slice_close_decision_ids={1: "x_slice_complete_WORKSTATE-60_y"},
        suppress_bare_slice_refs=False,
    )
    matched, _ = sync._match_item(item, ev_open)
    assert matched is True

    ev_collision = sync.Evidence(
        slice_close_decision_ids={1: "x_slice_complete_WORKSTATE-60_y"},
        suppress_bare_slice_refs=True,
    )
    matched, reason = sync._match_item(item, ev_collision)
    assert matched is False
    assert reason == "no_slice_evidence_match"
