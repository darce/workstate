"""implementation note implementation note — DASHBOARD.d/ fragment files."""

from __future__ import annotations

import json
from pathlib import Path

SAMPLE_MARKDOWN = """\
# Workstate Handoff Dashboard

## Needs Attention
- task A is blocked

## All Tasks
| ref | status |
|-----|--------|
| A   | open   |

## Open Findings
- finding 1
"""


def test_collect_dashboard_fragments_splits_per_section() -> None:
    from workstate_handoff_mcp.dashboard_fragments import collect_dashboard_fragments

    fragments = collect_dashboard_fragments(SAMPLE_MARKDOWN)

    assert len(fragments) >= 3
    filenames = [f.filename for f in fragments]
    assert any("needs_attention" in fn for fn in filenames)
    assert any("all_tasks" in fn for fn in filenames)
    assert any("open_findings" in fn for fn in filenames)

    for fragment in fragments:
        assert fragment.content
        assert fragment.dirty_key  # hash for change-detection


def test_maybe_write_dashboard_fragments_writes_all_on_first_run(tmp_path: Path) -> None:
    from workstate_handoff_mcp.dashboard_fragments import (
        collect_dashboard_fragments,
        maybe_write_dashboard_fragments,
    )

    fragments = collect_dashboard_fragments(SAMPLE_MARKDOWN)
    result = maybe_write_dashboard_fragments(tmp_path, fragments)

    assert result["written"] == [f.filename for f in fragments]
    fragment_dir = tmp_path / "DASHBOARD.d"
    for fragment in fragments:
        assert (fragment_dir / fragment.filename).read_text() == fragment.content

    manifest_path = tmp_path / "dashboard_fragments.manifest.json"
    manifest = json.loads(manifest_path.read_text())
    assert set(manifest["fragments"]) == {f.filename for f in fragments}


def test_maybe_write_dashboard_fragments_skips_unchanged_on_second_run(tmp_path: Path) -> None:
    from workstate_handoff_mcp.dashboard_fragments import (
        collect_dashboard_fragments,
        maybe_write_dashboard_fragments,
    )

    fragments = collect_dashboard_fragments(SAMPLE_MARKDOWN)
    maybe_write_dashboard_fragments(tmp_path, fragments)

    second = maybe_write_dashboard_fragments(tmp_path, fragments)
    assert second["written"] == []  # nothing dirty, nothing written
    assert set(second["unchanged"]) == {f.filename for f in fragments}


def test_maybe_write_dashboard_fragments_writes_only_changed_section(tmp_path: Path) -> None:
    from workstate_handoff_mcp.dashboard_fragments import (
        collect_dashboard_fragments,
        maybe_write_dashboard_fragments,
    )

    first = collect_dashboard_fragments(SAMPLE_MARKDOWN)
    maybe_write_dashboard_fragments(tmp_path, first)

    changed_markdown = SAMPLE_MARKDOWN.replace("- finding 1", "- finding 1\n- finding 2")
    second = collect_dashboard_fragments(changed_markdown)
    result = maybe_write_dashboard_fragments(tmp_path, second)

    assert len(result["written"]) == 1
    assert "open_findings" in result["written"][0]
