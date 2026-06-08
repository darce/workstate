"""implementation note S3 step-10 — `export_public` privacy filter regression.

The public-export privacy filter (`scripts/export_public.py`) is keyed on the
*literal* paths of internal-only material, which deliberately stay OUTSIDE the
shipped package payload (`packages/workstate-system/workstate_system/payload/`).
This test pins two invariants under the post-payload-cutover layout:

* representative internal paths (evals, adrs, plans, bootstrap state) stay
  **excluded** from the public export, and
* representative shipped-payload paths are **not** excluded (they ship publicly).

If a future change ever moves internal material under `payload/`, the public
export would leak it; the `payload/`-side negative guard in
`test_payload_colocation.py` catches that converse, and this test guards that the
filter itself did not regress when the layout moved.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
SOURCE = REPO_ROOT / "scripts" / "export_public.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("export_public_under_test", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# Representative internal-only paths that must stay out of the public export.
INTERNAL_DENIED = [
    "packages/workstate-system/Makefile.d/evals.mk",  # EXCLUDED_EXACT
    "packages/workstate-system/config/evals/suite.yaml",  # EXCLUDED_PREFIXES
    "packages/workstate-system/scripts/workstate/evals/bench.py",
    "packages/workstate-system/tests/evals/test_quality.py",
    "packages/workstate-system/docs/workstate/adrs/ADR-001-foo.md",  # parts[:5] guard
    ".workstate-bootstrap.json",
    "docs/plans/0020-streamline-overlay-packaging.md",
    "scripts/export_public.py",  # the filter never ships itself
]

# Representative shipped-payload paths that MUST survive into the public export.
PAYLOAD_ALLOWED = [
    "packages/workstate-system/workstate_system/payload/skills/branch-review/SKILL.md",
    "packages/workstate-system/workstate_system/payload/Makefile.d/lifecycle.mk",
    "packages/workstate-system/workstate_system/payload/config/agent-workflows/mcp_servers.yaml",
    "packages/workstate-system/workstate_system/payload/scripts/hooks/git/pre-push",
    "packages/workstate-system/workstate_system/payload/docs/workstate/rules/branch-review-guide.md",
]


@pytest.mark.parametrize("path", INTERNAL_DENIED)
def test_internal_paths_stay_excluded(path: str) -> None:
    module = _load_module()
    assert module._is_excluded(path) is True, path


@pytest.mark.parametrize("path", PAYLOAD_ALLOWED)
def test_shipped_payload_paths_are_not_excluded(path: str) -> None:
    module = _load_module()
    assert module._is_excluded(path) is False, path
