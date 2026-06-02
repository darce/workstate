"""Minimal install flow for the workstate-bootstrap CLI.

This slice implements four responsibilities:

1. Clone (or fast-forward) ``<remote_url>`` at ``<remote_ref>`` into
   ``<target>/.workstate/remote/``.
2. Symlink the six known shared overlay surfaces from the clone into the
   consumer repo, preserving any pre-existing real local directory at the
   same path (overlay precedence: local wins per surface).
3. When ``mcp_servers`` is provided, configure the three consumer-tool
   surfaces — ``.mcp.json`` (Claude Code), ``.vscode/mcp.json`` (VS Code),
   and ``.codex/config.toml`` (Codex CLI) — by deep-merging or
   tomlkit-replacing only the managed entries while preserving everything
   else the user had configured.
4. When ``<target>`` is a git repo, point ``core.hooksPath`` at the
   materialized ``scripts/hooks/git`` directory so git resolves shared
   hooks by name (``post-checkout``, ``pre-commit``, ``pre-push`` …).
   The parent ``scripts/hooks/`` symlink ships Python helpers and other
   non-git-hook files; setting ``core.hooksPath`` there makes git
    silently resolve nothing; the bootstrap-managed git hook directory is the
    only valid hooksPath target.
5. Write ``<target>/.workstate-bootstrap.json`` describing the resolved remote,
   the materialized surfaces, and the configs that were touched. Older
   installs wrote ``.workstate-overlay.json``; the legacy file is migrated
   in-place on first run when present.

The ``doctor`` / ``repair`` / ``update`` / ``status`` subcommands are
implemented in adjacent modules and are deliberately out of scope here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from collections.abc import Iterable
from typing import Any, Mapping

import tomlkit
import yaml

from workstate_protocol import (
    CONTRACTS_DIR,
    RULES_DIR,
    RUNTIME_ROOT_DIRNAME,
)

BOOTSTRAP_MANIFEST_NAME = ".workstate-bootstrap.json"
LEGACY_OVERLAY_MANIFEST_NAME = ".workstate-overlay.json"
# Deprecated alias kept for downstream code importing the old name. Points
# at the canonical (new) filename, NOT the legacy file. Reading legacy
# installs goes through _migrate_legacy_manifest below.
OVERLAY_MANIFEST_NAME = BOOTSTRAP_MANIFEST_NAME
SCHEMA_VERSION = 2
CLONE_SUBDIR = (RUNTIME_ROOT_DIRNAME, "remote")


def _build_install_manifest(
    *,
    remote_url: str | None = None,
    remote_ref: str | None = None,
    remote_sha: str | None = None,
    source_kind: str = "git_overlay",
    package_version: str | None = None,
    profile: str,
    surfaces: list[dict[str, str]],
    configs: list[dict[str, str]],
    mcp_servers: Mapping[str, Mapping[str, Any]] | None,
    plugin_overrides_path: str | None,
) -> dict[str, object]:
    """Build the dict that will be written to ``.workstate-bootstrap.json``.

    ``mcp_servers`` is the mapping that ``install()`` actually used to
    write the three config surfaces. Persisting the sorted key list as
    ``manifest["mcp_servers"]`` gives ``sync_mcp_configs(prune_removed_managed=True)``
    an authoritative previously-managed provenance: any name in this
    list that disappears from the new managed set is a removal that
    sync may prune from the surface files; everything else is treated
    as third-party and left untouched.
    """
    manifest: dict[str, object] = {"schema_version": SCHEMA_VERSION}
    if source_kind == "package":
        manifest["source_kind"] = "package"
        manifest["package_version"] = package_version
    else:
        # git_overlay output stays byte-identical to pre-WS-PKG-DELIVERY-01:
        # source_kind is omitted (BootstrapManifest defaults it) so existing
        # manifests and tests are unaffected.
        manifest["remote_url"] = remote_url
        manifest["remote_ref"] = remote_ref
        manifest["remote_sha"] = remote_sha
    manifest["profile"] = profile
    manifest["surfaces"] = surfaces
    manifest["configs"] = configs
    manifest["mcp_servers"] = sorted(mcp_servers) if mcp_servers else []
    if plugin_overrides_path is not None:
        manifest["plugin_overrides_path"] = plugin_overrides_path
    return manifest


def _finalize_install_manifest(
    target: Path,
    manifest: dict[str, object],
    *,
    override_backup_path: str | None = None,
    state_backup_path: str | None = None,
) -> dict[str, object]:
    """Validate the manifest against the protocol shape, write it, and return
    the install result. Shared by the git-overlay and package install paths.

    When workstate-protocol is not installed (partial migrations), validation
    is skipped — the manifest contract is best-effort until the protocol is
    mandatory.
    """
    try:
        from workstate_protocol import BootstrapManifest  # type: ignore[import-not-found]

        BootstrapManifest.model_validate(manifest)
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        raise BootstrapManifestValidationError(
            f"refusing to write {BOOTSTRAP_MANIFEST_NAME}: workstate_protocol.BootstrapManifest "
            f"validation failed: {exc}"
        ) from exc

    manifest_path = target / BOOTSTRAP_MANIFEST_NAME
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")

    result = dict(manifest)
    if override_backup_path is not None:
        result["override_backup_path"] = override_backup_path
    if state_backup_path is not None:
        result["state_backup_path"] = state_backup_path
    return result


# In the workstate, the shared workstate-system surfaces live
# under packages/workstate-system/ rather than at the clone root. We probe this
# subdirectory first when resolving a surface in the clone, and fall back to
# the clone root for legacy/hoisted overlay layouts (and for the
# fake_remote_with_surfaces fixture used elsewhere in the test suite).
WORKSTATE_SYSTEM_SUBDIR = "packages/workstate-system"

# Shared overlay surfaces materialized as symlinks into ``.workstate/remote``.
# Per-agent surfaces (.claude/skills, .claude/commands, .github/prompts,
# .codex/skills) are no longer canonical in the overlay clone; they are
# generated artifacts produced by generate_agent_workflows.py during install.
# Only the truly shared surfaces remain symlinked here.
SHARED_SURFACES: tuple[str, ...] = (
    ".github/hooks",
    "scripts/hooks",
    CONTRACTS_DIR,
    # Hoist canonical rule docs (development-workflow.md,
    # branch-review-guide.md, planning-artifact-home.md) to consumers.
    # Prior to this entry rule docs sat repo-local even though hooks and
    # skills cite them by path; only contracts propagated.
    RULES_DIR,
    # Plan-targets surface and the optional git plan-cat shell wrapper.
    # Hoisted as directory symlinks so consumers
    # inherit Makefile.d/plans.mk and scripts/workstate/git-plan-cat.sh
    # (and any sibling files added in later slices) without re-running
    # bootstrap on every file addition. The Python logic the wrappers
    # invoke lives in the workstate_handoff_mcp package, fetched on demand
    # by uvx — no Python module is hoisted via overlay.
    "Makefile.d",
    "scripts/workstate",
)

# WS-REBRAND-01 Phase A: children of a SHARED_SURFACE that must be *absent*
# from the consumer tree. A whole-directory symlink exposes every child, so a
# surface that has any excluded child is materialized as a real directory with
# an individual symlink per non-excluded child instead — the named children
# simply never appear. The evals harness (config + runner + Make fragment) is
# private operational tooling excluded from the public consumer surface; its
# config (config/evals) was never shipped, so the runner and Make fragment
# that rode the whole-directory symlinks are carved out here to match.
SURFACE_CHILD_EXCLUSIONS: dict[str, frozenset[str]] = {
    "scripts/workstate": frozenset({"evals"}),
    "Makefile.d": frozenset({"evals.mk"}),
}

# Per-agent surfaces written by the generator into the target as real
# directories (not symlinks). Bootstrap ensures these exist as real
# dirs before the generator runs; pre-existing symlinks pointing into
# .workstate/remote/ (left over from the legacy overlay model) are
# replaced. Recorded in the manifest with ``source: "generated"``.
#
# ``.claude/skills``, ``.codex/skills``, and ``.claude/commands`` were
# dropped when the generated plugin tree became canonical; it
# (``.workstate/generated/plugins/workstate-system/base/{claude,codex}/``)
# now owns the Claude/Codex SKILL.md surface and command discovery. The
# legacy generator path emits Copilot prompts and the codex-command-router
# from the manifest path; everything else flows through the plugin
# marketplace pin.
GENERATED_SURFACES: tuple[str, ...] = (".github/prompts",)

PLUGIN_NAME = "workstate-system"
PLUGIN_MARKETPLACE_NAME = "workstate-marketplace"
PLUGIN_OWNER_NAME = "workstate maintainers"
PLUGIN_DESCRIPTION = (
    "Cross-harness workstate-system plugin: portable workflow skills "
    "(SKILL.md) plus uvx-stdio MCP servers (workstate-handoff-mcp, workstate-orchestrator-mcp)."
)
PLUGIN_GENERATED_ROOT: tuple[str, ...] = (
    RUNTIME_ROOT_DIRNAME,
    "generated",
    "plugins",
    PLUGIN_NAME,
)
PLUGIN_OVERRIDE_ROOT: tuple[str, ...] = ("workstate-overrides", PLUGIN_NAME)
PLUGIN_OVERRIDE_MANIFEST = "overrides.yaml"
PLUGIN_OVERRIDE_LOCK = "overrides.lock.json"
CLAUDE_MARKETPLACE_PATH = Path(".claude-plugin") / "marketplace.json"
CLAUDE_SETTINGS_PATH = Path(".claude") / "settings.json"
CODEX_MARKETPLACE_PATH = Path(".agents") / "plugins" / "marketplace.json"
CODEX_CONFIG_PATH = Path(".codex") / "config.toml"
PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE_NAME}"

# Path to the generator script inside the cloned overlay.
GENERATOR_SCRIPT = "scripts/generate_agent_workflows.py"
GENERATOR_MANIFEST = "config/agent-workflows/portable_commands.json"
GENERATOR_SKILLS_SOURCE = "skills"

# Lifecycle profile: hoist the lifecycle Make fragment and the Python runner
# package into the consumer overlay.
# Source paths are resolved through ``_resolve_in_clone`` so they pick
# up the ``packages/workstate-system/`` prefix in the monorepo layout and
# fall back to a flat layout for hoisted fixture remotes. Destination
# paths are flat under the consumer root because the runner/Makefile
# fragment must be reachable from a vanilla consumer with no monorepo
# packaging knowledge.
LIFECYCLE_HOISTS: tuple[tuple[str, str], ...] = (
    ("Makefile.d/lifecycle.mk", "Makefile.d/lifecycle.mk"),
    ("scripts/workstate/lifecycle", "scripts/workstate/lifecycle"),
)

# Sentinel block managed by ``_ensure_consumer_makefile_include`` so we
# can recognize and uninstall our edit without clobbering user content.
LIFECYCLE_INCLUDE_SENTINEL_BEGIN = "# >>> WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE >>>"
LIFECYCLE_INCLUDE_SENTINEL_END = "# <<< WORKSTATE_BOOTSTRAP LIFECYCLE INCLUDE <<<"
LEGACY_LIFECYCLE_INCLUDE_SENTINEL_BEGIN = (
    "# >>> AGENTIC_BOOTSTRAP LIFECYCLE INCLUDE >>>"
)
LIFECYCLE_INCLUDE_DIRECTIVE = "-include Makefile.d/*.mk"
LIFECYCLE_TARGET_NAMES = frozenset(
    {
        "task-start",
        "task-finish",
        "context",
        "slice-start",
        "slice-commit",
        "review-ready",
        "close-check",
        "handoff-close-check",
        "plan-review",
        "plan-analyze",
        "review-run",
        "handoff-review-run",
        "status",
        "tasks",
        "doctor",
        "project-events-replay",
        "tasks-gc",
        "dashboard",
        "format",
    }
)

# Profile contract. ``all`` is the default for both the library
# ``install()`` API and the CLI, so a no-argument ``workstate-bootstrap
# install`` materializes the full surface set out of the box. ``minimal``
# and ``lifecycle`` remain opt-in.
PROFILE_MINIMAL = "minimal"
PROFILE_LIFECYCLE = "lifecycle"
PROFILE_ALL = "all"
SUPPORTED_PROFILES: frozenset[str] = frozenset(
    {PROFILE_MINIMAL, PROFILE_LIFECYCLE, PROFILE_ALL}
)

# Built-in managed-server map. The two Workstate MCP servers ship from this
# repo and are runnable via ``uvx``. Package
# specs are pinned to the latest coordinated release so consumer repos do
# not drift when PyPI advances independently of their overlay tag. Used
# when callers pass ``mcp_servers="default"`` or, in the CLI, when
# ``--mcp-servers`` is omitted and ``--no-mcp-servers`` is not set.
# Operators wanting a custom managed map keep providing a JSON file via
# ``--mcp-servers <path>``.
DEFAULT_MCP_SERVERS: dict[str, dict[str, Any]] = {
    "workstate-handoff-mcp": {
        "type": "stdio",
        "command": "uvx",
        "args": [
            "mcp-workstate-handoff@0.12.0",
            "--workspace-root",
            ".",
            "serve-stdio",
        ],
    },
    "workstate-orchestrator-mcp": {
        "type": "stdio",
        "command": "uvx",
        "args": [
            "mcp-workstate-orchestrator@0.5.0",
            "--workspace-root",
            ".",
            "serve-stdio",
        ],
    },
}


# Distributed MCP servers use Workstate-native identities
# (``workstate-handoff-mcp`` / ``workstate-orchestrator-mcp``) only. No legacy
# read-side compatibility is carried.
#
# NOTE (D1): ``workstate-canvas-mcp`` keeps its private/non-distributed identity
# per implementation note §9-D and implementation note §0.5 D1; it is owned by a separate follow-up.


def _local_handoff_project_candidates() -> tuple[tuple[str, str], ...]:
    return (("packages/mcp-workstate-handoff", "mcp-workstate-handoff"),)


def _build_local_handoff_retry_cmd(target: Path, cmd: list[str]) -> list[str] | None:
    if not cmd or cmd[0] != "uvx":
        return None

    use_from = len(cmd) >= 4 and cmd[1] == "--from"
    if use_from:
        package_ref = cmd[2]
        tail = cmd[3:]
    elif len(cmd) >= 2:
        package_ref = cmd[1]
        tail = cmd[2:]
    else:
        return None

    if not package_ref.startswith("mcp-workstate-handoff"):
        return None

    clone = target.joinpath(*CLONE_SUBDIR)
    for relative_path, cli_name in _local_handoff_project_candidates():
        project = clone / relative_path
        if not (project / "pyproject.toml").is_file():
            continue
        base = ["uv", "run", "--project", str(project)]
        if use_from:
            return [*base, *tail]
        return [*base, cli_name, *tail]

    return None


def _resolve_local_mcp_project(
    target: Path,
    candidates: tuple[tuple[str, str], ...],
) -> tuple[str, str] | None:
    clone = target.joinpath(*CLONE_SUBDIR)
    for relative_path, cli_name in candidates:
        project = clone / relative_path
        if not (project / "pyproject.toml").is_file():
            continue
        return project.relative_to(target).as_posix(), cli_name
    return None


def _build_local_default_mcp_servers(target: Path) -> dict[str, dict[str, Any]] | None:
    """Build the ``uv run`` launch specs for the locally-cloned MCP servers.

    The serve commands pass ``--no-sync`` so launching a server is a plain exec
    against an already-built environment — uv performs no dependency
    resolution, hits no network, and acquires no shared cache lock on the
    startup hot path. That removes the race where two servers cold-starting at
    once contend on uv's lock and one blows the MCP connection timeout (its
    tools then never register for the session). Environment construction is
    hoisted to install time via :func:`_presync_local_mcp_envs`, which must run
    before these specs are written so ``--no-sync`` always finds a ready venv.
    """
    handoff = _resolve_local_mcp_project(
        target,
        (("packages/mcp-workstate-handoff", "mcp-workstate-handoff"),),
    )
    orchestrator = _resolve_local_mcp_project(
        target,
        (("packages/mcp-workstate-orchestrator", "mcp-workstate-orchestrator"),),
    )
    if handoff is None or orchestrator is None:
        return None

    handoff_project, handoff_cli = handoff
    orchestrator_project, orchestrator_cli = orchestrator
    return {
        "workstate-handoff-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--no-sync",
                "--project",
                handoff_project,
                handoff_cli,
                "--workspace-root",
                ".",
                "serve-stdio",
            ],
        },
        "workstate-orchestrator-mcp": {
            "type": "stdio",
            "command": "uv",
            "args": [
                "run",
                "--no-sync",
                "--project",
                orchestrator_project,
                orchestrator_cli,
                "--workspace-root",
                ".",
                "serve-stdio",
            ],
        },
    }


def _resolve_install_mcp_servers(
    target: Path,
    remote_ref: str,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Mapping[str, Any]] | None:
    # The --no-sync invariant for local launchers is enforced at the shared
    # render/write seam (_canonicalize_managed_servers), so the resolver does
    # not normalize here — install, update, repair, and mcp-sync all converge
    # on the same launcher when their map is serialised. implementation note A1.
    if mcp_servers is not DEFAULT_MCP_SERVERS:
        return mcp_servers
    if remote_ref.startswith("v"):
        return mcp_servers
    return _build_local_default_mcp_servers(target) or mcp_servers


def _local_uv_project_from_spec(
    target: Path,
    spec: Mapping[str, Any],
) -> Path | None:
    if spec.get("command") != "uv":
        return None
    args = spec.get("args", [])
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return None
    if not args or args[0] != "run" or "--project" not in args:
        return None
    project_index = args.index("--project") + 1
    if project_index >= len(args):
        return None

    target_root = target.resolve()
    project = (target_root / args[project_index]).resolve()
    try:
        project.relative_to(target_root)
    except ValueError:
        return None
    if not (project / "pyproject.toml").is_file():
        return None
    return project


def _presync_local_mcp_envs(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None,
) -> list[Path]:
    """Pre-build each locally-cloned MCP server's uv environment at install time.

    The generated serve commands launch with ``uv run --no-sync`` (see
    :func:`_build_local_default_mcp_servers`), so the environment must already
    exist by the time a session starts a server. Resolution belongs in the
    install phase — run once here, off the server-startup hot path — rather
    than lazily on every server boot. Returns the project dirs that were
    synced (deduplicated) so callers/tests can assert coverage. A spec that
    is not a local ``uv run --project`` launch is skipped.
    """
    if not mcp_servers:
        return []
    synced: list[Path] = []
    seen: set[Path] = set()
    for spec in mcp_servers.values():
        project = _local_uv_project_from_spec(target, spec)
        if project is None:
            continue
        if project in seen or not (project / "pyproject.toml").is_file():
            continue
        seen.add(project)
        subprocess.run(
            ["uv", "sync", "--project", str(project)],
            check=True,
            cwd=str(target),
            timeout=300,
        )
        synced.append(project)
    return synced


class BootstrapManifestValidationError(RuntimeError):
    """Raised when the install manifest fails the cross-repo wire-shape contract."""


class RemoteUrlMismatchError(RuntimeError):
    """Raised when an existing ``.workstate/remote`` clone tracks a different
    ``origin`` URL than the one passed to ``install``.

    Silently rewriting the manifest while leaving the on-disk clone pointed at
    the old origin would make ``.workstate-bootstrap.json`` lie about provenance.
    Operators get an actionable error instead.
    """


def _managed_clone_can_switch_remote(
    *,
    existing_origin: str,
    existing_manifest_remote_url: str | None,
) -> bool:
    return existing_manifest_remote_url == existing_origin


def _replace_managed_clone_for_remote_switch(
    clone: Path,
    *,
    existing_origin: str,
    remote_url: str,
) -> None:
    dirty = _git("status", "--short", cwd=clone).strip()
    if dirty:
        raise RemoteUrlMismatchError(
            f"{clone} already tracks origin {existing_origin!r}, "
            f"but install was called with remote_url={remote_url!r}. "
            "The existing managed clone also has uncommitted changes; stash or "
            "remove .workstate/remote before switching overlays."
        )
    shutil.rmtree(clone)


class OverrideResetRequiresBackupError(RuntimeError):
    """Raised when ``reset_overrides`` would delete overrides from a dirty
    git worktree without an explicit backup preflight.
    """


def _migrate_legacy_manifest(target: Path) -> Path | None:
    """One-shot rename of legacy ``.workstate-overlay.json`` to ``.workstate-bootstrap.json``.

    Renames only when the legacy file looks like a bootstrap manifest
    (top-level dict with a list ``surfaces`` key) so consumer-owned files
    that happen to share the legacy name are not touched. Prefers
    ``git mv`` when ``target`` is a git worktree so the rename is tracked
    in history; falls back to ``Path.rename`` otherwise. Returns the new
    path on success, ``None`` when no migration was needed or the legacy
    file did not match the bootstrap shape.
    """
    legacy = target / LEGACY_OVERLAY_MANIFEST_NAME
    canonical = target / BOOTSTRAP_MANIFEST_NAME
    if not legacy.is_file() or canonical.exists():
        return None
    try:
        data = json.loads(legacy.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("surfaces"), list):
        return None
    try:
        _git("mv", LEGACY_OVERLAY_MANIFEST_NAME, BOOTSTRAP_MANIFEST_NAME, cwd=target)
    except (subprocess.CalledProcessError, FileNotFoundError):
        legacy.rename(canonical)
    return canonical


def _load_existing_manifest_remote_url(target: Path) -> str | None:
    for name in (BOOTSTRAP_MANIFEST_NAME, LEGACY_OVERLAY_MANIFEST_NAME):
        manifest_path = target / name
        if not manifest_path.is_file():
            continue
        try:
            payload = json.loads(manifest_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        remote_url = payload.get("remote_url")
        if isinstance(remote_url, str) and remote_url:
            return remote_url
    return None


def _prepare_state_for_remote_switch(
    target: Path,
    remote_url: str,
) -> tuple[str | None, str | None]:
    existing_remote_url = _load_existing_manifest_remote_url(target)
    if existing_remote_url is None or existing_remote_url == remote_url:
        return remote_url, None

    state_dir = target / ".task-state"
    backup_path: str | None = None
    if state_dir.exists():
        stamp = _utc_stamp()
        backup_root = target / RUNTIME_ROOT_DIRNAME / "state-backups" / stamp
        archive_target = backup_root / state_dir.name
        archive_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(state_dir), str(archive_target))
        backup_path = backup_root.relative_to(target).as_posix()

    # The old adjacent bootstrap manifest still points at the prior remote
    # until install writes the new one below. Skip the reuse guard for this
    # init only after moving the old runtime state out of the way.
    return None, backup_path


def _git_worktree_is_dirty(target: Path) -> bool:
    if not (target / ".git").exists():
        return False
    return bool(
        _git(
            "status",
            "--short",
            "--",
            ".",
            f":(exclude){'/'.join(CLONE_SUBDIR)}",
            cwd=target,
        ).strip()
    )


def _prune_empty_parent_dirs(root: Path, stop: Path) -> None:
    current = root.resolve()
    stop = stop.resolve()
    try:
        current.relative_to(stop)
    except ValueError:
        return
    while current != stop:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _reset_plugin_overrides(
    target: Path,
    override_root: Path | None,
    *,
    reset_overrides: bool,
    backup_overrides: bool,
) -> tuple[Path | None, str | None]:
    if not reset_overrides or override_root is None:
        return override_root, None

    override_root = override_root.resolve()
    if _git_worktree_is_dirty(target) and not backup_overrides:
        raise OverrideResetRequiresBackupError(
            "refusing to reset plugin overrides from a dirty git worktree without "
            "backup_overrides=True; commit/stash changes first or opt into backup preflight"
        )

    backup_path: str | None = None
    if backup_overrides:
        stamp = _utc_stamp()
        backup_root = target / RUNTIME_ROOT_DIRNAME / "override-backups" / stamp
        archive_target = backup_root / override_root.name
        archive_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(override_root, archive_target)
        backup_path = backup_root.relative_to(target).as_posix()

    shutil.rmtree(override_root)
    _prune_empty_parent_dirs(override_root.parent, target)
    return None, backup_path


def _utc_stamp() -> str:
    """Return a filesystem-safe UTC timestamp (``YYYYMMDDTHHMMSSZ``).

    Shared by every archive/backup path so the stamp format is defined in
    exactly one place. Never invoked at import time — callers pass the
    resulting string into the path builders.
    """
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _git(*args: str, cwd: Path | None = None) -> str:
    """Run ``git`` with the given args, returning stripped stdout."""
    cmd = ["git"]
    if cwd is not None:
        cmd.extend(["-C", str(cwd)])
    cmd.extend(args)
    result = subprocess.run(
        cmd,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.stdout.strip()


def _resolve_ref_to_sha(clone: Path, remote_ref: str) -> str:
    """Resolve ``remote_ref`` against the just-fetched clone, preferring the
    fresh remote-tracking branch over any stale local ref.

    Resolution order:

     1. ``refs/remotes/origin/<ref>`` — picks up freshly fetched branch tips
         and avoids the stale local-branch trap that ``git checkout --detach
         <branch>`` falls into after ``fetch``.
    2. ``refs/tags/<ref>`` — tag refs.
    3. ``<ref>`` raw — last-resort for SHAs and exotic refspecs.
    """
    candidates = (
        f"refs/remotes/origin/{remote_ref}",
        f"refs/tags/{remote_ref}",
        remote_ref,
    )
    for candidate in candidates:
        try:
            return _git("rev-parse", "--verify", f"{candidate}^{{commit}}", cwd=clone)
        except subprocess.CalledProcessError:
            continue
    raise RuntimeError(
        f"could not resolve remote_ref {remote_ref!r} in {clone} "
        "(tried remote-tracking branch, tag, and raw ref)"
    )


def _resolve_in_clone(clone: Path, relpath: str) -> Path:
    """Resolve a surface/asset path against the clone.

    Probes ``<clone>/packages/workstate-system/<relpath>`` first (the
    workstate layout) and falls back to ``<clone>/<relpath>``
    for legacy hoisted overlays. Returns the nested path when neither exists
    so callers can use ``.exists()`` as the discriminator.
    """
    nested = clone / WORKSTATE_SYSTEM_SUBDIR / relpath
    if nested.exists():
        return nested
    root = clone / relpath
    if root.exists():
        return root
    return nested


def _materialize_one_symlink(
    rel: str,
    remote_path: Path,
    target_path: Path,
    clone_resolved: Path,
    remote_subtree_prefix: str,
) -> dict[str, str]:
    """Materialize one ``target_path -> remote_path`` relative symlink.

    Encapsulates the idempotency / repoint / foreign-precedence rules so it
    can be applied to a whole surface or to a single carved child. Returns the
    manifest entry for ``rel``:

    - Target absent: create parent, symlink, record ``source='shared'``.
    - Target already a symlink resolving to the current source: leave it;
        record ``source='shared'`` (idempotent rerun path).
    - Target already a symlink lexically under our clone but not resolving to
        the current source: repoint and record ``source='shared'``.
    - Target a foreign symlink, or a real file/dir: leave it untouched and
        record ``source='local'`` so overlay precedence is honored.
    """
    expected_rel = os.path.relpath(remote_path, target_path.parent)
    target_is_directory = remote_path.is_dir()

    if target_path.is_symlink():
        raw_target = os.readlink(target_path)
        if os.path.isabs(raw_target):
            abs_target_str = os.path.normpath(raw_target)
        else:
            abs_target_str = os.path.normpath(
                os.path.join(str(target_path.parent), raw_target)
            )
        try:
            resolved = target_path.resolve(strict=False)
        except OSError:
            resolved = None
        resolves_to_expected = (
            resolved is not None and resolved == remote_path.resolve()
        )
        lexically_in_remote_subtree = abs_target_str == str(
            clone_resolved
        ) or abs_target_str.startswith(remote_subtree_prefix)
        if resolves_to_expected:
            return {"path": rel, "source": "shared"}
        if lexically_in_remote_subtree:
            # Stale or broken pointer into our own remote subtree —
            # repoint to the current canonical location.
            target_path.unlink()
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.symlink_to(
                expected_rel, target_is_directory=target_is_directory
            )
            print(f"repointed: {rel}")
            return {"path": rel, "source": "shared"}
        # Foreign symlink — local content takes precedence.
        return {"path": rel, "source": "local"}

    if target_path.exists():
        return {"path": rel, "source": "local"}

    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.symlink_to(expected_rel, target_is_directory=target_is_directory)
    return {"path": rel, "source": "shared"}


def _raw_symlink_target_path(link_path: Path) -> str:
    raw_target = os.readlink(link_path)
    if os.path.isabs(raw_target):
        return os.path.normpath(raw_target)
    return os.path.normpath(os.path.join(str(link_path.parent), raw_target))


def _points_into_remote_subtree(
    abs_target_str: str,
    clone_resolved: Path,
    remote_subtree_prefix: str,
) -> bool:
    return abs_target_str == str(clone_resolved) or abs_target_str.startswith(
        remote_subtree_prefix
    )


def _remove_bootstrap_owned_excluded_child(
    child_path: Path,
    clone_resolved: Path,
    remote_subtree_prefix: str,
) -> None:
    if not child_path.is_symlink():
        return
    if _points_into_remote_subtree(
        _raw_symlink_target_path(child_path),
        clone_resolved,
        remote_subtree_prefix,
    ):
        child_path.unlink()


def _lifecycle_hoist_children(surface: str) -> frozenset[str]:
    """Child names of ``surface`` that :data:`LIFECYCLE_HOISTS` owns.

    A carved surface must not symlink these children: the lifecycle hoist
    copies them as real files later in ``install()``, and recording a
    ``source='shared'`` symlink entry here would collide with that pass's
    ``source='lifecycle'`` entry for the same path.
    """
    children: set[str] = set()
    for _src_rel, dest_rel in LIFECYCLE_HOISTS:
        parent, _, child = dest_rel.rpartition("/")
        if parent == surface and child:
            children.add(child)
    return frozenset(children)


def _materialize_carved_surface(
    surface: str,
    remote_path: Path,
    target_path: Path,
    clone_resolved: Path,
    remote_subtree_prefix: str,
) -> list[dict[str, str]]:
    """Materialize a SHARED_SURFACE that has excluded children.

    The parent becomes a real directory and each child is symlinked
    individually, except: children named in ``SURFACE_CHILD_EXCLUSIONS``
    (carved out — never appear in the consumer tree) and children that
    :data:`LIFECYCLE_HOISTS` copies separately. A legacy whole-directory
    symlink into our own clone is replaced with a real directory so the
    carve can take effect on upgrade; a foreign symlink is left untouched
    (local precedence). Returns one manifest entry per materialized child.
    """
    excluded = SURFACE_CHILD_EXCLUSIONS[surface] | _lifecycle_hoist_children(surface)

    if target_path.is_symlink():
        if _points_into_remote_subtree(
            _raw_symlink_target_path(target_path),
            clone_resolved,
            remote_subtree_prefix,
        ):
            # Legacy whole-directory symlink into our own clone — replace
            # with a real directory so the excluded children can be carved.
            target_path.unlink()
            print(f"carved: {surface}")
        else:
            # Foreign symlink: local content wins, leave untouched.
            return [{"path": surface, "source": "local"}]
    elif target_path.exists() and not target_path.is_dir():
        # A real file where a directory surface is expected — foreign/local.
        return [{"path": surface, "source": "local"}]

    target_path.mkdir(parents=True, exist_ok=True)

    for child_name in excluded:
        _remove_bootstrap_owned_excluded_child(
            target_path / child_name,
            clone_resolved,
            remote_subtree_prefix,
        )

    entries: list[dict[str, str]] = []
    for child in sorted(remote_path.iterdir(), key=lambda p: p.name):
        if child.name in excluded:
            continue
        entries.append(
            _materialize_one_symlink(
                f"{surface}/{child.name}",
                child,
                target_path / child.name,
                clone_resolved,
                remote_subtree_prefix,
            )
        )
    return entries


def _materialize_surfaces(target: Path, clone: Path) -> list[dict[str, str]]:
    """Symlink each known shared surface from ``clone`` into ``target``.

    Surfaces absent in the clone are skipped silently (not recorded). A
    surface listed in :data:`SURFACE_CHILD_EXCLUSIONS` is materialized
    per-child via :func:`_materialize_carved_surface` so its excluded
    children stay absent; every other surface is a single whole-directory
    symlink via :func:`_materialize_one_symlink`. See those helpers for the
    idempotency / repoint / foreign-precedence rules.
    """

    materialized: list[dict[str, str]] = []
    clone_resolved = clone.resolve()
    remote_subtree_prefix = str(clone_resolved) + os.sep

    for surface in SHARED_SURFACES:
        remote_path = _resolve_in_clone(clone, surface)
        if not remote_path.exists():
            continue

        if surface in SURFACE_CHILD_EXCLUSIONS:
            materialized.extend(
                _materialize_carved_surface(
                    surface,
                    remote_path,
                    target / surface,
                    clone_resolved,
                    remote_subtree_prefix,
                )
            )
            continue

        materialized.append(
            _materialize_one_symlink(
                surface,
                remote_path,
                target / surface,
                clone_resolved,
                remote_subtree_prefix,
            )
        )

    return materialized


def _copy_surface_entry(src: Path, dest: Path, rel: str) -> dict[str, str]:
    """Copy one surface (file or whole directory) from a package data root.

    ``shutil.copy2``/``copytree`` preserve file modes, so executable hook
    scripts keep their ``0o755`` bit. A pre-existing symlink at ``dest`` is a
    foreign/legacy overlay and left untouched (local precedence); a prior
    bootstrap-owned copy is replaced for idempotent reruns.
    """
    if dest.is_symlink():
        return {"path": rel, "source": "local"}
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dest)
    else:
        shutil.copy2(src, dest)
    return {"path": rel, "source": "shared"}


def _materialize_surfaces_copy(target: Path, source_root: Path) -> list[dict[str, str]]:
    """Copy each known shared surface from the package data root into target.

    The package delivery source has no clone to symlink into and runs in an
    ephemeral env, so surfaces are **copied** (the git-overlay path symlinks).
    Mirrors :func:`_materialize_surfaces`: carved surfaces drop the same
    excluded children (evals + lifecycle-hoist children, which are copied
    separately by :func:`_install_lifecycle_profile`); a real surface already
    present in the target is treated as local and left untouched.
    """
    materialized: list[dict[str, str]] = []
    for surface in SHARED_SURFACES:
        src = _resolve_in_clone(source_root, surface)
        if not src.exists():
            continue
        dest = target / surface
        if surface in SURFACE_CHILD_EXCLUSIONS:
            excluded = SURFACE_CHILD_EXCLUSIONS[surface] | _lifecycle_hoist_children(
                surface
            )
            if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
                materialized.append({"path": surface, "source": "local"})
                continue
            dest.mkdir(parents=True, exist_ok=True)
            for child in sorted(src.iterdir(), key=lambda p: p.name):
                if child.name in excluded:
                    continue
                materialized.append(
                    _copy_surface_entry(
                        child, dest / child.name, f"{surface}/{child.name}"
                    )
                )
        else:
            materialized.append(_copy_surface_entry(src, dest, surface))
    return materialized


def _package_source_root(package_root: Path | None) -> Path:
    """Resolve the workstate-system overlay payload root for the package source.

    Uses an explicit ``package_root`` (tests / pinned installs) when given,
    otherwise the installed ``workstate_system`` distribution's data root.
    """
    if package_root is not None:
        return Path(package_root).resolve()
    try:
        from workstate_system import data_root  # type: ignore[import-not-found]
    except ImportError as exc:
        raise FileNotFoundError(
            "source='package' requires the workstate-system distribution to be "
            "installed (import workstate_system failed); install it or pass "
            "package_root explicitly."
        ) from exc
    return Path(data_root()).resolve()


def _package_version(source_root: Path) -> str:
    """Return the installed workstate-system version (distribution metadata),
    falling back to a stable local marker when metadata is unavailable."""
    try:
        from importlib import metadata as importlib_metadata

        return importlib_metadata.version("workstate-system")
    except Exception:  # noqa: BLE001
        return "0.0.0+local"


def _prepare_generated_surfaces(target: Path, clone: Path) -> list[dict[str, str]]:
    """Ensure each per-agent generated surface exists as a real directory.

    Pre-existing symlinks pointing into the clone (left over from the
    pre-Plan-0002 overlay model where these surfaces were shared
    symlinks) are replaced with empty directories so the generator can
    write into them. Pre-existing real local content is preserved —
    the operator may have intentionally placed local overrides there;
    the generator's per-file write logic will only replace the files
    it owns.
    """
    materialized: list[dict[str, str]] = []
    clone_resolved = clone.resolve()

    for surface in GENERATED_SURFACES:
        target_path = target / surface

        if target_path.is_symlink():
            try:
                resolved = target_path.resolve(strict=False)
            except OSError:
                resolved = None
            points_into_clone = resolved is not None and str(resolved).startswith(
                str(clone_resolved) + os.sep
            )
            broken = resolved is not None and not target_path.exists()
            if points_into_clone or broken:
                # Legacy overlay symlink, or a dangling symlink whose
                # target is gone — replace with a real directory so the
                # generator can write into it. (A dangling symlink also
                # blocks the mkdir below, since lexists() is True.)
                target_path.unlink()
                target_path.mkdir(parents=True, exist_ok=True)
            # Foreign live symlinks are left alone (operator chose them);
            # the generator will write through them into wherever they point.

        elif not target_path.exists():
            target_path.mkdir(parents=True, exist_ok=True)

        materialized.append({"path": surface, "source": "generated"})

    return materialized


def _prepare_plugin_generated_surfaces(
    target: Path, clone: Path, override_root: Path | None
) -> list[dict[str, str]]:
    """Record generated plugin trees that install materializes for this target."""
    generator_script = _resolve_in_clone(clone, GENERATOR_SCRIPT)
    manifest_path = _resolve_in_clone(clone, GENERATOR_MANIFEST)
    if not generator_script.is_file() or not manifest_path.is_file():
        return []

    entries = [
        {"path": Path(*PLUGIN_GENERATED_ROOT, "base").as_posix(), "source": "generated"}
    ]
    if override_root is not None:
        entries.append(
            {
                "path": Path(*PLUGIN_GENERATED_ROOT, "effective").as_posix(),
                "source": "generated",
            }
        )
    return entries


def _plugin_tree_out(target: Path, kind: str) -> Path:
    return target.joinpath(*PLUGIN_GENERATED_ROOT, kind)


def _plugin_override_root_from_manifest(
    target: Path, manifest: Mapping[str, object] | None
) -> Path | None:
    if manifest is None:
        return None
    raw_path = manifest.get("plugin_overrides_path")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = target / candidate
    candidate = candidate.resolve()
    if (candidate / PLUGIN_OVERRIDE_MANIFEST).is_file():
        return candidate
    return None


def _plugin_override_root_manifest_path(
    target: Path, override_root: Path | None
) -> str | None:
    if override_root is None:
        return None
    try:
        return override_root.relative_to(target).as_posix()
    except ValueError:
        return override_root.as_posix()


def _discover_plugin_override_root(
    target: Path,
    *,
    manifest: Mapping[str, object] | None = None,
    plugin_overrides: Path | None = None,
) -> Path | None:
    if plugin_overrides is not None:
        candidate = Path(plugin_overrides).expanduser().resolve()
        manifest_path = candidate / PLUGIN_OVERRIDE_MANIFEST
        if not manifest_path.is_file():
            raise FileNotFoundError(
                f"plugin override manifest not found: {manifest_path}"
            )
        return candidate

    manifest_root = _plugin_override_root_from_manifest(target, manifest)
    if manifest_root is not None:
        return manifest_root

    override_root = target.joinpath(*PLUGIN_OVERRIDE_ROOT)
    if (override_root / PLUGIN_OVERRIDE_MANIFEST).is_file():
        return override_root
    return None


def _relative_plugin_tree_path(kind: str, harness: str) -> str:
    return f"./{Path(*PLUGIN_GENERATED_ROOT, kind, harness).as_posix()}"


def _write_json_file(
    path: Path, payload: dict[str, Any], *, manifest_path: str | None = None
) -> dict[str, str]:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(payload, indent=2) + "\n"
    manifest_entry_path = manifest_path or path.as_posix()
    if path.exists():
        previous = path.read_text()
        if previous == content:
            return {"path": manifest_entry_path, "action": "unchanged"}
        path.write_text(content)
        return {"path": manifest_entry_path, "action": "updated"}
    path.write_text(content)
    return {"path": manifest_entry_path, "action": "created"}


def _render_plugin_override_lock(override_root: Path, remote_sha: str) -> str:
    from workstate_protocol.bootstrap import PluginOverrideLock, PluginOverrideManifest

    raw_payload = (
        yaml.safe_load((override_root / PLUGIN_OVERRIDE_MANIFEST).read_text()) or {}
    )
    manifest = PluginOverrideManifest.model_validate(raw_payload)
    components: list[dict[str, str]] = []

    for name, override in sorted(manifest.components.skills.items()):
        entry: dict[str, str] = {
            "component_kind": "skill",
            "name": name,
            "mode": override.mode,
        }
        if override.path is not None:
            entry["local_path"] = override.path
        if override.upstream_digest is not None:
            entry["upstream_digest"] = override.upstream_digest
        components.append(entry)

    for name, override in sorted(manifest.components.mcp_servers.items()):
        entry = {
            "component_kind": "mcp_server",
            "name": name,
            "mode": override.mode,
        }
        if override.patch_path is not None:
            entry["patch_path"] = override.patch_path
        components.append(entry)

    payload = PluginOverrideLock.model_validate(
        {
            "schema_version": 1,
            "plugin": manifest.plugin,
            "base_remote_sha": remote_sha,
            "components": components,
        }
    ).model_dump(mode="json")
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def _write_plugin_override_lock(override_root: Path | None, remote_sha: str) -> None:
    if override_root is None:
        return
    lock_path = override_root / PLUGIN_OVERRIDE_LOCK
    lock_path.write_text(_render_plugin_override_lock(override_root, remote_sha))


def _write_plugin_pins(
    target: Path,
    override_root: Path | None = None,
    *,
    include_codex_activation: bool = True,
) -> list[dict[str, str]]:
    plugin_tree_kind = "effective" if override_root is not None else "base"

    claude_marketplace = {
        "name": PLUGIN_MARKETPLACE_NAME,
        "owner": {"name": PLUGIN_OWNER_NAME},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": _relative_plugin_tree_path(plugin_tree_kind, "claude"),
                "description": PLUGIN_DESCRIPTION,
            }
        ],
    }
    codex_marketplace = {
        "name": PLUGIN_MARKETPLACE_NAME,
        "interface": {"displayName": "Workstate Marketplace"},
        "owner": {"name": PLUGIN_OWNER_NAME},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {
                    "source": "local",
                    "path": _relative_plugin_tree_path(plugin_tree_kind, "codex"),
                },
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
                "description": PLUGIN_DESCRIPTION,
            }
        ],
    }

    settings_path = target / CLAUDE_SETTINGS_PATH
    if settings_path.exists():
        current_settings = json.loads(settings_path.read_text())
        if not isinstance(current_settings, dict):
            raise ValueError(f"{settings_path} must contain a JSON object")
    else:
        current_settings = {}
    _deep_merge(
        current_settings,
        {
            "extraKnownMarketplaces": {
                PLUGIN_MARKETPLACE_NAME: {
                    "source": {
                        "source": "directory",
                        "path": ".",
                    }
                }
            },
        },
    )
    enabled_plugins = current_settings.setdefault("enabledPlugins", {})
    if not isinstance(enabled_plugins, dict):
        raise ValueError(f"{settings_path} enabledPlugins must contain a JSON object")
    enabled_plugins.setdefault(PLUGIN_SELECTOR, True)

    entries = [
        _write_json_file(
            target / CLAUDE_MARKETPLACE_PATH,
            claude_marketplace,
            manifest_path=CLAUDE_MARKETPLACE_PATH.as_posix(),
        ),
        _write_json_file(
            settings_path,
            current_settings,
            manifest_path=CLAUDE_SETTINGS_PATH.as_posix(),
        ),
        _write_json_file(
            target / CODEX_MARKETPLACE_PATH,
            codex_marketplace,
            manifest_path=CODEX_MARKETPLACE_PATH.as_posix(),
        ),
    ]
    if include_codex_activation:
        entries.append(_write_codex_plugin_activation_config(target))
    return entries


def _render_codex_plugin_activation_config(target: Path) -> bytes:
    """Render repo-local Codex plugin activation without touching user state.

    ``codex plugin add`` persists activation in ``~/.codex/config.toml`` and
    the user plugin cache. Bootstrap keeps this project-scoped instead: the
    repo config points Codex at the checked-in local marketplace and enables
    the generated ``workstate-system`` plugin when the operator has not
    explicitly disabled it.
    """
    path = target / CODEX_CONFIG_PATH
    if path.exists():
        try:
            doc = tomlkit.parse(path.read_text())
        except (tomlkit.exceptions.TOMLKitError, UnicodeDecodeError):
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    marketplaces = doc.get("marketplaces")
    if not isinstance(marketplaces, dict):
        marketplaces = tomlkit.table(is_super_table=True)
        doc["marketplaces"] = marketplaces
    marketplace = tomlkit.table()
    marketplace["source_type"] = "local"
    marketplace["source"] = "."
    marketplaces[PLUGIN_MARKETPLACE_NAME] = marketplace

    plugins = doc.get("plugins")
    if not isinstance(plugins, dict):
        plugins = tomlkit.table(is_super_table=True)
        doc["plugins"] = plugins
    existing_plugin = plugins.get(PLUGIN_SELECTOR)
    if isinstance(existing_plugin, dict):
        plugin_table = existing_plugin
    else:
        plugin_table = tomlkit.table()
        plugins[PLUGIN_SELECTOR] = plugin_table
    if "enabled" not in plugin_table:
        plugin_table["enabled"] = True

    return tomlkit.dumps(doc).encode("utf-8")


def _write_codex_plugin_activation_config(target: Path) -> dict[str, str]:
    path = target / CODEX_CONFIG_PATH
    existed = path.exists()
    rendered = _render_codex_plugin_activation_config(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered)
    return {
        "path": CODEX_CONFIG_PATH.as_posix(),
        "action": "merged" if existed else "created",
    }


def _append_config_entry(entries: list[dict[str, str]], entry: dict[str, str]) -> None:
    """Append a manifest config entry, coalescing duplicate managed paths."""
    path = entry.get("path")
    for existing in entries:
        if existing.get("path") == path:
            existing["action"] = "merged"
            return
    entries.append(entry)


def _run_generator(
    target: Path, clone: Path, remote_sha: str, override_root: Path | None = None
) -> None:
    """Invoke the agent-workflow generator against the target.

    Uses the generator + manifest + skills source from the overlay
    clone. Writes per-agent surfaces into the target via the
    generator's ``--target`` convenience flag.
    """
    generator_script = _resolve_in_clone(clone, GENERATOR_SCRIPT)
    manifest_path = _resolve_in_clone(clone, GENERATOR_MANIFEST)
    skills_source = _resolve_in_clone(clone, GENERATOR_SKILLS_SOURCE)

    if not generator_script.is_file():
        # Older overlays don't ship the generator. That's acceptable when
        # bootstrapping from a legacy ref; emit nothing rather than fail.
        return

    cmd = [
        sys.executable,
        str(generator_script),
        "--manifest",
        str(manifest_path),
        "--skills-source-root",
        str(skills_source),
        "--target",
        str(target),
    ]
    subprocess.run(cmd, check=True, cwd=str(clone), timeout=120)

    base_plugin_cmd = [
        sys.executable,
        str(generator_script),
        "--mode=plugin",
        "--manifest",
        str(manifest_path),
        "--skills-source-root",
        str(skills_source),
        "--plugin-out",
        str(_plugin_tree_out(target, "base")),
    ]
    subprocess.run(base_plugin_cmd, check=True, cwd=str(clone), timeout=120)

    if override_root is None:
        return

    effective_plugin_cmd = [
        sys.executable,
        str(generator_script),
        "--mode=plugin",
        "--manifest",
        str(manifest_path),
        "--skills-source-root",
        str(skills_source),
        "--plugin-out",
        str(_plugin_tree_out(target, "effective")),
        "--plugin-overrides",
        str(override_root),
        "--plugin-base-remote-sha",
        remote_sha,
    ]
    subprocess.run(effective_plugin_cmd, check=True, cwd=str(clone), timeout=120)


def _install_lifecycle_profile(target: Path, clone: Path) -> list[dict[str, str]]:
    """Hoist the lifecycle Make fragment + runner into ``target``.

    Each entry in :data:`LIFECYCLE_HOISTS` is resolved against the clone
    (preferring the ``packages/workstate-system/`` layout, falling back to
    a flat layout for hoisted fixture remotes), then copied to the
    consumer at the destination relpath. Files use ``shutil.copy2``;
    directories use ``shutil.copytree`` with ``dirs_exist_ok=True`` so
    re-runs are idempotent. Sources missing in the clone are skipped
    silently for older overlay refs.
    """
    entries: list[dict[str, str]] = []
    for src_rel, dest_rel in LIFECYCLE_HOISTS:
        src = _resolve_in_clone(clone, src_rel)
        if not src.exists():
            continue
        dest = target / dest_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Under ``--profile all``, the shared-overlay materialization may
        # already have linked ``dest`` to the same
        # file inside the clone (Makefile.d/ and scripts/workstate/ ride
        # on the overlay symlink path). When dest already resolves to
        # src, ``shutil.copy2``/``copytree`` would raise
        # ``SameFileError``. Treat the existing symlink as the canonical
        # materialization and record the surface entry without copying.
        if dest.exists() and dest.resolve() == src.resolve():
            entries.append({"path": dest_rel, "source": "lifecycle"})
            continue
        if src.is_dir():
            shutil.copytree(src, dest, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dest)
        entries.append({"path": dest_rel, "source": "lifecycle"})
    return entries


def _ensure_consumer_makefile_include(target: Path) -> dict[str, str] | None:
    """Idempotently inject the lifecycle ``-include`` directive into
    ``<target>/Makefile``.

    Wraps the directive in a sentinel-bracketed block so re-runs don't
    duplicate, and a future uninstall can excise it cleanly. When the
    sentinel block is already present the file is left untouched and
    ``action='already_present'`` is returned. When the consumer already
    declares lifecycle target names, the file is also left untouched so
    bootstrap does not inject a wildcard include that overrides
    repo-owned recipes. When the consumer has no Makefile, one is
    created containing only the sentinel block.
    """
    makefile = target / "Makefile"
    block = (
        f"{LIFECYCLE_INCLUDE_SENTINEL_BEGIN}\n"
        f"{LIFECYCLE_INCLUDE_DIRECTIVE}\n"
        f"{LIFECYCLE_INCLUDE_SENTINEL_END}\n"
    )
    if not makefile.exists():
        makefile.write_text(block)
        return {"path": "Makefile", "action": "created"}
    existing = makefile.read_text()
    if (
        LIFECYCLE_INCLUDE_SENTINEL_BEGIN in existing
        or LEGACY_LIFECYCLE_INCLUDE_SENTINEL_BEGIN in existing
    ):
        return {"path": "Makefile", "action": "already_present"}
    if _makefile_declares_lifecycle_targets(existing):
        return {"path": "Makefile", "action": "skipped_existing_lifecycle_targets"}
    sep = "" if existing.endswith("\n") else "\n"
    makefile.write_text(existing + sep + block)
    return {"path": "Makefile", "action": "appended"}


def _makefile_declares_lifecycle_targets(text: str) -> bool:
    """Return true when user-owned Makefile text already defines lifecycle recipes."""
    for raw_line in text.splitlines():
        if not raw_line or raw_line[0].isspace():
            continue
        line = raw_line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        before, after = line.split(":", 1)
        if after.lstrip().startswith("="):
            continue
        for token in before.split():
            if token in LIFECYCLE_TARGET_NAMES:
                return True
    return False


def _install_from_package(
    *,
    target: Path,
    package_root: Path | None,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None,
    plugin_overrides: Path | None,
    reset_overrides: bool,
    backup_overrides: bool,
    enforce_required_surfaces: bool,
    profile: str,
    install_claude_stop_hook: bool,
    install_claude_stop_hook_local: bool,
    install_codex_stop_hook: bool,
    install_vscode_stop_hook: bool,
) -> dict[str, object]:
    """Install the overlay from the installed workstate-system package.

    Additive sibling of the git-overlay path: resolves the payload from the
    package data root, copies surfaces (no clone, no symlinks), and reuses the
    generator / plugin-pin / config / lifecycle / hook helpers via
    ``_resolve_in_clone`` (which resolves package-data paths through its
    fallback). Records ``source_kind='package'`` + ``package_version``.
    """
    source_root = _package_source_root(package_root)
    package_version = _package_version(source_root)
    # Plugin locks anchor on a 40-char base SHA (Sha40). The package source has
    # no git SHA, so derive a stable synthetic anchor from the version.
    base_anchor = hashlib.sha1(
        f"workstate-system@{package_version}".encode("utf-8")
    ).hexdigest()

    override_root = _discover_plugin_override_root(
        target, plugin_overrides=plugin_overrides
    )
    override_root, override_backup_path = _reset_plugin_overrides(
        target,
        override_root,
        reset_overrides=reset_overrides,
        backup_overrides=backup_overrides,
    )

    surfaces: list[dict[str, str]] = []
    configs: list[dict[str, str]] = []

    if profile == PROFILE_ALL:
        surfaces.extend(_materialize_surfaces_copy(target, source_root))
        surfaces.extend(_prepare_generated_surfaces(target, source_root))
        plugin_surfaces = _prepare_plugin_generated_surfaces(
            target, source_root, override_root
        )
        surfaces.extend(plugin_surfaces)

        materialized_paths = {
            entry["path"] for entry in surfaces if isinstance(entry, dict)
        }
        if enforce_required_surfaces and "scripts/hooks" not in materialized_paths:
            raise BootstrapManifestValidationError(
                "refusing to declare install successful: required surface 'scripts/hooks' "
                "was not materialized from the workstate-system package."
            )

        _run_generator(target, source_root, base_anchor, override_root)
        if plugin_surfaces:
            _write_plugin_override_lock(override_root, base_anchor)
            configs.extend(
                _write_plugin_pins(
                    target, override_root, include_codex_activation=False
                )
            )
        configs.extend(_write_configs(target, mcp_servers, include_hooks=False))
        if plugin_surfaces:
            _append_config_entry(configs, _write_codex_plugin_activation_config(target))
        _run_init_state(target, mcp_servers, expected_remote_url=None)

    if profile in (PROFILE_ALL, PROFILE_LIFECYCLE):
        surfaces.extend(_install_lifecycle_profile(target, source_root))
        include_entry = _ensure_consumer_makefile_include(target)
        if include_entry is not None:
            configs.append(include_entry)

    hooks_entry = _set_git_hooks_path(target)
    if hooks_entry is not None:
        configs.append(hooks_entry)

    active_flags: set[str] = set()
    if install_claude_stop_hook:
        active_flags.add("--install-claude-stop-hook")
    if install_claude_stop_hook_local:
        active_flags.add("--install-claude-stop-hook-local")
    if install_codex_stop_hook:
        active_flags.add("--install-codex-stop-hook")
    if install_vscode_stop_hook:
        active_flags.add("--install-vscode-stop-hook")
    configs.extend(
        _walk_hook_adapters(
            manifest=_load_portable_manifest(source_root),
            clone=source_root,
            target=target,
            profile=profile,
            active_flags=active_flags,
        )
    )

    manifest = _build_install_manifest(
        source_kind="package",
        package_version=package_version,
        profile=profile,
        surfaces=surfaces,
        configs=configs,
        mcp_servers=mcp_servers,
        plugin_overrides_path=_plugin_override_root_manifest_path(
            target, override_root
        ),
    )
    return _finalize_install_manifest(
        target, manifest, override_backup_path=override_backup_path
    )


def install(
    *,
    target: Path,
    remote_url: str | None = None,
    remote_ref: str | None = None,
    source: str = "git_overlay",
    package_root: Path | None = None,
    mcp_servers: Mapping[str, Mapping[str, Any]] | str | None = None,
    plugin_overrides: Path | None = None,
    reset_overrides: bool = False,
    backup_overrides: bool = False,
    enforce_required_surfaces: bool = False,
    profile: str = PROFILE_ALL,
    install_claude_stop_hook: bool = False,
    install_claude_stop_hook_local: bool = False,
    install_codex_stop_hook: bool = False,
    install_vscode_stop_hook: bool = False,
) -> dict[str, object]:
    """Clone the shared workstate-system remote, materialize overlay surfaces,
    write consumer-tool configs, and write the overlay manifest.

    Args:
        target: Consumer repository root. Must already exist.
        remote_url: Git URL for the shared workstate-system remote.
        remote_ref: Tag, branch, or SHA to check out (e.g. ``"v0.1.0"``).
        mcp_servers: Mapping of ``<server_name> -> {command, args, env}`` to
            register in ``.mcp.json``, ``.vscode/mcp.json``, and
            ``.codex/config.toml``. Pass the sentinel string ``"default"``
            to use :data:`DEFAULT_MCP_SERVERS` (the two MCP servers shipped
            by this monorepo). When ``None``, the three file-writers are
            skipped. ``core.hooksPath`` is set independently whenever the
            target is a git repo.
        plugin_overrides: Optional explicit plugin override root. When set,
            bootstrap composes the effective plugin tree from this root and
            records the resolved path in the manifest for later doctor /
            update / repair runs.
        reset_overrides: When True, remove the resolved plugin override root
            before regeneration so marketplace pins fall back to the base
            plugin tree.
        backup_overrides: When True together with ``reset_overrides``, archive
            the override root under ``.workstate/override-backups/<timestamp>/``
            before removal.
        enforce_required_surfaces: When True, refuse the install if any
            surface declared as required by the manifest fails to
            materialize. Defaults to False (warn-only).
        profile: Install profile selecting how much overlay surface to
            materialize. One of :data:`PROFILE_MINIMAL`,
            :data:`PROFILE_LIFECYCLE`, or :data:`PROFILE_ALL` (default).
        install_claude_stop_hook: When True, write the shared, checked-in
            Claude stop-hook wiring at ``.claude/settings.json``. Off by
            default; no file is touched unless the operator opts in.
        install_claude_stop_hook_local: When True, write the user-owned,
            gitignored Claude stop-hook wiring at
            ``.claude/settings.local.json``. Off by default.
        install_codex_stop_hook: When True, write the Codex CLI harness
            stop-hook wiring at ``.codex/hooks/stop.json``. Off by default.
        install_vscode_stop_hook: When True, write the VS Code harness
            stop-hook wiring at ``.vscode/workstate-stop-hooks.json``. Off
            by default.

    Returns:
        The manifest dict that was written to ``<target>/.workstate-bootstrap.json``.

    Raises:
        FileNotFoundError: ``target`` does not exist.
        FileExistsError: ``<target>/.workstate/remote`` exists but is not a git clone.
        RemoteUrlMismatchError: existing clone tracks a different ``origin`` URL.
        subprocess.CalledProcessError: ``git`` command failed.
    """
    if profile not in SUPPORTED_PROFILES:
        raise ValueError(
            f"profile={profile!r} is not a recognized install profile; "
            f"expected one of {sorted(SUPPORTED_PROFILES)!r}."
        )

    target = Path(target).resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"target directory does not exist: {target}")

    _migrate_legacy_manifest(target)

    if isinstance(mcp_servers, str):
        if mcp_servers != "default":
            raise ValueError(
                f"mcp_servers={mcp_servers!r} is not a recognized sentinel; "
                "pass a mapping, the literal 'default', or None."
            )
        mcp_servers = DEFAULT_MCP_SERVERS

    if source == "package":
        return _install_from_package(
            target=target,
            package_root=package_root,
            mcp_servers=mcp_servers,
            plugin_overrides=plugin_overrides,
            reset_overrides=reset_overrides,
            backup_overrides=backup_overrides,
            enforce_required_surfaces=enforce_required_surfaces,
            profile=profile,
            install_claude_stop_hook=install_claude_stop_hook,
            install_claude_stop_hook_local=install_claude_stop_hook_local,
            install_codex_stop_hook=install_codex_stop_hook,
            install_vscode_stop_hook=install_vscode_stop_hook,
        )
    if source != "git_overlay":
        raise ValueError(
            f"source={source!r} is not recognized; expected 'git_overlay' or 'package'."
        )
    if not remote_url or not remote_ref:
        raise ValueError("source='git_overlay' requires remote_url and remote_ref.")

    clone = target.joinpath(*CLONE_SUBDIR)
    existing_manifest_remote_url = _load_existing_manifest_remote_url(target)

    if (clone / ".git").exists():
        existing_origin = _git("remote", "get-url", "origin", cwd=clone)
        if existing_origin != remote_url:
            if _managed_clone_can_switch_remote(
                existing_origin=existing_origin,
                existing_manifest_remote_url=existing_manifest_remote_url,
            ):
                _replace_managed_clone_for_remote_switch(
                    clone,
                    existing_origin=existing_origin,
                    remote_url=remote_url,
                )
                _git("clone", "--branch", remote_ref, remote_url, str(clone))
            else:
                raise RemoteUrlMismatchError(
                    f"{clone} already tracks origin {existing_origin!r}, "
                    f"but install was called with remote_url={remote_url!r}. "
                    "Move or remove .workstate/remote (or pass the original URL) to "
                    "switch overlays."
                )
        else:
            _git("fetch", "--tags", "--prune", "--force", "origin", cwd=clone)
    else:
        clone.parent.mkdir(parents=True, exist_ok=True)
        if clone.exists():
            raise FileExistsError(
                f"{clone} exists but is not a git clone. "
                "Move or remove it before re-running install."
            )
        _git("clone", "--branch", remote_ref, remote_url, str(clone))

    sha = _resolve_ref_to_sha(clone, remote_ref)
    if len(sha) != 40:
        raise RuntimeError(f"unexpected sha shape from git rev-parse: {sha!r}")

    _git("checkout", "--detach", sha, cwd=clone)
    mcp_servers = _resolve_install_mcp_servers(target, remote_ref, mcp_servers)
    init_state_expected_remote_url = remote_url
    state_backup_path: str | None = None

    override_root = _discover_plugin_override_root(
        target,
        plugin_overrides=plugin_overrides,
    )
    override_root, override_backup_path = _reset_plugin_overrides(
        target,
        override_root,
        reset_overrides=reset_overrides,
        backup_overrides=backup_overrides,
    )

    surfaces: list[dict[str, str]] = []
    configs: list[dict[str, str]] = []

    if profile == PROFILE_ALL:
        if mcp_servers:
            # Build the local MCP server venvs now (install phase) so the
            # generated ``uv run --no-sync`` serve commands launch against a
            # ready environment and never resolve/sync on the session-startup
            # hot path. Lean profiles skip MCP config/init-state, so they also
            # skip this network/cache-touching setup.
            _presync_local_mcp_envs(target, mcp_servers)
            init_state_expected_remote_url, state_backup_path = _prepare_state_for_remote_switch(
                target,
                remote_url,
            )

        surfaces.extend(_materialize_surfaces(target, clone))
        surfaces.extend(_prepare_generated_surfaces(target, clone))
        plugin_surfaces = _prepare_plugin_generated_surfaces(
            target, clone, override_root
        )
        surfaces.extend(plugin_surfaces)

        # Required-surfaces refusal keeps consumers from ending up with a
        # half-installed harness when required hooks are missing. Run BEFORE the
        # generator, config writers, and init-state so a failing install
        # cannot leave generated artifacts, .mcp.json, or .task-state/
        # behind on disk.
        materialized_paths = {
            entry["path"] for entry in surfaces if isinstance(entry, dict)
        }
        if enforce_required_surfaces and "scripts/hooks" not in materialized_paths:
            raise BootstrapManifestValidationError(
                "refusing to declare install successful: required surface 'scripts/hooks' "
                "was not materialized. Bootstrap-installed hooks are part of the harness "
                "contract; without them, target-side guardrails do not run. "
                "Set enforce_required_surfaces=False to bypass for non-standard remotes."
            )

        _run_generator(target, clone, sha, override_root)
        if plugin_surfaces:
            _write_plugin_override_lock(override_root, sha)
            configs.extend(
                _write_plugin_pins(
                    target,
                    override_root,
                    include_codex_activation=False,
                )
            )
        configs.extend(_write_configs(target, mcp_servers, include_hooks=False))
        if plugin_surfaces:
            _append_config_entry(configs, _write_codex_plugin_activation_config(target))
        _run_init_state(
            target,
            mcp_servers,
            expected_remote_url=init_state_expected_remote_url,
        )

    # ``all`` also performs the lifecycle hoist so a consumer that ships the
    # lifecycle-referencing skills (branch-
    # lifecycle / tdd / incremental-implementation / branch-review /
    # handoff-lifecycle and the body-only references in auto-fix /
    # review-parallel / investigate) also receives the matching
    # ``Makefile.d/lifecycle.mk`` + ``scripts/workstate/lifecycle/``
    # runner that defines those targets. ``--profile lifecycle``
    # remains the dedicated lean profile (no skills, lifecycle only);
    # ``--profile minimal`` is unchanged. Both hoist helpers below are
    # idempotent on rerun.
    if profile in (PROFILE_ALL, PROFILE_LIFECYCLE):
        surfaces.extend(_install_lifecycle_profile(target, clone))
        include_entry = _ensure_consumer_makefile_include(target)
        if include_entry is not None:
            configs.append(include_entry)

    hooks_entry = _set_git_hooks_path(target)
    if hooks_entry is not None:
        configs.append(hooks_entry)

    active_flags: set[str] = set()
    if install_claude_stop_hook:
        active_flags.add("--install-claude-stop-hook")
    if install_claude_stop_hook_local:
        active_flags.add("--install-claude-stop-hook-local")
    if install_codex_stop_hook:
        active_flags.add("--install-codex-stop-hook")
    if install_vscode_stop_hook:
        active_flags.add("--install-vscode-stop-hook")
    configs.extend(
        _walk_hook_adapters(
            manifest=_load_portable_manifest(clone),
            clone=clone,
            target=target,
            profile=profile,
            active_flags=active_flags,
        )
    )

    manifest: dict[str, object] = _build_install_manifest(
        remote_url=remote_url,
        remote_ref=remote_ref,
        remote_sha=sha,
        profile=profile,
        surfaces=surfaces,
        configs=configs,
        mcp_servers=mcp_servers,
        plugin_overrides_path=_plugin_override_root_manifest_path(
            target, override_root
        ),
    )

    return _finalize_install_manifest(
        target,
        manifest,
        override_backup_path=override_backup_path,
        state_backup_path=state_backup_path,
    )


# ---------------------------------------------------------------------------
# Config writers
# ---------------------------------------------------------------------------


HOOKS_PATH_VALUE = "scripts/hooks/git"
"""Workspace-relative ``core.hooksPath`` value.

