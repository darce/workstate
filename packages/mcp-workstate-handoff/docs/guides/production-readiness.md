# Production Readiness: Steady-State Controls

This guide tells maintainers where the steady-state levers live so the package remains safe under sustained workloads. It covers response-size controls, task archive and GC behavior, artifact purge, subprocess timeouts, and failure modes for ambiguous workspace and task context.

**Related:** `docs/guides/token-efficient-usage.md` covers the caller-side read patterns in more detail.

---

## 1. Response-size controls

### `get_handoff_state` read shaping

Every call to `get_handoff_state` (and `load_session`, which wraps it) is bounded by `HandoffReadLimits`. Defaults are applied from `DEFAULT_HANDOFF_LIMITS` in `shared_primitives.py`:

| Section | Default limit |
|---|---|
| blockers | 5 |
| actions | 5 |
| decisions | 3 |
| slices | 20 |
| tests | 3 |
| findings | 10 |

Override any of these per-call with the `top_n_*` parameters:

```python
get_handoff_state(
    task_ref="TASK-1",
    top_n_decisions=1,
    top_n_findings=3,
    detail="summary",
)
```

`detail="summary"` truncates long rationale and verification evidence fields. It is opt-in; the default is `"full"`.

`sections="identity"` returns only the active-task identity and limits block — no decision/finding/blocker data. Use it for health checks and task-context polls.

### Read profiles and response budgets (WORKSTATE-REF-71)

Routine paths should prefer `read_profile=` instead of hand-rolling `sections=`/`top_n_*`. The profile is the stable name for the bounded shape and shows up in `data.read_shape.applied_profile` on the response.

| Profile         | Use when…                                                                | Notes                                                                                            |
| --------------- | ------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------ |
| `identity`      | Polling / hook context loads / "which task is active?"                   | Equivalent to `sections="identity"`.                                                              |
| `hot_summary`   | Fresh session orientation, loop iterations.                              | Summary detail, `top_n_*` bounded to 3/5/5/3/3.                                                  |
| `review_packet` | Planning / branch-review triage.                                         | Summary detail, `top_n_*` 20/20/20/5/5, add-ons 20.                                              |
| `open_items`    | Close gates and readiness checks.                                        | Full detail; open sections are required and never omitted by `auto_summary`.                     |
| `full_debug`    | Broad diagnostic reads.                                                  | Legacy default; the only profile the budget planner may fully reshape.                           |

For production retry loops set both: `read_profile="hot_summary"` plus `response_budget_bytes=N`. The Layer-2 planner reduces section limits and detail level *before* heavy rows materialise, so a budgeted call typically returns within a single round trip. The active policy follows: `warn` (effective default when no budget), `auto_summary` (effective default with a budget), or explicit `fail` (server returns `ok=false` with `data.read_budget.retry_with` rather than materialising an over-budget payload).

The response carries `data.read_budget = {requested_bytes, policy, estimated_initial_bytes, estimated_after_bytes, applied_reductions[], omitted_sections[], over_budget_after, retry_with?}` whenever a budget was supplied or the planner applied reductions.

### Envelope oversize warning

When the serialized response exceeds `RESPONSE_OVERSIZE_WARN_BYTES` (8,000 bytes, roughly 5,000 tokens), the envelope appends a warning naming the bounded-read levers (now including `read_profile` and `response_budget_bytes`). This is a soft nudge, not a hard cap — the response is still returned. If the warning fires routinely on profile/budget paths, the profile is too broad or the budget too generous; tighten one of them.

### Text field hard limits (writes)

| Field | Limit |
|---|---|
| Rationale (decisions) | 1,500 chars soft / 3,000 chars hard |
| Rationale (slice-complete decisions) | 1,500 chars soft / 4,000 chars hard |
| Resolution notes | 500 chars |
| Reopen reason | 500 chars |
| Verification evidence | 2,000 chars |
| Verified-test result summary | 280 chars |

Writes that exceed a hard limit are rejected before they reach the server.

### Artifact index threshold

`artifacts(operation='record', ...)` only indexes a file into the full-text index when the file exceeds the `artifact_index_min_bytes` (default 4,096) and `artifact_index_min_lines` (default 80) thresholds from `RuntimeConfig`. Smaller files are stored but not FTS-indexed. Adjust both thresholds via `RuntimeConfig.for_workspace(...)` if a workload produces many small artifacts that need search.

### touched_files and search_handoff limits

