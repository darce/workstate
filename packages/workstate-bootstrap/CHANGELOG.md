# Changelog — workstate-bootstrap

## Unreleased

## [0.7.2] — 2026-06-02

> Ships the linked-worktree overlay self-heal (implementation note) **and** its deferred
> follow-ups (implementation note), both of which landed after 0.7.1 was published.

### Added

- **Linked-worktree overlay self-heal (implementation note):** `adopt-worktree` re-runs the
  install materializer against a linked worktree with `clone=<primary>/.workstate/remote`;
  worktree-aware `doctor`/`repair` short-circuit emits a single `unadopted_worktree`
  finding; a managed sentinel-delimited `.gitignore` block keeps an adopted
  worktree's `git status` clean.

### Changed

- **Apply and `--check` share one surface enumeration (implementation note S1,
  `revB-install-private-symbol-coupling`):** new `iter_expected_surface_targets`
  is the single source of the surface/carve/exclusion rule consumed by both the
  materializer and `adopt._compute_drift`, so the drift guard can no longer
  desync from apply; the `_materialize_surfaces_copy` (package-install) path is
  single-sourced through the same helper. `adopt` drops the `importlib` shim for
  explicit imports.
- **Overlay-root resolver prefers a materialized overlay (implementation note S3,
  `revA-overlay-root-unbounded-walk`):** the upward walk skips an unmaterialized
  stray ancestor marker, falling back to the nearest marker so a genuinely
  un-materialized primary still fails loudly.
- **Relocation repoint (implementation note S3b, `revB-relocation-dangling-symlink-no-repoint`):**
  a dangling bootstrap-owned surface link (e.g. a relocated primary) is repointed
  to the live clone via a shared, segment-anchored repoint predicate used by both
  apply and `--check`.

### Notes

- `.claude-plugin/marketplace.json` continues to resolve via the tracked file +
  the adopted `.workstate/generated` symlink; `adopt` does not materialize it
  (implementation note S2 locks this contract). Consumers that gitignore `.claude-plugin`
  need a separate `install` pass.


## [0.7.1] — 2026-06-02

### Fixed

- **Default managed orchestrator pin realigned to
  `mcp-workstate-orchestrator@0.5.1`.** The 0.7.0 coordinated release
  published orchestrator 0.5.1 but left `DEFAULT_MCP_SERVERS` pinned at
  `@0.5.0`, so package-source / default-server installs launched the
  superseded 0.5.0 wheel via `uvx`. The pin (and its two drift-guard test
  assertions) now track 0.5.1; `mcp-workstate-handoff` stays at `@0.12.0`.
  git_overlay installs were unaffected (they run from the cloned source).


## [0.7.0] — 2026-06-01

### Added

- **Package-source overlay delivery (WS-PKG-DELIVERY-01).** `install` /
  `update` can resolve the harness overlay from an installed
  `workstate-system` distribution (`source_kind="package"`) instead of a git
  clone, writing a `package`-kind `BootstrapManifest`. `update` / `repair`
  reject a package-source manifest with a clear error rather than
  dereferencing a `remote_url` that package manifests do not carry.

### Changed

- **MCP launch decoupled from resolution (implementation note Theme A,
  WS-MCP-LAUNCH-01).** Generated serve commands now use `uv run --no-sync`
  and the server venvs are pre-built at install time
  (`_presync_local_mcp_envs`), eliminating the cold-start re-sync race that
  overran the harness's 30s MCP connection timeout and registered zero tools.
  The `--no-sync` invariant is enforced at the shared render seam
  (`_canonicalize_managed_servers`), so `install` / `update` / `repair` /
  `mcp-sync` all rewrite an older launcher; the earlier preserve-path
  `_normalize_local_mcp_server_specs` patch is retired.

### Removed

- **Runtime-path migration shim retired (implementation note Slice D cutover complete).**
  The legacy-runtime-tree migration helpers (`migrate_runtime_paths()`,
  `plan_runtime_path_migration()`) and the `legacy_runtime_path` `doctor` check
  are removed. `.workstate/` / `docs/workstate/` are the sole runtime paths with
  no backward compatibility — a consumer still carrying the pre-rebrand runtime
  tree must reinstall cleanly.

## [0.6.0] — 2026-05-30

### Changed

- **MCP server identity cutover (implementation note Slice B).** Default managed servers
  register under the canonical `workstate-handoff-mcp` /
  `workstate-orchestrator-mcp` names. `LEGACY_MCP_SERVER_RENAMES` +
  `_legacy_prune_for()` dedup and forward-rewrite a stale `agent-*-mcp`
  registration across all three config surfaces (`.mcp.json`,
  `.vscode/mcp.json`, `.codex/config.toml`) so a re-install collapses the
  duplicate to one canonical entry — fixing the "MCP servers not loading"
  symptom caused by deep-merge preserving both old and new entries.
