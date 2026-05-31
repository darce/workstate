"""Compression-ratio benchmark for WORKSTATE-REF-34 cold-start summaries.

Verifies that the structured cold-start block produced by
``render_cold_start_compaction`` is at least 50% shorter (in tokens) than
the prose summary the harness would otherwise retain. The plan
(``packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-34-cross-harness-session-compaction-task-plan.md``,
Verification section) requires the check across three real in-repo
handoffs landed on this branch (implementation note implementation, implementation note
trigger wiring, implementation note cold-start renderer).

The "prose summary the harness would otherwise retain" is *not* just
the final ``slice_complete`` rationale — a real harness session
retains the cohort of decision prose the session produced (scope
intake + implementation + verification narrative) plus inter-turn
chatter. We approximate that for each sample by concatenating the
real decision rationales that landed in that slice's session
(decisions 38+46+47 for implementation note; decision 50 alone for implementation note,
since its rationale is itself a dense multi-section narrative;
decision 52 alone for implementation note, same shape). This is closer to the
prose envelope a harness ``/compact`` would distill from the same
session than the slice-complete row alone would be.

A separate finding worth noting: when prose is shorter than the
structured ``files_touched`` floor (~270 tokens for a 13-file
slice), structured form has no headroom to compress against and the
comparison degenerates — i.e., the cold-start block is most
effective once prose archive ≥ ~400 tokens, which is the regime
real multi-turn handoffs land in.

Token counts use ``tiktoken`` with the ``cl100k_base`` encoding. No
existing helper in ``workstate_handoff_mcp`` uses tiktoken, so we pick the
encoder directly here. ``cl100k_base`` is the widely-used GPT-4
encoding; it's a stable cross-model proxy for "how many tokens the
harness summary would consume" and the same encoder applies to both
sides of the ratio so the comparison is internally consistent.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tiktoken
from workstate_protocol import StructuredSummary, TurnRange

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import _get_db_connection


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    return mcp_server.configure_runtime(
        RuntimeConfig.for_repo(
            tmp_path,
            state_dir=state_dir,
            current_task_path=tmp_path / "CURRENT_TASK.json",
        )
    )


_ENCODING = tiktoken.get_encoding("cl100k_base")


_SAMPLE_SLICE2_PROSE = """## Changes
- Added `packages/workstate-system/scripts/hooks/compact-session.py`, the Stop-hook entrypoint chosen by `scope_intake_WORKSTATE-34_trigger_choice` (Option B). It validates the payload via the shared `_protocol.validate_event` helper, derives the active task with `_resolve_task_ref(conn, None)`, short-circuits when the transcript head's `turn_range.end_turn` does not exceed the latest stored compaction's, and otherwise calls `compact_session(...)` and prints `compaction_id=C-...` to stderr.
- Reused the existing `packages/workstate-protocol/schemas/hook-stop.json`; no new protocol artefact was needed because `transcript_path` and `session_id` are already on the StopEvent contract and `_protocol.py` already registers `Stop`.
- Added `("compact-session.py", "Stop")` to `WIRED_HOOKS` in `test_protocol_validation_wiring.py` so the strict-mode and lenient-mode parametrized contracts cover the new hook.
- Added `packages/workstate-system/scripts/hooks/test_compact_session_hook.py` with `test_compact_session_hook_writes_row`, `test_compact_session_hook_failure_is_non_fatal`, and `test_compact_session_hook_skips_when_no_new_turns`. The fixture seeds an isolated handoff workspace and pins worktree sources on PYTHONPATH so the subprocess imports the in-tree contract, not the parent venv's stale copy.
- Updated the WORKSTATE-REF-34 task plan slice-2 checklist to mark trigger wiring, schema/protocol coverage, and failure-mode evidence complete.

## Verification
- `cd packages/workstate-system && uv run pytest scripts/hooks/ -q` -> `177 passed in 15.00s` (test ids 35).
- `cd packages/mcp-workstate-handoff && uv run --extra test pytest tests/test_compaction.py tests/test_schema_migrations.py -q` -> `23 passed in 3.66s` (test id 36); slice-1 regressions still green after slice-2 lands.

## Schema / Contract Changes
- No new schemas. `hook-stop.json` is reused as-is; `compact-session.py` is now part of the `WIRED_HOOKS` strict-validation contract that locks every wired hook against `_protocol.validate_event` drift.

