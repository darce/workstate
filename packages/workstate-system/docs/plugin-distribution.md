# Plugin Distribution

Operator-facing guide for emitting and installing the workstate-system
Claude Code / Codex plugin trees. The architectural decision is recorded
in
[ADR-001: Agentic Plugin Distribution](workstate/adrs/ADR-001-agentic-plugin-distribution.md)
and
[ADR-003: Plugin Consumer Overrides](workstate/adrs/ADR-003-plugin-consumer-overrides.md);
this page covers the day-to-day flow.

## Audience

- Maintainers of `workstate` (APM) who emit and ship the
  plugin trees.
- Consumer-repo operators installing the plugin
  into Claude Code or Codex.

VS Code Copilot does not consume the Claude/Codex plugin tree. Its
`.github/prompts/<command>.prompt.md` files stay on the legacy generator
output, but those prompts are rendered from the same canonical manifest
and skill bodies.

## Canonical Inputs

The plugin emission reads three files from `packages/workstate-system/`:

| Input                                                      | Role                                                                                     |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------- |
| `skills/<slug>/{skill.yaml, body.md}`                      | Canonical cross-harness skill bodies. SKILL.md is rendered identically for both harnesses. |
| `config/agent-workflows/portable_commands.json`            | Portable command manifest; selects which skills are emitted into the plugin tree.        |
| `config/agent-workflows/mcp_servers.yaml`                  | MCP server registration manifest; sources each harness's `.mcp.json` server map and the plugin version. |

## Emitted Layout

`make plugins-build` writes one tree per supported harness under
`.workstate/generated/plugins/workstate-system/base/{claude,codex}`. That is
the same bootstrap-generated base tree that the checked-in marketplace
pins reference. Paths in the layout sketch below are relative to the repo
root:

```text
.workstate/generated/plugins/workstate-system/base/claude/
  .claude-plugin/plugin.json   # metadata only: name, version, skills, mcpServers
  .mcp.json                    # canonical uvx-stdio launch block
  skills/<slug>/SKILL.md       # one per portable_commands.json skill

.workstate/generated/plugins/workstate-system/base/codex/
  .codex-plugin/plugin.json    # same body as claude/.claude-plugin/plugin.json
  .mcp.json                    # Codex shape: bare server map
  skills/<slug>/SKILL.md       # byte-identical to base/claude/skills/<slug>/SKILL.md
```

`plugin.json` is metadata-only: it carries `name=workstate-system`, a
`version` pulled from `mcp_servers.yaml plugin_version`, a short
`description` (constant in the generator), and the sibling-path
references `skills: "./skills/"` and `mcpServers: "./.mcp.json"`. Slash
commands and skills are discovered by each harness from those sibling
directories — there is no inline `slashCommands` array.

The two `.mcp.json` blobs include `"type": "stdio"` and the same server
entries, but their top-level shape differs by harness. Claude uses the
camelCase wrapper from the Claude plugin schema; live Codex CLI 0.131.0
rejects wrapped plugin MCP config, so Codex uses a bare server map:

Claude:

```json
{
  "mcpServers": {
    "workstate-handoff-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-workstate-handoff@0.12.0", "--workspace-root", ".", "serve-stdio"]
    },
    "workstate-orchestrator-mcp": {
      "type": "stdio",
      "command": "uvx",
      "args": ["mcp-workstate-orchestrator@0.5.1", "--workspace-root", ".", "serve-stdio"]
    }
  }
}
```

Codex:

```json
{
  "workstate-handoff-mcp": {
    "type": "stdio",
    "command": "uvx",
    "args": ["mcp-workstate-handoff@0.12.0", "--workspace-root", ".", "serve-stdio"]
  },
  "workstate-orchestrator-mcp": {
    "type": "stdio",
    "command": "uvx",
    "args": ["mcp-workstate-orchestrator@0.5.1", "--workspace-root", ".", "serve-stdio"]
  }
}
```

## Operator Commands

Run both from the repo root:

```bash
make plugins-build   # emit .workstate/generated/plugins/workstate-system/base/{claude,codex}/
make plugins-check   # re-emit with --check; fails on any hand-mutation
```

`plugins-build` is destination-idempotent: re-running it produces a
byte-identical tree. `plugins-check` is the gate to wire into CI for the
generated plugin tree that marketplace pins load.

Override the destination by passing `PLUGINS_DIST_ROOT=/some/path`.

