"""Post-install subcommands: status / doctor / update / repair.

Each subcommand is a small library function with a clear contract:

- ``status(target)`` returns a human-readable summary of the overlay manifest.
- ``doctor(target, mcp_servers=None)`` returns a list of drift findings.
- ``update(target, remote_ref, ...)`` (future slice) re-runs install at a new ref.
- ``repair(target, ..., force_dirty)`` (future slice) rewrites drifted overlays.

The CLI in ``workstate_bootstrap.cli`` is a thin argparse wrapper over these.
"""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Mapping

import yaml

from workstate_bootstrap.install import (
    BOOTSTRAP_MANIFEST_NAME,
    CLAUDE_MARKETPLACE_PATH,
    CLONE_SUBDIR,
    CODEX_CONFIG_PATH,
    CODEX_MARKETPLACE_PATH,
    GENERATED_SURFACES,
    GENERATOR_MANIFEST,
    GENERATOR_SCRIPT,
    GENERATOR_SKILLS_SOURCE,
    PLUGIN_NAME,
    PLUGIN_GENERATED_ROOT,
    PLUGIN_MARKETPLACE_NAME,
    PLUGIN_OVERRIDE_MANIFEST,
    PLUGIN_OVERRIDE_ROOT,
    _discover_plugin_override_root,
    _relative_plugin_tree_path,
    _resolve_in_clone,
    SHARED_SURFACES,
    SURFACE_CHILD_EXCLUSIONS,
    _migrate_legacy_manifest,
)
from workstate_bootstrap.worktree import (
    WorktreeError,
    is_linked_worktree,
    overlay_is_materialized,
    primary_overlay_root,
)


def _unadopted_worktree_primary(target: Path) -> Path | None:
    """Return the primary overlay root when ``target`` is a linked worktree whose
    overlay has not been adopted yet, else ``None``.

    Adoption state is keyed on the CLONE redirect (``.workstate/remote``), not the
    marker: the ``.workstate-bootstrap.json`` marker is typically tracked and so
    survives into a fresh worktree, while the gitignored clone does not. A
    non-git, primary, or already-adopted target returns ``None`` so the regular
    doctor checks proceed untouched.
    """
    try:
        if not is_linked_worktree(target):
            return None
        if target.joinpath(*CLONE_SUBDIR).exists():
            return None  # clone redirect present -> already adopted
        primary = primary_overlay_root(target)
    except WorktreeError:
        return None
    if primary != target and overlay_is_materialized(primary):
        return primary
    return None


def _load_manifest(target: Path) -> dict[str, object]:
    _migrate_legacy_manifest(target)
    manifest_path = target / BOOTSTRAP_MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"{manifest_path} not found. Run `workstate-bootstrap install --target "
            f"{target}` first."
        )
    return json.loads(manifest_path.read_text())


def _preserved_mcp_servers(
    target: Path, manifest: Mapping[str, object]
) -> Mapping[str, Mapping[str, Any]] | None:
    """Return the currently registered managed MCP mapping, if any.

    Updates inherit the existing managed registration by default. This keeps
    `.mcp.json` / `.vscode/mcp.json` / `.codex/config.toml` listed in the
    refreshed manifest and ensures init-state still runs after a managed
    install when the caller omits ``mcp_servers``.
    """
    configs = manifest.get("configs", []) or []
    registered_mcp = any(
        isinstance(entry, dict) and entry.get("path") == ".mcp.json"
        for entry in configs
    )
    if not registered_mcp:
        return None

    mcp_path = target / ".mcp.json"
    if not mcp_path.is_file():
        raise FileNotFoundError(
            f"{mcp_path} missing for managed update; re-run install or pass --mcp-servers."
        )

    try:
        doc = json.loads(mcp_path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{mcp_path} is not valid JSON; repair or replace it before update."
        ) from exc

    servers = doc.get("mcpServers")
    if not isinstance(servers, dict):
        raise ValueError(
            f"{mcp_path} does not contain an mcpServers mapping; repair it before update."
        )

    preserved: dict[str, Mapping[str, Any]] = {}
    for name, spec in servers.items():
        if not (isinstance(name, str) and isinstance(spec, dict)):
            continue
        preserved[name] = spec
    return preserved


def status(*, target: Path) -> str:
    """Return a multi-line human-readable summary of the overlay manifest at
    ``<target>/.workstate-bootstrap.json``.

    When the install registered MCP servers (``.mcp.json`` recorded in
    ``configs``), invokes ``init-state --check`` to append the resolved
    state directory, database path, exports directory, and schema
    version. ``--no-mcp-servers`` installs skip this section.

    Raises ``FileNotFoundError`` when the manifest is absent.
    """
    target = Path(target).resolve()
    manifest = _load_manifest(target)

    surfaces = manifest.get("surfaces", []) or []
    configs = manifest.get("configs", []) or []
    shared = sum(1 for s in surfaces if s.get("source") == "shared")
    local = sum(1 for s in surfaces if s.get("source") == "local")
    generated = sum(1 for s in surfaces if s.get("source") == "generated")

    lines = [
        f"workstate-bootstrap overlay at {target}",
        f"  remote_url:  {manifest.get('remote_url')}",
        f"  remote_ref:  {manifest.get('remote_ref')}",
        f"  remote_sha:  {manifest.get('remote_sha')}",
        f"  surfaces:    {len(surfaces)} ({shared} shared, {local} local, {generated} generated)",
        f"  configs:     {len(configs)}",
    ]
    for entry in configs:
        lines.append(f"    - {entry.get('path')} ({entry.get('action')})")

    state_lines = _status_handoff_state_lines(target, configs)
    if state_lines:
        lines.append("  handoff state:")
        lines.extend(f"    {line}" for line in state_lines)

    return "\n".join(lines) + "\n"


def _status_handoff_state_lines(
    target: Path, configs: list[dict[str, Any]]
) -> list[str]:
    """Invoke ``init-state --check`` against the workstate-handoff-mcp entry in
    ``.mcp.json`` and return zero or more summary lines.

    Returns an empty list when the install did not register MCP servers
    (so init-state was never expected to run) — this mirrors doctor's
    state-check gating.
    """
    import subprocess

    registered_mcp = any(
        isinstance(entry, dict) and entry.get("path") == ".mcp.json"
        for entry in configs
    )
    if not registered_mcp:
        return []

    mcp_path = target / ".mcp.json"
    if not mcp_path.is_file():
        return ["error: .mcp.json missing — re-run install"]

    try:
        mcp_doc = json.loads(mcp_path.read_text())
    except json.JSONDecodeError:
        return ["error: .mcp.json is not valid JSON"]
    if not isinstance(mcp_doc, dict):
        return ["error: .mcp.json is not a JSON object — re-run install"]
    servers = mcp_doc.get("mcpServers")
    if servers is not None and not isinstance(servers, dict):
        return ["error: .mcp.json mcpServers is malformed — re-run install"]
    spec = None
    if isinstance(servers, dict):
        spec = servers.get("workstate-handoff-mcp")
    if not isinstance(spec, dict):
        return []

    cmd = _resolve_init_state_check_command(target, spec)
    if cmd is None:
        return ["error: workstate-handoff-mcp entry in .mcp.json is malformed"]

    env = os.environ.copy()
    raw_env = spec.get("env")
    if isinstance(raw_env, dict):
        for key, value in raw_env.items():
            if isinstance(key, str) and isinstance(value, str):
                env[key] = value

    try:
        proc = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            cwd=str(target),
            env=env,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"error: init-state --check failed: {exc}"]

    if proc.returncode != 0:
        first = (proc.stderr or proc.stdout or "").strip().splitlines()
        detail = first[-1] if first else f"exit {proc.returncode}"
        return [f"error: init-state --check failed: {detail}"]

    payload: dict[str, Any]
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [f"error: init-state --check returned non-JSON: {proc.stdout[:120]!r}"]

    return [
        f"state_dir:      {payload.get('state_dir')}",
        f"db_path:        {payload.get('db_path')}",
        f"exports_dir:    {payload.get('exports_dir')}",
        f"schema_version: {payload.get('schema_version')}",
        f"initialized:    {payload.get('initialized')}",
    ]


