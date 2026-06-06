from __future__ import annotations

import json
import time
from pathlib import Path
from unittest import mock

from workstate_orchestrator_mcp import api

REPO_ROOT = Path(__file__).resolve().parents[3]


def _parse(payload: str | dict) -> dict:
    """WORKSTATE-REF-10 dict-return migration: handler returns are dicts now;
    only fall back to json.loads when something legitimately hands us a
    string (e.g. CLI stdout capture)."""
    if not isinstance(payload, dict):
        payload = json.loads(payload)
    if isinstance(payload, dict) and payload.get("schema_version") == 2:
        data = payload.get("data")
        scope = payload.get("scope")
        flat = dict(payload)
        if isinstance(data, dict):
            flat.update(data)
        if "task_ref" not in flat and isinstance(scope, dict) and scope.get("task_ref"):
            flat["task_ref"] = scope["task_ref"]
        return flat
    return payload


def _write_harness_contract(tmp_path: Path, *, daemons_enabled: bool) -> None:
    contract_path = tmp_path / "docs" / "workstate" / "contracts" / "harness-protocol.yaml"
    contract_path.parent.mkdir(parents=True, exist_ok=True)
    enabled = "true" if daemons_enabled else "false"
    contract_path.write_text(
        f"version: 1\norchestrator:\n  daemons:\n    enabled: {enabled}\n",
        encoding="utf-8",
    )


def _configure_runtime(tmp_path: Path, *, daemons_enabled: bool = True) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=daemons_enabled)
    api.configure_runtime(
        api.RuntimeConfig.for_workspace(
            tmp_path,
            state_dir=tmp_path / ".task-state",
            current_task_path=tmp_path / "CURRENT_TASK.json",
            exports_dir=tmp_path / ".task-state" / "exports",
        )
    )


def test_manage_orchestrator_start_requires_daemon_opt_in(tmp_path: Path) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=False)

    with (
        mock.patch.object(api, "get_runtime_config", return_value=mock.Mock(workspace_root=tmp_path)),
        mock.patch.object(api, "orchestrator_start") as mock_start,
    ):
        payload = _parse(api.manage_orchestrator(operation="start", task_ref="daemon-8"))

    assert payload["ok"] is False
    assert "Daemons are opt-in." in payload["error"]
    mock_start.assert_not_called()


def test_manage_orchestrator_single_cycle_requires_daemon_opt_in(tmp_path: Path) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=False)

    with (
        mock.patch.object(api, "get_runtime_config", return_value=mock.Mock(workspace_root=tmp_path)),
        mock.patch.object(api, "orchestrator_single_cycle") as mock_single_cycle,
    ):
        payload = _parse(api.manage_orchestrator(operation="single_cycle", task_ref="daemon-8"))

    assert payload["ok"] is False
    assert "Daemons are opt-in." in payload["error"]
    mock_single_cycle.assert_not_called()


def test_manage_worker_start_requires_daemon_opt_in(tmp_path: Path) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=False)

    with (
        mock.patch.object(api, "get_runtime_config", return_value=mock.Mock(workspace_root=tmp_path)),
        mock.patch.object(api, "worker_start") as mock_start,
    ):
        payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="frontend", action="start"))

    assert payload["ok"] is False
    assert "Daemons are opt-in." in payload["error"]
    mock_start.assert_not_called()


def test_manage_worker_start_all_requires_daemon_opt_in(tmp_path: Path) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=False)

    with (
        mock.patch.object(api, "get_runtime_config", return_value=mock.Mock(workspace_root=tmp_path)),
        mock.patch.object(api, "worker_start_all") as mock_start_all,
    ):
        payload = _parse(api.manage_worker(task_ref="daemon-10", action="start_all"))

    assert payload["ok"] is False
    assert "Daemons are opt-in." in payload["error"]
    mock_start_all.assert_not_called()


def test_dispatch_lane_work_start_worker_requires_daemon_opt_in(tmp_path: Path) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=False)

    with (
        mock.patch.object(api, "get_runtime_config", return_value=mock.Mock(workspace_root=tmp_path)),
        mock.patch.object(api.core, "_get_db_connection") as mock_conn_factory,
        mock.patch.object(api.core, "_resolve_task_ref", return_value="daemon-10"),
        mock.patch.object(
            api._lanes,
            "_get_lane_row",
            return_value={
                "worktree_path": str(tmp_path / "frontend"),
                "branch": "feature/frontend",
                "title": "Frontend",
                "objective": "Frontend lane",
                "owner_agent": "codex",
                "backend": "codex-subagent",
                "model": None,
                "reasoning_effort": "inherit",
                "status": "active",
                "notes": None,
            },
        ),
        mock.patch.object(api, "manage_worktree_lane") as mock_manage_lane,
        mock.patch.object(api, "worker_start") as mock_worker_start,
    ):
        mock_conn = mock.MagicMock()
        mock_conn_factory.return_value.__enter__.return_value = mock_conn
        payload = _parse(api.dispatch_lane_work(lane_id="frontend", task_ref="daemon-10", start_worker=True))

    assert payload["ok"] is False
    assert "Daemons are opt-in." in payload["error"]
    mock_manage_lane.assert_not_called()
    mock_worker_start.assert_not_called()


