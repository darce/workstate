# Consuming the workstate

This is the entry point for target repos that want the workstate-system
overlay (skills, hooks, MCP-server configs) installed into their tree.
The monorepo distributes a single CLI — `workstate-bootstrap` — that
clones the shared remote, materializes the overlay surfaces, and
registers the two managed MCP servers.

## One-command install

> **Note:** Pin to `v0.1.2` or later. Earlier tags are broken:
>
> - `v0.1.0` — bootstrap looks for shared surfaces at the clone root and
>   fails with `required surface 'scripts/hooks' was not materialized`.
> - `v0.1.1` — bootstrap is missing the PyYAML runtime dep; the
>   generator subprocess exits with `PyYAML is required to read skill.yaml`.

The monorepo root has no `pyproject.toml` (each package owns its own
under `packages/<name>/`). To install `workstate-bootstrap` straight from
git, point `uvx` at the package subdirectory via the `#subdirectory=`
URL fragment:

```bash
uvx --from "git+https://github.com/darce/workstate@v0.1.4#subdirectory=packages/workstate-bootstrap" \
    workstate-bootstrap install \
    --target /path/to/your/repo
```

For repeated `status` / `doctor` runs after install, use a persistent
tool install instead so `workstate-bootstrap` lands on `$PATH`:

```bash
uv tool install "git+https://github.com/darce/workstate@workstate-bootstrap-v0.2.1#subdirectory=packages/workstate-bootstrap"
workstate-bootstrap status  --target /path/to/your/repo
workstate-bootstrap doctor  --target /path/to/your/repo
uv tool upgrade workstate-bootstrap   # later, to pull a newer bootstrap
```

> If `uv tool install` prints `Failed to hardlink files; falling back
> to full copy`, your `uv` cache and tool dir are on different
> filesystems. The install still succeeds — silence the warning with
> `export UV_LINK_MODE=copy` in your shell profile.

That single command:

- clones the monorepo at `v0.1.4` into `<target>/.agentic/remote/`,
- symlinks or carves the SHARED surfaces (`scripts/hooks`, `.github/hooks`,
  `docs/agentic/contracts`, `docs/agentic/rules`, `Makefile.d`, and
  `scripts/workstate`) into the target,
- runs the workflow generator to populate the Copilot prompt surface
  (`.github/prompts`) and the Claude/Codex plugin trees under
  `.agentic/generated/plugins/workstate-system/`,
- writes `.mcp.json`, `.vscode/mcp.json`, and `.codex/config.toml`
  registering both managed MCP servers (`mcp-workstate-handoff` and
  `mcp-workstate-orchestrator`, both runnable via `uvx`),
- runs the handoff server's `init-state` to provision
  `<target>/.task-state/` with `handoff.db` and `exports/` (implementation note;
  skipped under `--no-mcp-servers`),
- sets `core.hooksPath` so `git status` runs the harness hooks (only
  after `init-state` succeeds, so hooks never fire against an
  uninitialized DB),
- writes the install ledger at `<target>/.workstate-bootstrap.json`
  (the legacy `.workstate-overlay.json` filename is auto-migrated on
  upgrade).

No hand-edits required.

### State-ready install contract

After `workstate-bootstrap install`, the cold-start workflow `register
task → switch_task → first record_event` completes from any branch
without `BranchMismatchError`. The handoff `switch_task` operation no
longer enforces branch parity (it is the operation that *resolves* a
branch-mismatch pointer), but content writes (`record_event`,
`close_slice`, `set_handoff_state`, `record_review_finding`,
`record_verified_test`, etc.) keep their branch-isolation checks. The
context-drift warning still surfaces in the `switch_task` response
envelope.

`workstate-bootstrap status` reports the resolved `state_dir` /
`db_path` / `exports_dir` / `schema_version` after a managed install
(via `init-state --check`), so you can confirm the state contract was
satisfied without booting a server. `workstate-bootstrap doctor` flags
a missing `.task-state/handoff.db` as `state_drift` *only* when the
install registered `.mcp.json`; `--no-mcp-servers` installs suppress
that check so config-only installs do not look broken.

