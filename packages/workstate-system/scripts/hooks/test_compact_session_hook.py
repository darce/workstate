"""Tests for the compact-session Stop hook (implementation note of WORKSTATE-REF-34).

The hook fires on the harness Stop event, derives the active task from
the workspace, and writes a structured ``session_compactions`` row via
``workstate_handoff_mcp.compact_session``. Per the failure-mode contract
(see WORKSTATE-REF-34 task plan, implementation note), the hook MUST exit 0 in every
operational outcome and surface its disposition on stderr:

- success          -> ``compaction_id=C-...``
- nothing-to-do    -> ``compaction skipped: <reason>``
- internal failure -> ``compaction failed: <reason>``

Strict-mode protocol violations (``AGENTIC_HOOK_PROTOCOL_STRICT=1``
plus a malformed event payload) remain the one exception and propagate
``SystemExit(2)`` via the shared ``_protocol.validate_event`` helper.
That contract is locked in by ``test_protocol_validation_wiring.py``
once ``compact-session.py`` is added to ``WIRED_HOOKS``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest

HOOK_SCRIPT = Path(__file__).parent / "compact-session.py"

PACKAGES_DIR = Path(__file__).resolve().parents[3]
HANDOFF_SRC = PACKAGES_DIR / "mcp-workstate-handoff" / "src"
PROTOCOL_SRC = PACKAGES_DIR / "workstate-protocol" / "src"


def _run_hook(
    payload: dict,
    *,
    workspace: Path,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLAUDE_PROJECT_DIR"] = str(workspace)
    env["AGENT_HANDOFF_STATE_DIR"] = str(workspace / ".task-state")
    # Pin PYTHONPATH at the in-repo sources so the hook subprocess
    # imports the worktree's workstate_handoff_mcp + workstate_protocol
    # rather than whichever copies the parent monorepo's venv has
    # editable-installed.
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
    """Isolated handoff workspace with an active task seeded.

    Drives the same configure_runtime path the hook will reach so the
    seeded row is visible to the subprocess.
    """
    for src in (PROTOCOL_SRC, HANDOFF_SRC):
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
    # Some sibling tests may have already imported a stale workstate_protocol
    # from the parent monorepo's venv; drop those modules so the worktree
    # source wins on the next import.
    for mod_name in list(sys.modules):
        if mod_name == "workstate_protocol" or mod_name.startswith("workstate_protocol."):
            del sys.modules[mod_name]
        if mod_name == "workstate_handoff_mcp" or mod_name.startswith("workstate_handoff_mcp."):
            del sys.modules[mod_name]

    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("AGENT_HANDOFF_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AGENT_HANDOFF_SKIP_SHA_VALIDATION", "1")
    monkeypatch.setenv("AGENT_HANDOFF_SKIP_BRANCH_ENFORCEMENT", "1")

    from workstate_handoff_mcp import RuntimeConfig, configure_runtime, set_handoff_state

    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    configure_runtime(runtime)
    set_handoff_state(
        task_ref="WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",
        objective="Test the compact-session Stop hook end-to-end.",
        status="in_progress",
        target_branch="feature/WORKSTATE-34",
    )
    yield tmp_path


def _write_transcript(workspace: Path, body: str) -> Path:
    transcript = workspace / "transcript.jsonl"
    transcript.write_text(body)
    return transcript


def test_compact_session_hook_writes_row(workspace: Path) -> None:
    """A real Stop event with a transcript persists one compaction row
    and prints ``compaction_id=...`` on stderr.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hello\nturn 2 assistant: world\nturn 3 user: bye\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-write-row",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
        "stop_hook_active": False,
    }

    result = _run_hook(payload, workspace=workspace)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction_id=" in result.stderr, (
        f"expected compaction_id= on stderr; got stderr={result.stderr!r}"
    )

    from workstate_handoff_mcp import get_latest_compaction

    latest = get_latest_compaction("WORKSTATE-REF-34-COMTASKCT-HOOK-TEST")
    assert latest is not None
    assert latest.session_id == "session-write-row"
    assert latest.harness == "claude-code"
    # Stable handle should round-trip from stderr to the persisted row.
    stderr_id = next(
        line.split("=", 1)[1].strip()
        for line in result.stderr.splitlines()
        if line.startswith("compaction_id=")
    )
    assert latest.compaction_id == stderr_id


