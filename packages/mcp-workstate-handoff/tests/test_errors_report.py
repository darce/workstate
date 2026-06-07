"""implementation note (implementation note / WS-ERRTEL-01): errors-report + errors-export harvest.

``mcp-workstate-handoff errors-report`` clusters ``agent_errors`` rows by
``(error_class, package_name)`` with a package-version range per cluster,
emitting counts, first/last seen, and a representative sample. Two modes:

- local: no sources -> the primary repo's ``.task-state/handoff.db``
  resolved via the git common dir (same path as ``errors-record``)
- collect: operator passes N handoff.db paths or ``errors-export``
  JSONL bundles; rows merge with ``(repo_instance_id, id)`` dedup so a
  DB and its own export do not double-count

``errors-export --since <ts>`` emits the redacted rows as a JSONL
bundle a consumer can hand the maintainer; report over the bundle must
equal report over the source DB (round-trip).
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from workstate_handoff_mcp import api as mcp_server
from workstate_handoff_mcp.agent_errors_report import (
    _version_range,
    build_errors_report,
    errors_export,
    errors_report,
)
from workstate_handoff_mcp.config import RuntimeConfig
from workstate_handoff_mcp.shared_schema import _get_db_connection


def _seed_db(repo: Path) -> Path:
    """A git repo with a bootstrapped .task-state/handoff.db at current schema."""
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    runtime = RuntimeConfig.for_workspace(
        repo,
        state_dir=repo / ".task-state",
        current_task_path=repo / "CURRENT_TASK.json",
    )
    mcp_server.configure_runtime(runtime)
    with _get_db_connection():
        pass  # bootstrap schema
    return repo / ".task-state" / "handoff.db"


def _insert_error(
    db_path: Path,
    *,
    error_class: str,
    summary: str,
    repo_instance_id: str = "repo-1",
    task_ref: str | None = None,
    detail: str | None = None,
    tool_name: str | None = None,
    command_preview: str | None = None,
    package_name: str | None = None,
    package_version: str | None = None,
    workstate_release: str | None = None,
    harness: str = "hook",
    occurrence_count: int = 1,
    created_at: str = "2026-06-01 10:00:00",
    last_seen_at: str | None = None,
) -> None:
    conn = sqlite3.connect(db_path)
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO repo_instances ("
                "repo_instance_id, workspace_root, git_common_dir, created_at, last_seen_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    repo_instance_id,
                    f"/tmp/{repo_instance_id}",
                    f"/tmp/{repo_instance_id}/.git",
                    created_at,
                    created_at,
                ),
            )
            conn.execute(
                "INSERT INTO agent_errors ("
                "repo_instance_id, task_ref, harness, error_class, summary, detail,"
                " tool_name, command_preview, package_name, package_version,"
                " workstate_release, occurrence_count, created_at, last_seen_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    repo_instance_id,
                    task_ref,
                    harness,
                    error_class,
                    summary,
                    detail,
                    tool_name,
                    command_preview,
                    package_name,
                    package_version,
                    workstate_release,
                    occurrence_count,
                    created_at,
                    last_seen_at or created_at,
                ),
            )
    finally:
        conn.close()


def _cluster_index(report: dict) -> dict[tuple, dict]:
    return {(c["error_class"], c["package_name"]): c for c in report["clusters"]}


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_report_clusters_by_class_and_package(tmp_path: Path) -> None:
    db = _seed_db(tmp_path / "repo")
    _insert_error(
        db,
        error_class="install_drift",
        summary="ImportError: cannot import name 'list_handoff_rows'",
        package_name="workstate_handoff_mcp",
        package_version="0.4.0",
        occurrence_count=2,
        created_at="2026-06-01 10:00:00",
        last_seen_at="2026-06-02 10:00:00",
    )
    _insert_error(
        db,
        error_class="install_drift",
        summary="ImportError: cannot import name 'close_slice'",
        package_name="workstate_handoff_mcp",
        package_version="0.6.0",
        created_at="2026-06-03 10:00:00",
    )
    _insert_error(db, error_class="cli_failure", summary="make task-start exited 2")

    result = errors_report(sources=[db])
    assert result["ok"] is True
    report = result["report"]
    assert report["total_rows"] == 3
    assert report["total_occurrences"] == 4

    clusters = _cluster_index(report)
    assert set(clusters) == {
        ("install_drift", "workstate_handoff_mcp"),
        ("cli_failure", None),
    }
    drift = clusters[("install_drift", "workstate_handoff_mcp")]
    assert drift["row_count"] == 2
    assert drift["occurrence_count"] == 3
    assert drift["package_version_range"] == ["0.4.0", "0.6.0"]
    assert drift["first_seen"] == "2026-06-01 10:00:00"
    assert drift["last_seen"] == "2026-06-03 10:00:00"
    # Representative sample: highest occurrence_count wins.
    assert drift["sample"]["summary"] == "ImportError: cannot import name 'list_handoff_rows'"

    # Busiest cluster first.
    assert report["clusters"][0]["error_class"] == "install_drift"


def test_collect_merges_dbs_and_counts_repo_instances(tmp_path: Path) -> None:
    db_a = _seed_db(tmp_path / "repo-a")
    db_b = _seed_db(tmp_path / "repo-b")
    _insert_error(
        db_a,
        error_class="install_drift",
        summary="ImportError: cannot import name 'list_handoff_rows'",
        repo_instance_id="repo-a",
        package_name="workstate_handoff_mcp",
        package_version="0.4.0",
    )
    _insert_error(
        db_b,
        error_class="install_drift",
        summary="ImportError: cannot import name 'render_handoff'",
        repo_instance_id="repo-b",
        package_name="workstate_handoff_mcp",
        package_version="0.5.0",
    )

    result = errors_report(sources=[db_a, db_b])
    assert result["ok"] is True
    assert result["mode"] == "collect"
    clusters = _cluster_index(result["report"])
    drift = clusters[("install_drift", "workstate_handoff_mcp")]
    assert drift["row_count"] == 2
    assert drift["repo_instance_count"] == 2
    assert drift["package_version_range"] == ["0.4.0", "0.5.0"]


def test_collect_dedupes_db_and_its_own_export(tmp_path: Path) -> None:
    db = _seed_db(tmp_path / "repo")
    _insert_error(
        db,
        error_class="mcp_write_rejected",
        summary="close_slice rejected: missing sections",
        occurrence_count=3,
    )
    export = errors_export(db_path=db)
    assert export["ok"] is True
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in export["rows"]))

    merged = errors_report(sources=[db, bundle])
    assert merged["ok"] is True
    report = merged["report"]
    assert report["total_rows"] == 1
    assert report["total_occurrences"] == 3


def test_since_filters_older_rows(tmp_path: Path) -> None:
    db = _seed_db(tmp_path / "repo")
    _insert_error(
        db,
        error_class="cli_failure",
        summary="old failure",
        created_at="2026-05-01 10:00:00",
    )
    _insert_error(
        db,
        error_class="cli_failure",
        summary="new failure",
        created_at="2026-06-04 10:00:00",
    )

    result = errors_report(sources=[db], since="2026-06-01 00:00:00")
    assert result["ok"] is True
    assert result["report"]["total_rows"] == 1
    assert result["report"]["clusters"][0]["sample"]["summary"] == "new failure"


def test_build_errors_report_empty_rows() -> None:
    report = build_errors_report([])
    assert report["total_rows"] == 0
    assert report["clusters"] == []


# ---------------------------------------------------------------------------
# Export round-trip
# ---------------------------------------------------------------------------


def test_export_roundtrip_report_equivalence(tmp_path: Path) -> None:
    db = _seed_db(tmp_path / "repo")
    _insert_error(
        db,
        error_class="install_drift",
        summary="ImportError: cannot import name 'list_handoff_rows'",
        package_name="workstate_handoff_mcp",
        package_version="0.4.0",
        detail="Traceback (most recent call last): ...",
        task_ref="WS-ERRTEL-01",
    )
    _insert_error(db, error_class="env_misconfig", summary="dead cwd: errno 2")

    export = errors_export(db_path=db)
    assert export["ok"] is True
    assert len(export["rows"]) == 2
    bundle = tmp_path / "bundle.jsonl"
    bundle.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in export["rows"]))

    from_db = errors_report(sources=[db])
    from_bundle = errors_report(sources=[bundle])
    assert from_db["report"] == from_bundle["report"]


def test_export_since_filter(tmp_path: Path) -> None:
    db = _seed_db(tmp_path / "repo")
    _insert_error(
        db,
        error_class="cli_failure",
        summary="old failure",
        created_at="2026-05-01 10:00:00",
    )
    _insert_error(
        db,
        error_class="cli_failure",
        summary="new failure",
        created_at="2026-06-04 10:00:00",
    )
    export = errors_export(db_path=db, since="2026-06-01 00:00:00")
    assert export["ok"] is True
    assert [row["summary"] for row in export["rows"]] == ["new failure"]


# ---------------------------------------------------------------------------
# Local mode + source errors
# ---------------------------------------------------------------------------


def test_local_mode_resolves_primary_db(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    db = _seed_db(repo)
    _insert_error(db, error_class="other", summary="something odd")

    result = errors_report(cwd=repo)
    assert result["ok"] is True
    assert result["mode"] == "local"
    assert result["report"]["total_rows"] == 1


def test_missing_source_reports_error(tmp_path: Path) -> None:
    result = errors_report(sources=[tmp_path / "nope.db"])
    assert result["ok"] is False
    assert "nope.db" in result["error"]


# ---------------------------------------------------------------------------
# CLI subcommands
# ---------------------------------------------------------------------------


def test_errors_report_cli_subprocess(tmp_path: Path) -> None:
    db = _seed_db(tmp_path / "repo")
    _insert_error(
        db,
        error_class="install_drift",
        summary="ImportError: cannot import name 'list_handoff_rows'",
        package_name="workstate_handoff_mcp",
        package_version="0.4.0",
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_handoff_mcp",
            "errors-report",
            "--source",
            str(db),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["report"]["clusters"][0]["error_class"] == "install_drift"


def test_errors_export_cli_writes_jsonl(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    db = _seed_db(repo)
    _insert_error(db, error_class="cli_failure", summary="make review-run exited 1")
    out = tmp_path / "bundle.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "workstate_handoff_mcp",
            "errors-export",
            "--output",
            str(out),
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["ok"] is True
    assert payload["exported"] == 1
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert lines[0]["error_class"] == "cli_failure"


# ---------------------------------------------------------------------------
# Version-range ordering (REV-B-002)
# ---------------------------------------------------------------------------


def test_version_range_orders_numerically_not_lexicographically() -> None:
    assert _version_range(["0.10.0", "0.9.0"]) == ["0.9.0", "0.10.0"]
    assert _version_range(["0.9.0", "0.10.0", "0.2.1"]) == ["0.2.1", "0.10.0"]
    assert _version_range(["1.0.0"]) == ["1.0.0", "1.0.0"]
    assert _version_range([]) is None
    # Pre-releases order BELOW their bare release (REV-D-001, PEP 440-ish)…
    assert _version_range(["1.2.0", "1.2.0rc1"]) == ["1.2.0rc1", "1.2.0"]
    # …while extra numeric segments still extend upward.
    assert _version_range(["0.9", "0.9.1"]) == ["0.9", "0.9.1"]


def test_cluster_version_range_with_multi_digit_versions(tmp_path: Path) -> None:
    db = _seed_db(tmp_path / "repo")
    for version in ("0.10.0", "0.9.0"):
        _insert_error(
            db,
            error_class="install_drift",
            summary=f"drift at {version}",
            package_name="workstate_handoff_mcp",
            package_version=version,
        )
    result = errors_report(sources=[db])
    drift = _cluster_index(result["report"])[("install_drift", "workstate_handoff_mcp")]
    assert drift["package_version_range"] == ["0.9.0", "0.10.0"]
