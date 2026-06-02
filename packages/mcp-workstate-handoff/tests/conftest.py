"""Pytest session bootstrap for ``workstate-handoff-mcp``.

Two responsibilities:

1. Put this package's ``src/`` on ``sys.path`` so direct ``pytest``
   invocations from the package directory work without an editable install.
2. Hard-fail the session if the resolved ``workstate_handoff_mcp`` import is
   not coming from this package's ``src/`` directory. That guard exists
   because Python's editable installs are environment-wide: a single
   ``pip install -e packages/mcp-workstate-handoff`` from the repo root
   makes every Python interpreter in the venv resolve
   ``import workstate_handoff_mcp`` to whichever path was last installed,
   regardless of which git worktree the test session is running from.
   Linked worktrees inherit that same install pointer, so a refactor
   that lives only in the linked worktree's ``src/`` will silently NOT
   be exercised by tests run inside that worktree -- pytest will run
   against the root worktree's source instead. The guard catches that
   class of false-positive verification at session start.
3. Mark the session so the workstate-handoff-mcp commit-SHA validator
   accepts synthetic test SHAs (`"abc123"`, `"def456"`, etc.) without
   trying to resolve them through git. Production callers always run
   without this env var set.
4. Keep branch enforcement opt-in so ambient shell env does not change
   unrelated test behavior. Enforcement tests delete the bypass and set
   `WORKSTATE_HANDOFF_ENFORCE_BRANCH=1` explicitly.
"""

from __future__ import annotations

import os
import sys
from functools import wraps
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PACKAGE_ROOT / "src"
EXPECTED_PACKAGE_DIR = SRC_ROOT / "workstate_handoff_mcp"
WORKTREE_ROOT = PACKAGE_ROOT.parents[1]
WORKSTATE_SYSTEM_SCRIPTS = WORKTREE_ROOT / "packages" / "workstate-system" / "scripts"

if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))
if str(WORKSTATE_SYSTEM_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(WORKSTATE_SYSTEM_SCRIPTS))


# ---------------------------------------------------------------------------
# Embedded-monorepo integration tests
# ---------------------------------------------------------------------------
#
# A subset of the test suite predates the workstate-protocol-monorepo
# extraction. Those tests assume the package is embedded inside a
# parent monorepo (originally example-repo) that ships
# the harness assets at predictable repo-root paths:
#
#   - .github/hooks/                (token_budget.py, terminal-guard.py)
#   - .vscode/mcp.json              (consumer-tool config)
#   - mk/handoff.mk                 (Make wrapper)
#   - config/agent-workflows/       (portable command catalog)
#   - scripts/generate_agent_workflows.py
#   - packages/mcp-workstate-handoff/   (the OLD pre-rename package dir)
#
# In workstate-protocol-monorepo the package lives under
# packages/mcp-workstate-handoff/ and the harness assets have moved to
# workstate-system. Those tests still walk up looking for the legacy
# layout and fail with FileNotFoundError. They are integration tests
# against a parent layout, not handoff-package unit tests.
#
# Resolution: detect the embedded-monorepo layout at session start.
# If present, run them. If absent, skip them with a clear marker so
# the package's own test suite stays green. A future slice can move
# these tests to packages/workstate-system or packages/workstate-bootstrap
# where they conceptually belong.

_EMBEDDED_LAYOUT_MARKERS = (
    Path(".github/hooks/token_budget.py"),
    Path("mk/handoff.mk"),
    Path(".vscode/mcp.json"),
    Path("config/agent-workflows/portable_commands.json"),
    Path("scripts/generate_agent_workflows.py"),
    # The gated tests resolve the launcher from the legacy
    # ``packages/mcp-workstate-handoff/`` path (pre-rename to
    # ``mcp-workstate-handoff``). A repo that has the other harness assets
    # but only the renamed package path would otherwise enable these
    # tests and re-introduce the FileNotFoundError on the launcher.
    Path("packages/mcp-workstate-handoff/src/workstate_handoff_mcp_launcher.py"),
)


def _embedded_layout_root() -> Path | None:
    """Return the parent dir that ships the embedded-monorepo harness, or None.

    Walks up from the package root looking for ``.github/hooks/token_budget.py``
    + the other markers above. Returns the first ancestor where ALL markers
    resolve. The workstate-protocol-monorepo root is intentionally not a match —
    its harness lives under ``packages/workstate-system`` rather than at the root.
    """
    for parent in [PACKAGE_ROOT, *PACKAGE_ROOT.parents]:
        if all((parent / marker).exists() for marker in _EMBEDDED_LAYOUT_MARKERS):
            return parent
    return None


_EMBEDDED_LAYOUT_AVAILABLE = _embedded_layout_root() is not None

