# Changelog

All notable changes to `mcp-workstate-handoff` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This file is the canonical migration notice for in-monorepo agents and
external consumers. When you see a new entry, read the **Migration** block
in that entry before relying on previously cached field shapes.

## Unreleased

### Added

- **Compaction receipt fields on operator-visible outputs (WORKSTATE-REF-76).**
  Successful `compact-session` Stop-hook runs and `make compact-now` /
  `workstate_handoff_mcp.compaction_cli` output now preserve
  `compaction_id=<id>` as the stable first line, followed by
  `tokens_saved_estimate=<n>`, `input_chars=<n>`, `summary_chars=<n>`, and
  `prose_residual_chars=<n>` on separate lines. The MCP
  `compaction(operation="record", ...)` response already exposed these
  fields through `CompactionRecordReceipt`; this change makes the same
  values visible in the operator surfaces without changing persistence.
  The docs now clarify that the receipt describes the durable WORKSTATE-REF
  artifact and does not claim host-harness context-window savings.

## [0.11.5] — 2026-05-20

### Fixed

- **ACTIVE TASK PLANS rows now keep `task_plan_path` repo-relative.**
  `DASHBOARD.txt` once again renders the declared `task_plan_path`
  verbatim under `plan:` and moves the branch/readability cue into the
  read hint, so root-workspace task-plan discovery matches the published
  contract and the consumer acceptance test.

### Added

- **Adaptive read profiles + response budgeting (WORKSTATE-REF-71).**
  `get_handoff_state` and `load_session` now accept three layered read
  controls:
  - **Layer 1 — `read_profile`** selects a stable bundled shape under one
    of `identity` (active task + limits only), `hot_summary` (summary
    detail, `top_n_*=3/5/5/3/3`), `review_packet` (summary detail,
    `top_n_*=20/20/20/5/5`, additive add-ons capped at 20), `open_items`
    (full detail with `blockers_open`/`actions_pending`/`findings_open`
    required and never omitted by `auto_summary`), or `full_debug` (the
    legacy full shape; the only profile the budget planner may fully
    reshape). Explicit `sections`/`detail`/`top_n_*` parameters still
    override profile defaults. Responses surface
    `data.read_shape = {applied_profile, sections, detail, top_n_*}`.
  - **Layer 2 — `response_budget_bytes` / `budget_policy`** drives the
    server-side budget planner. The effective default policy is `warn`
    without a budget and `auto_summary` with one; `fail` returns
    `ok=false` with `data.read_budget.retry_with` rather than
    materializing an over-budget payload. The planner reduces detail,
    halves `top_n_*`, and drops optional sections *before* heavy rows
    materialize, so a budgeted call typically lands in one round trip.
    Responses carry `data.read_budget = {requested_bytes, policy,
    estimated_initial_bytes, estimated_after_bytes, applied_reductions[],
    omitted_sections[], over_budget_after, retry_with?}` whenever a budget
    was supplied or the planner applied reductions.
  - The compaction-style envelope advisory now names `read_profile=` and
    `response_budget_bytes=` as the bounded-read levers. The
    `scripts/hooks/slim-handoff-response.py` PostToolUse hook emits a
    structured `hookSpecificOutput.handoffSuggestion =
    {suggested_profile, suggested_budget_bytes, rationale}` so transport
    consumers can drive automatic retries. Default callers (no
    `read_profile`, no `response_budget_bytes`) receive the legacy
    unbounded response — this change is **additive and
    backward-compatible**.
  - **Migration:** routine consumers (orchestrator helpers, skills, hooks)
    should prefer `read_profile=` over hand-rolled `sections=`/`top_n_*`.
    A repo-level lint at
    `packages/workstate-system/tests/test_profile_reads_required.py`
    enforces this on `skills/**/body.md` and `scripts/hooks/**/*.py`.
    Production retry loops should pair `read_profile="hot_summary"` with
    an explicit `response_budget_bytes` (8000 is a reasonable default).

- **Compaction-vs-host explainer + cold-start hook test alignment
  (WORKSTATE-REF-67 implementation note).** New
  `docs/explainers/compaction-vs-default-harness-compaction.md` covers
  where each mechanism runs (in-conversation vs. cross-session), what each
  consumes and produces, when each fires, and — critically — what the
  `tests/test_compression_ratio.py` benchmark actually measures
  (cold-start retained-context ratio, **not** in-conversation token cost).
  Cross-linked from `compaction.py` module docstring and the README's
  *Disabling WORKSTATE-REF compaction* subsection. The Stop-hook round-trip test
  (`test_compact_session_hook_round_trips_into_cold_start_render`) now
  asserts against `render_cold_start_compaction(task_ref=...)` instead of
  a top-level `cold_start_compaction` key on `CURRENT_TASK.json` — the v2
  slim projection intentionally omits that key; the block is served on
  demand by the renderer.

- **Typed compaction record receipt (WORKSTATE-REF-67 implementation note).** The
  `compact_session` implementation (now exposed only as the typed callable
  `workstate_handoff_mcp.compaction.compact_session`; the `api.py` bare-string
  wrapper was deleted after the WORKSTATE-REF-005 caller audit confirmed no external
  caller depended on it) returns a Pydantic `CompactionRecordReceipt` with
  `{compaction_id, summary: StructuredSummary, input_chars, summary_chars,
  prose_residual_chars, tokens_saved_estimate, db_row_id}`. The inlined
  `summary` carries the same `StructuredSummary` that `get_compaction`
  returns — no parallel `*_count` fields are emitted (WORKSTATE-REF-004 fix). The
  `tokens_saved_estimate` uses the contract-aligned `chars / 4` lineage:
  `max(0, (input_chars - summary_chars - prose_residual_chars) // 4)`. The
  MCP op `compaction(operation="record", ...)` now returns
  `receipt.model_dump(mode="json")` (dict) rather than the bare
  `compaction_id` string; callers reach the id at `result["compaction_id"]`.

  **Migration**: internal callers (`api.compact_session`,
  `compaction_cli.py`, the `compact-session` Stop hook,
  `test_compaction.py`) now consume `receipt.compaction_id`. Any external
  consumer that imported `workstate_handoff_mcp.api.compact_session` for the
  bare-string return must update to either (a) the typed implementation
  at `workstate_handoff_mcp.compaction.compact_session` and read
  `receipt.compaction_id`, or (b) the MCP `compaction(operation="record")`
  dict response and read `result["compaction_id"]`.