- **Default managed server pins bumped** to `mcp-workstate-handoff@0.12.0` and
  `mcp-workstate-orchestrator@0.5.0`. `workstate-protocol` floor raised to
  `>=0.1.6`.

### Added

- **Runtime path migration `.workstate/` → `.workstate/` and
  `docs/workstate/` → `docs/workstate/` (implementation note Slice D).**
  `migrate_runtime_paths()` / `plan_runtime_path_migration()` run on
  `install`/`update`: idempotent, archive-backed (a both-present collision
  moves the legacy tree aside rather than overwriting), dry-run-capable, and
  routed through the shared `workstate_protocol.paths` constants. `doctor`
  flags a surviving legacy tree as `legacy_runtime_path`.

## [0.5.2] — 2026-05-20

### Changed

- **Bump the default managed handoff server pin** to
  `mcp-workstate-handoff@0.11.5` so fresh `workstate-bootstrap install`
  dogfood installs pick up the `ACTIVE TASK PLANS` task-plan-path fix by
  default. `mcp-workstate-orchestrator` remains pinned at `0.4.7`.

## [0.5.1] — 2026-05-11

### Fixed

- **WORKSTATE-REF-57 — stale shared-surface symlinks now repointed on rerun.**
  When a consumer was installed pre-v0.2.0 (legacy root layout
  `<clone>/<surface>`) and the layout subsequently moved into
  `<clone>/packages/workstate-system/<surface>`, the target-side
  `scripts/hooks -> ../.workstate/remote/scripts/hooks` symlink survived
  every subsequent `workstate-bootstrap install` because the pre-fix
  `points_into_clone` check still passed lexically for the broken
  resolved path. `_materialize_surfaces` now classifies each existing
  symlink into three buckets — resolves-to-expected (shared,
  idempotent), lexically inside our remote subtree but stale-or-broken
  (repointed to the canonical target, audited to stdout as
  `repointed: <surface>`), and foreign (preserved as local override).
  The lexical containment check uses `os.readlink` +
  `os.path.normpath` so broken targets are classified without raising
  during `Path.resolve(strict=True)`.

## [0.5.0] — 2026-05-10

### Changed

- **WORKSTATE-REF-56 — cross-harness install manifest is now the single source of
  truth for hook adapter wiring.** `config/agent-workflows/portable_commands.json`
  schema v2 introduces a top-level `hooks[]` array; install dispatches
  adapter rows through a manifest-driven walker (closed-set operation
  table) instead of bespoke per-harness writers. New harnesses (Codex,
  VS Code, …) are onboarded by appending adapter rows.
- **CLI default profile flips back to `all`** so a no-argument
  `workstate-bootstrap install` materializes the full surface set out of
  the box — per-agent generated surfaces (`.claude/skills`,
  `.claude/commands`, `.github/prompts`, `.codex/skills`), shared
  overlay symlinks (`scripts/hooks`, `Makefile.d`, `scripts/workstate`,
  …), and the lifecycle hoist (`Makefile.d/lifecycle.mk` plus the
  sentinel-bracketed `-include` block). `--profile minimal` and
  `--profile lifecycle` remain opt-in for lean installs. The library
  `install()` API has always defaulted to `"all"`; this realigns the
  two layers.
- **The install manifest (`.workstate-bootstrap.json`) now records the
  active profile** under `manifest["profile"]` so downstream tools
  (`sync`, `doctor`, rehearsals) can reason about what the consumer
  installed without re-inferring it from the surface set.
- **Stop-hook wiring is fully opt-in across every harness shipped by
  the manifest.** The legacy `--harness-hook-scope` flag is replaced by
  four boolean flags: `--install-claude-stop-hook` (shared, checked-in
  `.claude/settings.json`), `--install-claude-stop-hook-local`
  (user-owned, gitignored `.claude/settings.local.json`),
  `--install-codex-stop-hook` (`.codex/hooks/stop.json`), and
  `--install-vscode-stop-hook` (`.vscode/agentic-stop-hooks.json`).
  All four default off — no file is touched unless the operator
  explicitly opts in. The library `install()` API surfaces the same
  switches as `install_claude_stop_hook` / `install_claude_stop_hook_local`
  / `install_codex_stop_hook` / `install_vscode_stop_hook`.

## [0.4.2] — 2026-05-10

### Changed

