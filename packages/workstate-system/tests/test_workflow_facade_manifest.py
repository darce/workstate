"""Manifest-vs-recipe drift check for portable_commands.json (WORKSTATE-REF-46 implementation note).

The plan documents `make plan-analyze DOC=<path>` etc. across six surfaces,
but until implementation note the recipe forwarded only ``LIFECYCLE_ARGS`` — the DOC
value was silently dropped. These tests pin a new lint that asserts every
``<UPPERCASE>=`` token in the manifest's ``makefile_target`` either:

1. matches an ``argument_schema[].name`` (under the ``VAR.lower().replace('_','-')``
   translation), or
2. appears on a ``(command_id, VAR)`` allowlist for documented mismatches
   (e.g. ``TASK`` ↔ ``task-ref`` is intentional ergonomic divergence).

In addition, regardless of allowlist, the recipe body in ``Makefile.d/*.mk``
must reference ``$(VAR)`` so the Make variable is actually forwarded.

Three regression vectors are pinned:

* ``test_as_shipped_manifest_passes`` — locks the allowlist by data so a
  future commit cannot quietly extend the allowlist without updating tests.
* ``test_missing_recipe_var_fails`` — a recipe that omits ``$(DOC)`` for a
  manifest entry that documents ``DOC=<path>`` must trip the check.
* ``test_unknown_var_without_allowlist_fails`` — a manifest entry whose
  Make var is missing from ``argument_schema`` and not allowlisted must
  fail.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path



PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
FACADE_CHECK = PAYLOAD_ROOT / "scripts" / "check_workflow_facade.py"
MANIFEST_REL = Path("config") / "agent-workflows" / "portable_commands.json"
MAKEFILE_D_REL = Path("Makefile.d")
LIFECYCLE_MK_REL = Path("Makefile.d") / "lifecycle.mk"
WORKFLOW_DOC_REL = Path("docs") / "workstate" / "rules" / "development-workflow.md"

FACADE_CHECK_SPEC = importlib.util.spec_from_file_location(
    "workflow_facade_check_manifest", FACADE_CHECK
)
assert FACADE_CHECK_SPEC is not None and FACADE_CHECK_SPEC.loader is not None
workflow_facade_check = importlib.util.module_from_spec(FACADE_CHECK_SPEC)
FACADE_CHECK_SPEC.loader.exec_module(workflow_facade_check)


def _run_facade_check(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(FACADE_CHECK), "--root", str(root)],
        capture_output=True,
        text=True,
    )


def _materialize_fixture_root(tmp_path: Path) -> Path:
    """Copy the real manifest + lifecycle.mk into tmp_path so each test can
    mutate one in isolation. Skill bodies are not copied — the orientation
    lint is unrelated to the manifest drift check."""
    root = tmp_path / "workstate-system"
    (root / MANIFEST_REL.parent).mkdir(parents=True)
    (root / LIFECYCLE_MK_REL.parent).mkdir(parents=True)
    shutil.copy(PAYLOAD_ROOT / MANIFEST_REL, root / MANIFEST_REL)
    shutil.copy(PAYLOAD_ROOT / LIFECYCLE_MK_REL, root / LIFECYCLE_MK_REL)
    return root


def test_as_shipped_manifest_passes() -> None:
    """The repo as it ships must satisfy the new check. This pins the
    allowlist by data: a future PR cannot extend the allowlist without
    a corresponding test edit."""
    proc = _run_facade_check(PAYLOAD_ROOT)
    assert proc.returncode == 0, (
        f"as-shipped manifest failed the new manifest-vs-recipe check.\n"
        f"stdout: {proc.stdout!r}\nstderr: {proc.stderr!r}"
    )


def test_make_help_defers_step_order_to_workflow_doc() -> None:
    """The canonical workflow order lives in development-workflow.md.

    Make help descriptions may name commands, but must not duplicate
    `Workflow loop step N` labels that drift when the canonical loop changes.
    """
    workflow_doc = (PAYLOAD_ROOT / WORKFLOW_DOC_REL).read_text(encoding="utf-8")
    assert "## Canonical Workflow Loop" in workflow_doc

    offenders: list[str] = []
    for mk_path in sorted((PAYLOAD_ROOT / MAKEFILE_D_REL).glob("*.mk")):
        for line_no, line in enumerate(
            mk_path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if "## Workflow loop step" in line:
                rel_path = mk_path.relative_to(PAYLOAD_ROOT)
                offenders.append(f"{rel_path}:{line_no}: {line.strip()}")

    assert not offenders, (
        "Make help must not duplicate canonical workflow step numbers; "
        f"update {WORKFLOW_DOC_REL} for ordering and keep help descriptions "
        f"number-free. Offenders: {offenders!r}"
    )


def test_missing_recipe_var_fails(tmp_path: Path) -> None:
    """Stripping ``$(DOC)`` from the plan-analyze recipe must fail —
    that is exactly the silent-drop regression implementation note protects against."""
    root = _materialize_fixture_root(tmp_path)
    mk_path = root / LIFECYCLE_MK_REL
    text = mk_path.read_text(encoding="utf-8")
    needle = "plan-analyze $(if $(DOC),--doc '$(DOC)') $(LIFECYCLE_ARGS)"
    replacement = "plan-analyze $(LIFECYCLE_ARGS)"
    assert needle in text, (
        "fixture invariant: lifecycle.mk no longer contains the slice-1 "
        "DOC-forwarding token; update this test if the recipe shape changed."
    )
    mk_path.write_text(text.replace(needle, replacement), encoding="utf-8")

    proc = _run_facade_check(root)
    assert proc.returncode != 0, (
        f"check should have failed when plan-analyze recipe drops $(DOC). "
        f"stdout: {proc.stdout!r}; stderr: {proc.stderr!r}"
    )
    assert "plan-analyze" in proc.stderr
    assert "DOC" in proc.stderr


def test_unknown_var_without_allowlist_fails(tmp_path: Path) -> None:
    """A manifest entry that documents a Make var missing from
    ``argument_schema`` (under the translation rule) and not allowlisted
    must fail. Uses ``WIDGET=`` because no real schema entry exists."""
    root = _materialize_fixture_root(tmp_path)
    manifest_path = root / MANIFEST_REL
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    plan_analyze = next(
        c for c in manifest["commands"] if c["command_id"] == "plan-analyze"
    )
    plan_analyze["makefile_target"] = "make plan-analyze DOC=<path> WIDGET=<id>"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    proc = _run_facade_check(root)
    assert proc.returncode != 0, (
        f"check should have failed for unknown WIDGET= var. "
        f"stdout: {proc.stdout!r}; stderr: {proc.stderr!r}"
    )
    assert "WIDGET" in proc.stderr
    assert "plan-analyze" in proc.stderr


def test_allowlisted_task_var_is_accepted(tmp_path: Path) -> None:
    """``branch-lifecycle`` documents ``TASK=`` while its schema names the
    arg ``task-ref`` — that mismatch is intentional and allowlisted. The
    as-shipped manifest already exercises this path; this test makes the
    intent explicit so a future check refactor cannot regress it."""
    root = _materialize_fixture_root(tmp_path)
    proc = _run_facade_check(root)
    assert proc.returncode == 0, (
        f"copied-as-shipped fixture failed; the allowlist must accept "
        f"the (branch-lifecycle, TASK), (incremental-implementation, TASK), "
        f"and (tdd, TASK) pairs. stdout: {proc.stdout!r}; stderr: {proc.stderr!r}"
    )


def test_check_module_exposes_allowlist_constant() -> None:
    """The allowlist is part of the contract this test pins. Importing the
    check module must surface it as a top-level set so reviewers (and this
    test file) can audit it without parsing the function body.

    Asserts exact equality (not subset) so adding a new ``(command_id, var)``
    exemption fails the suite until this expected-set literal is updated in
    the same change. A subset check would silently accept new entries and
    weaken the manifest-vs-schema drift guard (WORKSTATE-REF-46-BR-01)."""
    allowlist = getattr(workflow_facade_check, "MANIFEST_ARG_NAME_ALLOWLIST", None)
    assert allowlist is not None, (
        "check_workflow_facade.py must export MANIFEST_ARG_NAME_ALLOWLIST as "
        "a module-level constant; reviewers need a single audit point for "
        "(command_id, var) exemptions."
    )
    expected = frozenset(
        {
            ("branch-lifecycle", "TASK"),
            ("incremental-implementation", "TASK"),
            ("tdd", "TASK"),
        }
    )
    assert frozenset(allowlist) == expected, (
        f"MANIFEST_ARG_NAME_ALLOWLIST drifted from the expected exemption set. "
        f"Adding or removing an exemption requires a deliberate review and a "
        f"matching update to this literal. expected={sorted(expected)!r}; "
        f"actual={sorted(allowlist)!r}."
    )