def test_compact_session_hook_success_emits_receipt_fields(workspace: Path) -> None:
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hello\nturn 2 assistant: world\nturn 3 user: receipt\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-receipt-lines",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
        "stop_hook_active": False,
    }

    result = _run_hook(payload, workspace=workspace)

    assert result.returncode == 0, f"hook exited {result.returncode}; stderr={result.stderr!r}"
    lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
    assert lines[0].startswith("compaction_id=C-WORKSTATE-REF-34-COMTASKCT-HOOK-TEST-")
    assert [line.split("=", 1)[0] for line in lines[:5]] == [
        "compaction_id",
        "tokens_saved_estimate",
        "input_chars",
        "summary_chars",
        "prose_residual_chars",
    ]
    for line in lines[1:5]:
        key, raw_value = line.split("=", 1)
        assert raw_value.isdigit(), f"{key} must be an integer receipt value; got {line!r}"


def test_compact_session_hook_failure_is_non_fatal(
    workspace: Path, tmp_path: Path
) -> None:
    """A bogus state-dir surfaces as ``compaction failed:`` on stderr
    and exit 0 — never blocks the harness turn.
    """
    transcript = _write_transcript(workspace, "turn 1 user: hello\n")
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-fail",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }

    # Plant a regular file at the path the DB would want to create its
    # parent directory under. RuntimeConfig.for_repo will try to mkdir
    # the parent and fail, surfacing the documented failure path.
    bogus_parent = tmp_path / "blocker-file"
    bogus_parent.write_text("not a directory")
    bogus_state = bogus_parent / ".task-state"
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"AGENT_HANDOFF_STATE_DIR": str(bogus_state)},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction failed:" in result.stderr, (
        f"expected 'compaction failed:' on stderr; got stderr={result.stderr!r}"
    )
    assert "compaction_id=" not in result.stderr, (
        "failure path must not emit a compaction_id= envelope; "
        f"stderr={result.stderr!r}"
    )


def test_compact_session_hook_round_trips_into_cold_start_render(workspace: Path) -> None:
    """End-to-end synthetic-transcript fixture from the WORKSTATE-REF-34 plan
    Verification section: harness fires the Stop event -> hook writes a
    session_compactions row -> next process rendering the same task
    sees the structured cold-start block, dereferenced through
    compaction_id (never rowid).

    This locks in the cross-package contract that the hook (in
    packages/workstate-system) and the renderer (in
    packages/mcp-workstate-handoff) actually compose without an integration
    gap. Component tests cover each layer in isolation; this guards
    against the wiring drifting silently between releases.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: design the renderer\n"
        "turn 2 assistant: shipped renderer\n"
        "turn 3 user: end-to-end probe\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-e2e-cold-start",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }

    result = _run_hook(payload, workspace=workspace)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    compaction_id = next(
        line.split("=", 1)[1].strip()
        for line in result.stderr.splitlines()
        if line.startswith("compaction_id=")
    )

    from workstate_handoff_mcp import api as mcp_server

    rendered = mcp_server.render_handoff(
        kind="current_task",
        task_ref="WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",
        write_file=True,
    )
    assert rendered["ok"] is True, rendered

    # WORKSTATE-REF-67 implementation note: the v2 slim CURRENT_TASK.json projection does not
    # carry `cold_start_compaction` at the top level — that block is served
    # on demand by `render_cold_start_compaction(task_ref=...)`. Cold-start
    # consumers dereference compaction_id via that renderer (or via
    # `compaction(operation="get", compaction_id=...)`), not via a slim
    # projection field. Assert against the renderer instead.
    block = mcp_server.render_cold_start_compaction(
        task_ref="WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",
    )
    assert block is not None, (
        "render_cold_start_compaction must return a block after the "
        "Stop hook persists a row"
    )
    assert compaction_id in block, (
        f"cold-start block must dereference {compaction_id} written by "
        f"the hook; block={block!r}"
    )


def test_compact_session_hook_uses_env_harness(workspace: Path) -> None:
    """The hook must derive the harness from AGENT_HANDOFF_HARNESS so
    Codex / Cursor / manual callers don't get mislabeled as claude-code
    in session_compactions and StructuredSummary.

    Reproduces WORKSTATE-REF-34-BR-20260501-02.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hello\nturn 2 assistant: world\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-codex-harness",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }

    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"AGENT_HANDOFF_HARNESS": "codex"},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction_id=" in result.stderr

    from workstate_handoff_mcp import get_latest_compaction

    latest = get_latest_compaction("WORKSTATE-REF-34-COMTASKCT-HOOK-TEST")
    assert latest is not None
    assert latest.harness == "codex", (
        f"expected harness=codex from AGENT_HANDOFF_HARNESS env; "
        f"got harness={latest.harness!r}"
    )


