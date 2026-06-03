"""implementation note implementation note — BR-03 production fragment emission.

implementation note introduced ``collect_dashboard_fragments`` and
``maybe_write_dashboard_fragments``, but the production
``generate_dashboard_md`` render path only wrote DASHBOARD.txt. The
fragment files under ``.task-state/DASHBOARD.d/`` and the manifest
that scopes prompt-cache invalidation never landed on disk.

These tests exercise the real render path end-to-end and assert that
the per-section fragment files and manifest are emitted alongside
DASHBOARD.txt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.dashboard_fragments import FRAGMENT_DIR_NAME, MANIFEST_FILENAME


@pytest.fixture()
def isolated_handoff(tmp_path: Path) -> dict[str, Path]:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
        dashboard_path=tmp_path / "DASHBOARD.txt",
        current_task_auto_regen=True,
    )
    mcp_server.configure_runtime(runtime)
    return {"state_dir": state_dir, "dashboard_path": tmp_path / "DASHBOARD.txt"}


def test_render_dashboard_emits_fragment_directory(isolated_handoff: dict[str, Path]) -> None:
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-DEMO",
        objective="demo objective for fragment emission",
        status="in_progress",
    )
    mcp_server.render_handoff(kind="dashboard", write_file=True)

    fragment_dir = isolated_handoff["state_dir"] / FRAGMENT_DIR_NAME
    assert fragment_dir.exists() and fragment_dir.is_dir()
    fragment_files = sorted(p.name for p in fragment_dir.glob("*.md"))
    assert len(fragment_files) >= 1, f"expected at least one fragment file, got {fragment_files}"


def test_render_dashboard_emits_fragment_manifest(isolated_handoff: dict[str, Path]) -> None:
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-DEMO",
        objective="demo objective for manifest emission",
        status="in_progress",
    )
    mcp_server.render_handoff(kind="dashboard", write_file=True)

    manifest_path = isolated_handoff["state_dir"] / MANIFEST_FILENAME
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert "fragments" in manifest
    assert isinstance(manifest["fragments"], dict)
    assert len(manifest["fragments"]) >= 1
    for filename, entry in manifest["fragments"].items():
        assert "dirty_key" in entry
        assert "section_title" in entry
        assert "last_written_at" in entry
        assert filename.endswith(".md")


def test_render_dashboard_returns_fragment_summary_in_envelope(isolated_handoff: dict[str, Path]) -> None:
    """The dashboard render result must carry a fragments report so callers
    can see which sections changed without re-reading the manifest."""
    mcp_server.set_handoff_state(
        task_ref="WORKSTATE-REF-DEMO",
        objective="demo objective for envelope summary",
        status="in_progress",
    )
    result = mcp_server.render_handoff(kind="dashboard", write_file=True)
    data = result.get("data") if isinstance(result, dict) else None
    if data is None:
        data = result
    fragments_report = data.get("fragments")
    assert isinstance(fragments_report, dict)
    assert "written" in fragments_report
    assert "unchanged" in fragments_report
    assert isinstance(fragments_report["written"], list)
