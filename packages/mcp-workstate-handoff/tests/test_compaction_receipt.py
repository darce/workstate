"""WORKSTATE-REF-67 implementation note: ``compact_session`` returns a ``CompactionRecordReceipt``.

Shape contract (WORKSTATE-REF-004 fix from the task plan): the receipt **inlines** the
``StructuredSummary`` rather than duplicating its counts. Callers read counts
as ``len(receipt.summary.decisions)`` etc. — no parallel ``*_count`` fields.

Receipt field semantics (WORKSTATE-REF-007 fix — chars/4 divisor lineage):
``harness-protocol.yaml`` line 126-127 documents the ``chars / 4`` fallback
estimator (``70_000 * 4 = 280_000``). ``tokens_saved_estimate`` reuses that
divisor on ``(input_chars - summary_chars - prose_residual_chars)`` clamped
non-negative.

The legacy bare-string wrapper ``api.compact_session`` is removed by this
slice — all internal callers consume the receipt directly. The audit that
gates the removal is `claude_WORKSTATE67_slice2_caller_audit_compact_session_wrapper`
(decision id 662).
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from workstate_protocol import StructuredSummary

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.compaction import (
    CompactionRecordReceipt,
    compact_session,
    format_compaction_record_receipt_lines,
)
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


def test_compact_session_returns_typed_receipt(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    transcript_path = tmp_path / "transcript.md"
    transcript_text = "Residual transcript content used to size the receipt.\n"
    transcript_path.write_text(transcript_text)

    receipt = compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-67",
        harness="codex",
        session_id="session-receipt-1",
    )

    assert isinstance(receipt, CompactionRecordReceipt)
    assert receipt.compaction_id == "C-WORKSTATE-REF-67-0001"
    assert isinstance(receipt.summary, StructuredSummary)
    assert receipt.summary.compaction_id == receipt.compaction_id
    assert receipt.summary.session_id == "session-receipt-1"


def test_receipt_inlines_structured_summary_no_duplicate_counts(
    isolated_runtime: RuntimeConfig, tmp_path: Path
) -> None:
    """WORKSTATE-REF-004: receipt has no parallel ``*_count`` fields; callers read
    ``len(receipt.summary.decisions)`` etc. directly."""
    transcript_path = tmp_path / "t.md"
    transcript_path.write_text("any\n")

    receipt = compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-67",
        harness="codex",
        session_id="session-receipt-no-dup",
    )

    receipt_fields = set(CompactionRecordReceipt.model_fields.keys())
    forbidden = {
        "decisions_count",
        "findings_fixed_count",
        "findings_opened_count",
        "tests_verified_count",
        "files_touched_count",
    }
    assert forbidden.isdisjoint(receipt_fields), (
        f"Receipt must inline StructuredSummary, not duplicate its counts. "
        f"Forbidden fields present: {forbidden & receipt_fields}"
    )
    # Sanity: counts are reachable through the inlined summary.
    assert len(receipt.summary.decisions) == 0  # nothing seeded
    assert len(receipt.summary.files_touched) == 0


def test_receipt_char_counts_and_tokens_saved_estimate(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    """WORKSTATE-REF-007: ``tokens_saved_estimate`` reuses the chars/4 divisor from
    ``harness-protocol.yaml`` lines 126-127 on
    ``(input_chars - summary_chars - prose_residual_chars)`` clamped >= 0."""
    transcript_path = tmp_path / "t.md"
    transcript_text = "A short transcript.\n"
    transcript_path.write_text(transcript_text)

    receipt = compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-67",
        harness="codex",
        session_id="session-receipt-chars",
    )

    assert receipt.input_chars == len(transcript_text)
    assert receipt.summary_chars == len(receipt.summary.model_dump_json())
    assert receipt.prose_residual_chars == len(receipt.summary.prose_residual or "")
    expected = max(
        0,
        (receipt.input_chars - receipt.summary_chars - receipt.prose_residual_chars) // 4,
    )
    assert receipt.tokens_saved_estimate == expected


def test_receipt_operator_lines_preserve_id_first(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    transcript_path = tmp_path / "t.md"
    transcript_path.write_text("Operator receipt transcript.\n")

    receipt = compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-67",
        harness="codex",
        session_id="session-receipt-lines",
    )

    assert format_compaction_record_receipt_lines(receipt) == [
        "compaction_id=C-WORKSTATE-REF-67-0001",
        f"tokens_saved_estimate={receipt.tokens_saved_estimate}",
        f"input_chars={receipt.input_chars}",
        f"raw_input_bytes={receipt.raw_input_bytes}",
        f"summary_chars={receipt.summary_chars}",
        f"prose_residual_chars={receipt.prose_residual_chars}",
    ]


def test_receipt_db_row_id_matches_insert_lastrowid(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    """``db_row_id`` is the lastrowid captured from the INSERT cursor."""
    transcript_path = tmp_path / "t.md"
    transcript_path.write_text("first\n")
    first = compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-67",
        harness="codex",
        session_id="s-1",
    )

    transcript_path2 = tmp_path / "t2.md"
    transcript_path2.write_text("second\n")
    second = compact_session(
        transcript_path=transcript_path2,
        task_ref="WORKSTATE-REF-67",
        harness="codex",
        session_id="s-2",
    )

    assert isinstance(first.db_row_id, int)
    assert isinstance(second.db_row_id, int)
    assert second.db_row_id > first.db_row_id


def test_receipt_summary_equals_get_compaction_dereference(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    """The receipt's inlined summary equals what ``get_compaction(id)`` returns."""
    transcript_path = tmp_path / "t.md"
    transcript_path.write_text("contents\n")

    receipt = compact_session(
        transcript_path=transcript_path,
        task_ref="WORKSTATE-REF-67",
        harness="codex",
        session_id="session-receipt-dereference",
    )

    fetched = mcp_server.get_compaction(receipt.compaction_id)
    assert isinstance(fetched, StructuredSummary)
    assert fetched.model_dump(mode="json") == receipt.summary.model_dump(mode="json")


