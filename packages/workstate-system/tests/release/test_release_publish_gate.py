"""Regression tests locking the CI publish-gate accepted-state set (implementation note S4b).

The publish gate decides whether a package version may proceed to CI
Trusted-Publishing. It was inline heredoc Python in
``.github/workflows/release-publish.yml`` (un-testable, drift-prone). S4b
extracts it to ``scripts/release_publish_gate.py`` and locks the accepted set:
``pending_upload`` and ``remote_tag_without_pypi`` pass (version absent from
PyPI), ``released`` and every other state fail-close. This pins the 155989a
hot-fix that taught the gate to accept ``remote_tag_without_pypi`` (the state
the tag-first ``release-public`` flow lands a package in).
"""

from __future__ import annotations

import importlib.util as _importlib_util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
GATE_SOURCE = REPO_ROOT / "scripts" / "release_publish_gate.py"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-publish.yml"

_spec = _importlib_util.spec_from_file_location("release_publish_gate", GATE_SOURCE)
gate = _importlib_util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


def _plan(
    state: str, *, name: str = "workstate-protocol", version: str = "1.2.3"
) -> dict:
    return {"packages": [{"name": name, "state": state, "version": version}]}


def test_accepted_state_set_is_exactly_the_two_pre_pypi_states() -> None:
    # The lock: only these two states mean "version absent from PyPI, safe to
    # publish". Any drift in this set is a release-safety regression.
    assert gate.PUBLISHABLE_STATES == frozenset(
        {"pending_upload", "remote_tag_without_pypi"}
    )


@pytest.mark.parametrize("state", ["pending_upload", "remote_tag_without_pypi"])
def test_publishable_states_return_the_version(state: str) -> None:
    assert gate.select_version(_plan(state), "workstate-protocol") == "1.2.3"


@pytest.mark.parametrize(
    "state",
    [
        "released",
        "local_tag_only",
        "pypi_without_tag",
        "local_tag_with_pypi_missing_remote",
    ],
)
def test_non_publishable_states_fail_closed(state: str) -> None:
    # `released` (already on PyPI) and every anomalous state must abort.
    with pytest.raises(SystemExit):
        gate.select_version(_plan(state), "workstate-protocol")


def test_absent_package_fails_closed() -> None:
    with pytest.raises(SystemExit):
        gate.select_version({"packages": []}, "workstate-protocol")


def test_workflow_invokes_the_extracted_gate_not_an_inline_copy() -> None:
    # Lock that the workflow delegates to the unit-tested gate, so the accepted
    # set cannot silently drift back into an un-testable inline heredoc.
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "release_publish_gate.py" in text
    # The old inline definition must be gone (its drift was the whole problem).
    assert "PUBLISHABLE_STATES = {" not in text
