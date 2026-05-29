# Token-Efficient Usage

`workstate-handoff-mcp` now emits compact v2 envelopes by default, but callers still control a large share of total token cost. Use the smallest read surface that answers the current question.

## Principles

- Treat `data` as the canonical payload block. Do not depend on mirrored top-level fields.
- **Prefer named `read_profile=` over hand-rolled `sections=`/`top_n_*` knobs** — profiles are the WORKSTATE-REF-71 Layer-1 control and stay stable when the server adds new section keys.
- **Set `response_budget_bytes=` for production retry loops** — the Layer-2 budget planner reduces the response before heavy rows materialise, so a single call lands within budget instead of a re-read.
- Use full-detail reads for session-start hot state and review work.
- Use scoped reads for polling, health checks, and targeted lookups.
- Prefer additive shaping parameters over post-processing large payloads client-side.

## Read Profiles (WORKSTATE-REF-71 Layer 1)

Profiles bundle a documented `sections=`/`detail=`/`top_n_*` shape under a stable name. When a profile is supplied the server returns `data.read_shape` describing the applied expansion.

| Profile         | Use when…                                                                | Notes                                                                                            |
| --------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `identity`      | Routine "which task is active?" / polling / hook context loads.          | Returns active-task identity + limits; omits decisions, findings, blockers, tests.               |
| `hot_summary`   | Fresh session orientation; loop iterations between fix attempts.         | Summary detail with bounded `top_n_*` (3/5/5/3/3).                                               |
| `review_packet` | Planning or branch-review triage.                                        | Summary detail with wider `top_n_*` (20/20/20/5/5) and add-on findings + touched files at 20.    |
| `open_items`    | Close gates and readiness checks that need every open blocker/action.    | Full detail on `blockers_open` / `actions_pending` / `findings_open`; required sections protected against `auto_summary` omission. |
| `full_debug`    | Explicit broad diagnostic read.                                          | Equivalent to the legacy default; the only profile the budget planner may fully reshape.        |

Explicitly supplied low-level args (`sections=`, `detail=`, `top_n_*`) override the profile defaults.

## Response Budget Planner (WORKSTATE-REF-71 Layer 2)

Pair `response_budget_bytes=` with one of three policies:

| Policy         | Behavior                                                                                              | Effective default                      |
| -------------- | ----------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `warn`         | Pass the requested shape through; `_envelope()` still appends an oversize advisory if the response overruns. | When `response_budget_bytes` is absent. |
| `auto_summary` | Force `detail="summary"`, halve row limits in priority order, omit optional sections.                 | When `response_budget_bytes` is set without an explicit policy. |
| `fail`         | If the requested shape cannot fit, return `ok=false` with `data.read_budget.retry_with` and do not materialise the broad payload. | Opt-in.                                |

The response carries `data.read_budget` whenever a budget was supplied or the planner applied reductions. Hook consumers (`scripts/hooks/slim-handoff-response.py`) read this block and emit a structured `handoffSuggestion = {suggested_profile, suggested_budget_bytes, rationale}` so transport-side consumers can drive automatic retries.

## Recommended Read Patterns

### Routine task checks

When the caller only needs to know which task is active, use the identity profile:

```python
get_handoff_state(read_profile="identity")
```

This keeps the response to the active-task identity plus limits, instead of fetching decisions, findings, blockers, and tests. Equivalent to the legacy `sections="identity"`; the profile form is preferred so the call name documents intent.

### Session-start hot state

For a fresh task load, the WORKSTATE-REF-71 default-bounded profile is `hot_summary`:

```python
load_session(task_ref="WORKSTATE-REF-7", read_profile="hot_summary", response_budget_bytes=8000)
```

The pairing keeps routine session-orientation reads under ~2K tokens. If the caller needs the full hot state (review/triage), use `read_profile="review_packet"`; for explicit broad diagnostic reads, use `read_profile="full_debug"`.

