"""Per-harness plugin activation dispatcher (WORKSTATE-REF-09).

S1 runtime verdicts (2026-06-06, grok 0.2.22):
- Project-scoped ``.grok/config.toml`` ``enabled`` alone does not activate
  discovery; ``grok plugin install --trust`` + ``grok plugin enable`` required.
- Bare-name selector ``workstate-system`` is stable across paths/worktrees.
- Symlinked ``.grok/plugins/workstate-system`` is discovered by the loader.
- ``grok plugin install`` copies into ``~/.grok/installed-plugins/`` (not a
  live reference); re-materialization can stale the installed copy until install
  re-runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import yaml

from workstate_bootstrap.install import (
    CLAUDE_SETTINGS_PATH,
    CODEX_CONFIG_PATH,
    GROK_PLUGIN_DEST,
    PLUGIN_NAME,
    PLUGIN_SELECTOR,
    _plugin_tree_out,
    _resolve_in_clone,
    _write_codex_plugin_activation_config,
)

HARNESS_PROTOCOL_REL = Path("docs/workstate/contracts/harness-protocol.yaml")
GROK_BARE_SELECTOR = PLUGIN_NAME
STALE_DISCOVERY_SELECTOR_RE = re.compile(
    rf"^project/[0-9a-f]+/{re.escape(PLUGIN_NAME)}$"
)
GROK_CLI_TIMEOUT_SECONDS = 120


def _grok_user_home() -> Path | None:
    """Resolve the Grok user config home; skip when ``HOME`` is unset."""
    home = os.environ.get("HOME", "").strip()
    if not home:
        return None
    return Path(home)


def load_harness_protocol(clone: Path) -> dict[str, Any]:
    path = _resolve_in_clone(clone, HARNESS_PROTOCOL_REL.as_posix())
    if not path.is_file():
        return {}
    payload = yaml.safe_load(path.read_text())
    return payload if isinstance(payload, dict) else {}


def plugin_activation_row(clone: Path, harness_key: str) -> dict[str, Any] | None:
    protocol = load_harness_protocol(clone)
    capabilities = protocol.get("harness_capabilities") or {}
    block = capabilities.get("plugin_activation") or {}
    rows = block.get("rows") or []
    for row in rows:
        if isinstance(row, dict) and row.get("harness_key") == harness_key:
            return row
    return None


def detect_stale_grok_discovery_selectors(home: Path | None = None) -> list[str]:
    """Read-only scan of user-global ``~/.grok/config.toml`` for stale selectors."""
    home = home if home is not None else _grok_user_home()
    if home is None:
        return []
    config_path = home / ".grok" / "config.toml"
    if not config_path.is_file():
        return []
    try:
        payload = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return []
    plugins = payload.get("plugins")
    if not isinstance(plugins, dict):
        return []
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        return []
    return [
        entry
        for entry in enabled
        if isinstance(entry, str) and STALE_DISCOVERY_SELECTOR_RE.match(entry)
    ]


def grok_bare_selector_enabled(home: Path | None = None) -> bool | None:
    """Return whether bare-name selector is enabled in user-global config.

    ``None`` when ``HOME`` is unset, the config is missing, or unreadable.
    """
    home = home if home is not None else _grok_user_home()
    if home is None:
        return None
    config_path = home / ".grok" / "config.toml"
    if not config_path.is_file():
        return None
    try:
        payload = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    plugins = payload.get("plugins")
    if not isinstance(plugins, dict):
        return None
    enabled = plugins.get("enabled")
    if not isinstance(enabled, list):
        return None
    return GROK_BARE_SELECTOR in enabled


def _grok_cli_available() -> bool:
    return shutil.which("grok") is not None


def _run_grok_cli(
    args: tuple[str, ...], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["grok", "plugin", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=GROK_CLI_TIMEOUT_SECONDS,
    )


def activate_grok_plugin(target: Path) -> dict[str, str]:
    """Best-effort Grok activation via CLI install + bare-name enable."""
    plugin_dest = target / GROK_PLUGIN_DEST
    if not plugin_dest.exists():
        return {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "failed",
            "kind": "grok_plugin_activation",
            "message": "materialized grok plugin tree is missing",
        }
    if not _grok_cli_available():
        return {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "skipped_no_cli",
            "kind": "grok_plugin_activation",
        }

    try:
        install_result = _run_grok_cli(
            ("install", plugin_dest.as_posix(), "--trust"), cwd=target
        )
    except subprocess.TimeoutExpired:
        return {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "failed",
            "kind": "grok_plugin_activation",
            "message": "grok plugin install timed out",
        }
    if install_result.returncode != 0:
        detail = (install_result.stderr or install_result.stdout or "").strip()
        return {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "failed",
            "kind": "grok_plugin_activation",
            "message": detail or "grok plugin install failed",
        }

    try:
        enable_result = _run_grok_cli(("enable", GROK_BARE_SELECTOR), cwd=target)
    except subprocess.TimeoutExpired:
        return {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "failed",
            "kind": "grok_plugin_activation",
            "message": "grok plugin enable timed out",
        }
    if enable_result.returncode != 0:
        detail = (enable_result.stderr or enable_result.stdout or "").strip()
        return {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "failed",
            "kind": "grok_plugin_activation",
            "message": detail or "grok plugin enable failed",
        }

    return {
        "path": GROK_PLUGIN_DEST.as_posix(),
        "action": "applied",
        "kind": "grok_plugin_activation",
    }


def _record_claude_activation(target: Path) -> dict[str, str]:
    settings_path = target / CLAUDE_SETTINGS_PATH
    if not settings_path.is_file():
        return {
            "path": CLAUDE_SETTINGS_PATH.as_posix(),
            "action": "failed",
            "kind": "claude_plugin_activation",
        }
    try:
        payload = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        return {
            "path": CLAUDE_SETTINGS_PATH.as_posix(),
            "action": "failed",
            "kind": "claude_plugin_activation",
        }
    enabled = payload.get("enabledPlugins")
    if not isinstance(enabled, dict) or not enabled.get(PLUGIN_SELECTOR):
        return {
            "path": CLAUDE_SETTINGS_PATH.as_posix(),
            "action": "failed",
            "kind": "claude_plugin_activation",
        }
    return {
        "path": CLAUDE_SETTINGS_PATH.as_posix(),
        "action": "applied",
        "kind": "claude_plugin_activation",
    }


def _activate_codex_plugin(target: Path) -> dict[str, str]:
    entry = _write_codex_plugin_activation_config(target)
    action = entry.get("action", "merged")
    if action == "unchanged":
        action = "applied"
    elif action in {"created", "merged", "updated"}:
        action = "applied"
    return {
        "path": CODEX_CONFIG_PATH.as_posix(),
        "action": action,
        "kind": "codex_plugin_activation",
    }


def write_plugin_activation(
    harness: str,
    target: Path,
    *,
    clone: Path,
) -> dict[str, str]:
    """Dispatch per-harness activation keyed by harness-protocol capability rows."""
    if plugin_activation_row(clone, harness) is None:
        kind_by_harness = {
            "claude-code": "claude_plugin_activation",
            "codex": "codex_plugin_activation",
            "grok": "grok_plugin_activation",
        }
        path_by_harness = {
            "claude-code": CLAUDE_SETTINGS_PATH,
            "codex": CODEX_CONFIG_PATH,
            "grok": GROK_PLUGIN_DEST,
        }
        return {
            "path": path_by_harness[harness].as_posix(),
            "action": "skipped_no_contract",
            "kind": kind_by_harness[harness],
        }
    if harness == "claude-code":
        return _record_claude_activation(target)
    if harness == "codex":
        return _activate_codex_plugin(target)
    if harness == "grok":
        return activate_grok_plugin(target)
    raise ValueError(f"unsupported harness activation: {harness!r}")


def materialize_grok_plugin_symlink(
    target: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    """Symlink the worktree grok surface to the effective generated tree."""
    source_root = _plugin_tree_out(target, "effective") / "grok"
    dest_root = target / GROK_PLUGIN_DEST
    if not source_root.is_dir():
        raise FileNotFoundError(
            f"effective grok plugin tree is missing at {source_root}; "
            "run plugin emission before materializing Grok surfaces"
        )
    if dest_root.exists() and not dest_root.is_symlink():
        surface = {"path": GROK_PLUGIN_DEST.as_posix(), "source": "local"}
        config = {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "skipped_foreign_content",
            "kind": "grok_plugin",
        }
        return surface, config
    rel = os.path.relpath(source_root, dest_root.parent)
    action = "updated" if dest_root.exists() or dest_root.is_symlink() else "created"
    if dest_root.is_symlink():
        dest_root.unlink()
    elif dest_root.exists():
        dest_root.unlink()
    dest_root.parent.mkdir(parents=True, exist_ok=True)
    dest_root.symlink_to(rel, target_is_directory=True)
    surface = {"path": GROK_PLUGIN_DEST.as_posix(), "source": "generated"}
    config = {
        "path": GROK_PLUGIN_DEST.as_posix(),
        "action": action,
        "kind": "grok_plugin",
    }
    return surface, config


def grok_dest_is_unmanaged_dir(target: Path) -> bool:
    """True when the grok plugin dest is a real directory lacking the manifest.

    Managed materializations always carry ``.grok-plugin/plugin.json`` (the
    primary-repo copy via copytree of the effective tree, the worktree path
    via symlink). A real directory without it is operator-owned content that
    no managed write path may destroy.
    """
    dest_root = target / GROK_PLUGIN_DEST
    return (
        dest_root.exists()
        and not dest_root.is_symlink()
        and not (dest_root / ".grok-plugin" / "plugin.json").is_file()
    )


def grok_surface_is_foreign_local(target: Path) -> bool:
    """True when the grok plugin surface is operator-owned local content.

    A linked worktree only ever receives a managed symlink
    (:func:`materialize_grok_plugin_symlink`); a real directory there that
    lacks the generated plugin manifest is operator-owned, preserved under
    local precedence (``skipped_foreign_content``), and must not be
    integrity-checked as drift. A bootstrap-shaped copy (manifest present)
    stays integrity-checkable.
    """
    from workstate_bootstrap.worktree import is_linked_worktree

    return grok_dest_is_unmanaged_dir(target) and is_linked_worktree(target)


def grok_plugin_surface_problems(target: Path) -> list[str]:
    """Return integrity problems for the materialized grok plugin surface."""
    from workstate_bootstrap.subcommands import _plugin_tree_integrity_problems

    dest_root = target / GROK_PLUGIN_DEST
    effective_root = _plugin_tree_out(target, "effective") / "grok"
    if not dest_root.exists():
        if effective_root.is_dir():
            return ["materialized grok plugin is missing"]
        return []
    if grok_surface_is_foreign_local(target):
        # Local precedence: adopt/install deliberately preserve this content
        # (skipped_foreign_content), so reporting it as drift would be
        # perpetual and unfixable by repair.
        return []
    if dest_root.is_symlink():
        try:
            resolved = dest_root.resolve()
            if not resolved.is_dir():
                return ["grok plugin symlink target is missing"]
            dest_root = resolved
        except OSError:
            return ["grok plugin symlink is broken"]
    problems = _plugin_tree_integrity_problems(dest_root, "grok")
    if (
        effective_root.is_dir()
        and (effective_root / "skills").is_dir()
        and (dest_root / "skills").is_dir()
    ):
        expected_skills = {
            path.name for path in (effective_root / "skills").iterdir() if path.is_dir()
        }
        actual_skills = {
            path.name for path in (dest_root / "skills").iterdir() if path.is_dir()
        }
        if expected_skills != actual_skills:
            problems.append("skills/ slug set differs from effective grok plugin tree")
    return problems