def test_compact_session_hook_does_not_skip_on_new_session(workspace: Path) -> None:
    """A fresh session whose transcript restarts turn numbering at 1 must
    NOT be skipped just because the previous session reached a higher
    end_turn. The skip decision must be session-scoped.

    Reproduces WORKSTATE-REF-34-BR-20260501-01: prior session compaction had
    turn_range end=5, new session ships a 2-turn transcript -> the hook
    used to emit `compaction skipped: no new turns since ...` and never
    write a new row.
    """
    long_transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\nturn 3 user: how\n"
        "turn 4 assistant: fine\nturn 5 user: bye\n",
    )
    first_payload = {
        "hook_event_name": "Stop",
        "session_id": "session-long-prior",
        "transcript_path": str(long_transcript),
        "cwd": str(workspace),
    }
    first = _run_hook(first_payload, workspace=workspace)
    assert first.returncode == 0
    assert "compaction_id=" in first.stderr

    short_transcript = _write_transcript(
        workspace.parent / "short_transcript_dir" if False else workspace,
        # Distinct file so reading the prior compaction's turn-range
        # doesn't accidentally cover this content.
        "turn 1 user: ping\nturn 2 assistant: pong\n",
    )
    # New, different session_id with restarted turn numbering.
    second_payload = {
        "hook_event_name": "Stop",
        "session_id": "session-new-resumed",
        "transcript_path": str(short_transcript),
        "cwd": str(workspace),
    }
    second = _run_hook(second_payload, workspace=workspace)

    assert second.returncode == 0, (
        f"hook exited {second.returncode}; stderr={second.stderr!r}"
    )
    assert "compaction_id=" in second.stderr, (
        "new session must not be skipped just because turn numbering "
        f"restarted; stderr={second.stderr!r}"
    )
    assert "compaction skipped" not in second.stderr


def test_disabled_env_var_skips(workspace: Path) -> None:
    """``WORKSTATE_COMPACTION_DISABLED=1`` makes the hook emit a skip line
    and exit 0 without writing any session_compactions row.

    implementation note implementation note.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-disabled",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"WORKSTATE_COMPACTION_DISABLED": "1"},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction skipped: disabled" in result.stderr, (
        f"expected disabled-skip line; got stderr={result.stderr!r}"
    )
    assert "compaction_id=" not in result.stderr

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS n FROM session_compactions WHERE task_ref = ?",
            ("WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",),
        ).fetchone()
    assert row_count["n"] == 0, "disabled gate must not insert any row"


def test_min_new_turns_threshold_skips(workspace: Path) -> None:
    """``WORKSTATE_COMPACTION_MIN_NEW_TURNS`` larger than the transcript's
    turn count must short-circuit with a skip line.

    implementation note implementation note.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-min-turns",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"WORKSTATE_COMPACTION_MIN_NEW_TURNS": "100"},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction skipped: only" in result.stderr, (
        f"expected min-turns skip line; got stderr={result.stderr!r}"
    )
    assert "threshold 100" in result.stderr
    assert "compaction_id=" not in result.stderr


