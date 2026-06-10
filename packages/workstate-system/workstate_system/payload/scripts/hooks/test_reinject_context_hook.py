"""Tests for the reinject-context SessionStart hook (implementation note of internal).

The hook fires on the harness ``SessionStart`` event, gates on the event
``source`` (default: ``compact`` / ``resume``), resolves the active task
from the workspace, and emits ONE budgeted fenced block of handoff.db
references to **stdout** — the surface Claude Code injects into model
context. Per the failure-mode contract (implementation note, implementation note) the hook MUST
exit 0 in every operational outcome, emit nothing on stdout unless a block
is produced, and surface its disposition on stderr:

- success     -> fenced ```workstate-reinject block on stdout
- gated/noop  -> ``reinject skipped: <reason>`` on stderr, empty stdout
- any failure -> ``reinject skipped: <reason>`` on stderr, empty stdout

The hook is strictly read-only: it must never write handoff.db rows.

Strict-mode protocol violations (``WORKSTATE_HOOK_PROTOCOL_STRICT=1`` plus
a malformed event payload) remain the one exception and propagate
``SystemExit(2)`` via the shared ``_protocol.validate_event`` helper.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterator

import pytest

HOOK_SCRIPT = Path(__file__).parent / "reinject-context.py"
CONTRACT_PATH = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "workstate"
    / "contracts"
    / "harness-protocol.yaml"
)

PACKAGES_DIR = Path(__file__).resolve().parents[3]
HANDOFF_SRC = PACKAGES_DIR / "mcp-workstate-handoff" / "src"
PROTOCOL_SRC = PACKAGES_DIR / "workstate-protocol" / "src"
WORKSTATE_PACKAGE_PREFIXES = ("workstate_protocol", "workstate_handoff_mcp")

TASK_REF = "internal"


def _is_workstate_module(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(f"{prefix}.")
        for prefix in WORKSTATE_PACKAGE_PREFIXES
    )


def _prepare_source_imports() -> tuple[list[str], dict[str, ModuleType]]:
    saved_path = list(sys.path)
    saved_modules = {
        name: module
        for name, module in sys.modules.items()
        if _is_workstate_module(name)
    }
    for src in (PROTOCOL_SRC, HANDOFF_SRC):
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
    for mod_name in list(sys.modules):
        if _is_workstate_module(mod_name):
            del sys.modules[mod_name]
    return saved_path, saved_modules


def _restore_source_imports(
    saved_path: list[str], saved_modules: dict[str, ModuleType]
) -> None:
    sys.path[:] = saved_path
    for mod_name in list(sys.modules):
        if _is_workstate_module(mod_name):
            del sys.modules[mod_name]
    sys.modules.update(saved_modules)


def _run_hook(
    payload: dict,
    *,
    workspace: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(workspace)
    env["WORKSTATE_HANDOFF_STATE_DIR"] = str(workspace / ".task-state")
    # Pin PYTHONPATH at the in-repo sources so the hook subprocess imports
    # the worktree's workstate_handoff_mcp + workstate_protocol rather than
    # whichever copies the parent monorepo's venv has editable-installed.
    existing_pp = env.get("PYTHONPATH", "")
    parts = [str(HANDOFF_SRC), str(PROTOCOL_SRC)]
    if existing_pp:
        parts.append(existing_pp)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(workspace),
    )


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Isolated handoff workspace with an active task seeded."""
    saved_path, saved_modules = _prepare_source_imports()
    try:
        state_dir = tmp_path / ".task-state"
        state_dir.mkdir(parents=True, exist_ok=True)

        monkeypatch.setenv("WORKSTATE_HANDOFF_STATE_DIR", str(state_dir))
        monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION", "1")
        monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT", "1")

        from workstate_handoff_mcp import (
            RuntimeConfig,
            configure_runtime,
            set_handoff_state,
        )

        runtime = RuntimeConfig.for_workspace(
            tmp_path,
            state_dir=state_dir,
            current_task_path=tmp_path / "CURRENT_TASK.json",
        )
        configure_runtime(runtime)
        set_handoff_state(
            task_ref=TASK_REF,
            objective="Test the reinject-context SessionStart hook end-to-end.",
            status="in_progress",
            target_branch="feature/ws-reinj-01",
        )
        yield tmp_path
    finally:
        _restore_source_imports(saved_path, saved_modules)