Points at the ``git/`` subdirectory of the materialized ``scripts/hooks``
surface. The parent directory ships Python helpers, ``.sh`` utilities,
and tests alongside the actual hook scripts; pointing git at the parent
makes git look for hook files by name (``post-checkout`` etc.) at a path
where they do not exist, so it silently resolves nothing. The named
hooks themselves live at ``scripts/hooks/git/<name>``.

Single-line invariant: the on-disk hook layout and this value MUST agree.
The install rehearsal pins both halves of that contract.
"""


def _deep_merge(dst: dict[str, Any], src: Mapping[str, Any]) -> dict[str, Any]:
    """Recursively merge ``src`` into ``dst`` and return ``dst``.

    Dict-into-dict merges recurse. Any non-dict value in ``src`` (including
    lists) replaces the corresponding key in ``dst`` outright — list-concat
    semantics would silently grow user config across reruns.
    """
    for key, value in src.items():
        existing = dst.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge(existing, value)
        elif isinstance(value, Mapping):
            new_dict: dict[str, Any] = {}
            _deep_merge(new_dict, value)
            dst[key] = new_dict
        else:
            dst[key] = value
    return dst


def _write_configs(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None,
    *,
    include_hooks: bool = True,
) -> list[dict[str, str]]:
    """Run the four post-install config writers and return per-surface entries
    suitable for ``manifest['configs']``.

    The three file-writers run only when ``mcp_servers`` is provided. The git
    ``core.hooksPath`` writer runs whenever the target looks like a git repo.
    """
    entries: list[dict[str, str]] = []

    if mcp_servers:
        entries.append(_write_mcp_json(target, mcp_servers))
        entries.append(_write_vscode_mcp_json(target, mcp_servers))
        entries.append(_write_codex_config(target, mcp_servers))

    if include_hooks:
        hooks_entry = _set_git_hooks_path(target)
        if hooks_entry is not None:
            entries.append(hooks_entry)

    return entries


def _run_init_state(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]] | None,
    *,
    expected_remote_url: str | None = None,
) -> None:
    if not mcp_servers:
        return

    spec = mcp_servers.get("workstate-handoff-mcp")
    if spec is None:
        return

    command = spec.get("command")
    if not isinstance(command, str) or not command:
        raise ValueError(
            "workstate-handoff-mcp config must include a non-empty command"
        )

    raw_args = spec.get("args", [])
    if not isinstance(raw_args, list) or not all(
        isinstance(arg, str) for arg in raw_args
    ):
        raise ValueError("workstate-handoff-mcp config args must be a list[str]")

    args = list(raw_args)
    raw_env = spec.get("env")
    env_has_state_dir = (
        isinstance(raw_env, Mapping) and "WORKSTATE_HANDOFF_STATE_DIR" in raw_env
    )
    if args and args[-1] in {"serve-stdio", "serve-http", "init-state"}:
        args = args[:-1]
    if not any(
        arg == "--workspace-root" or arg.startswith("--workspace-root=") for arg in args
    ):
        args.extend(["--workspace-root", str(target)])
    if (
        not any(arg == "--state-dir" or arg.startswith("--state-dir=") for arg in args)
        and not env_has_state_dir
    ):
        args.extend(["--state-dir", str(target / ".task-state")])
    args.append("init-state")
    if expected_remote_url is not None:
        args.extend(["--expected-remote-url", expected_remote_url])

    cmd = [command, *args]
    env = os.environ.copy()
    if raw_env is not None:
        if not isinstance(raw_env, Mapping) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_env.items()
        ):
            raise ValueError(
                "workstate-handoff-mcp config env must be a mapping[str, str]"
            )
        env.update(raw_env)

    try:
        subprocess.run(
            cmd,
            check=True,
            capture_output=True,
            text=True,
            cwd=str(target),
            env=env,
            timeout=120,
        )
    except subprocess.CalledProcessError as exc:
        retry_cmd = _build_local_handoff_retry_cmd(target, cmd)
        if retry_cmd is None:
            raise
        try:
            subprocess.run(
                retry_cmd,
                check=True,
                capture_output=True,
                text=True,
                cwd=str(target),
                env=env,
                timeout=120,
            )
        except subprocess.CalledProcessError as retry_exc:
            raise retry_exc from exc


def _canonicalize_managed_servers(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Enforce launch invariants at the render seam, independent of how the
    server map was resolved (install/update/repair/mcp-sync all funnel
    through the renderers). implementation note A1: every managed local
    ``uv run --project`` launcher must pass ``--no-sync`` so server startup
    never re-resolves on the hot path. Idempotent; non-local specs pass
    through unchanged."""
    canonical: dict[str, Mapping[str, Any]] = {}
    target_root = target.resolve()
    for name, spec in mcp_servers.items():
        args = spec.get("args")
        if not (
            spec.get("command") == "uv"
            and isinstance(args, list)
            and args[:1] == ["run"]
            and "--project" in args
            and "--no-sync" not in args
        ):
            canonical[name] = spec
            continue

        project_index = args.index("--project") + 1
        if project_index >= len(args):
            canonical[name] = spec
            continue

        project = (target_root / args[project_index]).resolve()
        try:
            project.relative_to(target_root)
        except ValueError:
            canonical[name] = spec
            continue

        # Eligible managed local launcher: inject --no-sync. The pyproject.toml
        # existence check that _local_uv_project_from_spec / _presync_local_mcp_envs
        # apply is intentionally omitted here — the seam canonicalizes the
        # launch command at write time, independent of whether the env has been
        # built yet, so a fresh install's not-yet-synced launcher still lands
        # contention-free. implementation note A1.
        canonical[name] = {**spec, "args": [args[0], "--no-sync", *args[1:]]}
    return canonical


