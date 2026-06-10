"""Distribution-name alias package for ``workstate_handoff_mcp``.

Resolves implementation note BR-06: the wheel ships as ``mcp-workstate-handoff``,
so callers reasonably expect ``import mcp_workstate_handoff`` to work.
The legacy import path ``workstate_handoff_mcp`` remains the source of
truth — this module is a thin re-export so callers can use either
name without a code-base-wide rename.

To import a submodule, use either::

    from mcp_workstate_handoff import api
    from workstate_handoff_mcp import api  # legacy, still supported

Both yield the same module object (verified by tests).
"""

from __future__ import annotations

import importlib
import sys

import workstate_handoff_mcp as _real_pkg
from workstate_handoff_mcp import *  # noqa: F401,F403 — re-export public surface

__all__ = list(getattr(_real_pkg, "__all__", []))


def __getattr__(name: str):
    """Forward attribute lookups (including submodule imports) to ``workstate_handoff_mcp``."""

    try:
        return getattr(_real_pkg, name)
    except AttributeError:
        try:
            module = importlib.import_module(f"workstate_handoff_mcp.{name}")
        except ImportError as exc:
            raise AttributeError(f"module 'mcp_workstate_handoff' has no attribute {name!r}") from exc
        sys.modules[f"mcp_workstate_handoff.{name}"] = module
        return module
