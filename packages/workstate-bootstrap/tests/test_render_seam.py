"""Byte-parity contract for the install.py render/write seam.

implementation note of WORKSTATE-REF-50 splits each `_write_<surface>` into a pure
`_render_<surface>(target, mcp_servers) -> bytes` plus a thin disk-write
wrapper. `sync_mcp_configs(check_only=True)` will rely on render returning
the exact bytes that write would persist, so the contract is:

    _render_<surface>(target, mcp_servers) == on-disk bytes after
    _write_<surface>(target, mcp_servers)

These tests pin that equality for all three surfaces so a future change
cannot let render and write diverge silently.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workstate_bootstrap.install import (
    _render_codex_config,
    _render_mcp_json,
    _render_vscode_mcp_json,
    _write_codex_config,
    _write_mcp_json,
    _write_vscode_mcp_json,
)


MANAGED_SERVERS = {
    "workstate-handoff-mcp": {
        "command": "uvx",
        "args": ["workstate-handoff-mcp@1.2.3"],
        "env": {"WORKSTATE_HANDOFF_LOG_LEVEL": "INFO"},
    },
    "workstate-orchestrator-mcp": {
        "command": "uvx",
        "args": ["workstate-orchestrator-mcp@4.5.6"],
    },
}


def _render_then_write(
    target: Path,
    servers,
    render_fn,
    write_fn,
    surface_path: str,
) -> tuple[bytes, bytes]:
    """Return (rendered_bytes, on_disk_bytes_after_write).

    Render must not mutate the target. Write must persist the same bytes
    that render returned for the identical (target, servers) input.
    """
    rendered = render_fn(target, servers)
    assert isinstance(rendered, bytes), "render helpers must return bytes"
    assert not (target / surface_path).exists(), (
        "render must not write the file (read-only seam)"
    )
    write_fn(target, servers)
    on_disk = (target / surface_path).read_bytes()
    return rendered, on_disk


def test_render_mcp_json_byte_parity_with_writer(tmp_path: Path) -> None:
    rendered, on_disk = _render_then_write(
        tmp_path,
        MANAGED_SERVERS,
        _render_mcp_json,
        _write_mcp_json,
        ".mcp.json",
    )
    assert rendered == on_disk


def test_render_vscode_mcp_json_byte_parity_with_writer(tmp_path: Path) -> None:
    rendered, on_disk = _render_then_write(
        tmp_path,
        MANAGED_SERVERS,
        _render_vscode_mcp_json,
        _write_vscode_mcp_json,
        ".vscode/mcp.json",
    )
    assert rendered == on_disk


def test_render_codex_config_byte_parity_with_writer(tmp_path: Path) -> None:
    rendered, on_disk = _render_then_write(
        tmp_path,
        MANAGED_SERVERS,
        _render_codex_config,
        _write_codex_config,
        ".codex/config.toml",
    )
    assert rendered == on_disk


@pytest.mark.parametrize(
    "render_fn,write_fn,surface_path,seed_content",
    [
        (
            _render_mcp_json,
            _write_mcp_json,
            ".mcp.json",
            '{"mcpServers": {"third-party": {"command": "node", "args": ["./local.js"]}}}\n',
        ),
        (
            _render_vscode_mcp_json,
            _write_vscode_mcp_json,
            ".vscode/mcp.json",
            '{"servers": {"third-party": {"command": "node", "args": ["./local.js"]}}}\n',
        ),
    ],
)
def test_render_preserves_third_party_entries(
    tmp_path: Path, render_fn, write_fn, surface_path, seed_content
) -> None:
    """Render must include unmanaged third-party launchers untouched."""
    surface = tmp_path / surface_path
    surface.parent.mkdir(parents=True, exist_ok=True)
    surface.write_text(seed_content)

    rendered = render_fn(tmp_path, MANAGED_SERVERS)
    write_fn(tmp_path, MANAGED_SERVERS)
    on_disk = surface.read_bytes()

    assert rendered == on_disk
    assert b"third-party" in rendered, "third-party launcher must be preserved"
    assert b"workstate-handoff-mcp" in rendered, "managed server must be present"
