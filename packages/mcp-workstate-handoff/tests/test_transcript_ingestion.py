from __future__ import annotations

import json
from pathlib import Path

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.compaction import CompactionSettings, _read_transcript
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
    mcp_server.set_handoff_state(task_ref="WS-CMPERR-01", objective="implementation note", status="in_progress")
    return runtime


def _tool_result_heavy_jsonl(*, target_bytes: int) -> str:
    payload = "X" * 8_000
    tool_result_line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_test",
                        "content": payload,
                    }
                ],
            },
        }
    )
    lines = [tool_result_line]
    while sum(len(line) + 1 for line in lines) < target_bytes:
        lines.append(tool_result_line)
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "turn 42 assistant: compaction summary"}],
                },
            }
        )
    )
    return "\n".join(lines) + "\n"


def test_large_claude_jsonl_transcript_compacts(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    transcript_path = tmp_path / "large.jsonl"
    transcript_path.write_text(_tool_result_heavy_jsonl(target_bytes=1_100_000), encoding="utf-8")
    assert transcript_path.stat().st_size > 1_000_000

    receipt = mcp_server.compact_session(
        transcript_path=transcript_path,
        task_ref="WS-CMPERR-01",
        harness="claude-code",
        session_id="slice-1-large-jsonl",
    )

    assert receipt.compaction_id.startswith("C-WS-CMPERR-01-")


def test_plain_text_transcript_passes_through_unchanged(tmp_path: Path) -> None:
    plain = "Residual-only transcript content for compaction.\n"
    transcript_path = tmp_path / "plain.md"
    transcript_path.write_text(plain, encoding="utf-8")

    loaded = _read_transcript(transcript_path)

    assert loaded == plain


def test_oversized_raw_transcript_rejected_at_backstop(tmp_path: Path) -> None:
    transcript_path = tmp_path / "oversized.txt"
    transcript_path.write_bytes(b"x" * 5_000)
    settings = CompactionSettings(max_transcript_bytes=1_024)

    with pytest.raises(ValueError, match="max_transcript_bytes"):
        _read_transcript(transcript_path, settings=settings)


def test_jsonl_slimming_drops_tool_result_bodies(tmp_path: Path) -> None:
    transcript_path = tmp_path / "slim.jsonl"
    transcript_path.write_text(_tool_result_heavy_jsonl(target_bytes=50_000), encoding="utf-8")

    loaded = _read_transcript(transcript_path)

    assert "tool_result" not in loaded
    assert "assistant: turn 42 assistant: compaction summary" in loaded
    assert len(loaded) < transcript_path.stat().st_size


def test_extract_chars_cap_keeps_tail_with_marker(tmp_path: Path) -> None:
    transcript_path = tmp_path / "tail.jsonl"
    lines = [
        json.dumps(
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": [{"type": "text", "text": f"line-{index}"}]},
            }
        )
        for index in range(200)
    ]
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    settings = CompactionSettings(max_extract_chars=500)

    loaded = _read_transcript(transcript_path, settings=settings)

    assert loaded.startswith("[transcript clipped:")
    assert "line-199" in loaded
    assert "line-0" not in loaded


def test_compaction_settings_reads_transcript_cap_env_names() -> None:
    env = {
        "WORKSTATE_HANDOFF_COMPACTION_MAX_TRANSCRIPT_BYTES": "1048576",
        "WORKSTATE_HANDOFF_COMPACTION_MAX_EXTRACT_CHARS": "8192",
    }
    settings = CompactionSettings.from_env(env=env)
    assert settings.max_transcript_bytes == 1_048_576
    assert settings.max_extract_chars == 8_192


def test_compaction_settings_transcript_defaults_when_unset() -> None:
    settings = CompactionSettings.from_env(env={})
    assert settings.max_transcript_bytes == 52_428_800
    assert settings.max_extract_chars == 400_000


def test_codex_shaped_jsonl_keeps_tagged_user_assistant_lines(tmp_path: Path) -> None:
    transcript_path = tmp_path / "codex.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "message": {"role": "user", "content": "user: run make test-handoff"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "assistant: tests passed"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    loaded = _read_transcript(transcript_path)

    assert "user: run make test-handoff" in loaded
    assert "assistant: tests passed" in loaded


def test_jsonl_with_only_droppable_records_falls_back_to_raw(tmp_path: Path) -> None:
    transcript_path = tmp_path / "droppable.jsonl"
    lines = [json.dumps({"type": "system", "content": f"system-{index}"}) for index in range(4)]
    transcript_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    loaded = _read_transcript(transcript_path)

    assert loaded
    assert "system-0" in loaded


def test_jsonl_detection_tolerates_non_object_first_line(tmp_path: Path) -> None:
    transcript_path = tmp_path / "array-first.jsonl"
    payload = "X" * 8_000
    dict_line = json.dumps(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "toolu_test", "content": payload}],
            },
        }
    )
    text_line = json.dumps(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "kept tail"}]},
        }
    )
    transcript_path.write_text(
        "\n".join([json.dumps(["array", "first"]), dict_line, dict_line, dict_line, text_line]) + "\n",
        encoding="utf-8",
    )

    loaded = _read_transcript(transcript_path)

    assert "tool_result" not in loaded
    assert "assistant: kept tail" in loaded


def test_plain_text_with_leading_json_object_line_not_misdetected(tmp_path: Path) -> None:
    plain = (
        '{"note": "a stray JSON object line"}\n'
        "Plain prose line one.\n"
        "Plain prose line two.\n"
        "Plain prose line three.\n"
        "Plain prose line four.\n"
    )
    transcript_path = tmp_path / "prose-with-json-head.md"
    transcript_path.write_text(plain, encoding="utf-8")

    loaded = _read_transcript(transcript_path)

    assert loaded == plain


def test_tiny_extract_cap_keeps_raw_tail_instead_of_broken_marker(tmp_path: Path) -> None:
    plain = "Plain prose transcript body that exceeds the tiny cap.\n"
    transcript_path = tmp_path / "tiny-cap.md"
    transcript_path.write_text(plain, encoding="utf-8")
    settings = CompactionSettings(max_extract_chars=10)

    loaded = _read_transcript(transcript_path, settings=settings)

    assert loaded == plain[-10:]
    assert not loaded.startswith("[")