## Consumer Install

Consumer install is **project-scoped via local plugin marketplace files**
that live in the consumer repo. Each harness reads a different file, but
both pin the same `workstate-system` plugin from the same generated plugin
tree. The pin files are committed in the consumer repo so every clone
opens the project in the same plugin state.

The three pin files (paths are repo-root-relative):

| File                                  | Harness | Purpose                                                                                            |
| ------------------------------------- | ------- | -------------------------------------------------------------------------------------------------- |
| `.claude-plugin/marketplace.json`     | Claude  | Declares the local marketplace and lists the `workstate-system` plugin with its generated source.    |
| `.claude/settings.json`               | Claude  | Registers the marketplace under `extraKnownMarketplaces` and pins it on via `enabledPlugins`.      |
| `.agents/plugins/marketplace.json`    | Codex   | Codex's project-scoped marketplace; same plugin name, points at the generated Codex plugin root.   |

The canonical name is `workstate-system@workstate-marketplace`.

No-override consumers keep their marketplace pins pointed at `.workstate/generated/plugins/workstate-system/base/{claude,codex}`. Override-aware consumers repoint those marketplace pins to `.workstate/generated/plugins/workstate-system/effective/{claude,codex}`.

`.claude/settings.json` uses Claude's `extraKnownMarketplaces` schema,
whose inner `source.source` discriminator is **`directory`** for a
repo-local marketplace tree (Claude Code 2.1.144+). This is **not** the
same as `.agents/plugins/marketplace.json`'s inner source object, which
uses Codex's `local` discriminator. Pasting one schema into the other
file silently breaks session start. The exact shape of the Claude pin:

```json
{
  "extraKnownMarketplaces": {
    "workstate-marketplace": {
      "source": { "source": "directory", "path": "." }
    }
  },
  "enabledPlugins": {
    "workstate-system@workstate-marketplace": true
  }
}
```

`scripts/validate_claude_settings_pin.py` enforces this shape; it runs
under `make check-claude-settings-pin` and is wired into
`make check-agent-workflows`.

### Claude Code

1. Run `make plugins-build` from the APM root (or in the consumer repo
   if it vendors APM via the bootstrap), so
   `.workstate/generated/plugins/workstate-system/base/claude/` exists.
2. Commit `.claude-plugin/marketplace.json` and `.claude/settings.json`
   alongside the project. Both files are tiny and human-readable; do not
  edit the generated plugin tree by hand.
3. **Fresh clone, one-time per checkout:** register the local
   marketplace from the repo root before any `claude plugin install`
   invocation:

   ```bash
   claude plugin marketplace add ./ --scope project
   ```

   This binds the project's `workstate-marketplace` name (declared
   in `.claude-plugin/marketplace.json` and referenced by the
  `enabledPlugins` pin in `.claude/settings.json`) to the local
  `.workstate/generated/plugins/workstate-system/base/claude/` source. Skipping this step makes
   `claude plugin install workstate-system@workstate-marketplace`
   fail with `Plugin "workstate-system" not found in marketplace
   "workstate-marketplace"`, because the CLI does not auto-discover
   marketplaces from `.claude-plugin/marketplace.json` outside of an
   interactive Claude Code session.

   The Codex pin discussed below does not need an equivalent step — Codex
   auto-discovers project-scoped marketplaces from
   `.agents/plugins/marketplace.json` on session start.

   > **Caveat:** `claude plugin marketplace add ./ --scope project`
   > rewrites `.claude/settings.json`'s
   > `extraKnownMarketplaces.<name>.source.path` to an absolute,
   > machine-local path. The committed pin keeps `path: "."` so it stays
   > portable across clones; if you re-run the CLI step, restore
   > `path: "."` (and confirm `source.source: "directory"`) before
   > committing. `make check-claude-settings-pin` fails on either drift.

4. Open the project in Claude Code. Claude resolves the marketplace
   declared by `.claude-plugin/marketplace.json`, sees the plugin pinned
   on in `.claude/settings.json`, and discovers skills + MCP servers
   from the generated plugin tree. (Interactive sessions also accept the
   `marketplace add` step at runtime, but committing the pin files plus
   the one-time CLI registration keeps the install reproducible across
   clones.)

The equivalent CLI form (writes the same `enabledPlugins` entry, and
must follow the `marketplace add` step above) is:

```bash
claude plugin install workstate-system@workstate-marketplace --scope project
```

