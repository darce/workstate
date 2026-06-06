"""Slug canonicalization contract (WORKSTATE-REF-52 implementation note).

Both feature-branch writers — the lifecycle ``task-start`` handler and
the ``workstate_protocol`` reference implementation — must resolve
``(task_ref, slug)`` to the *same* canonical ``feature/<task-ref>...``
string. Today the lifecycle resolver carries a hand-copied duplicate of
``format_suggested_branch_name`` and the WORKSTATE-REF-52 task plan calls out
that any drift breaks implementation note's derive-from-``target_branch`` path
(it would key off a known-stale slug).

The RED gate here is the *identity* assertion: the two public callables
must be the same function, not merely byte-for-byte twins. Equivalence
tests catch present-day drift; identity is what prevents future drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

# Hand-curated grid covering: empty/None task_ref short-circuits, mixed
# case, multi-segment task refs, slug present/absent, and slug case
# folding.
_BRANCH_INPUTS: list[tuple[str | None, str | None]] = [
    (None, None),
    ("", None),
    ("WORKSTATE-REF-52", None),
    ("WORKSTATE-52", None),
    ("WORKSTATE-REF-52", "Write-Context"),
    ("WORKSTATE-REF-52", "WRITE-CONTEXT"),
    ("WORKSTATE-REF-52", "write-context"),
    ("WORKSTATE-REF-44-2", "wrong-cwd"),
    ("WORKSTATE-REF-PLANNING-REVIEW-TASK-52", "doc-fix"),
    ("WORKSTATE-REF-1", None),
]


def _load_lifecycle_resolver():
    """Import the lifecycle ``resolver`` module without polluting sys.path globally.

    Lifecycle scripts live outside the package layout (they ship as a
    runnable directory under ``packages/workstate-system/workstate_system/payload/scripts/workstate/lifecycle``)
    so an ordinary ``import`` would not find them. We mimic the lifecycle
    runner's own ``__main__`` shim: prepend the lifecycle dir to ``sys.path``
    just long enough to import ``resolver`` by name.
    """
    repo_root = Path(__file__).resolve().parents[3]
    lifecycle_dir = (
        repo_root
        / "packages"
        / "workstate-system"
        / "workstate_system"
        / "payload"
        / "scripts"
        / "workstate"
        / "lifecycle"
    )
    if not lifecycle_dir.exists():
        pytest.skip(f"lifecycle dir not present: {lifecycle_dir}")
    spec = importlib.util.spec_from_file_location("WORKSTATE52_slice4_resolver", lifecycle_dir / "resolver.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_format_branch_name_delegates_to_format_suggested_branch_name() -> None:
    """The lifecycle resolver's ``format_branch_name`` must *be* the same
    callable as ``workstate_protocol.branch_naming.format_suggested_branch_name``
    (or at minimum delegate to it via ``__wrapped__`` / equivalent), so
    no future edit can drift one without the other.

    WORKSTATE-REF-52 implementation note RED gate: today the lifecycle resolver defines a
    standalone copy. After the slice lands, the two names must resolve
    to the same function object.
    """
    from workstate_protocol.branch_naming import format_suggested_branch_name

    resolver = _load_lifecycle_resolver()
    assert resolver.format_branch_name is format_suggested_branch_name, (
        "lifecycle.resolver.format_branch_name must delegate to "
        "workstate_protocol.branch_naming.format_suggested_branch_name; "
        "found two distinct callables — slug grammar will drift."
    )


@pytest.mark.parametrize("task_ref,slug", _BRANCH_INPUTS)
def test_format_branch_name_outputs_match_canonical(task_ref: str | None, slug: str | None) -> None:
    """Equivalence sweep: every ``(task_ref, slug)`` pair must produce
    the same string from both writers. The identity test above catches
    structural drift; this one catches behavioral drift even if a future
    refactor accidentally replaces the delegation with a re-implementation.
    """
    from workstate_protocol.branch_naming import format_suggested_branch_name

    resolver = _load_lifecycle_resolver()
    assert resolver.format_branch_name(task_ref, slug=slug) == (format_suggested_branch_name(task_ref, slug=slug))
