"""DOC= forwarding for ``plan-analyze`` and ``plan-review`` (WORKSTATE-REF-46 implementation note).

Six surfaces document ``make plan-analyze DOC=<path>`` /
``make plan-review DOC=<path>`` (root ``CLAUDE.md``, package ``CLAUDE.md``,
the four ``plan-analyze``/``planning-review`` SKILL.md files), but the
``Makefile.d/lifecycle.mk`` recipes forwarded only ``LIFECYCLE_ARGS`` —
``DOC=<path>`` was silently dropped. These tests pin both the
``make -n`` resolution (substring + flag check on the resolved command
line) and the end-to-end JSON receipt so a future regression that
reverts to the lossy form fails loudly instead of producing the same
``--doc is required`` argparse exit that motivated the plan.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
PACKAGE_ROOT = Path(__file__).resolve().parents[2]
LIFECYCLE_RUNNER = (PACKAGE_ROOT / "workstate_system" / "payload" / "scripts" / "workstate" / "lifecycle").resolve()


pytestmark = pytest.mark.skipif(
    shutil.which("make") is None, reason="make not installed"
)


@pytest.mark.parametrize("target", ("plan-analyze", "plan-review"))
def test_make_n_forwards_doc_flag_and_path(target: str) -> None:
    """``make -n <target> DOC=<path>`` resolves to a command line containing
    both ``--doc`` and the supplied path.

    Asserting both tokens (not just the substring ``foo.md``) keeps the
    test honest if a future regression forwards the path as a bare
    positional argument — that would still match a substring check yet
    fail at runtime because ``skill_broadcast.py`` requires ``--doc``.
    """
    proc = subprocess.run(
        ["make", "-n", target, "DOC=docs/plans/0099-fixture.md"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"`make -n {target} DOC=...` failed (exit {proc.returncode}). "
        f"stderr: {proc.stderr!r}"
    )
    assert "--doc" in proc.stdout, (
        f"`make -n {target}` did not forward DOC as --doc; "
        f"stdout: {proc.stdout!r}"
    )
    assert "docs/plans/0099-fixture.md" in proc.stdout, (
        f"`make -n {target}` did not forward the supplied path; "
        f"stdout: {proc.stdout!r}"
    )


@pytest.mark.parametrize("target", ("plan-analyze", "plan-review"))
def test_make_n_lifecycle_args_still_forwarded(target: str) -> None:
    """``LIFECYCLE_ARGS=...`` continues to land on the resolved command
    line. Regression guard: the implementation note edit must add ``--doc`` without
    dropping the existing escape hatch."""
    proc = subprocess.run(
        ["make", "-n", target, "LIFECYCLE_ARGS=--explain"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--explain" in proc.stdout, (
        f"`make -n {target} LIFECYCLE_ARGS=--explain` lost the escape "
        f"hatch flag; stdout: {proc.stdout!r}"
    )


@pytest.mark.parametrize("command", ("plan-analyze", "plan-review"))
def test_lifecycle_runner_emits_doc_in_receipt(
    command: str, tmp_path: Path
) -> None:
    """Invoking the lifecycle runner end-to-end emits a structured JSON
    receipt that names the supplied doc, not an argparse ``--doc is
    required`` exit.

    The receipt's ``doc`` field is the operator-visible proof that the
    flag was honored. Asserting on the parsed JSON (not just the exit
    code) catches a regression that forwarded the flag but lost the
    value mid-pipeline.
    """
    subprocess.run(
        ["git", "init", "--initial-branch=main", "-q"],
        cwd=tmp_path,
        check=True,
    )
    doc_path = tmp_path / "0099-fixture-plan.md"
    doc_path.write_text("# Fixture plan\n")

    proc = subprocess.run(
        [
            sys.executable,
            str(LIFECYCLE_RUNNER),
            command,
            "--doc",
            str(doc_path),
            "--json",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, (
        f"lifecycle {command} exited {proc.returncode}; "
        f"stderr: {proc.stderr!r}; stdout: {proc.stdout!r}"
    )
    payload = json.loads(proc.stdout)
    assert payload.get("ok") is True, payload
    assert payload.get("command") == command, payload
    assert payload.get("doc") == str(doc_path), payload