def test_min_new_tokens_threshold_skips(workspace: Path) -> None:
    """``WORKSTATE_COMPACTION_MIN_NEW_TOKENS`` larger than the encoded
    transcript token count must short-circuit with a skip line.

    implementation note implementation note.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-min-tokens",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"WORKSTATE_COMPACTION_MIN_NEW_TOKENS": "100000"},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction skipped: only" in result.stderr, (
        f"expected min-tokens skip line; got stderr={result.stderr!r}"
    )
    assert "threshold 100000" in result.stderr
    assert "compaction_id=" not in result.stderr


def test_agent_handoff_compaction_disabled_skips(workspace: Path) -> None:
    """``AGENT_HANDOFF_COMPACTION_DISABLED=1`` is the canonical
    consolidated env-var name (matches the package's dominant
    ``AGENT_HANDOFF_*`` prefix). The hook must short-circuit identically
    to the legacy ``WORKSTATE_COMPACTION_DISABLED=1`` and emit no
    deprecation noise when only the new name is set.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-disabled-new",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"AGENT_HANDOFF_COMPACTION_DISABLED": "1"},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction skipped: disabled" in result.stderr, (
        f"expected disabled-skip line; got stderr={result.stderr!r}"
    )
    assert "compaction_id=" not in result.stderr
    assert "deprecated" not in result.stderr.lower(), (
        "no deprecation warning expected when only the canonical name "
        f"is set; stderr={result.stderr!r}"
    )


def test_compaction_settings_db_disable_skips_hook(workspace: Path) -> None:
    """WORKSTATE-REF-67: a workspace-default ``compaction_settings`` row with
    ``enabled=0`` (the row that ``compaction(operation='disable')``
    writes) silences the Stop hook through the same unified resolver as
    the env var. Source label is ``db``.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )

    # Seed the workspace-default disable row by reusing the upsert helper
    # against the same workspace state dir the hook subprocess will hit.
    from workstate_handoff_mcp import RuntimeConfig, configure_runtime
    from workstate_handoff_mcp.compaction import upsert_compaction_disabled
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    configure_runtime(
        RuntimeConfig.for_repo(workspace, state_dir=workspace / ".task-state")
    )
    with _get_db_connection() as conn:
        upsert_compaction_disabled(
            conn,
            scope_kind="workspace",
            task_ref=None,
            enabled=False,
            actor="test",
        )
        conn.commit()

    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-db-disabled",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(payload, workspace=workspace)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction skipped: disabled (source=db)" in result.stderr, (
        f"expected db-disabled-skip line; got stderr={result.stderr!r}"
    )
    assert "compaction_id=" not in result.stderr


def test_compaction_settings_task_scoped_disable_skips_hook(workspace: Path) -> None:
    """WORKSTATE-REF-67-BR-01: task-scoped DB disables must silence the Stop hook
    for the active task, not only the advisory path.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )

    from workstate_handoff_mcp import RuntimeConfig, configure_runtime
    from workstate_handoff_mcp.compaction import upsert_compaction_disabled
    from workstate_handoff_mcp.shared_schema import _get_db_connection

    configure_runtime(
        RuntimeConfig.for_repo(workspace, state_dir=workspace / ".task-state")
    )
    with _get_db_connection() as conn:
        upsert_compaction_disabled(
            conn,
            scope_kind="task",
            task_ref="WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",
            enabled=False,
            actor="test",
        )
        conn.commit()

    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-task-db-disabled",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(payload, workspace=workspace)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction skipped: disabled (source=db)" in result.stderr, (
        f"expected task-scoped db-disabled-skip line; got stderr={result.stderr!r}"
    )
    assert "compaction_id=" not in result.stderr

    with _get_db_connection() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS n FROM session_compactions WHERE task_ref = ?",
            ("WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",),
        ).fetchone()["n"]
    assert row_count == 0


