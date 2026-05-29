"""Regression guards for ``docs/agentic/rules/development-workflow.md``.

implementation note of WORKSTATE-REF-53 introduced a forward reference to "Dirty-Main Policy
below" in the control-plane section, but the slice-3 review (finding
WORKSTATE-REF-53-S1-BR-01) flagged that the referenced section was never landed.
These tests pin the dangling-reference contract so the doc stays
self-consistent.
"""

from __future__ import annotations

import re
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = PACKAGE_ROOT / "docs" / "agentic" / "rules" / "development-workflow.md"


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def test_dirty_main_policy_section_exists() -> None:
    text = _doc_text()
    assert re.search(r"^##\s+Dirty-Main Policy\s*$", text, flags=re.MULTILINE), (
        "WORKSTATE-REF-53-S1-BR-01: development-workflow.md references 'Dirty-Main "
        "Policy below' but no '## Dirty-Main Policy' heading exists. The "
        "forward reference is dangling."
    )


def test_dirty_main_policy_section_documents_required_facets() -> None:
    text = _doc_text()
    match = re.search(
        r"(?ms)^##\s+Dirty-Main Policy\s*$(.*?)(?=^##\s+|\Z)",
        text,
    )
    assert match, "Dirty-Main Policy section must be present (see prior test)."
    body = match.group(1).lower()

    # Protected-path dirty state: the policy must name what counts as
    # dirty so operators know which surface triggers the gate.
    assert "protected" in body, (
        "Dirty-Main Policy must define what 'dirty' means (protected paths)."
    )

    # Routine warn behavior on root main.
    assert "warn" in body, (
        "Dirty-Main Policy must document the routine warn-only behavior."
    )

    # Publish/close blocking boundary.
    assert "block" in body, (
        "Dirty-Main Policy must document the publish/close blocking boundary."
    )

    # Doctor remediation surface.
    assert "doctor" in body, (
        "Dirty-Main Policy must point at the doctor remediation command."
    )


def test_dirty_main_policy_forward_reference_resolvable() -> None:
    """The control-plane section's forward reference must resolve in-page."""
    text = _doc_text()
    forward_ref_idx = text.find("Dirty-Main Policy below")
    assert forward_ref_idx != -1, (
        "Sanity: control-plane section should keep its 'Dirty-Main Policy "
        "below' forward reference (this is what S1-BR-01 reported)."
    )
    section_idx = text.find("## Dirty-Main Policy", forward_ref_idx)
    assert section_idx != -1, (
        "Forward reference 'Dirty-Main Policy below' must point at a section "
        "that appears later in the doc."
    )


def test_compaction_ownership_section_exists() -> None:
    text = _doc_text()
    assert re.search(
        r"^##\s+Cross-Harness Compaction Ownership\s*$",
        text,
        flags=re.MULTILINE,
    ), (
        "WORKSTATE-REF-1 implementation note: development-workflow.md must document the compaction "
        "ownership split."
    )


def test_compaction_ownership_section_documents_canonical_split() -> None:
    text = _doc_text()
    match = re.search(
        r"(?ms)^##\s+Cross-Harness Compaction Ownership\s*$(.*?)(?=^##\s+|\Z)",
        text,
    )
    assert match, "Cross-Harness Compaction Ownership section must be present."
    body = match.group(1)

    assert "harness-protocol.yaml" in body, (
        "Compaction ownership docs must name the canonical YAML contract."
    )
    assert "skills/handoff-lifecycle/body.md" in body, (
        "Compaction ownership docs must name the canonical skill guidance source."
    )
    assert "make generate-agent-workflows" in body, (
        "Compaction ownership docs must include the regeneration command."
    )
    assert "make check-agent-workflows" in body, (
        "Compaction ownership docs must include the drift verification command."
    )
    # WORKSTATE-REF-1-BR3-04: the umbrella proof command was not invocable as written
    # because the bare `python3` interpreter usually lacks PyYAML. The doc
    # must name the WORKFLOWS_PYTHON override so operators can recover.
    assert "WORKFLOWS_PYTHON" in body, (
        "Compaction ownership docs must name the WORKFLOWS_PYTHON override "
        "so operators without pyenv can recover from missing PyYAML."
    )