- **Unified compaction runtime-disable surface (WORKSTATE-REF-67).** A single
  resolver, `resolve_compaction_disabled(env, conn, task_ref)`, now silences
  both WORKSTATE-REF compaction surfaces with the same precedence chain:
  `AGENT_HANDOFF_COMPACTION_DISABLED` env (legacy
  `WORKSTATE_COMPACTION_DISABLED` alias) → task-scoped row in
  `compaction_settings` → workspace-default row → enabled. When disabled,
  `compute_compaction_advisory` short-circuits with
  `disabled=true, disabled_source=<env|db>` (threshold/floor logic
  skipped), the `compact-session` Stop hook logs
  `compaction skipped: disabled (source=<env|db>)`, and the dashboard's
  Needs Attention rail prints `compaction: disabled via <source>`. DB
  rows are writable per-task or workspace-default via the new
  `compaction(operation="disable"|"enable"|"status")` MCP op,
  `mcp-workstate-handoff compaction --operation <op> [--task-ref <ref>]`
  CLI, and `make compaction-{disable,enable,status} [TASK=<ref>]`
  operator wrappers. Each returns a `CompactionStatusReceipt`
  (`{disabled, source, env_override, db_row}`). Schema bump:
  `HANDOFF_SCHEMA_VERSION` 9 → 10 adds the `compaction_settings` table
  with `UNIQUE(scope_kind, COALESCE(task_ref, ''))` enforcing the
  workspace-default singleton. **Host-harness compaction (Claude Code's
  own `/compact`, Codex's internal summarization, etc.) is unaffected.**

### Changed

- **Compaction advisory default thresholds lowered to 70,000 tokens /
  280,000 chars** in
  `packages/workstate-system/docs/agentic/contracts/harness-protocol.yaml`
  (was 120,000 / 500,000). The pairing comes from
  `docs/assessments/handoff-id-auto-compaction-threshold-scope-2026-04-30.md`,
  which fixes the canonical starting threshold at 70k tokens and documents
  the `chars / 4` fallback estimator (70_000 * 4 = 280_000). The
  `harness-protocol.yaml` `compaction:` block is the single source of
  truth for these defaults; the README no longer duplicates the literals.
  Deployments that need the previous values can pin them via the WORKSTATE-REF-63
  override surfaces (env vars or `.agentic-overlay.json`).

### Added

- **Per-deployment compaction threshold overrides (WORKSTATE-REF-63).** Operators can
  now tune the compaction advisory's token and char gates without editing
  `harness-protocol.yaml`. Precedence per knob: env > overlay > contract.
  - Env vars: `AGENT_HANDOFF_COMPACTION_THRESHOLD_TOKENS`,
    `AGENT_HANDOFF_COMPACTION_THRESHOLD_CHARS`.
  - Overlay JSON: `.agentic-overlay.json -> compaction.thresholds.{tokens,chars}`
    (sibling of the existing `surfaces` key).
  - Advisory envelope gains additive `thresholds_source: {tokens, chars}`
    field reporting which layer (`"env"` / `"overlay"` / `"contract"`)
    supplied each effective threshold. Existing `thresholds` shape unchanged.
  - Invalid overrides (non-int, negative) append a
    `compaction_threshold_override_invalid: <source>=<key>=<value>` warning
    to the advisory `warnings` list and fall through to the next layer; no
    exception. Resolution is per-call — no module-level cache, no restart
    required to change a knob.

- **Canonical compaction advisory at `get_handoff_state(sections="identity")`
  (WORKSTATE-REF-61 / WORKSTATE-REF-1).** A new `compute_compaction_advisory()` evaluator
  reads `docs/agentic/contracts/harness-protocol.yaml`, the configured
  transcript path (env var first, fallback glob second), and the running
  `turn_metrics` token total, and returns a stable 7-key envelope:
  `{recommended, thresholds, observed, harness, transcript,
  latest_compaction_id, warnings}`. `get_handoff_state` and `load_session`
  publish the envelope at `data.compaction_advisory` and mirror a
  boolean at `data.compaction_recommended` (field name controlled by
  `compaction.advisory_field` in the contract). The workspace-summary
  `CURRENT_TASK.json` exposes the same advisory at
  `active.compaction_advisory` for cold-start consumers (no `data`
  wrapper — the projection file is not an MCP tool envelope). The
  cross-task `DASHBOARD.txt` NEEDS ATTENTION block surfaces a
  `compaction` line for any live task whose advisory recommends
  compaction. Missing-contract and unknown-harness inputs fall through
  to the documented warn-and-skip envelope (`recommended=False`,
  populated `warnings[]`) rather than raising.

  **Migration**: cold-start consumers MUST read advisory state from
  `get_handoff_state(sections="identity")` (or the mirrored
  `CURRENT_TASK.json` / `load_session` keys) instead of recomputing
  token or character totals locally. The advisory is additive — agents
  that did not previously read it require no change.

## [0.11.4] — 2026-05-13

### Changed

- **`handoff_close_check` materializes `CURRENT_TASK.json` on demand.**
  The close check is now the single materialization point for the
  workspace summary file: it calls
  `_write_workspace_summary_current_task_json(unconditional=True)`
  before reading the on-disk copy for its `current_task_sync`
  comparison. Routine MCP writes still respect
  `current_task_auto_regen` (default `False`) — derive-on-read for the
  hot write path is unchanged. The on-disk file is now guaranteed to
  reflect live state after any close-check, so callers (orchestrator
  `review_ready`, `make review-ready`, monorepo lifecycle scripts) no
  longer need a caller-side `render_handoff` pre-render. Resolves the
  blocker recorded against `HARNESS-BASH-STALL-20260512` where
  `make review-ready` reported `CURRENT_TASK.json is out of sync with
  handoff state` whenever the on-disk file was stale.

  **Migration**: any caller that asserted
  `current_task_sync.exists is False` after `handoff_close_check`
  should now expect `exists: True` and `is_in_sync: True`. The
  `is_violation: False` and `mode: "on_demand_export"` fields are
  unchanged.

## [0.11.3] — 2026-05-11

### Added

- **`docs/guides/production-readiness.md`**: new operator guide covering
  response-size controls (`HandoffReadLimits`, oversize warning),
  archive/GC behavior, artifact purge, subprocess timeouts, and failure
  modes for ambiguous workspace and task context.

### Changed

- **`HandoffReadLimits` read-limit policy**: `get_handoff_state` and
  `load_session` now route all `top_n_*` parameters through a typed
  `HandoffReadLimits` dataclass. Behavior is preserved; the
  `RESPONSE_OVERSIZE_WARN_BYTES` (8 000 byte) advisory warning and
  bounded-read levers (`top_n_*`, `detail`, `sections`) are unchanged.
- **Named migration helpers**: `_apply_handoff_migrations` now delegates
  to four explicitly named, idempotent functions
  (`_migrate_add_audit_tables`, `_migrate_add_column_extensions`,
  `_migrate_handoff_state_schema`, `_migrate_add_turn_metrics`). No
  schema changes; each helper is safe to call on an already-current DB.
- **Named doctor-check helpers**: `run_doctor` delegates to
  `_check_state_dir_writable`, `_check_fts_index_health`,
  `_check_fts5_available`, `_check_cli_startup`, and
  `_check_stdio_startup`. Each returns `{"ok": bool, "remediation": str}`
  on failure instead of raising. The `checks.state_dir_writable` field
  in the doctor result changes shape from `True` (bool) to a dict.
- **Typed request objects**: `_parse_import_snapshot` (returns
  `SnapshotImportData`) and `_parse_provenance_repair_request` (returns
  `ProvenanceRepairRequest`) extract validation from their callers.
  `_parse_import_snapshot` now validates that all child-array fields are
  lists *before* any DB writes begin — a malformed snapshot no longer
  silently deletes rows before raising.
- **Tool-registry group builders**: `_build_tool_registry` delegates to
  `_task_state_tool_entries`, `_review_tool_entries`,
  `_lifecycle_tool_entries`, and `_artifact_tool_entries`. Registry
  content is unchanged.

### Fixed

- **`write_contracts` grammar alignment**: `record_event.decision` regex
  relaxed from `^[A-Za-z][A-Za-z0-9_]*$` to `^[A-Za-z][A-Za-z0-9_-]*$`
  and `close_slice.work_ref` relaxed from `^[a-z0-9][a-z0-9_]*$` to
  `^[A-Za-z0-9][A-Za-z0-9_-]*$`. Previously the published canonical
  `valid_examples` (`copilot_slice_complete_WORKSTATE-DASHBOARD-AUTOREGEN_...`)
  could not be stored through either tool because hyphens and uppercase
  were rejected. No migration needed — existing rows are unaffected.

## [0.11.2] — 2026-05-11

### Fixed

- **File-descriptor leak in `_get_db_connection`**: the prior factory
  returned a bare `sqlite3.Connection` that callers wrapped with
  `with ... as conn:`. `sqlite3.Connection.__exit__` only commits or
  rolls back the transaction — it does **not** close the file handle.
  Each call leaked an fd, and a full test-suite run could cross the
  per-process open-file limit (observed as
  `OSError: [Errno 24] Too many open files` during temp-directory
  cleanup, and as `sqlite3.OperationalError: unable to open database
  file` at `PRAGMA journal_mode=WAL;` under heavier load). The factory
  is now a `@contextmanager` that closes on exit while preserving the
  prior auto-commit / auto-rollback semantics. A new
  `_open_db_connection()` helper is exposed for the small number of
  test helpers that explicitly own the connection lifecycle.

## [0.11.1] — 2026-05-10

### Fixed

- **WORKSTATE-REF-54 BR-01/02/03 fixes** on the multi-active CURRENT_TASK
  projection landed in 0.11.0:
  - **BR-01** — multi-active dashboard rendering no longer collapses
    distinct active tasks into one row when their `task_ref`s collide
    only by case.
  - **BR-02** — `import_handoff_state` now rejects malformed v2
    `single` and `workspace_ambiguous` payloads with a typed validation
    error instead of partially mutating the workspace.
  - **BR-03** — `import_handoff_state` preserves `target_branch`,
    `target_worktree_path`, and `plan_path` on imported rows rather
    than silently zeroing them when the import payload omits the keys.
- **WORKSTATE-REF-55 compaction env-var consolidation**: the
  `compact-session` hook now resolves every compaction-tuning value
  through `CompactionSettings.from_env()` so the canonical
  `AGENT_HANDOFF_COMPACTION_*` prefix and its deprecated `WORKSTATE_*`
  alias share one parse/validate/warn path. Setting both forms now
  emits exactly one deprecation warning per variable.

### Added

- **`doctor` reports package `version` (WORKSTATE-REF-47 implementation note)**: `run_doctor`
  now includes a `"version"` key at the **top level** of its returned
  dict, equal to `workstate_handoff_mcp.__version__`. The key sits alongside
  `ok`, `workspace_root`, `state_dir`, `db_path`, `artifact_db_path`,
  `current_task_path`, `exports_dir`, `checks`, and
  `portable_hook_semantics` — `run_doctor` remains a raw top-level CLI
  dict (no `_envelope()` wrapper, no `data` sub-dict). Consumer MCP
  clients can now confirm the running package version with
  `mcp-workstate-handoff --workspace-root <ws> doctor` without leaving the
  protocol surface.
- **Package `__version__` and CLI `--version` flag (WORKSTATE-REF-47 implementation note)**:
  `workstate_handoff_mcp.__version__` is now exposed at module load (sourced
  from `importlib.metadata.version("mcp-workstate-handoff")` with a
  source-checkout fallback that mirrors `pyproject.toml:[project].version`).
  The console script gains a top-level `--version` flag —
  `mcp-workstate-handoff --version` prints `mcp-workstate-handoff <semver>` and
  exits 0 before subcommand dispatch, giving consumer MCP clients a
  reproducibility/triage path that does not require the wire envelope.

## [0.11.0] — 2026-05-08

### Changed

- **Tool-surface flattening: 30 → 21 (WORKSTATE-REF-45)**: the workstate-handoff-mcp
  surface contracts via the discriminated-operation pattern established
  by ADR-005. Ten tools are removed in favor of consolidated entry-points.
  Python-level aliases for the legacy names are retained on
  `workstate_handoff_mcp.api` (and re-exported from `workstate_handoff_mcp`) so
  in-process callers (orchestrator, tests) keep working; the MCP wire
  surface (stdio/HTTP/CLI) only exposes the consolidated tools.

  | Removed (MCP tool name) | Replacement |
  | --- | --- |
  | `validate_decision_id` | `validate(payload={'kind': 'decision_id', 'decision': ...})` |
  | `validate_write` | `validate(payload={'kind': 'write', 'tool_name': ..., 'payload': ...})` |
  | `compact_session` | `compaction(payload={'operation': 'record', 'transcript_path': ..., 'task_ref': ..., 'harness': ..., 'session_id': ...})` |
  | `get_compaction` | `compaction(payload={'operation': 'get', 'compaction_id': ...})` |
  | `get_latest_compaction` | `compaction(payload={'operation': 'get_latest', 'task_ref': ...})` |
  | `record_file_touch` | `touched_files(payload={'operation': 'record', 'file_path': ..., 'change_kind': ...})` |
  | `get_touched_files` | `touched_files(payload={'operation': 'list', 'task_ref': ..., 'limit': ..., 'offset': ...})` |
  | `working_tree_integrity_check` | `integrity_check(payload={'kind': 'working_tree', 'workspace_root': ..., 'expected_dirty': ...})` |
  | `post_merge_integrity_check` | `integrity_check(payload={'kind': 'post_merge', 'merged_sha': ..., 'expected_changed_files': ...})` |
  | `handoff_close_check` | `integrity_check(payload={'kind': 'close', 'task_ref': ..., 'enforce': ..., 'require_fresh_tests': ..., 'current_commit_sha': ...})` |
  | `archive_task_state` | `archive(payload={'operation': 'archive', 'task_ref': ...})` |
  | `tasks_gc` | `archive(payload={'operation': 'gc', 'apply': ...})` |
  | `get_archived_task` | `archive(payload={'operation': 'get', 'task_ref': ..., 'include_snapshot': ...})` |
  | `update_task_status` | `set_handoff_state(task_ref=..., status=..., expected_revision=..., status_only=True)` |

  CLI subcommand renames track the same mapping:
  - `mcp-workstate-handoff validate-decision-id ...` /
    `mcp-workstate-handoff validate-write ...` →
    `mcp-workstate-handoff validate --kind decision_id|write ...`
  - `mcp-workstate-handoff compact-session ...` /
    `mcp-workstate-handoff get-compaction ...` /
    `mcp-workstate-handoff get-latest-compaction ...` →
    `mcp-workstate-handoff compaction --operation record|get|get_latest ...`
  - `mcp-workstate-handoff record-file-touch ...` /
    `mcp-workstate-handoff get-touched-files ...` →
    `mcp-workstate-handoff touched-files --operation record|list ...`
  - `mcp-workstate-handoff working-tree-integrity-check ...` /
    `mcp-workstate-handoff post-merge-integrity-check ...` /
    `mcp-workstate-handoff handoff-close-check ...` →
    `mcp-workstate-handoff integrity-check --kind working_tree|post_merge|close ...`
  - `mcp-workstate-handoff archive-task-state ...` /
    `mcp-workstate-handoff tasks-gc ...` /
    `mcp-workstate-handoff get-archived-task ...` →
    `mcp-workstate-handoff archive --operation archive|gc|get ...`
  - `mcp-workstate-handoff task-status ...` →
    `mcp-workstate-handoff set --status-only --task-ref X --status Y [--expected-revision N] ...`

  The `update_task_status` four-case concurrency contract is preserved
  verbatim under `set_handoff_state(status_only=True, ...)`: active-row
  `status='done'` elides `expected_revision` (revision-inference under
  `BEGIN IMMEDIATE`); active-row mid-lifecycle transitions
  (`in_progress`/`blocked`/`review`) require `expected_revision`;
  archived-snapshot status updates remain revisionless via the snapshot
  path.

  The `EXPECTED_HANDOFF_TOOL_COUNT` cross-transport invariant moves
  from 30 to 21. See
  `packages/mcp-workstate-handoff/docs/tasks/WORKSTATE-REF-45-tool-surface-flattening-continuation-task-plan.md`
  for the complete slice-by-slice rationale, including ADR-005 carve-out
  overrides for the integrity-check pair (implementation note) and the archive split
  (implementation note).

### Migration

- **In-process Python callers**: no action required. `archive_task_state`,
  `tasks_gc`, `get_archived_task`, `update_task_status`,
  `record_file_touch`, `get_touched_files`,
  `working_tree_integrity_check`, `post_merge_integrity_check`,
  `handoff_close_check`, `compact_session`, `get_compaction`,
  `get_latest_compaction`, `validate_decision_id`, and `validate_write`
  remain available as attributes on `workstate_handoff_mcp.api` and are
  re-exported from `workstate_handoff_mcp`.
- **MCP wire callers** (stdio/HTTP/CLI): switch to the consolidated
  tools per the table above. The legacy names no longer appear in the
  MCP `tools/list` response or the CLI subcommand registry.

## [0.10.0] — 2026-05-08

### Added

- **Side-effect-free preflight validators** (implementation note implementation note):
  `validate_review_ready` and `validate_finding_resolution` let callers
  preflight gate-readiness and finding-resolution requests without
  bouncing off the mutating path. The resolution validator reuses the
  same WORKSTATE-REF-41 same-or-newer-descendant guard as the mutating path
  (`_classify_commit_relation`), so ancestor or divergent commits are
  rejected at preflight rather than at write time.
- **Dashboard fragment renderer** (implementation note implementation note): the production
  dashboard render path now splits rendered markdown into per-section
  fragment files under `.task-state/DASHBOARD.d/` with a manifest
  (`dashboard_fragments.manifest.json`) so prompt-cache invalidation is
  scoped to the section that actually changed. Both ATX (`## title`) and
  Setext (`title\n---`) H2 headings open a new fragment so the
  production renderer (Setext) and unit-test corpora (ATX) both split
  per-section. `DASHBOARD.txt` is still emitted as a concatenated index
  for back-compat.
- **Write-contract registry** (implementation note implementation note): the registry that
  describes per-tool required fields and field grammars is now exported
  through `limits.write.tools`, and `validate_write` is registered as a
  side-effect-free MCP tool (peer of `validate_decision_id`) so callers
  can preflight an arbitrary write payload against the registry. The
  CLI fallback path also exposes `mcp-workstate-handoff validate-write
  --tool-name <name> --payload-json <json>` so the documented branch-
  review CLI fallback can reach it without the stdio server. Registry
  schemas are aligned with the real Pydantic discriminated-union
  schemas (severity grammar, record/merge/repair_provenance/resolve
  variant shapes).
- **Distribution-name alias package** `workstate_handoff_mcp` re-exports the
  full `workstate_handoff_mcp` public surface (via `__all__` plus
  `__getattr__` submodule forwarding). The wheel ships as
  `mcp-workstate-handoff`, so `import workstate_handoff_mcp` and
  `from workstate_handoff_mcp import api` now work alongside the legacy
  `workstate_handoff_mcp` import path. No rename churn for existing
  callers; both paths yield the same module object.

### Changed

- `EXPECTED_HANDOFF_TOOL_COUNT` bumped from 29 to 30 to cover the new
  `validate_write` tool. Cross-transport invariant tests (`stdio`,
  `http`, CLI fallback) reference the same named constant so transport
  parity is enforced at a single location.

## [0.9.1] — 2026-05-08

### Changed

- **Write-context attribution rollout completed** (WORKSTATE-REF-44). Threads
  `task_ref` through every remaining `_resolve_write_actor()` caller so
  MCP writes that originate from a cwd different than the active task's
  `target_worktree_path` get attributed to the task's git context.
  Wired callers: `record_file_touch`, `update_handoff_state` (UPDATE
  path), `archive_task_state`, `update_task_status`, `switch_task`,
  `record_review_run`. `tasks_gc` is intentionally left un-threaded
  (multi-task scope). The change is strictly additive — explicit
  `actor.branch`/`actor.commit_sha` overrides still win, and there is
  no public-API contract change. Eliminates the `context_drift`
  warnings observed when writes originate from outside the canonical
  worktree.

### Migration

- No migration needed. Existing callers that already passed
  `task_ref=` continue to work unchanged. Callers that omit `task_ref`
  fall back to workspace-row resolution as before; the only behavior
  difference is that when a `task_ref` IS passed, attribution now
  prefers the task row's recorded provenance over the cwd's git state.

## [0.9.0] — 2026-05-07

### Added

- **Commit-backed review-finding reconciliation** (WORKSTATE-REF-41). The `resolve`
  operation classifies finding outcomes against the worktree commit at
  resolution time, and reconciliation now refuses provenance updates that
  would silently rewrite the source commit. New `LIVE_ACTIVE_STATUSES`
  filter and `list_handoff_rows` MCP surface expose live-active rows
  without falling back to legacy enumeration. `update_task_status` elides
  `expected_revision` for terminal `status=done` writes.
- **Working-tree integrity helpers** (WORKSTATE-REF-42 implementation note + working-tree
  assessment items E/G). New module `workstate_handoff_mcp.working_tree`
  exposes `working_tree_integrity_check` (wired into `handoff_close_check`)
  and `post_merge_integrity_check`. Both compare the tree against an
  allowlist sourced from `.task-state/dirty-allowlist`. A new
  `_IMPLICIT_DIRTY_ALLOWLIST` constant tolerates the DB-derived operator
  views (`DASHBOARD.txt`, `CURRENT_TASK.json`) by default so fresh
  worktrees do not trip drift checks before any operator pre-seeding.
- **Compaction CLI** (`workstate_handoff_mcp.compaction_cli`) — bounded read
  surface for compacting session state from the command line; `make
  compact-now` driver target wraps it.
- **Cascade archive + tasks-gc janitor**: archiving an WORKSTATE-REF parent now
  cascades to its `WORKSTATE-REF-PLANNING-REVIEW-*` rows; new janitor pass bulk
  archives status=done WORKSTATE-REF rows whose parent is already archived.

### Changed

- `handoff_close_check` now consults `working_tree_integrity_check` so
  the pre-merge gate refuses to pass when the tree has drifted from HEAD
  on paths outside the effective allowlist.

### Migration

- Consumers reading the working-tree integrity envelope should expect the
  new `allowlist` and `allowlist_source` fields and a non-empty default
  allowlist of `{CURRENT_TASK.json, DASHBOARD.txt}`. To restore the prior
  bare-allowlist behavior, callers can pass `expected_dirty=[...]`
  explicitly to `working_tree_integrity_check`.
- Reconciliation callers that relied on silent provenance rewrites must
  now use `repair_provenance` with explicit `expected_branch` /
  `expected_commit_sha` guards.

## [0.8.0] — 2026-05-04

### Added

- **MCP-resolved task plan paths** (implementation note / WORKSTATE-REF-38). New public
  surface for resolving and editing the active-task plan without
  switching branches:
  - `workstate_handoff_mcp.PlanLocation`, `PlanPathNotRegistered`,
    `resolve_plan_location`, `list_active_task_locations`,
    `plan_show_command` — programmatic plan-path resolution. The
    resolver prefers the active worktree first and falls back to the
    canonical workspace.
  - `workstate_handoff_mcp.plan_cli` — CLI driver with
    `{show, edit, list, register}` subcommands invoked by the
    `make plan-show / plan-edit / plans-list / plan-register` recipes.
  - `workstate_handoff_mcp.scripts.backfill_plan_paths` — one-shot
    enumerator + writer that populates `task_plan_path` on existing
    in-progress tasks via frontmatter discovery.
  - `set_handoff_state` now accepts and persists `task_plan_path`.

### Added (continued — pre-0.8 work that ships in this release)

- **Re-export `workstate_protocol.branch_naming` symbols** (implementation note
  implementation note). `workstate_handoff_mcp.TASK_REF_RE`,
  `derive_task_ref_candidates`, and `format_suggested_branch_name` are
  now part of the public surface, re-exported by reference (not
  literal copy) from `workstate_protocol.branch_naming`. Identity is the
  contract:
  `workstate_handoff_mcp.TASK_REF_RE is workstate_protocol.branch_naming.TASK_REF_RE`
  is asserted by tests so a grammar tweak in one module updates every
  consumer (the four-layer branch-naming gate in `workstate-system`
  imports through `workstate_handoff_mcp`).

### Changed

- **Raise `workstate-protocol` lower bound to `>=0.1.2,<0.2.0`** (implementation note BR-01 fix). `workstate_handoff_mcp.__init__` imports
  `workstate_protocol.branch_naming` at module import, which only ships
  in protocol 0.1.2+. The previous `>=0.1.0` floor allowed
  `uvx mcp-workstate-handoff` to resolve a protocol release missing the
  module, crashing the CLI / MCP server / init-state / hook helpers
  on import. A new packaging test
  (`tests/test_package_metadata.py::test_pyproject_pins_workstate_protocol_lower_bound_at_branch_naming_release`)
  pins the floor so the declaration cannot silently drift back below
  the contract. A behavioral smoke test
  (`tests/test_packaging_floor_smoke.py`) extends this from a
  declaration check into a runtime guarantee: it materializes a clean
  `uv` venv, installs both packages from local source, and asserts
  `from workstate_handoff_mcp import TASK_REF_RE` succeeds *and* matches
  `workstate_protocol.branch_naming.TASK_REF_RE` by identity (implementation note
  BR-R2-04 fix). Note the editable-install gotcha that motivated this:
  in-tree pyenv editable installs of `workstate_handoff_mcp` may resolve
  to whatever copy the root worktree carries, so subprocess hook tests
  on a feature-branch worktree can spuriously fail until the
  packaging-floor changes are merged. The smoke test bypasses that
  ambiguity by constructing a fresh resolver env.

## [0.7.0] — 2026-05-02

### Breaking

- **Minimum Python is now 3.12.** `requires-python` is bumped from
  `>=3.11` to `>=3.12`. The published 0.6.0 wheel was advertised as
  Python 3.11-compatible but crashed at startup under 3.11 because the
  package uses `typing.TypedDict` and pydantic requires
  `typing_extensions.TypedDict` on Python <3.12. Rather than rewrite
  every offending import, the floor is raised to match the version the
  code actually runs on.

  **Migration:** consumers running `uvx mcp-workstate-handoff` must use
  Python 3.12 or newer. With `uv` this is automatic via the package
  metadata; with other resolvers, ensure the active interpreter is
  ≥3.12.

### Changed

- **Active-task plan metadata is now a pinned consumer contract.**
  `set_handoff_state(..., task_plan_path=...)` is the explicit write path
  for task-plan discovery, and `get_handoff_state` now always returns the
  full task-plan field set on active rows:
  `task_plan_path`, `task_plan_abs_path`, `task_plan_exists`, and
  `task_plan_resolution`.
- **Routine writes keep `CURRENT_TASK.json` on-demand by default.**
  The default runtime remains `current_task_auto_regen=False`; routine
  mutation paths such as `close_slice`, review-finding record/update, and
  reconcile only rewrite `CURRENT_TASK.json` when legacy consumers opt back
  in with `AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN=1`. Explicit
  `render_handoff(kind="current_task")` and `export_handoff_state(...,
  include_markdown=True)` remain unconditional export paths.
- **`DASHBOARD.txt` now exposes a stable `ACTIVE TASK PLANS` operator
  section.** The section renders each active task's task ref, target
  branch, declared `task_plan_path`, resolved absolute path, and an
  existence marker (`✓` or `✗`), followed by a footer listing active tasks
  that have not set `task_plan_path`.

- **Review-run CLI subject-kind validation now mirrors the persisted
  contract.** `review-runs --subject-kind branch_diff` is rejected by
  argparse as an invalid choice; branch-diff reviews should persist as
  `subject_kind="branch"` with `subject_path="<base>...<head>"`.
- **Dashboard task-plan enrichment now validates against the full
  `ActiveTask` shape.** The renderer includes `objective` when
  enriching active rows for `ACTIVE TASK PLANS`, avoiding validation
  warnings during dashboard generation.
- **Review-run mutations now expose numeric row ids.** The
  `review_runs(record)` response returns `data.review_run.id` in
  `mutation.affected_ids` and keeps the human-stable `review_run_id`
  in `mutation.affected_keys`, so agents can print compact MCP write
  receipts for context compaction.
- **Dashboard recent-decision rows now preserve handoff ids for all
  active scopes.** Non-epic active tasks get their own `RECENT
  DECISIONS (<task_ref>)` section, and decision lines prefer
  `model_label reasoning_level` in the suffix when available.
- **`DASHBOARD.txt` is now server-owned and auto-regenerated on every
  state-mutating MCP call.** Each public write tool exported from
  `workstate_handoff_mcp/api.py` rewrites `DASHBOARD.txt` once per outer
  call after the underlying transaction commits. The new
  `dashboard_md_regen` envelope field (one of `"ok" | "skipped" |
  "failed"`, with an optional `dashboard_md_regen_error` when render
  fails) is added additively to every mutation envelope; existing
  envelope keys are unchanged. Render failure is reported but never
  rolls back the mutation. Auto-regen defaults to enabled
  (`dashboard_auto_regen=True`); opt out with
  `AGENT_HANDOFF_DASHBOARD_AUTO_REGEN=0` or
  `RuntimeConfig(dashboard_auto_regen=False)` when a CI / batch
  importer manages the file out-of-band. The render path is bounded
  by a 50 ms wall-clock budget enforced by a benchmark in
  `tests/test_dashboard_rendering.py`.
- **`regenerate-task-views` harness hook contract removed.** The
  `regenerate-task-views` PostToolUse row has been dropped from
  `harness-protocol.yaml`, and bootstrap no longer materializes any
  Claude / VS Code / Codex hook wiring that invokes it. The shared
  `scripts/hooks/regenerate-task-views.sh` script remains available
  as a documented manual fallback for the auto-regen opt-out path.
- **Slice-complete decision-id grammar is now published in the
  identity envelope.**
  `get_handoff_state(sections="identity")` (and `load_session`) now
  expose `data.limits.write.slice_complete_decision_id` with
  `canonical_form`, `regex` (the validator constant by reference),
  per-segment `segment_rules`, `valid_examples`, and a
  legacy-write note. Field descriptions on `close_slice.decision`,
  `record_decision.decision`, and `RecordDecisionEvent.decision`
  carry the canonical form and a valid example. `close_slice` now
  accepts semantic parts `(author_tag, work_ref, slug)` and composes
  the canonical id when `decision` is omitted; mixed inputs that
  disagree are rejected with `decision conflicts with semantic slice
  id parts`.
- **New `validate_decision_id` preflight surface.** Exposed via the
  Python API, the MCP tool registry (`validate_decision_id`), and the
  CLI (`mcp-workstate-handoff validate-decision-id`). Returns
  `{ ok, category, error?, suggested? }` using the same validator as
  the mutation path. The MCP surface count is now **24** tools.

### Migration

- Consumers that were running `make render-dashboard` or invoking
  `regenerate-task-views.sh` from a harness PostToolUse hook can
  remove that step; `DASHBOARD.txt` now refreshes inside the server.
  CI pipelines that manage the file out-of-band should set
  `AGENT_HANDOFF_DASHBOARD_AUTO_REGEN=0` and call
  `render_handoff(kind="dashboard")` explicitly after their batch.
- Cold-start agents should read the slice-complete decision-id
  grammar from `get_handoff_state(sections="identity")` rather than
  copying the regex; pass semantic `(author_tag, work_ref, slug)` to
  `close_slice` to let the server compose the id.
- Legacy `decision`-only callers of `close_slice` and direct callers
  of `record_decision` are unchanged.


- To expose a task plan from the repo root, set
  `task_plan_path="docs/plans/..."` on the active task and read the
  resolved fields from `get_handoff_state` or the `ACTIVE TASK PLANS`
  section in `DASHBOARD.txt`.
- Consumers that still expect routine writes to refresh `CURRENT_TASK.json`
  must opt in explicitly with `AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN=1`.
  New integrations should treat `render_handoff(kind="current_task")` as
  the explicit snapshot/export step.
- No schema migration is required. Review automation that records
  branch diffs should use `subject_kind="branch"` and keep
  `branch_diff` only as a human-readable review-scope label.

## [0.5.1] — 2026-04-28

### Changed

- **Package-local runtime and review-intake guidance is now explicit for distributed clients.**
  The package README/specs now tell operators and agents to prefer MCP review
  surfaces (`get_latest_slice_review_packet`, `get_review_findings_summary`,
  `load_session`, `search_handoff`, `get_verified_tests`) before inspecting
  `.task-state/handoff.db` directly, and they point package-local test runs at
  the package root / Makefile flow instead of assuming a workspace-level Python
  interpreter already has `pytest` installed.

### Migration

- No code changes required. On upgrade, prefer the documented package-local
  `make test-handoff` / `make check-handoff` flow when validating from source,
  and use the documented MCP-first review-intake path before dropping to raw DB
  inspection.

## [0.5.0] — 2026-04-26

### Breaking

- **Distribution name renamed from `workstate-handoff-mcp` to
  `mcp-workstate-handoff`.** The console script (`mcp-workstate-handoff`) and
  importable Python module (`workstate_handoff_mcp`) are unchanged. Only the
  PyPI / `pip install` name moves, to align with the binary name and the
  broader MCP ecosystem convention (`mcp-server-*`).

  **Migration:** anywhere you wrote `pip install workstate-handoff-mcp` or
  pinned `workstate-handoff-mcp @ git+ssh://...`, swap to `pip install
  mcp-workstate-handoff` (or `mcp-workstate-handoff>=0.5.0,<0.6.0` for a
  range). No source-code changes required — `from workstate_handoff_mcp
  import ...` still works.

  The legacy `workstate-handoff-mcp` distribution is left at `0.4.3` on
  PyPI; it will not receive future updates.

### Packaging

- **Hoist Workstate System MVP packaging metadata.** `pyproject.toml`
  declares a `[tool.hoisted]` table for harness scripts that need to
  resolve the install surface via
  `git+https://github.com/darce/workstate.git@mcp-workstate-handoff-v{version}#subdirectory=packages/mcp-workstate-handoff`.

## [0.4.3] — 2026-04-24

### Breaking

- **Console script renamed from `workstate-handoff-mcp` to `mcp-workstate-handoff`**
  to match the `mcp-*` prefix naming convention shared with sibling MCP
  servers (`mcp-workstate-orchestrator`, etc.). The PyPI/git package name
  (`workstate-handoff-mcp`) and the importable Python module
  (`workstate_handoff_mcp`) are unchanged — only the installed CLI entry point
  flips. Consumers must update `[mcpServers].*.command` entries in MCP
  client configs (e.g. `.mcp.json`, `.vscode/mcp.json`, `.codex/config.toml`),
  release scripts, and any direct subprocess invocations.

### Migration

- Update the `command` field wherever the server is launched:

  ```diff
  - "command": "workstate-handoff-mcp"
  + "command": "mcp-workstate-handoff"
  ```

  No Python-level changes are required: `from workstate_handoff_mcp import ...`
  keeps working. Pipe the same flags (`--workspace-root`, `serve-stdio`,
  `doctor`) — arg semantics are unchanged.

## [0.4.1] — 2026-04-22

### Fixed

- **`run_doctor` no longer hard-fails on transient stdio handshake errors
  in fresh consumer venvs.** The stdio + CLI startup probes are now
  best-effort by default: if either probe raises (e.g. `mcp.shared.exceptions.McpError:
  Connection closed` from the fastmcp `Client`, or a `CalledProcessError`
  from the CLI subprocess), the failure is captured into
  `checks.stdio_startup.error` / `checks.cli_fallback_startup.error` in the
  JSON report and `doctor` exits 0 unless **both** probes fail. Set
  `AGENT_HANDOFF_DOCTOR_STRICT=1` (CI / release smokes) to restore the
  hard-fail-on-any-probe-error behaviour.

### Migration

- Consumer setup scripts that parsed `payload["ok"]` as the only
  health signal still work — `ok` now reflects whether at least one of
  the two probes succeeded. Scripts that need the prior strict semantic
  must export `AGENT_HANDOFF_DOCTOR_STRICT=1` before invoking `doctor`.
- Programmatic readers of `checks.stdio_startup` and
  `checks.cli_fallback_startup` should expect an optional `error` key on
  each block, present only when that probe failed.

### Versioning realignment

- The standalone `darce/mcp-workstate-handoff` v0.1.0 tag (the original
  packaging cut) is retired in favour of the in-source `pyproject.toml`
  version line. From v0.4.1 forward, the standalone repo always tags
  `v<pyproject.version>`. Consumers pinned to `@v0.1.0` should re-pin to
  `@v0.4.1` (or `@main` for tracking).

## [0.4.0] — 2026-04-07

### Added

- **Oversize-response advisory warning.** The response envelope built by
  `_envelope()` now appends an `oversize_response: ~<bytes> bytes (~<tokens>
  tokens) ...` warning to `payload["warnings"]` whenever the serialised
  payload exceeds `RESPONSE_OVERSIZE_WARN_BYTES` (default 20,000 bytes,
  ~5,000 tokens). The warning is purely advisory — the response is still
  returned in full so callers are not silently truncated — but it names the
  bounded-read levers callers should adopt for the next call:

  - `sections="identity"` for routine identity-only checks (returns just
    `active` + `limits`).
  - `sections="<comma-separated>"` to fetch only the sections you need.
  - `detail="summary"` to truncate long-form rationale, fix, and verification
    fields to 200 chars.
  - Lower `top_n_blockers`, `top_n_actions`, `top_n_decisions`, `top_n_tests`,
    `top_n_findings` to reduce row counts.
  - `fields=...` (where supported) to project specific columns.

  This is exposed via the new `RESPONSE_OVERSIZE_WARN_BYTES` constant in
  `shared_primitives.py`. Callers that already use bounded-read parameters
  will never see the warning. The threshold is tunable but should remain a
  soft cap — hard truncation belongs at the caller's discretion, not the
  envelope's.

  Motivated by WORKSTATE-REF-14: a routine `get_handoff_state(top_n_decisions=10,
  detail="full")` call against WORKSTATE-REF-3 returned ~17.6k tokens because
  slice-complete decision rationales dominate the payload, and WORKSTATE-REF-7 /
  WORKSTATE-REF-10 wire-format optimizations only attack the wrapper, not the
  rationale text itself. The warning is the cheapest possible nudge toward
  the documented narrowing levers.

### Migration — what callers must do

- **Nothing required.** This is an additive change. Existing callers will
  start seeing an extra warning entry on oversize responses; the response
  body is unchanged.
- If you were already filtering `payload["warnings"]` for `context_drift:`
  prefixes, add `oversize_response:` to your filter list to surface the new
  advisory.
- Treat the advisory as a soft signal: **the next call** should be narrowed,
  not the current one. Do not retry the same call expecting different output.

### Companion enforcement (outside the package)

This release also introduces an out-of-package PreToolUse hook —
`scripts/hooks/guard-task-plan-findings.py` in the monorepo root — that
rejects any Edit/Write attempting to paste a review-finding list into a
task plan, epic, or planning document. The hook is wired into both
`.claude/settings.json` and `.github/hooks/terminal-guard.json`, runs in
`make check-all` via `make lint-task-plans`, and exposes a `--scan-staged`
mode for opt-in `git pre-commit` integration (the monorepo does not ship
a checked-in `.git/hooks/pre-commit`; teams that want commit-time
enforcement should wire it themselves via `core.hooksPath` or a tool like
`pre-commit`). Review findings live in `workstate-handoff-mcp` and are
recorded with `review_findings(review={"operation":"record"|"batch_record",
...})`; pasting them inline duplicates the source of truth and escapes the
pre-merge gate.

## [0.3.0] — 2026-04-08

### Changed (BREAKING — wire format)

- **MCP tool responses are now native JSON objects, not JSON strings.**
  Every handoff-mcp tool handler is annotated `-> dict` and returns a real
  Python dict via `_envelope()` / `_json_response()`. FastMCP serialises
  the dict exactly once on the wire instead of running a
  `json.dumps -> structured_content={"result": "<escaped JSON>"} -> json.loads`
  round trip.

  **Old wire payload (≤0.2.x):**
  ```json
  {"structured_content": {"result": "{\"ok\": true, \"schema_version\": 2, \"data\": {...}}"}}
  ```

  **New wire payload (≥0.3.0):**
  ```json
  {"structured_content": {"ok": true, "schema_version": 2, "data": {...}}}
  ```

  The envelope **fields** (`ok`, `schema_version`, `tool`, `scope`, `data`,
  `mutation`, `artifacts`, `warnings`, `task_ref`) are unchanged. The
  envelope `schema_version` stays at `2` because the field set and contract
  have not moved — only the wire format went from JSON-string-inside-JSON
  to native nested object.

  Per-call wire savings range from ~9.5% on large structured payloads to
  ~24% on small ones, depending on how many `"` and `\n` characters the
  legacy form had to escape.

### Migration — what callers must do

- **Use the canonical access pattern** (this was always documented in the
  README and `docs/guides/token-efficient-usage.md`, but is now mandatory):
  ```python
  result = mcp_tool.call(...)        # in-process or via FastMCP client
  active = result["data"]["active"]  # canonical
  # NOT result["active"] — the legacy top-level mirror was removed in 0.3.0
  # and never returns
  ```
- **Stop wrapping handler results in `json.loads(...)`.** Pre-0.3.0 callers
  did `parsed = json.loads(handoff_tool(...))`. Post-0.3.0 the call
  returns a dict directly:
  ```python
  result = handoff_tool(...)         # already a dict
  if not result.get("ok"):
      ...
  ```
  In-monorepo callers under `workstate-orchestrator-mcp` and `scripts/` were
  updated in lockstep with this release. The four private `_json_load`
  helpers in `packages/mcp-workstate-orchestrator/src/agent_orchestrator_mcp/orchestration/`
  now accept either `str` or `dict` so caller sites need not change.
- **External tools that read `result.content[0].text` and then
  `json.loads()` the inner string** must instead read
  `result.structured_content` directly. The `text` field still exists for
  backward compatibility with MCP clients that only consume the text
  channel, but the canonical payload is now in `structured_content`.
- **Tests** that build a flat-access dict via a local helper like
  `_parse_response(raw)` should accept both `str` (CLI stdout capture)
  and `dict` (in-process handler call) input. Eight in-tree test files
  use the smart pattern; copy the same idiom for new tests:
  ```python
  def _parse(raw: str | dict) -> dict:
      result = raw if isinstance(raw, dict) else json.loads(raw)
      ...
  ```

### Removed

- The `_make_dict_wrapper` shim in `packages/mcp-workstate-handoff/src/workstate_handoff_mcp/api.py`
  is gone. Earlier versions wrapped string-returning handlers at FastMCP
  registration time to convince it the result was a dict; the wrapper is no
  longer needed because handlers return dicts natively.
- The `_flatten_v2` helper in `core.py` is gone. Compound tools
  (`load_session`, `close_slice`) now read inner-tool results from the
  canonical `result["data"][...]` path directly with no merge step.
- The legacy top-level mirror introduced by WORKSTATE-REF-3 (where every `data`
  field was duplicated at the envelope root) was removed by WORKSTATE-REF-7
  implementation note; WORKSTATE-REF-10 finishes the cleanup by deleting all the bridging
  shims that depended on it.

### History notice

This release closes out the deferred half of WORKSTATE-REF-7 ("Response Envelope
Token Optimization"). WORKSTATE-REF-7 documented implementation note ("Dict Return at MCP
Boundary") as complete in late March, but the work landed as a
backward-compat shim (`_make_dict_wrapper`) instead of a real dict-return
end to end. WORKSTATE-REF-10 (this release) removes the shim and flips every
handler to its honest return type. There is no deprecation window: agents
running against `workstate-handoff-mcp ≥0.3.0` see the new wire format
immediately, and any consumer that hardcodes the legacy
`structured_content.result` shape will break on first call. The
in-monorepo orchestrator package, scripts, and tests were all migrated in
the same commit (`406dbae3` followed by an amend that includes this
changelog entry and the `_runtime_pythonpath` annotation revert from
branch-review finding `WORKSTATE-REF-10-BR-01`).

## [0.2.0] — 2026-03-09

Initial published release. v2 envelope, discriminated tool surface
(WORKSTATE-REF-6), SQLite-backed handoff state store, FastMCP stdio transport.
