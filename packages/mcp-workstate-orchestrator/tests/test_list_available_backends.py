"""Availability contract for ``api.list_available_backends``.

The default MCP-facing call MUST be truthful enough for skills to route without
first attempting a failing turn. It includes the probed availability view
(delegated to ``backend_registry.probe_availability``) and distinguishes a
declared-but-not-installed optional bridge from a reachable one. Callers that
need the old cheap declaration-only view may pass ``probe=False`` explicitly.
"""

from __future__ import annotations

from unittest import mock

from workstate_orchestrator_mcp import api
from workstate_orchestrator_mcp.orchestration import backend_registry

PROBED_KEYS = {"is_available", "availability_state", "availability_detail"}


def _registry_module():
    """Return the exact backend_registry module object the api handler uses."""
    return api._import_orchestration_module("backend_registry")


def test_default_call_reports_probed_availability() -> None:
    """Default returns availability fields so MCP callers can route safely."""
    mod = _registry_module()

    def fake_probe(name: str) -> dict:
        spec = mod.get_backend_spec(name)
        reachable = name != "codex-subagent"
        return {
            "capabilities": spec.capabilities,
            "is_available": reachable,
            "state": mod.AVAIL_REACHABLE if reachable else mod.AVAIL_NOT_INSTALLED,
            "detail": "stub",
        }

    with mock.patch.object(mod, "probe_availability", side_effect=fake_probe) as probe:
        result = api.list_available_backends()
    assert probe.call_count == len(backend_registry.BACKENDS)
    assert result["ok"] is True
    assert result["probed"] is True
    for name, entry in result["backends"].items():
        assert PROBED_KEYS.issubset(entry), name


def test_probe_false_is_cheap_and_omits_probed_fields() -> None:
    """probe=False returns the static table only — no probed fields."""
    with (
        mock.patch.object(backend_registry, "probe_availability") as probe,
        mock.patch("subprocess.run", side_effect=AssertionError("probe=False path must not shell out")),
    ):
        result = api.list_available_backends(probe=False)
    probe.assert_not_called()
    assert result["ok"] is True
    assert result["probed"] is False
    for name, spec in backend_registry.BACKENDS.items():
        entry = result["backends"][name]
        assert set(entry) == {"kind", "description", "supports_reasoning_effort", "supports_sync_turn"}, name
        assert entry["kind"] == spec.kind
        assert entry["description"] == spec.description
        assert entry["supports_reasoning_effort"] == spec.capabilities.supports_reasoning_effort
        assert entry["supports_sync_turn"] == spec.capabilities.supports_sync_turn


def test_probe_true_adds_availability_fields_for_every_backend() -> None:
    """probe=True surfaces is_available + availability_state/detail per backend."""
    mod = _registry_module()

    def fake_probe(name: str) -> dict:
        spec = mod.get_backend_spec(name)
        reachable = name != "codex-subagent"  # force one declared_not_installed
        return {
            "capabilities": spec.capabilities,
            "is_available": reachable,
            "state": mod.AVAIL_REACHABLE if reachable else mod.AVAIL_NOT_INSTALLED,
            "detail": "stub",
        }

    with mock.patch.object(mod, "probe_availability", side_effect=fake_probe):
        result = api.list_available_backends(probe=True)

    assert result["ok"] is True
    assert result["probed"] is True
    for name, entry in result["backends"].items():
        assert PROBED_KEYS.issubset(entry), name

    # The forced-missing optional bridge is flagged distinctly, not "available".
    subagent = result["backends"]["codex-subagent"]
    assert subagent["is_available"] is False
    assert subagent["availability_state"] == backend_registry.AVAIL_NOT_INSTALLED
    assert subagent["supports_sync_turn"] is True


def test_probe_true_prefers_probed_capability_flags() -> None:
    """Probed capability flags win over the static table (e.g. codex-cli effort)."""
    mod = _registry_module()
    probed_caps = backend_registry.BackendCapabilities(
        is_available=True,
        supports_reasoning_effort=True,
        supports_sync_turn=True,
    )

    def fake_probe(name: str) -> dict:
        return {
            "capabilities": probed_caps,
            "is_available": True,
            "state": mod.AVAIL_AVAILABLE,
            "detail": "stub",
        }

    with mock.patch.object(mod, "probe_availability", side_effect=fake_probe):
        result = api.list_available_backends(probe=True)

    # codex-cli statically declares supports_reasoning_effort=False; the probed
    # view must override it to True.
    codex_cli = result["backends"]["codex-cli"]
    assert codex_cli["supports_reasoning_effort"] is True