> **Distribution status (WORKSTATE-REF-02):** the plugin is exercised against
> local `.workstate/generated/plugins/workstate-system/base/claude/` and
> `.workstate/generated/plugins/workstate-system/base/codex/` trees only; it is not yet
> published to an external marketplace. Every install path documented
> here is project-scoped against the committed `.claude-plugin/` and
> `.agents/plugins/` files.

#### Claude Delivery Proof

Claude docs checked on 2026-05-22:

- https://code.claude.com/docs/en/plugin-marketplaces — local marketplace flow uses `.claude-plugin/marketplace.json`, relative plugin `source` values rooted at the marketplace root, `claude plugin validate .`, `claude plugin marketplace add <source> --scope project`, and project-scoped install.
- https://code.claude.com/docs/en/plugins-reference — plugin manifests live at `.claude-plugin/plugin.json`, skills load from `skills/<slug>/SKILL.md`, `.mcp.json` can supply MCP servers, `claude plugin list --json` reports installed plugins, and `claude plugin details <name>` reports component inventory.

Sandbox transcript summary, using `claude` 2.1.146 with a `mktemp -d` project, disposable `HOME`, and disposable `CLAUDE_CODE_PLUGIN_CACHE_DIR`:

```bash
claude plugin validate .
claude plugin marketplace add ./ --scope project
claude plugin install workstate-system@workstate-marketplace --scope project
claude plugin list --json
claude plugin details workstate-system@workstate-marketplace
```

Claude delivery proof result: pass. Validation passed with one non-blocking warning (`description: No marketplace description provided`). The install succeeded at project scope, `claude plugin list --json` showed `workstate-system@workstate-marketplace` enabled with the workstate MCP pins, and `claude plugin details workstate-system@workstate-marketplace` listed 10 skills plus the two MCP servers.

### Codex

1. Run `make plugins-build` so `.workstate/generated/plugins/workstate-system/base/codex/`
   exists.
2. Commit `.agents/plugins/marketplace.json` — the committed Codex pin that
   Codex auto-discovers from the project on session start. `.codex/config.toml`
   is **not** committed: it is gitignored and developer-local (`.gitignore`
   ignores `/.codex/config.toml`, the same surface that carries the per-machine
   MCP-server block). `workstate-bootstrap install` (and `repair`) regenerate its
   Codex activation tables on each checkout:

   ```toml
   [marketplaces.workstate-marketplace]
   source_type = "local"
   source = "."

   [plugins."workstate-system@workstate-marketplace"]
   enabled = true
   ```

   The marketplace source stays relative to the project root (`source = "."`),
   so bootstrap regenerates this activation locally on every checkout without
   ever writing `~/.codex/config.toml` or the user's plugin cache. Activation
   travels with the repo through the committed marketplace pin plus this
   bootstrap-regenerated config, not by committing `.codex/config.toml` itself.
3. Open the project with Codex. The repo-local activation points Codex at
   the local marketplace, and the marketplace's `local` source resolves the
   generated plugin tree. Skills come from `skills/<slug>/SKILL.md` and MCP
   servers from the sibling `.mcp.json`.

The user-global CLI install form remains a compatibility fallback, not the
bootstrap contract:

```bash
codex plugin marketplace add ./
codex plugin add workstate-system@workstate-marketplace
```

#### Codex Delivery Proof

Codex docs checked on 2026-05-22 and rechecked for the WORKSTATE-REF-06 `.mcp.json`
shape fix on 2026-05-30:

- https://developers.openai.com/codex/plugins/build — repo-scoped marketplaces live at `$REPO_ROOT/.agents/plugins/marketplace.json`; local entries use `source: local` with a `./`-prefixed `source.path` relative to the marketplace root; `codex plugin marketplace add <source>` registers a local marketplace; `codex plugin list --marketplace <name>` shows available plugins; `codex plugin add <plugin>@<marketplace>` installs into the Codex plugin cache. Bootstrap does not use that cache path by default; it writes repo-local activation into `.codex/config.toml`.

Sandbox transcript summary, using `codex-cli` 0.131.0 with a `mktemp -d` project and disposable `HOME`:

```bash
codex plugin marketplace add ./
codex plugin marketplace list
codex plugin list --marketplace workstate-marketplace
codex plugin add workstate-system@workstate-marketplace
codex debug prompt-input
codex exec --skip-git-repo-check --ephemeral --dangerously-bypass-approvals-and-sandbox "Use workstate-handoff-mcp/load_session once."
```