def test_non_start_daemon_operations_remain_available_when_disabled(tmp_path: Path) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=False)

    with (
        mock.patch.object(api, "get_runtime_config", return_value=mock.Mock(workspace_root=tmp_path)),
        mock.patch.object(api, "worker_status", return_value={"ok": True, "running": False}) as mock_status,
        mock.patch.object(api, "orchestrator_stop", return_value={"ok": True, "running": False}) as mock_stop,
    ):
        worker_payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="frontend", action="status"))
        orchestrator_payload = _parse(api.manage_orchestrator(operation="stop"))

    assert worker_payload["ok"] is True
    assert orchestrator_payload["ok"] is True
    mock_status.assert_called_once()
    mock_stop.assert_called_once()


def test_daemons_enabled_for_workspace_defaults_false_from_contract(tmp_path: Path) -> None:
    _write_harness_contract(tmp_path, daemons_enabled=False)

    assert api._daemons_enabled_for_workspace(tmp_path) is False


def test_daemons_enabled_for_workspace_uses_local_overlay_override(tmp_path: Path) -> None:
    shared_contract = tmp_path / ".workstate" / "remote" / "docs" / "workstate" / "contracts"
    local_contract = tmp_path / "local" / "docs" / "workstate" / "contracts"
    shared_contract.mkdir(parents=True, exist_ok=True)
    local_contract.mkdir(parents=True, exist_ok=True)
    (shared_contract / "harness-protocol.yaml").write_text(
        "version: 1\norchestrator:\n  daemons:\n    enabled: false\n",
        encoding="utf-8",
    )
    (local_contract / "harness-protocol.yaml").write_text(
        "version: 1\norchestrator:\n  daemons:\n    enabled: true\n",
        encoding="utf-8",
    )
    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_clone_path": ".workstate/remote",
                "remote_sha": "0123456789abcdef0123456789abcdef01234567",
                "surfaces": {
                    "contracts": {
                        "shared_root": ".workstate/remote/docs/workstate/contracts",
                        "local_root": "local/docs/workstate/contracts",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert api._daemons_enabled_for_workspace(tmp_path) is True


def test_orchestrator_start_returns_pid_and_lock_path(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    proc = mock.Mock(pid=43210)
    script_path = REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py"

    with (
        mock.patch.object(api, "_pid_is_running", return_value=False),
        mock.patch.object(
            api,
            "_orchestrator_paths",
            return_value={
                "workspace_root": REPO_ROOT,
                "state_dir": tmp_path / ".task-state",
                "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
                "pause_path": tmp_path / ".task-state" / "daemon-paused",
                "log_dir": tmp_path / "logs" / "daemon",
                "log_path": tmp_path / "logs" / "daemon" / "orchestrator.jsonl",
                "script_path": script_path,
            },
        ),
        mock.patch.object(api, "_import_orchestration_module") as mock_import_module,
        mock.patch.object(api.subprocess, "Popen", return_value=proc) as mock_popen,
    ):
        fake_registry = mock.Mock()
        fake_registry.validate_backend.return_value = "codex-subagent"
        mock_import_module.return_value = fake_registry
        payload = _parse(
            api.manage_orchestrator(
                operation="start",
                task_ref="daemon-8",
                backend="codex-subagent",
                poll_interval=15,
            )
        )

    assert payload["ok"] is True
    assert payload["pid"] == 43210
    assert payload["backend"] == "codex-subagent"
    assert payload["worker_start_mode"] == "mcp"
    assert payload["worker_reasoning_effort"] == "auto"
    assert payload["lock_path"].endswith("/.task-state/orchestrator.lock")
    cmd = mock_popen.call_args.args[0]
    assert "--task-ref" in cmd
    assert "daemon-8" in cmd


def test_orchestrator_status_reports_running_state(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    log_dir = tmp_path / "logs" / "daemon"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "orchestrator.jsonl"
    log_path.write_text(
        json.dumps({"event": "daemon_start", "task_ref": "daemon-8"})
        + "\n"
        + json.dumps({"event": "cycle_end", "task_ref": "daemon-8"})
        + "\n"
    )

    fake_module = mock.Mock()
    fake_module.daemon_status.return_value = {
        "paused": False,
        "lock": {"held": True, "pid": 1234},
        "last_cycle": {"event": "cycle_end"},
        "last_verify": None,
    }

    with (
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_module),
        mock.patch.object(
            api,
            "_orchestrator_paths",
            return_value={
                "workspace_root": tmp_path,
                "state_dir": tmp_path / ".task-state",
                "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
                "pause_path": tmp_path / ".task-state" / "daemon-paused",
                "log_dir": log_dir,
                "log_path": log_path,
                "script_path": REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py",
            },
        ),
        mock.patch.object(api, "_pid_is_running", return_value=True),
    ):
        payload = _parse(api.manage_orchestrator(operation="status"))

    assert payload["ok"] is True
    assert payload["running"] is True
    assert payload["pid"] == 1234
    assert payload["task_ref"] == "daemon-8"
    assert payload["cycle_count"] == 1


def test_worker_start_returns_pid_and_paths(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-subagent"
    fake_lane_manifest = mock.Mock()
    fake_lane_manifest.get_lane_config.return_value = {
        "worktree_path": str(tmp_path / "domain"),
    }
    fake_ctl = mock.Mock()
    fake_ctl.daemon_start.return_value = {"ok": True, "pid": 6789, "lock_path": "/tmp/lock", "log_path": "/tmp/log"}
    (tmp_path / "domain").mkdir()

    def _import(name: str):
        if name == "backend_registry":
            return fake_registry
        if name == "lane_manifest":
            return fake_lane_manifest
        if name == "worker_daemon_ctl":
            return fake_ctl
        raise AssertionError(name)

    with mock.patch.object(api, "_import_orchestration_module", side_effect=_import):
        payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="domain", action="start"))

    assert payload["ok"] is True
    assert payload["pid"] == 6789
    kwargs = fake_ctl.daemon_start.call_args.kwargs
    assert kwargs["task_ref"] == "daemon-10"
    assert kwargs["lane_id"] == "domain"
    assert kwargs["backend"] == "codex-subagent"
    assert kwargs["session_mode"] == "fresh_turn"
    assert kwargs["reasoning_effort"] == "inherit"


def test_worker_start_uses_runtime_state_dir(tmp_path: Path) -> None:
    custom_state_dir = tmp_path / "custom-state"
    api.configure_runtime(
        api.RuntimeConfig.for_workspace(
            tmp_path,
            state_dir=custom_state_dir,
            current_task_path=tmp_path / "CURRENT_TASK.json",
            exports_dir=custom_state_dir / "exports",
        )
    )
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-subagent"
    fake_lane_manifest = mock.Mock()
    fake_lane_manifest.get_lane_config.return_value = {
        "worktree_path": str(tmp_path / "domain"),
    }
    fake_ctl = mock.Mock()
    fake_ctl.daemon_start.return_value = {"ok": True, "pid": 6789}
    (tmp_path / "domain").mkdir()

    def _import(name: str):
        if name == "backend_registry":
            return fake_registry
        if name == "lane_manifest":
            return fake_lane_manifest
        if name == "worker_daemon_ctl":
            return fake_ctl
        raise AssertionError(name)

    with mock.patch.object(api, "_import_orchestration_module", side_effect=_import):
        payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="domain", action="start"))

    assert payload["ok"] is True
    assert fake_ctl.daemon_start.call_args.kwargs["state_dir"] == custom_state_dir


