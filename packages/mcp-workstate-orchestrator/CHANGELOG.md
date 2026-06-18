# mcp-workstate-orchestrator

Condensed public changelog — internal references removed, one headline
per change. Auto-generated from the project's release notes.

## [0.7.0] — 2026-06-11

### Changed
- Bump member pins to the 0.1.24 stack (protocol 0.3.0, handoff 0.13.0); raise the `[bridge]` extra floor to `workstate-codex-bridge>=0.2.0,<0.3.0` so the managed uvx pin resolves the current bridge wheel.

## [0.6.6] — 2026-06-10

### Changed
- Build: migrate sdist build backend setuptools→hatchling with at-build privacy scrub (implementation note sdist-privacy sweep).

## [0.6.5] — 2026-06-08

### Changed
- Privacy: internal project ids scrubbed from shipped source.

## [0.6.3] — 2026-06-07

### Changed
- Re-cut of the unpublished 0.6.2: dependency floors moved to `workstate-protocol>=0.2.4`, `mcp-workstate-handoff>=0.12.6`.

## [0.6.2] — 2026-06-07

### Changed
- Dependency floors raised: `workstate-protocol>=0.2.3`, `mcp-workstate-handoff>=0.12.5` (internal grok harness parity release).

## [0.6.1] — 2026-06-06

### Changed
- `run_structured_turn` kind-branched dispatch: in-process backends route via the adapter runner seam (verbatim schema pass-through, single timeout layer, recursion guard); `probe_availability` annotates downstream prerequisites and `list_available_backends` passes them through (internal).

## [0.6.0] — 2026-06-04

### Changed
- **Breaking default:** `list_available_backends` now probes live availability by default (`probe=true`), so MCP callers and skills can distinguish "declared" from "actually reachable" before dispatching.
- Dependency floor: `workstate-protocol>=0.2.0`.

## [0.5.2] — 2026-06-03

### Added
- **Probed availability for `list_available_backends`.** The tool now accepts an optional `probe: bool = True` argument (CLI: `list-backends --probe`).
- **Optional `bridge` extra.** `workstate-codex-bridge` is now an installable optional-dependency (`mcp-workstate-orchestrator[bridge]`), resolved locally from the sibling source.

## [0.5.1] — 2026-06-01

### Changed
- **Drop the stale `"duplicate altcontext"` orchestrator-guidance string.** Final cleanup of the purged legacy `altcontext-*` naming so generated guidance no longer references a name that no longer exists.

## [0.5.0] — 2026-05-30

### Changed
- **MCP server identity cutover — `workstate-orchestrator-mcp` → `workstate-orchestrator-mcp` (implementation note Slice B).** Canonical registered server name updated; bootstrap collapses any stale duplicate registration to the single canonical name.
- **Doc paths resolve through `workstate_protocol` (implementation note Slice D).** `api` now imports `HARNESS_CONTRACT_RELPATH` and `INSTRUCTIONS_RELPATH` from `workstate-protocol` (>=0.1.6), reading from the renamed `docs/workstate/` mirror.

### Notes
- Coordinated rebrand release with `workstate-protocol` 0.1.6, `mcp-workstate-handoff` 0.12.0, and `workstate-bootstrap` 0.6.0.

## [0.4.7] — 2026-05-13

### Changed
- **`evaluate_review_ready` trusts `current_task_sync.is_violation` explicitly.** Removed the `not current_task_in_sync` fallback that silently re-introduced `CURRENT_TASK.json is out of sync with handoff state` as a hard blocking reason whenever an older `mcp-workstate-handoff` envelope omitted the `is_violation` key.

## [0.4.6] — 2026-05-11

### Changed
- **Track `mcp-workstate-handoff` 0.11.2 contextmanager change**: the local re-exporter `lanes._get_db_connection` now declares its return type as `AbstractContextManager[sqlite3.Connection]` to match the upstream factory, which is now a generator-based context manager that closes the underlying connection on exit.

## [0.4.5] — 2026-05-10