Codex delivery proof result: pass. The marketplace registered from the isolated repo root, `codex plugin marketplace list` showed `workstate-marketplace`, `codex plugin list --marketplace workstate-marketplace` showed `workstate-system@workstate-marketplace` available, and `codex plugin add workstate-system@workstate-marketplace` installed the plugin into `~/.codex/plugins/cache/workstate-marketplace/workstate-system/0.2.0/`. The installed cache contained `.codex-plugin/plugin.json`, `.mcp.json`, and the `skills/` directory. Codex may print non-blocking remote-plugin or icon warnings in disposable homes; plugin marketplace discovery and install still completed successfully.

WORKSTATE-REF-06 loader-shape follow-up result: pass for plugin parsing and tool
resolution. The installed Codex `.mcp.json` was a bare server map,
`codex debug prompt-input` listed the `workstate-system:*` plugin skills, and
`codex exec` no longer emitted the prior `invalid transport` warning that
occurred when the file used a `mcp_servers` wrapper. A disposable
`CODEX_HOME` seeded with existing auth completed a noninteractive
`workstate-handoff-mcp/load_session` call; read-only non-bypass mode registered
the server but cancelled the MCP call before return, so the final proof used
Codex's explicit noninteractive bypass flag in the disposable home.

Repo-local activation proof: `workstate-bootstrap install` now writes the same
marketplace source and enabled plugin selector into `.codex/config.toml` with
`source = "."`, preserving unrelated Codex config and an explicit local
`enabled = false` override. This is the preferred bootstrap path because it is
project-scoped and avoids user-global config/cache writes.

### Smoke-Only Fallback: Local Plugin Directory

Claude's historical local-tree install form is still useful for one-off
smoke tests, but it is **smoke-only** — it does not produce a committed,
reproducible install:

```bash
# Smoke-test only; not part of the consumer-install contract.
claude --plugin-dir path/to/repo/.workstate/generated/plugins/workstate-system/base/claude /skills
```

Use this form to verify a freshly generated Claude plugin tree before
committing a pin update. Current Codex CLI builds no longer expose a
top-level `--plugin-dir`; Codex smoke tests should use the repo-local
marketplace/config path above or the user-global CLI fallback in a
disposable `HOME`.

### Consumer Overrides

WORKSTATE-REF-03 adds one explicit customization path for consumers: put
repo-owned overrides under `workstate-overrides/workstate-system/` and let
bootstrap or the composition flow generate the effective plugin tree at
`.workstate/generated/plugins/workstate-system/effective/{claude,codex}/`.

Operator rules:

- do not edit `.workstate/generated/plugins/workstate-system/base/...`,
  harness plugin caches, or
  `.workstate/generated/plugins/workstate-system/effective/...` by hand.
  Those are generated outputs and will be replaced on the next
  install/update/repair cycle.
- Do not rely on undeclared same-name shadowing. The override root is
  the supported contract.
- Normal install/update preserves override files. Destructive cleanup
  must use the explicit `--reset-overrides` flow instead of a generic
  "clean install" shortcut.

The generated effective tree is allowed to stay gitignored because it is
bootstrap-managed output, not source. The tracked source of truth is the
override root plus its lock/provenance files.

The override root carries tracked inputs such as `overrides.yaml`,
`overrides.lock.json`, `skills/<slug>/SKILL.md`, and structured MCP patch
files under `tools/`. The generated effective tree emits its own
`plugin-lock.json` receipt so operators can distinguish tracked override
intent from generated output provenance.

#### Walkthrough: replace a shipped skill

1. Create the override file under
   `workstate-overrides/workstate-system/skills/<slug>/SKILL.md`.
2. Declare it in `workstate-overrides/workstate-system/overrides.yaml` with
   `mode: replace`, the relative file path, and the recorded
   `upstream_digest`.
3. Run:

   ```bash
   workstate-bootstrap install --plugin-overrides workstate-overrides/workstate-system
   ```

4. Confirm the generated body now lives under
   `.workstate/generated/plugins/workstate-system/effective/{claude,codex}/skills/<slug>/SKILL.md`
   and that `overrides.lock.json` records the tracked replacement.

#### Walkthrough: add a repo-specific skill

