# workstate-system

Shared workstate-system surface for Daniel's projects: skills, hooks, contracts,
commands, prompts, and the validators that keep them coherent.

This repository is **MVP-scope-private** internal tooling. It is consumed by
the `workstate-bootstrap` CLI, which clones it into
`<consumer-root>/.agentic/remote/` and symlinks selected surfaces into
consumer repos. There is no LICENSE or CONTRIBUTING file by design.

## Contents

The canonical sources are model-agnostic. Per-agent surfaces
(`.claude/skills/`, `.claude/commands/`, `.github/prompts/`,
`.codex/skills/`, …) are *generated* into target repos by
`scripts/generate_agent_workflows.py` during `workstate-bootstrap install`.
They do not exist in this package's source tree.

| Path                                          | Purpose                                                                 |
| --------------------------------------------- | ----------------------------------------------------------------------- |
| `skills/<slug>/skill.yaml`                    | Canonical structured metadata per skill. Validates against `workstate_protocol.SkillManifest`. |
| `skills/<slug>/body.md`                       | Canonical prose body per skill. Plain Markdown, no frontmatter.         |
| `config/agent-workflows/portable_commands.json` | Source-of-truth manifest for the portable command router.             |
| `scripts/generate_agent_workflows.py`         | Renders the manifest + skills into per-agent artifacts (Claude commands + skill packs, Copilot prompts, Codex router blocks, Codex skill copies). |
| `scripts/check_skills.py`                     | Skill-anatomy validator (delegates structured validation to `workstate_protocol.SkillManifest`). |
| `scripts/check_harness_sync.py`               | Cross-harness contract validator.                                       |
| `scripts/migrate_skills_to_neutral_layout.py` | One-shot migration helper from the legacy `.claude/skills/<slug>/SKILL.md` layout. |
| `scripts/hooks/`                              | Claude Code / Copilot shared hooks plus client-side git hooks.          |
| `docs/agentic/contracts/`                     | YAML contracts (`harness-protocol.yaml` and friends).                   |
| `docs/agentic/maps/mcp-tool-routing.yaml`     | Routing map consumed by `check_skills.py` to validate `mcp_tools` references. |
| `docs/plugin-distribution.md`                 | Operator guide for `make plugins-build` / `make plugins-check` and the Claude / Codex install flow. See also [ADR-001](docs/agentic/adrs/ADR-001-agentic-plugin-distribution.md). |
| `Makefile.d/plugins.mk`                       | Make-target fragment wiring `plugins-build` / `plugins-check` to `generate_agent_workflows.py --mode=plugin`. |

## Versioning

This repository uses Semantic Versioning. The `v0.2.0` tag is the
current release and the default `--remote-ref` for the
`workstate-bootstrap` CLI. See `CHANGELOG.md` for the full history.

## Provenance

- `v0.2.0` was extracted from a consumer repository snapshot. It expands the skill
  catalog to 21 populated skills, hoists the
  `.claude/commands/`, `.github/prompts/`, and
  `docs/agentic/templates/` surfaces, and splits the
  `docs/agentic/contracts/` directory so only the six agent/agentic
  contracts ship in the hoisted surface (the consumer-specific
  contracts were removed).
- `v0.1.0` was extracted from the first consumer repository snapshot
  used to seed this package.

## Consumer Setup

The `workstate-bootstrap` CLI handles the clone + symlink +
overlay-manifest write cycle for consumers. Consumer repos may publish
repo-local setup notes that point back to this package.