def _resolve_init_state_check_command(
    target: Path, spec: dict[str, Any]
) -> list[str] | None:
    """Map the ``.mcp.json`` workstate-handoff-mcp entry to an
    ``init-state --check`` invocation, mirroring install-time resolution
    in ``install._run_init_state``.
    """
    command = spec.get("command")
    raw_args = spec.get("args", [])
    if not isinstance(command, str) or not command:
        return None
    if not isinstance(raw_args, list) or not all(isinstance(a, str) for a in raw_args):
        return None

    args = list(raw_args)
    if args and args[-1] in {"serve-stdio", "serve-http", "init-state"}:
        args = args[:-1]
    if not any(
        a == "--workspace-root" or a.startswith("--workspace-root=") for a in args
    ):
        args.extend(["--workspace-root", str(target)])
    raw_env = spec.get("env")
    env_has_state_dir = (
        isinstance(raw_env, dict) and "WORKSTATE_HANDOFF_STATE_DIR" in raw_env
    )
    if (
        not any(a == "--state-dir" or a.startswith("--state-dir=") for a in args)
        and not env_has_state_dir
    ):
        args.extend(["--state-dir", str(target / ".task-state")])
    args.extend(["init-state", "--check"])
    return [command, *args]


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


Finding = dict[str, str]


def doctor(
    *,
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None = None,
    plugin_overrides: Path | None = None,
) -> list[Finding]:
    """Return a list of drift findings for the overlay at ``target``.

    Each finding is a dict with at least ``kind`` and ``path``. Recognized
    kinds:

    - ``missing_manifest`` — ``.workstate-bootstrap.json`` is gone.
    - ``missing_clone`` — ``.workstate/remote/.git`` is gone.
    - ``surface_drift`` — a surface recorded as ``shared`` in the manifest is
      no longer a symlink resolving into the clone.
    - ``local_stale`` — a ``source=local`` surface exactly matches an *older*
      payload revision: a bootstrap-era copy silently starving updates
      (implementation note implementation note).
    - ``local_redundant`` — a ``source=local`` surface is identical to the
      current payload; candidate for adoption into a managed surface.
    - ``local_override`` — a ``source=local`` surface matches no payload
      revision: consumer-authored. Emitted with ``severity=info``; never
      affects the doctor exit code and is never repair-eligible.
    - ``generated_drift`` — a surface recorded as ``generated`` differs from
      what ``scripts/generate_agent_workflows.py`` would write today. Detected
      by re-running the generator in ``--check`` mode against the target.
        - ``stale_override`` — a warn-mode replacement override still composes, but
            its recorded upstream digest no longer matches the current base skill.
    - ``config_drift`` — a managed MCP server in ``mcp_servers`` is no longer
      present (or no longer matches) in ``.mcp.json`` / ``.vscode/mcp.json``.
      Only checked when ``mcp_servers`` is provided.
        - ``pin_target_drift`` — a plugin marketplace pin no longer points at the
            expected base/effective generated tree for the current override state.
        - ``plugin_source_drift`` — a plugin marketplace pin resolves to a missing
            or incomplete plugin tree (missing plugin.json, .mcp.json, or skills).
    - ``state_drift`` — ``.task-state/handoff.db`` is missing even though the
      manifest's ``configs`` array recorded ``.mcp.json``. Suppressed when the
      install was ``--no-mcp-servers`` (no managed servers registered, so no
      state init was expected).
    - ``hook_adapter_drift`` — a compact-session Stop adapter that bootstrap
      installed (its target is in the manifest ``configs``) is missing or no
      longer matches the manifest-declared managed entry. Never-installed
      adapters stay optional and are not reported (WORKSTATE-REF-80 implementation note).
    Returns an empty list when everything is clean.
    """
    target = Path(target).resolve()
    findings: list[Finding] = []

    # Worktree short-circuit (M4): a bare linked worktree lacks the whole overlay,
    # so the marker/clone/surface checks below would emit a missing_clone +
    # surface_drift storm whose naive repair targets the worktree's own absent
    # clone. Emit a single actionable finding and stop; repair routes it to
    # adopt_worktree against the primary.
    unadopted_primary = _unadopted_worktree_primary(target)
    if unadopted_primary is not None:
        return [{"kind": "unadopted_worktree", "path": str(unadopted_primary)}]

    _migrate_legacy_manifest(target)
    manifest_path = target / BOOTSTRAP_MANIFEST_NAME
    if not manifest_path.is_file():
        findings.append({"kind": "missing_manifest", "path": BOOTSTRAP_MANIFEST_NAME})
        return findings

    manifest = json.loads(manifest_path.read_text())
    is_package_source = str(manifest.get("source_kind") or "git_overlay") == "package"
    clone = target.joinpath(*CLONE_SUBDIR)
    clone_resolved = clone.resolve(strict=False)

    # The package delivery source has no git clone and copies surfaces instead
    # of symlinking, so the clone/symlink drift checks below are git_overlay-only.
    if not is_package_source and not (clone / ".git").exists():
        findings.append({"kind": "missing_clone", "path": "/".join(CLONE_SUBDIR)})

    surfaces = manifest.get("surfaces") or []
    for entry in surfaces:
        if entry.get("source") != "shared":
            continue
        surface = entry.get("path", "")
        link = target / surface
        if is_package_source:
            # Copied surface: drift only when the materialized path is gone.
            if not link.exists():
                findings.append({"kind": "surface_drift", "path": surface})
            continue
        if not link.is_symlink():
            findings.append({"kind": "surface_drift", "path": surface})
            continue
        try:
            resolved = link.resolve(strict=False)
        except OSError:
            findings.append({"kind": "surface_drift", "path": surface})
            continue
        in_clone = resolved == (clone / surface).resolve(strict=False) or str(
            resolved
        ).startswith(str(clone_resolved) + os.sep)
        if not in_clone:
            findings.append({"kind": "surface_drift", "path": surface})

    findings.extend(
        _doctor_local_surfaces(
            target, clone, manifest, is_package_source=is_package_source
        )
    )

    override_root = _discover_plugin_override_root(
        target,
        manifest=manifest,
        plugin_overrides=plugin_overrides,
    )

    # Generated-surface, hidden-override, and stop-adapter re-checks re-run the
    # generator / read the portable manifest from the git clone, which the
    # package source does not keep. They are git_overlay-only for now; the
    # consumer-tree checks below (plugin pins, codex activation, plugin source
    # integrity, override state, handoff state) apply to both sources.
    if not is_package_source:
        findings.extend(
            _doctor_generated_surfaces(target, clone, manifest, override_root)
        )

    if mcp_servers:
        findings.extend(_doctor_mcp_config_drift(target, manifest, mcp_servers))

    findings.extend(_doctor_plugin_pin_targets(target, override_root))
    findings.extend(_doctor_codex_activation_config(target))
    effective_missing = _doctor_effective_tree_missing(target)
    if effective_missing:
        # Whole-tree-missing subsumes per-pin plugin_source_drift noise.
        findings.extend(effective_missing)
    else:
        findings.extend(_doctor_plugin_source_integrity(target))
    if not is_package_source:
        findings.extend(
            _doctor_hidden_override_collisions(target, clone, override_root)
        )
    findings.extend(_doctor_plugin_override_state(target, override_root))
    if not is_package_source:
        findings.extend(_doctor_managed_stop_adapters(target, clone, manifest))
    findings.extend(_doctor_state(target, manifest))

    return findings


