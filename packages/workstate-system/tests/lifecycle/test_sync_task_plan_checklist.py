"""Unit tests for the ``sync-task-plan-checklist`` handler (WORKSTATE-REF-64 implementation note).

These tests exercise the three pure layers (``parse`` / ``resolve`` /
``apply``) directly with hand-built ``Evidence`` inputs, plus a
subprocess test for the dry-run-default + ``--apply`` CLI contract.
The integration test that drives the handler with a real HANDOFF_DB
and an in-process ``close_slice`` lives under
``test_sync_task_plan_checklist_integration.py`` (WORKSTATE-REF-64 implementation note).
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
def handler_module():
    """Load handlers.sync_task_plan_checklist via its on-disk package."""
    import importlib.util

    # The lifecycle runner is invoked as `python <abs>/lifecycle <subcmd>`;
    # ``__main__.py`` adds the package dir to ``sys.path`` so ``from
    # handlers import ...`` works. Tests do the same here so we exercise
    # the same import surface.
    if str(LIFECYCLE_PKG) not in sys.path:
        sys.path.insert(0, str(LIFECYCLE_PKG))
    # Load handlers/__init__.py first so the relative import resolves.
    handlers_init = HANDLERS_DIR / "__init__.py"
    spec_pkg = importlib.util.spec_from_file_location(
        "handlers", handlers_init, submodule_search_locations=[str(HANDLERS_DIR)]
    )
    assert spec_pkg is not None
    pkg = importlib.util.module_from_spec(spec_pkg)
    sys.modules["handlers"] = pkg
    assert spec_pkg.loader is not None
    spec_pkg.loader.exec_module(pkg)
    # Load _common so relative `from . import _common` works at import time.
    common_path = HANDLERS_DIR / "_common.py"
    spec_common = importlib.util.spec_from_file_location(
        "handlers._common", common_path
    )
    assert spec_common is not None
    common_mod = importlib.util.module_from_spec(spec_common)
    sys.modules["handlers._common"] = common_mod
    assert spec_common.loader is not None
    spec_common.loader.exec_module(common_mod)
    # Finally load the target handler.
    handler_path = HANDLERS_DIR / "sync_task_plan_checklist.py"
    spec = importlib.util.spec_from_file_location(
        "handlers.sync_task_plan_checklist", handler_path
    )
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["handlers.sync_task_plan_checklist"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------


SAMPLE_PLAN = """\
# Example Plan

## Consolidated Checklist

## Context and Ownership

- [ ] Loaded `TASK_PLAN.template.md` and reviewed `lifecycle.mk`.
- [x] Confirmed boundary owner.

### Checklist for implementation note: handler + dispatch

- [ ] Added `packages/example/foo.py` with `parse` and `apply`.
- [ ] Added `packages/example/tests/test_foo.py` covering parse round-trip.
- [ ] `cd packages/example && uv run pytest tests/test_foo.py -x` passes.
- [ ] implementation note commit lands.

### Checklist for implementation note: make wiring

- [ ] Added `packages/example/Makefile.d/foo.mk`.
- [ ] `make foo-target` green.

## Review Readiness

- [ ] No path flips `- [x]` back to `- [ ]`.

## Stretch Goals

- [ ] Add `--explain` flag (anchor visible in `packages/example/foo.py`).

## Success Criteria

