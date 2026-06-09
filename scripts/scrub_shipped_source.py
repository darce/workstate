#!/usr/bin/env python3
"""Strip internal project ids from the source that ships to PyPI, in place.

PyPI wheels build from the raw tree, so internal task/process refs in shipped
source (docstrings, comments, contract docs, payload) leak publicly. This applies
the SAME transforms as the public git export (scripts/export_public.py) directly
to the wheel-shipped source roots, so source == scrubbed output and
scripts/check_shipped_privacy.py (which shares the matchers) passes.

  - internal task/work refs (internal / WS-* / internal / internal / internal / internal / MAINT-* /
    internal / epic refs)         -> "internal"
  - plan / slice / step process refs -> "implementation note"

Product vocabulary (BR-* / REV-* finding ids, the bare MAINT task category) is
NOT a ref match and is left untouched.

Usage:
  python scripts/scrub_shipped_source.py            # report files that would change
  python scripts/scrub_shipped_source.py --apply    # rewrite them in place
"""

from __future__ import annotations

import re
import sys

from export_public import (
    INLINE_EPIC_REF_RE,
    INLINE_INTERNAL_PREFIX_RE,
    INTERNAL_REF_RE,
    PROCESS_REF_RES,
)
from check_shipped_privacy import (
    REPO_ROOT,
    _iter_shipped_files,
    _publishable_packages,
    _shipped_roots,
)


# Adjacent refs collapse to repeated placeholders (e.g. "internal internal" ->
# "internal internal", "Plan 9 implementation note" -> "implementation note ...""); fold any
# run of the two placeholders back into a single neutral token.
_COLLAPSE_RE = re.compile(r"\b(?:internal|implementation note)(?:[ \t]+(?:internal|implementation note))+\b")


def _scrub(text: str) -> str:
    scrubbed = INTERNAL_REF_RE.sub("internal", text)
    scrubbed = INLINE_INTERNAL_PREFIX_RE.sub("internal", scrubbed)
    scrubbed = INLINE_EPIC_REF_RE.sub("internal", scrubbed)
    for regex in PROCESS_REF_RES:
        scrubbed = regex.sub("implementation note", scrubbed)
    scrubbed = _COLLAPSE_RE.sub("internal", scrubbed)
    return scrubbed


def main() -> int:
    apply = "--apply" in sys.argv[1:]
    changed: list[str] = []
    for pkg in _publishable_packages():
        package_dir = REPO_ROOT / str(pkg["path"])
        for root in _shipped_roots(package_dir):
            for path in _iter_shipped_files(root):
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                scrubbed = _scrub(text)
                if scrubbed != text:
                    changed.append(path.relative_to(REPO_ROOT).as_posix())
                    if apply:
                        path.write_text(scrubbed, encoding="utf-8")

    verb = "scrubbed" if apply else "would scrub"
    print(f"{verb} {len(changed)} shipped source file(s)")
    for rel in changed:
        print(f"  {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
