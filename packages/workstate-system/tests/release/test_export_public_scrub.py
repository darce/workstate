"""WORKSTATE-REF-EXPORT-SCRUB-01 — inline prefix scrub must not corrupt embedded identifiers.

`INLINE_INTERNAL_PREFIX_RE` is case-insensitive; without word boundaries the
`WORKSTATE-REF` alternative matched *inside* identifiers (`fnmatchcase`,
`BranchClassification`, `PatchChangeKind`), rewriting them to
`fnmatWORKSTATEase`-style garbage in every public export. The corruption
shipped in the workstate-protocol 0.2.4 and workstate-system 0.2.4 wheels and
broke payload hook imports for consumers. These tests pin both directions:
embedded identifiers survive untouched, standalone refs (any case) still scrub.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE = REPO_ROOT / "scripts" / "export_public.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("export_public_scrub_under_test", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Identifiers embedding scrub-prefix substrings case-insensitively
# (`WORKSTATE` in fnmatchcase, `WORKSTATE` in CamelCase) that must survive untouched.
EMBEDDED_IDENTIFIERS = [
    "fnmatch.fnmatchcase(normalized, pattern)",
    "class BranchClassification:",
    "PatchChangeKind",
    "ToolSearchCallResponseItem",
    "WebSearchContextSize",
]

# Standalone internal refs (any case, incl. digit/underscore neighbours) that
# must still be scrubbed — letter-adjacency is the only protected context.
STANDALONE_REFS = [
    "see WORKSTATE-REF-60 for details",
    "see WORKSTATE-60 for details",
    "WORKSTATE-12 follow-up",
    "WORKSTATE-REF rollout",
    "populated registry (WORKSTATE65-BR-02 invariant)",
    'decision_id == "scope_intake_WORKSTATE-34_trigger_choice"',
]


@pytest.mark.parametrize("text", EMBEDDED_IDENTIFIERS)
def test_inline_prefix_scrub_leaves_embedded_identifiers_alone(text: str) -> None:
    module = _load_module()
    assert module.INLINE_INTERNAL_PREFIX_RE.sub("WORKSTATE", text) == text


@pytest.mark.parametrize("text", STANDALONE_REFS)
def test_inline_prefix_scrub_still_hits_standalone_refs(text: str) -> None:
    module = _load_module()
    scrubbed = module.INLINE_INTERNAL_PREFIX_RE.sub("WORKSTATE", text)
    assert scrubbed != text
    assert "WORKSTATE" in scrubbed


def test_scrub_public_text_end_to_end(tmp_path: Path) -> None:
    module = _load_module()
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from fnmatch import fnmatchcase  # WORKSTATE-REF-60\n"
        "class BranchClassification:\n"
        "    pass\n"
    )
    module._scrub_public_text(tmp_path)
    out = sample.read_text()
    assert "fnmatchcase" in out
    assert "BranchClassification" in out
    assert "WORKSTATE-REF-60" not in out
    assert module._denylist(tmp_path) == []
