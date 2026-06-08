"""implementation note S3: the clone resolver must find surfaces at the co-located
``workstate_system/payload/`` layout that a fresh monorepo clone now has.

This is the runbook's "highest-risk gap": the monorepo's own make/hooks read the
package SOURCE directly, so an in-repo regression in the CLONE resolver
(``_resolve_in_clone``) is invisible to every other test — a downstream consumer
cloning the monorepo would silently materialize an EMPTY overlay. These tests
pin the payload-first probe + the legacy fallback directly.
"""

from __future__ import annotations

from pathlib import Path

from workstate_bootstrap.install import (
    SHARED_SURFACES,
    WORKSTATE_SYSTEM_PAYLOAD_SUBDIR,
    WORKSTATE_SYSTEM_SUBDIR,
    _resolve_in_clone,
)


def _seed(base: Path, surface: str) -> Path:
    target = base / surface
    target.mkdir(parents=True, exist_ok=True)
    (target / "marker.txt").write_text("x", encoding="utf-8")
    return target


def test_resolve_in_clone_finds_every_shared_surface_at_payload_layout(tmp_path: Path) -> None:
    """A clone at the post-S3 layout resolves every SHARED_SURFACE non-empty."""
    clone = tmp_path / "clone"
    payload = clone / WORKSTATE_SYSTEM_PAYLOAD_SUBDIR
    for surface in SHARED_SURFACES:
        _seed(payload, surface)

    for surface in SHARED_SURFACES:
        resolved = _resolve_in_clone(clone, surface)
        assert resolved == payload / surface, surface
        assert resolved.exists() and any(resolved.iterdir()), (
            f"surface {surface} resolved empty from a payload-layout clone"
        )


def test_resolve_in_clone_keeps_legacy_subdir_fallback(tmp_path: Path) -> None:
    """Already-installed/hoisted consumers at the pre-S3 layout still resolve."""
    clone = tmp_path / "clone"
    _seed(clone / WORKSTATE_SYSTEM_SUBDIR, "scripts/hooks")
    resolved = _resolve_in_clone(clone, "scripts/hooks")
    assert resolved == clone / WORKSTATE_SYSTEM_SUBDIR / "scripts" / "hooks"
    assert resolved.exists()


def test_resolve_in_clone_payload_wins_over_legacy(tmp_path: Path) -> None:
    """When both layouts exist, the co-located payload is canonical."""
    clone = tmp_path / "clone"
    _seed(clone / WORKSTATE_SYSTEM_PAYLOAD_SUBDIR, "skills")
    _seed(clone / WORKSTATE_SYSTEM_SUBDIR, "skills")
    assert _resolve_in_clone(clone, "skills") == clone / WORKSTATE_SYSTEM_PAYLOAD_SUBDIR / "skills"