- [ ] Operators stop manually flipping checkboxes.
"""


def test_parse_classifies_sections(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    by_section: dict[str, int] = {}
    for item in parsed.items:
        by_section[item.section_class] = by_section.get(item.section_class, 0) + 1
    assert by_section["context"] == 2
    assert by_section["slice"] == 6
    assert by_section["review"] == 1
    assert by_section["stretch"] == 1
    assert by_section["success"] == 1


def test_parse_captures_slice_number(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    slice_items = [i for i in parsed.items if i.section_class == "slice"]
    slice1_items = [i for i in slice_items if i.slice_number == 1]
    slice2_items = [i for i in slice_items if i.slice_number == 2]
    assert len(slice1_items) == 4
    assert len(slice2_items) == 2


def test_parse_extracts_file_path_anchors(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    foo_item = next(
        i for i in parsed.items if "packages/example/foo.py" in i.body
    )
    assert "packages/example/foo.py" in foo_item.anchors.paths


def test_parse_extracts_make_target_anchors(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    target_item = next(
        i for i in parsed.items if "make foo-target" in i.body
    )
    assert "foo-target" in target_item.anchors.make_targets


def test_parse_records_already_ticked(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    ticked = [i for i in parsed.items if i.already_ticked]
    assert len(ticked) == 1
    assert "boundary owner" in ticked[0].body


def test_parse_extracts_canonical_uppercase_hyphenated_decision_id(handler_module) -> None:
    """Regression for WORKSTATE-REF-64-BR-03.

    Canonical slice-complete ids accept uppercase + hyphenated work_refs
    (e.g. ``WORKSTATE-REF-64``); the anchor regex must accept them so plan items
    that explicitly reference such ids resolve to ``tick`` instead of
    falling through to ``keep``/``unresolved``.
    """
    plan = """\
## Context and Ownership

- [ ] References `claude_slice_complete_WORKSTATE-64_slice_2b_recipe_post_step_wiring`.
"""
    parsed = handler_module.parse(plan)
    assert len(parsed.items) == 1
    anchors = parsed.items[0].anchors
    assert (
        "claude_slice_complete_WORKSTATE-64_slice_2b_recipe_post_step_wiring"
        in anchors.decision_ids
    ), anchors.decision_ids


def test_parse_skips_fenced_code_blocks(handler_module) -> None:
    plan = """\
## Context and Ownership

```
- [ ] this is in a fence and should not match
```

- [ ] this is a real item.
"""
    parsed = handler_module.parse(plan)
    assert len(parsed.items) == 1
    assert "real item" in parsed.items[0].body


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def _evidence_for_slice_1(handler_module):
    return handler_module.Evidence(
        slice_changed_files={
            1: {
                "packages/example/foo.py",
                "packages/example/tests/test_foo.py",
            }
        },
        slice_basenames={
            1: {"foo.py", "test_foo.py"}
        },
        slice_close_decision_ids={
            1: "claude_slice_complete_example_slice_1_handler_dispatch"
        },
        test_commands={
            "cd packages/example && uv run pytest tests/test_foo.py -x"
        },
        all_changed_files={
            "packages/example/foo.py",
            "packages/example/tests/test_foo.py",
        },
        all_basenames={"foo.py", "test_foo.py"},
        all_decision_ids={
            "claude_slice_complete_example_slice_1_handler_dispatch"
        },
    )


def test_resolve_ticks_slice_items_with_matching_changed_files(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    foo_item = next(
        i for i in parsed.items if "packages/example/foo.py" in i.body
    )
    assert resolutions[foo_item.line_index].action == "tick"


def test_resolve_ticks_slice_item_with_matching_test_command(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    pytest_item = next(
        i for i in parsed.items if "pytest tests/test_foo.py" in i.body
    )
    assert resolutions[pytest_item.line_index].action == "tick"


def test_resolve_keeps_slice2_items_without_evidence(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    slice2_items = [
        i for i in parsed.items
        if i.section_class == "slice" and i.slice_number == 2
    ]
    for item in slice2_items:
        action = resolutions[item.line_index].action
        assert action in ("keep", "unresolved"), (item, action)
        assert action != "tick"


def test_resolve_never_ticks_stretch_section(handler_module) -> None:
    """Even when the Stretch item's anchor matches changed_files, it
    must stay `- [ ]`. The Stretch carveout is the whole point of the
    plan's opt-in section."""
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    stretch_item = next(i for i in parsed.items if i.section_class == "stretch")
    assert "packages/example/foo.py" in stretch_item.anchors.paths
    assert resolutions[stretch_item.line_index].action == "keep"
    assert resolutions[stretch_item.line_index].reason == "stretch_section_never_auto_ticks"


