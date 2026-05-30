# Workstate Orchestrator MCP

MCP server for orchestration, lane management, worker daemons, review dispatch, and ACE metrics.

## Installation

### From PyPI (recommended)

```bash
pip install mcp-workstate-orchestrator
# or, as an isolated tool:
uv tool install mcp-workstate-orchestrator
# or, ad-hoc without installing:
uvx mcp-workstate-orchestrator --help
```

`mcp-workstate-orchestrator` declares `mcp-workstate-handoff>=0.5.0,<0.6.0` as a
required dependency; pip resolves it from PyPI automatically.
`workstate-codex-bridge` remains optional unless you want the local
bridge backend.

### From the monorepo source tree (development)

From this package root inside `workstate`:

```bash
cd packages/mcp-workstate-orchestrator
python -m pip install -e ".[dev]"
```

When developing both MCP servers in lockstep, install the sibling
handoff package as an editable first so the orchestrator picks it up:

```bash
pip install -e ../mcp-workstate-handoff
pip install -e ".[dev]"
```

## Development

Run package-local commands from the package root:

```bash
make lint-orchestrator
make fix-lint-orchestrator
make format-orchestrator
make mypy-orchestrator
make test-orchestrator
make check-orchestrator
```

The package Makefile keeps `workstate-codex-bridge` as an optional sibling source path for local bridge-backend development, but it expects `mcp-workstate-handoff` to be installed as a normal package dependency.

Direct commands also work:

```bash
PYTHONPATH=src python -m ruff check src tests
PYTHONPATH=src python -m mypy src
PYTHONPATH=src python -m pytest tests -q
```

## Token-Efficient Usage

For bounded reads and compact caller patterns, follow the shared guide in [`packages/mcp-workstate-handoff/docs/guides/token-efficient-usage.md`](../mcp-workstate-handoff/docs/guides/token-efficient-usage.md). The orchestrator package reuses that guidance instead of maintaining a separate copy of the same parameter semantics.

## Runtime Notes

This package orchestrates work against a target workspace. The workspace you point it at still needs the expected task state and orchestration inputs, such as:

- `.task-state/`
- lane manifests
- task plans or other orchestration docs the lane logic references

Those assets belong to the workspace being orchestrated, not to the package checkout itself.

## Backends

The orchestration layer supports multiple execution backends, including:

- `codex-cli`
- `codex-subagent`
- `claude-code`
- `local-model-openai`

Some backends are optional and require host-specific tooling to be installed separately.

## Source Checkout Usage

For local source execution without installation:

```bash
PYTHONPATH=src python -m workstate_orchestrator_mcp --help
```

If you are testing against a sibling `workstate-codex-bridge` checkout instead of an installed bridge dependency, extend `PYTHONPATH` with that sibling `src` directory as needed.
