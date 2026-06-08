"""Plugin override composition helpers (implementation note S4).

Extracted from ``generate_agent_workflows.py`` so the two merge engines stay
under ~60 lines each while preserving WORKSTATE-REF-07 semantics byte-for-byte.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from workstate_protocol.bootstrap import (
    PluginMcpServerPatch,
    PluginOverrideManifest,
)

PLUGIN_OVERRIDE_MANIFEST = "overrides.yaml"


def sha256_digest(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def merge_file_three_way(base: str, ours: str, theirs: str) -> tuple[str, bool]:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_root = Path(tmp)
        ours_file = tmp_root / "consumer"
        base_file = tmp_root / "base"
        theirs_file = tmp_root / "upstream"
        ours_file.write_text(ours)
        base_file.write_text(base)
        theirs_file.write_text(theirs)
        proc = subprocess.run(
            [
                "git",
                "merge-file",
                "-p",
                "-L",
                "consumer",
                "-L",
                "base",
                "-L",
                "upstream",
                str(ours_file),
                str(base_file),
                str(theirs_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode < 0 or proc.returncode > 127:
        raise SystemExit(
            f"git merge-file failed (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout, proc.returncode != 0


def _read_override_skill(override_root: Path, relative_path: str) -> str:
    override_path = override_root / relative_path
    try:
        return override_path.read_text()
    except FileNotFoundError as exc:
        raise SystemExit(f"plugin override skill not found: {override_path}") from exc


def _skill_replace(
    slug: str,
    override,
    *,
    composed: dict[str, str],
    override_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    if slug not in composed:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: cannot replace unknown skill {slug!r}"
        )
    relative_path = override.path
    upstream_digest = override.upstream_digest
    base_digest = sha256_digest(composed[slug])
    stale_component: dict[str, str] = {}
    if upstream_digest != base_digest:
        on_upstream_change = override.on_upstream_change
        if on_upstream_change == "warn":
            stale_component = {
                "status": "stale",
                "override_path": relative_path,
                "recorded_upstream_digest": upstream_digest,
                "current_base_digest": base_digest,
            }
        elif on_upstream_change != "ignore":
            raise SystemExit(
                f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.skills.{slug}.upstream_digest "
                f"{upstream_digest!r} does not match current base digest {base_digest!r}"
            )
    composed_skill = _read_override_skill(override_root, relative_path)
    composed[slug] = composed_skill
    component = {
        "component_kind": "skill",
        "name": slug,
        "mode": "replace",
        "effective_digest": sha256_digest(composed_skill),
        **stale_component,
    }
    return composed, component


def _skill_add(
    slug: str,
    override,
    *,
    composed: dict[str, str],
    skill_order: list[str],
    override_root: Path,
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    if slug in composed:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: cannot add existing skill {slug!r}; "
            "use mode=replace instead"
        )
    composed_skill = _read_override_skill(override_root, override.path)
    composed[slug] = composed_skill
    skill_order.append(slug)
    component = {
        "component_kind": "skill",
        "name": slug,
        "mode": "add",
        "effective_digest": sha256_digest(composed_skill),
    }
    return composed, skill_order, component


def _skill_disable(
    slug: str,
    *,
    composed: dict[str, str],
    skill_order: list[str],
    override_root: Path,
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    if slug not in composed:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: cannot disable unknown skill {slug!r}"
        )
    del composed[slug]
    skill_order = [entry for entry in skill_order if entry != slug]
    component = {
        "component_kind": "skill",
        "name": slug,
        "mode": "disable",
        "effective_digest": sha256_digest(f"disabled:{slug}\n"),
    }
    return composed, skill_order, component


def _skill_patch(
    slug: str,
    override,
    *,
    composed: dict[str, str],
    override_root: Path,
) -> tuple[dict[str, str], dict[str, str]]:
    if slug not in composed:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: cannot patch unknown skill {slug!r}"
        )
    base_file = override_root / override.base_path
    try:
        forked_base = base_file.read_text()
    except FileNotFoundError as exc:
        raise SystemExit(
            f"plugin override base copy not found: {base_file}"
        ) from exc
    if override.upstream_digest != sha256_digest(forked_base):
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.skills.{slug}.upstream_digest "
            f"does not match the stored base copy at {override.base_path}; "
            "re-fork the base copy or run overrides accept-upstream"
        )
    consumer_edit = _read_override_skill(override_root, override.path)
    current_upstream = composed[slug]
    merged, conflicted = merge_file_three_way(
        forked_base, consumer_edit, current_upstream
    )
    if conflicted:
        composed[slug] = consumer_edit
        component = {
            "component_kind": "skill",
            "name": slug,
            "mode": "patch",
            "effective_digest": sha256_digest(consumer_edit),
            "status": "merge_conflict",
            "override_path": override.path,
            "recorded_upstream_digest": override.upstream_digest,
            "current_base_digest": sha256_digest(current_upstream),
        }
        return composed, component
    composed[slug] = merged
    component = {
        "component_kind": "skill",
        "name": slug,
        "mode": "patch",
        "effective_digest": sha256_digest(merged),
        "override_path": override.path,
        "recorded_upstream_digest": override.upstream_digest,
        "current_base_digest": sha256_digest(current_upstream),
    }
    return composed, component


def compose_plugin_skill_overrides(
    rendered_skills: dict[str, str],
    override_root: Path,
    payload: PluginOverrideManifest,
) -> tuple[dict[str, str], list[str], list[dict[str, str]]]:
    composed = dict(rendered_skills)
    skill_order = list(rendered_skills)
    components: list[dict[str, str]] = []
    handlers = {
        "replace": _skill_replace,
        "add": _skill_add,
        "disable": _skill_disable,
        "patch": _skill_patch,
    }
    for slug, override in sorted(payload.components.skills.items()):
        mode = override.mode
        handler = handlers.get(mode)
        if handler is None:
            raise SystemExit(
                f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.skills.{slug}.mode "
                f"must be one of 'replace', 'patch', 'add', or 'disable' for the current "
                f"composition slice; found {mode!r}"
            )
        if mode == "replace":
            composed, component = handler(
                slug, override, composed=composed, override_root=override_root
            )
            components.append(component)
        elif mode == "add":
            composed, skill_order, component = handler(
                slug,
                override,
                composed=composed,
                skill_order=skill_order,
                override_root=override_root,
            )
            components.append(component)
        elif mode == "disable":
            composed, skill_order, component = handler(
                slug, composed=composed, skill_order=skill_order, override_root=override_root
            )
            components.append(component)
        else:
            composed, component = handler(
                slug, override, composed=composed, override_root=override_root
            )
            components.append(component)
    return composed, skill_order, components


def load_plugin_mcp_patch(override_root: Path, relative_path: str) -> PluginMcpServerPatch:
    path = override_root / relative_path
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError as exc:
        raise SystemExit(f"plugin override MCP patch not found: {path}") from exc
    try:
        return PluginMcpServerPatch.model_validate(raw)
    except ValidationError as exc:
        raise SystemExit(f"{path}: {exc}") from exc


def canonical_mcp_server_digest(server: dict[str, object]) -> str:
    return sha256_digest(json.dumps(server, sort_keys=True, ensure_ascii=False))


def base_plugin_mcp_server(
    name: str, mode: str, server: dict[str, object] | None
) -> dict[str, object]:
    if mode == "add":
        return {"name": name, "command": "", "args": []}
    if server is None:
        raise SystemExit(
            f"MCP server override references unknown canonical server {name!r}; use mode='add' to declare a new one"
        )
    return dict(server)


def mcp_override_unconsumed(server: dict | None, plugin_harnesses: set[str]) -> bool:
    if not plugin_harnesses:
        return True
    harnesses = (server or {}).get("harnesses") or []
    return bool(harnesses) and not (set(harnesses) & plugin_harnesses)


def apply_mcp_patch_ops(
    composed: dict[str, object],
    patch: PluginMcpServerPatch,
    *,
    mode: str,
    name: str,
    override_root: Path,
    patch_path: str,
) -> tuple[dict[str, object], list[str], bool]:
    mutated_fields: list[str] = []
    disabled = False
    for op in patch.ops:
        if op.op == "replace_command":
            composed["command"] = op.value
            mutated_fields.append("command")
        elif op.op == "replace_args":
            composed["args"] = list(op.value)
            mutated_fields.append("args")
        elif op.op == "append_args":
            composed["args"] = [*list(composed.get("args", [])), *op.value]
            mutated_fields.append("args")
        elif op.op == "upsert_env":
            env = dict(composed.get("env", {}))
            env[op.name] = op.value
            composed["env"] = env
            mutated_fields.append("env")
        elif op.op == "remove_env":
            env = dict(composed.get("env", {}))
            env.pop(op.name, None)
            if env:
                composed["env"] = env
            else:
                composed.pop("env", None)
            mutated_fields.append("env")
        elif op.op == "disable_server":
            if mode == "add":
                raise SystemExit(
                    f"{override_root / patch_path}: disable_server cannot be used with "
                    f"components.mcp_servers.{name}.mode='add'"
                )
            disabled = True
            break
    return composed, mutated_fields, disabled


def _mcp_disable_one(
    name: str,
    *,
    servers: list[dict],
    by_name: dict[str, dict],
    plugin_harnesses: set[str],
    unconsumed: list[str],
) -> tuple[list[dict], dict[str, dict], dict[str, str], str | None]:
    if name not in by_name:
        raise SystemExit(
            f"cannot disable unknown MCP server {name!r}"
        )
    if mcp_override_unconsumed(by_name[name], plugin_harnesses):
        unconsumed.append(name)
    servers = [entry for entry in servers if entry.get("name") != name]
    by_name.pop(name, None)
    component = {
        "component_kind": "mcp_server",
        "name": name,
        "mode": "disable",
        "effective_digest": sha256_digest(f"disabled:{name}\n"),
    }
    return servers, by_name, component, f"mcp audit: {name} disabled"


def _mcp_patch_or_add_one(
    name: str,
    override,
    *,
    servers: list[dict],
    by_name: dict[str, dict],
    plugin_harnesses: set[str],
    unconsumed: list[str],
    override_root: Path,
) -> tuple[list[dict], dict[str, dict], dict[str, str] | None, str | None, bool]:
    mode = override.mode
    server = by_name.get(name)
    if mode == "patch" and server is None:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: cannot patch unknown MCP server {name!r}"
        )
    if mode == "add" and server is not None:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: cannot add existing MCP server {name!r}; use mode='patch' or 'disable'"
        )
    if not override.requires_trust_ack:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.mcp_servers.{name}.requires_trust_ack "
            "must be true before patching MCP command, args, or env"
        )
    patch = load_plugin_mcp_patch(override_root, override.patch_path)
    if patch.target_server != name:
        raise SystemExit(
            f"{override_root / override.patch_path}: target_server {patch.target_server!r} "
            f"does not match override key {name!r}"
        )
    composed = base_plugin_mcp_server(name, mode, server)
    composed, mutated_fields, disabled = apply_mcp_patch_ops(
        composed,
        patch,
        mode=mode,
        name=name,
        override_root=override_root,
        patch_path=override.patch_path,
    )
    if disabled:
        if mcp_override_unconsumed(server, plugin_harnesses):
            unconsumed.append(name)
        servers = [entry for entry in servers if entry.get("name") != name]
        by_name.pop(name, None)
        return (
            servers,
            by_name,
            {
                "component_kind": "mcp_server",
                "name": name,
                "mode": "disable",
                "effective_digest": sha256_digest(f"disabled:{name}\n"),
            },
            f"mcp audit: {name} disabled",
            True,
        )
    command = composed.get("command")
    args = composed.get("args", [])
    if not isinstance(command, str) or not command:
        raise SystemExit(
            f"{override_root / override.patch_path}: composed MCP server {name!r} must set a non-empty command"
        )
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise SystemExit(
            f"{override_root / override.patch_path}: composed MCP server {name!r} must carry args as a list of strings"
        )
    if mode == "add":
        servers.append(composed)
    else:
        for index, existing in enumerate(servers):
            if existing.get("name") == name:
                servers[index] = composed
                break
    if mcp_override_unconsumed(composed, plugin_harnesses):
        unconsumed.append(name)
    by_name[name] = composed
    unique_fields = [
        field for field in ("command", "args", "env") if field in mutated_fields
    ]
    component = {
        "component_kind": "mcp_server",
        "name": name,
        "mode": mode,
        "effective_digest": canonical_mcp_server_digest(composed),
    }
    audit = f"mcp audit: {name} {', '.join(unique_fields)}" if unique_fields else None
    return servers, by_name, component, audit, False


def _apply_mcp_override_entry(
    name: str,
    override,
    *,
    servers: list[dict],
    by_name: dict[str, dict],
    plugin_harnesses: set[str],
    unconsumed: list[str],
    override_root: Path,
) -> tuple[list[dict], dict[str, dict], dict[str, str] | None, str | None]:
    mode = override.mode
    if mode == "disable":
        servers, by_name, component, audit = _mcp_disable_one(
            name,
            servers=servers,
            by_name=by_name,
            plugin_harnesses=plugin_harnesses,
            unconsumed=unconsumed,
        )
        return servers, by_name, component, audit
    if mode not in {"patch", "add"}:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.mcp_servers.{name}.mode "
            f"must be one of 'patch', 'disable', or 'add'; found {mode!r}"
        )
    servers, by_name, component, audit, _ = _mcp_patch_or_add_one(
        name,
        override,
        servers=servers,
        by_name=by_name,
        plugin_harnesses=plugin_harnesses,
        unconsumed=unconsumed,
        override_root=override_root,
    )
    return servers, by_name, component, audit


def compose_plugin_mcp_overrides(
    mcp_manifest: dict,
    override_root: Path,
    payload: PluginOverrideManifest,
) -> tuple[dict, list[dict[str, str]], list[str]]:
    servers = [dict(server) for server in mcp_manifest["mcp_servers"]]
    by_name = {
        server["name"]: server
        for server in servers
        if isinstance(server.get("name"), str)
    }
    components: list[dict[str, str]] = []
    audit_lines: list[str] = []
    plugin_harnesses = {
        harness
        for harness, owner in (mcp_manifest.get("registration") or {}).items()
        if owner == "plugin"
    }
    unconsumed: list[str] = []

    for name, override in sorted(payload.components.mcp_servers.items()):
        servers, by_name, component, audit = _apply_mcp_override_entry(
            name,
            override,
            servers=servers,
            by_name=by_name,
            plugin_harnesses=plugin_harnesses,
            unconsumed=unconsumed,
            override_root=override_root,
        )
        if component is not None:
            components.append(component)
        if audit:
            audit_lines.append(audit)

    if unconsumed:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.mcp_servers "
            f"overrides for {sorted(set(unconsumed))} are consumed by no emitted "
            ".mcp.json: each overridden server needs at least one "
            "`registration: plugin` harness in its effective `harnesses` list "
            "(implementation note). Flip a harness to `registration: plugin` or change "
            "the launch specs in the canonical mcp_servers.yaml + "
            "`make mcp-pins-sync` instead."
        )
    return (
        {
            **mcp_manifest,
            "mcp_servers": servers,
        },
        components,
        audit_lines,
    )


def _import_portable_command_validator():
    import sys
    from pathlib import Path

    script_dir = str(Path(__file__).resolve().parent)
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from generate_agent_workflows import _validate_command

    return _validate_command


def _read_override_portable_command(override_root: Path, relative_path: str) -> dict:
    override_path = (override_root / relative_path).resolve()
    root = override_root.resolve()
    if not override_path.is_relative_to(root):
        raise SystemExit(
            f"plugin override portable command escapes override root: {relative_path}"
        )
    try:
        raw = json.loads(override_path.read_text())
    except FileNotFoundError as exc:
        raise SystemExit(
            f"plugin override portable command not found: {override_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"plugin override portable command is not valid JSON: {override_path}"
        ) from exc
    if not isinstance(raw, dict):
        raise SystemExit(
            f"plugin override portable command must be a JSON object: {override_path}"
        )
    return raw


def _portable_command_add(
    command_id: str,
    override,
    *,
    manifest: dict,
    override_root: Path,
    composed_skills: dict[str, str],
) -> tuple[dict, dict[str, str]]:
    if any(entry.get("command_id") == command_id for entry in manifest.get("commands", [])):
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: cannot add portable command {command_id!r}; "
            "an entry with that command_id already exists in the base manifest"
        )
    command = _read_override_portable_command(override_root, override.path)
    if command.get("command_id") != command_id:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.portable_commands.{command_id}.path "
            f"command_id {command.get('command_id')!r} must match override key {command_id!r}"
        )
    validate_command = _import_portable_command_validator()
    validate_command(command, len(manifest.get("commands", [])))
    skill = command["skill"]
    if skill not in composed_skills:
        raise SystemExit(
            f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.portable_commands.{command_id}.path "
            f"references skill {skill!r} that does not resolve in the composed effective tree"
        )
    commands = [*list(manifest.get("commands", [])), command]
    composed = {**manifest, "commands": commands}
    component = {
        "component_kind": "portable_command",
        "name": command_id,
        "mode": "add",
        "effective_digest": sha256_digest(
            json.dumps(command, sort_keys=True, ensure_ascii=False)
        ),
        "override_path": override.path,
    }
    return composed, component


def compose_plugin_portable_command_overrides(
    manifest: dict,
    override_root: Path,
    payload: PluginOverrideManifest,
    *,
    composed_skills: dict[str, str],
) -> tuple[dict, list[dict[str, str]]]:
    composed = deepcopy(manifest)
    components: list[dict[str, str]] = []
    handlers = {
        "add": _portable_command_add,
    }
    for command_id, override in sorted(payload.components.portable_commands.items()):
        mode = override.mode
        handler = handlers.get(mode)
        if handler is None:
            raise SystemExit(
                f"{override_root / PLUGIN_OVERRIDE_MANIFEST}: components.portable_commands.{command_id}.mode "
                f"must be one of 'add' for the current composition slice; found {mode!r}"
            )
        composed, component = handler(
            command_id,
            override,
            manifest=composed,
            override_root=override_root,
            composed_skills=composed_skills,
        )
        components.append(component)
    return composed, components