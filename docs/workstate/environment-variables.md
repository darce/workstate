# Environment Variables Registry

Canonical catalog of every environment variable read by the monorepo's runtime
code. Tests-only and dev-tooling-only knobs are listed in their own section so
operators can tell production knobs apart from test scaffolding.

> **Prefix conventions**
>
> - `WORKSTATE_HANDOFF_*` — `mcp-workstate-handoff` runtime config (state
>   dir, render paths, write-context overrides, hook knobs).
> - `WORKSTATE_*` — cross-cutting knobs owned by `workstate-system` (hook
>   protocol, branch-naming overrides, lane plumbing, lifecycle tooling).
>
> The legacy `AGENT_HANDOFF_*` / `AGENT_ORCHESTRATOR_*` / `AGENTIC_*` /
> `WORKSTATE_*` env-var names are **no longer read** — the one-release alias shim
> was retired in `WS-ENV-SHIM-RETIRE-01`. Set the canonical `WORKSTATE_*`
> names only.

## How to add a new variable

1. Pick the prefix from the package that owns the read site
   (`WORKSTATE_HANDOFF_*` for `mcp-workstate-handoff`, `WORKSTATE_*` for
   `workstate-system`). Do not invent a new prefix.
2. Add a row to the appropriate table below with: name, type, default,
   one-line description, source file.
3. If the var configures the compaction subsystem, add the field to
   `workstate_handoff_mcp.compaction.CompactionSettings` and read it through
   `CompactionSettings.from_env()` — never via a fresh `os.environ.get`.
4. If the var has a typo-prone parser (int, enum), prefer Pydantic
   validation at the boundary so bad values raise rather than fall back
   to defaults silently.

## `WORKSTATE_HANDOFF_*` — handoff runtime

