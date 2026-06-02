"""WS-HARNESS-FAILSAFE-01 implementation note: canonical bootstrap ledger detection.

These fixtures pin the four overlay modes the resolver must distinguish
(source-tree, canonical bootstrap, legacy mapping, ambiguous dual-manifest)
plus the fail-closed behavior for a malformed ledger or a broken
``source="shared"`` surface. The canonical ledger is path-keyed and
source-tagged (``surfaces`` is a *list* of ``{path, source}``), unlike the
legacy ``.workstate-overlay.json`` mapping (``surfaces.<kind>.shared_root``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from scripts.overlay_resolver import (  # noqa: WORKSTATE-REF-402
    BrokenOverlayError,
    OverlayResolverError,
    detect_overlay_mode,
    resolve_surface,
)


# --------------------------------------------------------------------------
# fixtures helpers
# --------------------------------------------------------------------------
def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _seed_contract_dir(root: Path) -> Path:
    contracts = root / "docs" / "workstate" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "harness-protocol.yaml").write_text("version: 1\n", encoding="utf-8")
    return contracts


def _seed_clone_contracts(root: Path) -> Path:
    """Create a real shared-contracts dir inside the bootstrap clone."""
    clone_contracts = root / ".workstate" / "remote" / "docs" / "workstate" / "contracts"
    clone_contracts.mkdir(parents=True, exist_ok=True)
    (clone_contracts / "harness-protocol.yaml").write_text("version: 1\n", encoding="utf-8")
    return clone_contracts


def _bootstrap_manifest(surfaces: list[dict[str, str]]) -> dict[str, object]:
    return {
        "schema_version": 2,
        "remote_url": "https://example.invalid/workstate.git",
        "remote_ref": "main",
        "remote_sha": "0123456789abcdef0123456789abcdef01234567",
        "profile": "all",
        "surfaces": surfaces,
        "configs": [],
        "mcp_servers": [],
    }


# --------------------------------------------------------------------------
# source-tree mode (no manifest)
# --------------------------------------------------------------------------
def test_no_manifest_is_source_tree_mode(tmp_path: Path) -> None:
    _seed_contract_dir(tmp_path)
    assert detect_overlay_mode(tmp_path) == "source_tree"


def test_no_manifest_resolves_default_contracts(tmp_path: Path) -> None:
    _seed_contract_dir(tmp_path)
    resolved = resolve_surface("contracts", tmp_path)
    names = {p.effective_path.name for p in resolved}
    assert names == {"harness-protocol.yaml"}
    assert all(p.source == "shared" for p in resolved)


# --------------------------------------------------------------------------
# canonical bootstrap mode
# --------------------------------------------------------------------------
def test_canonical_ledger_is_detected(tmp_path: Path) -> None:
    _seed_clone_contracts(tmp_path)
    link = tmp_path / "docs" / "workstate" / "contracts"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / ".workstate" / "remote" / "docs" / "workstate" / "contracts", link)
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    assert detect_overlay_mode(tmp_path) == "canonical"


def test_canonical_clean_shared_contracts_resolves(tmp_path: Path) -> None:
    _seed_clone_contracts(tmp_path)
    link = tmp_path / "docs" / "workstate" / "contracts"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / ".workstate" / "remote" / "docs" / "workstate" / "contracts", link)
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    resolved = resolve_surface("contracts", tmp_path)
    names = {p.effective_path.name for p in resolved}
    assert "harness-protocol.yaml" in names
    assert all(p.source == "shared" for p in resolved)


def test_canonical_generated_prompts_real_dir_is_not_broken(tmp_path: Path) -> None:
    prompts = tmp_path / ".github" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "example.prompt.md").write_text("hi\n", encoding="utf-8")
    _seed_clone_contracts(tmp_path)
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": ".github/prompts", "source": "generated"}]),
    )
    # generated surfaces are real dirs, not clone symlinks: no BrokenOverlayError
    resolved = resolve_surface("prompts", tmp_path)
    names = {p.effective_path.name for p in resolved}
    assert "example.prompt.md" in names
    assert all(p.source == "local" for p in resolved)


def test_canonical_lifecycle_prompts_report_local(tmp_path: Path) -> None:
    prompts = tmp_path / ".github" / "prompts"
    prompts.mkdir(parents=True, exist_ok=True)
    (prompts / "lifecycle.prompt.md").write_text("hi\n", encoding="utf-8")
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": ".github/prompts", "source": "lifecycle"}]),
    )
    resolved = resolve_surface("prompts", tmp_path)
    assert {p.effective_path.name for p in resolved} == {"lifecycle.prompt.md"}
    assert all(p.source == "local" for p in resolved)


def test_canonical_absent_skills_entry_is_not_drift(tmp_path: Path) -> None:
    # skills/commands are not ledger surfaces (moved to the generated plugin
    # tree); an absent skills entry must fall through, never fail closed.
    _seed_clone_contracts(tmp_path)
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    # no .claude/skills dir at all → empty result, no exception
    assert resolve_surface("skills", tmp_path) == []


def test_canonical_empty_surfaces_falls_back_to_source_tree_contracts(tmp_path: Path) -> None:
    _seed_contract_dir(tmp_path)
    _write_json(tmp_path / ".workstate-bootstrap.json", _bootstrap_manifest([]))
    resolved = resolve_surface("contracts", tmp_path)
    assert {p.effective_path.name for p in resolved} == {"harness-protocol.yaml"}
    assert all(p.source == "shared" for p in resolved)


# --------------------------------------------------------------------------
# fail-closed: malformed ledger
# --------------------------------------------------------------------------
def test_malformed_canonical_json_raises(tmp_path: Path) -> None:
    (tmp_path / ".workstate-bootstrap.json").write_text("{not valid json", encoding="utf-8")
    with pytest.raises(OverlayResolverError):
        resolve_surface("contracts", tmp_path)


def test_canonical_surfaces_not_list_raises(tmp_path: Path) -> None:
    payload = _bootstrap_manifest([])
    payload["surfaces"] = {"contracts": {"shared_root": "x"}}  # wrong shape
    _write_json(tmp_path / ".workstate-bootstrap.json", payload)
    with pytest.raises(OverlayResolverError):
        resolve_surface("contracts", tmp_path)


# --------------------------------------------------------------------------
# fail-closed: broken shared surface
# --------------------------------------------------------------------------
def test_broken_canonical_shared_symlink_raises(tmp_path: Path) -> None:
    link = tmp_path / "docs" / "workstate" / "contracts"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / ".workstate" / "remote" / "docs" / "workstate" / "contracts", link)
    # note: clone target intentionally absent → dangling symlink
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    with pytest.raises(BrokenOverlayError):
        resolve_surface("contracts", tmp_path)


def test_shared_surface_replaced_by_real_dir_raises(tmp_path: Path) -> None:
    # a shared surface that is no longer a bootstrap-managed symlink is drift
    _seed_contract_dir(tmp_path)  # real dir, not a symlink
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    with pytest.raises(BrokenOverlayError):
        resolve_surface("contracts", tmp_path)


# --------------------------------------------------------------------------
# legacy mapping manifest
# --------------------------------------------------------------------------
def test_legacy_mapping_manifest_detected_and_resolves(tmp_path: Path) -> None:
    shared = tmp_path / "shared" / "contracts"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "harness-protocol.yaml").write_text("version: 1\n", encoding="utf-8")
    local = tmp_path / "local" / "contracts"
    local.mkdir(parents=True, exist_ok=True)
    (local / "extra.yaml").write_text("version: 1\n", encoding="utf-8")
    _write_json(
        tmp_path / ".workstate-overlay.json",
        {
            "schema_version": 1,
            "surfaces": {
                "contracts": {"shared_root": "shared/contracts", "local_root": "local/contracts"}
            },
        },
    )
    assert detect_overlay_mode(tmp_path) == "legacy"
    resolved = resolve_surface("contracts", tmp_path)
    names = {p.effective_path.name for p in resolved}
    assert {"harness-protocol.yaml", "extra.yaml"} <= names
    assert any(p.source == "local" for p in resolved)


# --------------------------------------------------------------------------
# ambiguous dual-manifest
# --------------------------------------------------------------------------
def test_ambiguous_dual_manifest_user_owned_raises(tmp_path: Path) -> None:
    # user-owned legacy overlay = mapping-shaped surfaces -> ambiguous, must refuse
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    _write_json(
        tmp_path / ".workstate-overlay.json",
        {"schema_version": 1, "surfaces": {"contracts": {"shared_root": "s", "local_root": "l"}}},
    )
    with pytest.raises(OverlayResolverError):
        detect_overlay_mode(tmp_path)


def test_ambiguous_dual_manifest_bootstrap_owned_prefers_canonical(tmp_path: Path) -> None:
    # a stale bootstrap-owned legacy file (surfaces is a *list*) is migratable,
    # not user-owned: canonical wins instead of raising.
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    _write_json(
        tmp_path / ".workstate-overlay.json",
        {"schema_version": 1, "surfaces": [{"path": "docs/workstate/contracts", "source": "shared"}]},
    )
    assert detect_overlay_mode(tmp_path) == "canonical"


# --------------------------------------------------------------------------
# fail-closed: malformed ledger (additional guards) + source-aware handling
# --------------------------------------------------------------------------
def test_canonical_missing_remote_sha_raises(tmp_path: Path) -> None:
    payload = _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}])
    del payload["remote_sha"]
    _write_json(tmp_path / ".workstate-bootstrap.json", payload)
    with pytest.raises(OverlayResolverError):
        resolve_surface("contracts", tmp_path)


def test_canonical_local_source_contracts_reports_local(tmp_path: Path) -> None:
    # a `source="local"` canonical entry is a real path (no clone symlink) and
    # must resolve as source="local", not fall back or fail closed.
    local_contracts = tmp_path / "docs" / "workstate" / "contracts"
    local_contracts.mkdir(parents=True, exist_ok=True)
    (local_contracts / "harness-protocol.yaml").write_text("version: 1\n", encoding="utf-8")
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "local"}]),
    )
    resolved = resolve_surface("contracts", tmp_path)
    assert {p.effective_path.name for p in resolved} == {"harness-protocol.yaml"}
    assert all(p.source == "local" for p in resolved)


def test_canonical_shared_surface_outside_clone_raises(tmp_path: Path) -> None:
    # a shared surface symlink that resolves OUTSIDE .workstate/remote is drift
    # (matches doctor's in_clone check), even though it exists.
    foreign = tmp_path / "elsewhere" / "contracts"
    foreign.mkdir(parents=True, exist_ok=True)
    (foreign / "harness-protocol.yaml").write_text("version: 1\n", encoding="utf-8")
    link = tmp_path / "docs" / "workstate" / "contracts"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(foreign, link)
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}]),
    )
    with pytest.raises(BrokenOverlayError):
        resolve_surface("contracts", tmp_path)


# --------------------------------------------------------------------------
# canonical hooks: two ledger paths (.github/hooks + scripts/hooks)
# --------------------------------------------------------------------------
def _seed_clone_hook(tmp_path: Path, rel: str, name: str) -> None:
    clone_dir = tmp_path / ".workstate" / "remote" / rel
    clone_dir.mkdir(parents=True, exist_ok=True)
    (clone_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")


def _symlink_into_clone(tmp_path: Path, rel: str) -> None:
    link = tmp_path / rel
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(tmp_path / ".workstate" / "remote" / rel, link)


def test_canonical_hooks_scripts_only_shared_resolves(tmp_path: Path) -> None:
    # Ledger records ONLY scripts/hooks (no .github/hooks). The resolver must
    # still return the scripts/hooks files (regression: the old anchor logic
    # returned 0 entries for a scripts/hooks-only ledger).
    _seed_clone_hook(tmp_path, "scripts/hooks", "bar.py")
    _symlink_into_clone(tmp_path, "scripts/hooks")
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest([{"path": "scripts/hooks", "source": "shared"}]),
    )
    resolved = resolve_surface("hooks", tmp_path)
    assert {p.effective_path.name for p in resolved} == {"bar.py"}
    assert all(p.source == "shared" for p in resolved)


def test_canonical_hooks_mixed_sources_tags_each_path(tmp_path: Path) -> None:
    # .github/hooks shared (clone symlink) + scripts/hooks local (real dir):
    # each ledger path's own source must be applied to its own files.
    _seed_clone_hook(tmp_path, ".github/hooks", "gh.sh")
    _symlink_into_clone(tmp_path, ".github/hooks")
    local_hooks = tmp_path / "scripts" / "hooks"
    local_hooks.mkdir(parents=True, exist_ok=True)
    (local_hooks / "local.py").write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    _write_json(
        tmp_path / ".workstate-bootstrap.json",
        _bootstrap_manifest(
            [
                {"path": ".github/hooks", "source": "shared"},
                {"path": "scripts/hooks", "source": "local"},
            ]
        ),
    )
    resolved = resolve_surface("hooks", tmp_path)
    by_name = {p.effective_path.name: p.source for p in resolved}
    assert by_name == {"gh.sh": "shared", "local.py": "local"}