def test_legacy_WORKSTATE_prefix_still_works_with_deprecation_warning(
    workspace: Path,
) -> None:
    """One-release back-compat: ``WORKSTATE_COMPACTION_DISABLED=1`` still
    works but the hook emits a one-line deprecation warning naming the
    canonical replacement. Setting both the legacy and canonical names
    must prefer the canonical and stay quiet about the legacy duplicate.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-disabled-legacy",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    legacy_only = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"WORKSTATE_COMPACTION_DISABLED": "1"},
    )

    assert legacy_only.returncode == 0
    assert "compaction skipped: disabled" in legacy_only.stderr
    assert (
        "WORKSTATE_COMPACTION_DISABLED is deprecated; "
        "rename to AGENT_HANDOFF_COMPACTION_DISABLED"
    ) in legacy_only.stderr, (
        f"expected deprecation warning line; got stderr={legacy_only.stderr!r}"
    )

    both = _run_hook(
        payload,
        workspace=workspace,
        extra_env={
            "AGENT_HANDOFF_COMPACTION_DISABLED": "1",
            "WORKSTATE_COMPACTION_DISABLED": "1",
        },
    )
    assert both.returncode == 0
    assert "compaction skipped: disabled" in both.stderr
    assert "deprecated" not in both.stderr.lower(), (
        "canonical name wins; deprecation warning must not fire when "
        f"both are set; stderr={both.stderr!r}"
    )


def test_legacy_disabled_falsy_emits_single_deprecation_warning(
    workspace: Path,
) -> None:
    """BR-WORKSTATE-REF-55-01: when only the legacy alias is set with a falsy
    value (``WORKSTATE_COMPACTION_DISABLED=0``), the deprecation warning
    must fire exactly once. The pre-import gate used to emit it from
    ``main()`` and ``CompactionSettings.from_env()`` emitted it again
    once ``_compact()`` ran, so operators saw two identical lines.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-disabled-falsy-legacy",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"WORKSTATE_COMPACTION_DISABLED": "0"},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    deprecation_count = result.stderr.count(
        "WORKSTATE_COMPACTION_DISABLED is deprecated;"
    )
    assert deprecation_count == 1, (
        f"expected exactly one deprecation warning; got {deprecation_count}; "
        f"stderr={result.stderr!r}"
    )


def test_invalid_int_env_var_emits_compaction_failed(workspace: Path) -> None:
    """Bad int values for ``AGENT_HANDOFF_COMPACTION_MIN_NEW_TURNS`` must
    surface as a ``compaction failed: invalid compaction settings: ...``
    line via the typed ``CompactionSettings.from_env()`` boundary, not
    silently fall back to the default and write a compaction row.

    Reproduces BR-WORKSTATE-REF-34-COMPENV-01: implementation note added the typed
    surface but the live Stop hook kept its inline ``int(...)``
    fallback, so invalid env values still wrote a compaction row.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-bad-int",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(
        payload,
        workspace=workspace,
        extra_env={"AGENT_HANDOFF_COMPACTION_MIN_NEW_TURNS": "abc"},
    )

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction failed: invalid compaction settings:" in result.stderr, (
        "expected loud failure on invalid int env var; "
        f"got stderr={result.stderr!r}"
    )
    assert "compaction_id=" not in result.stderr, (
        "invalid env value must not silently fall back to default and "
        f"persist a row; stderr={result.stderr!r}"
    )

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS n FROM session_compactions WHERE task_ref = ?",
            ("WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",),
        ).fetchone()
    assert row_count["n"] == 0, (
        "invalid-settings path must not insert any row; "
        f"saw {row_count['n']} rows"
    )


def test_thresholds_default_match_pre_WORKSTATE_39_behavior(workspace: Path) -> None:
    """With no WORKSTATE_COMPACTION_* env vars set, the hook must behave
    identically to the pre-WORKSTATE-REF-39 baseline: a 2-turn transcript
    persists a single compaction row.

    implementation note implementation note — default-preservation gate. The full pre-existing
    WORKSTATE-REF-34 test suite running unchanged is the broader baseline; this
    case anchors the contract directly inside the slice's own file.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hi\nturn 2 assistant: hello\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-defaults",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }
    result = _run_hook(payload, workspace=workspace)

    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction_id=" in result.stderr
    assert "compaction skipped" not in result.stderr

    from workstate_handoff_mcp import get_latest_compaction

    latest = get_latest_compaction("WORKSTATE-REF-34-COMTASKCT-HOOK-TEST")
    assert latest is not None
    assert latest.session_id == "session-defaults"