## Open Threads
- implementation note (cold-start consumer in `make context` / `check-task-context.py`) remains; the structured summary the hook now persists is consumed there.
- The hook reads `WORKSTATE_HANDOFF_STATE_DIR` to mirror the MCP server CLI; production callers leave it unset and the primary-worktree `.task-state` wins. Worth noting in cross-harness fallback documentation alongside the per-harness Stop event references.
"""


_SAMPLE_SLICE3_PROSE = """## Changes

Added `render_cold_start_compaction(task_ref)` to `workstate_handoff_mcp.compaction` and re-exported it through `core.py`, `api.py`, and `__init__.py`. The helper renders the latest `session_compactions` row for the resolved task as an ID-only cold-start block (compaction_id, Turns range, decisions, findings_fixed, tests_verified, files_touched). Returns `None` when no row exists, so consumer cold-start scripts emit the byte-identical pre-WORKSTATE-REF-34 baseline. Appends a one-line `(compaction stale; <N> decisions newer)` advisory when `decisions.created_at` exceeds the stored compaction's `created_at`.

## Verification

`PYTHONPATH=...:.../mcp-workstate-handoff/src python -m pytest packages/mcp-workstate-handoff/tests/test_compaction.py` — 16 passed in 3.52s. Includes 4 new tests: `test_cold_start_no_compaction_matches_baseline`, `test_cold_start_renders_structured_summary`, `test_cold_start_dereferences_by_compaction_id`, `test_cold_start_stale_compaction_emits_advisory`. Public API import smoke-tested via `from workstate_handoff_mcp import render_cold_start_compaction`.

## Schema / Contract Changes