def _render_mcp_json(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]],
    *,
    prune_names: Iterable[str] = (),
) -> bytes:
    """Pure render half of the .mcp.json seam: read the existing file
    (if any), deep-merge managed servers under ``mcpServers``, and return
    the bytes that ``_write_mcp_json`` would persist. No filesystem
    mutation.

    ``prune_names`` are server names to remove from the existing
    ``mcpServers`` block before the merge — driven by
    ``sync_mcp_configs(prune_removed_managed=True)`` reading the
    ledger's previously-managed provenance. Default ``()`` keeps the
    install path's behavior byte-identical."""
    path = target / ".mcp.json"
    doc: dict[str, Any] = _load_json_or_empty(path)
    if prune_names:
        servers = doc.get("mcpServers")
        if isinstance(servers, dict):
            for name in prune_names:
                servers.pop(name, None)
    mcp_servers = _canonicalize_managed_servers(target, mcp_servers)
    incoming = {"mcpServers": {name: dict(spec) for name, spec in mcp_servers.items()}}
    _deep_merge(doc, incoming)
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def _load_json_or_empty(path: Path) -> dict[str, Any]:
    """Return parsed JSON or ``{}`` when the file is missing or malformed.

    Managed surfaces are this tool's own output. If the file is invalid
    JSON (interrupted prior write, hand edit), treat it as empty so the
    next reconcile rewrites it cleanly instead of letting JSONDecodeError
    escape through doctor / mcp-sync. Third-party preservation is
    impossible in that case (the existing content is already lost).
    """
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_mcp_json(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]],
    *,
    prune_names: Iterable[str] = (),
) -> dict[str, str]:
    """Deep-merge managed servers into ``<target>/.mcp.json`` under
    ``mcpServers``. Preserves all other keys and other servers."""
    path = target / ".mcp.json"
    existed = path.exists()
    rendered = _render_mcp_json(target, mcp_servers, prune_names=prune_names)
    path.write_bytes(rendered)
    return {"path": ".mcp.json", "action": "merged" if existed else "created"}


