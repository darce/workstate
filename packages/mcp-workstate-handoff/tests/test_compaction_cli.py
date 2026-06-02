"""implementation note implementation note — manual compaction CLI (`make compact-now`).

Smoke-level coverage that ``compaction_cli.main()`` writes a
``session_compactions`` row and prints ``compaction_id=<id>`` to stdout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig


@pytest.fixture()
def isolated_runtime(tmp_path: Path) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    return runtime


def test_cli_writes_session_compaction_row_and_prints_id(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from workstate_handoff_mcp import compaction_cli

    transcript = tmp_path / "transcript.md"
    transcript.write_text("Manual compact-now smoke transcript.\n")

    rc = compaction_cli.main(
        [
            "--workspace-root",
            str(isolated_runtime.workspace_root),
            "--state-dir",
            str(isolated_runtime.state_dir),
            "--current-task-path",
            str(isolated_runtime.current_task_path),
            "--task-ref",
            "WORKSTATE-REF-39",
            "--transcript",
            str(transcript),
            "--session-id",
            "manual-session-1",
        ]
    )
    assert rc == 0

    captured = capsys.readouterr()
    output_lines = [line.strip() for line in captured.out.splitlines() if line.strip()]
    assert output_lines[0] == "compaction_id=C-WORKSTATE-REF-39-0001"
    assert [line.split("=", 1)[0] for line in output_lines[:5]] == [
        "compaction_id",
        "tokens_saved_estimate",
        "input_chars",
        "summary_chars",
        "prose_residual_chars",
    ]
    for line in output_lines[1:5]:
        key, raw_value = line.split("=", 1)
        assert raw_value.isdigit(), f"{key} must be an integer receipt value; got {line!r}"

    stored = mcp_server.get_compaction("C-WORKSTATE-REF-39-0001")
    assert stored.task_ref == "WORKSTATE-REF-39"
    assert stored.harness == "manual"
    assert stored.session_id == "manual-session-1"


def test_cli_defaults_harness_to_manual_and_generates_session_id(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from workstate_handoff_mcp import compaction_cli

    transcript = tmp_path / "transcript.md"
    transcript.write_text("Defaults transcript.\n")

    rc = compaction_cli.main(
        [
            "--workspace-root",
            str(isolated_runtime.workspace_root),
            "--state-dir",
            str(isolated_runtime.state_dir),
            "--current-task-path",
            str(isolated_runtime.current_task_path),
            "--task-ref",
            "WORKSTATE-REF-39",
            "--transcript",
            str(transcript),
        ]
    )
    assert rc == 0

    captured = capsys.readouterr()
    assert "compaction_id=C-WORKSTATE-REF-39-0001" in captured.out

    stored = mcp_server.get_compaction("C-WORKSTATE-REF-39-0001")
    assert stored.harness == "manual"
    # Auto-generated session_id is non-empty.
    assert stored.session_id and isinstance(stored.session_id, str)


def test_cli_accepts_cursor_harness_alias_and_normalizes_to_vscode(
    isolated_runtime: RuntimeConfig,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`make compact-now --harness cursor` must accept and normalize the alias.

    Regression for the missed twin of BR3-02: the consolidated MCP entry
    point widened to accept ``cursor`` (normalized to ``vscode`` before
    storage), but the standalone CLI launcher must accept the same input
    set or operators get an argparse failure before normalization runs.
    """
    from workstate_handoff_mcp import compaction_cli

    transcript = tmp_path / "transcript.md"
    transcript.write_text("Cursor alias transcript.\n")

    rc = compaction_cli.main(
        [
            "--workspace-root",
            str(isolated_runtime.workspace_root),
            "--state-dir",
            str(isolated_runtime.state_dir),
            "--current-task-path",
            str(isolated_runtime.current_task_path),
            "--task-ref",
            "WORKSTATE-REF-39",
            "--transcript",
            str(transcript),
            "--harness",
            "cursor",
            "--session-id",
            "cursor-session-1",
        ]
    )
    assert rc == 0

    stored = mcp_server.get_compaction("C-WORKSTATE-REF-39-0001")
    assert stored.harness == "vscode", (
        "compaction_cli must normalize the 'cursor' input alias to "
        "'vscode' the same way the consolidated MCP tool does."
    )