def _hash_local_files(root: Path) -> dict[str, str] | None:
    """Map of payload-relative path -> git blob OID for a surface's content.

    Works for a single file (key ``""``) or a directory tree. Returns ``None``
    when hashing fails (git unavailable or unreadable content) so the caller
    can skip classification instead of guessing.
    """
    import subprocess

    if root.is_file():
        files: dict[str, Path] = {"": root}
    else:
        files = {
            p.relative_to(root).as_posix(): p
            for p in sorted(root.rglob("*"))
            if p.is_file()
        }
    if not files:
        return {}
    try:
        out = subprocess.run(
            ["git", "hash-object", "--", *[str(p) for p in files.values()]],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return None
    if len(out) != len(files):
        return None
    return dict(zip(files.keys(), out))


def _payload_blob_index(
    clone: Path, sha: str, payload_rel: str
) -> dict[str, str] | None:
    """Blob OIDs for ``payload_rel`` at revision ``sha``, keyed like
    ``_hash_local_files`` (relative to the payload path, ``""`` for a file)."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "-C", str(clone), "ls-tree", "-r", sha, "--", payload_rel],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None
    index: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) != 3 or parts[1] != "blob":
            continue
        if path == payload_rel:
            rel = ""
        elif path.startswith(payload_rel + "/"):
            rel = path[len(payload_rel) + 1 :]
        else:
            continue
        index[rel] = parts[2]
    return index


def _strip_excluded_children(
    index: dict[str, str], excluded: frozenset[str]
) -> dict[str, str]:
    """Drop carve-excluded children from a surface blob index.

    A carved surface (:data:`SURFACE_CHILD_EXCLUSIONS`) is materialized
    *without* its excluded children, so comparing a local copy against the
    full payload tree would never match — every carved surface would
    misclassify as ``local_override``. Filtering both sides of the comparison
    restores the carve semantics: keys are payload-relative (``""`` for a
    file), so a key's first path component naming an excluded child drops it.
    """
    if not excluded:
        return index
    return {
        rel: oid for rel, oid in index.items() if rel.partition("/")[0] not in excluded
    }


def _matches_older_payload_revision(
    clone: Path,
    payload: Path,
    local_index: dict[str, str],
    *,
    excluded_children: frozenset[str] = frozenset(),
) -> bool:
    """True when the local content exactly matches the payload path at any
    revision in the clone's history — the update-starvation signature.

    ``excluded_children`` filters carve-excluded children out of each
    historical payload index (``local_index`` must already be filtered by the
    caller) so a carved surface can match the rest of an older revision even
    when that revision shipped the excluded children.
    """
    import subprocess

    try:
        payload_rel = payload.resolve().relative_to(clone.resolve()).as_posix()
    except ValueError:
        return False
    try:
        shas = subprocess.run(
            ["git", "-C", str(clone), "log", "--format=%H", "--", payload_rel],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()
    except (OSError, subprocess.CalledProcessError):
        return False
    for sha in shas:
        index = _payload_blob_index(clone, sha, payload_rel)
        if index is None:
            continue
        if _strip_excluded_children(index, excluded_children) == local_index:
            return True
    return False


def _doctor_local_surfaces(
    target: Path,
    clone: Path,
    manifest: dict[str, Any],
    *,
    is_package_source: bool,
) -> list[Finding]:
    """Classify ``source=local`` surfaces against the payload (implementation note S1).

    - identical to current payload -> ``local_redundant`` finding (adoption
      candidate)
    - identical to an *older* payload revision -> ``local_stale`` finding (a
      bootstrap-era copy silently starving updates)
    - matches no payload revision -> ``local_override`` with ``severity=info``:
      consumer-authored, respected, never repair-eligible, never affects the
      doctor exit code.

    History matching needs the git clone; the package source has none, so its
    divergent local content falls back to ``local_override`` (safe default)
    while ``local_redundant`` still works via filesystem comparison.
    """
    local_entries = [
        entry
        for entry in (manifest.get("surfaces") or [])
        if isinstance(entry, dict) and entry.get("source") == "local"
    ]
    if not local_entries:
        return []
    history_available = False
    if is_package_source:
        from workstate_bootstrap.install import _package_source_root

        try:
            source_root = _package_source_root(None)
        except Exception:
            return []
    else:
        if not (clone / ".git").exists():
            # missing_clone is already reported; nothing to diff against.
            return []
        source_root = clone
        history_available = True
    findings: list[Finding] = []
    for entry in local_entries:
        surface = entry.get("path", "")
        local = target / surface
        if not local.exists():
            # Broken symlink or deleted path: name it instead of skipping
            # silently (the never-silent goal of implementation note). Informational
            # only — nothing exists to drift-classify or adopt.
            findings.append(
                {"kind": "local_missing", "path": surface, "severity": "info"}
            )
            continue
        payload = _resolve_in_clone(source_root, surface)
        if not payload.exists():
            # No shipped counterpart: consumer-owned path, nothing to compare.
            continue
        local_index = _hash_local_files(local)
        if local_index is None:
            # Hashing failed (git unavailable / unreadable content): name the
            # surface rather than leaving it indistinguishable from clean.
            findings.append(
                {"kind": "local_unreadable", "path": surface, "severity": "info"}
            )
            continue
        # Carved surfaces are materialized without their excluded children;
        # compare both sides minus those children or the carve itself would
        # force every carved surface into local_override.
        excluded = SURFACE_CHILD_EXCLUSIONS.get(surface, frozenset())
        local_index = _strip_excluded_children(local_index, excluded)
        payload_index = _hash_local_files(payload)
        if payload_index is not None and local_index == _strip_excluded_children(
            payload_index, excluded
        ):
            findings.append({"kind": "local_redundant", "path": surface})
            continue
        if history_available and _matches_older_payload_revision(
            clone, payload, local_index, excluded_children=excluded
        ):
            findings.append({"kind": "local_stale", "path": surface})
            continue
        findings.append({"kind": "local_override", "path": surface, "severity": "info"})
    return findings


_MANAGED_SURFACE_BY_CONFIG_PATH: dict[str, str] = {
    ".mcp.json": "claude",
    ".vscode/mcp.json": "vscode",
    ".codex/config.toml": "codex",
}
_CONFIG_PATH_BY_MANAGED_SURFACE: dict[str, str] = {
    surface: path for path, surface in _MANAGED_SURFACE_BY_CONFIG_PATH.items()
}


def _registered_managed_surfaces(manifest: Mapping[str, object]) -> list[str]:
    """Return the managed-surface names recorded in the ledger's configs.

    Doctor / repair only reconcile surfaces that ``install`` actually
    wrote. Legacy ledgers without ``.codex/config.toml`` therefore skip
    codex even when the resolved map is supplied.
    """
    registered: list[str] = []
    for entry in manifest.get("configs") or []:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        surface = _MANAGED_SURFACE_BY_CONFIG_PATH.get(str(path))
        if surface is not None and surface not in registered:
            registered.append(surface)
    return registered


def _doctor_mcp_config_drift(
    target: Path,
    manifest: Mapping[str, object],
    mcp_servers: Mapping[str, Mapping[str, Any]],
) -> list[Finding]:
    """Run ``sync_mcp_configs(check_only=True)`` and translate per-surface
    drift into ``config_drift`` findings.

    Filtered to surfaces the ledger says ``install`` wrote so doctor
    does not invent drift for surfaces the consumer never opted into.
    """
    from workstate_bootstrap.mcp_sync import sync_mcp_configs

    surfaces = _registered_managed_surfaces(manifest)
    if not surfaces:
        return []
    report = sync_mcp_configs(target, mcp_servers, surfaces=surfaces, check_only=True)
    return [
        {"kind": "config_drift", "path": s.path} for s in report.surfaces if s.drift
    ]


def _doctor_state(target: Path, manifest: dict[str, object]) -> list[Finding]:
    # implementation note §4: the manifest's configs array records whether bootstrap
    # registered MCP servers. .mcp.json is only present when an mcp_servers
    # map was provided, so its presence is the gate for expecting init-state
    # to have run. --no-mcp-servers installs leave .mcp.json out of configs
    # and must not trigger state_drift.
    registered_mcp = any(
        isinstance(entry, dict) and entry.get("path") == ".mcp.json"
        for entry in manifest.get("configs") or []
    )
    if not registered_mcp:
        return []

    db_path = target / ".task-state" / "handoff.db"
    if db_path.is_file():
        return []
    return [{"kind": "state_drift", "path": ".task-state/handoff.db"}]


_COMPACT_SESSION_HOOK_ID = "compact-session"


def _compact_session_stop_adapters(clone: Path) -> dict[str, Mapping[str, Any]]:
    """Return the ``compact-session`` Stop adapters keyed by target path.

    Read from the portable-commands manifest in the clone — the same
    source ``install`` walks. Returns ``{}`` when the manifest predates
    schema v2 (no ``hooks`` array) so doctor stays a noop on legacy
    overlays.
    """
    from workstate_bootstrap.install import _load_portable_manifest

    portable = _load_portable_manifest(clone)
    adapters: dict[str, Mapping[str, Any]] = {}
    for hook in portable.get("hooks") or []:
        if not isinstance(hook, Mapping):
            continue
        if hook.get("hook_id") != _COMPACT_SESSION_HOOK_ID:
            continue
        for adapter in hook.get("adapters") or []:
            if not isinstance(adapter, Mapping):
                continue
            tgt = adapter.get("target")
            if isinstance(tgt, str):
                adapters[tgt] = adapter
    return adapters


def _managed_stop_adapter_drifted(
    settings_path: Path, adapter: Mapping[str, Any], *, target: Path
) -> bool:
    """True when the installed adapter file no longer carries the
    manifest-declared managed Stop entry.

    Drift covers: the file is gone, is unparseable, the patch container
    (e.g. ``$.hooks.Stop``) is missing, the managed entry (matched by
    ``match_key``) is absent, or it is present but differs from the
    rendered manifest entry. A present, exact-match entry is clean.
    """
    from workstate_bootstrap.install import _render_template

    if not settings_path.is_file():
        return True
    try:
        doc = json.loads(settings_path.read_text())
    except (OSError, ValueError):
        return True

    patch = adapter.get("patch") or {}
    json_path = str(patch.get("json_path", ""))
    match_key = patch.get("match_key")
    expected = _render_template(patch.get("entry"), target=target)
    if not json_path.startswith("$.") or match_key is None:
        return True
    if not isinstance(expected, Mapping) or match_key not in expected:
        return True

    node: Any = doc
    for seg in json_path[2:].split("."):
        if not isinstance(node, Mapping) or seg not in node:
            return True
        node = node[seg]
    if not isinstance(node, list):
        return True

    match_value = expected[match_key]
    for item in node:
        if isinstance(item, Mapping) and item.get(match_key) == match_value:
            return item != expected
    return True


def _doctor_managed_stop_adapters(
    target: Path, clone: Path, manifest: Mapping[str, object]
) -> list[Finding]:
    """Flag drift for compact-session Stop adapters that bootstrap installed.

    An adapter is *managed* only when its target appears in the install
    manifest's ``configs`` (i.e. an opt-in flag wrote it). Never-installed
    adapters stay optional and are not reported here — lifecycle doctor owns
    optional-not-installed visibility. See WORKSTATE-REF-80 implementation note.
    """
    adapters = _compact_session_stop_adapters(clone)
    if not adapters:
        return []
    installed_paths = {
        entry.get("path")
        for entry in manifest.get("configs") or []
        if isinstance(entry, dict)
    }
    findings: list[Finding] = []
    for tgt, adapter in adapters.items():
        if tgt not in installed_paths:
            continue
        if _managed_stop_adapter_drifted(target / tgt, adapter, target=target):
            findings.append({"kind": "hook_adapter_drift", "path": tgt})
    return findings


def _doctor_generated_surfaces(
    target: Path,
    clone: Path,
    manifest: dict[str, object],
    override_root: Path | None,
) -> list[Finding]:
    """Detect drift in per-agent generated surfaces.

    Runs ``scripts/generate_agent_workflows.py --check --target <target>``
    against the target. Each ``drift detected: <path>`` line in the
    generator's stderr is mapped back to the manifest's ``generated``
    surface that owns it; one ``generated_drift`` finding per affected
    surface (deduplicated). Silent when no generated surfaces are recorded
    (legacy manifests) or the generator script is missing from the clone
    (older overlay refs that pre-date implementation note).
    """
    import subprocess
    import sys

    surfaces = manifest.get("surfaces") or []
    generated_surfaces = [
        str(entry.get("path", ""))
        for entry in surfaces
        if entry.get("source") == "generated" and entry.get("path")
    ]
    if not generated_surfaces:
        return []

    plugin_root = Path(*PLUGIN_GENERATED_ROOT).as_posix()
    plugin_surfaces = [
        surface
        for surface in generated_surfaces
        if surface.startswith(plugin_root + "/")
    ]
    legacy_surfaces = [
        surface for surface in generated_surfaces if surface not in plugin_surfaces
    ]

    findings: list[Finding] = []
    if legacy_surfaces:
        findings.extend(
            _doctor_legacy_generated_surfaces(target, clone, legacy_surfaces)
        )
    if plugin_surfaces:
        findings.extend(
            _doctor_plugin_generated_surfaces(
                target,
                clone,
                manifest,
                plugin_surfaces,
                override_root,
            )
        )
    return findings


def _doctor_legacy_generated_surfaces(
    target: Path, clone: Path, generated_surfaces: list[str]
) -> list[Finding]:
    import subprocess
    import sys

    from workstate_bootstrap.install import _resolve_in_clone

    generator_script = _resolve_in_clone(clone, GENERATOR_SCRIPT)
    manifest_path = _resolve_in_clone(clone, GENERATOR_MANIFEST)
    skills_source = _resolve_in_clone(clone, GENERATOR_SKILLS_SOURCE)
    if not generator_script.is_file() or not manifest_path.is_file():
        return []

    try:
        proc = subprocess.run(
            [
                sys.executable,
                str(generator_script),
                "--manifest",
                str(manifest_path),
                "--skills-source-root",
                str(skills_source),
                "--target",
                str(target),
                "--check",
            ],
            cwd=str(clone),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        # Generator unavailable / hung — surface as a single coarse finding
        # rather than crashing doctor.
        return [
            {"kind": "generated_drift", "path": surface}
            for surface in generated_surfaces
        ]

    if proc.returncode == 0:
        return []

    # Parse "drift detected: <abs path>" lines and map each back to the
    # manifest surface whose target-relative path it sits under.
    drifted: set[str] = set()
    for line in (proc.stderr or "").splitlines():
        line = line.strip()
        if not line.startswith("drift detected:"):
            continue
        drifted_path = line.split(":", 1)[1].strip()
        try:
            rel = Path(drifted_path).resolve().relative_to(target)
        except (ValueError, OSError):
            continue
        rel_str = str(rel)
        for surface in generated_surfaces:
            if rel_str == surface or rel_str.startswith(surface.rstrip("/") + "/"):
                drifted.add(surface)
                break

    if not drifted:
        # Generator reported failure but we couldn't map any path. Flag
        # all generated surfaces coarsely so the operator knows there's
        # work to do.
        drifted.update(generated_surfaces)

    return [{"kind": "generated_drift", "path": surface} for surface in sorted(drifted)]


def _doctor_plugin_generated_surfaces(
    target: Path,
    clone: Path,
    manifest: Mapping[str, object],
    generated_surfaces: list[str],
    override_root: Path | None,
) -> list[Finding]:
    import subprocess
    import sys

    from workstate_bootstrap.install import _resolve_in_clone

    generator_script = _resolve_in_clone(clone, GENERATOR_SCRIPT)
    manifest_path = _resolve_in_clone(clone, GENERATOR_MANIFEST)
    skills_source = _resolve_in_clone(clone, GENERATOR_SKILLS_SOURCE)
    if not generator_script.is_file() or not manifest_path.is_file():
        return []

    remote_sha = manifest.get("remote_sha")
    findings: list[Finding] = []
    base_surface = Path(*PLUGIN_GENERATED_ROOT, "base").as_posix()
    effective_surface = Path(*PLUGIN_GENERATED_ROOT, "effective").as_posix()

    for surface in generated_surfaces:
        cmd = [
            sys.executable,
            str(generator_script),
            "--mode=plugin",
            "--manifest",
            str(manifest_path),
            "--skills-source-root",
            str(skills_source),
            "--plugin-out",
            str(target / surface),
            "--check",
        ]

        if surface == effective_surface:
            if not isinstance(remote_sha, str):
                findings.append({"kind": "generated_drift", "path": surface})
                continue
            if override_root is None:
                cmd.append("--plugin-passthrough-lock")
            else:
                cmd.extend(["--plugin-overrides", str(override_root)])
            cmd.extend(["--plugin-base-remote-sha", remote_sha])
        elif surface != base_surface:
            findings.append({"kind": "generated_drift", "path": surface})
            continue

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(clone),
                capture_output=True,
                text=True,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            findings.append({"kind": "generated_drift", "path": surface})
            continue

        if proc.returncode != 0 and not _plugin_check_reports_only_stale_override(
            target / surface, proc.stderr
        ):
            findings.append({"kind": "generated_drift", "path": surface})

    return findings


def _plugin_check_reports_only_stale_override(plugin_root: Path, stderr: str) -> bool:
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    drift_markers = (
        "Plugin tree is out of sync",
        "missing plugin tree file:",
        "plugin tree drift:",
    )
    if any(any(marker in line for marker in drift_markers) for line in lines):
        return False

    lock_path = plugin_root / "plugin-lock.json"
    if not lock_path.is_file():
        return False

    try:
        payload = json.loads(lock_path.read_text())
    except json.JSONDecodeError:
        return False

    components = payload.get("components", [])
    # WORKSTATE-REF-07: merge_conflict (skill patch mode) is, like stale, an override
    # condition — not generated-tree drift. The dedicated doctor finding kinds
    # report it; generated_drift must not double-report.
    return any(
        isinstance(entry, dict) and entry.get("status") in {"stale", "merge_conflict"}
        for entry in components
    )


def _doctor_plugin_pin_targets(
    target: Path, override_root: Path | None
) -> list[Finding]:
    findings: list[Finding] = []
    # WORKSTATE-REF-07 always-effective: the expected pin target is the effective tree
    # in every state; legacy base/ pins surface as repairable pin_target_drift.
    plugin_tree_kind = "effective"

    claude_path = target / CLAUDE_MARKETPLACE_PATH
    if claude_path.is_file():
        try:
            claude_payload = json.loads(claude_path.read_text())
        except json.JSONDecodeError:
            claude_payload = {}
        plugins = claude_payload.get("plugins")
        expected = _relative_plugin_tree_path(plugin_tree_kind, "claude")
        actual = None
        if isinstance(plugins, list):
            for plugin in plugins:
                if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME:
                    actual = plugin.get("source")
                    break
        if actual != expected:
            findings.append(
                {"kind": "pin_target_drift", "path": CLAUDE_MARKETPLACE_PATH.as_posix()}
            )

    codex_path = target / CODEX_MARKETPLACE_PATH
    if codex_path.is_file():
        try:
            codex_payload = json.loads(codex_path.read_text())
        except json.JSONDecodeError:
            codex_payload = {}
        plugins = codex_payload.get("plugins")
        expected = {
            "source": "local",
            "path": _relative_plugin_tree_path(plugin_tree_kind, "codex"),
        }
        actual = None
        if isinstance(plugins, list):
            for plugin in plugins:
                if isinstance(plugin, dict) and plugin.get("name") == PLUGIN_NAME:
                    actual = plugin.get("source")
                    break
        if actual != expected:
            findings.append(
                {"kind": "pin_target_drift", "path": CODEX_MARKETPLACE_PATH.as_posix()}
            )

    return findings


def _doctor_codex_activation_config(target: Path) -> list[Finding]:
    if not (target / CODEX_MARKETPLACE_PATH).is_file():
        return []

    path = target / CODEX_CONFIG_PATH
    if not path.is_file():
        return [
            {"kind": "codex_activation_drift", "path": CODEX_CONFIG_PATH.as_posix()}
        ]

    problems: list[str] = []
    try:
        payload = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [
            {
                "kind": "codex_activation_drift",
                "path": CODEX_CONFIG_PATH.as_posix(),
                "message": f"invalid TOML: {exc}",
            }
        ]

    marketplaces = payload.get("marketplaces")
    if not isinstance(marketplaces, dict):
        problems.append("marketplaces must be a table")
        marketplace = None
    else:
        marketplace = marketplaces.get(PLUGIN_MARKETPLACE_NAME)
    if not isinstance(marketplace, dict):
        problems.append(f"missing marketplaces.{PLUGIN_MARKETPLACE_NAME}")
    else:
        if marketplace.get("source_type") != "local":
            problems.append(
                f"marketplaces.{PLUGIN_MARKETPLACE_NAME}.source_type must be local"
            )
        if marketplace.get("source") != ".":
            problems.append(f"marketplaces.{PLUGIN_MARKETPLACE_NAME}.source must be .")

    selector = f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE_NAME}"
    plugins = payload.get("plugins")
    if not isinstance(plugins, dict):
        problems.append("plugins must be a table")
        plugin = None
    else:
        plugin = plugins.get(selector)
    if not isinstance(plugin, dict) or not isinstance(plugin.get("enabled"), bool):
        problems.append(f'plugins."{selector}".enabled must be a boolean')

    if not problems:
        return []
    return [
        {
            "kind": "codex_activation_drift",
            "path": CODEX_CONFIG_PATH.as_posix(),
            "message": "; ".join(problems),
        }
    ]


def _resolve_plugin_source_path(
    target: Path,
    raw_path: object,
    *,
    field_name: str,
) -> tuple[Path | None, list[str]]:
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None, [f"{field_name} must be a non-empty string"]
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return None, [f"{field_name} must be relative"]
    if ".." in candidate.parts:
        return None, [f"{field_name} must not traverse outside the repo"]
    resolved = (target / raw_path.removeprefix("./")).resolve(strict=False)
    try:
        resolved.relative_to(target.resolve())
    except ValueError:
        return None, [f"{field_name} resolves outside the repo"]
    return resolved, []


def _plugin_tree_integrity_problems(root: Path, harness: str) -> list[str]:
    manifest_dir = ".claude-plugin" if harness == "claude" else ".codex-plugin"
    problems: list[str] = []
    if not root.is_dir():
        return [f"source path does not exist: {root}"]
    if not (root / manifest_dir / "plugin.json").is_file():
        problems.append(f"missing {manifest_dir}/plugin.json")
    if not (root / ".mcp.json").is_file():
        problems.append("missing .mcp.json")
    skills_root = root / "skills"
    if not skills_root.is_dir():
        problems.append("missing skills/")
    elif not any(path.is_file() for path in skills_root.glob("*/SKILL.md")):
        problems.append("skills/ contains no SKILL.md entries")
    return problems


def _doctor_effective_tree_missing(target: Path) -> list[Finding]:
    """WORKSTATE-REF-07 fresh-clone diagnosis: pins always target the effective tree,
    so an entirely absent effective root means install/update has not
    materialized the generated target yet. Subsumes per-pin source-drift
    noise for that state."""
    effective_root = target.joinpath(*PLUGIN_GENERATED_ROOT, "effective")
    if effective_root.is_dir():
        return []
    if (
        not (target / CLAUDE_MARKETPLACE_PATH).is_file()
        and not (target / CODEX_MARKETPLACE_PATH).is_file()
    ):
        return []
    try:
        display = effective_root.relative_to(target).as_posix()
    except ValueError:
        display = str(effective_root)
    return [
        {
            "kind": "effective_tree_missing",
            "path": display,
            "message": (
                "effective plugin tree is missing; run workstate-bootstrap "
                "install or update (or repair) to materialize it"
            ),
        }
    ]


def _doctor_plugin_source_integrity(target: Path) -> list[Finding]:
    findings: list[Finding] = []

    claude_path = target / CLAUDE_MARKETPLACE_PATH
    if claude_path.is_file():
        problems: list[str] = []
        try:
            payload = json.loads(claude_path.read_text())
        except json.JSONDecodeError as exc:
            payload = {}
            problems.append(f"invalid JSON: {exc}")
        plugins = payload.get("plugins") if isinstance(payload, dict) else None
        if not isinstance(plugins, list):
            problems.append("plugins must be a list")
        else:
            for plugin in plugins:
                if not isinstance(plugin, dict) or plugin.get("name") != PLUGIN_NAME:
                    continue
                root, path_problems = _resolve_plugin_source_path(
                    target,
                    plugin.get("source"),
                    field_name="plugins[].source",
                )
                problems.extend(path_problems)
                if root is not None:
                    problems.extend(_plugin_tree_integrity_problems(root, "claude"))
                break
            else:
                problems.append(f"missing {PLUGIN_NAME} plugin entry")
        if problems:
            findings.append(
                {
                    "kind": "plugin_source_drift",
                    "path": CLAUDE_MARKETPLACE_PATH.as_posix(),
                    "message": "; ".join(problems),
                }
            )

    codex_path = target / CODEX_MARKETPLACE_PATH
    if codex_path.is_file():
        problems = []
        try:
            payload = json.loads(codex_path.read_text())
        except json.JSONDecodeError as exc:
            payload = {}
            problems.append(f"invalid JSON: {exc}")
        plugins = payload.get("plugins") if isinstance(payload, dict) else None
        if not isinstance(plugins, list):
            problems.append("plugins must be a list")
        else:
            for plugin in plugins:
                if not isinstance(plugin, dict) or plugin.get("name") != PLUGIN_NAME:
                    continue
                source = plugin.get("source")
                if not isinstance(source, dict) or source.get("source") != "local":
                    problems.append("plugins[].source.source must be local")
                    break
                root, path_problems = _resolve_plugin_source_path(
                    target,
                    source.get("path"),
                    field_name="plugins[].source.path",
                )
                problems.extend(path_problems)
                if root is not None:
                    problems.extend(_plugin_tree_integrity_problems(root, "codex"))
                break
            else:
                problems.append(f"missing {PLUGIN_NAME} plugin entry")
        if problems:
            findings.append(
                {
                    "kind": "plugin_source_drift",
                    "path": CODEX_MARKETPLACE_PATH.as_posix(),
                    "message": "; ".join(problems),
                }
            )

    return findings


def _doctor_hidden_override_collisions(
    target: Path, clone: Path, override_root: Path | None
) -> list[Finding]:
    if override_root is None:
        return []

    override_manifest_path = override_root / PLUGIN_OVERRIDE_MANIFEST
    if not override_manifest_path.is_file():
        return []

    try:
        override_manifest = yaml.safe_load(override_manifest_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return []

    declared_paths: set[str] = set()
    components = override_manifest.get("components")
    if isinstance(components, dict):
        skills = components.get("skills")
        if isinstance(skills, dict):
            for spec in skills.values():
                if not isinstance(spec, dict):
                    continue
                path = spec.get("path")
                if isinstance(path, str) and path:
                    declared_paths.add(path)

    override_skills_root = override_root / "skills"
    if not override_skills_root.is_dir():
        return []

    skills_source_root = _resolve_in_clone(clone, GENERATOR_SKILLS_SOURCE)
    if not skills_source_root.is_dir():
        return []

    findings: list[Finding] = []
    for candidate in sorted(override_skills_root.glob("*/SKILL.md")):
        relative_path = candidate.relative_to(override_root).as_posix()
        if relative_path in declared_paths:
            continue
        if not (skills_source_root / candidate.parent.name).is_dir():
            continue
        findings.append(
            {
                "kind": "hidden_override_collision",
                "path": _plugin_override_display_path(
                    target, override_root, relative_path
                ),
            }
        )

    return findings


def _plugin_override_display_path(
    target: Path, override_root: Path | None, relative_path: str
) -> str:
    if override_root is None:
        return Path(*PLUGIN_OVERRIDE_ROOT, relative_path).as_posix()

    candidate = override_root / relative_path
    try:
        return candidate.relative_to(target).as_posix()
    except ValueError:
        return candidate.as_posix()


def _doctor_plugin_override_state(
    target: Path, override_root: Path | None
) -> list[Finding]:
    lock_path = target.joinpath(*PLUGIN_GENERATED_ROOT, "effective", "plugin-lock.json")

    findings: list[Finding] = []

    if lock_path.is_file():
        try:
            payload = json.loads(lock_path.read_text())
        except json.JSONDecodeError:
            payload = {}

        # WORKSTATE-REF-07: merge_conflict (skill patch mode) gets its own finding kind
        # alongside the existing stale_override reporting.
        status_kinds = {
            "stale": "stale_override",
            "merge_conflict": "override_merge_conflict",
        }
        for entry in payload.get("components", []):
            if not isinstance(entry, dict):
                continue
            kind = status_kinds.get(entry.get("status"))
            if kind is None:
                continue
            override_path = entry.get("override_path")
            if not isinstance(override_path, str) or not override_path:
                continue
            findings.append(
                {
                    "kind": kind,
                    "path": _plugin_override_display_path(
                        target, override_root, override_path
                    ),
                }
            )

    override_lock_path = (
        None if override_root is None else override_root / "overrides.lock.json"
    )
    if not override_lock_path or not override_lock_path.is_file():
        return findings

    try:
        override_payload = json.loads(override_lock_path.read_text())
    except json.JSONDecodeError:
        return findings

    unsafe_op_names = {
        "replace_command",
        "replace_args",
        "append_args",
        "upsert_env",
        "remove_env",
    }
    seen_paths: set[str] = set()
    for entry in override_payload.get("components", []):
        if not isinstance(entry, dict) or entry.get("component_kind") != "mcp_server":
            continue
        patch_path = entry.get("patch_path")
        if not isinstance(patch_path, str) or not patch_path:
            continue

        patch_file = override_root / patch_path
        display_path = _plugin_override_display_path(target, override_root, patch_path)
        try:
            patch_payload = yaml.safe_load(patch_file.read_text()) or {}
        except (OSError, yaml.YAMLError):
            if display_path not in seen_paths:
                seen_paths.add(display_path)
                findings.append(
                    {"kind": "invalid_override_schema", "path": display_path}
                )
            continue

        ops = patch_payload.get("ops")
        if not isinstance(ops, list):
            if display_path not in seen_paths:
                seen_paths.add(display_path)
                findings.append(
                    {"kind": "invalid_override_schema", "path": display_path}
                )
            continue
        if not any(
            isinstance(op, dict) and op.get("op") in unsafe_op_names for op in ops
        ):
            continue

        if display_path in seen_paths:
            continue
        seen_paths.add(display_path)
        findings.append({"kind": "unsafe_tool_patch", "path": display_path})

    return findings


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def update(
    *,
    target: Path,
    remote_ref: str,
    remote_url: str | None = None,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None = None,
    plugin_overrides: Path | None = None,
    reset_overrides: bool = False,
    backup_overrides: bool = False,
    enforce_required_surfaces: bool = True,
) -> dict[str, object]:
    """Re-run ``install`` against ``target`` at a new ``remote_ref``.

    ``remote_url`` defaults to whatever the current manifest already records,
    so callers don't need to repeat it. When ``mcp_servers`` is omitted,
    managed installs preserve their existing ``.mcp.json`` registration so
    config surfaces and init-state still refresh. ``enforce_required_surfaces``
    defaults to ``True`` to match the install CLI contract.
    """
    from workstate_bootstrap.install import _discover_plugin_override_root, install

    target = Path(target).resolve()
    manifest = _load_manifest(target)
    if str(manifest.get("source_kind") or "git_overlay") == "package":
        raise ValueError(
            "update is not supported for a package-source install "
            "(source_kind=package has no remote overlay to refresh). Re-run "
            "`workstate-bootstrap install --source package --target <dir>` to "
            "refresh surfaces from the installed workstate-system distribution."
        )
    if remote_url is None:
        remote_url = str(manifest["remote_url"])
    if mcp_servers is None:
        mcp_servers = _preserved_mcp_servers(target, manifest)
    override_root = _discover_plugin_override_root(
        target,
        manifest=manifest,
        plugin_overrides=plugin_overrides,
    )

    return install(
        target=target,
        remote_url=remote_url,
        remote_ref=remote_ref,
        mcp_servers=mcp_servers,
        plugin_overrides=override_root,
        reset_overrides=reset_overrides,
        backup_overrides=backup_overrides,
        enforce_required_surfaces=enforce_required_surfaces,
    )


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def _backup_local_surface(target: Path, surface: str) -> Path:
    """Copy a local surface to ``.workstate/backup/<utc-ts>/<surface>``."""
    import shutil
    from datetime import datetime, timezone

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = target / ".workstate" / "backup" / ts / surface
    dest.parent.mkdir(parents=True, exist_ok=True)
    src = target / surface
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
    return dest


def _restore_local_surface(backup: Path, local: Path) -> None:
    """Best-effort rollback: put the backed-up local copy back at ``local``.

    Used when adoption fails between deleting the local surface and finishing
    re-materialization, so a failed adoption never leaves the surface absent.
    """
    import shutil

    if local.is_symlink() or local.is_file():
        local.unlink()
    elif local.is_dir():
        shutil.rmtree(local)
    local.parent.mkdir(parents=True, exist_ok=True)
    if backup.is_dir():
        shutil.copytree(backup, local)
    else:
        shutil.copy2(backup, local)


def _rematerialize_carved_parent(
    target: Path, surface: str, payload: Path, *, is_package_source: bool
) -> list[dict[str, str]]:
    """Re-materialize an adopted carved surface in install's carved form.

    A whole-directory symlink/copy would re-expose the carve-excluded
    children, so the parent becomes a real directory whose non-excluded
    children are symlinked (git overlay) or copied (package source), exactly
    like :func:`workstate_bootstrap.install._materialize_surfaces` /
    ``_materialize_surfaces_copy``. Lifecycle-hoist children are copied as
    real files (the lifecycle pass owns them at install time and does not
    re-run under repair). Returns the per-child manifest entries that replace
    the adopted parent's ``source=local`` receipt entry.
    """
    import shutil

    from workstate_bootstrap.install import (
        _carved_surface_children,
        _lifecycle_hoist_children,
    )

    local = target / surface
    local.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, str]] = []
    for child in _carved_surface_children(surface, payload):
        dest = local / child.name
        if is_package_source:
            if child.is_dir():
                shutil.copytree(child, dest)
            else:
                shutil.copy2(child, dest)
        else:
            rel = os.path.relpath(child, dest.parent)
            dest.symlink_to(rel, target_is_directory=child.is_dir())
        entries.append({"path": f"{surface}/{child.name}", "source": "shared"})
    for child_name in sorted(_lifecycle_hoist_children(surface)):
        src = payload / child_name
        if not src.exists():
            continue
        dest = local / child_name
        if src.is_dir():
            shutil.copytree(src, dest)
        else:
            shutil.copy2(src, dest)
        entries.append({"path": f"{surface}/{child_name}", "source": "lifecycle"})
    return entries


def repair(
    *,
    target: Path,
    force_dirty: bool = False,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None = None,
    plugin_overrides: Path | None = None,
    adopt_stale_local: list[str] | None = None,
) -> dict[str, list[Finding]]:
    """Restore drifted overlay surfaces flagged by :func:`doctor`.

    For each surface flagged as ``surface_drift``:

    - If the path no longer exists or is a broken/foreign symlink, the
      canonical symlink into the clone is recreated.
    - If the path has been replaced by a real directory or file, the surface
      is **skipped** unless ``force_dirty=True`` (per regression guard rg-017
      "never silently force-remove dirty content"). With ``force_dirty=True``
      the dirty content is removed and the symlink reinstated.

    Config drift (``.mcp.json`` / ``.vscode/mcp.json``) is repaired by
    re-running the install-time writers when ``mcp_servers`` is supplied.

    ``adopt_stale_local`` (implementation note implementation note) opts specific surfaces into
    adoption: a surface doctor classified ``local_stale`` or
    ``local_redundant`` is backed up to ``.workstate/backup/<ts>/<surface>``,
    re-materialized as the managed surface (symlink for the git overlay
    source, copy for the package source), and its receipt entry flips back to
    ``source=shared``. Never automatic; ``local_override`` surfaces are
    excluded even when requested.

    Returns a report dict ``{"repaired": [...], "skipped": [...]}`` whose
    entries reuse the :func:`doctor` finding shape.
    """
    from workstate_bootstrap.install import (
        _run_generator,
        _set_git_hooks_path,
        _write_plugin_pins,
        _discover_plugin_override_root,
    )
    from workstate_bootstrap.mcp_sync import sync_mcp_configs
    import shutil

    target = Path(target).resolve()

    # Worktree short-circuit (M4): an unadopted linked worktree is repaired by
    # adopting the overlay from its primary, not by the surface-drift path below
    # (which would target the worktree's own absent clone). Runs before
    # _load_manifest, which would fail when the marker is gitignored.
    unadopted_primary = _unadopted_worktree_primary(target)
    if unadopted_primary is not None:
        from workstate_bootstrap.adopt import adopt_worktree

        # force_dirty / mcp_servers / plugin_overrides are intentionally NOT
        # threaded here: adoption never force-removes local content (it honors
        # foreign-precedence), so the surface-drift force-overwrite path has no
        # analog for a bare unadopted worktree.
        adopt_worktree(target=target, primary=unadopted_primary)
        return {
            "repaired": [
                {"kind": "unadopted_worktree", "path": str(unadopted_primary)}
            ],
            "skipped": [],
        }

    manifest = _load_manifest(target)
    override_root = _discover_plugin_override_root(
        target,
        manifest=manifest,
        plugin_overrides=plugin_overrides,
    )
    findings = doctor(
        target=target,
        mcp_servers=mcp_servers,
        plugin_overrides=override_root,
    )
    repaired: list[Finding] = []
    skipped: list[Finding] = []

    if not findings:
        return {"repaired": repaired, "skipped": skipped}

    clone = target.joinpath(*CLONE_SUBDIR)

    config_drift_paths = {f["path"] for f in findings if f["kind"] == "config_drift"}
    if config_drift_paths and mcp_servers:
        drifted_surfaces = [
            _MANAGED_SURFACE_BY_CONFIG_PATH[p]
            for p in config_drift_paths
            if p in _MANAGED_SURFACE_BY_CONFIG_PATH
        ]
        if drifted_surfaces:
            sync_mcp_configs(
                target, mcp_servers, surfaces=drifted_surfaces, check_only=False
            )

    for finding in findings:
        kind = finding["kind"]
        path = finding["path"]

        if finding.get("severity") == "info":
            # Informational notes (local_override) are never repair-eligible:
            # consumer-authored content in a managed path is respected.
            continue

        if kind in {"local_stale", "local_redundant"}:
            if not adopt_stale_local or path not in adopt_stale_local:
                # Adoption is strictly opt-in (implementation note implementation note).
                skipped.append(finding)
                continue
            from workstate_bootstrap.install import _resolve_in_clone

            is_package_source = (
                str(manifest.get("source_kind") or "git_overlay") == "package"
            )
            if is_package_source:
                from workstate_bootstrap.install import _package_source_root

                source_root = _package_source_root(None)
            else:
                source_root = clone
            payload = _resolve_in_clone(source_root, path)
            if not payload.exists():
                skipped.append(finding)
                continue
            local = target / path
            try:
                backup = _backup_local_surface(target, path)
            except OSError:
                skipped.append(finding)
                continue
            # Per-child entries for an adopted carved parent, else None
            # (plain surface: flip the existing entry in place).
            new_entries: list[dict[str, str]] | None = None
            try:
                if local.is_symlink() or local.is_file():
                    local.unlink()
                elif local.is_dir():
                    shutil.rmtree(local)
                local.parent.mkdir(parents=True, exist_ok=True)
                if path in SURFACE_CHILD_EXCLUSIONS:
                    # Carved surface: a whole-directory symlink/copy would
                    # re-expose the carve-excluded children; re-materialize
                    # in install's carved per-child form instead.
                    new_entries = _rematerialize_carved_parent(
                        target, path, payload, is_package_source=is_package_source
                    )
                elif is_package_source:
                    if payload.is_dir():
                        shutil.copytree(payload, local)
                    else:
                        shutil.copy2(payload, local)
                else:
                    rel = os.path.relpath(payload, local.parent)
                    local.symlink_to(rel, target_is_directory=payload.is_dir())
            except OSError:
                # Mid-re-materialization failure must not strand the surface
                # deleted: restore the backup we just took and report the
                # adoption as skipped instead of half-done.
                try:
                    _restore_local_surface(backup, local)
                except OSError:
                    pass  # backup itself remains under .workstate/backup/
                skipped.append(finding)
                continue
            surfaces_list = manifest.get("surfaces")
            if isinstance(surfaces_list, list):
                if new_entries is None:
                    for entry in surfaces_list:
                        if isinstance(entry, dict) and entry.get("path") == path:
                            entry["source"] = "shared"
                else:
                    # Carved parent adopted: replace its parent-level local
                    # entry with the per-child entries install would record.
                    surfaces_list[:] = [
                        e
                        for e in surfaces_list
                        if not (isinstance(e, dict) and e.get("path") == path)
                    ]
                    existing = {
                        e.get("path"): e for e in surfaces_list if isinstance(e, dict)
                    }
                    for new_entry in new_entries:
                        current = existing.get(new_entry["path"])
                        if current is not None:
                            current["source"] = new_entry["source"]
                        else:
                            surfaces_list.append(new_entry)
                (target / BOOTSTRAP_MANIFEST_NAME).write_text(
                    json.dumps(manifest, indent=2) + "\n"
                )
            repaired.append(finding)
            continue

        if kind == "surface_drift":
            from workstate_bootstrap.install import _resolve_in_clone

            link = target / path
            remote_path = _resolve_in_clone(clone, path)
            if not remote_path.exists():
                skipped.append(finding)
                continue

            if link.is_symlink() or not link.exists():
                # Broken/foreign symlink or missing entirely: safe to replace.
                if link.is_symlink():
                    link.unlink()
            elif link.is_dir() and any(link.iterdir()):
                if not force_dirty:
                    skipped.append(finding)
                    continue
                shutil.rmtree(link)
            elif link.is_dir():
                link.rmdir()
            else:
                if not force_dirty:
                    skipped.append(finding)
                    continue
                link.unlink()

            link.parent.mkdir(parents=True, exist_ok=True)
            rel = os.path.relpath(remote_path, link.parent)
            link.symlink_to(rel, target_is_directory=True)
            repaired.append(finding)
            continue

        if kind in {"generated_drift", "plugin_source_drift"}:
            # Re-run the generator once for the whole batch — it rewrites
            # every per-agent surface from the canonical source. Collapse
            # subsequent generated/source findings into the same repair op.
            if any(
                f["kind"] in {"generated_drift", "plugin_source_drift"}
                for f in repaired
            ):
                repaired.append(finding)
                continue
            try:
                remote_sha = manifest.get("remote_sha")
                if not isinstance(remote_sha, str):
                    raise ValueError("install manifest missing remote_sha")
                _run_generator(target, clone, remote_sha, override_root)
                _write_plugin_pins(target, override_root)
            except Exception:
                skipped.append(finding)
                continue
            repaired.append(finding)
            continue

        if kind in {"pin_target_drift", "codex_activation_drift"}:
            if any(
                f["kind"] in {"pin_target_drift", "codex_activation_drift"}
                for f in repaired
            ):
                repaired.append(finding)
                continue
            try:
                _write_plugin_pins(target, override_root)
            except Exception:
                skipped.append(finding)
                continue
            repaired.append(finding)
            continue

        if kind == "config_drift":
            if mcp_servers and path in _MANAGED_SURFACE_BY_CONFIG_PATH:
                repaired.append(finding)
            else:
                skipped.append(finding)
            continue

        if kind == "hook_adapter_drift":
            # Re-apply the manifest-declared managed Stop entry. The walker's
            # merge is idempotent and preserves unrelated user entries, so
            # restoring a drifted managed adapter is safe without force_dirty.
            from workstate_bootstrap.install import _apply_merge_array_entry

            adapter = _compact_session_stop_adapters(clone).get(path)
            if adapter is None:
                skipped.append(finding)
                continue
            try:
                _apply_merge_array_entry(adapter, target=target)
            except Exception:
                skipped.append(finding)
                continue
            repaired.append(finding)
            continue

        # missing_clone / missing_manifest are out of scope here — caller
        # should run install/update instead. Surface as skipped.
        skipped.append(finding)

    # Refresh git hooks path defensively when we touched anything.
    if repaired and (target / ".git").exists():
        try:
            _set_git_hooks_path(target)
        except Exception:
            pass

    return {"repaired": repaired, "skipped": skipped}
