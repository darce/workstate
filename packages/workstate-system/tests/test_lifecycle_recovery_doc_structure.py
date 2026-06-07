"""WORKSTATE-REF-04 implementation note: structure lint for ``docs/workstate/rules/lifecycle-recovery.md``.

The recovery doc is the epic-level operator deliverable for the
workflow-block-friction epic. It must enumerate all six original scope
surfaces plus the WORKSTATE-REF-05 and WORKSTATE-REF-WSG recovery cases, and every section
must carry the same three sub-fields so an operator can act without
spelunking: the guard (with its file path), the invariant it protects,
and a one-line escape hatch.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_ROOT = PACKAGE_ROOT / "workstate_system" / "payload"
DOC_PATH = PAYLOAD_ROOT / "docs" / "workstate" / "rules" / "lifecycle-recovery.md"

# Each entry is the leading token of a ``## <token>...`` heading. The doc
# may add a human-readable title after the token (e.g. "Surface 1:
# Linked-worktree resolve-gate"); the lint pins the token, not the prose.
EXPECTED_SECTION_TOKENS = (
    "Surface 1",
    "Surface 2",
    "Surface 3",
    "Surface 4",
    "Surface 5",
    "Surface 6",
    "WORKSTATE-REF-05",
    "WORKSTATE-REF-WSG",
)

REQUIRED_SUBFIELDS = ("**Guard**", "**Invariant**", "**Escape hatch**")

# A backtick-wrapped path ending in .py — every guard surface names a
# concrete source/hook file so the operator can open it directly.
_PY_PATH_RE = re.compile(r"`[^`]+\.py`")


def _doc_text() -> str:
    return DOC_PATH.read_text(encoding="utf-8")


def _section_body(text: str, token: str) -> str | None:
    match = re.search(
        rf"(?ms)^##\s+{re.escape(token)}\b.*?$(.*?)(?=^##\s+|\Z)",
        text,
    )
    return match.group(1) if match else None


def test_doc_exists_with_h1_title() -> None:
    assert DOC_PATH.exists(), f"lifecycle-recovery.md must exist at {DOC_PATH}"
    text = _doc_text()
    assert re.search(r"^#\s+\S", text, flags=re.MULTILINE), "Doc must open with an H1 title."


@pytest.mark.parametrize("token", EXPECTED_SECTION_TOKENS)
def test_section_present(token: str) -> None:
    body = _section_body(_doc_text(), token)
    assert body is not None, (
        f"lifecycle-recovery.md must contain a '## {token} ...' section "
        "(one per scope surface plus WORKSTATE-REF-05 and WORKSTATE-REF-WSG)."
    )


@pytest.mark.parametrize("token", EXPECTED_SECTION_TOKENS)
def test_section_has_required_subfields(token: str) -> None:
    body = _section_body(_doc_text(), token)
    assert body is not None, f"Section '{token}' must exist (see prior test)."
    for field in REQUIRED_SUBFIELDS:
        assert field in body, f"Section '{token}' must document {field}."
    assert _PY_PATH_RE.search(body), (
        f"Section '{token}' must name its guard file path as a backticked `*.py` path."
    )


def test_all_six_surfaces_and_two_extra_cases_counted() -> None:
    text = _doc_text()
    surface_headings = re.findall(r"(?m)^##\s+Surface\s+\d+\b", text)
    assert len(surface_headings) == 6, (
        "The doc must enumerate exactly the six original scope surfaces; "
        f"found {len(surface_headings)}."
    )
    assert _section_body(text, "WORKSTATE-REF-05") is not None
    assert _section_body(text, "WORKSTATE-REF-WSG") is not None
