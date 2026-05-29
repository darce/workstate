from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest import mock

import pytest

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import HANDOFF_SCHEMA_VERSION


def test_init_state_bootstraps_handoff_db_and_exports_dir(tmp_path: Path) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)

    result = mcp_server.init_state(runtime)

    assert result["ok"] is True
    assert result["state_dir"] == str(runtime.state_dir)
    assert result["exports_dir"] == str(runtime.exports_dir)
    assert result["db_path"] == str(runtime.db_path)
    assert result["state_dir_created"] is True
    assert result["exports_dir_created"] is True
    assert result["db_created"] is True
    assert result["schema_version"] == HANDOFF_SCHEMA_VERSION
    assert runtime.state_dir.is_dir()
    assert runtime.exports_dir.is_dir()
    assert runtime.db_path.is_file()

    with sqlite3.connect(runtime.db_path) as conn:
        user_version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert user_version == HANDOFF_SCHEMA_VERSION


def test_init_state_rejects_existing_db_when_adjacent_manifest_is_invalid(tmp_path: Path) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)
    runtime.state_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(runtime.db_path):
        pass

    (tmp_path / ".workstate-overlay.json").write_text(json.dumps({"remote_url": "git@example.com:demo/repo.git"}))

    with pytest.raises(RuntimeError, match="force-reuse-state"):
        mcp_server.init_state(runtime)


def test_init_state_reuses_existing_db_when_adjacent_manifest_is_valid(tmp_path: Path) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)
    runtime.state_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(runtime.db_path):
        pass

    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_url": "git@example.com:demo/repo.git",
                "remote_ref": "main",
                "remote_sha": "a" * 40,
                "surfaces": [],
                "configs": [],
            }
        )
    )

    result = mcp_server.init_state(runtime)

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["db_created"] is False


def test_init_state_check_mode_allows_existing_db_without_manifest(tmp_path: Path) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)
    runtime.state_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("PRAGMA user_version = 7")

    result = mcp_server.init_state(runtime, check=True)

    assert result["ok"] is True
    assert result["initialized"] is True
    assert result["state_dir_created"] is False
    assert result["exports_dir_created"] is False
    assert result["db_created"] is False
    assert result["schema_version"] == 7


def test_init_state_is_idempotent_on_second_run(tmp_path: Path) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)

    first = mcp_server.init_state(runtime)
    second = mcp_server.init_state(runtime, force_reuse_state=True)

    assert first["ok"] is True
    assert second["ok"] is True
    assert second["initialized"] is True
    assert second["state_dir_created"] is False
    assert second["exports_dir_created"] is False
    assert second["db_created"] is False
    assert second["schema_version"] == HANDOFF_SCHEMA_VERSION


def test_init_state_surfaces_unexpected_manifest_validation_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)
    runtime.state_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(runtime.db_path):
        pass

    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_url": "git@example.com:demo/repo.git",
                "remote_ref": "main",
                "remote_sha": "a" * 40,
                "surfaces": [],
                "configs": [],
            }
        )
    )

    def blow_up(payload: object) -> object:
        raise RuntimeError("manifest validator blew up")

    monkeypatch.setattr("workstate_handoff_mcp.state_init.BootstrapManifest.model_validate", blow_up)

    with pytest.raises(RuntimeError, match="manifest validator blew up"):
        mcp_server.init_state(runtime)


def test_init_state_rejects_manifest_when_expected_remote_url_mismatches(tmp_path: Path) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)
    runtime.state_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(runtime.db_path):
        pass

    (tmp_path / ".workstate-overlay.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "remote_url": "git@example.com:demo/repo.git",
                "remote_ref": "main",
                "remote_sha": "a" * 40,
                "surfaces": [],
                "configs": [],
            }
        )
    )

    with pytest.raises(RuntimeError, match="remote_url"):
        mcp_server.init_state(
            runtime,
            expected_remote_url="git@example.com:other/repo.git",
        )


def test_init_state_reports_v6_to_current_migration_summary(tmp_path: Path) -> None:
    runtime = RuntimeConfig.for_workspace(tmp_path)
    mcp_server.init_state(runtime)

    with sqlite3.connect(runtime.db_path) as conn:
        conn.execute("ALTER TABLE handoff_state DROP COLUMN task_plan_path")
        conn.execute("PRAGMA user_version = 6")
        conn.commit()

    result = mcp_server.init_state(runtime, force_reuse_state=True)

    assert result["ok"] is True
    assert result["schema_version"] == HANDOFF_SCHEMA_VERSION
    assert result["migrated_from"] == 6
    assert result["migrated_to"] == HANDOFF_SCHEMA_VERSION


def test_init_state_does_not_render_current_task_when_auto_regen_enabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGENT_HANDOFF_CURRENT_TASK_AUTO_REGEN", "1")
    runtime = RuntimeConfig.for_workspace(tmp_path)

    with mock.patch("workstate_handoff_mcp.current_task_rendering._write_current_task_md_for_task") as mocked_render:
        result = mcp_server.init_state(runtime)

    assert result["ok"] is True
    mocked_render.assert_not_called()
    assert not runtime.current_task_path.exists()
