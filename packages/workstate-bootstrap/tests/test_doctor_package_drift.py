"""implementation note implementation note: doctor flags package/stack version drift offline.

The 2026-06-05 incident's discovery gap: an overlay materialized from
workstate-system 0.1.23 while PyPI shipped 0.2.1 produced a clean doctor —
doctor compared surface content only, never installed-wheel vs manifest
versions. New offline findings (package source only):

* ``package_drift`` — installed ``workstate-system`` wheel version differs
  from (or is missing vs) ``manifest.package_version``. Actionable, exit 1,
  hint names ``make workstate-update``.
* ``stack_drift`` — installed anchor (``workstate-stack``) or any
  ``stack_members`` distribution differs from / is missing vs the recorded
  exact versions. Actionable, exit 1, same hint.
* ``stack_provenance_missing`` — legacy package manifest without stack
  fields: informational only (``severity=info``), resolved by the next
  package update backfilling them.
* ``doctor --check-pypi`` — optional network note when PyPI has a newer
  ``workstate-stack``; informational only, never affects the exit code.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest

from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.subcommands import doctor


subcommands_mod = importlib.import_module("workstate_bootstrap.subcommands")


def _seed_package_manifest(
    target: Path,
    *,
    package_version: str = "0.2.1",
    stack: dict[str, object] | None = None,
) -> None:
    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "package",
        "package_version": package_version,
        "surfaces": [],
        "configs": [],
        "mcp_servers": [],
    }
    if stack is not None:
        payload.update(stack)
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(json.dumps(payload, indent=2) + "\n")


STACK_FIELDS = {
    "stack_distribution": "workstate-stack",
    "stack_version": "0.1.0",
    "stack_members": {
        "workstate-protocol": "0.2.1",
        "workstate-system": "0.2.1",
    },
}


def _fake_versions(monkeypatch: pytest.MonkeyPatch, versions: dict[str, str]) -> None:
    """Doctor resolves installed versions through this module-level seam."""

    def fake_version(distribution: str) -> str:
        if distribution in versions:
            return versions[distribution]
        raise subcommands_mod.importlib_metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(subcommands_mod.importlib_metadata, "version", fake_version)


def _kinds(findings: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for finding in findings:
        grouped.setdefault(finding["kind"], []).append(finding)
    return grouped


def test_clean_when_installed_matches_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_package_manifest(tmp_path, stack=STACK_FIELDS)
    _fake_versions(
        monkeypatch,
        {
            "workstate-system": "0.2.1",
            "workstate-stack": "0.1.0",
            "workstate-protocol": "0.2.1",
        },
    )

    kinds = _kinds(doctor(target=tmp_path))

    assert "package_drift" not in kinds
    assert "stack_drift" not in kinds
    assert "stack_provenance_missing" not in kinds


def test_package_drift_when_wheel_is_newer_than_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The incident shape: wheel upgraded, overlay never re-materialized."""
    _seed_package_manifest(tmp_path, package_version="0.1.23", stack=STACK_FIELDS)
    _fake_versions(
        monkeypatch,
        {
            "workstate-system": "0.2.1",
            "workstate-stack": "0.1.0",
            "workstate-protocol": "0.2.1",
        },
    )

    kinds = _kinds(doctor(target=tmp_path))

    assert "package_drift" in kinds
    finding = kinds["package_drift"][0]
    assert finding.get("severity") != "info"
    blob = json.dumps(finding)
    assert "0.1.23" in blob and "0.2.1" in blob
    assert "workstate-update" in blob  # remediation is named


def test_package_drift_when_wheel_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_package_manifest(tmp_path, stack=STACK_FIELDS)
    _fake_versions(monkeypatch, {"workstate-stack": "0.1.0"})

    kinds = _kinds(doctor(target=tmp_path))

    assert "package_drift" in kinds
    assert "not installed" in json.dumps(kinds["package_drift"][0])


