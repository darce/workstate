"""Doctor informational note for unharvested agent_errors rows (implementation note implementation note).

Plan-review decision 3: doctor surfaces "N unharvested error rows, top
class X" as an informational note only — ``severity=info``, listed with
the ``note`` prefix, never affecting the exit code (implementation note
informational-output conventions). A missing DB, a DB without the
``agent_errors`` table (pre-v12 schema), or an empty table emits
nothing.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from workstate_bootstrap.install import BOOTSTRAP_MANIFEST_NAME, SCHEMA_VERSION
from workstate_bootstrap.subcommands import doctor

subcommands_mod = importlib.import_module("workstate_bootstrap.subcommands")


def _quiet_package_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the installed-version seam to the manifest version so implementation note's
    ``package_drift`` check stays quiet — these tests pin the note path only."""

    def fake_version(distribution: str) -> str:
        if distribution == "workstate-system":
            return "0.1.23"
        raise subcommands_mod.importlib_metadata.PackageNotFoundError(distribution)

    monkeypatch.setattr(subcommands_mod.importlib_metadata, "version", fake_version)


def _seed_manifest(target: Path) -> None:
    """Minimal package-source manifest: no clone, no surfaces, no configs."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source_kind": "package",
        "package_version": "0.1.23",
        "remote_url": "file:///tmp/fake.git",
        "remote_ref": "v0.1.23",
        "remote_sha": "0" * 40,
        "surfaces": [],
        "configs": [],
        "mcp_servers": [],
    }
    (target / BOOTSTRAP_MANIFEST_NAME).write_text(json.dumps(payload, indent=2) + "\n")


def _seed_agent_errors_db(target: Path, rows: list[tuple[str, str]]) -> Path:
    """A handoff.db with just the tables the note check reads."""
    state_dir = target / ".task-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    db_path = state_dir / "handoff.db"
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE agent_errors ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT,"
                "repo_instance_id TEXT NOT NULL,"
                "error_class TEXT NOT NULL,"
                "summary TEXT NOT NULL,"
                "occurrence_count INTEGER NOT NULL DEFAULT 1,"
                "created_at TEXT NOT NULL DEFAULT (datetime('now')),"
                "last_seen_at TEXT NOT NULL DEFAULT (datetime('now')))"
            )
            for error_class, summary in rows:
                conn.execute(
                    "INSERT INTO agent_errors (repo_instance_id, error_class, summary)"
                    " VALUES ('repo-1', ?, ?)",
                    (error_class, summary),
                )
    finally:
        conn.close()
    return db_path


def test_doctor_notes_unharvested_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_package_drift(monkeypatch)
    _seed_manifest(tmp_path)
    _seed_agent_errors_db(
        tmp_path,
        [
            ("install_drift", "ImportError: cannot import name 'list_handoff_rows'"),
            ("install_drift", "ImportError: cannot import name 'render_handoff'"),
            ("cli_failure", "make task-start exited 2"),
        ],
    )

    findings = doctor(target=tmp_path)

    notes = [f for f in findings if f["kind"] == "unharvested_agent_errors"]
    assert len(notes) == 1
    note = notes[0]
    assert note["severity"] == "info"
    assert note["path"] == ".task-state/handoff.db"
    assert "3 unharvested error rows" in note["detail"]
    assert "top class install_drift" in note["detail"]


def test_doctor_no_note_when_table_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_package_drift(monkeypatch)
    _seed_manifest(tmp_path)
    _seed_agent_errors_db(tmp_path, [])

    findings = doctor(target=tmp_path)
    assert not [f for f in findings if f["kind"] == "unharvested_agent_errors"]


def test_doctor_no_note_for_pre_v12_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_package_drift(monkeypatch)
    _seed_manifest(tmp_path)
    state_dir = tmp_path / ".task-state"
    state_dir.mkdir(parents=True)
    # Empty file: sqlite opens it fine but there is no agent_errors table.
    (state_dir / "handoff.db").write_bytes(b"")

    findings = doctor(target=tmp_path)
    assert not [f for f in findings if f["kind"] == "unharvested_agent_errors"]


def test_doctor_no_note_when_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _quiet_package_drift(monkeypatch)
    _seed_manifest(tmp_path)

    findings = doctor(target=tmp_path)
    assert not [f for f in findings if f["kind"] == "unharvested_agent_errors"]


def test_cli_doctor_note_keeps_exit_zero(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from workstate_bootstrap.cli import main

    _quiet_package_drift(monkeypatch)
    _seed_manifest(tmp_path)
    _seed_agent_errors_db(
        tmp_path, [("install_drift", "ImportError: cannot import name 'close_slice'")]
    )

    rc = main(["doctor", "--target", str(tmp_path)])

    out = capsys.readouterr().out
    assert "note unharvested_agent_errors: .task-state/handoff.db" in out
    assert "1 unharvested error rows, top class install_drift" in out
    assert rc == 0, out
