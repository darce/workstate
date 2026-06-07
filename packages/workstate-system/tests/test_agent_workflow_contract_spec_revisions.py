"""APM-SP-01..05 spec revision invariants (planning-review run 239)."""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SPEC = REPO_ROOT / "packages" / "workstate-system" / "docs" / "specs" / "agent-workflow-contract-spec.md"


def _read() -> str:
    return SPEC.read_text(encoding="utf-8")


def test_tr5_sequences_after_tr4_apm_sp_01() -> None:
    text = _read()
    tr5_idx = text.index("### TR5.")
    tr4_idx = text.index("### TR4.")
    assert tr4_idx < tr5_idx
    tr5_block = text[tr5_idx : text.index("### TR6.")]
    assert "TR4" in tr5_block, "TR5 must explicitly sequence after TR4 (APM-SP-01)"


def test_tr7_moved_out_of_recommendations_apm_sp_02() -> None:
    text = _read()
    recs_start = text.index("## Recommendations to Specify")
    recs_end = text.index("## Spec Pipeline Status")
    recs = text[recs_start:recs_end]
    assert "### TR7." not in recs, "TR7 must not live under Recommendations to Specify (APM-SP-02)"
    out_of_scope = text[text.index("## Out of Scope") : text.index("## Recommendations to Specify")]
    assert "template" in out_of_scope.lower() or "consumer" in out_of_scope.lower(), (
        "TR7 prose must be relocated into Out of Scope or an explicit scope-marker section (APM-SP-02)"
    )


def test_references_uses_live_queries_apm_sp_03() -> None:
    text = _read()
    refs = text[text.index("## References") :]
    assert "516" not in refs and "517" not in refs and "518" not in refs and "520" not in refs, (
        "References must not embed finding numeric ids; use live queries (APM-SP-03)"
    )
    assert "231" not in refs, "References must not embed review_run numeric id 231 (APM-SP-03)"
    assert "907b9bb" not in refs and "907b" not in refs.lower(), (
        "References must not embed transient short-SHA citations (APM-SP-03)"
    )
    assert "review_findings" in refs or "review_runs" in refs, (
        "References must point at a live-query syntax (APM-SP-03)"
    )


def test_pipeline_status_is_live_queryable_apm_sp_04() -> None:
    text = _read()
    pipeline = text[text.index("## Spec Pipeline Status") : text.index("## References")]
    assert "review_runs" in pipeline, (
        "Spec Pipeline Status must point at live review_runs queries, not a static checklist (APM-SP-04)"
    )


def test_in_scope_collapses_to_index_apm_sp_05() -> None:
    text = _read()
    in_scope = text[text.index("## In Scope") : text.index("## Out of Scope")]
    bullets = [line for line in in_scope.splitlines() if line.lstrip().startswith("- ")]
    assert len(bullets) <= 3, (
        f"In Scope must be a one-paragraph index (<=3 bullets) to avoid duplicating TR1..TR7; got {len(bullets)} (APM-SP-05)"
    )