- **Bump default managed MCP server pins** to `mcp-workstate-handoff@0.11.1`
  and `mcp-workstate-orchestrator@0.4.5` so consumer repos pick up the
  WORKSTATE-REF-54 (multi-active CURRENT_TASK projection, import/export
  malformed-payload rejection, target_branch/worktree_path/plan_path
  preservation) and WORKSTATE-REF-55 (compaction env-var namespace
  consolidation under `AGENT_HANDOFF_COMPACTION_*` with `WORKSTATE_*` kept
  as a deprecated alias) fixes by default.

## [0.4.1] — 2026-05-09

### Fixed

- **`--profile all` now performs the lifecycle hoist** (WORKSTATE-REF-48). The
  legacy default profile shipped lifecycle-referencing skills
  (`branch-lifecycle`, `tdd`, `incremental-implementation`,
  `branch-review`, `handoff-lifecycle`) but did not install the
  matching `Makefile.d/lifecycle.mk` and
  `scripts/workstate/lifecycle/` runner that those skills' `make
  task-start` / `make slice-start` / `make context` /
  `make review-ready` / `make handoff-close-check` /
  `make format-all` references resolve through. Consumer repos
  bootstrapped under `--profile all` now receive the runner and the
  sentinel-bracketed `-include Makefile.d/*.mk` directive, so the
  skill manifest and the make graph stay aligned.

### Changed

- **Default managed MCP servers are now version-pinned.** The built-in
  `--mcp-servers default` map writes `uvx mcp-workstate-handoff@0.11.0` and
  `uvx mcp-workstate-orchestrator@0.4.4` (rather than unpinned `uvx
  mcp-workstate-handoff` / `uvx mcp-workstate-orchestrator`) so consumer repos
  do not silently drift when PyPI advances independently of the overlay
  tag they bootstrapped against. Operators wanting a different pin
  continue to pass `--mcp-servers <path>`.
- **Raise `workstate-protocol` lower bound to `>=0.1.4,<0.2.0`** to match
  the floor pinned by `mcp-workstate-handoff` 0.11.0 and
  `mcp-workstate-orchestrator` 0.4.4 — the two packages bootstrap launches
  via `uvx` — so the bootstrap venv cannot resolve a protocol release
  older than what those servers import at startup.

## [0.4.0] — 2026-05-04

### Added

- **Install profile contract: `--profile {minimal,lifecycle,all}`**
  (implementation note / WORKSTATE-REF-40 implementation note.5.a). The CLI now accepts an explicit
  install profile flag and honors it across the manifest layers, so
  consumer repos can pick a smaller hoist surface than the default.
- **Hoist `Makefile.d/plans.mk` + `git-plan-cat.sh` stub** (implementation note /
  WORKSTATE-REF-38 implementation note). Consumer repos installed via `workstate-bootstrap`
  pick up `make plan-show / plan-edit / plans-list / plan-register`
  out of the box; the targets shell out to `uvx mcp-workstate-handoff`
  under the hood.

### Changed

- Bootstrap installs and rehearsals are validated against
  `mcp-workstate-handoff>=0.8.0` (the version that ships the
  `plan_resolve` / `plan_cli` surface targeted by the new make
  recipes).

## [0.3.1] — 2026-05-03

- **implementation note BR-01 — raise `workstate-protocol` lower bound to
  `>=0.1.2,<0.2.0`.** Bootstrap's default install path invokes
  `uvx mcp-workstate-handoff`, which imports `workstate_protocol.branch_naming`
  at startup; the previous `>=0.1.0` floor let `uvx` resolve a protocol
  release missing the module, crashing init-state on a fresh install. A
  new packaging test (`tests/test_package_metadata.py`) pins the floor
  so the declaration cannot silently drift back below the contract.
- **implementation note implementation note — install rehearsal pins six-hook surface +
  helper materialization.** `SHARED_GIT_HOOK_NAMES` now includes
  `pre-commit` (in addition to `post-checkout`, `post-commit`,
  `post-merge`, `post-rewrite`, `pre-push`); the install rehearsal
  test asserts that `core.hooksPath` resolves to the directory
  carrying all six executable hook scripts AND that the Python helper
  `scripts/hooks/check_branch_naming.py` (the delegate the
  post-checkout / pre-commit / pre-push hooks `exec`) is materialized
  alongside them. Without the helper, every branch-naming gate
  silently no-ops. The shared surface itself is unchanged
  (`scripts/hooks` is symlinked from `.workstate/remote`); the new
  assertion catches a future regression where the upstream package
  drops the helper.
- **Manifest renamed: `.workstate-overlay.json` → `.workstate-bootstrap.json`
  (schema_version bumped to 2).** Resolves a name collision with consumer
  repos that also use `.workstate-overlay.json` for unrelated config. On first
  run, an existing `.workstate-overlay.json` carrying the bootstrap shape
  (top-level dict with a list `surfaces` key) is renamed in-place via
  `git mv` (or filesystem rename when the target is not a git worktree).
  Consumer-owned files at the legacy name are left untouched. The Python
  alias `OVERLAY_MANIFEST_NAME` is preserved (now pointing at the canonical
  filename) for downstream import compatibility.
- **Managed MCP defaults now launch stdio servers.** The built-in
  `--mcp-servers default` map writes `uvx mcp-workstate-handoff
  --workspace-root . serve-stdio` and `uvx mcp-workstate-orchestrator
  --workspace-root . serve-stdio` into `.mcp.json`,
  `.vscode/mcp.json`, and `.codex/config.toml`, so external clients
  start runnable MCP servers from a fresh install or update.
- **`regenerate-task-views` harness hook contract dropped (implementation note).**
  Bootstrap no longer materializes any Claude / VS Code / Codex hook
  wiring that invokes `regenerate-task-views`; `DASHBOARD.txt` is now
  auto-regenerated server-side inside `mcp-workstate-handoff` on every
  state-mutating MCP call. The shared `scripts/hooks/` surface is still
  materialized; `regenerate-task-views.sh` remains there only as a
  documented manual fallback for the auto-regen opt-out path
  (`WORKSTATE_HANDOFF_DASHBOARD_AUTO_REGEN=0`). Operators upgrading from
  0.3.0 do not need to touch any harness config — the next
  `workstate-bootstrap update` removes the obsolete hook wiring.

## 0.3.0 — 2026-04-28

- **Install-time state provisioning (implementation note).** `install` now runs
  the handoff server's `init-state` after surface/config materialization
  but before `core.hooksPath` is set, so a fresh install ends with a
  schema-current `.task-state/handoff.db` and `.task-state/exports/`
  ready for the first MCP call. The init-state invocation is resolved
  from the same `mcp_servers` map written into `.mcp.json` /
  `.vscode/mcp.json` / `.codex/config.toml` (avoiding dogfood version
  skew between PyPI and local-source schemas), and the bootstrap
  manifest's `remote_url` is threaded through `--expected-remote-url`
  so a stale adjacent overlay from a different remote is rejected.
  Skipped under `--no-mcp-servers`.
- **Required-surfaces refusal runs before the generator and
  init-state.** A failing required-surface check now leaves no
  `.task-state/`, no generated artifacts, and no manifest behind on
  disk, so refused installs no longer half-write the target.
- **`status` reports handoff state.** When the install registered MCP
  servers, `status` invokes `init-state --check` and appends the
  resolved `state_dir` / `db_path` / `exports_dir` / `schema_version` /
  `initialized` to the summary. `--no-mcp-servers` installs suppress
  the section.
- **`doctor` flags missing `.task-state/handoff.db` as `state_drift`,**
  gated on `.mcp.json` being present in the manifest's `configs` array
  so config-only installs (`--no-mcp-servers`) do not produce
  false-positive drift.
- **`switch_task` cold-start fix.** implementation note implementation note (in
  `mcp-workstate-handoff` 0.5.0+) drops `BranchMismatchError` from
  `switch_task`; the cold-start cycle (register task → `switch_task`
  → first content write) now completes from any branch. Branch
  enforcement on content writes (`record_event`, `close_slice`,
  `set_handoff_state`, `record_review_finding`,
  `record_verified_test`) is unchanged — bootstrap's docs reflect
  this, but the behavior change lives in the handoff package.

## 0.2.1 — 2026-04-26

- Add `pyyaml>=6` to runtime dependencies. The agent-workflow generator
  imports `yaml` to read `skill.yaml`; bootstrap invokes the generator
  via its own `sys.executable`, so PyYAML must be present in bootstrap's
  uvx-isolated venv. Without it, install fails with
  `PyYAML is required to read skill.yaml` on a clean uvx run.

## 0.2.0 — 2026-04-26

- Resolve shared overlay surfaces and the agent-workflow generator under
  `packages/workstate-system/` (the workstate monorepo layout) with
  fallback to the clone root for legacy hoisted overlays. Fixes
  `BootstrapManifestValidationError: required surface 'scripts/hooks' was
  not materialized` against monorepo refs at or after implementation note step 1.
- Rehearsal fixture (`fake_remote_with_generator`) now mirrors the real
  monorepo layout so this regression cannot return silently.

### Note on previously published refs

The monorepo `v0.1.0` tag pre-dates this fix. Consumers using
`uvx --from "...@v0.1.0..." workstate-bootstrap install ...` will hit the
required-surface error. Pin to `v0.1.1` or later.
