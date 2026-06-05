"""Pytest bootstrap for workstate-system tests.

Puts the package's scripts/ directories on sys.path so generator/check
modules can be imported by name. implementation note S3 co-located the shipped scripts
under ``workstate_system/payload/scripts`` while internal-only scripts
(``pytest_path_guard``, ``overlay_resolver``, …) stayed at ``scripts``; both
dirs go on the path, and ``workstate`` resolves as a namespace package spanning
``payload/scripts/workstate`` (lifecycle) and ``scripts/workstate`` (evals).
Also relaxes a pair of workstate-handoff
runtime guards so the auto-fix and review-parallel scaffold tests
(moved here in implementation note step 1.10) can drive the handoff API with
synthetic SHAs and off-feature branches without tripping production
validators — same posture as ``mcp-workstate-handoff/tests/conftest.py``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PACKAGE_ROOT / "scripts"
PAYLOAD_SCRIPTS_DIR = PACKAGE_ROOT / "workstate_system" / "payload" / "scripts"
WORKTREE_ROOT = PACKAGE_ROOT.parents[1]
HANDOFF_SRC = PACKAGE_ROOT.parent / "mcp-workstate-handoff" / "src"

# Internal scripts (pytest_path_guard, overlay_resolver, …) stayed at SCRIPTS_DIR;
# the shipped scripts (generator, facade/pin checks, workstate.lifecycle) moved
# to PAYLOAD_SCRIPTS_DIR. Both go on sys.path (payload ends up first).
for _scripts_dir in (SCRIPTS_DIR, PAYLOAD_SCRIPTS_DIR):
    if str(_scripts_dir) not in sys.path:
        sys.path.insert(0, str(_scripts_dir))

# WORKSTATE-REF-53-S2-BR-01: pin the in-tree mcp-workstate-handoff source ahead of any
# site-packages install so tests like
# ``test_tasks_live_status_filter_matches_shared_primitives`` resolve
# ``workstate_handoff_mcp.shared_primitives`` to the canonical sibling source
# rather than a stale installed package missing WORKSTATE-REF-41 implementation note symbols.
if HANDOFF_SRC.is_dir() and str(HANDOFF_SRC) not in sys.path:
    sys.path.insert(0, str(HANDOFF_SRC))

os.environ.setdefault("WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION", "1")
os.environ.setdefault("WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT", "1")


def pytest_sessionstart(session) -> None:  # type: ignore[no-untyped-def]
    """Per-package path-guard (WORKSTATE-REF-41 implementation note).

    Fails the session if any in-repo agentic package resolved outside
    the active worktree root. Honors
    ``WORKSTATE_DISABLE_PYTEST_PATH_GUARD=1`` for cross-worktree fixture
    work.
    """
    from pytest_path_guard import check_path_guard  # noqa: PLC0415

    check_path_guard(WORKTREE_ROOT)
