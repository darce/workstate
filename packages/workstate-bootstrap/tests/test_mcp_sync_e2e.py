"""End-to-end contract test for ``workstate-bootstrap mcp-sync``
(WORKSTATE-REF-50 implementation note).

Pins the four-surface write boundary: ``--check`` followed by
``--apply`` must touch only

- ``.mcp.json``
- ``.vscode/mcp.json``
- ``.codex/config.toml``
- ``.workstate-bootstrap.json``

and nothing else in the target tree. Snapshots every other tracked
artifact (skill files, generated workflows, hooks, .task-state) before
``--apply`` and reasserts byte-equality afterward.

This is the regression guard cited in the task plan implementation note proof:
"the contract test fails if anyone later widens the write surface
(e.g. accidentally regenerates a skill)".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_bootstrap.cli import main as cli_main
from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION


_MANAGED_SURFACE_PATHS = {
    ".mcp.json",
    ".vscode/mcp.json",
    ".codex/config.toml",
    BOOTSTRAP_MANIFEST_NAME,
}


def _seed_ledger(target: Path, *, mcp_servers: list[str]) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "remote_url": "file:///tmp/fake.git",
        "remote_ref": "main",
        "remote_sha": "0" * 40,
        "surfaces": [],
        "configs": [
            {"path": ".mcp.json", "action": "merged"},
            {"path": ".vscode/mcp.json", "action": "merged"},
            {"path": ".codex/config.toml", "action": "merged"},
        ],
        "mcp_servers": mcp_servers,
    }
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(payload, indent=2) + "\n"
    )


def _seed_stale_managed_surfaces(target: Path) -> None:
    (target / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"workstate-handoff-mcp": {"command": "OLD"}}},
            indent=2,
        )
        + "\n"
    )
    (target / ".vscode").mkdir(parents=True, exist_ok=True)
    (target / ".vscode" / "mcp.json").write_text(
        json.dumps(
            {"servers": {"workstate-handoff-mcp": {"command": "OLD"}}}, indent=2
        )
        + "\n"
    )
    (target / ".codex").mkdir(parents=True, exist_ok=True)
    (target / ".codex" / "config.toml").write_text(
        '[mcp_servers.workstate-handoff-mcp]\ncommand = "OLD"\n'
    )


def _seed_unrelated_artifacts(target: Path) -> None:
    """Files outside the managed write boundary; mcp-sync must NOT touch
    these. Mirrors what ``install`` would have produced for skills,
    generated workflows, hooks, lifecycle, state."""
    (target / ".claude" / "skills").mkdir(parents=True, exist_ok=True)
    (target / ".claude" / "skills" / "scope" / "SKILL.md").parent.mkdir(
        parents=True, exist_ok=True
    )
    (target / ".claude" / "skills" / "scope" / "SKILL.md").write_text(
        "# scope\nplaceholder\n"
    )
    (target / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (target / ".github" / "workflows" / "ci.yml").write_text("name: ci\n")
    (target / ".task-state").mkdir(parents=True, exist_ok=True)
    (target / ".task-state" / "handoff.db").write_bytes(b"\x00\x01\x02")
    (target / "Makefile").write_text("default:\n\t@echo ok\n")
    (target / "consumer-readme.md").write_text("# consumer readme\n")


def _snapshot(target: Path) -> dict[str, bytes]:
    """Return {rel_path: bytes} for every file under target."""
    snap: dict[str, bytes] = {}
    for path in target.rglob("*"):
        if path.is_file() or path.is_symlink():
            rel = str(path.relative_to(target))
            try:
                snap[rel] = path.read_bytes()
            except OSError:
                snap[rel] = b"<unreadable>"
    return snap


def _write_servers_spec(path: Path) -> Path:
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "workstate-handoff-mcp": {
                        "command": "uvx",
                        "args": ["workstate-handoff-mcp@1.2.3"],
                    },
                }
            }
        )
    )
    return path


def test_mcp_sync_check_then_apply_only_touches_four_surfaces(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_ledger(tmp_path, mcp_servers=[])
    _seed_stale_managed_surfaces(tmp_path)
    _seed_unrelated_artifacts(tmp_path)
    spec = _write_servers_spec(tmp_path / "servers.json")

    pre = _snapshot(tmp_path)

    rc_check = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--check",
        ]
    )
    capsys.readouterr()
    assert rc_check == 1, "check mode must report drift on stale fixture"
    assert _snapshot(tmp_path) == pre, "check mode wrote to disk"

    rc_apply = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--apply",
        ]
    )
    capsys.readouterr()
    assert rc_apply == 0, "apply mode must succeed on a drifted target"

    post = _snapshot(tmp_path)
    changed = {
        path
        for path in set(pre) | set(post)
        if pre.get(path) != post.get(path)
    }

    assert changed <= _MANAGED_SURFACE_PATHS, (
        f"mcp-sync widened its write surface beyond the four managed "
        f"paths: {sorted(changed - _MANAGED_SURFACE_PATHS)!r}"
    )
    assert _MANAGED_SURFACE_PATHS <= changed, (
        f"mcp-sync did not refresh every expected managed surface; "
        f"missing: {sorted(_MANAGED_SURFACE_PATHS - changed)!r}"
    )

    rc_recheck = cli_main(
        [
            "mcp-sync",
            "--target",
            str(tmp_path),
            "--mcp-servers",
            str(spec),
            "--check",
        ]
    )
    capsys.readouterr()
    assert rc_recheck == 0, "after apply, --check must report no drift"


def test_mcp_sync_apply_is_idempotent(tmp_path: Path) -> None:
    _seed_ledger(tmp_path, mcp_servers=["workstate-handoff-mcp"])
    _seed_stale_managed_surfaces(tmp_path)
    spec = _write_servers_spec(tmp_path / "servers.json")

    cli_main(
        ["mcp-sync", "--target", str(tmp_path), "--mcp-servers", str(spec), "--apply"]
    )
    after_first = _snapshot(tmp_path)

    cli_main(
        ["mcp-sync", "--target", str(tmp_path), "--mcp-servers", str(spec), "--apply"]
    )
    after_second = _snapshot(tmp_path)

    assert after_first == after_second, (
        "apply on a clean target must be a byte-identical no-op"
    )