def _render_vscode_mcp_json(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]],
    *,
    prune_names: Iterable[str] = (),
) -> bytes:
    """Pure render half of the .vscode/mcp.json seam: read the existing
    file (if any), deep-merge managed servers under ``servers``, and
    return the bytes that ``_write_vscode_mcp_json`` would persist. No
    filesystem mutation (the ``.vscode/`` directory is created by the
    write half).

    ``prune_names`` removes those entries from the existing ``servers``
    block before the merge; see ``_render_mcp_json`` for the contract."""
    path = target / ".vscode" / "mcp.json"
    doc: dict[str, Any] = _load_json_or_empty(path)
    if prune_names:
        servers = doc.get("servers")
        if isinstance(servers, dict):
            for name in prune_names:
                servers.pop(name, None)
    mcp_servers = _canonicalize_managed_servers(target, mcp_servers)
    incoming = {"servers": {name: dict(spec) for name, spec in mcp_servers.items()}}
    _deep_merge(doc, incoming)
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def _write_vscode_mcp_json(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]],
    *,
    prune_names: Iterable[str] = (),
) -> dict[str, str]:
    """Deep-merge managed servers into ``<target>/.vscode/mcp.json`` under
    ``servers``. Creates the ``.vscode`` directory if absent."""
    path = target / ".vscode" / "mcp.json"
    existed = path.exists()
    rendered = _render_vscode_mcp_json(target, mcp_servers, prune_names=prune_names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered)
    return {"path": ".vscode/mcp.json", "action": "merged" if existed else "created"}


