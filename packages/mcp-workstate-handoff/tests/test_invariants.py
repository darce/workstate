from __future__ import annotations

from workstate_handoff_mcp.api import (
    TOOL_DESCRIPTIONS,
    _artifact_tool_entries,
    _build_tool_registry,
    _lifecycle_tool_entries,
    _review_tool_entries,
    _task_state_tool_entries,
)
from workstate_handoff_mcp.invariants import EXPECTED_HANDOFF_TOOL_COUNT

_VALID_PROFILES = frozenset({"core", "extended"})


def test_search_handoff_description_advertises_decision_fields() -> None:
    """MCP clients discover the decision projection through the tool description."""
    desc = TOOL_DESCRIPTIONS["search_handoff"]
    assert "decision_fields" in desc, (
        "search_handoff description must advertise the decision_fields projection so "
        "MCP clients can discover it without reading the contract doc"
    )


def test_get_handoff_state_description_advertises_decision_filters() -> None:
    """MCP clients discover the exact-read decision filters through the tool description."""
    desc = TOOL_DESCRIPTIONS["get_handoff_state"]
    for token in ("decision_fields", "decision_branch", "decision_id_prefix"):
        assert token in desc, f"get_handoff_state description must mention {token}"


def test_tool_registry_matches_named_invariant() -> None:
    """The live tool registry size must equal the named cross-transport invariant.

    Adding or removing a tool requires updating EXPECTED_HANDOFF_TOOL_COUNT in
    one place; this test ensures the registry never drifts away from it.
    """
    registry = _build_tool_registry()
    tool_names = {entry.name for entry in registry}
    assert len(tool_names) == len(registry), "duplicate tool names in registry"
    assert len(registry) == EXPECTED_HANDOFF_TOOL_COUNT


def test_registry_group_builders_exported() -> None:
    """_build_tool_registry must delegate to named module-level group builders.

    Each builder owns one domain cluster; their concatenation must reproduce
    the full registry in order so callers that consume a slice can rely on
    stable relative ordering within each group.
    """
    full = _build_tool_registry()
    from_builders = (
        _task_state_tool_entries() + _review_tool_entries() + _lifecycle_tool_entries() + _artifact_tool_entries()
    )
    assert [e.name for e in full] == [e.name for e in from_builders]


def test_tool_registry_profile_values_in_supported_set() -> None:
    """Every registry entry must carry a profile value from the supported set."""
    registry = _build_tool_registry()
    invalid = [(entry.name, entry.profile) for entry in registry if entry.profile not in _VALID_PROFILES]
    assert not invalid, (
        f"registry entries with unsupported profile values: {invalid}; allowed: {sorted(_VALID_PROFILES)}"
    )