### Focused handoff reads

For one-off shapes that do not map to a profile, the legacy low-level knobs still work and combine freely with `response_budget_bytes=` / `budget_policy=`:

```python
get_handoff_state(
    task_ref="WORKSTATE-REF-7",
    sections="tests_recent,decisions_recent",
    detail="summary",
    top_n_tests=3,
    top_n_decisions=2,
    response_budget_bytes=4000,
)
```

Useful section patterns:

- `identity`
- `tests_recent`
- `decisions_recent`
- `findings_open`
- `blockers_open`
- `actions_pending`

### Findings and search queries

Use `detail="summary"` and `limit=` for inspection flows, and project only the fields you need where supported:

```python
review_findings(
    review={
        "operation": "list",
        "task_ref": "WORKSTATE-REF-7",
        "status": "open",
        "detail": "summary",
        "limit": 20,
    }
)

search_handoff(
    queries="projection",
    record_types="decision,finding",
    detail="summary",
    limit=10,
    fields="record_type,task_ref,title,snippet",
)
```

### Coordinator-centric finding merges

When a coordinator task consolidates review findings from several source task_refs (the WORKSTATE-REF-17-9 parallel-review flow, or a release-audit roll-up), use `review_findings(operation="merge", ...)` instead of re-recording findings by hand. The call reuses the atomic batch-record upsert path, copies source rows under the `target_task_ref`, and stamps each merged row with a `merged_from` pointer naming the source `(task_ref, session, finding_id)` triple. Source rows remain intact; the merge is additive, so re-running the same merge is an idempotent upsert rather than a duplication. Omit `session` to auto-generate `merge-<target_task_ref>-<utc-ts>` so merged rows stay attributable:

```python
review_findings(
    review={
        "operation": "merge",
        "source_task_refs": ["SRC-A", "SRC-B"],
        "target_task_ref": "COORD",
    }
)
```

### Artifacts

When browsing indexed artifacts, request summary mode or a field projection instead of full chunk content:

```python
artifacts(
    artifact={
        "operation": "search",
        "task_ref": "WORKSTATE-REF-7",
        "detail": "summary",
        "fields": "source_id,source_label,title,snippet",
    }
)
```

## Anti-Patterns

- Calling `get_handoff_state()` with no shaping parameters in a tight polling loop.
- Using `load_session(detail="full")` for a single-field liveness check.
- Reading top-level mirrored fields instead of `data`, which prevents future envelope simplification.
- Fetching hundreds of findings/actions/decisions and trimming them client-side when `top_n_*`, `limit`, `sections`, `detail`, or `fields` could do it server-side.
- Hand-rolling `sections=`/`top_n_*` kwargs in routine paths instead of naming a `read_profile=`. The orchestrator and `workstate-system` skills enforce this via the lint at `packages/workstate-system/tests/test_profile_reads_required.py`.

## Caller Checklist

- Use `read_profile="identity"` for routine state checks (or `sections="identity"` in legacy paths).
- Pair `read_profile=` with `response_budget_bytes=` in production loops so the planner trims before heavy fetches.
- Use `detail="summary"` when truncation is acceptable.
- Cap `top_n_*` and `limit=` aggressively for UI/polling paths.
- Use `fields=` for `search_handoff`, `review_findings`, and artifact reads when only a subset is needed.
- Reserve `read_profile="full_debug"` (or unprofiled full reads) for human review, task start, and close-check workflows.

## When to Use Full Debug

`read_profile="full_debug"` (or an unprofiled call) returns the broadest legacy shape. Reach for it when:

- Investigating a specific incident and the missing context is unknown ahead of time.
- Generating audit / archival snapshots that must record the full payload.
- A budgeted profile call returned `data.read_budget.over_budget_after = true` and you need to know what was elided.

For everything else (loops, polling, hooks, dashboards) prefer a named profile — it documents intent and stays compatible when new section keys are added.