# Test files that depend on the embedded-monorepo layout. When the layout
# is absent, every test in these files is skipped at collection time so
# the package's standalone suite stays green inside workstate-protocol-monorepo.
_EMBEDDED_LAYOUT_TEST_FILES = frozenset(
    {
        "test_token_budget.py",
        "test_stdio.py",
        "test_adapters.py",
        "test_lifecycle_scripts.py",
        # implementation note step 1.10: test_agent_workflow_generation.py,
        # test_auto_fix_scaffold.py, and test_review_parallel_scaffold.py
        # were moved to packages/workstate-system/tests/ — they exercise
        # workstate-system content (the workflow generator + skill bodies),
        # not handoff-side runtime, so they no longer belong here.
        "test_http.py",
    }
)

# Tell the commit-SHA validator to accept synthetic test SHAs without
# resolving them through git. This MUST be set before any
# ``workstate_handoff_mcp`` import that walks the validation path.
os.environ.setdefault("WORKSTATE_HANDOFF_SKIP_SHA_VALIDATION", "1")
os.environ.setdefault("WORKSTATE_HANDOFF_SKIP_BRANCH_ENFORCEMENT", "1")
# WORKSTATE-REF-52 implementation note: tests use synthetic target_branch values that have no
# matching worktree on disk; the production resolver derives via
# `_canonical_worktree_for_task` and raises WorktreeNotFoundError when
# nothing matches. The bypass keeps existing fixtures reading the stored
# column. Dedicated implementation note tests in `test_derive_worktree_path.py`
# exercise the production path directly with real `tmp_path` git repos
# and do not need the bypass.
os.environ.setdefault("WORKSTATE_HANDOFF_SKIP_WORKTREE_DERIVATION", "1")


def _wrap_typed_api_surface(api_module) -> None:  # type: ignore[no-untyped-def]
    """Coerce legacy dict kwargs inside tests only.

    Production code is expected to use the typed models directly. The test
    suite still contains many direct Python helper calls with plain dicts,
    so the compatibility shim lives here rather than in the production API.
    """

    actor_model = api_module.WriteActorInput
    details_model = api_module.ReviewFindingDetailsInput

    def _coerce_kwargs(kwargs: dict) -> dict:
        updated = dict(kwargs)
        actor = updated.get("actor")
        if isinstance(actor, dict):
            updated["actor"] = actor_model.model_validate(actor)
        details = updated.get("details")
        if isinstance(details, dict):
            updated["details"] = details_model.model_validate(details)
        return updated

    def _wrap(name: str) -> None:
        original = getattr(api_module, name)

        @wraps(original)
        def _wrapped(*args, **kwargs):  # type: ignore[no-untyped-def]
            return original(*args, **_coerce_kwargs(kwargs))

        setattr(api_module, name, _wrapped)

    for name in (
        "set_handoff_state",
        "update_task_status",
        "record_decision",
        "update_next_actions",
        "record_test_result",
        "report_blocker",
        "update_review_finding",
        "record_review_run",
        "close_slice",
    ):
        _wrap(name)


def pytest_collection_modifyitems(config, items) -> None:  # type: ignore[no-untyped-def]
    """Skip embedded-monorepo integration tests when the layout is absent.

    See the module-level rationale block. Skipping happens at collection
    time so the rest of the suite reports a clean tally; when the
    embedded layout IS present (e.g. running this package's tests from
    inside example-repo), every test runs unchanged.
    """
    if _EMBEDDED_LAYOUT_AVAILABLE:
        return
    import pytest

    skip_marker = pytest.mark.skip(
        reason=(
            "requires embedded-monorepo harness layout "
            "(.github/hooks/, mk/handoff.mk, .vscode/mcp.json, "
            "config/agent-workflows/, scripts/generate_agent_workflows.py, "
            "packages/mcp-workstate-handoff/src/workstate_handoff_mcp_launcher.py)"
        )
    )
    for item in items:
        if item.path.name in _EMBEDDED_LAYOUT_TEST_FILES:
            item.add_marker(skip_marker)


def pytest_sessionstart(session) -> None:  # type: ignore[no-untyped-def]
    """Verify ``workstate_handoff_mcp`` resolves to *this* worktree's source.

    Delegates to the shared per-package path-guard helper
    (WORKSTATE-REF-41 implementation note). Fails the session on cross-worktree drift with
    the canonical ``uv sync --extra dev`` remediation message; honors
    ``AGENTIC_DISABLE_PYTEST_PATH_GUARD=1`` for cross-worktree fixture
    work.
    """
    from pytest_path_guard import check_path_guard  # noqa: PLC0415

    import workstate_handoff_mcp  # noqa: PLC0415 - intentional late import for the guard.
    from workstate_handoff_mcp import api as handoff_api  # noqa: PLC0415

    check_path_guard(WORKTREE_ROOT)
    _wrap_typed_api_surface(handoff_api)
    _ = workstate_handoff_mcp  # imported for guard side-effect; silences linters.