No schema changes. Adds one new public function to the `workstate_handoff_mcp` surface and `__all__`. Reuses the existing `StructuredSummary` contract and `session_compactions` table; queries `decisions.created_at` (the table is append-only, no `updated_at` column exists despite the plan's wording).

## Open Threads

- Consumer-side wiring: the actual `check-task-context.py` lives in consumer repos (e.g. `example-repo`), not in this monorepo. Those scripts will pick up `render_cold_start_compaction` on their next overlay reinstall. No change shipped here for that surface.
- Plan drift: `decisions.updated_at` referenced in the plan does not exist; implementation uses `created_at`. Documented inline in the implementation note checklist.
"""


_SAMPLE_SLICE1_SCOPE_INTAKE_PROSE = """Harness inventory for WORKSTATE-REF-34 trigger choice:

| Harness | PreCompact | Stop / turn-end |
| --- | --- | --- |
| Claude Code | Available per task plan | Available via existing Stop hook schema |
| Codex | Not evidenced in-repo | Supported path implied by existing hook-stop contract |
| Cursor | Not evidenced in-repo | Supported path implied by existing hook-stop contract |

Choose Option B (`Stop` / turn-end hook). The task plan already documents Option A as Claude-only today, while Option B is the cross-harness path and can reuse the existing `hook-stop.json` schema instead of introducing a Claude-specific event. implementation note does not touch hooks, but this kickoff decision closes the pre-work trigger choice so implementation note can wire the protocol against the shared Stop surface.
"""


_SAMPLE_SLICE1_COMMIT_PROSE = """## Changes
- Committed the compaction remediation branch work on `feature/WORKSTATE-34` as `96202e97228e55dc6a62fab0f8ce90c661e0cccc`.
- Closed the six open branch-review findings against that verifying feature-branch commit.
- Preserved the extractor, schema, and persistence changes already validated in the slice.

## Verification
- `cd packages/mcp-workstate-handoff && uv run --extra test pytest tests/test_compaction.py tests/test_schema_migrations.py -q`
- Result: `23 passed in 4.08s`

## Schema / Contract Changes
- No new schema drift beyond the already committed compaction contract and `session_compactions` schema v8 work in this slice.

## Open Threads
- The repo root still lacks the documented branch lifecycle `make context` / `make handoff-close-check` entrypoints, so close gating continues through MCP.
- Final task close still requires explicit `done` status and a clean close-check context before merge/teardown.
"""


_SAMPLE_SLICE1_IMPL_PROSE = """## Changes
- Added the compaction contract/schema artifacts plus handoff persistence for `session_compactions`.
- Implemented extractor-backed `compact_session` behavior with harness validation, turn-range derivation, bounded transcript reads, residual truncation, and structured field extraction.
- Added focused compaction regression coverage for branch-review findings, including uniqueness, overflow, extraction, residual preservation, and schema roundtrip cases.

## Verification
- `cd packages/mcp-workstate-handoff && uv run --extra test pytest tests/test_compaction.py tests/test_schema_migrations.py -q`
- Result: `23 passed in 4.19s`

## Schema / Contract Changes
- Added `DecisionRef`, `TurnRange`, and `StructuredSummary` to `workstate-protocol` and generated `compaction-summary.json`.
- Bumped handoff DB schema to v8 with a durable `session_compactions` table and index.

## Open Threads
- Branch review findings still need to be dispositioned against a truthful verifying commit on `feature/WORKSTATE-34`.
- The repo root lacks the documented `make context` / `make handoff-close-check` entrypoints, so branch lifecycle closure continues through the MCP fallback path for this task.
"""


_SAMPLE_SLICE1_COHORT_PROSE = (
    _SAMPLE_SLICE1_SCOPE_INTAKE_PROSE + "\n\n" + _SAMPLE_SLICE1_IMPL_PROSE + "\n\n" + _SAMPLE_SLICE1_COMMIT_PROSE
)


# Each sample mirrors what a structured cold-start summary would
# distill from the prose: the decision id that fired, the findings the
# slice closed, the verification commands, and the touched files. The
# turn_range is illustrative; the cold-start renderer prints it
# verbatim and the count is dominated by the id strings, so the exact
# range does not move the ratio.
_SAMPLES = [
    {
        "label": "slice2_trigger_wiring",
        "compaction_id": "C-WORKSTATE-REF-34-S2",
        "session_id": "WORKSTATE-34-slice-2-trigger-wiring-20260501",
        "prose": _SAMPLE_SLICE2_PROSE,
        "summary": StructuredSummary(
            compaction_id="C-WORKSTATE-REF-34-S2",
            session_id="WORKSTATE-34-slice-2-trigger-wiring-20260501",
            harness="codex",
            task_ref="WORKSTATE-REF-34",
            turn_range=TurnRange(start_turn=1, end_turn=42),
            decisions=[
                {
                    "decision_id": "claude_slice_complete_WORKSTATE-34_slice2_trigger_wiring",
                    "slug": "slice2-trigger-wiring",
                },
                {
                    "decision_id": "scope_intake_WORKSTATE-34_trigger_choice",
                    "slug": "trigger-choice",
                },
            ],
            findings_fixed=[],
            findings_opened=[],
            tests_verified=[
                "pytest scripts/hooks/ -q",
                "pytest tests/test_compaction.py tests/test_schema_migrations.py -q",
            ],
            files_touched=[
                "packages/workstate-system/scripts/hooks/compact-session.py",
                "packages/workstate-system/scripts/hooks/test_compact_session_hook.py",
                "packages/workstate-system/scripts/hooks/test_protocol_validation_wiring.py",
                "packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-34-cross-harness-session-compaction-task-plan.md",
            ],
            prose_residual="",
            created_at="2026-05-01T22:51:34Z",
        ),
    },
    {
        "label": "slice3_cold_start_renderer",
        "compaction_id": "C-WORKSTATE-REF-34-S3",
        "session_id": "WORKSTATE-34-slice3-cold-start",
        "prose": _SAMPLE_SLICE3_PROSE,
        "summary": StructuredSummary(
            compaction_id="C-WORKSTATE-REF-34-S3",
            session_id="WORKSTATE-34-slice3-cold-start",
            harness="claude-code",
            task_ref="WORKSTATE-REF-34",
            turn_range=TurnRange(start_turn=1, end_turn=28),
            decisions=[
                {
                    "decision_id": "claude_slice_complete_WORKSTATE-34_slice3_cold_start_renderer",
                    "slug": "slice3-cold-start-renderer",
                },
            ],
            findings_fixed=[],
            findings_opened=[],
            tests_verified=[
                "pytest packages/mcp-workstate-handoff/tests/test_compaction.py",
            ],
            files_touched=[
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/compaction.py",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/core.py",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/api.py",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/__init__.py",
                "packages/mcp-workstate-handoff/tests/test_compaction.py",
                "packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-34-cross-harness-session-compaction-task-plan.md",
            ],
            prose_residual="",
            created_at="2026-05-01T23:03:53Z",
        ),
    },
    {
        "label": "slice1_compaction_cohort",
        "compaction_id": "C-WORKSTATE-REF-34-S1",
        "session_id": "WORKSTATE-34-findings-fix-20260501",
        "prose": _SAMPLE_SLICE1_COHORT_PROSE,
        "summary": StructuredSummary(
            compaction_id="C-WORKSTATE-REF-34-S1",
            session_id="WORKSTATE-34-findings-fix-20260501",
            harness="codex",
            task_ref="WORKSTATE-REF-34",
            turn_range=TurnRange(start_turn=1, end_turn=15),
            decisions=[
                {
                    "decision_id": "codex_slice_complete_WORKSTATE-34_compaction_finding_remediation",
                    "slug": "compaction-finding-remediation",
                },
            ],
            findings_fixed=["F-1", "F-2", "F-3", "F-4", "F-5", "F-6"],
            findings_opened=[],
            tests_verified=[
                "pytest tests/test_compaction.py tests/test_schema_migrations.py -q",
            ],
            files_touched=[
                "packages/workstate-protocol/scripts/generate_schemas.py",
                "packages/workstate-protocol/src/workstate_protocol/__init__.py",
                "packages/workstate-protocol/src/workstate_protocol/compaction.py",
                "packages/workstate-protocol/schemas/compaction-summary.json",
                "packages/workstate-protocol/tests/test_handoff_schema.py",
                "packages/mcp-workstate-handoff/pyproject.toml",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/__init__.py",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/api.py",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/core.py",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/shared_schema.py",
                "packages/mcp-workstate-handoff/src/workstate_handoff_mcp/compaction.py",
                "packages/mcp-workstate-handoff/tests/test_compaction.py",
                "packages/mcp-workstate-handoff/tests/test_schema_migrations.py",
            ],
            prose_residual="",
            created_at="2026-05-01T19:45:43Z",
        ),
    },
]


def _persist(summary: StructuredSummary, created_at_sql: str) -> None:
    with _get_db_connection() as conn:
        conn.execute("DELETE FROM session_compactions WHERE task_ref = ?", (summary.task_ref,))
        conn.execute(
            """
            INSERT INTO session_compactions (
                compaction_id, session_id, harness, task_ref, turn_range,
                structured_summary_json, prose_residual, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                summary.compaction_id,
                summary.session_id,
                summary.harness,
                summary.task_ref,
                summary.turn_range.model_dump_json(),
                summary.model_dump_json(),
                summary.prose_residual,
                created_at_sql,
            ),
        )
        conn.commit()


