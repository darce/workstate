from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
ORCHESTRATION_DIR = Path(__file__).resolve().parents[1] / "src" / "workstate_orchestrator_mcp" / "orchestration"
SCRIPT_PATH = ORCHESTRATION_DIR / "backend_registry.py"
if str(ORCHESTRATION_DIR) not in sys.path:
    sys.path.insert(0, str(ORCHESTRATION_DIR))


def _load_module():
    spec = importlib.util.spec_from_file_location("backend_registry", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load backend_registry module from {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_get_backend_choices_returns_tuple() -> None:
    mod = _load_module()
    choices = mod.get_backend_choices()
    assert isinstance(choices, tuple)
    assert "codex-cli" in choices
    assert "codex-subagent" in choices
    assert "copilot-host" in choices


def test_validate_backend_accepts_registered_backend() -> None:
    mod = _load_module()
    assert mod.validate_backend("codex-cli") == "codex-cli"


def test_validate_backend_rejects_unknown_backend() -> None:
    mod = _load_module()
    with pytest.raises(RuntimeError, match="Unsupported execution backend 'unknown'"):
        mod.validate_backend("unknown")


def test_get_backend_spec_returns_registered_spec() -> None:
    mod = _load_module()
    spec = mod.get_backend_spec("codex-subagent")
    assert spec.kind == "bridge"
    assert spec.module == "workstate_codex_bridge"
    assert spec.adapter_path.endswith(".CodexSubagentAdapter")


def test_resolve_bridge_rejects_cli_backend() -> None:
    mod = _load_module()
    with pytest.raises(RuntimeError, match="does not expose a bridge runner"):
        mod.resolve_bridge("codex-cli")


def test_resolve_bridge_rejects_unknown_backend() -> None:
    mod = _load_module()
    with pytest.raises(RuntimeError, match="Unsupported execution backend 'unknown'"):
        mod.resolve_bridge("unknown")


def test_resolve_bridge_reports_missing_module() -> None:
    mod = _load_module()
    with mock.patch.object(mod.importlib, "import_module", side_effect=ImportError("missing")):
        with pytest.raises(RuntimeError, match="codex-subagent backend is unavailable"):
            mod.resolve_bridge("codex-subagent")


def test_resolve_bridge_returns_runner() -> None:
    mod = _load_module()
    runner = mock.Mock()
    fake_bridge = mock.Mock(run_subagent=runner)
    with mock.patch.object(mod.importlib, "import_module", return_value=fake_bridge):
        assert mod.resolve_bridge("codex-subagent") is runner


def test_get_adapter_returns_initialized_adapter() -> None:
    mod = _load_module()
    mock_cli = mock.Mock()
    mock_cls = mock.Mock(return_value=mock_cli)
    spec = mod.get_backend_spec("codex-cli")
    new_spec = mod.BackendSpec(
        kind=spec.kind,
        adapter_path="fake.adapters.MockCliAdapter",
        description=spec.description,
        capabilities=spec.capabilities,
    )
    with mock.patch.dict(mod.BACKENDS, {"codex-cli": new_spec}):
        with mock.patch.object(mod.importlib, "import_module", return_value=mock.Mock(MockCliAdapter=mock_cls)):
            adapter = mod.get_adapter("codex-cli", codex_bin="/path/to/codex")
        mock_cls.assert_called_once_with(codex_bin="/path/to/codex")
        assert adapter is mock_cli


def test_get_adapter_for_bridge_returns_subagent_adapter() -> None:
    mod = _load_module()
    runner = mock.Mock()
    mock_sub = mock.Mock()
    mock_cls = mock.Mock(return_value=mock_sub)
    spec = mod.get_backend_spec("codex-subagent")
    new_spec = mod.BackendSpec(
        kind=spec.kind,
        adapter_path="fake.adapters.MockSubagentAdapter",
        description=spec.description,
        module=spec.module,
        capabilities=spec.capabilities,
    )
    with mock.patch.dict(mod.BACKENDS, {"codex-subagent": new_spec}):
        with mock.patch.object(mod, "resolve_bridge", return_value=runner):
            with mock.patch.object(
                mod.importlib, "import_module", return_value=mock.Mock(MockSubagentAdapter=mock_cls)
            ):
                adapter = mod.get_adapter("codex-subagent")
        mock_cls.assert_called_once_with(runner, name="codex-subagent")
        assert adapter is mock_sub


def test_get_backend_choices_includes_copilot_host() -> None:
    mod = _load_module()
    choices = mod.get_backend_choices()
    assert "copilot-host" in choices


def test_backend_spec_has_capabilities() -> None:
    mod = _load_module()
    spec = mod.get_backend_spec("codex-subagent")
    assert spec.capabilities.supports_structured_output is True
    assert spec.capabilities.supports_sandbox is True
    assert spec.capabilities.supports_sync_turn is True


def test_copilot_host_capabilities() -> None:
    mod = _load_module()
    spec = mod.get_backend_spec("copilot-host")
    assert spec.kind == "bridge"
    assert spec.capabilities.supports_structured_output is False
    assert spec.capabilities.supports_sandbox is False
    assert spec.capabilities.supports_sync_turn is True


def test_cli_backend_capabilities() -> None:
    mod = _load_module()
    spec = mod.get_backend_spec("codex-cli")
    assert spec.capabilities.supports_sync_turn is False
    assert spec.capabilities.supports_sandbox is True


def test_register_backend_adds_new_entry() -> None:
    mod = _load_module()
    custom_spec = mod.BackendSpec(
        kind="bridge",
        adapter_path="fake.adapters.CustomAdapter",
        module="my_custom_bridge",
        description="Custom bridge for testing.",
        capabilities=mod.BackendCapabilities(
            supports_structured_output=True,
            supports_sandbox=False,
            supports_sync_turn=True,
        ),
    )
    mod.register_backend("my-custom", custom_spec)
    try:
        assert "my-custom" in mod.get_backend_choices()
        assert mod.get_backend_spec("my-custom") is custom_spec
    finally:
        del mod.BACKENDS["my-custom"]


def test_detect_runtime_returns_none_without_vscode_signals() -> None:
    mod = _load_module()
    with mock.patch.dict("os.environ", {}, clear=True):
        assert mod.detect_runtime() is None


def test_detect_runtime_returns_copilot_host_with_vscode_signals() -> None:
    mod = _load_module()
    env = {
        "VSCODE_PID": "12345",
        "VSCODE_AGENT_FOLDER": "/some/path/copilot-agent",
    }
    with mock.patch.dict("os.environ", env, clear=True):
        assert mod.detect_runtime() == "copilot-host"


def test_detect_runtime_returns_none_with_vscode_but_no_copilot() -> None:
    mod = _load_module()
    env = {"VSCODE_PID": "12345"}
    with mock.patch.dict("os.environ", env, clear=True):
        assert mod.detect_runtime() is None


def test_find_codex_from_search_paths(tmp_path: Path) -> None:
    _load_module()
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    fake_bin = fake_home / ".local" / "bin" / "codex"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text("#!/bin/sh")
    fake_bin.chmod(0o755)

    def fake_exists(p):
        return str(p) == str(fake_bin)

    with (
        mock.patch("pathlib.Path.home", return_value=fake_home),
        mock.patch(
            "workstate_orchestrator_mcp.orchestration.adapters.codex_cli.Path.exists", autospec=True
        ) as mock_exists,
    ):
        mock_exists.side_effect = lambda self: str(self) == str(fake_bin)
        from workstate_orchestrator_mcp.orchestration.adapters.codex_cli import find_codex

        assert find_codex() == str(fake_bin)


def test_probe_capabilities_codex_cli_not_found() -> None:
    mod = _load_module()
    with mock.patch(
        "workstate_orchestrator_mcp.orchestration.adapters.codex_cli.find_codex", side_effect=RuntimeError("not found")
    ):
        caps = mod.probe_capabilities("codex-cli")
        assert caps.is_available is False
        assert caps.supports_reasoning_effort is False


def test_probe_capabilities_codex_cli_found_with_reasoning() -> None:
    mod = _load_module()
    fake_help = "Options:\n  --reasoning-effort [low|medium|high]"
    with (
        mock.patch("workstate_orchestrator_mcp.orchestration.adapters.codex_cli.find_codex", return_value="/bin/codex"),
        mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=fake_help)),
    ):
        caps = mod.probe_capabilities("codex-cli")
        assert caps.is_available is True
        assert caps.supports_reasoning_effort is True