def _seed_compaction_row(workspace: Path) -> str:
    """Persist one session_compactions row for TASK_REF; return its id."""
    transcript = workspace / "transcript.jsonl"
    transcript.write_text(
        "turn 1 user: design the hook\nturn 2 assistant: shipped\nturn 3 user: probe\n"
    )
    from workstate_handoff_mcp import compact_session

    receipt = compact_session(
        transcript_path=str(transcript),
        task_ref=TASK_REF,
        harness="claude-code",
        session_id="seed-session",
    )
    return receipt.summary.compaction_id


def _stdout_injection(result: subprocess.CompletedProcess) -> str:
    """Return injected context from stdout (raw block or Claude JSON envelope)."""
    stdout = result.stdout
    if not stdout.strip():
        return ""
    if stdout.lstrip().startswith("{"):
        envelope = json.loads(stdout)
        return str(envelope["hookSpecificOutput"]["additionalContext"])
    return stdout


def _payload(source: str | None, session_id: str = "session-reinject") -> dict:
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "cwd": "",
    }
    if source is not None:
        payload["source"] = source
    return payload


def _db_write_snapshot() -> dict[str, int]:
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        return {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in ("session_compactions", "decisions", "handoff_state")
        }


def test_reinject_emits_block_on_compact_source(workspace: Path) -> None:
    """source=compact emits ONE fenced block on stdout carrying task_ref,
    the latest compaction_id, and the literal deep-recovery command hints —
    and performs zero handoff.db writes.
    """
    compaction_id = _seed_compaction_row(workspace)
    before = _db_write_snapshot()

    result = _run_hook(_payload("compact"), workspace=workspace)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "reinject skipped" not in result.stderr, result.stderr
    block = _stdout_injection(result)
    assert block.startswith("```workstate-reinject"), f"stdout={result.stdout!r}"
    assert block.rstrip().endswith("```"), f"stdout={block!r}"
    assert TASK_REF in block
    assert compaction_id in block, (
        f"block must dereference latest compaction row; stdout={block!r}"
    )
    assert "in_progress" in block
    # Literal command hints for deeper agent-initiated recovery.
    assert "compaction(get_latest)" in block
    assert 'get_handoff_state(read_profile="hot_summary")' in block

    after = _db_write_snapshot()
    assert after == before, (
        f"reinject hook must be read-only; row counts drifted {before} -> {after}"
    )


def test_reinject_emits_block_on_resume_source(workspace: Path) -> None:
    result = _run_hook(_payload("resume"), workspace=workspace)

    assert result.returncode == 0
    block = _stdout_injection(result)
    assert block.startswith("```workstate-reinject")
    assert TASK_REF in block


