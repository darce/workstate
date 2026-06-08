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


# --- WORKSTATE-REF-5 implementation note: downstream-aware in-process probe + advertise/dispatch consistency ---


def test_in_process_probe_reports_downstream_prerequisite() -> None:
    """In-process probe names its downstream composition and the downstream's probed state."""
    probed = backend_registry.probe_availability("structured-turn")

    downstream = probed["downstream"]
    assert downstream["backend"] == "codex-subagent"
    expected = backend_registry.probe_availability("codex-subagent")
    assert downstream["state"] == expected["state"]
    assert downstream["is_available"] == expected["is_available"]
    # reachability contract unchanged: the adapter itself stays available
    assert probed["is_available"] is True
    assert probed["state"] == backend_registry.AVAIL_AVAILABLE
    assert "codex-subagent" in probed["detail"]


def test_list_available_backends_passes_downstream_through() -> None:
    """The MCP surface forwards the additive downstream annotation untouched."""
    mod = _registry_module()

    def fake_probe(name: str) -> dict:
        spec = mod.get_backend_spec(name)
        probed = {
            "capabilities": spec.capabilities,
            "is_available": True,
            "state": mod.AVAIL_AVAILABLE,
            "detail": "stub",
        }
        if name == "structured-turn":
            probed["downstream"] = {
                "backend": "codex-subagent",
                "state": mod.AVAIL_NOT_INSTALLED,
                "is_available": False,
                "detail": "stub downstream",
            }
        return probed

    with mock.patch.object(mod, "probe_availability", side_effect=fake_probe):
        result = api.list_available_backends()

    assert result["ok"] is True
    assert result["backends"]["structured-turn"]["downstream"]["backend"] == "codex-subagent"
    assert "downstream" not in result["backends"]["codex-subagent"]


def test_advertised_sync_backends_never_hit_bridge_runner_contract_error() -> None:
    """Registry-wide consistency: advertised sync-dispatchable backends are dispatchable.

    Any backend whose probe reports supports_sync_turn and is_available must not
    fail run_structured_turn with the resolve_bridge contract error. CLI kinds
    are excluded — probing them shells out and they are rejected up front by
    run_structured_turn anyway.

    The bridge seam is stubbed so the test pins ROUTING consistency
    deterministically: no real bridge subprocess runs on hosts that have one
    installed, and the dispatch leg cannot pass vacuously — the advertised set
    is computed first against the real registry and must include the
    always-available in-process anchor.
    """
    import json as _json

    def stub_bridge_runner(**kwargs: object) -> dict:
        return {"stubbed": True}

    advertised = []
    for name, spec in backend_registry.BACKENDS.items():
        if spec.kind == "cli":
            continue
        probed = backend_registry.probe_availability(name)
        if probed["capabilities"].supports_sync_turn and probed["is_available"]:
            advertised.append(name)
    # structured-turn is always-available by contract; the loop must never be vacuous.
    assert "structured-turn" in advertised

    mod = _registry_module()
    with mock.patch.object(mod, "resolve_bridge", return_value=stub_bridge_runner):
        for name in advertised:
            payload = api.run_structured_turn(
                prompt="ping",
                schema={"type": "object"},
                cwd=".",
                backend=name,
                timeout_seconds=5.0,
            )
            if isinstance(payload, str):
                payload = _json.loads(payload)
            error = payload.get("error") or ""
            assert "does not expose a bridge runner" not in error, (name, error)


def test_probe_and_adapter_recursion_guards_agree() -> None:
    """The probe-side downstream mirror and the adapter resolve_runner guard fire on the same configuration.

    Both guards refuse an in-process downstream composition; this pins them
    together so they cannot drift independently (WORKSTATE-REF-5 review).
    """
    import pytest

    from workstate_orchestrator_mcp.orchestration.adapters.structured_turn import StructuredTurnAdapter

    recursive_adapter = StructuredTurnAdapter(name="structured-turn", downstream_backend="structured-turn")

    # Adapter-side guard: dispatch refuses at runner resolution.
    with pytest.raises(RuntimeError, match="recursive composition"):
        recursive_adapter.resolve_runner()

    # Probe-side mirror: the same configuration annotates the downstream
    # unavailable with the same refusal, without recursing the probe.
    with mock.patch.object(backend_registry, "get_adapter", return_value=recursive_adapter):
        probed = backend_registry.probe_availability("structured-turn")

    downstream = probed["downstream"]
    assert downstream["backend"] == "structured-turn"
    assert downstream["is_available"] is False
    assert downstream["state"] == backend_registry.AVAIL_UNAVAILABLE
    assert "recursive composition is refused" in downstream["detail"]
    # reachability contract for the adapter itself is unchanged
    assert probed["is_available"] is True


def test_get_adapter_threads_registry_name_to_in_process_adapter() -> None:
    """In-process adapters are identified by construction, not single-backend coincidence (WORKSTATE-REF-5 review)."""
    adapter = backend_registry.get_adapter("structured-turn")
    assert adapter.name == "structured-turn"
    assert adapter.downstream_backend == "codex-subagent"
    # default (no injected runner) ⇒ the dispatch layer may unwrap the downstream envelope
    assert adapter.runner_emits_envelope is True
