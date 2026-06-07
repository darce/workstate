"""Integrity guards for ``docs/workstate/rules/*.md`` payload docs.

WS-RULES-SHIP-01: the canonical payload linked rules docs it did not ship
(finding WS-RULES-DANGLING-01). ``planning-review-guide.md`` lived only in
the orchestrator ``_assets/rules/`` tree, hand-duplicated with no sync
mechanism. These tests pin the canonical-source contract:

- payload ``docs/workstate/rules/`` is the single canonical home for shared
  rule docs; the orchestrator ``_assets/rules/`` copies are derived and must
  stay byte-identical (drift guard, implementation note).
- every ``rules/<name>.md`` reference under ``payload/`` — including bare
  same-dir links between rules docs — resolves to a shipped payload file
  (link-resolution guard, implementation note).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
MONOREPO_ROOT = PACKAGE_ROOT.parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
PAYLOAD_RULES = PAYLOAD_ROOT / "docs" / "workstate" / "rules"
ASSETS_RULES = (
    MONOREPO_ROOT
    / "packages"
    / "mcp-workstate-orchestrator"
    / "src"
    / "workstate_orchestrator_mcp"
    / "_assets"
    / "rules"
)

# Docs that exist in both homes. Payload is canon; _assets is derived.
DUAL_HOME_DOCS = (
    "branch-review-guide.md",
    "branch-review-php.md",
    "branch-review-python.md",
    "branch-review-typescript.md",
    "contract-change-checklist.md",
    "planning-review-guide.md",
)


@pytest.mark.parametrize("doc_name", DUAL_HOME_DOCS)
def test_assets_rules_match_payload_canon(doc_name: str) -> None:
    """Drift guard: every dual-home rules doc is byte-identical to canon."""
    canon = PAYLOAD_RULES / doc_name
    derived = ASSETS_RULES / doc_name

    assert canon.is_file(), (
        f"WS-RULES-DANGLING-01: payload rules dir must ship {doc_name} as the "
        f"canonical copy (expected at {canon})."
    )
    assert derived.is_file(), (
        f"Orchestrator _assets/rules must carry the derived {doc_name} copy "
        f"(expected at {derived})."
    )
    assert canon.read_bytes() == derived.read_bytes(), (
        f"{doc_name} drifted between payload canon and the derived "
        f"_assets/rules copy. Payload is the single canonical home; sync the "
        f"_assets copy from {canon}."
    )


# Path-form reference to a rules doc anywhere under payload, e.g.
# ``../../../docs/workstate/rules/planning-review-guide.md`` or the bare
# ``docs/workstate/rules/mcp-loading-protocol.md`` comment form in yaml maps.
_RULES_PATH_REF = re.compile(r"rules/([A-Za-z0-9._-]+\.md)")
# Bare same-dir markdown link inside a rules doc, e.g. ``](planning-pipeline.md)``
# or ``](branch-review-guide.md#anchor)`` — no slash in the target.
_SAME_DIR_LINK = re.compile(r"\]\(([A-Za-z0-9._-]+\.md)(?:#([^)]*))?\)")

# WS-RULES-SHIP-01-REV-A-01: payload scripts/hooks/Makefile fragments carry
# rules refs too — scan them so a future dangling ref there cannot evade the
# payload-wide guard.
_SCAN_SUFFIXES = {".md", ".yaml", ".yml", ".py", ".mk"}


def _payload_text_files() -> list[Path]:
    return sorted(
        path
        for path in PAYLOAD_ROOT.rglob("*")
        if path.is_file() and path.suffix in _SCAN_SUFFIXES
    )


def test_payload_rules_refs_resolve() -> None:
    """Link-resolution guard: no dangling ``rules/*.md`` reference in payload.

    WS-RULES-DANGLING-01 root cause: skills/docs/prompts linked rules docs
    the payload never shipped (``planning-pipeline.md``,
    ``testing-principles.md``, ``mcp-loading-protocol.md``). Scans every
    payload markdown/yaml surface for path-form ``rules/<name>.md``
    references and asserts each names a shipped payload rules doc.
    """
    shipped = {path.name for path in PAYLOAD_RULES.glob("*.md")}
    dangling: list[str] = []
    for path in _payload_text_files():
        text = path.read_text(encoding="utf-8")
        for name in _RULES_PATH_REF.findall(text):
            if name not in shipped:
                rel = path.relative_to(PAYLOAD_ROOT)
                dangling.append(f"{rel}: rules/{name}")
    assert not dangling, (
        "Dangling rules/*.md references in payload (doc not shipped in "
        "docs/workstate/rules/):\n  " + "\n  ".join(sorted(set(dangling)))
    )


def test_payload_rules_same_dir_links_resolve() -> None:
    """Bare same-dir links between rules docs must also resolve.

    WS-RULES-SHIP-01-PR2-01: the path-form scan misses links like
    ``](planning-pipeline.md)`` written from inside ``rules/`` — the exact
    dangling-ref class the planning-review guide itself carried.
    """
    shipped = {path.name for path in PAYLOAD_RULES.glob("*.md")}
    dangling: list[str] = []
    for path in sorted(PAYLOAD_RULES.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        for name, anchor in _SAME_DIR_LINK.findall(text):
            if name not in shipped:
                dangling.append(f"{path.name}: ]({name})")
            elif anchor and anchor not in _heading_slugs(PAYLOAD_RULES / name):
                # WS-RULES-SHIP-01-REV-A-02: a resolvable file with a dead
                # fragment is still a dangling reference for the reader.
                dangling.append(f"{path.name}: ]({name}#{anchor})")
    assert not dangling, (
        "Dangling same-dir links inside payload rules docs:\n  "
        + "\n  ".join(sorted(set(dangling)))
    )


_HEADING = re.compile(r"^#{1,6}\s+(.+?)\s*$", re.MULTILINE)


def _heading_slugs(doc: Path) -> set[str]:
    """GitHub-style anchor slugs for every heading in ``doc``."""
    slugs: set[str] = set()
    for heading in _HEADING.findall(doc.read_text(encoding="utf-8")):
        slug = re.sub(r"[^\w\s-]", "", heading).strip().lower()
        slugs.add(re.sub(r"\s+", "-", slug))
    return slugs
