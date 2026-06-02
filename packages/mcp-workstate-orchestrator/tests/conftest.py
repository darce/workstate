"""Pytest session bootstrap for ``workstate-orchestrator-mcp``.

Three responsibilities:

1. Put this package's ``src/`` and the sibling ``workstate-handoff-mcp``
   ``src/`` on ``sys.path`` so direct ``pytest`` invocations from the
   package directory work without an editable install AND so the
   handoff-mcp import resolves to *this* worktree's source rather than
   to whatever the editable install points at.
2. Hard-fail the session if either ``workstate_handoff_mcp`` or
   ``workstate_orchestrator_mcp`` resolves to a path outside this worktree.
   The orchestrator package depends on ``workstate_handoff_mcp`` (via the
   editable install), so if pytest is invoked from a linked worktree
   but the editable install points at the root checkout,
   ``import workstate_handoff_mcp`` resolves to the root's source code and
   the linked worktree's refactor never gets exercised. WORKSTATE-REF-10
   regressed 65 orchestrator tests because of exactly this bug; the
   guard catches that class of false-positive verification at session
   start.
3. Mark the session so the workstate-handoff-mcp commit-SHA validator
   accepts synthetic test SHAs without resolving them through git.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATOR_SRC = REPO_ROOT / "packages" / "mcp-workstate-orchestrator" / "src"
HANDOFF_SRC = REPO_ROOT / "packages" / "mcp-workstate-handoff" / "src"
WORKSTATE_SYSTEM_SCRIPTS = REPO_ROOT / "packages" / "workstate-system" / "scripts"
EXPECTED_HANDOFF_PACKAGE_DIR = HANDOFF_SRC / "workstate_handoff_mcp"
EXPECTED_ORCHESTRATOR_PACKAGE_DIR = ORCHESTRATOR_SRC / "workstate_orchestrator_mcp"

# Prepend both src/ paths so this worktree's source wins over any
# environment-wide editable install pointing at a different checkout.
for _path in (HANDOFF_SRC, ORCHESTRATOR_SRC):
    _path_str = str(_path)
    if _path_str in sys.path:
        sys.path.remove(_path_str)
    sys.path.insert(0, _path_str)
if str(WORKSTATE_SYSTEM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(WORKSTATE_SYSTEM_SCRIPTS))

# Tell the workstate-handoff-mcp commit-SHA validator to accept synthetic
# test SHAs without resolving them through git.
os.environ.setdefault("WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION", "1")


def pytest_sessionstart(session) -> None:  # type: ignore[no-untyped-def]
    """Verify both packages resolve to *this* worktree's source.

    Delegates to the shared per-package path-guard helper
    (WORKSTATE-REF-41 implementation note). Fails the session on cross-worktree drift with
    the canonical ``uv sync --extra dev`` remediation message; honors
    ``AGENTIC_DISABLE_PYTEST_PATH_GUARD=1``.
    """
    import workstate_handoff_mcp  # noqa: PLC0415 - intentional late import for the guard.
    from pytest_path_guard import check_path_guard  # noqa: PLC0415

    import workstate_orchestrator_mcp  # noqa: PLC0415 - intentional late import for the guard.

    check_path_guard(REPO_ROOT)
    _ = (workstate_handoff_mcp, workstate_orchestrator_mcp)  # imported for guard side-effect.
