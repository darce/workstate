# workstate

Workstate is a portable harness that turns any code agent — Claude Code, Codex,
or anything else that speaks MCP — into a disciplined collaborator
with persistent task state, branch isolation, and adversarial
multi-agent review built in.

This monorepo houses the five packages that make up the Workstate
harness — typed protocol contracts, two MCP servers, the bootstrap
installer, and the shared skill / hook content — under `packages/`
so cross-cutting changes ship atomically.

External target repos consume the published artifacts produced from
this monorepo (the `workstate-bootstrap` CLI, the PyPI packages, and
the canonical `vX.Y.Z` distribution tag), not its source.

## What it does

A code agent in isolation is an amnesiac: it does not remember what
it shipped yesterday, who reviewed it, which slice is half-done, or
why a previous attempt was rejected. Each new session starts cold.

Workstate fixes that by externalising the parts of a
software-engineering workflow that should not live inside any single
agent's context window:

- **Two MCP servers** persist task state, decisions, review findings,
  branch lifecycle, lane activity, and worker reports outside the
  agent.
- **Skills** define the rituals an agent follows — scoping a request,
  planning, reviewing a plan, opening a branch, running TDD slices,
  reviewing a branch, closing it cleanly.
- **Generated workflow adapters** ship the same skills to Claude
  Code, Codex, and VS Code so every agent sees an identical command
  surface (`/scope`, `/plan-analyze`, `/planning-review`,
  `/branch-lifecycle`, `/incremental-implementation`, `/tdd`,
  `/branch-review`, `/auto-fix`).
- **Hooks** wired through `core.hooksPath` enforce branch isolation,
  formatting, and close-check gates at the git layer — agents cannot
  skip them by editing settings.

The result is a workflow where the agent is a stateless executor and
Workstate is the durable substrate.

## Problems it solves

| Problem | How the harness fixes it |
| --- | --- |
| Agent forgets context between sessions | `load_session` reconstitutes objective, status, open findings, touched files, and slice history from `.task-state/handoff.db`. |
| Edits land directly on `main` | `make task-start` creates a feature branch and linked worktree; pre-commit hooks refuse code edits on `main`. |
| Half-finished plans, "I'll address that later" | `/plan-analyze` and `/planning-review` produce findings that must be closed before `make review-ready` passes. |
| Reviews disappear into chat history | Findings, verdicts, and review runs are recorded in handoff state and surface in every subsequent `load_session`. |
| Tasks marked done without evidence | `make handoff-close-check` enforces that every slice closed with a recorded decision and that no findings remain open. |
| Two agents trample each other | Worktree lanes register in the orchestrator; the dashboard surfaces who owns what. |
| Switching from Claude to Codex (or vice versa) loses state | All state lives in MCP — any agent that can reach the servers picks up exactly where the last one left off. |

## How adversarial review works

The harness treats review as a first-class artifact, not a chat
exchange, and is built to let **different agents review each other's
work**:

1. The authoring agent drafts a plan or opens a branch and records
   intent via `set_handoff_state` and `record_event`.
2. A reviewing agent — typically a different model family, sometimes
   on a separate worktree lane — runs `/plan-analyze`,
   `/planning-review`, or `/branch-review`. Each skill is a
   reviewer-side script with explicit verdict semantics.
3. Findings land in `review_findings` with severity, status, and a
   stable id. Verdicts land as decisions. Both are tied to the task
   row and survive the reviewer's session.
4. The authoring agent receives the findings on its next
   `load_session` and must close them before `make review-ready`
   will let the branch proceed.
5. `make handoff-close-check` runs on the final HEAD as the
   merge-readiness gate, refusing close if any finding is still open
   or any slice lacks a recorded decision.

Because the reviewer and the author are decoupled through MCP
state, two adversarial passes from independent models — for example,
Claude Opus reviewing a plan that Codex authored, then GPT-5
reviewing Claude's implementation — compose without either agent
needing to trust the other's chat transcript.

## Agent-agnostic handoff

Every primitive an agent needs is exposed via MCP and named
identically across agent surfaces:

- **State**: `load_session`, `get_handoff_state`, `set_handoff_state`,
  `switch_task`, `archive`.
- **Decisions and events**: `record_event`, `close_slice`,
  `review_findings`, `review_runs`.
- **Lifecycle gates**: `integrity_check`, `validate`, `compaction`.
- **Lane coordination**: `manage_worktree_lane`,
  `dispatch_lane_work`, `lane_communication`, `worker_reports`.

A Claude Code session and a Codex session pointed at the same
workspace see the same task rows, the same findings, and the same
dashboard. `make context` is the single entry point both honour.

The same applies inside a single session: if compaction kicks in
mid-task, the post-compaction conversation reloads from MCP rather
than relying on a summarised transcript.

## Consumer entry point

Install the workstate-system overlay into a target repo with a single
command:

```bash
uvx --from "git+https://github.com/darce/workstate@v0.1.9#subdirectory=packages/workstate-bootstrap" \
    workstate-bootstrap install \
    --target /path/to/your/repo
```