def test_reinject_notify_claude_emits_json_envelope(workspace: Path) -> None:
    """internal: Claude + notify-on wraps block in SessionStart JSON."""
    compaction_id = _seed_compaction_row(workspace)
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={"WORKSTATE_HANDOFF_HARNESS": "claude-code"},
    )
    assert result.returncode == 0, result.stderr
    envelope = json.loads(result.stdout)
    assert envelope["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    block = envelope["hookSpecificOutput"]["additionalContext"]
    assert block.startswith("```workstate-reinject")
    assert compaction_id in block
    assert envelope["systemMessage"].startswith("workstate: re-fed compaction")
    assert TASK_REF in envelope["systemMessage"]


def test_reinject_notify_off_emits_raw_block_on_claude(workspace: Path) -> None:
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={
            "WORKSTATE_HANDOFF_HARNESS": "claude-code",
            "WORKSTATE_HANDOFF_COMPACTION_NOTIFY": "0",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("```workstate-reinject")
    assert "systemMessage" not in result.stdout


def test_reinject_notify_codex_emits_raw_block(workspace: Path) -> None:
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={"WORKSTATE_HANDOFF_HARNESS": "codex"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("```workstate-reinject")
    assert not result.stdout.lstrip().startswith("{")


def test_reinject_notify_grok_env_emits_raw_block(workspace: Path) -> None:
    """implementation note R1 / REV-E-010: a grok launcher sets GROK_WORKSPACE_ROOT but
    no WORKSTATE_HANDOFF_HARNESS export (the compat-loaded .claude entry must
    not carry one). _resolve_harness must classify this as grok — NOT fall
    through to the claude-code default and emit the Claude-only JSON envelope.
    Mirrors compact-session.py's grok fallback so both hooks agree.
    """
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        # Force the harness override empty so the GROK_WORKSPACE_ROOT
        # fallback is exercised deterministically regardless of ambient env.
        extra_env={
            "WORKSTATE_HANDOFF_HARNESS": "",
            "GROK_WORKSPACE_ROOT": str(workspace),
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("```workstate-reinject"), (
        "grok must receive the raw fenced block, not the Claude JSON "
        f"envelope; got stdout={result.stdout[:80]!r}"
    )
    assert not result.stdout.lstrip().startswith("{")
    assert "systemMessage" not in result.stdout


def test_reinject_notify_claude_context_parity_with_raw_block(workspace: Path) -> None:
    """Envelope additionalContext must match the raw fenced block byte-for-byte."""
    _seed_compaction_row(workspace)
    raw = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={
            "WORKSTATE_HANDOFF_HARNESS": "codex",
            "WORKSTATE_HANDOFF_COMPACTION_NOTIFY": "1",
        },
    )
    wrapped = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={
            "WORKSTATE_HANDOFF_HARNESS": "claude-code",
            "WORKSTATE_HANDOFF_COMPACTION_NOTIFY": "1",
        },
    )
    assert raw.returncode == 0 and wrapped.returncode == 0
    assert _stdout_injection(wrapped).rstrip("\n") == raw.stdout.rstrip("\n")


def test_reinject_block_without_compaction_row_still_emits_state(
    workspace: Path,
) -> None:
    """No session_compactions row yet: the block still carries task identity
    but no compaction line.
    """
    result = _run_hook(_payload("compact"), workspace=workspace)

    assert result.returncode == 0
    block = _stdout_injection(result)
    assert block.startswith("```workstate-reinject")
    assert TASK_REF in block
    assert "latest_compaction" not in block


def test_reinject_skips_on_startup_source_by_default(workspace: Path) -> None:
    """Default source gate excludes startup so ordinary session starts are
    not taxed next to load_session guidance.
    """
    result = _run_hook(_payload("startup"), workspace=workspace)

    assert result.returncode == 0
    assert result.stdout == "", (
        f"gated source must emit nothing; stdout={result.stdout!r}"
    )
    assert "reinject skipped: source" in result.stderr, result.stderr


def test_reinject_sources_env_override(workspace: Path) -> None:
    """WORKSTATE_REINJECT_SOURCES extends the gate (comma list)."""
    result = _run_hook(
        _payload("startup"),
        workspace=workspace,
        extra_env={"WORKSTATE_REINJECT_SOURCES": "startup,compact"},
    )

    assert result.returncode == 0, result.stderr
    block = _stdout_injection(result)
    assert block.startswith("```workstate-reinject"), (
        f"startup must emit once allowlisted; stderr={result.stderr!r}"
    )


def test_reinject_budget_truncation(workspace: Path) -> None:
    """WORKSTATE_REINJECT_BUDGET_CHARS caps total stdout chars while keeping
    the fence closed.
    """
    _seed_compaction_row(workspace)
    budget = 200
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={
            "WORKSTATE_REINJECT_BUDGET_CHARS": str(budget),
            "WORKSTATE_HANDOFF_COMPACTION_NOTIFY": "0",
        },
    )

    assert result.returncode == 0
    assert result.stdout, "block must still be emitted under a small budget"
    assert len(result.stdout) <= budget, (
        f"stdout must fit the {budget}-char budget; got {len(result.stdout)}"
    )
    assert result.stdout.startswith("```workstate-reinject")
    assert result.stdout.rstrip().endswith("```"), (
        f"truncation must keep the fence closed; stdout={result.stdout!r}"
    )


def test_reinject_budget_below_task_ref_floor_skips(workspace: Path) -> None:
    """A budget too small to fit fences + the mandatory task_ref line emits
    NOTHING (no contentless fence pair) and exits 0.
    """
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={"WORKSTATE_REINJECT_BUDGET_CHARS": "10"},
    )

    assert result.returncode == 0
    assert result.stdout == "", (
        f"sub-floor budget must emit nothing; stdout={result.stdout!r}"
    )
    assert "reinject skipped: budget" in result.stderr, result.stderr