def _render_codex_config(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]],
    *,
    prune_names: Iterable[str] = (),
) -> bytes:
    """Pure render half of the .codex/config.toml seam: read the existing
    TOML (if any), replace the ``[mcp_servers.<name>]`` tables for each
    managed server while preserving every other key and comment, and
    return the bytes that ``_write_codex_config`` would persist. No
    filesystem mutation (the ``.codex/`` directory is created by the
    write half).

    ``prune_names`` removes those tables from ``[mcp_servers]`` before
    the managed tables are added; see ``_render_mcp_json`` for the
    contract."""
    path = target / ".codex" / "config.toml"
    if path.exists():
        try:
            doc = tomlkit.parse(path.read_text())
        except (tomlkit.exceptions.TOMLKitError, UnicodeDecodeError):
            doc = tomlkit.document()
    else:
        doc = tomlkit.document()

    if "mcp_servers" not in doc:
        doc["mcp_servers"] = tomlkit.table(is_super_table=True)
    servers_table = doc["mcp_servers"]

    if prune_names:
        for name in prune_names:
            if name in servers_table:
                del servers_table[name]

    mcp_servers = _canonicalize_managed_servers(target, mcp_servers)
    for name, spec in mcp_servers.items():
        new_table = tomlkit.table()
        for spec_key, spec_value in spec.items():
            new_table[spec_key] = spec_value
        servers_table[name] = new_table

    return tomlkit.dumps(doc).encode("utf-8")