That clones the monorepo at the requested ref, materialises the
overlay surfaces (`.github/prompts/`, the Claude/Codex plugin trees,
`scripts/hooks/`, `.github/hooks/`, `docs/agentic/contracts/`,
`docs/agentic/rules/`, `Makefile.d/`, and `scripts/workstate/`), registers
both managed MCP servers (`mcp-workstate-handoff`, `mcp-workstate-orchestrator`)
via `uvx`, and sets `core.hooksPath` so `git status` runs the harness
hooks.

See [`docs/CONSUMER.md`](docs/CONSUMER.md) for upgrade, drift
detection, MCP-server overrides, and skill-override workflow.

If your target previously consumed any of `darce/mcp-workstate-handoff`,
`darce/mcp-workstate-orchestrator`, `darce/workstate-system`, or
`darce/workstate-bootstrap` directly, see
[`docs/UPGRADING.md`](docs/UPGRADING.md) for the cutover guide.

Maintainers cutting a release (PyPI uploads + tag pushes) drive it
through `make` — `make help` lists every distribution target and
[`docs/RELEASING.md`](docs/RELEASING.md) carries the comprehensive
playbook keyed by intent (ship a patch, coordinate multi-package,
back out, smoke-test, rotate credentials, …).

## Developing in this repo (fresh clone)

Three agent surfaces are generated into **gitignored** paths, so a fresh
`git clone` carries the pins/sources but not the generated output — the
workflow commands silently fail to load until you build them:

| Surface | Generated path (gitignored) | Built by |
| --- | --- | --- |
| Claude Code plugin (`/branch-review`, …) | `.agentic/generated/plugins/workstate-system/base/claude` | `make plugins-build` |
| Codex plugin | `.agentic/generated/plugins/workstate-system/base/codex` | `make plugins-build` |
| Codex activation | `.codex/config.toml` | `workstate-bootstrap install --target .` |
| VS Code Copilot prompts | `.github/prompts/` | `make generate-agent-workflows` |

For a Codex-first setup, run the bootstrap installer so the repo-local Codex
activation config is written as well as the generated surfaces:

```bash
workstate-bootstrap install --target .
```

To rebuild only the generated plugin trees and VS Code prompts:

```bash
make plugins-build                                        # Claude + Codex plugin trees
make generate-agent-workflows WORKFLOW_TARGET_ROOT="$PWD" # VS Code Copilot prompts → root .github/prompts/
```

(`WORKFLOW_TARGET_ROOT="$PWD"` is required: without a target the generator
rewrites the package-internal `packages/workstate-system/.github/prompts/` copy,
not the root `.github/prompts/` surface VS Code actually reads.)

Because agents read these only at startup, **restart your agent** after the
first build.

**Auto-trigger:** a `SessionStart` hook in the tracked `.claude/settings.json`
(`.claude/hooks/ensure-agent-surfaces.sh`) runs both generators automatically
the first time you open **Claude Code** on a fresh clone, then prints a one-time
"restart" prompt. Because `plugins-build` emits *both* plugin trees and
`generate-agent-workflows` emits the Copilot prompts, that one Claude session
bootstraps all three surfaces. Codex and VS Code Copilot have **no** equivalent
session-start hook — if you open the repo there first (without ever opening
Claude Code), use `workstate-bootstrap install --target .` for Codex, or run the
two `make` commands above when you only need generated trees/prompts.

## Day-in-the-life

Once installed, a typical task moves through these portable commands.
Each `/command` resolves to the same skill on every supported agent:

```text
/scope            "we should probably add X"
/plan-analyze     triage the draft plan for ambiguity and gaps
/planning-review  formal review with verdicts and findings
/branch-lifecycle make task-start — open the feature branch + worktree
/tdd              first failing test for the slice
/incremental-implementation  drive the slice to green
/branch-review    pre-merge review, findings persisted
/branch-lifecycle make task-finish — close, archive, teardown
```

Anywhere along the way, `make context` reloads the active task and
`/handoff-lifecycle` resumes, switches, or ends the session.

## Layout

```text
workstate/
├── Makefile                                                          # `make help` — release + smoke-test driver
├── scripts/release.sh                                                # release engine wrapped by the Makefile
├── docs/
│   ├── CONSUMER.md                                                   # one-command install + drift workflow
│   ├── UPGRADING.md                                                  # four-repo era → monorepo era cutover
│   ├── RELEASING.md                                                  # maintainer playbook for PyPI + tag releases
│   ├── agentic/
│   │   ├── instructions.md                                           # portable command router (generated)
│   │   ├── lifecycle-map.md                                          # canonical task lifecycle
│   │   └── rules/                                                    # development-workflow + branch-review rules
└── packages/
    ├── workstate-protocol/        # typed schemas (Pydantic v2 + JSON Schema)
    ├── mcp-workstate-handoff/       # MCP server: handoff state + review tracking
    ├── mcp-workstate-orchestrator/  # MCP server: lane management + worker daemons
    ├── workstate-bootstrap/       # consumer-facing install CLI
    └── workstate-system/          # shared skills, hooks, generator scripts
```

## Status

This monorepo is the canonical Workstate implementation source. Earlier
standalone repositories remain temporarily reachable while active
consumers finish migrating; afterwards they will be archived read-only
with redirects here.
