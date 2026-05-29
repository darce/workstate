"""Regression guards for the canonical handoff-lifecycle skill guidance."""

from __future__ import annotations

from pathlib import Path


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = PACKAGE_ROOT / "skills" / "handoff-lifecycle" / "body.md"


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_handoff_lifecycle_documents_compaction_advisory_flow() -> None:
    text = _doc_text()

    assert "compaction_recommended: true" in text, (
        "WORKSTATE-REF-1 implementation note: handoff-lifecycle guidance must tell the agent when "
        "the compaction advisory has fired."
    )
    assert 'compaction(operation="record"' in text, (
        "WORKSTATE-REF-1 implementation note: handoff-lifecycle guidance must name the MCP "
        "compaction record call explicitly."
    )


def test_handoff_lifecycle_documents_transcript_discovery_contract() -> None:
    text = _doc_text()

    assert "env_var" in text and "fallback_glob" in text, (
        "WORKSTATE-REF-1 implementation note: handoff-lifecycle guidance must describe the "
        "contract-driven transcript discovery order."
    )
    assert "unknown_harness: warn_and_skip" in text, (
        "WORKSTATE-REF-1 implementation note: handoff-lifecycle guidance must preserve the "
        "contract's unknown-harness behavior."
    )