- `touched_files(operation='list', ...)`: bounded by the `limit` parameter (default 20, pass `limit=200` for a broader view).
- `load_session(top_n_touched_files=...)`: default 20, max 200.
- `search_handoff(...)`: bounded by `limit` (default 10).
- `artifacts(operation='search', ...)`: bounded by `limit` (default 10).

---

## 2. Archive and GC behavior

### Archiving a completed task

When a task finishes, call:

```python
archive(payload={"operation": "archive", "task_ref": "TASK-1"})
```

This snapshots the task's live state into `task_archives` and removes the row from the active `handoff_state` table. The dashboard renders archived tasks from the snapshot, not from live rows.

`archive(operation='archive', prune_working_rows=True, allow_destructive_clear=True)` also deletes live decision/finding/blocker rows for the task after archiving. Use this only when the working data is no longer needed.

### GC (bulk archive)

`archive(operation='gc', apply=False)` is a dry-run that lists status=done tasks eligible for bulk archiving. `apply=True` performs the bulk archive. GC targets `WORKSTATE-REF-PLANNING-REVIEW-*` task refs whose linked parent task is already archived.

**GC does not touch tasks with status other than `done`.** Set task status to `done` explicitly before GC can pick it up:

```python
set_handoff_state(task_ref="TASK-1", status="done", status_only=True)
archive(payload={"operation": "gc", "apply": True})
```

### Active-task eligibility

Only tasks with `status IN ('in_progress', 'review', 'blocked')` are `LIVE_ACTIVE_STATUSES`. Tasks with `status='done'` are archive-eligible and do not appear in active-task resolution. `list_handoff_rows` excludes archived tasks; use `get_archived_task(task_ref=...)` to read a snapshot.

---

## 3. Artifact purge behavior

Artifacts are stored in a local SQLite FTS index under the workspace state directory. There is no automatic expiry. Explicit purge:

```python
artifacts(artifact={"operation": "purge", "task_ref": "TASK-1", ...})
```

`purge_artifacts` in `core.py` is the underlying function. The purge is destructive and irreversible — there is no soft-delete. Run `artifacts(operation='search', ...)` first to confirm scope before purging.

---

## 4. Subprocess timeouts

All git subprocess calls within the package use `SUBPROCESS_TIMEOUT = 10` seconds (defined in `shared_primitives.py`). Calls that time out are silently skipped rather than erroring — the write still proceeds with whatever context could be resolved.

Doctor check subprocess timeouts:

- `_check_cli_startup`: catches `CalledProcessError` and `OSError`; reports a remediation string on failure.
- `_check_stdio_startup`: async; wrapped in `try/except (Exception, OSError)` in `run_doctor()`. A failed stdio probe sets `stdio_probe_error` but does not abort the doctor run unless `strict_mode=True`.

If `run_doctor()` is called from an environment without a reachable git repo or without the package binary on `PATH`, both CLI and stdio probes will fail gracefully. Pass `strict_mode=True` only when the deployment is expected to be fully operational.

---

## 5. Failure modes for ambiguous workspace and task context

### task_ref resolution

When `task_ref=None`, tools resolve the active task from the current workspace path. If exactly one `in_progress/review/blocked` task matches the workspace, it is used. If zero or more than one match, the tool raises a `ValueError` with an ambiguity message. Pass `task_ref` explicitly to bypass resolution.

### Global finding lookups

Operations like `repair_review_finding_provenance` with `task_ref=None` do a global `finding_id` lookup. If the same `finding_id` appears across multiple `task_ref` values, the call returns `ok=False` with an ambiguity error naming the candidate `task_ref` values. Pass `task_ref` explicitly to disambiguate.

### Branch mismatch enforcement

When the `AGENT_HANDOFF_BRANCH_MISMATCH_ENFORCEMENT` env var is truthy, writes that arrive from a branch other than the task's `target_branch` raise `BranchMismatchError`. In permissive mode (the default), the mismatch is recorded as a context warning in the envelope but the write still proceeds.

### Commit SHA validation

`_validate_and_expand_commit_sha` validates commit SHAs against the git repo at `workspace_root`. When git is unavailable or no repo is reachable (common in `tmp_path` test fixtures), validation is silently skipped and the input is returned unchanged. Set `AGENT_HANDOFF_SKIP_SHA_VALIDATION=1` to bypass validation in CI and non-git environments.

---

## See also

- `docs/guides/token-efficient-usage.md` — caller-side read patterns
- `docs/contracts/write-actor-attribution.md` — write provenance contract
- Assessment: `docs/assessments/mcp-workstate-handoff-refactoring-release-it-pass-2026-05-11.md` in the parent monorepo
