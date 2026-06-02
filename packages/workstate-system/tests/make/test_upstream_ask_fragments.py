"""implementation note Task 1 — asks D & E: canonical Make-fragment fixes.

These guard two consumer-unblocking gaps the downstream
`workstate-migration-upstream-asks.md` reported:

- **D**: `lifecycle.mk` `tasks-gc` invoked the retired `mcp-workstate-handoff
  tasks-gc` subcommand. The real gc path is `archive --operation gc`
  (handoff CLI `_dispatch_archive`, `--operation archive|gc|get`).
- **E**: `workflows.mk` `check-agent-workflows` ran the generator `--check`,
  the facade check, and the settings-pin check, but **not**
  `--check-codex-router-blocks` (which the generator supports), dropping codex
  router-block coverage that the consumer had to re-add locally.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
GENERATOR = REPO_ROOT / "packages" / "workstate-system" / "scripts" / "generate_agent_workflows.py"

FRAGMENTS = Path(__file__).resolve().parents[2] / "Makefile.d"
LIFECYCLE_MK = FRAGMENTS / "lifecycle.mk"
WORKFLOWS_MK = FRAGMENTS / "workflows.mk"


def _recipe_lines(mk: Path, target: str) -> list[str]:
    """Return the recipe lines for ``target``.

    Handles both the inline form (``target: ; @cmd``) and the multi-line form
    (``target: deps`` followed by tab-indented recipe lines).
    """
    lines = mk.read_text(encoding="utf-8").splitlines()
    recipe: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if not (stripped.startswith(f"{target}:") and not stripped.startswith(f"{target}::")):
            continue
        head = line.split(":", 1)[1]
        if ";" in head:  # inline recipe
            recipe.append(head.split(";", 1)[1].strip())
        # collect following tab-indented recipe lines
        for follow in lines[i + 1 :]:
            if follow.startswith("\t"):
                recipe.append(follow.strip())
            elif follow.strip() == "" or follow.lstrip().startswith("#"):
                continue
            else:
                break
        break
    return recipe


def test_d_tasks_gc_uses_archive_gc_subcommand() -> None:
    recipe = "\n".join(_recipe_lines(LIFECYCLE_MK, "tasks-gc"))
    assert recipe, "tasks-gc target/recipe not found in lifecycle.mk"
    # The real gc path, preserving the APPLY -> --apply flag the gc op accepts.
    assert "archive --operation gc" in recipe, recipe
    # The retired bare subcommand invocation must be gone.
    assert "mcp-workstate-handoff tasks-gc" not in recipe, recipe
    # APPLY=1 must still forward --apply (gc is dry-run by default).
    assert "$(if $(APPLY),--apply)" in recipe, recipe


def test_e_check_agent_workflows_runs_codex_router_block_check() -> None:
    recipe = "\n".join(_recipe_lines(WORKFLOWS_MK, "check-agent-workflows"))
    assert recipe, "check-agent-workflows recipe not found in workflows.mk"
    assert "--check-codex-router-blocks" in recipe, recipe


def test_e_codex_router_root_resolves_to_git_toplevel_via_make(tmp_path: Path) -> None:
    """Couple to the Makefile wiring (not just the generator): include the real
    workflows.mk fragment and assert ``CODEX_ROUTER_ROOT`` resolves — through
    make — to the git top-level, where the consumer docs live, NOT to
    WORKFLOWS_ROOT (the package root). A regression in the make var (wrong
    offset, dropped `git rev-parse`, mangled `$(if ...)`) fails here."""
    import shutil

    if shutil.which("make") is None or shutil.which("git") is None:
        import pytest

        pytest.skip("make/git required")
    wrapper = tmp_path / "Makefile"
    wrapper.write_text(
        f"include {WORKFLOWS_MK}\n"
        "print-codex-root:\n\t@echo \"$(CODEX_ROUTER_ROOT)\"\n",
        encoding="utf-8",
    )
    out = subprocess.run(
        ["make", "-s", "-f", str(wrapper), "print-codex-root"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    resolved = out.stdout.strip()
    toplevel = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert resolved == toplevel, f"CODEX_ROUTER_ROOT={resolved!r} != git toplevel {toplevel!r}"
    # And it must NOT be the package root (the bug the wiring fixes).
    assert resolved != str((WORKFLOWS_MK.parent).parent), resolved


def test_e_codex_router_block_check_passes_against_resolved_root() -> None:
    """Behavioral: the generator's codex-router-block check passes when targeted
    at the repo root (the root the make wiring resolves to). Guards real drift
    in CLAUDE.md / docs/workstate/instructions.md."""
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check-codex-router-blocks", "--target", str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
