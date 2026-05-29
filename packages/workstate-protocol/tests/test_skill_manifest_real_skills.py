"""Loads real harness skills from workstate-system to confirm the
SkillManifest contract matches what is actually shipped.

After implementation note step 1, the canonical layout is
``packages/workstate-system/skills/<slug>/{skill.yaml, body.md}``.
The Claude-namespaced ``.claude/skills/<slug>/SKILL.md`` is a generated
artifact in target repos, not source. This test reads the neutral
canonical layout.

If the workstate-system source tree is not present (e.g. distributed
package install), the test no-ops via skip — it's a guard against
drift between protocol and harness, not a hard contract.
"""

from __future__ import annotations

import os
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

from workstate_protocol import SkillManifest, SkillScope


def _resolve_skills_root() -> pathlib.Path | None:
    """Locate ``packages/workstate-system/skills`` robustly.

    Resolution order:
    1. ``AGENTIC_SYSTEM_SKILLS_ROOT`` env var override.
    2. Walk up from this file looking for a sibling
       ``packages/workstate-system`` directory.
    3. Sibling-package fallback two levels up.
    """
    env_override = os.environ.get("AGENTIC_SYSTEM_SKILLS_ROOT")
    if env_override:
        candidate = pathlib.Path(env_override).expanduser()
        return candidate if candidate.is_dir() else None

    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "packages" / "workstate-system" / "skills"
        if candidate.is_dir():
            return candidate

    sibling = here.parents[2] / "workstate-system" / "skills"
    return sibling if sibling.is_dir() else None


SKILLS_ROOT = _resolve_skills_root()


def _load_skill_yaml(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text()) or {}


@pytest.mark.skipif(SKILLS_ROOT is None, reason="workstate-system skills source not available")
def test_at_least_one_real_harness_skill_validates() -> None:
    candidates = sorted(SKILLS_ROOT.glob("*/skill.yaml"))
    real_files = [p for p in candidates if p.is_file()]
    assert real_files, f"no skill.yaml files under {SKILLS_ROOT}"
    sampled = real_files[0]
    fm = _load_skill_yaml(sampled)
    manifest = SkillManifest.model_validate(fm)
    assert manifest.scope is SkillScope.harness, (
        f"{sampled.relative_to(SKILLS_ROOT)} declared scope={manifest.scope}; "
        "shipped workstate-system skills must declare scope: harness."
    )


@pytest.mark.skipif(SKILLS_ROOT is None, reason="workstate-system skills source not available")
def test_every_real_skill_declares_scope_harness() -> None:
    failures: list[str] = []
    slug_dirs = sorted(p for p in SKILLS_ROOT.iterdir() if p.is_dir())
    for slug_dir in slug_dirs:
        skill_yaml = slug_dir / "skill.yaml"
        body_md = slug_dir / "body.md"
        if not skill_yaml.is_file():
            failures.append(f"{slug_dir.name}: missing skill.yaml")
            continue
        if not body_md.is_file():
            failures.append(f"{slug_dir.name}: missing body.md")
            continue
        try:
            fm = _load_skill_yaml(skill_yaml)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{slug_dir.name}: yaml parse: {exc}")
            continue
        try:
            manifest = SkillManifest.model_validate(fm)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{slug_dir.name}: {exc}")
            continue
        if manifest.scope is not SkillScope.harness:
            failures.append(
                f"{slug_dir.name}: scope={manifest.scope} (expected harness)"
            )
    assert not failures, "\n".join(failures)


@pytest.mark.skipif(SKILLS_ROOT is None, reason="workstate-system skills source not available")
def test_canonical_layout_has_no_claude_namespace() -> None:
    """Source-of-truth lives at skills/, not .claude/skills/."""
    package_root = SKILLS_ROOT.parent
    legacy = package_root / ".claude" / "skills"
    assert not legacy.exists(), (
        f"legacy {legacy} still present; canonical source must live at "
        f"{SKILLS_ROOT.relative_to(package_root)} only. Re-run "
        "scripts/migrate_skills_to_neutral_layout.py and remove the legacy dir."
    )
