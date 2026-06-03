"""Unit tests for the ``task-plan-checklist-audit`` handler (WORKSTATE-REF-70 implementation note).

The audit is read-only: it parses each discovered plan, runs the existing
``sync_task_plan_checklist`` resolver against handoff evidence, and emits
one receipt row per plan with the counts an operator needs. These tests
pin the receipt shape, the duplicate-task_ref discovery (WORKSTATE-REF-60
collision), the nested-package workspace-root regression
(WORKSTATE-REF-67-shape paths must resolve evidence through the repo root),
and the no-write guarantee.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_PKG = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle"
HANDLERS_DIR = LIFECYCLE_PKG / "handlers"

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


@pytest.fixture(scope="module")
def audit_module():
    """Load the audit handler via its on-disk package so the relative
    imports (``from . import _common``) resolve like the runner."""
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

    for name in ("_common", "sync_task_plan_checklist", "task_plan_checklist_audit"):
        path = HANDLERS_DIR / f"{name}.py"
        spec = importlib.util.spec_from_file_location(f"handlers.{name}", path)
        assert spec is not None
        mod = importlib.util.module_from_spec(spec)
        sys.modules[f"handlers.{name}"] = mod
        assert spec.loader is not None
        spec.loader.exec_module(mod)

    return sys.modules["handlers.task_plan_checklist_audit"]


def _run_audit_cli(
    cwd: Path, *extra: str, mcp_bin: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["MCP_WORKSTATE_HANDOFF_BIN"] = mcp_bin or "/nonexistent/no-such-binary-audit-xyz"
    return subprocess.run(
        [
            sys.executable, str(LIFECYCLE_PKG),
            "task-plan-checklist-audit",
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


def test_audit_emits_empty_rows_with_no_args(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _run_audit_cli(repo)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["command"] == "task-plan-checklist-audit"
    assert receipt["rows"] == []
    assert receipt["totals"]["rows"] == 0


def test_audit_with_explicit_plan_runs_parse_resolve(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    plan_dir = repo / "packages" / "example" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-99-example-task-plan.md"
    plan.write_text(SAMPLE_PLAN)
    proc = _run_audit_cli(repo, "--plans", str(plan))
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert len(receipt["rows"]) == 1
    row = receipt["rows"][0]
    assert row["plan_path"] == str(plan)
    assert row["plan_source"] == "flag"
    # With no MCP CLI available, evidence is empty: everything resolves
    # to keep or unresolved. The already-ticked box still counts.
    assert row["already_ticked"] == 1
    assert row["tick_candidates"] == 0
    # Stretch goals counted under stretch_skipped, not unresolved.
    assert row["stretch_skipped"] >= 1


def test_audit_discovers_both_WORKSTATE60_plans_via_glob(tmp_path: Path) -> None:
    """WORKSTATE-REF-60 collision: two unrelated plans share one task_ref.

    The audit must surface BOTH plan files as separate rows when the
    operator passes ``--tasks WORKSTATE-REF-60``, even though the handoff DB
    only stores one active ``task_plan_path``. The filesystem glob is
    what keeps the second plan from collapsing into the first.
    """
    repo = _make_repo(tmp_path)
    for pkg in ("workstate-system", "mcp-workstate-handoff"):
        plan_dir = repo / "packages" / pkg / "docs" / "tasks"
        plan_dir.mkdir(parents=True)
        slug = (
            "terminal-guard-blocklist-inversion"
            if pkg == "workstate-system"
            else "canonical-compaction-contract-consumer"
        )
        (plan_dir / f"WORKSTATE-REF-60-{slug}-task-plan.md").write_text(SAMPLE_PLAN)
    proc = _run_audit_cli(repo, "--tasks", "WORKSTATE-REF-60")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    paths = {row["plan_path"] for row in receipt["rows"]}
    assert len(paths) == 2, receipt["rows"]
    assert any("workstate-system" in p for p in paths)
    assert any("mcp-workstate-handoff" in p for p in paths)


def test_audit_glob_uses_linked_worktree_not_primary_checkout(
    tmp_path: Path,
) -> None:
    """Linked-worktree regression: plan discovery must read the task
    worktree, not the primary checkout that owns handoff state."""
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

    proc = _run_audit_cli(worktree, "--tasks", "WORKSTATE-REF-60")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["workspace_root"] == str(worktree.resolve())
    paths = [row["plan_path"] for row in receipt["rows"]]
    assert paths == [str(plan.resolve())]


def test_audit_is_readonly_does_not_mutate_plan_or_create_sidecar(
    tmp_path: Path,
) -> None:
    """Audit MUST NOT write to the plan or to ``.task-state/``.

    The contract: ``task-plan-checklist-audit`` is the read-only counter.
    Any sidecar write is the sync handler's domain. Operators inspecting
    historical plans need the audit to be safe in a clean worktree.
    """
    repo = _make_repo(tmp_path)
    plan_dir = repo / "packages" / "example" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-99-example-task-plan.md"
    plan.write_text(SAMPLE_PLAN)
    original_bytes = plan.read_bytes()
    state_dir = repo / ".task-state"
    assert not state_dir.exists()
    proc = _run_audit_cli(repo, "--plans", str(plan))
    assert proc.returncode == 0, proc.stderr
    assert plan.read_bytes() == original_bytes
    # No sidecar artifact must be created under .task-state/.
    if state_dir.exists():
        assert not (state_dir / "checklist_sync.json").exists(), (
            "audit must not write the sync sidecar"
        )


def test_audit_unresolved_task_ref_returns_explicit_row(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _run_audit_cli(repo, "--tasks", "WORKSTATE-REF-999")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert len(receipt["rows"]) == 1
    row = receipt["rows"][0]
    assert row["task_ref"] == "WORKSTATE-REF-999"
    assert row["plan_path"] is None
    assert row["error"] == "plan_unresolved"


def test_audit_nested_plan_uses_handoff_root_for_workspace(tmp_path: Path) -> None:
    """WORKSTATE-REF-70 regression: a nested-package plan resolves evidence
    through the repo root, not its own parent directory.

    The handler shells out to ``mcp-workstate-handoff`` with
    ``--workspace-root <root>``. This test wedges in a recording shim
    so the test asserts on the captured argv directly.
    """
    repo = _make_repo(tmp_path)
    plan_dir = repo / "packages" / "mcp-workstate-handoff" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-67-fake-plan.md"
    plan.write_text(SAMPLE_PLAN)
    argv_log = tmp_path / "argv.log"
    shim = tmp_path / "shim.sh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {argv_log}\n"
        # Exit non-zero so the handler degrades to empty evidence;
        # what we care about is the --workspace-root value that was
        # passed in.
        "exit 1\n"
    )
    shim.chmod(0o755)
    proc = _run_audit_cli(repo, "--plans", str(plan), mcp_bin=str(shim))
    assert proc.returncode == 0, proc.stderr
    assert argv_log.exists(), "shim must have recorded at least one call"
    log = argv_log.read_text()
    repo_resolved = str(repo.resolve())
    assert f"--workspace-root {repo_resolved} " in log + "\n", (
        f"expected repo root --workspace-root, got log:\n{log}"
    )
    assert f"--workspace-root {plan_dir}" not in log, (
        "audit must not pass plan parent as --workspace-root"
    )


def test_audit_invalid_task_ref_format_returns_invalid_row(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    proc = _run_audit_cli(repo, "--tasks", "bad/ref")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["rows"][0]["plan_source"] == "invalid"
    assert receipt["rows"][0]["error"] == "task_ref_invalid"


# ---------------------------------------------------------------------------
# Unit-level helpers
# ---------------------------------------------------------------------------


def test_audit_split_csv_handles_mixed_separators(audit_module) -> None:
    assert audit_module._split_csv(["a, b c", "d"]) == ["a", "b", "c", "d"]
    assert audit_module._split_csv(["x", "x"]) == ["x"]
    assert audit_module._split_csv(None) == []


def test_audit_one_plan_reports_stretch_skipped(audit_module, tmp_path: Path) -> None:
    plan = tmp_path / "WORKSTATE-REF-99-task-plan.md"
    plan.write_text(SAMPLE_PLAN)
    row = audit_module._audit_one_plan(tmp_path, "WORKSTATE-REF-99", plan, "flag")
    assert row["stretch_skipped"] >= 1
    assert row["already_ticked"] >= 1
    assert row["tick_candidates"] == 0  # no evidence available
