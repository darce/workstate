"""Tier-4 env-var alias resolution for the Workstate rebrand.

implementation note Slice C / B4. During the one-release cutover from the legacy
``AGENT_HANDOFF_*`` / ``AGENT_ORCHESTRATOR_*`` / ``AGENTIC_*`` /
``MCP_AGENT_HANDOFF_*`` env-var names to the canonical ``WORKSTATE_*`` prefix,
:func:`resolve_env_alias` reads the new name first, then falls back to any
legacy alias, emitting exactly one :class:`DeprecationWarning` per legacy name
that is actually read. The write side (exports, generated config) always sets
the new ``WORKSTATE_*`` name only — this module is read-side compatibility so
existing shells / CI that still export the old names keep working through the
cutover release, with a nudge to migrate.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Mapping
from typing import overload

__all__ = ["resolve_env_alias", "reset_alias_warnings"]

# Legacy names already warned about, so the deprecation nudge fires once per
# process regardless of how many call sites read the same var.
_warned_legacy: set[str] = set()


def reset_alias_warnings() -> None:
    """Clear the warn-once ledger. Intended for tests."""

    _warned_legacy.clear()


def _non_empty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


@overload
def resolve_env_alias(
    canonical: str,
    *legacy: str,
    env: Mapping[str, str] | None = ...,
    default: str,
) -> str: ...


@overload
def resolve_env_alias(
    canonical: str,
    *legacy: str,
    env: Mapping[str, str] | None = ...,
    default: None = ...,
) -> str | None: ...


def resolve_env_alias(
    canonical: str,
    *legacy: str,
    env: Mapping[str, str] | None = None,
    default: str | None = None,
) -> str | None:
    """Resolve an env var across its canonical and legacy alias names.

    Precedence: the canonical ``WORKSTATE_*`` name wins when set to a
    non-blank value. Otherwise the first non-blank legacy alias (in the order
    given) is returned, and a :class:`DeprecationWarning` is emitted once per
    process for that legacy name, naming the ``canonical`` replacement. When
    nothing is set, ``default`` is returned.

    Blank/whitespace-only values are treated as unset, matching the existing
    ``_first_non_empty_env`` behaviour in the handoff package.
    """

    source = os.environ if env is None else env

    canonical_value = _non_empty(source.get(canonical))
    if canonical_value is not None:
        return canonical_value

    for name in legacy:
        legacy_value = _non_empty(source.get(name))
        if legacy_value is not None:
            if name not in _warned_legacy:
                _warned_legacy.add(name)
                warnings.warn(
                    f"Environment variable {name} is deprecated; "
                    f"set {canonical} instead. The legacy name is read for "
                    f"one release and will be removed.",
                    DeprecationWarning,
                    stacklevel=2,
                )
            return legacy_value

    return default
