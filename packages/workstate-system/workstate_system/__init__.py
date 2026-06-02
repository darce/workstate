"""workstate-system overlay payload package.

Ships the canonical overlay surfaces — skills, the workflow generator,
agent-workflows config, and the shared hook/contract surfaces — as package
data so ``workstate-bootstrap`` can materialize them from an installed
distribution (the package delivery source) instead of a git clone.

The payload is force-included under this package directory at build time
(see ``pyproject.toml``), so :func:`data_root` resolves to the directory that
contains ``skills/``, ``scripts/``, ``config/``, ``docs/``, ``Makefile.d/``,
and ``.github/`` once installed.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

__all__ = ["data_root"]


def data_root() -> Path:
    """Return the filesystem root of the installed overlay payload."""
    return Path(str(resources.files(__name__)))