| Name | Type | Default | Description | Source |
|---|---|---|---|---|
| `WORKSTATE_HANDOFF_ACE_GUIDANCE_USED` | bool | unset | Marks that a turn already consumed the in-prompt agent-context guidance so the next prompt can omit it. | `mcp-workstate-handoff/src/workstate_handoff_mcp/api.py` |
| `WORKSTATE_HANDOFF_COMPACTION_DISABLED` | bool | `0` | When truthy, silences the unified disable resolver: the compact-session Stop hook short-circuits with `compaction skipped: disabled (source=env)` and `compute_compaction_advisory` returns a `disabled=true,disabled_source="env"` envelope with no threshold/floor evaluation. Host-harness compaction is unaffected. The same surface is also writable per-task or workspace-default via the `compaction(operation="disable"\|"enable"\|"status")` MCP op, `mcp-workstate-handoff compaction --operation <op>` CLI, and `make compaction-disable\|compaction-enable\|compaction-status [TASK=<ref>]` operator wrappers. | `workstate-system/scripts/hooks/compact-session.py`, `mcp-workstate-handoff/src/workstate_handoff_mcp/compaction.py` |
| `WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TURNS` | int (≥0) | `1` | Floor: skip compaction if the transcript has fewer than N new turns since the last compaction. | `workstate-system/scripts/hooks/compact-session.py` |
| `WORKSTATE_HANDOFF_COMPACTION_MIN_NEW_TOKENS` | int (≥0) | `0` | Floor: skip compaction if the encoded new-turn token count is below the threshold. `0` disables the floor. | `workstate-system/scripts/hooks/compact-session.py` |
| `WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN` | bool | unset | Opt-in to server-side auto-regeneration of `CURRENT_TASK.json` after handoff writes. See `docs/CONSUMER.md`. | `mcp-workstate-handoff/src/workstate_handoff_mcp/config.py` |
| `WORKSTATE_HANDOFF_CURRENT_TASK_PATH` | path | derived | Override the rendered `CURRENT_TASK.json` location. | `mcp-workstate-handoff/src/workstate_handoff_mcp/config.py` |
| `WORKSTATE_HANDOFF_DASHBOARD_PATH` | path | derived | Override the rendered `DASHBOARD.txt` location. | `mcp-workstate-handoff/src/workstate_handoff_mcp/config.py` |
| `WORKSTATE_HANDOFF_DEFAULT_AGENT` | string | derived | Stable agent identity used when MCP write payloads omit `actor.agent`. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_write_context.py` |
| `WORKSTATE_HANDOFF_DEFAULT_BRANCH` | string | derived | Branch label applied to write-provenance when git is unavailable. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_write_context.py` |
| `WORKSTATE_HANDOFF_DEFAULT_COMMIT_SHA` | sha | derived | Commit-SHA fallback for write-provenance when git is unavailable. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_write_context.py` |
| `WORKSTATE_HANDOFF_DOCTOR_STRICT` | bool | unset | When truthy, `run_doctor()` treats warnings as failures. | `mcp-workstate-handoff/src/workstate_handoff_mcp/...` |
| `WORKSTATE_HANDOFF_ENFORCE_BRANCH` | bool | `0` | Reject writes whose payload branch does not match the workspace branch. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_write_context.py` |
| `WORKSTATE_HANDOFF_EXPORTS_DIR` | path | `.task-state/exports` | Destination for `export_handoff_state()` artifacts. | `mcp-workstate-handoff/src/workstate_handoff_mcp/config.py` |
| `WORKSTATE_HANDOFF_HARNESS` | enum (`claude-code`/`codex`/`cursor`/`manual`) | `claude-code` | Harness label for compaction rows. Unknown values coerce to `manual`. | `workstate-system/scripts/hooks/compact-session.py` |
| `WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT` | bool | unset | Bypass `WORKSTATE_HANDOFF_ENFORCE_BRANCH` for the current process. Tests + bootstrapping. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_write_context.py` |
| `WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION` | bool | unset | Bypass commit-sha existence checks. Tests + bootstrapping. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_write_context.py` |
| `WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION` | bool | unset | Bypass git-worktree derivation in write-context resolution. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_write_context.py` |
| `WORKSTATE_HANDOFF_STATE_DIR` | path | `<workspace>/.task-state` | Override the SQLite + projection file root. Tests use this for isolation. | `mcp-workstate-handoff/src/workstate_handoff_mcp/config.py` |
| `WORKSTATE_HANDOFF_TOOL_PROFILE` | string | derived | Tool-profile selector for the MCP server's tool surface. | `mcp-workstate-handoff/src/workstate_handoff_mcp/config.py` |
| `WORKSTATE_HANDOFF_WORKSPACE_ROOT` | path | derived | Override workspace-root resolution when the consumer-root probe is ambiguous. | `mcp-workstate-handoff/src/workstate_handoff_mcp/config.py` |

On a successful `compact-session` Stop hook run, stderr preserves
`compaction_id=<id>` as the first line and then appends
`tokens_saved_estimate`, `input_chars`, `summary_chars`, and
`prose_residual_chars` as one `key=value` line each. Those receipt fields
describe the WORKSTATE-REF `session_compactions` artifact only; host-harness
compaction remains outside these environment knobs.

None of the `WORKSTATE_HANDOFF_COMPACTION_*` vars (nor the writable per-task /
workspace DB enable/disable rows behind `compaction(operation=...)`) install a
harness Stop adapter. They only gate whether the compaction surface evaluates
and records once an adapter is already wired. Installing the automatic recorder
is a separate, opt-in step (`workstate-bootstrap install --install-<harness>-stop-hook`);
`make doctor LIFECYCLE_ARGS=--json` reports per-harness adapter wiring as
installed / drifted (`stop_adapters_drifted`) / optional-not-installed. See the
"Enabled vs wired" subsection of
`packages/workstate-system/docs/workstate/rules/development-workflow.md`.

## `WORKSTATE_*` — cross-cutting

| Name | Type | Default | Description | Source |
|---|---|---|---|---|
| `WORKSTATE_ALLOW_NONCONFORMING_BRANCH` | bool | unset | Opt-out of the branch-naming pre-commit gate for the current commit. | `workstate-system/scripts/hooks/check_branch_naming.py` |
| `WORKSTATE_ALLOW_NONCONFORMING_BRANCH_REASON` | string | unset | Required when `WORKSTATE_ALLOW_NONCONFORMING_BRANCH=1`; recorded with the override. | `workstate-system/scripts/hooks/check_branch_naming.py` |
| `WORKSTATE_ALLOW_NONCONFORMING_BRANCH_PUSH` | bool | unset | Same opt-out, scoped to the pre-push hook. | `workstate-system/scripts/hooks/check_branch_naming.py` |
| `WORKSTATE_ALLOW_NONCONFORMING_BRANCH_PUSH_REASON` | string | unset | Reason payload for the pre-push override. | `workstate-system/scripts/hooks/check_branch_naming.py` |
| `WORKSTATE_HOOK_PROTOCOL_STRICT` | bool | unset | When truthy, `_protocol.validate_event` raises `SystemExit(2)` instead of logging on schema drift. | `workstate-system/scripts/hooks/_protocol.py` |
| `WORKSTATE_LANE_ID` | string | unset | Worktree-lane identifier surfaced in write provenance. | `mcp-workstate-handoff/src/workstate_handoff_mcp/import_export.py` |
| `WORKSTATE_LANE_ID_ENV` | string | `WORKSTATE_LANE_ID` | Indirection knob: name of the env var that actually carries the lane id. Lets callers redirect lookups. | `mcp-workstate-handoff/src/workstate_handoff_mcp/shared_primitives.py` |
| `WORKSTATE_LIFECYCLE_UV_BIN` | path | derived | Override the `uv` binary used by lifecycle scripts (`task-start`, `slice-start`, etc.). | `workstate-system/scripts/workstate/lifecycle/uv_provisioning.py` |

## Test + dev-only knobs

Read only from test fixtures or developer tooling. Production deployments
do not set these.

| Name | Use |
|---|---|
| `WORKSTATE_DISABLE_PYTEST_PATH_GUARD` | Test harness escape hatch for `test_pytest_path_guard.py`. |

## See also

- `docs/CONSUMER.md` — externally documented runtime knobs.
- `docs/UPGRADING.md` — upgrade notes that mention env vars at version
  boundaries.
- `workstate_handoff_mcp.CompactionSettings` — typed surface for
   `WORKSTATE_HANDOFF_COMPACTION_*`. Add new compaction knobs there, not as
  fresh `os.environ.get` calls.