def test_probe_capabilities_codex_cli_found_without_reasoning() -> None:
    mod = _load_module()
    fake_help = "Options:\n  --model NAME"
    with (
        mock.patch("workstate_orchestrator_mcp.orchestration.adapters.codex_cli.find_codex", return_value="/bin/codex"),
        mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=fake_help)),
    ):
        caps = mod.probe_capabilities("codex-cli")
        assert caps.is_available is True
        assert caps.supports_reasoning_effort is False


def test_probe_capabilities_codex_cli_timeout_reports_unavailable() -> None:
    mod = _load_module()
    with (
        mock.patch("workstate_orchestrator_mcp.orchestration.adapters.codex_cli.find_codex", return_value="/bin/codex"),
        mock.patch("subprocess.run", side_effect=mod.subprocess.TimeoutExpired(cmd=["codex"], timeout=10)),
    ):
        caps = mod.probe_capabilities("codex-cli")
        assert caps.is_available is False


# --- probe_availability: declared-not-installed vs reachable vs available ---


def test_probe_availability_in_process_backend_is_available() -> None:
    mod = _load_module()
    res = mod.probe_availability("structured-turn")
    assert res["state"] == mod.AVAIL_AVAILABLE
    assert res["is_available"] is True
    assert res["capabilities"].is_available is True


