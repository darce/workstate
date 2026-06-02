"""Contract test for WORKSTATE-REF-52 (implementation note onward).

After implementation note's resolver precedence collapse, the per-caller inventory previously
enforced here is retired. The new resolver follows a single uniform rule
(caller cwd HEAD wins; explicit ``WriteActor`` is the opt-out), so there is no
per-caller policy table to keep in lockstep.

This test now enforces two invariants:

1. The attribution-policy doc still exists and is non-empty (it documents the
   uniform rule and the explicit-actor opt-out).
2. The deleted ``_cwd_inside_task_worktree`` probe has not regrown. Its presence
   would indicate a regression toward the implementation note cwd-membership gate, which the
   precedence collapse intentionally removed.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DOC_PATH = REPO_ROOT / "packages" / "mcp-workstate-handoff" / "docs" / "contracts" / "write-actor-attribution.md"
RESOLVER_PATH = (
    REPO_ROOT / "packages" / "mcp-workstate-handoff" / "src" / "workstate_handoff_mcp" / "shared_write_context.py"
)


def test_attribution_doc_exists() -> None:
    assert DOC_PATH.exists(), f"WORKSTATE-REF-52 attribution-policy doc is missing: {DOC_PATH.relative_to(REPO_ROOT)}"
    assert DOC_PATH.read_text(encoding="utf-8").strip(), "WORKSTATE-REF-52 attribution-policy doc is empty"


def test_resolver_has_not_regrown_cwd_membership_probe() -> None:
    """implementation note deleted ``_cwd_inside_task_worktree``; re-introducing it would
    revive the per-caller cwd-membership gate the precedence collapse retired.
    """
    source = RESOLVER_PATH.read_text(encoding="utf-8")
    assert "_cwd_inside_task_worktree" not in source, (
        "WORKSTATE-REF-52 implementation note collapsed the resolver precedence. "
        "Reintroducing _cwd_inside_task_worktree resurrects the cwd-membership "
        "gate that the collapse retired — pass an explicit `WriteActor` instead."
    )
