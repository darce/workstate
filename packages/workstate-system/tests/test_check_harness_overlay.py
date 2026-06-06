"""WS-HARNESS-FAILSAFE-01 implementation note: check_harness_sync overlay-mode detection.

Pins that the harness-sync validator keys overlay mode off the shared
``detect_overlay_mode`` detector (not a raw ``.workstate-overlay.json``
filename probe), recognizes a canonical ``.workstate-bootstrap.json`` consumer,
and propagates a broken canonical shared surface as a fail-closed error rather
than a silent source-tree fallback.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

yaml = pytest.importorskip("yaml")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from scripts.check_harness_sync import _load_contract, _format_success_message  # noqa: WORKSTATE-REF-402
from scripts.overlay_resolver import BrokenOverlayError, OverlayResolverError  # noqa: WORKSTATE-REF-402

CONTRACT_REL = Path("docs/workstate/contracts/harness-protocol.yaml")


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


def _seed_source_contract(root: Path) -> None:
    path = root / CONTRACT_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("version: 1\n", encoding="utf-8")


def _seed_clone_contract(root: Path) -> Path:
    clone = root / ".workstate" / "remote" / "docs" / "workstate" / "contracts"
    clone.mkdir(parents=True, exist_ok=True)
    (clone / "harness-protocol.yaml").write_text("version: 1\n", encoding="utf-8")
    return clone


# --- source tree -----------------------------------------------------------
def test_source_tree_loads_contract_and_plain_success(tmp_path: Path) -> None:
    _seed_source_contract(tmp_path)
    assert isinstance(_load_contract(repo_root=tmp_path), dict)
    assert _format_success_message(repo_root=tmp_path) == "check-harness-sync: OK"


# --- canonical bootstrap ledger --------------------------------------------
def test_canonical_ledger_is_overlay_mode_with_counts(tmp_path: Path) -> None:
    clone = _seed_clone_contract(tmp_path)
    link = tmp_path / "docs" / "workstate" / "contracts"
    link.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(clone, link)
    (tmp_path / ".workstate-bootstrap.json").write_text(
        json.dumps(_bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}])),
        encoding="utf-8",
    )
    assert isinstance(_load_contract(repo_root=tmp_path), dict)
    msg = _format_success_message(repo_root=tmp_path)
    # Pin the concrete counts so a regression that drops the shared contract
    # entry (shared=0) is caught, not just the literal `shared=` substring.
    assert "contracts=1" in msg and "shared=1" in msg


def test_canonical_broken_shared_surface_fails_closed(tmp_path: Path) -> None:
    link = tmp_path / "docs" / "workstate" / "contracts"
    link.parent.mkdir(parents=True, exist_ok=True)
    # dangling symlink: clone target never created
    os.symlink(tmp_path / ".workstate" / "remote" / "docs" / "workstate" / "contracts", link)
    (tmp_path / ".workstate-bootstrap.json").write_text(
        json.dumps(_bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}])),
        encoding="utf-8",
    )
    with pytest.raises(BrokenOverlayError):
        _load_contract(repo_root=tmp_path)


def test_canonical_missing_contracts_entry_falls_back_to_source_tree(tmp_path: Path) -> None:
    _seed_source_contract(tmp_path)
    (tmp_path / ".workstate-bootstrap.json").write_text(
        json.dumps(_bootstrap_manifest([])),
        encoding="utf-8",
    )
    assert isinstance(_load_contract(repo_root=tmp_path), dict)


def test_canonical_missing_contracts_entry_without_source_tree_contract_raises(tmp_path: Path) -> None:
    (tmp_path / ".workstate-bootstrap.json").write_text(
        json.dumps(_bootstrap_manifest([])),
        encoding="utf-8",
    )
    with pytest.raises(OverlayResolverError, match="missing harness contract"):
        _load_contract(repo_root=tmp_path)


# --- legacy mapping manifest ------------------------------------------------
def test_legacy_overlay_mode_with_counts(tmp_path: Path) -> None:
    shared = tmp_path / "shared" / "contracts"
    shared.mkdir(parents=True, exist_ok=True)
    (shared / "harness-protocol.yaml").write_text("version: 1\n", encoding="utf-8")
    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "surfaces": {
                    "contracts": {"shared_root": "shared/contracts", "local_root": "local/contracts"}
                },
            }
        ),
        encoding="utf-8",
    )
    assert isinstance(_load_contract(repo_root=tmp_path), dict)
    msg = _format_success_message(repo_root=tmp_path)
    assert "contracts=" in msg