def test_fixture_only_env_vars_subsection_documents_skip_active_task_probe() -> None:
    """WORKSTATE-REF-1-BR2-02: WORKSTATE_SKIP_ACTIVE_TASK_PROBE must be documented."""
    text = _doc_text()
    assert re.search(
        r"^####\s+Fixture-only env vars\s*\(un-audited\)\s*$",
        text,
        flags=re.MULTILINE,
    ), (
        "development-workflow.md must declare a fixture-only env vars "
        "subsection so operators can audit unaudited bypasses."
    )
    assert "WORKSTATE_SKIP_ACTIVE_TASK_PROBE" in text, (
        "The fixture-only subsection must name WORKSTATE_SKIP_ACTIVE_TASK_PROBE "
        "explicitly so a stray export does not silently suppress the "
        "maintenance-task warning."
    )


def test_canonical_loop_requires_plan_accept_before_task_start() -> None:
    """WORKSTATE-REF-72 implementation note: task-start must follow accepted plan baselines."""
    text = _doc_text()
    loop_match = re.search(
        r"(?ms)^##\s+Canonical Workflow Loop\s*$(.*?)(?=^##\s+|\Z)",
        text,
    )
    assert loop_match, "Canonical Workflow Loop section must be present."
    loop = loop_match.group(1)

    plan_accept = loop.find("make plan-accept TASK=<task-ref>")
    task_start = loop.find("make task-start TASK=<task-ref>")
    assert plan_accept != -1, (
        "Canonical loop must show the explicit plan acceptance step for "
        "plan-backed tasks."
    )
    assert task_start != -1, "Canonical loop must still show task-start."
    assert plan_accept < task_start, (
        "Plan-backed tasks must run make plan-accept before make task-start."
    )
    assert "plan_baseline_missing" in loop


def test_development_workflow_names_enforced_plan_baseline_gates() -> None:
    text = _doc_text()
    assert "task-start" in text and "review-ready" in text
    assert "accepted plan baseline" in text
    assert "plan_baseline_missing" in text


def _session_compaction_body() -> str:
    text = _doc_text()
    match = re.search(
        r"(?ms)^##\s+Session Compaction\s*$(.*?)(?=^##\s+|\Z)",
        text,
    )
    assert match, "Session Compaction section must be present."
    return match.group(1)


def test_session_compaction_documents_enabled_vs_wired_distinction() -> None:
    """WORKSTATE-REF-80 implementation note: an enabled compaction surface is not an installed
    automatic recorder, and doctor is the diagnostic path that distinguishes
    installed / drifted / optional-not-installed adapter wiring."""
    body = _session_compaction_body().lower()

    # Enabling/advisory state does not imply automatic stop-time recording.
    assert "automatic recorder" in body, (
        "Session Compaction must explain that an enabled compaction surface is "
        "not an installed automatic recorder."
    )
    # The doctor diagnostic path and its three adapter states.
    assert "doctor" in body
    assert "optional-not-installed" in body, (
        "Session Compaction must name optional-not-installed so a never-opted-in "
        "adapter is understood as visible-but-not-an-error."
    )
    assert "hook_adapter_drift" in body, (
        "Session Compaction must name the bootstrap doctor hook_adapter_drift "
        "finding for managed adapters that drifted."
    )


def test_session_compaction_documents_linked_worktree_hoist_warning_is_nonfatal() -> None:
    """WORKSTATE-REF-80 implementation note: missing-hoist git-hook warnings during linked-worktree
    creation are a non-fatal readiness diagnostic, not a task-start failure."""
    body = _session_compaction_body().lower()
    assert "hoist" in body, "Session Compaction must document git-hook hoist readiness."
    assert "non-fatal" in body, (
        "Session Compaction must classify the missing-hoist worktree warning as "
        "non-fatal so a successful task-start is not read as broken."
    )
    assert "worktree" in body
