# Changelog

All notable changes to `workstate-system` are recorded here. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## Unreleased

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
- `docs/agentic/templates/` shared planning/review/decision templates.
- `scripts/lint_hoisted_paths.py` portability linter plus its
  `scripts/overlay_resolver.py` dependency.
- Root `.gitignore` excluding `__pycache__/`, `*.pyc`, and `.DS_Store`.

### Changed

- `scripts/hooks/filter-test-output.py` and its test refresh.
- `.claude/skills/plan-analyze/SKILL.md` and
  `.claude/skills/planning-review/SKILL.md` updated to reference
  `docs/agentic/templates/TASK_PLAN.template.md` (now hoisted).
- `docs/agentic/contracts/repo-intel-mcp-candidates.md` content
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
- `ls docs/agentic/contracts | wc -l` → `7`.

## [0.1.0] — 2026-04-21

### Added

- Initial extraction from `context-alt-text-monorepo` HEAD
  `841b8fb2e080f54e5e47b99cd911c254fb61c248` on branch
  `feature/e17-10`.