def _write_codex_config(
    target: Path,
    mcp_servers: Mapping[str, Mapping[str, Any]],
    *,
    prune_names: Iterable[str] = (),
) -> dict[str, str]:
    """Replace the ``[mcp_servers.<name>]`` tables in
    ``<target>/.codex/config.toml`` for each managed server, leaving every
    other root key, table, and comment untouched (tomlkit round-trip)."""
    path = target / ".codex" / "config.toml"
    existed = path.exists()
    rendered = _render_codex_config(target, mcp_servers, prune_names=prune_names)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered)
    return {"path": ".codex/config.toml", "action": "merged" if existed else "created"}


def _set_git_hooks_path(target: Path) -> dict[str, str] | None:
    """If ``target`` is a git repo, set ``core.hooksPath`` to
    ``scripts/hooks/git`` (under the materialized ``scripts/hooks``
    symlink) and return a manifest entry. Otherwise return ``None``
    (silent skip).

    See ``HOOKS_PATH_VALUE`` for why the path includes the ``git/``
    subdirectory.
    """
    if not (target / ".git").exists():
        return None
    _git("config", "core.hooksPath", HOOKS_PATH_VALUE, cwd=target)
    return {"path": "core.hooksPath", "action": "set"}


# Manifest-driven hook walker.
#
# ``portable_commands.json`` (schema v2) is the single source of truth
# for the per-harness adapter rows that materialize bootstrap-owned
# hooks. The walker reads the manifest from the cloned overlay, filters
# by install profile and the active set of opt-in flags, verifies the
# hook's ``required_artifacts`` exist in the clone, and dispatches each
# selected adapter on its ``patch.operation``. The previous single-harness
# writer (``_write_claude_settings_hooks``) is replaced by this table-driven
# walk so new harnesses (Codex, VS Code, etc.) can be
# added by appending adapter rows to the manifest rather than by
# growing bespoke writers in this module.
#
# Adapter target strings are NEVER hardcoded here — every ``.claude/...``
# / ``.codex/...`` path comes from the manifest. The walker only knows
# how to dispatch operations.