def test_worker_start_passes_shared_lane_session_mode(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-subagent"
    fake_lane_manifest = mock.Mock()
    fake_lane_manifest.get_lane_config.return_value = {
        "worktree_path": str(tmp_path / "frontend"),
    }
    fake_ctl = mock.Mock()
    fake_ctl.daemon_start.return_value = {"ok": True, "pid": 6789}
    (tmp_path / "frontend").mkdir()

    def _import(name: str):
        if name == "backend_registry":
            return fake_registry
        if name == "lane_manifest":
            return fake_lane_manifest
        if name == "worker_daemon_ctl":
            return fake_ctl
        raise AssertionError(name)

    with mock.patch.object(api, "_import_orchestration_module", side_effect=_import):
        payload = _parse(
            api.manage_worker(
                task_ref="daemon-10",
                lane_id="frontend",
                action="start",
                session_mode="shared_lane",
            )
        )

    assert payload["ok"] is True
    assert fake_ctl.daemon_start.call_args.kwargs["session_mode"] == "shared_lane"


def test_worker_start_passes_reasoning_effort(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-subagent"
    fake_lane_manifest = mock.Mock()
    fake_lane_manifest.get_lane_config.return_value = {
        "worktree_path": str(tmp_path / "frontend"),
    }
    fake_ctl = mock.Mock()
    fake_ctl.daemon_start.return_value = {"ok": True, "pid": 6789}
    (tmp_path / "frontend").mkdir()

    def _import(name: str):
        if name == "backend_registry":
            return fake_registry
        if name == "lane_manifest":
            return fake_lane_manifest
        if name == "worker_daemon_ctl":
            return fake_ctl
        raise AssertionError(name)

    with mock.patch.object(api, "_import_orchestration_module", side_effect=_import):
        payload = _parse(
            api.manage_worker(
                task_ref="daemon-10",
                lane_id="frontend",
                action="start",
                reasoning_effort="xhigh",
            )
        )

    assert payload["ok"] is True
    assert fake_ctl.daemon_start.call_args.kwargs["reasoning_effort"] == "xhigh"


def test_worker_status_delegates_to_control_module(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_ctl = mock.Mock()
    fake_ctl.daemon_status.return_value = {"lane_id": "frontend", "process": None}

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_ctl):
        payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="frontend", action="status"))

    assert payload["ok"] is True
    assert payload["running"] is False
    assert payload["lane_id"] == "frontend"
    fake_ctl.daemon_status.assert_called_once()


def test_worker_event_history_delegates_to_control_module(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_ctl = mock.Mock()
    fake_ctl.daemon_event_history.return_value = {
        "lane_id": "frontend",
        "process": None,
        "events": [{"event": "subagent_turn_observed"}],
        "returned": 1,
    }

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_ctl):
        payload = _parse(
            api.manage_worker(
                task_ref="daemon-10",
                lane_id="frontend",
                action="event_history",
                limit=10,
                event_name="subagent_turn_observed",
            )
        )

    assert payload["ok"] is True
    assert payload["returned"] == 1
    assert payload["events"][0]["event"] == "subagent_turn_observed"
    fake_ctl.daemon_event_history.assert_called_once()