### Changed
- **Bump `mcp-workstate-handoff` floor to `>=0.11.0,<0.12.0`** to pick up internal BR-01/02/03 fixes (multi-active dashboard projection, malformed import-payload rejection, target_branch/worktree_path/plan_path preservation) and internal compaction env-var namespace consolidation.

## [0.4.4] — 2026-05-08

### Changed
- Bump `mcp-workstate-handoff` floor to `>=0.10.0,<0.11.0` so the orchestrator picks up the implementation note surface: side-effect-free preflight validators, dashboard fragment renderer in the production render path, write-contract registry exposed via `limits.write.tools` + `validate_write` tool, and the `mcp_agent_handoff` distribution- name alias.

## [0.4.3] — 2026-05-08

### Changed
- Refresh bundled `_assets/rules/branch-review-guide.md` asset to include the revision-history guidance block.

## [0.4.2] — 2026-05-07

### Changed
- Bump `mcp-workstate-handoff` floor to `>=0.9.0,<0.10.0` so the orchestrator picks up commit-backed review-finding reconciliation (internal) and the working-tree integrity helpers (internal).
- `lane_exec` prefers bash for lane preflight invocations.

## [0.4.1] — 2026-05-04

### Changed
- Bump `mcp-workstate-handoff` floor to `>=0.8.0,<0.9.0` so the orchestrator picks up the new `plan_resolve` / `plan_cli` surface (implementation note / internal) for plan-path resolution.
- Identity-response baseline rebaselined (1159 → 1551 bytes) per internal / internal docs reorganization.

## [0.4.0] — 2026-05-02

### Breaking
- **Minimum Python is now 3.12.** `requires-python` is bumped from `>=3.11` to `>=3.12`, mirroring the same bump in the sibling `mcp-workstate-handoff` package.

### Changed
- **Sibling dependency repinned to `mcp-workstate-handoff>=0.7.0,<0.8.0`.** Tracks the 0.7.0 release of `mcp-workstate-handoff`, which carries the matching `requires-python` floor.

## [0.2.0] — 2026-04-26

### Breaking
- **Distribution published as `mcp-workstate-orchestrator`.** An earlier PyPI name was squatted by an unrelated party; the canonical name aligns with the binary name (`mcp-workstate-orchestrator`) and the sibling `mcp-workstate-handoff`.

### Changed
- Sibling dependency repinned: the previous `workstate-handoff-mcp @ git+ssh://...@v0.4.3` line is replaced with `mcp-workstate-handoff>=0.5.0,<0.6.0` from PyPI.

## [0.1.4] — 2026-04-24

### Breaking
- **Console script is `mcp-workstate-orchestrator`**, matching the `mcp-*` prefix naming convention shared with sibling MCP servers (`mcp-workstate-handoff`, etc.).

### Changed
- `mcp-workstate-handoff` dependency advanced from `v0.4.2` to `v0.4.3` to pick up the paired console-script name (`mcp-workstate-handoff`).
- `run_doctor` and `run_tools_snapshot` now return `{"server": "mcp-workstate-orchestrator"}` to match the new CLI name.
- `argparse` `prog=` and the `doctor` fallback default were updated to `mcp-workstate-orchestrator`.

### Migration
- Update the `command` field wherever the server is launched:
- Consumers parsing the `server` field of `doctor` / `tools-snapshot` output should expect `mcp-workstate-orchestrator` instead of `workstate-orchestrator-mcp`.

## [0.1.1] — 2026-04-22

### Added
- `SliceReviewPacket.external_changed_files` field.

### Changed
- `workstate-handoff-mcp` dependency advanced from `v0.1.0` to `v0.4.1` to pick up the `run_doctor` soft-fail patch and align with the current published consumer install URL.

## [0.1.0] — 2026-04-19

### Added
- **Hoist Agentic System MVP packaging metadata.** `pyproject.toml` now declares a `[tool.hoisted]` table for the standalone install surface: `git+ssh://git@github.com/darce/mcp-workstate-orchestrator.git@v{version}`.
