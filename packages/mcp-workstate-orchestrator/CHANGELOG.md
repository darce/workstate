# Changelog

All notable changes to `mcp-workstate-orchestrator` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## [0.5.2] — 2026-06-03

### Changed

- TODO: summarize this release.


### Added

- **Probed availability for `list_available_backends`.** The tool now accepts an
  optional `probe: bool = False` argument (CLI: `list-backends --probe`). When
  enabled it adds `is_available`, `availability_state`, and `availability_detail`
  per backend via the new `backend_registry.probe_availability`, distinguishing
  `declared_not_installed` (e.g. the optional `codex-subagent` bridge module is
  not importable in this runtime), `unavailable` (installed but unusable, or a
  missing CLI), and `reachable` (importable, but liveness not verified). The
  default call is unchanged and stays cheap — no subprocess calls and no optional
  bridge imports.
- **Optional `bridge` extra.** `workstate-codex-bridge` is now an installable
  optional-dependency (`mcp-workstate-orchestrator[bridge]`), resolved locally
  from the sibling source. It remains intentionally excluded from base installs
  and the bootstrap presync; the probed availability view documents and surfaces
  that contract.

## [0.5.1] — 2026-06-01

### Changed

- **Drop the stale `"duplicate altcontext"` orchestrator-guidance string.**
  Final cleanup of the purged legacy `altcontext-*` naming so generated
  guidance no longer references a name that no longer exists. No public-API
  changes; coordinated-release floor bump tracking `workstate-protocol`
  0.1.7.


## [0.5.0] — 2026-05-30

### Changed

- **MCP server identity cutover — `workstate-orchestrator-mcp` →
  `workstate-orchestrator-mcp` (implementation note Slice B).** Canonical registered
  server name updated; bootstrap collapses any stale duplicate registration to
  the single canonical name.
- **Doc paths resolve through `workstate_protocol` (implementation note Slice D).**
  `api` now imports `HARNESS_CONTRACT_RELPATH` and `INSTRUCTIONS_RELPATH` from
  `workstate-protocol` (>=0.1.6), reading from the renamed
  `docs/workstate/` mirror. Dependency floors raised: `workstate-protocol`
  `>=0.1.6`, `mcp-workstate-handoff` `>=0.12.0,<0.13.0`.

### Notes

- Coordinated rebrand release with `workstate-protocol` 0.1.6,
  `mcp-workstate-handoff` 0.12.0, and `workstate-bootstrap` 0.6.0.

## [0.4.7] — 2026-05-13

### Changed

- **`evaluate_review_ready` trusts `current_task_sync.is_violation`
  explicitly.** Removed the `not current_task_in_sync` fallback that
  silently re-introduced `CURRENT_TASK.json is out of sync with handoff
  state` as a hard blocking reason whenever an older
  `mcp-workstate-handoff` envelope omitted the `is_violation` key. Pairs
  with `mcp-workstate-handoff` 0.11.4, which makes `handoff_close_check`
  materialize the on-disk file on demand so `is_in_sync` is a
  guaranteed post-condition. Callers no longer need a pre-render
  workaround in monorepo Makefiles.

## [0.4.6] — 2026-05-11

### Changed

- **Track `mcp-workstate-handoff` 0.11.2 contextmanager change**: the local
  re-exporter `lanes._get_db_connection` now declares its return type
  as `AbstractContextManager[sqlite3.Connection]` to match the upstream
  factory, which is now a generator-based context manager that closes
  the underlying connection on exit. Runtime behavior is unchanged for
  all `with _get_db_connection() as conn:` callsites.

## [0.4.5] — 2026-05-10

### Changed

- **Bump `mcp-workstate-handoff` floor to `>=0.11.0,<0.12.0`** to pick up
  WORKSTATE-REF-54 BR-01/02/03 fixes (multi-active dashboard projection,
  malformed import-payload rejection,
  target_branch/worktree_path/plan_path preservation) and WORKSTATE-REF-55
  compaction env-var namespace consolidation. No orchestrator surface
  symbols changed; the bump is purely a coordinated-release floor.

## [0.4.4] — 2026-05-08

### Changed

- Bump `mcp-workstate-handoff` floor to `>=0.10.0,<0.11.0` so the
  orchestrator picks up the implementation note surface: side-effect-free
  preflight validators, dashboard fragment renderer in the production
  render path, write-contract registry exposed via `limits.write.tools`
  + `validate_write` tool, and the `mcp_agent_handoff` distribution-
  name alias. No orchestrator-public-API changes.

## [0.4.3] — 2026-05-08

### Changed

- Refresh bundled `_assets/rules/branch-review-guide.md` asset to include
  the revision-history guidance block. No orchestrator-public-API
  changes; this is a packaged-doc-only patch so consumers picking up
  the wheel get the same review guide bytes shipped from the monorepo.

## [0.4.2] — 2026-05-07

### Changed

- Bump `mcp-workstate-handoff` floor to `>=0.9.0,<0.10.0` so the orchestrator
  picks up commit-backed review-finding reconciliation (WORKSTATE-REF-41) and the
  working-tree integrity helpers (WORKSTATE-REF-42). No orchestrator-public-API
  changes.