_TEMPLATE_CONSUMER_ROOT = "{{consumer_root}}"


def _load_portable_manifest(clone: Path) -> dict[str, Any]:
    """Read the v2 portable-commands manifest out of the clone.

    Returns ``{}`` when the manifest is absent (older overlays that
    predate schema v2) so the walker becomes a noop instead of raising.
    """
    manifest_path = _resolve_in_clone(clone, GENERATOR_MANIFEST)
    if not manifest_path.is_file():
        return {}
    return json.loads(manifest_path.read_text())


def _render_template(value: Any, *, target: Path) -> Any:
    """Recursively substitute ``{{consumer_root}}`` with the resolved
    consumer-root path inside the adapter ``entry`` template."""
    if isinstance(value, str):
        return value.replace(_TEMPLATE_CONSUMER_ROOT, str(target))
    if isinstance(value, Mapping):
        return {k: _render_template(v, target=target) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(v, target=target) for v in value]
    return value


def _resolve_dotted_path(
    doc: dict[str, Any], json_path: str
) -> tuple[dict[str, Any], str]:
    """Resolve a closed-set JSONPath like ``$.hooks.Stop`` to the parent
    container and the leaf key, creating intermediate dicts as needed.

    The walker only dispatches array-merge patches today, so this parser
    is intentionally narrow: ``$.<seg>(.<seg>)*`` with object-keyed
    segments. Anything else raises ``ValueError`` rather than silently
    accepting a path the dispatcher can't honour.
    """
    if not json_path.startswith("$."):
        raise ValueError(f"unsupported json_path {json_path!r}; must start with '$.'")
    segments = json_path[2:].split(".")
    if not segments or not all(segments):
        raise ValueError(f"unsupported json_path {json_path!r}; empty segment")
    parent: dict[str, Any] = doc
    for seg in segments[:-1]:
        nxt = parent.setdefault(seg, {})
        if not isinstance(nxt, dict):
            raise ValueError(
                f"refusing to merge: {seg!r} along {json_path!r} is not an object"
            )
        parent = nxt
    return parent, segments[-1]


