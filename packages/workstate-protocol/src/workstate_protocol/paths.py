"""Canonical Workstate runtime + doc-mirror path roots — single source of truth.

The runtime install directory and the mirrored docs/contracts path were renamed
from the legacy ``.agentic/`` / ``docs/agentic/`` to ``.workstate/`` /
``docs/workstate/`` (implementation note Slice D, completing implementation note §11 items #3/#4).

Every package resolves these through this module so the names live in exactly
one place: a future path change is a one-line flip here, not a repo-wide sweep.
The ``LEGACY_*`` names and :data:`RUNTIME_PATH_RENAMES` exist so bootstrap can
detect and migrate an old checkout forward for one release.
"""

from __future__ import annotations

from pathlib import Path

# Runtime install root — bootstrap materializes overlay surfaces and the remote
# clone under ``<target>/.workstate/``.
RUNTIME_ROOT_DIRNAME = ".workstate"
LEGACY_RUNTIME_ROOT_DIRNAME = ".agentic"

# Mirrored docs / contracts path — the SHARED_SURFACES consumed at install time
# (rules, contracts, templates) live under ``docs/workstate/``.
DOCS_MIRROR_DIR = "docs/workstate"
LEGACY_DOCS_MIRROR_DIR = "docs/agentic"

# Ordered (legacy -> canonical) pairs for migration / detection sweeps.
RUNTIME_PATH_RENAMES: tuple[tuple[str, str], ...] = (
    (LEGACY_RUNTIME_ROOT_DIRNAME, RUNTIME_ROOT_DIRNAME),
    (LEGACY_DOCS_MIRROR_DIR, DOCS_MIRROR_DIR),
)

# Common derived locations under the canonical docs mirror.
CONTRACTS_DIR = f"{DOCS_MIRROR_DIR}/contracts"
RULES_DIR = f"{DOCS_MIRROR_DIR}/rules"
HARNESS_CONTRACT_RELPATH = Path(CONTRACTS_DIR) / "harness-protocol.yaml"
INSTRUCTIONS_RELPATH = Path(DOCS_MIRROR_DIR) / "instructions.md"

__all__ = [
    "CONTRACTS_DIR",
    "DOCS_MIRROR_DIR",
    "HARNESS_CONTRACT_RELPATH",
    "INSTRUCTIONS_RELPATH",
    "LEGACY_DOCS_MIRROR_DIR",
    "LEGACY_RUNTIME_ROOT_DIRNAME",
    "RULES_DIR",
    "RUNTIME_PATH_RENAMES",
    "RUNTIME_ROOT_DIRNAME",
    "docs_mirror_path",
    "runtime_root_path",
]


def docs_mirror_path(*parts: str) -> Path:
    """Return a path under the canonical docs mirror (``docs/workstate/...``)."""

    return Path(DOCS_MIRROR_DIR, *parts)


def runtime_root_path(base: Path, *parts: str) -> Path:
    """Return a path under ``<base>/.workstate/...``."""

    return base.joinpath(RUNTIME_ROOT_DIRNAME, *parts)
