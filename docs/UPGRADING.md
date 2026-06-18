# Upgrading from the four-repo era to `workstate`

If your target repo previously consumed any of these legacy private
repos directly, this guide walks the cutover:

- `darce/mcp-workstate-handoff`
- `darce/mcp-workstate-orchestrator`
- `darce/workstate-system`
- `darce/workstate-bootstrap`

All four now ship from `darce/workstate` under
`packages/<name>/`. The legacy repos stay reachable until active
consumers migrate; afterwards they will be archived (read-only) with
pinned issues redirecting to the monorepo.

## What changes for consumers

| Before                                                                | After                                                              |
| --------------------------------------------------------------------- | ------------------------------------------------------------------ |
| `workstate-bootstrap install --remote-url darce/workstate-system@vX`      | `workstate-bootstrap install` (default remote = monorepo `main`)     |
| Four separate `vX.Y.Z` tags to track                                  | One canonical `vX.Y.Z` monorepo tag (per-package tags exist alongside) |
| Hand-supply `--mcp-servers <json>` for each install                   | Default registers both managed MCP servers via `uvx`               |
| Orchestrator pinned a private handoff checkout via `git+ssh://...@v0.4.3` | Orchestrator depends on `mcp-workstate-handoff>=0.5.0,<0.6.0` from PyPI |
| Skill content under `.claude/skills/<slug>/SKILL.md` (Claude-only)    | Neutral source `skills/<slug>/{skill.yaml,body.md}` generates per-agent plugin/prompt surfaces |

## Cutover steps

1. **Update `--remote-url` and `--remote-ref`.** The `workstate-bootstrap`
   `0.3.0` release ships a new default `DEFAULT_REMOTE_URL`
   (`git@github.com:darce/workstate.git`) and
   `DEFAULT_REMOTE_REF` (`main`). If your install was pinned at the
   legacy `darce/workstate-system` URL, repoint it:

   ```bash
   workstate-bootstrap update \
       --target . \
       --remote-url git@github.com:darce/workstate.git \
       --remote-ref v0.1.24
   ```

   Subsequent upgrades only need `--remote-ref`.

2. **Drop hand-supplied `--mcp-servers` files** unless you carry
   non-default servers. The default map registers
   `mcp-workstate-handoff` and `mcp-workstate-orchestrator` via `uvx` with
   `--workspace-root . serve-stdio`; that is the shipped cross-client
   startup contract for Claude Code, Codex, Cursor, grok, and VS Code
   Copilot. To keep a custom
   map, continue passing `--mcp-servers <path>`, but include an MCP
   transport subcommand yourself. To skip MCP-config writes entirely,
   pass `--no-mcp-servers`.

3. **Re-run `workstate-bootstrap install`** (or `update`) with the new
   remote. Verify with `workstate-bootstrap doctor --target .` — exit
   `0` is clean. Investigate any drift before committing.

4. **Refresh per-agent surfaces.** The neutral skill layout
   (`skills/<slug>/{skill.yaml,body.md}`) regenerates harness-native
   plugin or prompt surfaces for Claude Code, Codex, Cursor, grok, and
   VS Code Copilot on every install. If your target had hand-edited any
   generated output, expect drift on the first
   `doctor` run; re-apply your overrides on top of the regenerated
   content. The `$branch-review` skill guidance is part of this
   regenerated surface and must be consumed from the release tag, not
   copied by hand.

5. **Mind the `current_task_auto_regen` default flip.** As of
   `mcp-workstate-handoff 0.5.0`, the default is **off**. If your tooling
   reads `CURRENT_TASK.json`, opt back in explicitly with
   `WORKSTATE_HANDOFF_CURRENT_TASK_AUTO_REGEN=1`.

## Pin the cross-package versions

When picking package versions in your own `pyproject.toml`,
`requirements.txt`, or `.workstate-overlay.json`, pin against the PyPI
releases produced from the monorepo, not the legacy git URLs:

```toml
# example for a target that depends on the typed contracts directly
workstate-protocol = ">=0.1.0,<0.2.0"
```

The `workstate-bootstrap` install pins the monorepo tag (`--remote-ref`)
end-to-end, so you do not need to enumerate package versions
individually.

## Worktree bootstrap hook (`LIFECYCLE_WORKTREE_BOOTSTRAP`)

As of `workstate-system` implementation note, consumers can declare a
post-provision shell command that `make task-start` runs automatically
after Python sync and overlay adopt — for example `npm install` for a
Node-backed app directory. Set it in your root `Makefile`:

```makefile
LIFECYCLE_WORKTREE_BOOTSTRAP = cd apps/my-app && npm install
```

The recipe forwards this to `WORKSTATE_WORKTREE_BOOTSTRAP_CMD` for the
lifecycle runner. Empty default = no-op. Bootstrap failures are
best-effort (they do not roll back the worktree). See
`packages/workstate-system/workstate_system/payload/docs/workstate/rules/development-workflow.md`
(§ Post-provision worktree bootstrap) for semantics, timeout, and trigger
coverage. Requires an overlay refresh of `.workstate/remote` after the
payload release lands.

## Removing legacy pins

After the migration is complete, search the target repo for any
remaining references to the four legacy remotes and remove them:

```bash
rg --hidden 'darce/(mcp-workstate-handoff|mcp-workstate-orchestrator|workstate-system|workstate-bootstrap)\.git'
```

Once these are clean and `workstate-bootstrap doctor --target .` is
green, the upgrade is complete.
