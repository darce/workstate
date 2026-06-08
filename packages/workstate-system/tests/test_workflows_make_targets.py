"""implementation note S2 completion — `make generate` + a regenerate-first check.

Once the generated adapters are gitignored (S2.2), the workflow check can no
longer `--check` them against committed copies — a fresh clone has none, so the
bare legacy `--check` would fail with "missing generated file". The check must
instead REGENERATE the adapters (producing the gitignored outputs) and rely on
the independent `--check-codex-router-blocks` pass for the tracked
source-embedded router blocks. This also gives operators a `make generate`
entry point.

These assertions run against the tracked SOURCE Makefile fragment
(`packages/workstate-system/Makefile.d/workflows.mk`) — the artifact that ships
and materialises into consumer/root overlays — so they are CI-safe and do not
depend on a materialised overlay copy.
"""

from __future__ import annotations

from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
WORKFLOWS_MK = PAYLOAD_ROOT / "Makefile.d" / "workflows.mk"


def _recipe_block(text: str, target: str) -> list[str]:
    """Return the lines of a Make target's recipe (the target line + its
    tab-indented recipe lines), stopping at the next non-indented line."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith(f"{target}:"))
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line and not line.startswith(("\t", " ")):
            break
        block.append(line)
    return block


def test_make_generate_alias_defined() -> None:
    text = WORKFLOWS_MK.read_text(encoding="utf-8")
    assert any(
        line.startswith("generate:") for line in text.splitlines()
    ), "workflows.mk must define a `generate` target (the regenerate entry point)"


def test_check_agent_workflows_regenerates_rather_than_check_only() -> None:
    text = WORKFLOWS_MK.read_text(encoding="utf-8")
    recipe = _recipe_block(text, "check-agent-workflows")
    # Router-block drift is still validated against source (compare, not write).
    assert any(
        "--check-codex-router-blocks" in line for line in recipe
    ), "check-agent-workflows must still validate codex router blocks"
    # But it must NOT gate on a bare legacy `--check` of the now-gitignored
    # adapters — a fresh clone has no committed copies to compare against.
    assert not any(
        line.rstrip().endswith(" --check") for line in recipe
    ), (
        "check-agent-workflows must regenerate the adapters, not `--check` them "
        "against committed copies (they are gitignored since S2.2)"
    )


def test_check_agent_workflows_has_router_drift_gate() -> None:
    """implementation note S3 (revA-standalone-router-doc-no-drift-gate +
    revA-check-target-writes-tracked-files): the regenerate HEALS the tracked
    generator outputs in place, then `--check-codex-router-blocks` runs against
    the healed tree — so neither catches committed drift of the standalone
    codex-command-router.md (which the block check does not cover at all). The
    target must therefore fail loud via a `git diff --exit-code` on the tracked
    generator outputs after the regenerate."""
    text = WORKFLOWS_MK.read_text(encoding="utf-8")
    recipe = "\n".join(_recipe_block(text, "check-agent-workflows"))
    assert "git" in recipe and "diff" in recipe and "--exit-code" in recipe, (
        "check-agent-workflows must fail loud (git diff --exit-code) on a "
        "regenerate that mutated a tracked router output"
    )
    assert "codex-command-router.md" in recipe, (
        "the drift gate must cover the standalone codex-command-router.md, which "
        "--check-codex-router-blocks does not validate"
    )