def test_worker_stop_delegates_force_flag(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_ctl = mock.Mock()
    fake_ctl.daemon_stop.return_value = {"ok": True, "signaled": [1234]}

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_ctl):
        payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="frontend", action="stop", force=True))

    assert payload["ok"] is True
    assert payload["signaled"] == [1234]
    assert fake_ctl.daemon_stop.call_args.kwargs["force"] is True


def test_worker_start_all_aggregates_lane_results(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_manifest = mock.Mock()
    fake_manifest.merge_order.return_value = ["domain", "frontend"]
    fake_manifest.list_lanes.return_value = ["domain", "frontend"]
    fake_orchestrator_lanes = mock.Mock()
    fake_orchestrator_lanes._lane_has_capacity.return_value = True

    with (
        mock.patch.object(api, "_import_orchestration_module", side_effect=[fake_manifest, fake_orchestrator_lanes]),
        mock.patch.object(
            api,
            "worker_start",
            side_effect=[
                json.dumps({"ok": True, "lane_id": "domain"}),
                json.dumps({"ok": True, "lane_id": "frontend"}),
            ],
        ),
    ):
        payload = _parse(api.manage_worker(task_ref="daemon-10", action="start_all"))

    assert payload["ok"] is True
    assert [item["lane_id"] for item in payload["results"]] == ["domain", "frontend"]
    assert fake_orchestrator_lanes._lane_has_capacity.call_args_list[0].args == ("daemon-10", "domain")


def test_worker_start_all_isolates_per_lane_exceptions(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_manifest = mock.Mock()
    fake_manifest.merge_order.return_value = ["domain", "frontend"]
    fake_manifest.list_lanes.return_value = ["domain", "frontend"]
    fake_orchestrator_lanes = mock.Mock()
    fake_orchestrator_lanes._lane_has_capacity.return_value = True

    with (
        mock.patch.object(api, "_import_orchestration_module", side_effect=[fake_manifest, fake_orchestrator_lanes]),
        mock.patch.object(
            api,
            "worker_start",
            side_effect=[
                json.dumps({"ok": True, "lane_id": "domain"}),
                RuntimeError("boom"),
            ],
        ),
    ):
        payload = _parse(api.manage_worker(task_ref="daemon-10", action="start_all"))

    assert payload["ok"] is False
    assert payload["results"][0]["ok"] is True
    assert payload["results"][1]["ok"] is False
    assert payload["results"][1]["lane_id"] == "frontend"
    assert "boom" in payload["results"][1]["error"]


def test_worker_start_all_skips_lanes_with_unresolved_upstream_dependencies(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_manifest = mock.Mock()
    fake_manifest.merge_order.return_value = ["domain", "frontend", "docs"]
    fake_manifest.list_lanes.return_value = ["domain", "frontend", "docs"]
    fake_orchestrator_lanes = mock.Mock()
    fake_orchestrator_lanes._lane_has_capacity.side_effect = [False, False, True]

    with (
        mock.patch.object(api, "_import_orchestration_module", side_effect=[fake_manifest, fake_orchestrator_lanes]),
        mock.patch.object(
            api, "worker_start", return_value=json.dumps({"ok": True, "lane_id": "domain"})
        ) as mock_worker_start,
    ):
        payload = _parse(api.manage_worker(task_ref="daemon-10", action="start_all", session_mode="shared_lane"))

    assert payload["ok"] is True
    assert payload["session_mode"] == "shared_lane"
    assert payload["results"][0] == {"ok": True, "lane_id": "domain"}
    assert payload["results"][1]["skipped"] is True
    assert payload["results"][1]["reason"] == "unresolved_upstream_dependencies"
    assert payload["results"][1]["blocked_by"] == ["domain"]
    assert payload["results"][2]["skipped"] is True
    assert payload["results"][2]["blocked_by"] == ["domain"]
    mock_worker_start.assert_called_once_with(
        task_ref="daemon-10",
        lane_id="domain",
        backend="codex-subagent",
        poll_interval=30,
        single_pass=False,
        session_mode="shared_lane",
        reasoning_effort="inherit",
        model=None,
    )


def test_worker_resume_delegates_to_control_module(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_ctl = mock.Mock()
    fake_ctl.daemon_resume.return_value = {"ok": True, "signaled": [2222]}

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_ctl):
        payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="frontend", action="resume"))

    assert payload["ok"] is True
    assert payload["signaled"] == [2222]
    fake_ctl.daemon_resume.assert_called_once()


def test_manage_worker_requires_lane_id_for_lane_actions(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)

    payload = _parse(api.manage_worker(task_ref="daemon-10", action="status"))

    assert payload["ok"] is False
    assert "requires lane_id" in payload["error"]


def test_manage_worker_rejects_unknown_action(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)

    payload = _parse(api.manage_worker(task_ref="daemon-10", lane_id="frontend", action="dance"))

    assert payload["ok"] is False
    assert "Valid values: start, stop, resume, status, event_history, start_all." in payload["error"]


def test_orchestrator_pause_and_resume_delegate_to_daemon_module(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_module = mock.Mock()

    with (
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_module),
        mock.patch.object(
            api,
            "_orchestrator_paths",
            return_value={
                "workspace_root": tmp_path,
                "state_dir": tmp_path / ".task-state",
                "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
                "pause_path": tmp_path / ".task-state" / "daemon-paused",
                "log_dir": tmp_path / "logs" / "daemon",
                "log_path": tmp_path / "logs" / "daemon" / "orchestrator.jsonl",
                "script_path": REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py",
            },
        ),
    ):
        paused = _parse(api.manage_orchestrator(operation="pause"))
        resumed = _parse(api.manage_orchestrator(operation="resume"))

    assert paused["ok"] is True
    assert paused["paused"] is True
    assert resumed["ok"] is True
    assert resumed["paused"] is False
    fake_module.daemon_pause.assert_called_once()
    fake_module.daemon_resume.assert_called_once()


def test_orchestrator_stop_returns_not_running_when_no_pid(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)

    with (
        mock.patch.object(
            api,
            "_orchestrator_paths",
            return_value={
                "workspace_root": tmp_path,
                "state_dir": tmp_path / ".task-state",
                "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
                "pause_path": tmp_path / ".task-state" / "daemon-paused",
                "log_dir": tmp_path / "logs" / "daemon",
                "log_path": tmp_path / "logs" / "daemon" / "orchestrator.jsonl",
                "script_path": REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py",
            },
        ),
        mock.patch.object(api, "_read_lock_pid", return_value=None),
    ):
        payload = _parse(api.manage_orchestrator(operation="stop"))

    assert payload["ok"] is True
    assert payload["running"] is False
    assert payload["pid"] is None


def test_run_structured_turn_rejects_cli_backend(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-cli"
    fake_registry.get_backend_spec.return_value = mock.Mock(kind="cli")

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="codex-cli",
            )
        )

    assert payload["ok"] is False
    assert "CLI backends are not supported" in payload["error"]


