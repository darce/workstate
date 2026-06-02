from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
FACADE_CHECK = PACKAGE_ROOT / "scripts" / "check_workflow_facade.py"
FACADE_CHECK_SPEC = importlib.util.spec_from_file_location("workflow_facade_check", FACADE_CHECK)
assert FACADE_CHECK_SPEC is not None and FACADE_CHECK_SPEC.loader is not None
workflow_facade_check = importlib.util.module_from_spec(FACADE_CHECK_SPEC)
FACADE_CHECK_SPEC.loader.exec_module(workflow_facade_check)


def _run_facade_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FACADE_CHECK), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def _write_skill_body(root: Path, slug: str, body: str) -> None:
    skill_dir = root / "skills" / slug
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "body.md").write_text(body, encoding="utf-8")


def test_facade_lint_rejects_cold_start_block_without_status_or_tasks(tmp_path: Path) -> None:
    _write_skill_body(
        tmp_path,
        "branch-review",
        """## Core Process

0. Ensure task scope before any cwd-resolving MCP read. On cold start:
   - Call `load_session(...)` first.
   - Then use `search_handoff(...)` and `review_findings(...)`.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "branch-review/body.md" in proc.stderr
    assert "make status" in proc.stderr


def test_facade_lint_accepts_cold_start_block_with_facade_commands(tmp_path: Path) -> None:
    _write_skill_body(
        tmp_path,
        "branch-review",
        """## Core Process

0. Ensure task scope before any cwd-resolving MCP read. On cold start:
   - Run `make status LIFECYCLE_ARGS=--json` from the target worktree.
   - If more than one task may be active, run `make tasks LIFECYCLE_ARGS=--json`.
   - After that, pass `task_ref` explicitly to `load_session(...)` and `review_findings(...)`.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 0, proc.stderr


def test_facade_lint_rejects_session_start_block_without_status_or_tasks(tmp_path: Path) -> None:
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, run `make context` and load hot state with `load_session`.
2. Use bounded reads unless you truly need full task history.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "handoff-lifecycle/body.md" in proc.stderr
    assert "make status" in proc.stderr


def test_facade_lint_rejects_cold_start_block_with_make_context_before_make_doctor(tmp_path: Path) -> None:
    """implementation note implementation note: doctor must come first when both commands appear."""
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, run `make context` for the deeper load, then `make doctor LIFECYCLE_ARGS=--json`.
2. Use `load_session(...)` only after a scoped task has already been selected.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "handoff-lifecycle/body.md" in proc.stderr
    assert "make doctor" in proc.stderr


def test_facade_lint_rejects_cold_start_block_with_cat_dashboard_before_make_doctor(tmp_path: Path) -> None:
    """`cat DASHBOARD.txt` first must fail even when doctor is mentioned later."""
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, `cat DASHBOARD.txt` for orientation, then run `make doctor LIFECYCLE_ARGS=--json` and `load_session(...)`.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "make doctor" in proc.stderr


def test_facade_lint_accepts_cold_start_block_with_make_doctor_first(tmp_path: Path) -> None:
    """Doctor-first ordering with deeper-load follow-ups is the intended shape."""
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, run `make doctor LIFECYCLE_ARGS=--json` first; drop down to `make context` and `make status LIFECYCLE_ARGS=--json` as deeper-load follow-ups before `load_session(...)`.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 0, proc.stderr


def test_facade_lint_rejects_raw_make_post_target_json_flag(tmp_path: Path) -> None:
    """WORKSTATE-REF-53 implementation note: ``make status --json`` is not a real Make spelling
    (Make parses ``--json`` as a target, not a flag). The facade lint must
    reject any session-start / cold-start orientation block that teaches
    that raw form so docs and generated prompts stay honest."""
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, run `make doctor LIFECYCLE_ARGS=--json` first; then
   drop down to `make status --json` and `load_session(...)`.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "handoff-lifecycle/body.md" in proc.stderr
    assert "LIFECYCLE_ARGS=--json" in proc.stderr or "make status --json" in proc.stderr


def test_facade_lint_rejects_unimplemented_workstate_cli_verb(tmp_path: Path) -> None:
    """WORKSTATE-REF-53 implementation note: the optional ``agentic`` CLI facade was decided
    *skip* (decision_id ``claude_WORKSTATE_53_cli_facade_skip``). Workflow
    docs and skill bodies must therefore not teach an ``agentic <verb>``
    surface that does not exist on disk. Reintroducing one without the
    matching scaffold would silently mislead operators back to a CLI
    they cannot run; the lint must reject it."""
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, run `agentic status --json` from the target worktree.
   Then call `load_session(...)` once a task scope is selected.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "handoff-lifecycle/body.md" in proc.stderr
    assert "agentic status" in proc.stderr or "agentic <verb>" in proc.stderr


def test_facade_lint_rejects_unimplemented_workstate_cli_in_session_start_block(tmp_path: Path) -> None:
    """The same scrub applies to ``At session start`` orientation blocks."""
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        """## Core Process

1. At session start, run `make doctor LIFECYCLE_ARGS=--json` first; then
   drop down to `agentic tasks --json` for the cross-task view.
""",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "agentic tasks" in proc.stderr or "agentic <verb>" in proc.stderr


def test_facade_lint_scans_generated_claude_commands_for_unimplemented_workstate_cli(
    tmp_path: Path,
) -> None:
    """WORKSTATE-REF-53 implementation note (finding 288): generated workflow adapters under
    `.claude/commands/*.md` are produced by `generate_agent_workflows.py` from
    skill bodies. If a body change reintroduces an `agentic <verb>` form, the
    rendered adapter inherits it. The lint must scan generated outputs so the
    skip recorded in implementation note cannot leak past the source skills."""
    generated_dir = tmp_path / ".claude" / "commands"
    generated_dir.mkdir(parents=True, exist_ok=True)
    (generated_dir / "handoff-lifecycle.md").write_text(
        """<!-- GENERATED by scripts/generate_agent_workflows.py; do not edit by hand. -->

## Core Process

1. At session start, run `agentic status --json` from the target worktree.
   Then call `load_session(...)` once a task scope is selected.
""",
        encoding="utf-8",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert ".claude/commands/handoff-lifecycle.md" in proc.stderr
    assert "agentic status" in proc.stderr or "agentic <verb>" in proc.stderr


def test_facade_lint_scans_generated_codex_router_for_unimplemented_workstate_cli(
    tmp_path: Path,
) -> None:
    """WORKSTATE-REF-53 implementation note (finding 288): the codex command router doc lives at
    `docs/workstate/generated/codex-command-router.md` and must also be scanned
    so manifest renders cannot reintroduce a non-existent `agentic <verb>`
    surface that operators cannot run."""
    generated_dir = tmp_path / "docs" / "workstate" / "generated"
    generated_dir.mkdir(parents=True, exist_ok=True)
    (generated_dir / "codex-command-router.md").write_text(
        """<!-- GENERATED by scripts/generate_agent_workflows.py; do not edit by hand. -->

## Core Process

1. At session start, run `make doctor LIFECYCLE_ARGS=--json` first; then drop
   down to `agentic tasks --json` for the cross-task view.
""",
        encoding="utf-8",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert "docs/workstate/generated/codex-command-router.md" in proc.stderr
    assert "agentic tasks" in proc.stderr or "agentic <verb>" in proc.stderr


def test_facade_lint_scans_generated_github_prompts_for_unimplemented_workstate_cli(
    tmp_path: Path,
) -> None:
    """WORKSTATE-REF-53 implementation note (finding 288): `.github/prompts/*.prompt.md` is the
    Copilot-facing render of each skill. It is regenerated from the same source
    as `.claude/commands/`, so the same lint scope must cover it."""
    generated_dir = tmp_path / ".github" / "prompts"
    generated_dir.mkdir(parents=True, exist_ok=True)
    (generated_dir / "handoff-lifecycle.prompt.md").write_text(
        """<!-- GENERATED by scripts/generate_agent_workflows.py; do not edit by hand. -->

## Core Process

1. At session start, run `agentic context --json` from the target worktree.
""",
        encoding="utf-8",
    )

    proc = _run_facade_check(tmp_path)

    assert proc.returncode == 1
    assert ".github/prompts/handoff-lifecycle.prompt.md" in proc.stderr
    assert "agentic context" in proc.stderr or "agentic <verb>" in proc.stderr


def test_facade_lint_skips_orientation_scan_when_marker_is_absent(tmp_path: Path, monkeypatch) -> None:
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        "Use `load_session(...)` only after a scoped task has already been selected.\n",
    )

    def _boom(_: str) -> list[tuple[int, str]]:
        raise AssertionError("orientation scan should be skipped")

    monkeypatch.setattr(workflow_facade_check, "_orientation_blocks", _boom)

    assert workflow_facade_check.check_root(tmp_path) == []


def test_facade_lint_skips_oversized_sources(tmp_path: Path, monkeypatch) -> None:
    _write_skill_body(
        tmp_path,
        "handoff-lifecycle",
        "## Core Process\n\n"
        "1. At session start, use `load_session(...)` before any status check.\n"
        + ("x" * 512),
    )

    monkeypatch.setattr(workflow_facade_check, "MAX_SOURCE_BYTES", 64)

    assert workflow_facade_check.check_root(tmp_path) == []


def test_checked_in_handoff_lifecycle_prompt_carries_compaction_guidance() -> None:
    body = (PACKAGE_ROOT / ".github" / "prompts" / "handoff-lifecycle.prompt.md").read_text(
        encoding="utf-8"
    )

    assert "compaction_recommended: true" in body
    assert 'compaction(operation="record"' in body
    assert "unknown_harness: warn_and_skip" in body


def test_branch_lifecycle_source_and_generated_adapter_carry_root_venv_recovery() -> None:
    """WORKSTATE-REF-07 implementation note: the branch-lifecycle skill must teach that a fresh task
    worktree carries a root ``.venv`` and must name the recovery path for when
    ``pytest`` still resolves to a pyenv shim inside that worktree (rerun
    lifecycle provisioning or ``source .venv/bin/activate``). The guidance has to
    survive into the generated Copilot prompt adapter, not just the source body,
    so an operator reading any rendered surface gets the same recovery."""
    source = (PACKAGE_ROOT / "skills" / "branch-lifecycle" / "body.md").read_text(
        encoding="utf-8"
    )
    generated = (PACKAGE_ROOT / ".github" / "prompts" / "branch-lifecycle.prompt.md").read_text(
        encoding="utf-8"
    )

    for surface in (source, generated):
        assert "root `.venv`" in surface
        assert "pyenv" in surface
        assert "source .venv/bin/activate" in surface
