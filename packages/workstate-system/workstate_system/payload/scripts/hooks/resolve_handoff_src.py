"""Shared handoff source resolution for payload hook scripts."""

from __future__ import annotations

import os


def is_package_source_repo(repo_root: str) -> bool:
    """Return True when ``repo_root`` ships the handoff package in-tree."""
    return os.path.isdir(
        os.path.join(repo_root, "packages", "mcp-workstate-handoff", "src")
    )


def resolve_agent_handoff_src(repo_root: str) -> str:
    """Resolve a PYTHONPATH entry exposing ``workstate_handoff_mcp``.

    Installed distributions win first. In the package-source repo, in-tree
    ``packages/mcp-workstate-handoff/src`` wins over the managed overlay
    clone; consumer repos keep overlay-preferred order.
    """
    try:
        from importlib import metadata as importlib_metadata

        dist = importlib_metadata.distribution("mcp-workstate-handoff")
        located = dist.locate_file("workstate_handoff_mcp")
        if located is not None and os.path.isdir(str(located)):
            return os.path.dirname(str(located))
    except Exception:  # noqa: BLE001
        pass

    in_tree = os.path.join(repo_root, "packages", "mcp-workstate-handoff", "src")
    overlay_src = os.path.join(
        repo_root,
        ".workstate",
        "remote",
        "packages",
        "mcp-workstate-handoff",
        "src",
    )
    if is_package_source_repo(repo_root) and os.path.isdir(in_tree):
        return in_tree
    if os.path.isdir(overlay_src):
        return overlay_src
    return in_tree