def test_reinject_minimal_budget_always_carries_task_ref(workspace: Path) -> None:
    """The smallest emitting budget still carries the task_ref line — the
    block is never an empty fence pair.
    """
    floor = (
        len("\n".join(["```workstate-reinject", f"task_ref: {TASK_REF}", "```"])) + 1
    )
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={
            "WORKSTATE_REINJECT_BUDGET_CHARS": str(floor),
            "WORKSTATE_HANDOFF_COMPACTION_NOTIFY": "0",
        },
    )

    assert result.returncode == 0, result.stderr
    assert len(result.stdout) <= floor
    content = [
        line
        for line in result.stdout.splitlines()
        if line not in ("```workstate-reinject", "```")
    ]
    assert content == [f"task_ref: {TASK_REF}"], (
        f"floor-budget block must carry exactly the task_ref line; "
        f"stdout={result.stdout!r}"
    )


def test_reinject_sanitizes_fence_tokens_in_field_values(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Agent-authored field values containing ``` or newlines must not close
    the injected fence early: exactly one closing fence, as the final line.
    """
    from workstate_handoff_mcp import get_handoff_state, set_handoff_state

    # The tmp workspace has no real worktree for the seeded target_branch;
    # skip derivation (standard test bypass) so the focus update lands.
    monkeypatch.setenv("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "1")
    identity = get_handoff_state(task_ref=TASK_REF, sections="identity")
    revision = identity["data"]["active"]["revision"]
    update = set_handoff_state(
        task_ref=TASK_REF,
        focus="evil\n```\ninjected fence line",
        status="in_progress",
        expected_revision=revision,
    )
    assert update.get("ok"), f"focus update must land: {update!r}"

    result = _run_hook(_payload("compact"), workspace=workspace)

    assert result.returncode == 0, result.stderr
    block_lines = _stdout_injection(result).rstrip("\n").splitlines()
    assert block_lines[0] == "```workstate-reinject"
    assert block_lines[-1] == "```"
    interior = block_lines[1:-1]
    assert all(not line.startswith("```") for line in interior), (
        f"sanitized block must not contain an interior fence; "
        f"stdout={result.stdout!r}"
    )
    focus_lines = [line for line in interior if line.startswith("focus: ")]
    assert focus_lines == ["focus: evil `` injected fence line"], (
        f"focus must be flattened + fence-token-stripped; interior={interior!r}"
    )


def test_reinject_invalid_budget_skips(workspace: Path) -> None:
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={"WORKSTATE_REINJECT_BUDGET_CHARS": "abc"},
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "reinject skipped: invalid budget" in result.stderr, result.stderr