def test_probe_availability_bridge_reachable_when_module_imports() -> None:
    mod = _load_module()
    fake_bridge = mock.Mock(run_subagent=mock.Mock())
    with mock.patch.object(mod.importlib, "import_module", return_value=fake_bridge):
        res = mod.probe_availability("codex-subagent")
    assert res["state"] == mod.AVAIL_REACHABLE
    assert res["is_available"] is True
    assert res["capabilities"].supports_sync_turn is True
    # Reachability is not liveness — the detail must say so.
    assert "liveness" in res["detail"].lower()


def test_probe_availability_bridge_declared_not_installed_when_import_fails() -> None:
    mod = _load_module()
    with mock.patch.object(mod.importlib, "import_module", side_effect=ImportError("missing module")):
        res = mod.probe_availability("codex-subagent")
    assert res["state"] == mod.AVAIL_NOT_INSTALLED
    assert res["is_available"] is False
    assert res["capabilities"].supports_sync_turn is True
    # The optional host module name is surfaced so operators know what to install.
    assert "workstate_codex_bridge" in res["detail"]


def test_probe_availability_bridge_unavailable_when_runner_missing() -> None:
    mod = _load_module()
    fake_bridge = mock.Mock(run_subagent=None)
    with mock.patch.object(mod.importlib, "import_module", return_value=fake_bridge):
        res = mod.probe_availability("codex-subagent")
    assert res["state"] == mod.AVAIL_UNAVAILABLE
    assert res["is_available"] is False
    assert res["capabilities"].supports_sync_turn is True
    assert "run_subagent" in res["detail"]


def test_probe_availability_cli_available_propagates_probed_caps() -> None:
    mod = _load_module()
    fake_help = "Options:\n  --reasoning-effort [low|medium|high]"
    with (
        mock.patch("workstate_orchestrator_mcp.orchestration.adapters.codex_cli.find_codex", return_value="/bin/codex"),
        mock.patch("subprocess.run", return_value=mock.Mock(returncode=0, stdout=fake_help)),
    ):
        res = mod.probe_availability("codex-cli")
    assert res["state"] == mod.AVAIL_AVAILABLE
    assert res["is_available"] is True
    assert res["capabilities"].supports_reasoning_effort is True


def test_probe_availability_cli_unavailable_when_binary_missing() -> None:
    mod = _load_module()
    with mock.patch(
        "workstate_orchestrator_mcp.orchestration.adapters.codex_cli.find_codex", side_effect=RuntimeError("not found")
    ):
        res = mod.probe_availability("codex-cli")
    assert res["state"] == mod.AVAIL_UNAVAILABLE
    assert res["is_available"] is False


def test_probe_availability_api_kind_reports_unknown() -> None:
    mod = _load_module()
    res = mod.probe_availability("local-model-openai")
    assert res["state"] == mod.AVAIL_UNKNOWN