def test_orchestrator_start_rejects_invalid_backend_before_spawn(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_registry = mock.Mock()
    fake_registry.validate_backend.side_effect = RuntimeError("Unsupported execution backend 'bad'")

    with (
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry),
        mock.patch.object(api.subprocess, "Popen") as mock_popen,
    ):
        payload = _parse(api.manage_orchestrator(operation="start", task_ref="daemon-8", backend="bad"))

    assert payload["ok"] is False
    assert "Unsupported execution backend 'bad'" in payload["error"]
    mock_popen.assert_not_called()


def test_run_structured_turn_returns_bridge_payload(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    runner = mock.Mock(return_value={"summary": "ok"})
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-subagent"
    fake_registry.get_backend_spec.return_value = mock.Mock(kind="bridge")
    fake_registry.resolve_bridge.return_value = runner

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="codex-subagent",
                env={"TMPDIR": "/tmp/test"},
            )
        )

    assert payload["ok"] is True
    assert payload["backend"] == "codex-subagent"
    assert payload["result"] == {"summary": "ok"}
    assert runner.call_args.kwargs["env"] == {"TMPDIR": "/tmp/test"}


def test_run_structured_turn_times_out(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-subagent"
    fake_registry.get_backend_spec.return_value = mock.Mock(kind="bridge")
    fake_registry.resolve_bridge.return_value = lambda **_: {"summary": "slow"}

    class FakeFuture:
        def result(self, timeout: float) -> dict:
            raise api.concurrent.futures.TimeoutError()

    class FakeExecutor:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def submit(self, fn):
            return FakeFuture()

    with (
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry),
        mock.patch.object(api.concurrent.futures, "ThreadPoolExecutor", return_value=FakeExecutor()),
    ):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="codex-subagent",
                timeout_seconds=0.1,
            )
        )

    assert payload["ok"] is False
    assert "timed out" in payload["error"]


def test_orchestrator_single_cycle_returns_exit_code(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    script_path = REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py"
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-cli"
    completed = mock.Mock(returncode=0, stderr="")

    with (
        mock.patch.object(
            api,
            "_orchestrator_paths",
            return_value={
                "workspace_root": REPO_ROOT,
                "state_dir": tmp_path / ".task-state",
                "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
                "pause_path": tmp_path / ".task-state" / "daemon-paused",
                "log_dir": tmp_path / "logs" / "daemon",
                "log_path": tmp_path / "logs" / "daemon" / "orchestrator.jsonl",
                "script_path": script_path,
            },
        ),
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry),
        mock.patch.object(api.subprocess, "run", return_value=completed) as mock_run,
    ):
        payload = _parse(
            api.manage_orchestrator(
                operation="single_cycle",
                task_ref="daemon-8",
                backend="codex-cli",
                dry_run=True,
            )
        )

    assert payload["ok"] is True
    assert payload["exit_code"] == 0
    assert payload["backend"] == "codex-cli"
    assert payload["dry_run"] is True
    assert payload["worker_start_mode"] == "mcp"
    cmd = mock_run.call_args.args[0]
    assert "--single-pass" in cmd
    assert "--dry-run" in cmd
    assert "--backend" in cmd