def test_resolve_already_ticked_short_circuits(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    ticked = next(i for i in parsed.items if i.already_ticked)
    assert resolutions[ticked.line_index].action == "already_ticked"


def test_resolve_unresolved_when_no_anchors(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    no_anchor = next(
        i for i in parsed.items
        if i.section_class == "slice" and "implementation note commit lands" in i.body
    )
    # "implementation note" is itself a slice ref anchor — it should resolve as tick
    # because the slice-1 close decision exists; verify the slice-ref
    # matching path explicitly.
    assert 1 in no_anchor.anchors.slice_refs
    assert resolutions[no_anchor.line_index].action == "tick"


def test_resolve_context_item_ticks_on_global_evidence(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    template_item = next(
        i for i in parsed.items if "TASK_PLAN.template.md" in i.body
    )
    # The context item references TASK_PLAN.template.md and lifecycle.mk
    # as basenames; if neither is in changed_files, it stays keep.
    assert resolutions[template_item.line_index].action == "keep"


def test_resolve_context_item_ticks_when_basename_in_evidence(handler_module) -> None:
    plan = """\
## Context and Ownership

- [ ] Loaded `TASK_PLAN.template.md` (canonical headings).
"""
    parsed = handler_module.parse(plan)
    evidence = handler_module.Evidence(
        all_changed_files={"some/path/TASK_PLAN.template.md"},
        all_basenames={"TASK_PLAN.template.md"},
    )
    resolutions = handler_module.resolve(parsed, evidence)
    item = parsed.items[0]
    assert resolutions[item.line_index].action == "tick"


# ---------------------------------------------------------------------------
# apply()
# ---------------------------------------------------------------------------


def test_apply_rewrites_only_ticked_lines(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    out = handler_module.apply(SAMPLE_PLAN, resolutions)

    foo_line = "- [x] Added `packages/example/foo.py` with `parse` and `apply`."
    assert foo_line in out
    # implementation note items must stay `- [ ]`.
    assert "- [ ] Added `packages/example/Makefile.d/foo.mk`." in out
    # Stretch item stays `- [ ]`.
    assert (
        "- [ ] Add `--explain` flag (anchor visible in `packages/example/foo.py`)."
        in out
    )
    # Already-ticked line stays `- [x]`.
    assert "- [x] Confirmed boundary owner." in out


def test_apply_is_idempotent(handler_module) -> None:
    parsed = handler_module.parse(SAMPLE_PLAN)
    evidence = _evidence_for_slice_1(handler_module)
    resolutions = handler_module.resolve(parsed, evidence)
    first = handler_module.apply(SAMPLE_PLAN, resolutions)
    parsed2 = handler_module.parse(first)
    resolutions2 = handler_module.resolve(parsed2, evidence)
    second = handler_module.apply(first, resolutions2)
    assert first == second


def test_apply_one_way_ratchet_never_unticks(handler_module) -> None:
    """A `- [x]` line whose anchor has no matching evidence must remain
    `- [x]`. Reversing operator intent is forbidden."""
    plan = """\
### Checklist for implementation note: foo

- [x] Did the thing (no anchor at all).
- [x] Touched `packages/example/foo.py` even though evidence is empty.
"""
    parsed = handler_module.parse(plan)
    evidence = handler_module.Evidence()  # totally empty
    resolutions = handler_module.resolve(parsed, evidence)
    out = handler_module.apply(plan, resolutions)
    assert out == plan
    for r in resolutions.values():
        assert r.action == "already_ticked"


# ---------------------------------------------------------------------------
# run() — CLI contract
# ---------------------------------------------------------------------------


def _run_cli(
    plan_path: Path, *extra: str, env_override: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    # Point at a non-existent CLI so the resolver degrades to empty evidence
    # and the resolver path never makes a real subprocess call. This keeps
    # unit-level CLI tests fully hermetic — the lifecycle integration test
    # in implementation note exercises the real handoff CLI path.
    env["MCP_AGENT_HANDOFF_BIN"] = "/nonexistent/no-such-binary-xyz-syncplan"
    if env_override:
        env.update(env_override)
    return subprocess.run(
        [
            sys.executable, str(LIFECYCLE_PKG),
            "sync-task-plan-checklist",
            "--task", "WORKSTATE-REF-64",
            "--plan", str(plan_path),
            *extra,
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def test_cli_dry_run_default_does_not_mutate_plan(tmp_path: Path) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text(SAMPLE_PLAN)
    proc = _run_cli(plan)
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["dry_run"] is True
    assert receipt["applied"] is False
    # Empty evidence + dry-run: file is byte-identical.
    assert plan.read_text() == SAMPLE_PLAN


def test_cli_apply_writes_only_when_evidence_present(tmp_path: Path) -> None:
    # With a non-existent CLI the resolver returns empty Evidence, so even
    # --apply rewrites nothing. The receipt confirms the contract:
    # ``applied`` is True only when at least one tick was applied.
    plan = tmp_path / "plan.md"
    plan.write_text(SAMPLE_PLAN)
    proc = _run_cli(plan, "--apply", "--quiet")
    assert proc.returncode == 0, proc.stderr
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is True
    assert receipt["dry_run"] is False
    assert receipt["applied"] is False
    assert plan.read_text() == SAMPLE_PLAN


def test_cli_missing_plan_returns_2_with_error(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-plan.md"
    proc = _run_cli(missing)
    assert proc.returncode == 2
    receipt = json.loads(proc.stdout)
    assert receipt["ok"] is False
    assert receipt["error"] == "plan_not_found"


def test_cli_uses_repo_root_for_workspace_arg_with_nested_plan(tmp_path: Path) -> None:
    """WORKSTATE-REF-70 implementation note regression.

    A task plan that lives under a nested package path (e.g.
    ``packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-67-...``) must
    resolve handoff evidence through the repo root, not the plan
    file's parent directory. Previously the handler passed
    ``plan_path.parent`` as ``--workspace-root`` which pointed the
    handoff CLI at a package docs folder with no DB.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        check=True,
    )
    plan_dir = repo / "packages" / "mcp-workstate-handoff" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-67-fake-plan.md"
    plan.write_text(SAMPLE_PLAN)
    argv_log = tmp_path / "argv.log"
    shim = tmp_path / "shim.sh"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f"printf '%s\\n' \"$*\" >> {argv_log}\n"
        "exit 1\n"
    )
    shim.chmod(0o755)
    env = os.environ.copy()
    env["MCP_AGENT_HANDOFF_BIN"] = str(shim)
    proc = subprocess.run(
        [
            sys.executable, str(LIFECYCLE_PKG),
            "sync-task-plan-checklist",
            "--task", "WORKSTATE-REF-67",
            "--plan", str(plan),
            "--quiet",
        ],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    log = argv_log.read_text() if argv_log.exists() else ""
    repo_resolved = str(repo.resolve())
    assert f"--workspace-root {repo_resolved} " in log + "\n", (
        f"expected repo root --workspace-root, got:\n{log}"
    )
    assert f"--workspace-root {plan_dir}" not in log, (
        f"plan parent must not appear as --workspace-root, got:\n{log}"
    )


def test_resolve_workspace_root_prefers_repo_root_over_plan_parent(
    handler_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``resolve_workspace_root`` returns the git toplevel for nested
    plans, not the plan's parent directory."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    plan_dir = repo / "packages" / "mcp-workstate-handoff" / "docs" / "tasks"
    plan_dir.mkdir(parents=True)
    plan = plan_dir / "WORKSTATE-REF-67-fake-plan.md"
    plan.write_text("# stub\n")
    monkeypatch.chdir(repo)
    resolved = handler_module.resolve_workspace_root(plan)
    assert resolved.resolve() == repo.resolve()


def test_resolve_workspace_root_returns_linked_worktree(
    handler_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Plan file operations run in the linked worktree, while a separate
    helper resolves the canonical handoff root."""
    primary = tmp_path / "repo"
    primary.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(primary)], check=True)
    subprocess.run(
        ["git", "-C", str(primary), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        check=True,
    )
    worktree = tmp_path / "repo-WORKSTATE-70"
    subprocess.run(
        [
            "git", "-C", str(primary), "worktree", "add", "-q",
            "-b", "feature/WORKSTATE-70", str(worktree),
        ],
        check=True,
    )

    monkeypatch.chdir(worktree)
    assert handler_module.resolve_workspace_root().resolve() == worktree.resolve()
    assert (
        handler_module.resolve_handoff_workspace_root(worktree).resolve()
        == primary.resolve()
    )


# ---------------------------------------------------------------------------
# build_evidence_from_handoff_payload
# ---------------------------------------------------------------------------


def test_build_evidence_projects_decision_changed_files(handler_module) -> None:
    search_payload = {
        "ok": True,
        "data": {
            "results": [
                {
                    "record_type": "decision",
                    "decision": "claude_slice_complete_example_slice_1_handler_dispatch",
                    "changed_files_json": json.dumps(
                        ["packages/example/foo.py", "packages/example/tests/test_foo.py"]
                    ),
                },
            ]
        },
    }
    tests_payload = {
        "ok": True,
        "data": {
            "tests": [
                {"command": "uv run pytest tests/test_foo.py -x"},
            ]
        },
    }
    evidence = handler_module.build_evidence_from_handoff_payload(
        search_payload, tests_payload
    )
    assert "packages/example/foo.py" in evidence.all_changed_files
    assert "foo.py" in evidence.all_basenames
    assert evidence.slice_changed_files.get(1) == {
        "packages/example/foo.py",
        "packages/example/tests/test_foo.py",
    }
    assert evidence.slice_basenames.get(1) == {"foo.py", "test_foo.py"}
    assert (
        evidence.slice_close_decision_ids.get(1)
        == "claude_slice_complete_example_slice_1_handler_dispatch"
    )
    assert "uv run pytest tests/test_foo.py -x" in evidence.test_commands


def test_build_evidence_maps_subslice_decision_to_parent_slice(handler_module) -> None:
    """Sub-slice close ids such as ``slice_2b`` must still feed the
    parent ``Checklist for implementation note`` bucket.

    This is the dogfood failure shape from completed WORKSTATE-REF-64 plans:
    the handoff DB had changed_files evidence, but the resolver never
    assigned it to implementation note, so finished checklist items stayed unchecked.
    """
    search_payload = {
        "ok": True,
        "data": {
            "results": [
                {
                    "record_type": "decision",
                    "decision": (
                        "claude_slice_complete_WORKSTATE-64_"
                        "slice_2b_recipe_post_step_wiring"
                    ),
                    "changed_files_json": json.dumps(
                        ["packages/workstate-system/Makefile.d/lifecycle.mk"]
                    ),
                },
            ]
        },
    }
    evidence = handler_module.build_evidence_from_handoff_payload(
        search_payload, None
    )

    assert evidence.slice_close_decision_ids.get(2) == (
        "claude_slice_complete_WORKSTATE-64_slice_2b_recipe_post_step_wiring"
    )
    assert evidence.slice_changed_files.get(2) == {
        "packages/workstate-system/Makefile.d/lifecycle.mk"
    }

    plan = """\
### Checklist for implementation note: lifecycle wiring

- [ ] Added `packages/workstate-system/Makefile.d/lifecycle.mk`.
"""
    parsed = handler_module.parse(plan)
    resolutions = handler_module.resolve(parsed, evidence)
    item = parsed.items[0]
    assert resolutions[item.line_index].action == "tick"