1. Create the skill source under
   `agentic-overrides/workstate-system/skills/<slug>/SKILL.md` with the
   normal `SKILL.md` frontmatter, for example `name` and `description`,
   followed by the skill body.
2. Declare it in `agentic-overrides/workstate-system/overrides.yaml` with
   `mode: add` and the relative file path. Added skills are repo-owned;
   they do not need an upstream digest.
3. Run:

   ```bash
   workstate-bootstrap install --plugin-overrides agentic-overrides/workstate-system
   ```

4. Confirm the generated body now lives under
   `.workstate/generated/plugins/workstate-system/effective/{claude,codex}/skills/<slug>/SKILL.md`
   and that `plugin-lock.json` records the component with `mode: add`.

#### Walkthrough: disable a shipped skill

Set the component entry in `overrides.yaml` to `mode: disable`, then run:

```bash
workstate-bootstrap update --plugin-overrides workstate-overrides/workstate-system
```

The next effective tree omits that skill while the generated base tree
stays untouched.

#### Walkthrough: patch MCP server args

1. Add a patch file such as
   `workstate-overrides/workstate-system/tools/mcp_servers.patch.yaml`.
2. Declare the server under `components.mcp_servers` in `overrides.yaml`
   with `mode: patch`, `patch_path`, and `requires_trust_ack: true`.
3. Run install or update with `--plugin-overrides`.
4. Review `overrides.lock.json`, generated `plugin-lock.json`, and
   `workstate-bootstrap doctor` output to confirm the command, args, or env
   mutation was recorded explicitly.

#### Walkthrough: update upstream and inspect a stale override

After pulling a newer upstream plugin version, run:

```bash
workstate-bootstrap update --plugin-overrides workstate-overrides/workstate-system
workstate-bootstrap doctor
```

If the canonical base digest changed beneath a warn-mode replacement,
`workstate-bootstrap doctor` reports `stale_override` and leaves the local override
in place. Review the upstream skill or MCP change, update the override if
needed, then rerun update so the new effective tree and receipts reflect
the reconciled state.

Fresh clones with override-aware marketplace pins should also use
`workstate-bootstrap doctor` when the effective tree has not yet
been materialized; that path reports the missing generated target and the
install/update remediation instead of silently failing.

#### Walkthrough: reset overrides safely

Normal install/update never removes consumer-owned override files. The
only destructive path is the explicit reset flow:

```bash
workstate-bootstrap update \
  --plugin-overrides workstate-overrides/workstate-system \
  --reset-overrides \
  --backup
```

`--reset-overrides` refuses to run on a dirty worktree unless `--backup`
is also supplied. With backup enabled, bootstrap archives the override
root under `.workstate/override-backups/<timestamp>/` before removal,
recomposes the base-only effective tree, and prints the backup path.

## Pin Updates

When a new MCP runtime release ships:

1. Update the `args` entries in
   `packages/workstate-system/config/agent-workflows/mcp_servers.yaml`.
2. Bump `plugin_version` in the same file (it flows into both
   `plugin.json` files via the generator).
3. Run `make plugins-build && make plugins-check` to refresh the tree.
4. Bump the developer-local `.mcp.json` (gitignored) and
   `workstate-bootstrap`'s `DEFAULT_MCP_SERVERS` in lockstep.

The bootstrap install path remains the source-of-truth for the live
developer config; the plugin distribution path is the source-of-truth
for what consumers receive when they install the plugin.

> **Note on developer-local MCP duplication.** The repo-root `.mcp.json`
> is gitignored (`.gitignore:51`) — it is the maintainer's live config,
> not part of the consumer-install contract. When the plugin is also
> installed via the pin, `claude doctor` will report the plugin's
> `mcpServers` as "skipped because identical servers are already
> configured elsewhere." That elsewhere is your local `.mcp.json`,
> taking precedence over the plugin tree. Consumers who install only
> via the pin see the plugin's MCP block as the sole source.

## Related

- [ADR-001: Agentic Plugin Distribution](workstate/adrs/ADR-001-agentic-plugin-distribution.md)
- [WORKSTATE-REF-01 task plan](tasks/WORKSTATE-REF-01-plugin-distribution-generator-task-plan.md)
- [Epic: agentic-plugin-distribution](../../../docs/epics/agentic-plugin-distribution-epic.md)
- `config/agent-workflows/mcp_servers.yaml`
- `scripts/generate_agent_workflows.py`
- `Makefile.d/plugins.mk`