def test_orchestrator_single_cycle_reports_failure(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    script_path = REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py"
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-cli"
    completed = mock.Mock(returncode=1, stderr="some error")

    with (
        mock.patch.object(
            api,
            "_orchestrator_paths",
            return_value={
                "workspace_root": REPO_ROOT,
                "state_dir": tmp_path / ".task-state",
                "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
                "pause_path": tmp_path / ".task-state" / "daemon-paused",
                "log_dir": tmp_path / "logs" / "daemon",
                "log_path": tmp_path / "logs" / "daemon" / "orchestrator.jsonl",
                "script_path": script_path,
            },
        ),
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry),
        mock.patch.object(api.subprocess, "run", return_value=completed),
    ):
        payload = _parse(api.manage_orchestrator(operation="single_cycle", task_ref="daemon-8"))

    assert payload["ok"] is False
    assert payload["exit_code"] == 1
    assert "some error" in payload["stderr"]


def test_orchestrator_single_cycle_handles_timeout(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    script_path = REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py"
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-cli"

    with (
        mock.patch.object(
            api,
            "_orchestrator_paths",
            return_value={
                "workspace_root": REPO_ROOT,
                "state_dir": tmp_path / ".task-state",
                "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
                "pause_path": tmp_path / ".task-state" / "daemon-paused",
                "log_dir": tmp_path / "logs" / "daemon",
                "log_path": tmp_path / "logs" / "daemon" / "orchestrator.jsonl",
                "script_path": script_path,
            },
        ),
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry),
        mock.patch.object(api.subprocess, "run", side_effect=api.subprocess.TimeoutExpired(cmd=[], timeout=1)),
    ):
        payload = _parse(
            api.manage_orchestrator(
                operation="single_cycle",
                task_ref="daemon-8",
                timeout_seconds=1.0,
            )
        )

    assert payload["ok"] is False
    assert "timed out" in payload["error"]


def test_runtime_pythonpath_only_requires_local_bridge_source() -> None:
    parts = api._runtime_pythonpath().split(":")
    expected_bridge = str(REPO_ROOT / "packages" / "workstate-codex-bridge" / "src")

    assert expected_bridge in parts
    assert Path(expected_bridge).exists()
    assert str(REPO_ROOT / "packages" / "workstate-handoff-mcp" / "src") not in parts
    assert str(REPO_ROOT / "packages" / "workstate-orchestrator-mcp" / "src") not in parts
    assert all("/packages/packages/" not in part for part in parts)


def test_daemon_runtime_env_does_not_inject_handoff_source_path(monkeypatch) -> None:
    monkeypatch.setenv("PYTHONPATH", "/existing/pythonpath")
    env = api._daemon_runtime_env()

    assert str(REPO_ROOT / "packages" / "workstate-codex-bridge" / "src") in env["PYTHONPATH"]
    assert "/existing/pythonpath" in env["PYTHONPATH"]
    assert str(REPO_ROOT / "packages" / "workstate-handoff-mcp" / "src") not in env["PYTHONPATH"]
    assert str(REPO_ROOT / "packages" / "workstate-orchestrator-mcp" / "src") not in env["PYTHONPATH"]


def _mock_orchestrator_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "workspace_root": REPO_ROOT,
        "state_dir": tmp_path / ".task-state",
        "lock_path": tmp_path / ".task-state" / "orchestrator.lock",
        "pause_path": tmp_path / ".task-state" / "daemon-paused",
        "log_dir": tmp_path / "logs" / "daemon",
        "log_path": tmp_path / "logs" / "daemon" / "orchestrator.jsonl",
        "script_path": REPO_ROOT / "scripts" / "mcp" / "orchestrator_daemon.py",
    }


def test_e2e_orchestrator_lifecycle_through_mcp_tools(tmp_path: Path) -> None:
    """Opus-style e2e: start -> status -> pause -> resume -> single-cycle -> stop."""
    _configure_runtime(tmp_path)
    paths = _mock_orchestrator_paths(tmp_path)

    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "codex-cli"
    fake_registry.get_backend_spec.return_value = mock.Mock(kind="cli")

    fake_daemon = mock.Mock()
    fake_daemon.daemon_status.return_value = {
        "paused": False,
        "lock": {"held": True, "pid": 99999},
        "last_cycle": {"event": "cycle_end", "task_ref": "e2e-test"},
        "last_verify": None,
    }

    def import_selector(name: str):
        if name == "backend_registry":
            return fake_registry
        if name == "orchestrator_daemon":
            return fake_daemon
        raise ImportError(name)

    proc_mock = mock.Mock(pid=99999)

    with (
        mock.patch.object(api, "_orchestrator_paths", return_value=paths),
        mock.patch.object(api, "_import_orchestration_module", side_effect=import_selector),
        mock.patch.object(api, "_pid_is_running", return_value=False),
        mock.patch.object(api.subprocess, "Popen", return_value=proc_mock),
    ):
        # Step 1: Start
        started = _parse(api.manage_orchestrator(operation="start", task_ref="e2e-test", backend="codex-cli"))
        assert started["ok"] is True
        assert started["pid"] == 99999

    # Step 2: Status (daemon now "running")
    with (
        mock.patch.object(api, "_orchestrator_paths", return_value=paths),
        mock.patch.object(api, "_import_orchestration_module", side_effect=import_selector),
        mock.patch.object(api, "_pid_is_running", return_value=True),
    ):
        status = _parse(api.manage_orchestrator(operation="status"))
        assert status["ok"] is True
        assert status["running"] is True

    # Step 3: Pause
    with (
        mock.patch.object(api, "_orchestrator_paths", return_value=paths),
        mock.patch.object(api, "_import_orchestration_module", side_effect=import_selector),
    ):
        paused = _parse(api.manage_orchestrator(operation="pause"))
        assert paused["ok"] is True
        assert paused["paused"] is True

    # Step 4: Resume
    with (
        mock.patch.object(api, "_orchestrator_paths", return_value=paths),
        mock.patch.object(api, "_import_orchestration_module", side_effect=import_selector),
    ):
        resumed = _parse(api.manage_orchestrator(operation="resume"))
        assert resumed["ok"] is True
        assert resumed["paused"] is False

    # Step 5: Single cycle
    completed = mock.Mock(returncode=0, stderr="")
    with (
        mock.patch.object(api, "_orchestrator_paths", return_value=paths),
        mock.patch.object(api, "_import_orchestration_module", side_effect=import_selector),
        mock.patch.object(api.subprocess, "run", return_value=completed),
    ):
        cycle = _parse(
            api.manage_orchestrator(
                operation="single_cycle",
                task_ref="e2e-test",
                backend="codex-cli",
                dry_run=True,
            )
        )
        assert cycle["ok"] is True
        assert cycle["exit_code"] == 0

    # Step 6: Stop
    with (
        mock.patch.object(api, "_orchestrator_paths", return_value=paths),
        mock.patch.object(api, "_read_lock_pid", return_value=99999),
        mock.patch.object(api, "_pid_is_running", side_effect=[True, False]),
        mock.patch("os.kill") as mock_kill,
    ):
        stopped = _parse(api.manage_orchestrator(operation="stop"))
        assert stopped["ok"] is True
        assert stopped["running"] is False
        mock_kill.assert_called_once()


