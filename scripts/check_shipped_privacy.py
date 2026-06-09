#!/usr/bin/env python3
"""Fail-closed privacy gate for the surface shipped to PyPI.

The public *git* export is scrubbed by ``scripts/export_public.py``, but PyPI
wheels are built from the RAW source tree, so a separate gate is needed to keep
personal info and internal project ids out of published wheels. This scans the
files that actually ship in each publishable wheel (the importable package dir
+ payload + README long-description) for the same leak classes the export gate
blocks:

  - personal identifiers and local home paths (FORBIDDEN_TEXT_TOKENS),
  - internal task/work refs — internal / WS-* / internal / internal / internal / internal /
    MAINT-* / internal / epic refs (INTERNAL_REF_RE + inline variants),
  - plan/slice/step process refs and dated internal-doc citations,
  - references to internal planning docs that exist in the private tree.

Product vocabulary (BR-* / REV-* review-finding ids, the bare MAINT task
category) is intentionally NOT flagged — it is not in the internal-ref matchers.

Run standalone (``python scripts/check_shipped_privacy.py``) or via
``make preflight``; exits non-zero with a per-file report when anything leaks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Reuse the single source of truth for what counts as a leak.
from export_public import (
    FORBIDDEN_TEXT_TOKENS,
    FORBIDDEN_TEXT_RES,
    INTERNAL_DOC_REF_RE,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "config" / "release" / "packages.json"

# Build noise that never lands in the wheel. NOTE: we do NOT skip "docs" or
# test files here — the workstate-system payload ships payload/docs/** and
# payload hook test_*.py, so those are part of the published surface. Package-
# level tests/ and docs/ are excluded structurally (they live outside the
# importable package dir returned by _shipped_roots()).
_SKIP_DIR_NAMES = {"dist", "build", "__pycache__"}
_SKIP_SUFFIXES = {".pyc"}


def _publishable_packages() -> list[dict[str, object]]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return [pkg for pkg in manifest.get("packages", []) if pkg.get("publish")]


def _shipped_roots(package_dir: Path) -> list[Path]:
    """The wheel-shipped source roots for a package: the importable package
    directory (under src/ or at the package root) plus the README."""
    roots: list[Path] = []
    if not package_dir.is_dir():
        # A manifest can name a package whose tree is absent (e.g. a minimal
        # test repo); nothing to scan.
        return roots
    src = package_dir / "src"
    search_bases = [src] if src.is_dir() else [package_dir]
    for base in search_bases:
        if not base.is_dir():
            continue
        for child in base.iterdir():
            if child.is_dir() and (child / "__init__.py").exists():
                roots.append(child)
    readme = package_dir / "README.md"
    if readme.exists():
        roots.append(readme)
    return roots


def _iter_shipped_files(root: Path):
    if root.is_file():
        yield root
        return
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if any(part in _SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.suffix in _SKIP_SUFFIXES:
            continue
        yield path


def _scan_text(rel: str, text: str) -> list[str]:
    findings: list[str] = []
    for token in FORBIDDEN_TEXT_TOKENS:
        if token in text:
            findings.append(f"  {rel}: forbidden token {token!r}")
    for regex in FORBIDDEN_TEXT_RES:
        match = regex.search(text)
        if match:
            findings.append(f"  {rel}: internal ref/process token {match.group(0)!r}")
            break
    for match in INTERNAL_DOC_REF_RE.finditer(text):
        ref = match.group(0)
        if (REPO_ROOT / ref).is_file():
            findings.append(f"  {rel}: internal planning-doc reference {ref!r}")
            break
    return findings


def main() -> int:
    findings: list[str] = []
    for pkg in _publishable_packages():
        package_dir = REPO_ROOT / str(pkg["path"])
        for root in _shipped_roots(package_dir):
            for path in _iter_shipped_files(root):
                try:
                    text = path.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    continue
                findings.extend(_scan_text(path.relative_to(REPO_ROOT).as_posix(), text))

    if findings:
        print("shipped-surface privacy gate — these files ship personal info or", file=sys.stderr)
        print("internal project ids in their PyPI wheels and must be scrubbed:", file=sys.stderr)
        for line in findings:
            print(line, file=sys.stderr)
        return 1
    print("ok: no publishable wheel ships personal info or internal project ids")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