def _apply_merge_array_entry(
    adapter: Mapping[str, Any], *, target: Path
) -> dict[str, str]:
    """Idempotently merge a managed entry into an array container in a
    JSON settings file. ``match_key`` identifies prior managed entries
    for replacement-in-place; everything else is preserved verbatim.
    """
    patch = adapter["patch"]
    target_rel = adapter["target"]
    settings_path = target / target_rel
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existed = settings_path.exists()
    doc: dict[str, Any] = json.loads(settings_path.read_text()) if existed else {}
    if not isinstance(doc, dict):
        raise ValueError(
            f"refusing to merge hook into {settings_path}: "
            "existing JSON document is not an object"
        )

    parent, leaf_key = _resolve_dotted_path(doc, patch["json_path"])
    array_raw = parent.get(leaf_key, [])
    if not isinstance(array_raw, list):
        raise ValueError(
            f"refusing to merge hook into {settings_path}: "
            f"{patch['json_path']!r} is not a list"
        )

    managed_entry = _render_template(patch["entry"], target=target)
    match_key = patch["match_key"]
    if not isinstance(managed_entry, Mapping) or match_key not in managed_entry:
        raise ValueError(
            f"adapter entry missing match_key {match_key!r}: {managed_entry!r}"
        )
    match_value = managed_entry[match_key]

    new_array: list[Any] = []
    replaced = False
    matched_existing = False
    for item in array_raw:
        if isinstance(item, Mapping) and item.get(match_key) == match_value:
            matched_existing = True
            if not replaced:
                new_array.append(managed_entry)
                replaced = True
            continue
        new_array.append(item)
    if not replaced:
        new_array.append(managed_entry)

    parent[leaf_key] = new_array
    settings_path.write_text(json.dumps(doc, indent=2) + "\n")

    if not existed:
        action = "created"
    elif matched_existing:
        action = "noop"
    else:
        action = "merged"
    return {"path": target_rel, "action": action}


_ADAPTER_DISPATCH: dict[str, Any] = {
    "merge_array_entry": _apply_merge_array_entry,
}


def _walk_hook_adapters(
    *,
    manifest: Mapping[str, Any],
    clone: Path,
    target: Path,
    profile: str,
    active_flags: set[str],
) -> list[dict[str, str]]:
    """Walk ``manifest.hooks`` and apply each adapter whose opt_in_flag is
    in ``active_flags``. Hooks whose ``profiles`` do not include the
    active install profile are skipped. When at least one adapter is
    selected, every ``required_artifacts`` row is verified to exist in
    the clone before any file is touched — opting in to a hook whose
    artifacts are missing is a hard fail."""
    configs: list[dict[str, str]] = []
    if not isinstance(manifest, Mapping):
        return configs
    hooks = manifest.get("hooks")
    if not isinstance(hooks, list):
        return configs

    for hook in hooks:
        if not isinstance(hook, Mapping):
            continue
        hook_profiles = hook.get("profiles") or []
        if profile not in hook_profiles and "all" not in hook_profiles:
            continue
        adapters = hook.get("adapters") or []
        selected = [
            a
            for a in adapters
            if isinstance(a, Mapping) and a.get("opt_in_flag") in active_flags
        ]
        if not selected:
            continue
        # Required-artifacts gate: refuse to silently skip a user-requested
        # hook just because the overlay clone is missing the script.
        for artifact in hook.get("required_artifacts") or []:
            consumer_path = (
                artifact.get("consumer_path") if isinstance(artifact, Mapping) else None
            )
            if not consumer_path:
                continue
            resolved = _resolve_in_clone(clone, consumer_path)
            if not resolved.is_file():
                raise RuntimeError(
                    f"hook {hook.get('hook_id')!r}: required artifact "
                    f"{consumer_path!r} is missing in the overlay clone "
                    f"(expected at {resolved}). Cannot honour the opt-in."
                )
        for adapter in selected:
            op = adapter["patch"]["operation"]
            handler = _ADAPTER_DISPATCH.get(op)
            if handler is None:
                raise NotImplementedError(
                    f"hook {hook.get('hook_id')!r}: unknown adapter "
                    f"patch operation {op!r}"
                )
            configs.append(handler(adapter, target=target))
    return configs