def test_compact_session_hook_skips_when_no_new_turns(workspace: Path) -> None:
    """A second invocation against an unchanged transcript head must
    short-circuit with ``compaction skipped:`` instead of writing a
    duplicate row.
    """
    transcript = _write_transcript(
        workspace,
        "turn 1 user: hello\nturn 2 assistant: world\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-skip",
        "transcript_path": str(transcript),
        "cwd": str(workspace),
    }

    first = _run_hook(payload, workspace=workspace)
    assert first.returncode == 0
    assert "compaction_id=" in first.stderr

    second = _run_hook(payload, workspace=workspace)
    assert second.returncode == 0, (
        f"second invocation exited {second.returncode}; "
        f"stderr={second.stderr!r}"
    )
    assert "compaction skipped:" in second.stderr, (
        f"expected 'compaction skipped:' on second invocation; "
        f"stderr={second.stderr!r}"
    )
    assert "compaction_id=" not in second.stderr

    from workstate_handoff_mcp.shared_schema import _get_db_connection

    with _get_db_connection() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS n FROM session_compactions WHERE task_ref = ?",
            ("WORKSTATE-REF-34-COMTASKCT-HOOK-TEST",),
        ).fetchone()
    assert row_count["n"] == 1, (
        "skip path must not insert a second row; "
        f"saw {row_count['n']} rows"
    )


# ---------------------------------------------------------------------------
# WORKSTATE-REF-54 implementation note.5e — Resolution-Rule shape coverage
#
# The hook's task-ref derivation routes through the server-side four-step
# Resolution Rule (``shared_primitives._resolve_task_ref`` ->
# ``resolve_active_task_ref``). The existing tests above all run under the
# ``single`` projection shape. The two tests below pin the hook's
# failure-mode contract under the other two shapes from the WORKSTATE-REF-54
# workspace summary contract: ``none`` (no live task) and
# ``workspace_ambiguous`` (multiple live tasks with no
# target_worktree_path / cwd disambiguator). In both cases the hook MUST
# exit 0 with ``compaction failed: active task unresolved`` on stderr —
# never blocking the harness turn — per the implementation note failure-mode contract
# documented at the top of this file.
# ---------------------------------------------------------------------------


def _seed_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure the in-process MCP runtime against ``tmp_path`` without
    seeding any handoff_state row. Mirrors the prefix of the ``workspace``
    fixture so the two ``shape`` fixtures below stay structurally aligned
    with the single-shape baseline.
    """
    for src in (PROTOCOL_SRC, HANDOFF_SRC):
        if str(src) not in sys.path:
            sys.path.insert(0, str(src))
    for mod_name in list(sys.modules):
        if mod_name == "workstate_protocol" or mod_name.startswith("workstate_protocol."):
            del sys.modules[mod_name]
        if mod_name == "workstate_handoff_mcp" or mod_name.startswith("workstate_handoff_mcp."):
            del sys.modules[mod_name]

    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("AGENT_HANDOFF_STATE_DIR", str(state_dir))
    monkeypatch.setenv("AGENT_HANDOFF_SKIP_SHA_VALIDATION", "1")
    monkeypatch.setenv("AGENT_HANDOFF_SKIP_BRANCH_ENFORCEMENT", "1")

    from workstate_handoff_mcp import RuntimeConfig, configure_runtime

    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    configure_runtime(runtime)
    return tmp_path


@pytest.fixture()
def workspace_no_active_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Workspace with the runtime configured but zero handoff_state rows.

    Exercises the ``none`` projection shape: the four-step resolver
    finds no live tasks and raises ``ValueError("No active task ...")``.
    """
    yield _seed_runtime(tmp_path, monkeypatch)