def test_mcp_record_op_returns_receipt_dict(isolated_runtime: RuntimeConfig, tmp_path: Path) -> None:
    """``compaction(operation="record", ...)`` returns the receipt as a dict
    (model_dump(mode="json")) so MCP clients see the typed shape on the wire."""
    transcript_path = tmp_path / "t.md"
    transcript_path.write_text("hello\n")

    result = mcp_server.compaction(
        cast(
            "mcp_server.CompactionParam",
            {
                "operation": "record",
                "transcript_path": str(transcript_path),
                "task_ref": "WORKSTATE-REF-67",
                "harness": "codex",
                "session_id": "session-receipt-mcp",
            },
        )
    )

    assert isinstance(result, dict)
    assert {
        "compaction_id",
        "summary",
        "input_chars",
        "raw_input_bytes",
        "summary_chars",
        "prose_residual_chars",
        "tokens_saved_estimate",
        "db_row_id",
    }.issubset(result.keys()), f"missing receipt keys; got {sorted(result.keys())}"
    assert result["compaction_id"] == "C-WORKSTATE-REF-67-0001"
    # summary is inlined as a dict-shaped StructuredSummary
    assert isinstance(result["summary"], dict)
    assert result["summary"]["compaction_id"] == result["compaction_id"]


def test_api_compact_session_wrapper_removed() -> None:
    """WORKSTATE-REF-005: the bare-string wrapper ``api.compact_session`` is deleted.

    All internal callers were enumerated in the audit decision
    ``claude_WORKSTATE67_slice2_caller_audit_compact_session_wrapper`` (id 662)
    and migrated to consume the receipt directly. No external caller was
    identified, so no compat shim is justified.

    The implementation symbol ``compaction.compact_session`` (re-exported as
    ``workstate_handoff_mcp.compact_session``) is preserved, but it now returns
    ``CompactionRecordReceipt`` rather than a bare ``str``.
    """
    # Wrapper must not be defined on the api module itself.
    import workstate_handoff_mcp.api as api_module

    api_locals = {name: getattr(api_module, name, None) for name in ("compact_session",)}
    # The implementation symbol is re-exported on the package via
    # `from .api import compact_session`. After implementation note, the api-layer
    # wrapper is gone but the implementation re-export is preserved.
    cs = api_locals["compact_session"]
    # Either it points to the implementation in `compaction` module, OR it
    # has been removed. Both states are acceptable; what is NOT acceptable
    # is a wrapper whose return annotation is `str`.
    if cs is not None:
        import inspect

        sig = inspect.signature(cs)
        assert sig.return_annotation in (
            CompactionRecordReceipt,
            "CompactionRecordReceipt",
        ), (
            f"api.compact_session must either be deleted or aliased to the "
            f"impl returning CompactionRecordReceipt. Got return annotation: "
            f"{sig.return_annotation!r}"
        )
