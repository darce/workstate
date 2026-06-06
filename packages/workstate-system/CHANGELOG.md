# Changelog

All notable changes to `workstate-system` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

## [0.2.2] — 2026-06-06

### Added

- Ship referenced rules docs canonical in payload rules
  (planning-review-guide + 4 companion docs) with payload-wide
  link-resolution and byte-equality drift guards (WS-RULES-SHIP-01).

### Changed

- Payload `mcp_servers.yaml` pins bumped: `mcp-workstate-handoff@0.12.4`,
  `mcp-workstate-orchestrator[bridge]@0.6.1`.
- Render coherence + harness sync gates; reinject budget floor and fence
  sanitization (WS-REINJ-01 review fixes).


## [0.2.1] — 2026-06-04

### Fixed

- Bash main-branch guard (implementation note, BR-17): separator-aware effective-cwd
  tracking (`cd` propagates through `&&`/`;` only — `|`/`||`/`&` degrade to
  unknown fail-closed), so the prescribed cd-into-worktree fallback pattern is
  no longer false-blocked; `git -C <dir>` global options parsed before
  subcommand detection (closes an invisible-stage bypass); Python `-c` inline
  write targets resolved against the stage's effective cwd.

### Changed

- Bypass env var renamed to `WORKSTATE_ALLOW_BASH_MAIN_WRITE` (legacy `ALT_*`
  honored with a deprecation warning); inline leading-assignment bypass now
  parsed (first stage only); every bypass writes a jsonl audit record
  (env|inline source). Managed MCP server pin moved to
  `mcp-workstate-handoff@0.12.3`.


## [0.2.0] — 2026-06-04

### Added

- Payload `generate_agent_workflows.py` composes the always-effective plugin
  tree (`effective/{claude,codex}`) from base plus consumer overrides, and
  propagates manifest `global_instructions` to every harness surface
  (Claude, VS Code, Codex adapters).
- `plugins.mk` targets for effective-tree composition and pin checks.

### Changed

- Plugin distribution docs and managed MCP server pins updated
  (`mcp-workstate-handoff@0.12.3`, `mcp-workstate-orchestrator@0.6.0`);
  marketplace pins documented against the effective tree.


## [0.1.4] — 2026-06-03

### Fixed

- **Claude Stop-hook adapters now emit Claude-valid entries.** The
  claude-code compact-session adapters in `portable_commands.json` used a
  flat `{"_managed_by", "command"}` entry that Claude Code silently
  ignores; they now emit the required nested
  `{"matcher": "", "hooks": [{"type": "command", ...}]}` shape, and the
  canonical command is `python3 "$CLAUDE_PROJECT_DIR/scripts/hooks/compact-session.py"`
  so fresh installs match (and no longer report drift against) the form
  working consumers already carry. `generate_agent_workflows.py` validates
  the nested shape for claude-code adapters (and the flat `command` for
  codex/vscode), lifecycle `doctor` now flags a stale flat managed Claude
  entry as drift with a re-install remediation instead of reporting it
  healthy, and the bootstrap walker's flat→nested upgrade path is covered
  by a migration test.


## [0.1.3] — 2026-06-03

### Changed

- implementation note S4 release-pipeline hardening: preflight distinguishes
  unverifiable from missing artifacts, the publish gate locks the accepted
  release-state set, and `pypi_without_tag` reconciliation is
  provenance-verified with bidirectional byte-parity.
- Release publishing moved from local `twine upload` to
  `gh workflow run` (PyPI Trusted Publishing).
- `make dogfood` gained `DOGFOOD_SOURCE=package` to rehearse the
  package-source install path; managed MCP-server pins bumped for the
  post-Plan-0020-S4 releases.


## [0.1.2] — 2026-06-02

### Added

- `task-start` now adopts the bootstrap overlay into a freshly created linked
  worktree (implementation note S3 durable self-heal trigger), and its gate walks upward
  for a *materialized* overlay so nested-source layouts self-heal (implementation note S4,
  `revC-nested-source-marker-gate-mismatch`); the monorepo self-host (tracked
  marker, no clone) still skips without spawning a doomed subprocess.

### Changed

- implementation note upstream asks D/E/G: `lifecycle.mk tasks-gc` → `archive --operation
  gc`; `check-agent-workflows` now validates the Codex router block; git hooks
  resolve guards via `GUARD_DIR` rather than `$REPO_ROOT`.
- Regenerated the per-harness workflow adapters so the Claude/Codex/VS Code
  surfaces match the manifest (review-parallel prompt drift).


## [0.1.1] — 2026-06-02

### Fixed

- **Plugin-emission orchestrator pin realigned to
  `mcp-workstate-orchestrator@0.5.1`.**
  `config/agent-workflows/mcp_servers.yaml` — the hand-maintained source for
  the emitted Claude/Codex plugin `.mcp.json` server maps — still pinned
  `@0.5.0` after the coordinated release published orchestrator 0.5.1, so the
  emitted plugin trees launched the superseded wheel. The pin, its two
  drift-guard tests, and the plugin-distribution doc + ADR-001 references now
  track 0.5.1; `mcp-workstate-handoff` stays at `@0.12.0`.


### Added