# --- WORKSTATE-REF-5 implementation note: in-process backend dispatch via adapter runner seam ---


def _fake_in_process_registry(adapter: mock.Mock) -> mock.Mock:
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "structured-turn"
    fake_registry.get_backend_spec.return_value = mock.Mock(kind="in-process")
    fake_registry.get_adapter.return_value = adapter
    return fake_registry


def _fake_adapter(runner: mock.Mock, *, emits_envelope: bool) -> mock.Mock:
    """Mock adapter mirroring StructuredTurnAdapter's dispatch surface.

    ``runner_emits_envelope`` must be set explicitly: real adapters report
    True only for their composed default runner, and dispatch keys envelope
    unwrapping off this flag (provenance, not payload-shape sniffing).
    """
    adapter = mock.Mock()
    adapter.resolve_runner.return_value = runner
    adapter.runner_emits_envelope = emits_envelope
    return adapter


def test_run_structured_turn_dispatches_in_process_via_runner_seam(tmp_path: Path) -> None:
    """In-process backends dispatch through the adapter runner seam, preserving arbitrary schemas verbatim."""
    _configure_runtime(tmp_path)
    arbitrary = {"verdict": "real", "confidence": 0.9, "notes": ["a", "b"]}
    runner = mock.Mock(return_value=dict(arbitrary))
    adapter = _fake_adapter(runner, emits_envelope=False)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
                env={"TMPDIR": "/tmp/test"},
                timeout_seconds=42.0,
            )
        )

    assert payload["ok"] is True
    assert payload["backend"] == "structured-turn"
    assert payload["result"] == arbitrary
    fake_registry.resolve_bridge.assert_not_called()
    # outer timeout owns the turn and is threaded into the adapter composition
    assert fake_registry.get_adapter.call_args.kwargs["timeout_seconds"] == 42.0
    assert runner.call_args.kwargs["env"] == {"TMPDIR": "/tmp/test"}


def test_run_structured_turn_in_process_unwraps_downstream_envelope(tmp_path: Path) -> None:
    """A downstream run_structured_turn envelope is unwrapped; backend reports the in-process name."""
    _configure_runtime(tmp_path)
    runner = mock.Mock(return_value={"ok": True, "backend": "codex-subagent", "result": {"a": 1}})
    adapter = _fake_adapter(runner, emits_envelope=True)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is True
    assert payload["backend"] == "structured-turn"
    assert payload["result"] == {"a": 1}


def test_run_structured_turn_in_process_propagates_downstream_error(tmp_path: Path) -> None:
    """Downstream failure surfaces as ok:false naming the downstream backend, never the bridge-runner contract error."""
    _configure_runtime(tmp_path)
    runner = mock.Mock(
        return_value={
            "ok": False,
            "error": "Bridge module 'workstate_codex_bridge' is not importable in this runtime.",
            "backend": "codex-subagent",
        }
    )
    adapter = _fake_adapter(runner, emits_envelope=True)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is False
    assert "workstate_codex_bridge" in payload["error"]
    assert payload["backend"] == "structured-turn"
    assert payload["downstream_backend"] == "codex-subagent"
    assert "does not expose a bridge runner" not in payload["error"]


def test_run_structured_turn_in_process_uses_single_timeout_layer(tmp_path: Path) -> None:
    """No second executor wraps the in-process call — the downstream composition owns the timeout."""
    _configure_runtime(tmp_path)
    runner = mock.Mock(return_value={"x": 1})
    adapter = _fake_adapter(runner, emits_envelope=False)
    fake_registry = _fake_in_process_registry(adapter)

    with (
        mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry),
        mock.patch.object(api.concurrent.futures, "ThreadPoolExecutor") as mock_executor,
    ):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is True
    mock_executor.assert_not_called()


