"""Per-harness plugin delivery adapters (implementation note S3).

Cycle-free module: both ``install`` and ``activation`` import from here;
neither imports the other's privates for harness dispatch.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import tomlkit
import yaml

from workstate_protocol import RUNTIME_ROOT_DIRNAME

PLUGIN_NAME = "workstate-system"
PLUGIN_MARKETPLACE_NAME = "workstate-marketplace"
PLUGIN_OWNER_NAME = "workstate maintainers"
PLUGIN_DESCRIPTION = (
    "Cross-harness workstate-system plugin: portable workflow skills "
    "(SKILL.md) plus uvx-stdio MCP servers (workstate-handoff-mcp, "
    "workstate-orchestrator-mcp)."
)
PLUGIN_GENERATED_ROOT: tuple[str, ...] = (
    RUNTIME_ROOT_DIRNAME,
    "generated",
    "plugins",
    PLUGIN_NAME,
)
PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{PLUGIN_MARKETPLACE_NAME}"

CLAUDE_MARKETPLACE_PATH = Path(".claude-plugin") / "marketplace.json"
CLAUDE_SETTINGS_PATH = Path(".claude") / "settings.json"
CODEX_MARKETPLACE_PATH = Path(".agents") / "plugins" / "marketplace.json"
CODEX_CONFIG_PATH = Path(".codex") / "config.toml"
GROK_PLUGIN_DEST = Path(".grok") / "plugins" / PLUGIN_NAME

WORKSTATE_SYSTEM_SUBDIR = "packages/workstate-system"
WORKSTATE_SYSTEM_PAYLOAD_SUBDIR = "packages/workstate-system/workstate_system/payload"
HARNESS_PROTOCOL_REL = Path("docs/workstate/contracts/harness-protocol.yaml")

GROK_BARE_SELECTOR = PLUGIN_NAME
STALE_DISCOVERY_SELECTOR_RE = re.compile(
    rf"^project/[0-9a-f]+/{re.escape(PLUGIN_NAME)}$"
)
GROK_CLI_TIMEOUT_SECONDS = 120
# `grok plugin install` keys its registry by content hash and exits non-zero
# with this marker when that exact hash is already registered (a re-run with
# unchanged plugin content). Treated as idempotent success, not a failure.
GROK_ALREADY_INSTALLED_MARKER = "already installed"


@dataclass(frozen=True)
class HarnessContext:
    """Runtime context passed to harness adapter methods."""

    target: Path
    clone: Path
    plugin_tree_kind: str = "effective"


def resolve_in_clone(clone: Path, relpath: str) -> Path:
    """Resolve a surface/asset path against the clone or package root."""
    from workstate_bootstrap.surfaces import clone_layout_probe_roots

    payload_root, nested_root, clone_root = clone_layout_probe_roots(clone)
    payload = payload_root / relpath
    if payload.exists():
        return payload
    nested = nested_root / relpath
    if nested.exists():
        return nested
    root = clone_root / relpath
    if root.exists():
        return root
    return payload


def plugin_tree_out(target: Path, kind: str) -> Path:
    from workstate_bootstrap.surfaces import plugin_tree_out as _plugin_tree_out

    return _plugin_tree_out(target, kind)


def relative_plugin_tree_path(kind: str, harness: str) -> str:
    from workstate_bootstrap.surfaces import relative_plugin_tree_path as _relative

    return _relative(kind, harness)


def load_harness_protocol(clone: Path) -> dict[str, Any]:
    path = resolve_in_clone(clone, HARNESS_PROTOCOL_REL.as_posix())
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


class HarnessAdapter(Protocol):
    key: str
    tree_dir_name: str
    activation_kind: str
    activation_path: Path

    def marketplace_paths(self) -> tuple[str, ...]: ...
    def materialized_paths(self) -> tuple[str, ...]: ...
    def plugin_tree_dir(self, ctx: HarnessContext, kind: str) -> Path: ...
    def pin_surfaces(
        self, ctx: HarnessContext, *, write_json: Any, deep_merge: Any
    ) -> list[dict[str, str]]: ...
    def activation_step(self, ctx: HarnessContext) -> dict[str, str]: ...
    def materialization(
        self, ctx: HarnessContext
    ) -> tuple[dict[str, str], dict[str, str]] | None: ...


@dataclass(frozen=True)
class ClaudeHarnessAdapter:
    key: str = "claude-code"
    tree_dir_name: str = "claude"
    activation_kind: str = "claude_plugin_activation"
    activation_path: Path = CLAUDE_SETTINGS_PATH

    def marketplace_paths(self) -> tuple[str, ...]:
        return (CLAUDE_MARKETPLACE_PATH.as_posix(),)

    def materialized_paths(self) -> tuple[str, ...]:
        return ()

    def plugin_tree_dir(self, ctx: HarnessContext, kind: str) -> Path:
        return plugin_tree_out(ctx.target, kind) / self.tree_dir_name

    def pin_surfaces(
        self, ctx: HarnessContext, *, write_json: Any, deep_merge: Any
    ) -> list[dict[str, str]]:
        claude_marketplace = {
            "name": PLUGIN_MARKETPLACE_NAME,
            "owner": {"name": PLUGIN_OWNER_NAME},
            "plugins": [
                {
                    "name": PLUGIN_NAME,
                    "source": relative_plugin_tree_path(
                        ctx.plugin_tree_kind, self.tree_dir_name
                    ),
                    "description": PLUGIN_DESCRIPTION,
                }
            ],
        }
        settings_path = ctx.target / CLAUDE_SETTINGS_PATH
        if settings_path.exists():
            current_settings = json.loads(settings_path.read_text())
            if not isinstance(current_settings, dict):
                raise ValueError(f"{settings_path} must contain a JSON object")
        else:
            current_settings = {}
        deep_merge(
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
            raise ValueError(
                f"{settings_path} enabledPlugins must contain a JSON object"
            )
        enabled_plugins.setdefault(PLUGIN_SELECTOR, True)
        return [
            write_json(
                ctx.target / CLAUDE_MARKETPLACE_PATH,
                claude_marketplace,
                manifest_path=CLAUDE_MARKETPLACE_PATH.as_posix(),
            ),
            write_json(
                settings_path,
                current_settings,
                manifest_path=CLAUDE_SETTINGS_PATH.as_posix(),
            ),
        ]

    def activation_step(self, ctx: HarnessContext) -> dict[str, str]:
        if plugin_activation_row(ctx.clone, self.key) is None:
            return {
                "path": self.activation_path.as_posix(),
                "action": "skipped_no_contract",
                "kind": self.activation_kind,
            }
        settings_path = ctx.target / CLAUDE_SETTINGS_PATH
        if not settings_path.is_file():
            return {
                "path": CLAUDE_SETTINGS_PATH.as_posix(),
                "action": "failed",
                "kind": self.activation_kind,
            }
        try:
            payload = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            return {
                "path": CLAUDE_SETTINGS_PATH.as_posix(),
                "action": "failed",
                "kind": self.activation_kind,
            }
        enabled = payload.get("enabledPlugins")
        if not isinstance(enabled, dict) or not enabled.get(PLUGIN_SELECTOR):
            return {
                "path": CLAUDE_SETTINGS_PATH.as_posix(),
                "action": "failed",
                "kind": self.activation_kind,
            }
        return {
            "path": CLAUDE_SETTINGS_PATH.as_posix(),
            "action": "applied",
            "kind": self.activation_kind,
        }

    def materialization(
        self, ctx: HarnessContext
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        return None


@dataclass(frozen=True)
class CodexHarnessAdapter:
    key: str = "codex"
    tree_dir_name: str = "codex"
    activation_kind: str = "codex_plugin_activation"
    activation_path: Path = CODEX_CONFIG_PATH

    def marketplace_paths(self) -> tuple[str, ...]:
        return (CODEX_MARKETPLACE_PATH.as_posix(),)

    def materialized_paths(self) -> tuple[str, ...]:
        return ()

    def plugin_tree_dir(self, ctx: HarnessContext, kind: str) -> Path:
        return plugin_tree_out(ctx.target, kind) / self.tree_dir_name

    def pin_surfaces(
        self, ctx: HarnessContext, *, write_json: Any, deep_merge: Any
    ) -> list[dict[str, str]]:
        codex_marketplace = {
            "name": PLUGIN_MARKETPLACE_NAME,
            "interface": {"displayName": "Workstate Marketplace"},
            "owner": {"name": PLUGIN_OWNER_NAME},
            "plugins": [
                {
                    "name": PLUGIN_NAME,
                    "source": {
                        "source": "local",
                        "path": relative_plugin_tree_path(
                            ctx.plugin_tree_kind, self.tree_dir_name
                        ),
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
        return [
            write_json(
                ctx.target / CODEX_MARKETPLACE_PATH,
                codex_marketplace,
                manifest_path=CODEX_MARKETPLACE_PATH.as_posix(),
            ),
        ]

    def activation_step(self, ctx: HarnessContext) -> dict[str, str]:
        if plugin_activation_row(ctx.clone, self.key) is None:
            return {
                "path": self.activation_path.as_posix(),
                "action": "skipped_no_contract",
                "kind": self.activation_kind,
            }
        entry = write_codex_plugin_activation_config(ctx.target)
        action = entry.get("action", "merged")
        if action == "unchanged":
            action = "applied"
        elif action in {"created", "merged", "updated"}:
            action = "applied"
        return {
            "path": CODEX_CONFIG_PATH.as_posix(),
            "action": action,
            "kind": self.activation_kind,
        }

    def materialization(
        self, ctx: HarnessContext
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        return None


@dataclass(frozen=True)
class VscodeHarnessAdapter:
    key: str = "vscode"
    tree_dir_name: str = "vscode"
    activation_kind: str = "vscode_plugin_activation"
    activation_path: Path = Path(".vscode") / "mcp.json"

    def marketplace_paths(self) -> tuple[str, ...]:
        return ()

    def materialized_paths(self) -> tuple[str, ...]:
        return ()

    def plugin_tree_dir(self, ctx: HarnessContext, kind: str) -> Path:
        return plugin_tree_out(ctx.target, kind) / self.tree_dir_name

    def pin_surfaces(
        self, ctx: HarnessContext, *, write_json: Any, deep_merge: Any
    ) -> list[dict[str, str]]:
        return []

    def activation_step(self, ctx: HarnessContext) -> dict[str, str]:
        return {
            "path": self.activation_path.as_posix(),
            "action": "skipped_no_contract",
            "kind": self.activation_kind,
        }

    def materialization(
        self, ctx: HarnessContext
    ) -> tuple[dict[str, str], dict[str, str]] | None:
        return None


@dataclass(frozen=True)
class GrokHarnessAdapter:
    key: str = "grok"
    tree_dir_name: str = "grok"
    activation_kind: str = "grok_plugin_activation"
    activation_path: Path = GROK_PLUGIN_DEST

    def marketplace_paths(self) -> tuple[str, ...]:
        return ()

    def materialized_paths(self) -> tuple[str, ...]:
        return (GROK_PLUGIN_DEST.as_posix(),)

    def plugin_tree_dir(self, ctx: HarnessContext, kind: str) -> Path:
        return plugin_tree_out(ctx.target, kind) / self.tree_dir_name

    def pin_surfaces(
        self, ctx: HarnessContext, *, write_json: Any, deep_merge: Any
    ) -> list[dict[str, str]]:
        return []

    def activation_step(self, ctx: HarnessContext) -> dict[str, str]:
        if plugin_activation_row(ctx.clone, self.key) is None:
            return {
                "path": self.activation_path.as_posix(),
                "action": "skipped_no_contract",
                "kind": self.activation_kind,
            }
        # Delegate through activation shim so tests can patch grok CLI helpers.
        from workstate_bootstrap.activation import activate_grok_plugin

        return activate_grok_plugin(ctx.target)

    def materialization(
        self, ctx: HarnessContext
    ) -> tuple[dict[str, str], dict[str, str]]:
        source_root = plugin_tree_out(ctx.target, "effective") / self.tree_dir_name
        dest_root = ctx.target / GROK_PLUGIN_DEST
        if not source_root.is_dir():
            raise FileNotFoundError(
                f"effective grok plugin tree is missing at {source_root}; "
                "run plugin emission before materializing Grok surfaces"
            )
        action = "updated" if dest_root.exists() else "created"
        if dest_root.exists():
            shutil.rmtree(dest_root)
        shutil.copytree(source_root, dest_root)
        surface = {"path": GROK_PLUGIN_DEST.as_posix(), "source": "generated"}
        config = {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": action,
            "kind": "grok_plugin",
        }
        return surface, config


HARNESSES: dict[str, HarnessAdapter] = {
    "claude-code": ClaudeHarnessAdapter(),
    "codex": CodexHarnessAdapter(),
    "vscode": VscodeHarnessAdapter(),
    "grok": GrokHarnessAdapter(),
}

HARNESS_PLUGIN_DELIVERY: dict[str, dict[str, tuple[str, ...]]] = {
    key: {
        "marketplace": adapter.marketplace_paths(),
        "materialized": adapter.materialized_paths(),
    }
    for key, adapter in HARNESSES.items()
}


def harness_materialized_surfaces() -> tuple[str, ...]:
    return tuple(
        surface
        for delivery in HARNESS_PLUGIN_DELIVERY.values()
        for surface in delivery.get("materialized", ())
    )


def render_codex_plugin_activation_config(target: Path) -> bytes:
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


def write_codex_plugin_activation_config(target: Path) -> dict[str, str]:
    path = target / CODEX_CONFIG_PATH
    existed = path.exists()
    rendered = render_codex_plugin_activation_config(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(rendered)
    return {
        "path": CODEX_CONFIG_PATH.as_posix(),
        "action": "merged" if existed else "created",
    }


def write_plugin_pins(
    target: Path,
    override_root: Path | None = None,
    *,
    include_codex_activation: bool = True,
) -> list[dict[str, str]]:
    from workstate_bootstrap.fsutil import deep_merge, write_json_file

    ctx = HarnessContext(target=target, clone=target)
    entries: list[dict[str, str]] = []
    for adapter in (HARNESSES["claude-code"], HARNESSES["codex"]):
        entries.extend(
            adapter.pin_surfaces(ctx, write_json=write_json_file, deep_merge=deep_merge)
        )
    if include_codex_activation:
        entries.append(write_codex_plugin_activation_config(target))
    return entries


def write_plugin_activation(
    harness_key: str,
    target: Path,
    *,
    clone: Path,
) -> dict[str, str]:
    adapter = HARNESSES.get(harness_key)
    if adapter is None:
        raise ValueError(f"unsupported harness activation: {harness_key!r}")
    return adapter.activation_step(HarnessContext(target=target, clone=clone))


def materialize_grok_plugin(
    target: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    adapter = HARNESSES["grok"]
    result = adapter.materialization(HarnessContext(target=target, clone=target))
    assert result is not None
    return result


def _grok_user_home() -> Path | None:
    home = os.environ.get("HOME", "").strip()
    if not home:
        return None
    return Path(home)


def _grok_cli_available() -> bool:
    return shutil.which("grok") is not None


def _run_grok_cli(
    args: tuple[str, ...], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    from workstate_bootstrap.external import run_external

    return run_external(
        ["grok", "plugin", *args],
        call_class="grok_cli",
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout_override=GROK_CLI_TIMEOUT_SECONDS,
    )


def activate_grok_plugin(target: Path) -> dict[str, str]:
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
        # ExternalCallTimeout subclasses TimeoutExpired, so this catches both.
        return {
            "path": GROK_PLUGIN_DEST.as_posix(),
            "action": "failed",
            "kind": "grok_plugin_activation",
            "message": "grok plugin install timed out",
        }
    already_present = False
    if install_result.returncode != 0:
        detail = (install_result.stderr or install_result.stdout or "").strip()
        # Already-registered content is idempotent success: fall through to
        # `enable` rather than reporting `failed` (see GROK_ALREADY_INSTALLED_MARKER).
        # Changed content produces a new hash and installs cleanly, so this
        # branch never masks a real content update; and a genuinely broken
        # install is still caught by the `enable` returncode check below.
        if GROK_ALREADY_INSTALLED_MARKER not in detail.lower():
            return {
                "path": GROK_PLUGIN_DEST.as_posix(),
                "action": "failed",
                "kind": "grok_plugin_activation",
                "message": detail or "grok plugin install failed",
            }
        already_present = True

    try:
        enable_result = _run_grok_cli(("enable", GROK_BARE_SELECTOR), cwd=target)
    except subprocess.TimeoutExpired:
        # ExternalCallTimeout subclasses TimeoutExpired, so this catches both.
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
        # Distinguish a re-run that found the plugin already registered from a
        # fresh install, matching the `already_present` convention used for
        # other idempotent surfaces (Makefile, .gitignore).
        "action": "already_present" if already_present else "applied",
        "kind": "grok_plugin_activation",
    }


def detect_stale_grok_discovery_selectors(home: Path | None = None) -> list[str]:
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


def materialize_grok_plugin_symlink(
    target: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    source_root = plugin_tree_out(target, "effective") / "grok"
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
    dest_root = target / GROK_PLUGIN_DEST
    return (
        dest_root.exists()
        and not dest_root.is_symlink()
        and not (dest_root / ".grok-plugin" / "plugin.json").is_file()
    )


def grok_surface_is_foreign_local(target: Path) -> bool:
    from workstate_bootstrap.worktree import is_linked_worktree

    return grok_dest_is_unmanaged_dir(target) and is_linked_worktree(target)


def grok_plugin_surface_problems(target: Path) -> list[str]:
    from workstate_bootstrap.subcommands import _plugin_tree_integrity_problems

    dest_root = target / GROK_PLUGIN_DEST
    effective_root = plugin_tree_out(target, "effective") / "grok"
    if not dest_root.exists():
        if effective_root.is_dir():
            return ["materialized grok plugin is missing"]
        return []
    if grok_surface_is_foreign_local(target):
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
