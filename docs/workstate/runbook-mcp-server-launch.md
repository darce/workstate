# Runbook: MCP server launch & regenerating `.mcp.json`

Operator playbook for the Workstate MCP servers (`workstate-handoff-mcp`,
`workstate-orchestrator-mcp`) and how to (re)generate the harness launch
surfaces — primarily the Claude Code `.mcp.json`.

`.mcp.json` is **gitignored and generated** by the `workstate-bootstrap`
installer. Never hand-edit it for a durable change; regenerate it with the
commands below so the managed-server ledger stays consistent.

---

## Symptom: a Workstate MCP server's tools never appear

You open a harness session and the tools from one server are missing — e.g.
`mcp__workstate-handoff-mcp__*` never surface — even though `claude mcp list`
reports the server **✓ Connected**.

This is **not** a deprecation or naming problem (legacy `agent*-handoff-mcp`
names are fully purged; `workstate-*` is correct). `claude mcp list` runs a
fresh *warm* health probe, so it succeeds while the live session's tool
registry — built at session startup — is empty for that server.

### Root cause

The generated launch command was `uv run --project <pkg> … serve-stdio`.
`uv run` re-resolves and **re-syncs** the project environment on every launch,
including rebuilding the editable `workstate-protocol` path dependency, whenever
the project metadata looks fresh — which the installer guarantees each time it
rewrites `.workstate/remote` (fresh mtimes). That cold sync is ~10s warm and
30–45s under a cold uv cache plus the two servers contending on uv's global
lock when the harness launches them simultaneously. The slower server overruns
the harness's **30s MCP connection timeout** and registers **zero tools** for
the session.

Confirm in the harness MCP logs:

```
~/Library/Caches/claude-cli-nodejs/<url-encoded-repo-path>/mcp-logs-workstate-handoff-mcp/*.jsonl
# look for: "Connection timeout triggered after NNNNNms (limit: 30000ms)"
```

### The fix (shipped in the installer)

The installer now decouples launch from resolution
(`packages/workstate-bootstrap/src/workstate_bootstrap/install.py`):

- Generated serve commands use **`uv run --no-sync`** — launch is a plain exec
  against an already-built environment: no resolution, no network, no shared
  uv cache lock on the startup hot path. Boot drops to ~2s, contention-free.
- The MCP server venvs are **pre-built at install time**
  (`_presync_local_mcp_envs`) — resolution happens once, in the install phase,
  where it belongs.
- Existing local specs are repaired on any refresh because the `--no-sync`
  invariant is enforced at the shared **render seam**
  (`_canonicalize_managed_servers`), so `install` / `update` / `repair` /
  `mcp-sync` all rewrite an older launcher to add `--no-sync` (implementation note A1;
  the earlier preserve-path `_normalize_local_mcp_server_specs` patch was
  retired once the seam owned the invariant).

> Do **not** "fix" this by raising `MCP_TIMEOUT`. That masks the smell; the
> servers should launch in seconds with no race.

---

## Regenerating `.mcp.json` (push the launch fix live)

The launch command lives in the generated surface, so you regenerate the
surface and **restart the harness**. Pick the path that matches your situation.

### A. Fresh consumer install — nothing to do

`workstate-bootstrap install` already emits `--no-sync` and pre-builds the
venvs. New installs are correct out of the box.

### B. Existing install — refresh (canonical)

`update` preserves the existing managed-server mapping and normalizes it
(adding `--no-sync`), re-builds the venvs, and refreshes every surface:

```bash
# from the consumer repo root; remote-ref defaults to the recorded manifest ref
uv run --project packages/workstate-bootstrap \
  workstate-bootstrap update --target . --remote-ref <ref>
```

Note: `update` re-fetches and re-checks-out the shared `.workstate/remote`
overlay. In a busy multi-session repo, prefer the surgical path (C) to avoid
disturbing other sessions' overlay reads.

### C. Config-only, overlay-safe (surgical)

Rewrites only the launch surfaces from an explicit normalized map — no overlay
re-fetch, no `init-state`, no skill regeneration. Use this to push the
`--no-sync` fix into `.mcp.json` in place.

```bash
# 1. derive the normalized map from the current .mcp.json (adds --no-sync)
python3 - <<'PY'
import json
d = json.load(open(".mcp.json"))
norm = {}
for name, spec in d["mcpServers"].items():
    args = list(spec.get("args", []))
    if spec.get("command") == "uv" and args[:1] == ["run"] and "--no-sync" not in args:
        args = [args[0], "--no-sync", *args[1:]]
    norm[name] = {**spec, "args": args}
json.dump({"mcpServers": norm}, open("/tmp/ws_norm_mcp.json", "w"), indent=2)
PY

# 2. preview drift (read-only; exit 1 == would change)
uv run --project packages/workstate-bootstrap \
  workstate-bootstrap mcp-sync --target . \
  --mcp-servers /tmp/ws_norm_mcp.json --surfaces claude --check --json

# 3. apply
uv run --project packages/workstate-bootstrap \
  workstate-bootstrap mcp-sync --target . \
  --mcp-servers /tmp/ws_norm_mcp.json --surfaces claude --apply --json
```

`mcp-sync` preserves third-party servers and updates the managed-server ledger.
Add `vscode codex` to `--surfaces` to fix the VS Code / Codex launchers too.

### Then: restart the harness, and verify

Surfaces are read at startup, so **restart Claude Code** (and re-open Codex /
VS Code) to pick up the new launch command.

```bash
# launch command now starts with: run --no-sync --project …
python3 -c "import json;s=json.load(open('.mcp.json'))['mcpServers'];[print(n,s[n]['args']) for n in s]"
```

---

## Gotchas

- **`--no-sync` needs a pre-built venv.** If a server's
  `.workstate/remote/packages/<pkg>/.venv` is missing or stale, `--no-sync`
  exec fails. Re-run `install`/`update` (which pre-builds it) or, as a one-off,
  `uv sync --project .workstate/remote/packages/<pkg>`.
- **`claude mcp list` ✓ Connected is not proof the session registered the
  tools** — it is a separate warm probe. Trust the live session's tool list and
  the MCP logs.
- **Server health check (independent of the harness):** a direct stdio
  `tools/list` handshake against the server proves the server itself exposes
  tools, isolating client-side registration failures.
- **Do not raise `MCP_TIMEOUT`** as the fix — see above.

## See also

- `packages/workstate-bootstrap/src/workstate_bootstrap/install.py` —
  `_build_local_default_mcp_servers`, `_presync_local_mcp_envs`,
  `_canonicalize_managed_servers` (the render-seam `--no-sync` enforcer).
- `docs/workstate/environment-variables.md`
- `docs/UPGRADING.md`