@pytest.fixture()
def workspace_ambiguous_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Workspace with two active tasks and no target_worktree_path on
    either row — neither matches cwd, so step 3+4 of the resolver raises
    ``AmbiguousWorkspaceContextError``. Pins the ``workspace_ambiguous``
    projection shape.
    """
    workspace = _seed_runtime(tmp_path, monkeypatch)
    from workstate_handoff_mcp import set_handoff_state

    set_handoff_state(
        task_ref="WORKSTATE-REF-54-COMTASKCT-AMBIG-A",
        objective="Ambiguous-shape fixture row A.",
        status="in_progress",
        target_branch="feature/ambig-a",
    )
    set_handoff_state(
        task_ref="WORKSTATE-REF-54-COMTASKCT-AMBIG-B",
        objective="Ambiguous-shape fixture row B.",
        status="in_progress",
        target_branch="feature/ambig-b",
    )
    yield workspace


def _assert_unresolved_failure(result: subprocess.CompletedProcess) -> None:
    """Shared assertion bundle for both shape failure cases.

    Pins the failure-mode contract: returncode 0, the documented stderr
    envelope, and no ``compaction_id=`` leak (which would imply a row
    was written despite the resolver failure).
    """
    assert result.returncode == 0, (
        f"hook exited {result.returncode}; stderr={result.stderr!r}"
    )
    assert "compaction failed: active task unresolved" in result.stderr, (
        "expected 'compaction failed: active task unresolved' on stderr; "
        f"got stderr={result.stderr!r}"
    )
    assert "compaction_id=" not in result.stderr, (
        "unresolved-task path must not emit a compaction_id= envelope; "
        f"stderr={result.stderr!r}"
    )


def test_compact_session_hook_none_shape_exits_clean(
    workspace_no_active_task: Path,
) -> None:
    """``none`` shape: the four-step resolver finds zero live tasks and
    raises. The hook MUST surface ``compaction failed: active task
    unresolved`` and exit 0 so the harness turn is not blocked. Covers
    WORKSTATE-REF-54 implementation note.5e proof for the ``none`` projection shape.
    """
    transcript = _write_transcript(
        workspace_no_active_task,
        "turn 1 user: hello\nturn 2 assistant: world\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-none-shape",
        "transcript_path": str(transcript),
        "cwd": str(workspace_no_active_task),
    }
    _assert_unresolved_failure(
        _run_hook(payload, workspace=workspace_no_active_task)
    )


def test_compact_session_hook_workspace_ambiguous_shape_exits_clean(
    workspace_ambiguous_tasks: Path,
) -> None:
    """``workspace_ambiguous`` shape: two live tasks with no
    target_worktree_path on either row. The four-step resolver step 4
    raises ``AmbiguousWorkspaceContextError`` (no last-writer-wins
    fallback). The hook MUST surface ``compaction failed: active task
    unresolved`` carrying the structured ambiguity message and exit 0.
    Covers WORKSTATE-REF-54 implementation note.5e proof for the ``workspace_ambiguous``
    projection shape.
    """
    transcript = _write_transcript(
        workspace_ambiguous_tasks,
        "turn 1 user: hello\nturn 2 assistant: world\n",
    )
    payload = {
        "hook_event_name": "Stop",
        "session_id": "session-ambiguous-shape",
        "transcript_path": str(transcript),
        "cwd": str(workspace_ambiguous_tasks),
    }
    result = _run_hook(payload, workspace=workspace_ambiguous_tasks)
    _assert_unresolved_failure(result)
    assert "Ambiguous active task" in result.stderr, (
        "ambiguous-shape failure must propagate the structured "
        "AmbiguousWorkspaceContextError message so operators can see the "
        f"candidate task_refs; got stderr={result.stderr!r}"
    )