- `lane_exec` prefers bash for lane preflight invocations.

## [0.4.1] — 2026-05-04

### Changed

- Bump `mcp-workstate-handoff` floor to `>=0.8.0,<0.9.0` so the orchestrator
  picks up the new `plan_resolve` / `plan_cli` surface (implementation note /
  WORKSTATE-REF-38) for plan-path resolution. No orchestrator-public-API changes.
- Identity-response baseline rebaselined (1159 → 1551 bytes) per
  WORKSTATE-REF-37 implementation note / WORKSTATE-REF-3 docs reorganization.

## [0.4.0] — 2026-05-02

### Breaking

- **Minimum Python is now 3.12.** `requires-python` is bumped from
  `>=3.11` to `>=3.12`, mirroring the same bump in the sibling
  `mcp-workstate-handoff` package. The previous 3.11 floor was advertised
  but not actually exercised — running under 3.11 fails at startup
  because of `typing.TypedDict` vs `typing_extensions.TypedDict` under
  pydantic.

  **Migration:** consumers running `uvx mcp-workstate-orchestrator` must
  use Python 3.12 or newer.

### Changed

- **Sibling dependency repinned to `mcp-workstate-handoff>=0.7.0,<0.8.0`.**
  Tracks the 0.7.0 release of `mcp-workstate-handoff`, which carries the
  matching `requires-python` floor.

## [0.2.0] — 2026-04-26

### Breaking

- **Distribution published as `mcp-workstate-orchestrator`.** An earlier
  PyPI name was squatted by an unrelated party; the canonical name aligns
  with the binary name (`mcp-workstate-orchestrator`) and the sibling
  `mcp-workstate-handoff`.

  **Migration:** install via `pip install mcp-workstate-orchestrator` (or
  `mcp-workstate-orchestrator>=0.2.0,<0.3.0` for a range). The console
  script (`mcp-workstate-orchestrator`) and importable module
  (`workstate_orchestrator_mcp`) are unchanged.

### Changed

- Sibling dependency repinned: the previous `workstate-handoff-mcp @
  git+ssh://...@v0.4.3` line is replaced with
  `mcp-workstate-handoff>=0.5.0,<0.6.0` from PyPI. PyPI rejects direct VCS
  deps on upload, so this is a hard pre-release requirement.

## [0.1.4] — 2026-04-24

### Breaking

- **Console script is `mcp-workstate-orchestrator`**, matching the `mcp-*`
  prefix naming convention shared with sibling MCP servers
  (`mcp-workstate-handoff`, etc.). The PyPI/git package name
  (`mcp-workstate-orchestrator`) and the importable Python module
  (`workstate_orchestrator_mcp`) align with it. Consumers must point
  `[mcpServers].*.command` entries in
  MCP client configs (`.mcp.json`, `.vscode/mcp.json`, `.codex/config.toml`),
  release scripts, and any direct subprocess invocations.

### Changed

- `mcp-workstate-handoff` dependency advanced from `v0.4.2` to `v0.4.3`
  to pick up the paired console-script name (`mcp-workstate-handoff`). The
  orchestrator's
  `handoff_integrity_guard` now subprocess-invokes
  `mcp-workstate-handoff` directly.
- `run_doctor` and `run_tools_snapshot` now return
  `{"server": "mcp-workstate-orchestrator"}` to match the new CLI name.
- `argparse` `prog=` and the `doctor` fallback default were updated
  to `mcp-workstate-orchestrator`.

### Migration

- Update the `command` field wherever the server is launched:

  ```diff
  - "command": "workstate-orchestrator-mcp"
  + "command": "mcp-workstate-orchestrator"
  ```

  No Python-level changes are required: `from workstate_orchestrator_mcp import ...`
  keeps working. Pipe the same flags (`--workspace-root`, `serve-stdio`,
  `doctor`) — arg semantics are unchanged.
- Consumers parsing the `server` field of `doctor` / `tools-snapshot`
  output should expect `mcp-workstate-orchestrator` instead of
  `workstate-orchestrator-mcp`.

## [0.1.1] — 2026-04-22

### Added

- `SliceReviewPacket.external_changed_files` field. Decision
  `changed_files` entries with a `<repo_alias>:<path>` prefix (for
  example `mcp-workstate-bootstrap:src/foo.py`) are partitioned out of
  the monorepo-relative `changed_files` list so reviewers operating
  from a monorepo worktree can still resolve the un-prefixed paths.
  External paths are surfaced under their alias in a separate dict
  with the prefix stripped. (WORKSTATE-REF-17-10-BR14-M-02)

### Changed

- `workstate-handoff-mcp` dependency advanced from `v0.1.0` to `v0.4.1`
  to pick up the `run_doctor` soft-fail patch and align with the
  current published consumer install URL.


## [0.1.0] — 2026-04-19

### Added

- **Hoist Agentic System MVP packaging metadata.** `pyproject.toml` now
  declares a `[tool.hoisted]` table for the standalone install surface:
  `git+ssh://git@github.com/darce/mcp-workstate-orchestrator.git@v{version}`.
  The package dependency on `workstate-handoff-mcp` is also pinned to the MVP
  handoff release tag `v0.1.0`.