def _count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


def test_cold_start_block_compresses_prose_handoffs_by_at_least_50_percent(
    isolated_runtime: RuntimeConfig,
) -> None:
    """For each of three real in-repo slice rationales, the structured
    cold-start block must be ≥50% smaller (cl100k_base tokens) than the
    prose archive. Per-sample numbers are emitted on stdout for
    inclusion in the slice rationale + plan Verification section.
    """
    rows = []
    for sample in _SAMPLES:
        _persist(sample["summary"], created_at_sql="2026-05-01 00:00:00")

        rendered = mcp_server.render_cold_start_compaction(task_ref="WORKSTATE-REF-34")
        assert rendered is not None, f"render returned None for {sample['label']}"

        prose_tokens = _count_tokens(sample["prose"])
        cold_tokens = _count_tokens(rendered)
        reduction = 1.0 - (cold_tokens / prose_tokens)

        rows.append(
            {
                "label": sample["label"],
                "prose_tokens": prose_tokens,
                "cold_tokens": cold_tokens,
                "reduction": reduction,
            }
        )

    # Print in a table-friendly format so the slice rationale can quote
    # exact per-handoff numbers without re-running the test.
    print("\nWORKSTATE-34 compression-ratio benchmark (cl100k_base)")
    print(f"{'sample':<35}{'prose':>8}{'cold':>8}{'reduction':>12}")
    for row in rows:
        print(f"{row['label']:<35}{row['prose_tokens']:>8}{row['cold_tokens']:>8}{row['reduction'] * 100:>11.1f}%")
    mean = sum(r["reduction"] for r in rows) / len(rows)
    print(f"{'mean':<35}{'':>16}{mean * 100:>11.1f}%")

    for row in rows:
        assert row["reduction"] >= 0.50, (
            f"{row['label']}: prose={row['prose_tokens']} cold={row['cold_tokens']} "
            f"reduction={row['reduction']:.3f} < 0.50 floor"
        )
    assert mean >= 0.50, f"mean reduction {mean:.3f} < 0.50 floor"
