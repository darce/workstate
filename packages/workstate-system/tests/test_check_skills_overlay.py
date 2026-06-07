"""WS-HARNESS-FAILSAFE-01 implementation note: check_skills overlay-mode detection.

Skills/commands are not canonical-ledger surfaces (they moved to the generated
plugin tree), so in canonical bootstrap mode ``check_skills`` must keep globbing
the direct ``skills_root`` and must never treat an absent skills ledger entry as
drift. Legacy mapping overlays keep delegating to ``resolve_surface``.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

pytest.importorskip("yaml")

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT))

from scripts.check_skills import _resolve_skill_dirs  # noqa: WORKSTATE-REF-402


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


def _seed_skill(skills_root: Path, slug: str) -> None:
    d = skills_root / slug
    d.mkdir(parents=True, exist_ok=True)
    (d / "skill.yaml").write_text("name: x\n", encoding="utf-8")
    (d / "body.md").write_text("# x\n", encoding="utf-8")


# --- source tree -----------------------------------------------------------
def test_source_tree_globs_skills_root(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _seed_skill(skills_root, "foo")
    dirs, failures = _resolve_skill_dirs(tmp_path, skills_root)
    assert failures == []
    assert {d.name for d in dirs} == {"foo"}


# --- canonical bootstrap ledger --------------------------------------------
def test_canonical_no_skills_entry_globs_skills_root(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    _seed_skill(skills_root, "foo")
    (tmp_path / ".workstate-bootstrap.json").write_text(
        json.dumps(_bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}])),
        encoding="utf-8",
    )
    dirs, failures = _resolve_skill_dirs(tmp_path, skills_root)
    assert failures == []
    assert {d.name for d in dirs} == {"foo"}


def test_canonical_with_stale_bootstrap_owned_legacy_still_globs(tmp_path: Path) -> None:
    # both manifests present, but the legacy file is a stale *bootstrap-owned*
    # ledger (surfaces is a list) -> canonical wins, must glob skills_root.
    skills_root = tmp_path / "skills"
    _seed_skill(skills_root, "foo")
    (tmp_path / ".workstate-bootstrap.json").write_text(
        json.dumps(_bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}])),
        encoding="utf-8",
    )
    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps({"schema_version": 1, "surfaces": [{"path": "docs/workstate/contracts", "source": "shared"}]}),
        encoding="utf-8",
    )
    dirs, failures = _resolve_skill_dirs(tmp_path, skills_root)
    assert failures == []
    assert {d.name for d in dirs} == {"foo"}


# --- ambiguous dual-manifest (fail closed) ----------------------------------
def test_ambiguous_user_owned_dual_manifest_fails_closed(tmp_path: Path) -> None:
    # canonical ledger + a USER-OWNED (mapping-shaped) legacy overlay is
    # ambiguous: _resolve_skill_dirs must surface a failure, not silently pick one.
    skills_root = tmp_path / "skills"
    _seed_skill(skills_root, "foo")
    (tmp_path / ".workstate-bootstrap.json").write_text(
        json.dumps(_bootstrap_manifest([{"path": "docs/workstate/contracts", "source": "shared"}])),
        encoding="utf-8",
    )
    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps({"schema_version": 1, "surfaces": {"skills": {"shared_root": "s", "local_root": "l"}}}),
        encoding="utf-8",
    )
    dirs, failures = _resolve_skill_dirs(tmp_path, skills_root)
    assert dirs == []
    assert failures and any("infrastructure error" in f for f in failures)


# --- legacy mapping manifest ------------------------------------------------
def test_legacy_overlay_delegates_to_resolve_surface(tmp_path: Path) -> None:
    shared_skills = tmp_path / "shared" / "skills"
    _seed_skill(shared_skills, "foo")
    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "surfaces": {"skills": {"shared_root": "shared/skills", "local_root": "local/skills"}},
            }
        ),
        encoding="utf-8",
    )
    dirs, failures = _resolve_skill_dirs(tmp_path, tmp_path / "skills")
    assert failures == []
    assert {d.name for d in dirs} == {"foo"}