def test_stack_drift_on_member_version_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_package_manifest(tmp_path, stack=STACK_FIELDS)
    _fake_versions(
        monkeypatch,
        {
            "workstate-system": "0.2.1",
            "workstate-stack": "0.1.0",
            "workstate-protocol": "0.1.9",  # behind the recorded 0.2.1
        },
    )

    kinds = _kinds(doctor(target=tmp_path))

    assert "stack_drift" in kinds
    drifted = {f["path"] for f in kinds["stack_drift"]}
    assert "workstate-protocol" in drifted


def test_stack_drift_on_missing_member_and_anchor_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_package_manifest(tmp_path, stack=STACK_FIELDS)
    _fake_versions(
        monkeypatch,
        {
            "workstate-system": "0.2.1",
            "workstate-stack": "0.2.0",  # anchor moved past the manifest
            # workstate-protocol not installed at all
        },
    )

    kinds = _kinds(doctor(target=tmp_path))

    drifted = {f["path"] for f in kinds.get("stack_drift", [])}
    assert "workstate-stack" in drifted
    assert "workstate-protocol" in drifted


def test_legacy_manifest_gets_informational_provenance_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_package_manifest(tmp_path)  # no stack fields
    _fake_versions(monkeypatch, {"workstate-system": "0.2.1"})

    findings = doctor(target=tmp_path)
    kinds = _kinds(findings)

    assert "stack_provenance_missing" in kinds
    assert kinds["stack_provenance_missing"][0].get("severity") == "info"
    assert "stack_drift" not in kinds


def test_git_overlay_manifest_skips_package_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / BOOTSTRAP_MANIFEST_NAME).write_text(
        json.dumps(
            {
                "schema_version": SCHEMA_VERSION,
                "remote_url": "file:///tmp/fake.git",
                "remote_ref": "v1",
                "remote_sha": "0" * 40,
                "surfaces": [],
                "configs": [],
                "mcp_servers": [],
            }
        )
    )
    _fake_versions(monkeypatch, {})

    kinds = _kinds(doctor(target=tmp_path))

    assert "package_drift" not in kinds
    assert "stack_drift" not in kinds
    assert "stack_provenance_missing" not in kinds


def test_check_pypi_note_is_informational_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_package_manifest(tmp_path, stack=STACK_FIELDS)
    _fake_versions(
        monkeypatch,
        {
            "workstate-system": "0.2.1",
            "workstate-stack": "0.1.0",
            "workstate-protocol": "0.2.1",
        },
    )
    monkeypatch.setattr(
        subcommands_mod, "_latest_pypi_version", lambda distribution: "0.9.9"
    )

    findings = doctor(target=tmp_path, check_pypi=True)
    kinds = _kinds(findings)

    assert "stack_update_available" in kinds
    note = kinds["stack_update_available"][0]
    assert note.get("severity") == "info"
    assert "0.9.9" in json.dumps(note)

    # Off by default: no note without the flag.
    assert "stack_update_available" not in _kinds(doctor(target=tmp_path))


def test_check_pypi_failure_is_silent_note_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_package_manifest(tmp_path, stack=STACK_FIELDS)
    _fake_versions(
        monkeypatch,
        {
            "workstate-system": "0.2.1",
            "workstate-stack": "0.1.0",
            "workstate-protocol": "0.2.1",
        },
    )
    monkeypatch.setattr(
        subcommands_mod, "_latest_pypi_version", lambda distribution: None
    )

    kinds = _kinds(doctor(target=tmp_path, check_pypi=True))
    assert "stack_update_available" not in kinds


def test_cli_doctor_exposes_check_pypi_flag() -> None:
    from workstate_bootstrap.cli import _build_parser

    args = _build_parser().parse_args(["doctor", "--target", ".", "--check-pypi"])
    assert args.check_pypi is True
    args = _build_parser().parse_args(["doctor", "--target", "."])
    assert args.check_pypi is False