- **implementation note — feature-branch naming enforcement.** Four-layer gate
  uniformly classifies every branch as protected
  (`main`/`master`/`release/*`/`hotfix/*`), conforming
  (matches `workstate_protocol.branch_naming.TASK_REF_RE`), or
  non-conforming. `scripts/hooks/check_branch_naming.py` is the single
  delegate invoked by post-checkout (warn), pre-commit (block + audited
  override), and pre-push (block + distinct override). The PreToolUse
  guards in `.github/hooks/guard-main-branch.py` and
  `scripts/hooks/_guard_main_branch_inline.py` import the same
  validator (`check_branch_naming` in `_branch_isolation_guard`) so a
  grammar tweak in `workstate_protocol.branch_naming` updates every gate
  with no other code change. Override env vars:
  `AGENTIC_ALLOW_NONCONFORMING_BRANCH=1` (commit side; reason via
  `AGENTIC_ALLOW_NONCONFORMING_BRANCH_REASON`) and
  `AGENTIC_ALLOW_NONCONFORMING_BRANCH_PUSH=1` (push side; reason via
  `AGENTIC_ALLOW_NONCONFORMING_BRANCH_PUSH_REASON`) — distinct so
  commit-side leniency cannot silently leak across the publish
  boundary. Both override paths record an audited `decision` event
  (sessions `branch_naming_override` and `branch_naming_push_override`)
  with a 2 s daemon-thread wall-clock budget; on timeout/error the
  override is still honored and the failure is appended to
  `.task-state/branch_naming_overrides.log` /
  `.task-state/branch_naming_push_overrides.log` (override never
  blocks).

### Changed

- `$branch-review` now records branch-diff review runs with
  `subject_kind="branch"` and explicitly forbids direct writes to
  `.task-state/handoff.db`; when MCP tools are unavailable, agents
  should use the `mcp-workstate-handoff` CLI wrapper or stop with a
  blocker. Generator round-trip coverage pins this guidance in both
  `.claude/skills` and `.codex/skills`.
- Handoff and review skills now require final responses to print MCP
  write receipts with row ids (`decision id`, `test_result id`,
  `review_run id`, finding counts, and dashboard refresh status) so
  context compaction can recover exact handoff provenance.
- Public skill and review-rule guidance now uses repo-local manifests,
  placeholder paths, and stack-level commands instead of consumer
  installation paths or lane names.

### Removed

- Removed the final consumer-specific conflict/sync contract from the
  hoisted contract surface; the current packaged contract set is the
  six agent/agentic contracts only.

## [0.2.0] — 2026-04-22

### Added

- Eleven previously empty skill folders are now populated with their
  full `SKILL.md` (commit2git, daemon-lifecycle, document-sync,
  investigate, refactor, rescue-lane, review, security-audit,
  subfeature-committer, worktree-orchestrator, worktree-worker). The
  skill catalog is now 21 skills, all with anatomy-validator-passing
  `SKILL.md` files.
- `.claude/commands/` and `.github/prompts/` are now hoisted, providing
  the eleven managed portable-command adapters
  (`auto-fix`, `branch-lifecycle`, `branch-review`,
  `handoff-lifecycle`, `incremental-implementation`, `plan-analyze`,
  `planning-review`, `review-parallel`, `review`, `scope`, `tdd`).
- `config/lane-orchestration/` lane configuration surface.
- `docs/workstate/templates/` shared planning/review/decision templates.
- `scripts/lint_hoisted_paths.py` portability linter plus its
  `scripts/overlay_resolver.py` dependency.
- Root `.gitignore` excluding `__pycache__/`, `*.pyc`, and `.DS_Store`.

### Changed

- `scripts/hooks/filter-test-output.py` and its test refresh.
- `.claude/skills/plan-analyze/SKILL.md` and
  `.claude/skills/planning-review/SKILL.md` updated to reference
  `docs/workstate/templates/TASK_PLAN.template.md` (now hoisted).
- `docs/workstate/contracts/repo-intel-mcp-candidates.md` content
  refresh.

### Removed (contract split)

- Eight alt-context-monorepo-specific contracts moved out of the
  hoisted surface so `workstate-system` carries only agent/agentic
  contracts:
  - `cluster-delta-api.md`
  - `cluster-snapshot-api.md`
  - `clustering-api.md`
  - `curation-sync-api.md`
  - `recognition-clustering.md`
  - `recognition-media-xmp-mapping.md`
  - `security.md`
  - `suggestion-extensions-api.md`

  The seven remaining contracts are: `workstate-handoff-mcp.md`,
  `workstate-orchestrator-mcp.md`,
  `conflict-resolution-sync-contract.md`, `harness-protocol.yaml`,
  `overlay-manifest.yaml`, `repo-intel-mcp-candidates.md`,
  `subagent-bridge-interface-note.md`.

### Verification

- `python3 scripts/lint_hoisted_paths.py --repo-root .` → `lint-hoisted-paths: PASS`.
- `find .claude/skills -maxdepth 2 -name SKILL.md | wc -l` → `21`.
- `ls docs/workstate/contracts | wc -l` → `7`.

## [0.1.0] — 2026-04-21

### Added

- Initial extraction from `upstream-harness-repo` HEAD
  `841b8fb2e080f54e5e47b99cd911c254fb61c248` on branch
  `feature/e17-10`.
