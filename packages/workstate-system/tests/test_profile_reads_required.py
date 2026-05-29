"""WORKSTATE-REF-71: deterministic guard against unshaped handoff reads.

Skill bodies and hook scripts are the routine sources of
``get_handoff_state(...)`` and ``load_session(...)`` invocations in this
repository. Both surfaces grew an WORKSTATE-REF-71 contract: every call must
either name a ``read_profile=`` (Layer 1) or pin an explicit
``sections=`` argument so the response stays bounded by construction
rather than by reviewer vigilance.

This module is the durable replacement for the one-time grep used during
implementation note adoption. It scans:

- ``packages/workstate-system/skills/**/body.md`` — natural-language
  descriptions of how skills call MCP tools.
- ``packages/workstate-system/scripts/hooks/**/*.py`` — production hook
  scripts that issue real MCP calls.

A call is "shaped" when it carries any of:

* ``read_profile=``
* ``sections=``
* an exact ``profile`` keyword such as ``read_profile="identity"`` (the
  regex tolerates whitespace and quote style).

The lint intentionally allows ``status_only=True`` / write-shaped
``set_handoff_state`` calls — only the two named *read* helpers are
inspected.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
SKILLS_DIR = PACKAGE_ROOT / "skills"
HOOKS_DIR = PACKAGE_ROOT / "scripts" / "hooks"

# Match ``get_handoff_state(...)`` / ``load_session(...)`` with any args.
# Tolerates whitespace and multi-line arg lists up to the matching close
# paren on the same logical line; for hook .py files multi-line calls
# are captured by reading the full file and matching across newlines.
_CALL_RE = re.compile(
    r"\b(get_handoff_state|load_session)\s*\(([^)]*)\)",
    re.DOTALL,
)

# Comment / placeholder lines we deliberately ignore.
#
# Skill bodies sometimes describe MCP write tools that *project* a
# shape internally — e.g. ``set_handoff_state(...).then(load_session)``
# or ``load_session.foo``. The regex above already requires a literal
# ``(`` so attribute references do not match.
_IGNORED_FILES: frozenset[str] = frozenset(
    {
        # The lint itself documents the patterns it forbids.
        "test_profile_reads_required.py",
        # Slim-handoff-response hook inspects responses post-hoc; the
        # only ``get_handoff_state`` reference in its body is in the
        # module docstring.
        "slim-handoff-response.py",
    }
)


def _call_is_shaped(args: str) -> bool:
    """Return True when ``args`` carries a profile/sections shape."""
    # Both ``read_profile=`` and ``sections=`` are accepted shape markers.
    # We do not require a *literal* enum value — slot resolution and
    # variable-based call sites (e.g. ``read_profile=profile_name``) are
    # allowed; what we forbid is the bare unshaped call.
    return bool(
        re.search(r"\bread_profile\s*=", args) or re.search(r"\bsections\s*=", args)
    )


def _scan(path: Path) -> list[tuple[int, str, str]]:
    """Return a list of ``(line_number, helper, args)`` unshaped reads."""
    if path.name in _IGNORED_FILES:
        return []
    text = path.read_text(encoding="utf-8", errors="replace")
    violations: list[tuple[int, str, str]] = []
    for match in _CALL_RE.finditer(text):
        helper = match.group(1)
        args = match.group(2).strip()
        if _call_is_shaped(args):
            continue
        # Skip docstring / docstring-like placeholders where the call is
        # presented as ``f({...})`` — those are illustrative ``review=``
        # / ``payload=`` arg dictionaries on adjacent helpers, not the
        # read helpers themselves. The regex already pins ``get_handoff_state``
        # / ``load_session`` so all matches are genuine.
        line_number = text.count("\n", 0, match.start()) + 1
        violations.append((line_number, helper, args))
    return violations


@pytest.mark.parametrize(
    "root",
    [
        pytest.param(SKILLS_DIR, id="skills"),
        pytest.param(HOOKS_DIR, id="hooks"),
    ],
)
def test_no_unshaped_handoff_reads(root: Path) -> None:
    """Every routine ``get_handoff_state`` / ``load_session`` call must
    name a ``read_profile=`` or pin ``sections=``.

    When this test fails, add ``read_profile="hot_summary"`` (status
    checks), ``"review_packet"`` (review triage), ``"identity"``
    (routine identity checks), or pin ``sections="..."`` explicitly.
    """
    assert root.exists(), f"expected lint root to exist: {root}"

    extensions = {".md", ".py"}
    failures: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in extensions:
            continue
        for line_no, helper, args in _scan(path):
            rel = path.relative_to(PACKAGE_ROOT)
            failures.append(f"{rel}:{line_no}: unshaped {helper}({args!s})")

    assert not failures, (
        "WORKSTATE-REF-71: handoff read calls must specify read_profile= or "
        "sections= so the response is bounded by construction. "
        "Offending calls:\n  " + "\n  ".join(failures)
    )


def test_lint_self_check_detects_unshaped_calls(tmp_path: Path) -> None:
    """Sanity check: the scanner flags a bare unshaped call."""
    sample = tmp_path / "sample.md"
    sample.write_text("Run `get_handoff_state()` next.\n", encoding="utf-8")
    violations = _scan(sample)
    assert violations, "scanner should detect bare get_handoff_state() calls"
    assert violations[0][1] == "get_handoff_state"


def test_lint_self_check_accepts_shaped_calls(tmp_path: Path) -> None:
    """Sanity check: the scanner permits shaped calls."""
    sample = tmp_path / "sample.md"
    sample.write_text(
        'Run `get_handoff_state(read_profile="identity")` next.\n'
        'Then `load_session(sections="identity")`.\n',
        encoding="utf-8",
    )
    assert _scan(sample) == []
