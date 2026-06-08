from __future__ import annotations

import json
from pathlib import Path

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.compaction import (
    PROSE_RESIDUAL_SOFT_LIMIT_CHARS,
    CompactionRecordReceipt,
    format_compaction_record_receipt_lines,
)
from workstate_handoff_mcp.config import RuntimeConfig


def _configure(tmp_path: Path) -> RuntimeConfig:
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    runtime = RuntimeConfig.for_workspace(
        tmp_path,
        state_dir=state_dir,
        current_task_path=tmp_path / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    mcp_server.set_handoff_state(task_ref="WS-CMPERR-01", objective="implementation note", status="in_progress")
    return runtime


def test_hard_residual_clips_instead_of_raises(tmp_path: Path) -> None:
    _configure(tmp_path)
    transcript_path = tmp_path / "hard.md"
    transcript_path.write_text("z" * 17_000)

    receipt = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WS-CMPERR-01",
        harness="codex",
        session_id="slice-2-hard-residual",
    )
    stored = mcp_server.get_compaction(receipt.compaction_id)

    assert stored.prose_residual is not None
    assert stored.prose_residual.endswith("chars omitted]")
    assert len(stored.prose_residual) <= PROSE_RESIDUAL_SOFT_LIMIT_CHARS + 64


def test_receipt_raw_input_bytes_exceeds_slimmed_input_chars(tmp_path: Path) -> None:
    _configure(tmp_path)
    payload = "Q" * 8_000
    line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_slice2", "content": payload}],
            },
        }
    )
    lines = [line] * 120
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "assistant: implementation note summary"}],
                },
            }
        )
    )
    transcript_path = tmp_path / "large.jsonl"
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    raw_bytes = transcript_path.stat().st_size

    receipt = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WS-CMPERR-01",
        harness="claude-code",
        session_id="slice-2-raw-bytes",
    )

    assert isinstance(receipt, CompactionRecordReceipt)
    assert receipt.raw_input_bytes == raw_bytes
    assert receipt.raw_input_bytes > receipt.input_chars


def test_receipt_operator_lines_include_raw_input_bytes(tmp_path: Path) -> None:
    _configure(tmp_path)
    transcript_path = tmp_path / "plain.md"
    transcript_path.write_text("plain transcript for receipt lines\n", encoding="utf-8")
    raw_bytes = transcript_path.stat().st_size

    receipt = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WS-CMPERR-01",
        harness="codex",
        session_id="slice-2-receipt-lines",
    )

    assert format_compaction_record_receipt_lines(receipt) == [
        f"compaction_id={receipt.compaction_id}",
        f"tokens_saved_estimate={receipt.tokens_saved_estimate}",
        f"input_chars={receipt.input_chars}",
        f"raw_input_bytes={raw_bytes}",
        f"summary_chars={receipt.summary_chars}",
        f"prose_residual_chars={receipt.prose_residual_chars}",
    ]