`.task-state/` is gitignored (see [`.gitignore` policy](#gitignore-policy-for-bootstrap-managed-surfaces)
below). Each fresh checkout regenerates the DB through bootstrap; this
is the same code path human developers run.

## MCP-server registration

Default behavior (omitting `--mcp-servers`, or passing the literal
`--mcp-servers default`) registers the two MCP servers shipped by this
monorepo:

| Server                   | Command line                                                |
| ------------------------ | ----------------------------------------------------------- |
| `workstate-handoff-mcp`      | `uvx mcp-workstate-handoff@0.11.5 --workspace-root . serve-stdio`      |
| `workstate-orchestrator-mcp` | `uvx mcp-workstate-orchestrator@0.4.7 --workspace-root . serve-stdio` |

Override with a JSON file when you need a non-default mapping:

```bash
workstate-bootstrap install --target . --mcp-servers ./my-mcp.json
```

The file accepts either `{"mcpServers": {...}}` or a flat mapping.

Opt out entirely with `--no-mcp-servers` (the install still writes
SHARED surfaces, generated prompts/plugin trees, lifecycle hoists, and
`core.hooksPath`):

```bash
workstate-bootstrap install --target . --no-mcp-servers
```

## Upgrade

To move to a newer monorepo release, bump `--remote-ref` and re-run
`update`:

```bash
workstate-bootstrap update --target . --remote-ref v0.2.0
```

`update` re-runs the generator, refreshes the SHARED symlinks against
the new clone, and (when `--mcp-servers` is supplied) refreshes the
three config files. Local edits to the GENERATED surfaces are
preserved unless `doctor` reports drift; see "Drift" below.

## Refresh MCP servers

`mcp-sync` is a config-only refresh of the three managed MCP-server
surfaces:

- `.mcp.json` (Claude Code)
- `.vscode/mcp.json` (VS Code)
- `.codex/config.toml` (Codex CLI)

It also rewrites the `mcp_servers` provenance block in
`.workstate-bootstrap.json` so the next run can prune removed managed
launchers without touching third-party entries.

```bash
workstate-bootstrap mcp-sync --target . --mcp-servers default --check    # exit 1 on drift
workstate-bootstrap mcp-sync --target . --mcp-servers default --apply    # write
```

`--mcp-servers` accepts the literal `default` (resolves to the bundled
`MCP_SERVER_PACKAGE_VERSIONS` constant) or a path to a JSON file
holding either a flat ``{name: spec, ...}`` mapping or
``{"mcpServers": {...}}``. Add `--prune-removed-managed` to drop names that previously
appeared in the ledger's `mcp_servers` block but are no longer in the
resolved map; third-party launchers (names absent from the ledger) are
never pruned. Add `--surfaces claude` (or `vscode`, `codex`) to limit
the write to a subset. Add `--json` for machine-readable output that
includes per-surface drift, action, preserved third-party names, and
the post-write ledger state.

`mcp-sync` does NOT fetch the remote, regenerate skills, or run
`init-state`. Use `update` for those. Exit codes: `0` clean reconcile,
`1` drift detected with `--check`, `2` resolution failure (e.g.
unparseable `--mcp-servers`).

## Drift detection and repair

Two subcommands keep the overlay honest after the install:

```bash
workstate-bootstrap doctor --target .   # exit 1 when drift found
workstate-bootstrap repair --target .   # restore drifted surfaces
```

`doctor` covers SHARED (broken or moved symlinks), GENERATED (the
generator's `--check` mode), and — when `--mcp-servers` is supplied —
the three config files. `repair` re-runs the generator for any
GENERATED drift, restores SHARED symlinks, and (with `--mcp-servers`)
rewrites managed config entries. Run with `--force-dirty` to overwrite
SHARED surfaces that contain real local content.

## Overriding individual skills

The Claude and Codex skill surfaces are generated plugin trees. To
override a skill, add an override component under
`workstate-overrides/workstate-system/` and rerun install/update so the
effective plugin tree is regenerated. Copilot prompts remain generated
as real files in the repo and can be edited directly when you accept
the resulting drift:

```text
.github/prompts/<slug>.prompt.md
.agentic/generated/plugins/workstate-system/effective/claude/skills/<slug>/SKILL.md
.agentic/generated/plugins/workstate-system/effective/codex/skills/<slug>/SKILL.md
```

`doctor` will flag direct edits to generated outputs as drift on the
next run; keep durable overrides in the override tree so update/repair
can compose them repeatedly.

To override a hook or shared script, replace the surface with a real
local directory before running `install` (or `repair`). The bootstrap
respects an existing real directory and records `source: "local"` in
the manifest.

## Optional `git plan-cat` alias

`workstate-bootstrap` hoists `scripts/workstate/git-plan-cat.sh` as a
shell wrapper around `make plan-show`'s underlying CLI. It is **not**
installed as a `git` alias automatically — the Make targets
(`make plan-show`, `make plan-edit`, `make plans-list`) remain the
canonical entrypoint. Opt in by adding the snippet below to your
`.gitconfig` (user-level or repo-level):

```gitconfig
[alias]
    plan-cat = "!sh scripts/workstate/git-plan-cat.sh"
```

Then `git plan-cat` prints the active task's plan, and
`git plan-cat WORKSTATE-REF-99` resolves a specific task. Both forms produce
byte-for-byte the same output as `make plan-show` because both shell
through `workstate_handoff_mcp.plan_cli show` — there is no second copy of
the resolver to drift.

Override the launcher by exporting `WORKSTATE_HANDOFF_PLAN_CLI` (legacy
`AGENT_HANDOFF_PLAN_CLI` is honored during the cutover release; e.g. when
the consumer manages its own venv); the default is the same `uvx`
invocation `Makefile.d/plans.mk` uses.

## `current_task_auto_regen` migration note

`mcp-workstate-handoff` flipped the default for `current_task_auto_regen`
to **off** in v0.5.0. If your tooling reads
`<target>/CURRENT_TASK.json` (e.g. dashboards, oncall scripts), opt
back in explicitly:

```bash
# in the target repo, before booting the handoff server
export WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN=1
```

If you have never read `CURRENT_TASK.json`, no action is required —
the file is no longer regenerated automatically.

## What lives where

The canonical source of truth for bootstrap-managed surfaces is the
installer itself: `SHARED_SURFACES` and `GENERATED_SURFACES` in
`packages/workstate-bootstrap/src/workstate_bootstrap/install.py`.
The table below is documentation of that contract, not an independent
surface registry.

| Surface                               | Source     | Layer       |
| ------------------------------------- | ---------- | ----------- |
| `scripts/hooks/`                      | shared     | symlink     |
| `.github/hooks/`                      | shared     | symlink     |
| `docs/agentic/contracts/`             | shared     | symlink     |
| `docs/agentic/rules/`                 | shared     | symlink     |
| `Makefile.d/` non-excluded children   | shared     | carved dir  |
| `scripts/workstate/` non-excluded children | shared  | carved dir  |
| `.github/prompts/`                    | generated  | real dir    |
| `.agentic/generated/plugins/workstate-system/base/` | generated | real dir |
| `.agentic/generated/plugins/workstate-system/effective/` | generated | real dir |
| `.mcp.json`                           | generated  | real file   |
| `.vscode/mcp.json`                    | generated  | real file   |
| `.codex/config.toml`                  | generated  | real file   |
| `core.hooksPath` git config           | generated  | git config  |
| `.agentic/remote/`                    | bootstrap  | git clone   |
| `.workstate-bootstrap.json`             | bootstrap  | manifest    |

All bootstrap-managed paths are listed in `<target>/.workstate-bootstrap.json`
(legacy `.workstate-overlay.json` is auto-renamed on the next install)
with their `source` discriminator (`shared` | `local` | `generated`).

## `.gitignore` policy for bootstrap-managed surfaces

The single rule: **commit the install ledger
(`.workstate-bootstrap.json`); regenerate everything else via
`workstate-bootstrap install` after `git clone`.** Add the block below to
the consumer repo's `.gitignore`.

This policy derives from the installer's owned-surface lists in
`packages/workstate-bootstrap/src/workstate_bootstrap/install.py`
(`SHARED_SURFACES` + `GENERATED_SURFACES`) plus the config writers.
Only ignore paths the installer actually owns.

```gitignore
# --- workstate-bootstrap-managed surfaces ---------------------------------
# Regenerate via `workstate-bootstrap install` from the pinned `remote_sha`
# in `.workstate-bootstrap.json` (which IS tracked — it's the install ledger).
#  - SHARED entries are symlinks into `.agentic/remote/`; they break on a
#    fresh clone until bootstrap recreates the cache.
#  - GENERATED entries are deterministic outputs of the workflow generator
#    and the MCP-config writer; committing them produces drift on every
#    `bootstrap update`.

.agentic/                  # disposable remote-clone cache

/scripts/hooks             # SHARED symlinks
/.github/hooks
/docs/agentic/contracts
/docs/agentic/rules
/Makefile.d
/scripts/workstate

/.github/prompts/          # GENERATED workflow outputs

/.mcp.json                 # GENERATED MCP-server configs
/.vscode/mcp.json
/.codex/config.toml

.task-state/               # local handoff SQLite (per checkout)
```

Dogfood exception: this monorepo has authored root content adjacent to
bootstrap-owned paths. Do not widen these rules to blanket-ignore
entire roots like `.claude/` or `.codex/`, and do not add non-owned
paths such as unrelated Make fragments or `docs/agentic/generated/`
unless the installer surface lists change first.

CI implications: `git clone` alone yields a checkout with no hooks, no
generated prompts/plugin trees, no MCP wiring. CI must run
`workstate-bootstrap install --target .` (using `remote_ref` +
`remote_sha` from the committed `.workstate-bootstrap.json`) before any
workstate-system surface is used.
This is the same flow human developers run, so it forces install
reproducibility through the same code path consumers ship.

Why not commit the symlinks and generated dirs? Two failure modes:

1. **Symlinks point into `.agentic/remote/` which is gitignored.** If
   you commit them, a freshly-cloned checkout has dangling symlinks
   until bootstrap recreates the cache. You still need bootstrap; the
   commit just hides the dependency.
2. **Generated content drifts on every `workstate-bootstrap update`.**
   Committing generated prompt or plugin outputs means each bump
   produces a noisy diff that's not the consumer's authorship. `doctor`
   already detects this as drift; gitignoring the surface eliminates
   the diff entirely.

External consumer repos can usually adopt the block as-is. Dogfood
installs in this monorepo should treat the installer-owned path list as
the boundary and keep authored repo content reviewable in git.
