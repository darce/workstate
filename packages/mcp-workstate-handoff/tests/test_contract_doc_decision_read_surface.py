"""Structural test: contract doc must publish a dedicated Decision Read Surface section.

implementation note (`search_handoff.decision_fields`) and implementation note
(`get_handoff_state.decision_*`) ship two halves of one operator-facing
workflow: discover decisions by FTS, then exact-read them by id/branch/
lane. The contract doc consolidates both into a single named section so
callers do not have to stitch the surface together from two disjoint
paragraphs.
"""

from __future__ import annotations

from pathlib import Path

CONTRACT_DOC = (
    Path(__file__).resolve().parents[3]
    / "packages"
    / "workstate-system"
    / "docs"
    / "agentic"
    / "contracts"
    / "workstate-handoff-mcp.md"
)


def test_contract_doc_has_decision_read_surface_section() -> None:
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    assert "## Decision Read Surface" in text, "missing top-level Decision Read Surface section"
    assert "### Discovery (`search_handoff.decision_fields`)" in text
    assert "### Exact Read (`get_handoff_state.decision_*`)" in text
    # Each half must include at least one runnable example fenced block.
    section_start = text.index("## Decision Read Surface")
    section_end = text.index("\n## ", section_start + 1)
    section = text[section_start:section_end]
    assert section.count("```python") >= 2, "section must include python examples for both halves"
    assert "decision_fields" in section
    assert "decision_id_prefix" in section
    assert "decision_branch" in section