def test_run_structured_turn_in_process_guard_error_is_clean(tmp_path: Path) -> None:
    """Adapter-level configuration errors (e.g. recursion guard) return a clean ok:false envelope."""
    _configure_runtime(tmp_path)
    adapter = mock.Mock()
    adapter.resolve_runner.side_effect = RuntimeError(
        "StructuredTurnAdapter downstream backend 'structured-turn' is in-process; refusing recursive composition."
    )
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is False
    assert "refusing recursive composition" in payload["error"]
    assert payload["backend"] == "structured-turn"


# --- WORKSTATE-REF-5 review fixes: provenance-based unwrap, guard breadth, env retry,
# --- string-payload contract, downstream-owned timeout enforcement ---


def test_run_structured_turn_in_process_verbatim_payload_never_unwrapped(tmp_path: Path) -> None:
    """A caller schema that looks like the downstream envelope passes through verbatim (injected runner)."""
    _configure_runtime(tmp_path)
    lookalike = {"ok": True, "result": {"inner": 1}, "extra": "must-survive"}
    runner = mock.Mock(return_value=dict(lookalike))
    adapter = _fake_adapter(runner, emits_envelope=False)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is True
    assert payload["result"] == lookalike


def test_run_structured_turn_in_process_unexpected_envelope_is_error(tmp_path: Path) -> None:
    """When the default runner promises an envelope, a non-envelope payload is a clean error, not a guess."""
    _configure_runtime(tmp_path)
    runner = mock.Mock(return_value={"weird": 1})
    adapter = _fake_adapter(runner, emits_envelope=True)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is False
    assert "unexpected envelope shape" in payload["error"]
    assert payload["backend"] == "structured-turn"


def test_run_structured_turn_in_process_parses_string_payload(tmp_path: Path) -> None:
    """Runners may return a JSON string; it is decoded before the envelope decision."""
    _configure_runtime(tmp_path)
    runner = mock.Mock(return_value='{"a": 1}')
    adapter = _fake_adapter(runner, emits_envelope=False)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is True
    assert payload["result"] == {"a": 1}


def test_run_structured_turn_in_process_invalid_json_string_is_clean_error(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    runner = mock.Mock(return_value="not json {")
    adapter = _fake_adapter(runner, emits_envelope=False)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is False
    assert "invalid JSON" in payload["error"]
    assert payload["backend"] == "structured-turn"


def test_run_structured_turn_in_process_non_object_payload_is_clean_error(tmp_path: Path) -> None:
    _configure_runtime(tmp_path)
    runner = mock.Mock(return_value="[1, 2]")
    adapter = _fake_adapter(runner, emits_envelope=False)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is False
    assert "non-object payload" in payload["error"]
    assert payload["backend"] == "structured-turn"


def test_run_structured_turn_in_process_adapter_import_error_is_clean(tmp_path: Path) -> None:
    """A broken adapter_path (ImportError/AttributeError) maps to the clean ok:false envelope, mirroring resolve_bridge."""
    _configure_runtime(tmp_path)
    fake_registry = mock.Mock()
    fake_registry.validate_backend.return_value = "structured-turn"
    fake_registry.get_backend_spec.return_value = mock.Mock(kind="in-process")
    fake_registry.get_adapter.side_effect = ImportError("No module named 'missing_adapter_module'")

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
            )
        )

    assert payload["ok"] is False
    assert "missing_adapter_module" in payload["error"]
    assert payload["backend"] == "structured-turn"


def test_run_structured_turn_in_process_retries_without_env_kwarg(tmp_path: Path) -> None:
    """Runner-signature tolerance parity with the bridge path: retry once without env on TypeError."""
    _configure_runtime(tmp_path)
    runner = mock.Mock(side_effect=[TypeError("got an unexpected keyword argument 'env'"), {"a": 1}])
    adapter = _fake_adapter(runner, emits_envelope=False)
    fake_registry = _fake_in_process_registry(adapter)

    with mock.patch.object(api, "_import_orchestration_module", return_value=fake_registry):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
                env={"TMPDIR": "/tmp/test"},
            )
        )

    assert payload["ok"] is True
    assert payload["result"] == {"a": 1}
    assert runner.call_count == 2
    assert "env" not in runner.call_args.kwargs


def test_run_structured_turn_in_process_timeout_enforced_by_downstream_layer(tmp_path: Path) -> None:
    """End-to-end single-timeout proof: the downstream bridge executor enforces the deadline
    and the in-process layer surfaces it naming both backends — no unbounded hang."""
    _configure_runtime(tmp_path)
    registry = api._import_orchestration_module("backend_registry")

    def slow_runner(**kwargs: object) -> dict:
        time.sleep(1.0)
        return {"never": "returned"}

    with mock.patch.object(registry, "resolve_bridge", return_value=slow_runner):
        payload = _parse(
            api.run_structured_turn(
                prompt="hello",
                schema={"type": "object"},
                cwd=str(REPO_ROOT),
                backend="structured-turn",
                timeout_seconds=0.2,
            )
        )

    assert payload["ok"] is False
    assert "timed out after 0.2 seconds" in payload["error"]
    assert payload["backend"] == "structured-turn"
    assert payload["downstream_backend"] == "codex-subagent"
