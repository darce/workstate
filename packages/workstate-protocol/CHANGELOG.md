# Changelog

All notable changes to `workstate-protocol` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This package is the single source of truth for cross-repo contracts in
the Workstate system. Symbols listed in `__all__` form the documented
public surface; downstream consumers (`workstate_handoff_mcp`,
`workstate_orchestrator_mcp`, `workstate-bootstrap`, `workstate-system`) MUST
re-export by reference rather than copying literals so a single edit
propagates everywhere.

## Unreleased

## [0.2.5] — 2026-06-07

### Fixed

- Re-cut of 0.2.4, whose published wheel was corrupted by the public-export
  scrub: the case-insensitive inline prefix regex matched `WORKSTATE-REF` inside
  identifiers and renamed `BranchClassification` to
  `BranWORKSTATElassification` in `branch_naming`. No source changes; the
  export scrub now letter-bounds its prefixes.


## [0.2.4] — 2026-06-07

### Changed

- Re-cut of the unpublished 0.2.3 with the runtime `__version__` string synced
  to the package version. No code or contract changes.


## [0.2.3] — 2026-06-07

### Changed

- `StructuredSummary.harness` literal gains `grok` (WORKSTATE-REF-09 harness parity
  with the canonical compaction-contract harness list).


## [0.2.2] — 2026-06-06

### Added

- `BootstrapManifest` stack provenance fields (`stack_distribution`,
  `stack_version`, `stack_members`) and a package-source update path
  with `--remote-ref` optional (validated post-manifest-load).


## [0.2.1] — 2026-06-04

### Changed

- Re-cut of the unreleased 0.2.0 with the runtime `__version__` string synced
  to the package version. No code or contract changes.


## [0.2.0] — 2026-06-04

### Added

- **Durable consumer recipe overrides (WORKSTATE-REF-07):** new `plugin-override-manifest.json`
  and `plugin-override-lock.json` schemas plus expanded `bootstrap-manifest.json`
  fields covering the override root, effective plugin tree, and
  `global_instructions` propagation.
- `bootstrap.py` helpers for resolving and validating override manifests/locks
  used by `workstate-bootstrap` composition and `overrides` subcommands.


## [0.1.7] — 2026-06-01

### Added

- **`BootstrapManifest` gains a `source_kind` discriminator
  (WS-PKG-DELIVERY-01).** New `source_kind: "git_overlay" | "package"` field
  (default `"git_overlay"`) plus `package_version`, with `remote_url` /
  `remote_ref` / `remote_sha` now optional. A `_check_source_provenance`
  validator enforces the git triple for `git_overlay` and `package_version`
  for `package`. The `git_overlay` default means manifests written before
  this release validate unchanged.

### Removed

- **Legacy runtime-path symbols retired (implementation note Slice D cutover complete).**
  `LEGACY_RUNTIME_ROOT_DIRNAME`, `LEGACY_DOCS_MIRROR_DIR`, and
  `RUNTIME_PATH_RENAMES` are removed from the public surface. `.workstate/`
  (`RUNTIME_ROOT_DIRNAME`) and `docs/workstate/` (`DOCS_MIRROR_DIR`) are
  canonical with no migration shim.

## [0.1.6] — 2026-05-30

### Added

- **`workstate_protocol.paths` — single source of truth for the runtime root
  and docs mirror (implementation note Slice D).** New module exporting
  `RUNTIME_ROOT_DIRNAME` (`.workstate`), `DOCS_MIRROR_DIR` (`docs/workstate`),
  their `LEGACY_*` counterparts (`.agentic` / `docs/workstate`),
  `RUNTIME_PATH_RENAMES`, `CONTRACTS_DIR`, `RULES_DIR`,
  `HARNESS_CONTRACT_RELPATH`, `INSTRUCTIONS_RELPATH`, and the
  `docs_mirror_path()` / `runtime_root_path()` helpers. Consumers
  (`workstate_handoff_mcp`, `workstate_orchestrator_mcp`,
  `workstate-bootstrap`) now resolve these names by reference so a future
  path change is a one-line flip here, not a repo-wide sweep. Purely
  additive — no existing symbol changed.

### Notes

- Coordinated rebrand release with `mcp-workstate-handoff` 0.12.0,
  `mcp-workstate-orchestrator` 0.5.0, and `workstate-bootstrap` 0.6.0.

## [0.1.5] — 2026-05-10

### Changed

- **Coordinated release with `mcp-workstate-handoff` 0.11.1,
  `mcp-workstate-orchestrator` 0.4.5, and `workstate-bootstrap` 0.4.2** to
  ship WORKSTATE-REF-54 (multi-active CURRENT_TASK projection, malformed
  import-payload rejection, target_branch/worktree_path/plan_path
  preservation) and WORKSTATE-REF-55 (compaction env-var namespace
  consolidation under `AGENT_HANDOFF_COMPACTION_*`, with `WORKSTATE_*`
  retained as a deprecated alias). No `workstate_protocol` surface
  symbols changed; this version is the floor pin reaffirmed by the
  downstream package bumps.

## [0.1.4] — 2026-05-08

### Added

- **Branch-grammar registry** (implementation note implementation note) under
  `workstate_protocol.branch_naming`. Documents the canonical task-branch
  grammar and prefix vocabulary (`feature/`, `maint/`, `release/`,
  etc.) so downstream packages enforce branch-name validation against a
  single source of truth instead of duplicating string literals.
  Existing consumer pins (`workstate-protocol>=0.1.2,<0.2.0`,
  `>=0.1.0,<0.2.0`) remain compatible; this release is strictly
  additive.

## [0.1.3] — 2026-05-04

### Changed

- Documentation refresh and minor packaging maintenance to support the
  WORKSTATE-REF-37 / WORKSTATE-REF-38 / WORKSTATE-REF-40 release wave. No public-symbol or
  schema changes; existing consumer pins (`workstate-protocol>=0.1.2,<0.2.0`)
  remain compatible.

## [0.1.2] — 2026-05-03

### Added

- **`workstate_protocol.branch_naming` is now a documented public
  module** (implementation note implementation note). Exports:
  - `TASK_REF_RE` — canonical regex describing the conforming
    feature-branch grammar (`feature/<task-ref>-<slug>`, lowercase,
    must contain at least one digit). The four-layer naming gate
    (post-checkout warn, PreToolUse block, pre-commit block, pre-push
    block) imports this regex by reference; a grammar tweak here
    propagates to every gate with no other code change required.
  - `derive_task_ref_candidates(branch_name)` — yields every
    digit-bearing prefix from longest to shortest (used by the
    "did you mean" suggestion in the post-checkout warn).
  - `format_suggested_branch_name(task_ref)` — render a conforming
    branch name from a registered task ref.
- The README declares `branch_naming` as a ✅ v0.1.0 schema row so
  external consumers can pin against the published surface.