def test_reinject_no_active_task_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Zero handoff_state rows: hook must skip cleanly, never block the
    session start.
    """
    saved_path, saved_modules = _prepare_source_imports()
    try:
        state_dir = tmp_path / ".task-state"
        state_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv("WORKSTATE_HANDOFF_STATE_DIR", str(state_dir))

        from workstate_handoff_mcp import RuntimeConfig, configure_runtime

        configure_runtime(
            RuntimeConfig.for_workspace(
                tmp_path,
                state_dir=state_dir,
                current_task_path=tmp_path / "CURRENT_TASK.json",
            )
        )

        result = _run_hook(_payload("compact"), workspace=tmp_path)

        assert result.returncode == 0
        assert result.stdout == ""
        assert "reinject skipped: active task unresolved" in result.stderr, (
            result.stderr
        )
    finally:
        _restore_source_imports(saved_path, saved_modules)


def test_reinject_disable_resolver_silences(workspace: Path) -> None:
    """A disabled compaction surface (internal unified resolver) also
    silences re-injection.
    """
    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={"WORKSTATE_HANDOFF_COMPACTION_DISABLED": "1"},
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "reinject skipped: disabled" in result.stderr, result.stderr


def test_reinject_db_unreachable_is_non_fatal(workspace: Path, tmp_path: Path) -> None:
    """A bogus state-dir surfaces as ``reinject skipped:`` on stderr and
    exit 0 — never blocks the session start.
    """
    bogus_parent = tmp_path / "blocker-file"
    bogus_parent.write_text("not a directory")
    bogus_state = bogus_parent / ".task-state"

    result = _run_hook(
        _payload("compact"),
        workspace=workspace,
        extra_env={"WORKSTATE_HANDOFF_STATE_DIR": str(bogus_state)},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert result.stdout == ""
    assert "reinject skipped:" in result.stderr, result.stderr


def test_reinject_strict_mode_protocol_drift_exits_2(workspace: Path) -> None:
    """WORKSTATE_HOOK_PROTOCOL_STRICT=1 plus a wrong-event payload propagates
    SystemExit(2), matching every other wired hook.
    """
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-strict",
        "source": "compact",
    }
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"WORKSTATE_HOOK_PROTOCOL_STRICT": "1"},
    )

    assert result.returncode == 2, (
        f"strict protocol drift must exit 2; rc={result.returncode} "
        f"stderr={result.stderr!r}"
    )
    assert result.stdout == ""


def test_reinject_malformed_stdin_skips(workspace: Path) -> None:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(workspace)
    env["WORKSTATE_HANDOFF_STATE_DIR"] = str(workspace / ".task-state")
    parts = [str(HANDOFF_SRC), str(PROTOCOL_SRC)]
    if env.get("PYTHONPATH"):
        parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(parts)
    result = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT)],
        input="not json {",
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(workspace),
    )

    assert result.returncode == 0
    assert result.stdout == ""
    assert "reinject skipped: malformed stdin payload" in result.stderr


# ---------------------------------------------------------------------------
# Contract block — harness-protocol.yaml `reinjection:` section (implementation note
# implementation note). Text-level assertions, matching the doc-test pattern in
# test_dev_workflow_compaction_docs.py (the payload test venv does not
# declare a YAML parser dependency).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def contract_text() -> str:
    return CONTRACT_PATH.read_text(encoding="utf-8")


def test_contract_has_reinjection_block(contract_text: str) -> None:
    assert "\nreinjection:" in contract_text, (
        "harness-protocol.yaml must declare a top-level `reinjection:` block "
        "as the single documented source for the hook's tunables (implementation note)."
    )


@pytest.mark.parametrize(
    "marker",
    [
        "WORKSTATE_REINJECT_SOURCES",
        "WORKSTATE_REINJECT_BUDGET_CHARS",
        "WORKSTATE_HANDOFF_COMPACTION_NOTIFY",
        "budget_chars: 1500",
        "- compact",
        "- resume",
        "reinject-context.py",
    ],
)
def test_contract_documents_reinjection_tunables(
    contract_text: str, marker: str
) -> None:
    # Bound the slice at the next top-level key so markers that only appear
    # in later sections (e.g. `orchestrator:`) cannot satisfy the assertion.
    tail = contract_text.split("\nreinjection:", 1)[-1]
    boundary = re.search(r"\n[A-Za-z_][A-Za-z0-9_-]*:", tail)
    reinjection_block = tail[: boundary.start()] if boundary else tail
    assert marker in reinjection_block, (
        f"`reinjection:` contract block must mention {marker!r}"
    )
